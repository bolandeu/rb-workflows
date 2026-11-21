"""A task that outlives the call: `--submit` starts it, `--task` asks how it is going.

Both sources can be slow — Arsenkin queues by design, our own `serp` CLI needs minutes for thirty queries — so the
caller must be able to place the work and come back. The registry here is the local record of such tasks: one JSON file
per task, written by the process that runs it and read by whoever polls.

Nothing secret is kept: the request (queries, engines, depth) and the report, which the caller already owns.
"""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import time
from collections.abc import Mapping
from pathlib import Path

import platformdirs

__all__ = [
    "KINDS",
    "new_id",
    "poll_local",
    "read",
    "run_task",
    "running",
    "spawn",
    "state_dir",
    "submit_local",
    "update",
    "wait_local",
    "write",
]

APP = "rb-workflows"
PENDING = frozenset({"submitted", "running", "process", "queue"})


def state_dir(base: Path | None = None) -> Path:
    """Where the task files live: `--dir`, `RB_WORKFLOWS_STATE_DIR`, or the user state dir of this CLI."""
    if base is not None:
        return base
    if custom := os.environ.get("RB_WORKFLOWS_STATE_DIR"):
        return Path(custom).expanduser()
    return platformdirs.user_state_path(APP, appauthor=False) / "tasks"


STORE_LOCAL = "local"


def new_id(store: str = STORE_LOCAL) -> str:
    """A namespaced id: `<store>:<sortable time>-<random>`, so a reader knows where the state lives."""
    return f"{store}:{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(4)}"


def split_id(task_id: str) -> tuple[str, str]:
    """`local:2026…` → `("local", "2026…")`; an id without a store is a local one (the CLI's own older files)."""
    store, _, rest = task_id.partition(":")
    return (store, rest) if rest and ":" not in rest else (STORE_LOCAL, task_id)


def _path(task_id: str, base: Path | None = None) -> Path:
    store, name = split_id(task_id)
    return state_dir(base) / f"{store}-{name}.json"


def write(task: Mapping, base: Path | None = None) -> dict:
    """Replace the task's file (created with mode 0600: a report may hold nothing secret, but it is the caller's)."""
    directory = state_dir(base)
    directory.mkdir(parents=True, exist_ok=True)
    path = _path(str(task["task_id"]), base)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(dict(task), ensure_ascii=False), encoding="utf-8")
    os.chmod(temp, 0o600)
    temp.replace(path)
    return dict(task)


def update(task_id: str, base: Path | None = None, **fields: object) -> dict:
    """Merge `fields` into the task file and write it back."""
    task = read(task_id, base)
    task.update(fields)
    return write(task, base)


def read(task_id: str, base: Path | None = None) -> dict:
    """The task by id; `FileNotFoundError` when the id is unknown (the caller's mistake, not a failure of a task)."""
    path = _path(task_id, base)
    if not path.is_file():
        raise FileNotFoundError(f"no task '{task_id}' in {state_dir(base)}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:  # a half-written file of a killed worker: say which task, not a JSON traceback
        raise ValueError(f"task '{task_id}' is not readable: {exc}") from exc


def running(task_id: str, base: Path | None = None) -> bool:
    """Whether the task has not finished yet (a terminal state is `done` or `error`)."""
    return read(task_id, base).get("state") in PENDING


def spawn(task_id: str, base: Path | None = None) -> int:
    """Start the worker for a task in its own session, so it outlives the submitting call; returns its pid.

    The worker is this very CLI (`top --run <id>`), which is what makes a submitted `serp` task behave like a submitted
    Arsenkin one: the caller gets an id at once and reads the report later.
    """
    directory = state_dir(base)
    directory.mkdir(parents=True, exist_ok=True)
    log = (directory / f"{task_id}.log").open("ab")
    argv = [sys.executable, "-m", "rb_workflows.cli", "top", "--run", task_id]
    if base is not None:
        argv += ["--tasks-dir", str(base)]
    process = subprocess.Popen(  # noqa: S603 - the command is ours, the id is generated here
        argv, stdout=log, stderr=log, stdin=subprocess.DEVNULL, start_new_session=True
    )
    return process.pid


# --- the generic layer ------------------------------------------------------------------------------------------
# A command says only what to run (`kind`) and with what arguments; where the task lives and who executes it is the
# store's business. Today the store is a file plus a worker of this CLI; a FastAPI or MCP front end replaces the store
# and keeps the same three calls: `submit`, `poll`, `wait`.

KINDS: dict[str, str] = {"top": "rb_workflows.top:collect", "semantics": "rb_workflows.semantics:collect"}


def _import(target: str):
    """`"module:function"` → the function, imported late so a missing optional dep of one job cannot break another."""
    import importlib

    module_name, _, attribute = target.partition(":")
    return getattr(importlib.import_module(module_name), attribute)


def submit_local(kind: str, args: Mapping, base: Path | None = None) -> dict:
    """Place a job of `kind` that this machine runs; returns the id and how to read it later, at once."""
    if kind not in KINDS:
        raise ValueError(f"unknown job '{kind}'; known: {', '.join(sorted(KINDS))}")
    task_id = new_id()
    write({"task_id": task_id, "kind": kind, "source": "local", "state": "running", "args": dict(args)}, base)
    spawn(task_id, base)
    return {
        "ok": True,
        "task_id": task_id,
        "kind": kind,
        "state": "running",
        "poll_after_s": 15,
        "poll": f"wf task {task_id} --json",
    }


def poll_local(task_id: str, base: Path | None = None) -> dict:
    """The stored state of a local job, or its report once it is done."""
    task = read(task_id, base)
    if task.get("state") == "done":
        return {**task["report"], "state": "done", "task_id": task_id}
    if task.get("state") == "error":
        return {"ok": False, "state": "error", "task_id": task_id, "error": task.get("error")}
    return {"ok": True, "task_id": task_id, "kind": task.get("kind"), "state": "running", "retry_after_s": 15}


def run_task(task_id: str, base: Path | None = None) -> None:
    """The worker body: run the job named in the task file and store the report (or the error)."""
    import asyncio

    task = read(task_id, base)
    try:
        job = _import(KINDS[task["kind"]])
        report = asyncio.run(job(**task["args"]))
        update(task_id, base, state="done", report=report)
    except Exception as exc:  # a worker must leave a readable state, not a traceback in a log
        update(task_id, base, state="error", error=f"{type(exc).__name__}: {exc}"[:400])


def wait_local(task_id: str, base: Path | None = None, *, poll_interval: float = 5.0) -> dict:
    """Poll a local job until it is done or failed."""
    import time as _time

    while True:
        answer = poll_local(task_id, base)
        if answer.get("state") in {"done", "error"}:
            return answer
        _time.sleep(poll_interval)
