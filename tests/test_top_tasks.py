"""A `top` task that outlives the call: `--submit` places it, `--task` reads it, `--wait` blocks on it.

Both sources can be slow (Arsenkin queues; our own `serp` CLI needs minutes for thirty queries), so the contract is
two calls: submit answers at once with an id and when to come back, poll answers with either a state or the report.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from rb_workflows import tasks, top

REQUEST = {"queries": ["диван"], "se": [{"type": 1, "region": 213}], "depth": 10}


def _run(coroutine):
    return asyncio.run(coroutine)


async def _fake_collect(request, **kwargs):
    body = request.get("request", request) if isinstance(request, dict) else request
    return top.build_report(
        request=top.normalise(body), source=kwargs.get("source", "serp"), collect=[[["https://a/1"]]]
    )


def test_submit_starts_a_task_and_answers_at_once(monkeypatch, tmp_path) -> None:
    spawned: list[str] = []
    monkeypatch.setattr(tasks, "spawn", lambda task_id, base=None: spawned.append(task_id) or 42)

    answer = _run(top.submit(REQUEST, source="serp", tasks_dir=tmp_path))

    assert answer["ok"] is True and answer["state"] == "running"
    assert answer["poll_after_s"] == 15
    assert answer["poll"] == f"wf task {answer['task_id']} --json"
    assert spawned == [answer["task_id"]]
    stored = tasks.read(answer["task_id"], tmp_path)
    assert (stored["state"], stored["kind"], stored["request"]["request"]["queries"]) == ("running", "top", ["диван"])


def test_poll_of_a_running_task_reports_the_state_and_when_to_come_back(tmp_path) -> None:
    task = tasks.write(
        {"task_id": "t1", "source": "serp", "state": "running", "request": top.normalise(REQUEST)}, tmp_path
    )
    answer = _run(top.poll(task["task_id"], tasks_dir=tmp_path))
    assert (answer["ok"], answer["state"], answer["retry_after_s"]) == (True, "running", 15)


def test_a_finished_task_is_read_back_as_the_full_report(tmp_path) -> None:
    report = top.build_report(request=top.normalise(REQUEST), source="serp", collect=[[["https://a/1"]]])
    tasks.write({"task_id": "t2", "source": "serp", "state": "done", "report": report}, tmp_path)

    answer = _run(top.poll("t2", tasks_dir=tmp_path))

    assert answer["state"] == "done"
    assert answer["result"]["result"]["collect"] == [[["https://a/1"]]]
    assert answer["summary"]["top"][0]["url"] == "https://a/1"


def test_a_failed_task_reports_its_error_and_is_not_ok(tmp_path) -> None:
    tasks.write({"task_id": "t3", "source": "serp", "state": "error", "error": "RuntimeError: boom"}, tmp_path)
    answer = _run(top.poll("t3", tasks_dir=tmp_path))
    assert (answer["ok"], answer["state"], answer["error"]) == (False, "error", "RuntimeError: boom")


def test_an_unknown_task_is_the_callers_mistake(tmp_path) -> None:
    with pytest.raises(FileNotFoundError, match="no task 'nope'"):
        _run(top.poll("nope", tasks_dir=tmp_path))


def test_the_worker_finishes_the_task_and_stores_the_report(monkeypatch, tmp_path) -> None:
    tasks.write(
        {"task_id": "t4", "kind": "top", "state": "running", "request": {"request": top.normalise(REQUEST)}}, tmp_path
    )
    monkeypatch.setattr(tasks, "_job", lambda target: _fake_collect)

    tasks.run_task("t4", tmp_path)

    stored = tasks.read("t4", tmp_path)
    assert stored["state"] == "done"
    assert stored["report"]["result"]["result"]["collect"] == [[["https://a/1"]]]


def test_a_worker_that_crashes_leaves_an_error_task_not_a_traceback(monkeypatch, tmp_path) -> None:
    async def boom(request: dict):
        raise RuntimeError("serp is not installed")

    tasks.write(
        {"task_id": "t5", "kind": "top", "state": "running", "request": {"request": top.normalise(REQUEST)}}, tmp_path
    )
    monkeypatch.setattr(tasks, "_job", lambda target: boom)

    tasks.run_task("t5", tmp_path)

    stored = tasks.read("t5", tmp_path)
    assert stored["state"] == "error"
    assert "serp is not installed" in stored["error"]


def test_submitting_to_arsenkin_only_places_the_task_and_remembers_the_remote_id(monkeypatch, tmp_path) -> None:
    placed: list[dict] = []

    class FakeStore:
        def create(self, kind: str, request: dict) -> dict:
            placed.append(request)
            return {"task_id": "arsenkin:top:777", "kind": kind, "state": "running"}

    monkeypatch.setattr(top.tasks, "default_store", lambda base=None: FakeStore())
    answer = _run(top.submit(REQUEST, source="arsenkin", tasks_dir=tmp_path))

    assert answer["state"] == "running"
    assert answer["task_id"] == "arsenkin:top:777"  # their id is the task; nothing local is written
    assert answer["poll"] == "wf task arsenkin:top:777 --json"
    assert placed[0]["request"]["queries"] == ["диван"]
    assert placed[0]["store"] == "arsenkin"


def test_polling_an_arsenkin_task_reads_the_remote_state(monkeypatch) -> None:
    class FakeStore:
        def __init__(self, task: dict) -> None:
            self.task = task

        def get(self, task_id: str) -> dict:
            return self.task

    monkeypatch.setattr(
        top.tasks, "default_store", lambda base=None: FakeStore({"state": "process", "progress": 40, "kind": "top"})
    )
    answer = _run(top.poll("arsenkin:top:777"))
    assert (answer["state"], answer["progress"]) == ("process", 40)

    report = top.build_report(request=top.normalise(REQUEST), source="arsenkin", collect=[[["https://a/9"]]])
    monkeypatch.setattr(
        top.tasks, "default_store", lambda base=None: FakeStore({"state": "done", "kind": "top", "report": report})
    )
    answer = _run(top.poll("arsenkin:top:777"))
    assert answer["state"] == "done"
    assert answer["result"]["result"]["collect"] == [[["https://a/9"]]]


def test_wait_blocks_until_the_task_finishes(monkeypatch, tmp_path) -> None:
    states = iter(["running", "running", "done"])
    answers = iter(
        [
            {"ok": True, "state": "running"},
            {"ok": True, "state": "running"},
            {"ok": True, "state": "done", "summary": {"top": []}},
        ]
    )

    async def fake_poll(task_id, **kwargs):
        return next(answers)

    monkeypatch.setattr(top, "poll", fake_poll)
    monkeypatch.setattr(top, "SLEEP", 0)
    assert _run(top.wait("t7", tasks_dir=tmp_path))["state"] == "done"
    assert next(states) == "running"  # the iterator is untouched: the test only pins the call sequence


def test_the_cli_submits_and_then_reads_the_task(monkeypatch, tmp_path, capsys) -> None:
    from rb_workflows import cli

    monkeypatch.setattr(tasks, "spawn", lambda task_id, base=None: 1)
    code = cli.main(
        ["top", "--input", str(_request_file(tmp_path)), "--submit", "--tasks-dir", str(tmp_path), "--json"]
    )
    submitted = json.loads(capsys.readouterr().out)
    assert (code, submitted["state"]) == (0, "running")

    code = cli.main(["top", "--task", submitted["task_id"], "--tasks-dir", str(tmp_path), "--json"])
    polled = json.loads(capsys.readouterr().out)
    assert (code, polled["state"]) == (0, "running")


def _request_file(tmp_path: Path) -> Path:
    path = tmp_path / "top.json"
    path.write_text(json.dumps(REQUEST), encoding="utf-8")
    return path


def test_the_cli_refuses_to_submit_a_serp_task_without_an_input(tmp_path, capsys) -> None:
    from rb_workflows import cli

    with pytest.raises(SystemExit) as exit_info:
        cli.main(["top", "--submit"])
    assert exit_info.value.code == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "INVALID_ARGUMENTS"
