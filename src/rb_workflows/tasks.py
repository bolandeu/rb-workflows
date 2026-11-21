"""A task that outlives the call: `--submit` places it, `wf task <id>` reads it.

Every command that leaves the machine can be slow — Arsenkin queues by design, our own `serp` CLI needs minutes for
thirty queries — so the caller places the work and comes back. The task id says where the state lives:

    local:<kind>:20261121-181455-18cfc414   a file on this machine, run by a worker of this CLI
    arsenkin:<kind>:32070249                the service's own task; we only ask how it is going

`kind` (`top`, `semantics`) is part of the id, so a reader needs nothing but the id — which is exactly what a FastAPI
or MCP front end has. A store implements four calls (`create`, `get`, `save`, `cancel`); the CLI uses files, a service
would use its database, and nothing else changes.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import secrets
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import platformdirs

__all__ = [
    "KINDS",
    "REPORTS",
    "ArsenkinStore",
    "CompositeStore",
    "FileStore",
    "Store",
    "cancel",
    "default_store",
    "gc",
    "get",
    "new_id",
    "read",
    "run_task",
    "spawn",
    "split_id",
    "state_dir",
    "submit",
    "update",
    "wait",
    "write",
]

APP = "rb-workflows"
STORES = ("local", "arsenkin")
KINDS: dict[str, str] = {"top": "rb_workflows.top:collect", "semantics": "rb_workflows.semantics:collect"}
# How a kind turns the service's own payload into the report the caller sees; filled by the modules that own the kind.
REPORTS: dict[str, Callable[[dict], dict]] = {}

# --- ids and their files ----------------------------------------------------------------------------------------


def state_dir(base: Path | None = None) -> Path:
    """Where the local task files live: the argument, `RB_WORKFLOWS_STATE_DIR`, or the user state dir of this CLI."""
    if base is not None:
        return base
    if custom := os.environ.get("RB_WORKFLOWS_STATE_DIR"):
        return Path(custom).expanduser()
    return platformdirs.user_state_path(APP, appauthor=False) / "tasks"


def new_id(kind: str, store: str = "local") -> str:
    """`<store>:<kind>:<sortable time>-<random>` — self-describing, so a reader never guesses."""
    return f"{store}:{kind}:{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(4)}"


def split_id(task_id: str) -> tuple[str, str, str]:
    """`local:top:2026…` → `("local", "top", "2026…")`; an id of an older file has no kind and is read as it is."""
    parts = task_id.split(":", 2)
    if len(parts) == 3:
        return parts[0], parts[1], parts[2]
    if len(parts) == 2 and parts[0] in STORES:
        return parts[0], "", parts[1]
    return "local", "", task_id


def gc(base: Path | None = None, *, older_than_days: float = 7) -> dict:
    """Remove the finished task files older than `older_than_days`: a store of reports is not a history to keep.

    A running task is never touched, whatever its age. The answer says what was removed, so a caller can log it.
    """
    directory = state_dir(base)
    if not directory.is_dir():
        return {"removed": [], "kept": 0}
    cutoff = time.time() - older_than_days * 86400
    removed, kept = [], 0
    for path in sorted(directory.glob("local-*.json")):
        try:
            task = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            continue
        if task.get("state") in {"done", "error", "cancelled"} and path.stat().st_mtime < cutoff:
            path.unlink(missing_ok=True)
            removed.append(str(task.get("task_id")))
        else:
            kept += 1
    return {"removed": removed, "kept": kept}


def _path(task_id: str, base: Path | None = None) -> Path:
    store, kind, name = split_id(task_id)
    return state_dir(base) / f"{store}-{kind + '-' if kind else ''}{name}.json"


def write(task: Mapping, base: Path | None = None) -> dict:
    """Replace the task's file (mode 0600: the report is the caller's, and the file may hold a request)."""
    directory = state_dir(base)
    directory.mkdir(parents=True, exist_ok=True)
    path = _path(str(task["task_id"]), base)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(dict(task), ensure_ascii=False), encoding="utf-8")
    os.chmod(temp, 0o600)
    temp.replace(path)
    return dict(task)


def read(task_id: str, base: Path | None = None) -> dict:
    """The stored task; `FileNotFoundError` when the id is unknown (the caller's mistake, not a task's failure)."""
    path = _path(task_id, base)
    if not path.is_file():
        raise FileNotFoundError(f"no task '{task_id}' in {state_dir(base)}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:  # a half-written file of a killed worker: name the task, not a JSON traceback
        raise ValueError(f"task '{task_id}' is not readable: {exc}") from exc


def update(task_id: str, base: Path | None = None, **fields: object) -> dict:
    """Merge `fields` into the task file and write it back."""
    task = read(task_id, base)
    task.update(fields)
    return write(task, base)


# --- the worker ------------------------------------------------------------------------------------------------


def spawn(task_id: str, base: Path | None = None) -> int:
    """Run the worker for a local task in its own session, so it outlives the submitting call; returns its pid."""
    directory = state_dir(base)
    directory.mkdir(parents=True, exist_ok=True)
    log = (directory / f"{task_id.replace(':', '-')}.log").open("ab")
    argv = [sys.executable, "-m", "rb_workflows.cli", "task", "--run", task_id]
    if base is not None:
        argv += ["--tasks-dir", str(base)]
    process = subprocess.Popen(  # noqa: S603 - the command is ours, the id is generated here
        argv, stdout=log, stderr=log, stdin=subprocess.DEVNULL, start_new_session=True
    )
    return process.pid


def stop_worker(pid: object) -> bool:
    """SIGTERM the worker of a local task; True when it was alive. A missing process is not an error."""
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, signal.SIGTERM)
    except (OSError, ValueError):
        return False
    return True


def _job(target: str):
    """`"module:function"` → the function, imported late so a missing optional dep breaks one job, not the module."""
    module_name, _, attribute = target.partition(":")
    return getattr(importlib.import_module(module_name), attribute)


def run_task(task_id: str, base: Path | None = None) -> None:
    """The worker body: run the job the task names and store the report — or the error, which the caller can read."""
    task = read(task_id, base)
    try:
        report = asyncio.run(_job(KINDS[task["kind"]])(task["request"]))
        result: dict = {"state": "done", "report": report}
    except Exception as exc:  # a worker must leave a readable state, not a traceback in a log
        result = {"state": "error", "error": f"{type(exc).__name__}: {exc}"[:400]}
    # a task cancelled while the worker was busy keeps its state: the caller asked for it, the report is late news
    if read(task_id, base).get("state") == "cancelled":
        return
    update(task_id, base, **result)


# --- stores ----------------------------------------------------------------------------------------------------


@runtime_checkable
class Store(Protocol):
    """Four calls a front end needs; a service swaps the file store for its own database without touching a command."""

    async def create(self, kind: str, request: Mapping) -> dict: ...
    async def get(self, task_id: str) -> dict: ...
    async def save(self, task: Mapping) -> dict: ...
    async def cancel(self, task_id: str) -> dict: ...


class FileStore:
    """One JSON file per task: the CLI's store. A created task is `running` and its worker is this same CLI."""

    def __init__(self, base: Path | None = None) -> None:
        self.base = base

    async def create(self, kind: str, request: Mapping) -> dict:
        task_id = new_id(kind)
        return write({"task_id": task_id, "kind": kind, "state": "running", "request": dict(request)}, self.base)

    async def get(self, task_id: str) -> dict:
        return read(task_id, self.base)

    async def save(self, task: Mapping) -> dict:
        return write(task, self.base)

    async def cancel(self, task_id: str) -> dict:
        """Mark the task cancelled first (so a finishing worker keeps its hands off) and then stop the worker."""
        task = read(task_id, self.base)
        if task.get("state") in {"done", "error", "cancelled"}:
            return task
        task = update(task_id, self.base, state="cancelled", cancelled_at=time.strftime("%Y-%m-%dT%H:%M:%SZ"))
        stop_worker(task.get("pid"))
        return task


class ArsenkinStore:
    """The service is the state: `create` places their task, `get` asks check/get, `cancel` deletes it."""

    def __init__(self, token: str | None = None) -> None:
        self.token = token

    def _secret(self) -> str:
        from .credentials import binding

        secret = self.token or binding("arsenkin")[0]
        if not secret:
            raise RuntimeError("no token for 'arsenkin': `token set arsenkin`")
        return secret

    def _client(self):
        import httpx

        return httpx.AsyncClient(timeout=60.0, trust_env=False)

    async def create(self, kind: str, request: Mapping) -> dict:
        from .services import arsenkin

        data = request.get("request") or request

        async def place() -> int:
            async with self._client() as client:
                return await arsenkin.set_task(client, self._secret(), dict(data))

        remote = await place()
        task_id = new_id(kind, store="arsenkin").rsplit(":", 1)[0] + f":{remote}"
        return {"task_id": task_id, "kind": kind, "state": "running", "remote_task_id": remote}

    async def get(self, task_id: str) -> dict:
        from .services import arsenkin

        _, kind, name = split_id(task_id)
        remote = int(name)

        async def read_remote() -> dict:
            async with self._client() as client:
                secret = self._secret()
                status = await arsenkin.task_state(client, secret, remote)
                state = str(status.get("status") or "").lower()
                if state in arsenkin.DONE:
                    payload = await arsenkin.task_result(client, secret, remote)
                    build = REPORTS.get(kind)
                    report = build(payload) if build else {"raw": payload}
                    return {"state": "done", "report": report, "progress": 100}
                if state in arsenkin.FAILED:
                    return {"state": "error", "error": f"arsenkin task {remote} ended as '{state}'", "progress": None}
                return {"state": "process", "progress": status.get("progress")}

        return await read_remote()

    async def save(self, task: Mapping) -> dict:
        return dict(task)  # nothing to write: their task is the record

    async def cancel(self, task_id: str) -> dict:
        from .services import arsenkin

        _, _, name = split_id(task_id)

        async def drop() -> None:
            async with self._client() as client:
                await arsenkin.cancel_task(client, self._secret(), int(name))

        try:
            await drop()
        except Exception as exc:  # deleting a finished task is not an error for the caller
            return {"task_id": task_id, "state": "cancelled", "warning": f"{type(exc).__name__}: {exc}"[:200]}
        return {"task_id": task_id, "state": "cancelled"}


class CompositeStore:
    """Routes an id to the store that owns it: `arsenkin:…` to the service, anything else to the files."""

    def __init__(self, local: Store | None = None, remote: Store | None = None) -> None:
        self.local = local or FileStore()
        self.remote = remote or ArsenkinStore()

    def _of(self, task_id: str) -> Store:
        store, _, _ = split_id(task_id)
        return self.remote if store == "arsenkin" else self.local

    async def create(self, kind: str, request: Mapping) -> dict:
        store = str(request.get("store") or "local")
        return await (self.remote if store == "arsenkin" else self.local).create(kind, request)

    async def get(self, task_id: str) -> dict:
        return await self._of(task_id).get(task_id)

    async def save(self, task: Mapping) -> dict:
        return await self._of(str(task["task_id"])).save(task)

    async def cancel(self, task_id: str) -> dict:
        return await self._of(task_id).cancel(task_id)


def default_store(base: Path | None = None) -> Store:
    """The store this process uses: files here, Arsenkin for their own ids."""
    return CompositeStore(FileStore(base), ArsenkinStore())


# --- the calls every front end makes ---------------------------------------------------------------------------


async def submit(kind: str, request: Mapping, *, store: Store | None = None) -> dict:
    """Place a job and answer at once with its id, the state and the call that reads it later."""
    if kind not in KINDS:
        raise ValueError(f"unknown job '{kind}'; known: {', '.join(sorted(KINDS))}")
    store = store or default_store()
    task = await store.create(kind, request)
    task_id = str(task["task_id"])
    if task_id.startswith("local:"):
        pid = spawn(task_id, getattr(store, "base", None))
        await store.save({**task, "pid": pid})
    return {
        "ok": True,
        "task_id": task_id,
        "kind": kind,
        "state": "running",
        "poll_after_s": 15,
        "poll": f"wf task {task_id} --json",
    }


async def get(task_id: str, *, store: Store | None = None) -> dict:
    """The state of a job — or its report once it is done; a failed or cancelled job is `ok: false` with the reason."""
    store = store or default_store()
    task = await store.get(task_id)
    state = task.get("state")
    if state == "done":
        return {**task.get("report", {}), "state": "done", "task_id": task_id}
    if state in {"error", "cancelled"}:
        return {"ok": False, "state": state, "task_id": task_id, "error": task.get("error") or f"task {state}"}
    return {
        "ok": True,
        "task_id": task_id,
        "kind": split_id(task_id)[1],
        "state": state or "running",
        "progress": task.get("progress"),
        "retry_after_s": 15,
    }


async def wait(task_id: str, *, store: Store | None = None, poll_interval: float = 5.0) -> dict:
    """Poll until the job is done, failed or cancelled."""
    while True:
        answer = await get(task_id, store=store)
        if answer.get("state") in {"done", "error", "cancelled"}:
            return answer
        await asyncio.sleep(poll_interval)


async def cancel(task_id: str, *, store: Store | None = None) -> dict:
    """Stop the job: their task is deleted, a local one is marked (its worker is not interrupted — the answer says so)."""
    store = store or default_store()
    task = await store.cancel(task_id)
    answer: dict[str, Any] = {"ok": True, "task_id": task_id, "state": task.get("state", "cancelled")}
    if task.get("warning"):
        answer["warning"] = task["warning"]
    return answer
