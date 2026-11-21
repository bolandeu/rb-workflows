"""The generic task layer: any slow job — `semantics`, `top` — is placed now and read later, the same way.

These tests pin the contract a FastAPI or MCP front end needs: `submit` answers without waiting, `poll` answers with a
state or a report, a failed job is a value, and the worker is the same one for every kind.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rb_workflows import tasks


def _run(coroutine):
    import asyncio

    return asyncio.run(coroutine)


def _request_file(tmp_path: Path, payload: dict) -> Path:
    path = tmp_path / "in.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_submit_answers_at_once_and_the_worker_is_started(monkeypatch, tmp_path) -> None:
    spawned: list[str] = []
    monkeypatch.setattr(tasks, "spawn", lambda task_id, base=None: spawned.append(task_id) or 1)

    answer = _run(tasks.submit("semantics", {"urls": ["https://a.example/"]}, store=tasks.FileStore(tmp_path)))

    assert (answer["ok"], answer["state"], answer["kind"]) == (True, "running", "semantics")
    assert answer["poll"] == f"wf task {answer['task_id']} --json"
    assert spawned == [answer["task_id"]]
    assert tasks.read(answer["task_id"], tmp_path)["request"] == {"urls": ["https://a.example/"]}


def test_an_unknown_kind_is_refused_before_a_file_is_written(tmp_path) -> None:
    with pytest.raises(ValueError, match="unknown job 'nope'; known: semantics, top"):
        _run(tasks.submit("nope", {}, store=tasks.FileStore(tmp_path)))


def test_poll_reports_running_then_the_report(tmp_path) -> None:
    tasks.write({"task_id": "t1", "kind": "semantics", "state": "running", "request": {}}, tmp_path)
    running = tasks.get("t1", store=tasks.FileStore(tmp_path))
    assert (running["ok"], running["state"], running["retry_after_s"]) == (True, "running", 15)

    tasks.update("t1", tmp_path, state="done", report={"ok": True, "urls": []})
    done = tasks.get("t1", store=tasks.FileStore(tmp_path))
    assert (done["state"], done["urls"]) == ("done", [])


def test_a_failed_job_is_a_value_with_its_error(tmp_path) -> None:
    tasks.write({"task_id": "t2", "kind": "semantics", "state": "error", "error": "RuntimeError: boom"}, tmp_path)
    answer = tasks.get("t2", store=tasks.FileStore(tmp_path))
    assert (answer["ok"], answer["state"], answer["error"]) == (False, "error", "RuntimeError: boom")


def test_the_worker_runs_the_job_named_in_the_task_file(monkeypatch, tmp_path) -> None:
    calls: list[dict] = []

    async def fake_job(request: dict):
        calls.append(request)
        return {"ok": True, "echo": request}

    monkeypatch.setattr(tasks, "_job", lambda target: fake_job)
    tasks.write(
        {"task_id": "t3", "kind": "semantics", "state": "running", "request": {"urls": ["https://a.example/"]}},
        tmp_path,
    )

    tasks.run_task("t3", tmp_path)

    stored = tasks.read("t3", tmp_path)
    assert stored["state"] == "done"
    assert stored["report"]["echo"] == {"urls": ["https://a.example/"]}
    assert calls == [{"urls": ["https://a.example/"]}]


def test_a_crashing_job_leaves_an_error_task(monkeypatch, tmp_path) -> None:
    async def boom(request: dict):
        raise RuntimeError("service exploded")

    monkeypatch.setattr(tasks, "_job", lambda target: boom)
    tasks.write({"task_id": "t4", "kind": "semantics", "state": "running", "request": {}}, tmp_path)

    tasks.run_task("t4", tmp_path)

    stored = tasks.read("t4", tmp_path)
    assert stored["state"] == "error"
    assert "service exploded" in stored["error"]


def test_wait_returns_after_the_first_terminal_state(monkeypatch, tmp_path) -> None:
    tasks.write({"task_id": "t5", "kind": "semantics", "state": "done", "report": {"ok": True}}, tmp_path)
    assert tasks.wait("t5", store=tasks.FileStore(tmp_path), poll_interval=0)["state"] == "done"


def test_the_cli_reads_a_task_and_exits_by_its_state(monkeypatch, tmp_path, capsys) -> None:
    from rb_workflows import cli

    tasks.write({"task_id": "t6", "kind": "semantics", "state": "running", "request": {}}, tmp_path)
    assert cli.main(["task", "t6", "--tasks-dir", str(tmp_path), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "running"

    tasks.update("t6", tmp_path, state="error", error="boom")
    assert cli.main(["task", "t6", "--tasks-dir", str(tmp_path), "--json"]) == 1


def test_the_cli_submits_a_semantics_job(monkeypatch, tmp_path, capsys) -> None:
    from rb_workflows import cli

    monkeypatch.setattr(tasks, "spawn", lambda task_id, base=None: 1)
    code = cli.main(["semantics", "--urls", "https://a.example/", "--submit", "--tasks-dir", str(tmp_path), "--json"])
    answer = json.loads(capsys.readouterr().out)
    assert (code, answer["kind"], answer["state"]) == (0, "semantics", "running")
