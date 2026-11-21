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

__all__ = ["new_id", "read", "running", "spawn", "state_dir", "update", "write"]

APP = "rb-workflows"
PENDING = frozenset({"submitted", "running", "process", "queue"})


def state_dir(base: Path | None = None) -> Path:
    """Where the task files live: `--dir`, `RB_WORKFLOWS_STATE_DIR`, or the user state dir of this CLI."""
    if base is not None:
        return base
    if custom := os.environ.get("RB_WORKFLOWS_STATE_DIR"):
        return Path(custom).expanduser()
    return platformdirs.user_state_path(APP, appauthor=False) / "tasks"


def new_id() -> str:
    """A short, sortable id: time prefix plus four random bytes (two submits in the same second do not collide)."""
    return f"{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(4)}"


def _path(task_id: str, base: Path | None = None) -> Path:
    return state_dir(base) / f"{task_id}.json"


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
