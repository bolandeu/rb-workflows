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
    with pytest.raises(ValueError, match="unknown job 'nope'; known: clustering, semantics, top"):
        _run(tasks.submit("nope", {}, store=tasks.FileStore(tmp_path)))


def test_poll_reports_running_then_the_report(tmp_path) -> None:
    tasks.write({"task_id": "t1", "kind": "semantics", "state": "running", "request": {}}, tmp_path)
    running = _run(tasks.get("t1", store=tasks.FileStore(tmp_path)))
    assert (running["ok"], running["state"], running["retry_after_s"]) == (True, "running", 15)

    tasks.update("t1", tmp_path, state="done", report={"ok": True, "urls": []})
    done = _run(tasks.get("t1", store=tasks.FileStore(tmp_path)))
    assert (done["state"], done["urls"]) == ("done", [])


def test_a_failed_job_is_a_value_with_its_error(tmp_path) -> None:
    tasks.write({"task_id": "t2", "kind": "semantics", "state": "error", "error": "RuntimeError: boom"}, tmp_path)
    answer = _run(tasks.get("t2", store=tasks.FileStore(tmp_path)))
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
    assert _run(tasks.wait("t5", store=tasks.FileStore(tmp_path), poll_interval=0))["state"] == "done"


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


def test_cancelling_a_running_task_stops_its_worker(monkeypatch, tmp_path) -> None:
    signals: list[tuple[int, int]] = []
    monkeypatch.setattr(tasks.os, "kill", lambda pid, sig: signals.append((pid, sig)))
    tasks.write({"task_id": "t8", "kind": "semantics", "state": "running", "request": {}, "pid": 4242}, tmp_path)

    answer = _run(tasks.cancel("t8", store=tasks.FileStore(tmp_path)))

    assert answer["state"] == "cancelled"
    assert signals == [(4242, 15)]  # SIGTERM, and only to the worker of this task
    assert tasks.read("t8", tmp_path)["state"] == "cancelled"


def test_cancelling_a_finished_task_changes_nothing(monkeypatch, tmp_path) -> None:
    signals: list[tuple[int, int]] = []
    monkeypatch.setattr(tasks.os, "kill", lambda pid, sig: signals.append((pid, sig)))
    tasks.write(
        {"task_id": "t9", "kind": "semantics", "state": "done", "request": {}, "pid": 1, "report": {}}, tmp_path
    )

    answer = _run(tasks.cancel("t9", store=tasks.FileStore(tmp_path)))

    assert answer["state"] == "done"
    assert signals == []


def test_a_worker_that_finishes_after_cancelling_does_not_overwrite_the_state(monkeypatch, tmp_path) -> None:
    async def slow_job(request: dict) -> dict:
        tasks.update("ta", tmp_path, state="cancelled")  # the caller cancelled while the job was running
        return {"ok": True, "late": True}

    monkeypatch.setattr(tasks, "_job", lambda target: slow_job)
    tasks.write({"task_id": "ta", "kind": "semantics", "state": "running", "request": {}}, tmp_path)

    tasks.run_task("ta", tmp_path)

    stored = tasks.read("ta", tmp_path)
    assert stored["state"] == "cancelled"
    assert "report" not in stored  # the late report is not news for a caller who cancelled


def test_gc_removes_finished_tasks_older_than_the_limit_and_keeps_the_rest(tmp_path) -> None:
    import os
    import time

    for task_id, state, age_days in (("t-old", "done", 30), ("t-new", "done", 0), ("t-run", "running", 30)):
        tasks.write({"task_id": task_id, "kind": "semantics", "state": state, "request": {}}, tmp_path)
        path = tasks.state_dir(tmp_path) / f"local-{task_id}.json"
        stamp = time.time() - age_days * 86400
        os.utime(path, (stamp, stamp))

    answer = tasks.gc(tmp_path, older_than_days=7)

    assert answer["removed"] == ["t-old"]
    assert answer["kept"] == 2  # a fresh one and a running one, whatever its age
    assert tasks.read("t-new", tmp_path)["state"] == "done"


def test_the_cli_says_an_unknown_id_in_the_envelope(tmp_path, capsys) -> None:
    from rb_workflows import cli

    with pytest.raises(SystemExit) as exit_info:
        cli.main(["task", "local:top:nope", "--tasks-dir", str(tmp_path), "--json"])
    assert exit_info.value.code == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"]["code"] == "INVALID_ARGUMENTS"
    assert "no task 'local:top:nope'" in payload["error"]["message"]
