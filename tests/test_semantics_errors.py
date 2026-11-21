"""What the wrapper promises about failures: written as the behaviour first, then made true.

Two rules the whole module follows:
- a failure of one URL is a value in the report, never an exception out of the batch;
- a failure of the service itself is an `ArsenkinError` with a message that names what happened (status, code, timeout),
  because the caller sees it inside `urls[].error` and has to understand it without a traceback.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from typing import Any

import httpx
import pytest

from rb_workflows import semantics, services
from rb_workflows.services import arsenkin


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False)


def _path(request: httpx.Request) -> str:
    return request.url.path.rsplit("/", 1)[-1]


def _answers(routes: Mapping[str, Any]) -> Callable[[httpx.Request], httpx.Response]:
    """A transport that answers per endpoint from a table: a dict is JSON, a callable is called for its response."""

    def handler(request: httpx.Request) -> httpx.Response:
        answer = routes[_path(request)]
        if isinstance(answer, httpx.Response):
            return answer
        if callable(answer):
            made = answer()
            return made if isinstance(made, httpx.Response) else httpx.Response(200, json=made)
        return httpx.Response(200, json=answer)

    return handler


# --- the service protocol: every refusal becomes one sentence ---------------------------------


async def test_a_spent_rate_limit_is_retried_and_then_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(arsenkin, "RATE_LIMIT_PAUSE", 0)
    monkeypatch.setattr(arsenkin, "RATE_LIMIT_TRIES", 3)
    calls = []

    def refuse() -> httpx.Response:
        calls.append(1)
        return httpx.Response(429, json={"code": "429"})

    async with _client(lambda request: refuse()) as client:
        with pytest.raises(arsenkin.ArsenkinError, match="rate limited"):
            await arsenkin.collect(client, "t", "https://a.example/")
    assert len(calls) == 3  # the tries are spent, not one silent attempt


async def test_an_http_error_names_the_status_and_a_slice_of_the_body() -> None:
    answer = httpx.Response(500, text="upstream is down for maintenance")
    async with _client(lambda request: answer) as client:
        with pytest.raises(arsenkin.ArsenkinError, match=re.escape("HTTP 500 for /set: upstream is down for"[:20])):
            await arsenkin.collect(client, "t", "https://a.example/")


async def test_a_service_level_error_code_is_reported_with_its_message() -> None:
    answer = httpx.Response(200, json={"code": "AUTH_ERROR", "msg": "token is revoked"})
    async with _client(lambda request: answer) as client:
        with pytest.raises(arsenkin.ArsenkinError, match="AUTH_ERROR token is revoked"):
            await arsenkin.collect(client, "t", "https://a.example/")


async def test_a_non_json_answer_is_a_service_error_not_a_traceback() -> None:
    answer = httpx.Response(200, text="<html>captcha</html>")
    async with _client(lambda request: answer) as client:
        with pytest.raises(arsenkin.ArsenkinError, match="not JSON"):
            await arsenkin.collect(client, "t", "https://a.example/")


async def test_a_transport_failure_is_a_service_error_with_the_exception_name() -> None:
    def boom() -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    async with _client(lambda request: boom()) as client:
        with pytest.raises(arsenkin.ArsenkinError, match="ConnectError"):
            await arsenkin.collect(client, "t", "https://a.example/")


async def test_a_task_without_an_id_is_reported_as_such() -> None:
    async with _client(lambda request: httpx.Response(200, json={"code": "SET_TASK_OK"})) as client:
        with pytest.raises(arsenkin.ArsenkinError, match="no task_id"):
            await arsenkin.collect(client, "t", "https://a.example/")


async def test_a_task_that_ends_in_an_error_state_names_the_state() -> None:
    routes = {
        "set": {"code": "SET_TASK_OK", "task_id": 5},
        "check": {"code": "TASK_STATUS", "status": "error", "msg": "site is down"},
    }
    async with _client(_answers(routes)) as client:
        with pytest.raises(arsenkin.ArsenkinError, match=r"ended as 'error'.*site is down"):
            await arsenkin.collect(client, "t", "https://a.example/", poll_interval=0)


async def test_a_task_that_never_finishes_times_out_with_the_last_state() -> None:
    routes = {
        "set": {"code": "SET_TASK_OK", "task_id": 5},
        "check": {"code": "TASK_STATUS", "status": "process"},
    }
    async with _client(_answers(routes)) as client:
        with pytest.raises(arsenkin.ArsenkinError, match="still 'process' after 0 s"):
            await arsenkin.collect(client, "t", "https://a.example/", poll_interval=0, timeout=0)


async def test_the_result_list_may_come_as_a_list_not_a_mapping() -> None:
    routes = {
        "set": {"code": "SET_TASK_OK", "task_id": 5},
        "check": {"code": "TASK_STATUS", "status": "finish"},
        "get": {"code": "TASK_RESULT", "result": {"result": [{"word": "диван", "ws": 5, "pos": 2}]}},
    }
    async with _client(_answers(routes)) as client:
        found = await arsenkin.collect(client, "t", "https://a.example/", poll_interval=0)
    assert [(query["phrase"], query["position"]) for query in found["queries"]] == [("диван", 2)]


async def test_an_unexpected_status_keeps_waiting_instead_of_reading_a_result() -> None:
    """Live the service answers `starting` where the docs show `process`: a status nobody named is still "working"."""
    states = iter(["starting", "process", "finish"])
    routes = {
        "set": {"code": "SET_TASK_OK", "task_id": 9},
        "check": lambda: {"code": "TASK_STATUS", "status": next(states)},
        "get": {"code": "TASK_RESULT", "result": {"result": {"1": {"word": "диван", "ws": 1, "pos": 1}}}},
    }
    async with _client(_answers(routes)) as client:
        found = await arsenkin.collect(client, "t", "https://a.example/", poll_interval=0, timeout=1)
    assert found["queries"][0]["phrase"] == "диван"


async def test_a_status_nobody_named_times_out_rather_than_passing_as_a_result() -> None:
    routes = {
        "set": {"code": "SET_TASK_OK", "task_id": 9},
        "check": {"code": "TASK_STATUS", "status": "quantum"},
    }
    async with _client(_answers(routes)) as client:
        with pytest.raises(arsenkin.ArsenkinError, match="still 'quantum' after 0 s"):
            await arsenkin.collect(client, "t", "https://a.example/", poll_interval=0, timeout=0)


# --- the wrapper: one bad URL never takes the batch down ---------------------------------------


class _Fake:
    def __init__(self, answer: Callable[[str], dict]) -> None:
        self.answer = answer

    async def collect(
        self, client, token, url, *, region="msk", stopwords=(), mode="url", poll_interval=15.0, timeout=1800.0
    ):
        return self.answer(url)


def _doc(url: str, phrase: str, position: int | None) -> dict:
    return {
        "url": url,
        "service": "fake",
        "region": "msk",
        "task_id": 1,
        "cost": 1,
        "queries": [
            {
                "phrase": phrase,
                "position": position,
                "ws": 1,
                "ws_absent": False,
                "toponym": False,
                "question": False,
                "extra": {},
            }
        ],
        "toponims": [],
    }


async def test_a_failed_url_is_recorded_with_the_exception_and_the_batch_goes_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def answer(url: str) -> dict:
        if "bad" in url:
            raise arsenkin.ArsenkinError("HTTP 500 for /get: boom")
        return _doc(url, "диван", 1)

    monkeypatch.setitem(services.SERVICES, "fake", _Fake(answer))
    monkeypatch.setattr(semantics, "_LIMITER", semantics.RateLimiter(limit=1000))
    report = await semantics.collect(["https://bad.example/", "https://ok.example/"], name="fake", token="t")
    assert report["ok"] is False
    assert "ArsenkinError: HTTP 500" in report["urls"][0]["error"]
    assert report["urls"][1]["ok"] is True
    assert [row["phrase"] for row in report["summary"]] == ["диван"]  # the failed URL adds nothing


async def test_an_unknown_service_is_reported_before_any_request() -> None:
    with pytest.raises(KeyError, match=r"unknown service 'nope'.*arsenkin"):
        await semantics.collect(["https://a.example/"], name="nope", token="t")


async def test_a_missing_token_is_reported_with_the_command_that_fixes_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(services.SERVICES, "fake", _Fake(lambda url: _doc(url, "диван", 1)))
    monkeypatch.setattr(semantics, "binding", lambda service: (None, None))  # nothing stored, nothing on PATH
    with pytest.raises(RuntimeError, match=r"no token for 'fake'.*token set fake"):
        await semantics.collect(["https://a.example/"], name="fake")


async def test_the_summary_ignores_failed_documents_but_counts_every_good_one() -> None:
    rows = semantics.summary(
        [
            {"url": "https://a.example/", "ok": True, "queries": _doc("a", "диван", 1)["queries"]},
            {"url": "https://b.example/", "ok": True, "queries": _doc("b", "диван", None)["queries"]},
            {"url": "https://c.example/", "ok": False, "error": "boom"},
        ]
    )
    assert rows == [
        {
            "phrase": "диван",
            "urls": {"https://a.example/": 1, "https://b.example/": None},
            "urls_count": 2,
            "ws": 1,
            "toponym": False,
            "question": False,
        }
    ]


# --- the CLI: exit codes tell the caller what happened -----------------------------------------


def test_the_cli_exits_zero_when_every_url_succeeded(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    from rb_workflows import cli

    async def fake_collect(urls, **kwargs):
        return {
            "ok": True,
            "urls": [{"url": url, "ok": True, "queries": []} for url in urls],
            "summary": [],
            "service": "fake",
            "region": "msk",
            "requests_waited_s": 0.0,
            "elapsed_s": 0.0,
        }

    monkeypatch.setattr(semantics, "collect", fake_collect)
    assert cli.main(["semantics", "--urls", "https://a.example/", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True


def test_the_cli_exits_one_when_a_url_failed(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    from rb_workflows import cli

    async def fake_collect(request, **kwargs):
        urls = request["queries"] if isinstance(request, dict) else list(request)
        return {
            "ok": False,
            "urls": [{"url": urls[0], "ok": False, "error": "boom"}],
            "summary": [],
            "service": "fake",
            "region": "msk",
            "requests_waited_s": 0.0,
            "elapsed_s": 0.0,
        }

    monkeypatch.setattr(semantics, "collect", fake_collect)
    assert cli.main(["semantics", "--urls", "https://a.example/", "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["urls"][0]["error"] == "boom"


def test_the_cli_refuses_an_unknown_engine_with_the_error_envelope(capsys) -> None:
    from rb_workflows import cli

    with pytest.raises(SystemExit) as exit_info:
        cli.main(["semantics", "--urls", "https://a.example/", "--engine", "yandex"])
    assert exit_info.value.code == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "INVALID_ARGUMENTS"


def test_semantics_accepts_arsenkins_own_body(monkeypatch, tmp_path, capsys) -> None:
    """A task written for their API runs here unchanged: `tools_name`, `data`, `mode`, `queries`, `region`, `stopwords`."""
    from rb_workflows import cli

    seen: list[dict] = []

    async def fake_collect(request, **kwargs):
        seen.append(dict(request) if isinstance(request, dict) else {"urls": request})
        return {"ok": True, "urls": [], "summary": [], "service": "fake", "region": "msk"}

    body = {
        "tools_name": "semantics",
        "data": {
            "mode": "domain",
            "queries": ["altika.ru"],
            "region": "gru",
            "stopwords": ["киа"],
        },
    }
    path = tmp_path / "semantics.json"
    path.write_text(json.dumps(body), encoding="utf-8")
    monkeypatch.setattr(semantics, "collect", fake_collect)

    assert cli.main(["semantics", "--input", str(path), "--json"]) == 0
    requested = seen[0]
    assert requested["queries"] == ["altika.ru"]
    assert (requested["mode"], requested["engine"], requested["stopwords"]) == ("domain", "gru", ["киа"])


def test_an_unknown_semantics_mode_is_refused() -> None:
    with pytest.raises(ValueError, match="unknown mode 'html'"):
        semantics.normalise({"queries": ["a.ru"], "mode": "html"})
