"""`semantics`: the batch fan-out, the summary, and the Arsenkin protocol against a fake transport."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable

import httpx
import pytest

from rb_workflows import semantics, services
from rb_workflows.services import arsenkin


class FakeService:
    """A stand-in service: one URL in, one document out, recording what ran."""

    def __init__(self, answer: Callable[[str], dict]) -> None:
        self.answer = answer
        self.seen: list[str] = []

    async def collect(self, client, token, url, *, region="msk", stopwords=(), poll_interval=10.0, timeout=1800.0):
        self.seen.append(url)
        await asyncio.sleep(0.01)
        return self.answer(url)


def _service(answer: Callable[[str], dict]) -> FakeService:
    return FakeService(answer)


def _document(url: str, *phrases: tuple[str, int | None]) -> dict:
    return {
        "url": url,
        "service": "fake",
        "region": "msk",
        "task_id": 1,
        "cost": 50,
        "queries": [
            {
                "phrase": phrase,
                "position": position,
                "ws": 10,
                "ws_absent": False,
                "toponym": False,
                "question": False,
                "extra": {},
            }
            for phrase, position in phrases
        ],
        "toponims": [],
    }


async def test_a_batch_runs_many_urls_without_waiting_for_the_first(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _service(lambda url: _document(url, ("купить диван", 3)))
    monkeypatch.setitem(services.SERVICES, "fake", fake)
    monkeypatch.setattr(semantics, "_LIMITER", semantics.RateLimiter(limit=1000))
    urls = [f"https://site.example/{n}" for n in range(3)]
    report = await semantics.collect(urls, name="fake", concurrency=3, token="t")
    assert report["ok"] is True
    assert [doc["url"] for doc in report["urls"]] == urls
    assert len(fake.seen) == 3


async def test_the_summary_counts_urls_and_keeps_the_phrases_without_a_position(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answers = {
        "https://a.example/": _document("https://a.example/", ("купить диван", 3), ("диван", None)),
        "https://b.example/": _document("https://b.example/", ("купить диван", 7)),
    }
    monkeypatch.setitem(services.SERVICES, "fake", _service(lambda url: answers[url]))
    monkeypatch.setattr(semantics, "_LIMITER", semantics.RateLimiter(limit=1000))
    report = await semantics.collect(list(answers), name="fake", token="t")
    by_phrase = {row["phrase"]: row for row in report["summary"]}
    assert by_phrase["купить диван"]["urls_count"] == 2
    assert by_phrase["купить диван"]["urls"] == {"https://a.example/": 3, "https://b.example/": 7}
    assert by_phrase["диван"]["urls"] == {"https://a.example/": None}
    assert by_phrase["диван"]["urls_count"] == 1  # no position, still a phrase of one URL


async def test_a_failed_url_does_not_stop_the_batch(monkeypatch: pytest.MonkeyPatch) -> None:
    def answer(url: str) -> dict:
        if "bad" in url:
            raise RuntimeError("service exploded")
        return _document(url, ("диван", 1))

    monkeypatch.setitem(services.SERVICES, "fake", _service(answer))
    monkeypatch.setattr(semantics, "_LIMITER", semantics.RateLimiter(limit=1000))
    report = await semantics.collect(["https://ok.example/", "https://bad.example/"], name="fake", token="t")
    assert [doc["ok"] for doc in report["urls"]] == [True, False]
    assert "service exploded" in report["urls"][1]["error"]
    assert report["ok"] is False


async def test_the_rate_limiter_waits_instead_of_failing() -> None:
    limiter = semantics.RateLimiter(limit=2, window=0.2)
    started = asyncio.get_running_loop().time()
    for _ in range(4):
        async with limiter:
            pass
    assert asyncio.get_running_loop().time() - started >= 0.2
    assert limiter.waited > 0


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False)


async def test_arsenkin_sets_polls_and_parses_the_real_result_shape() -> None:
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content or b"{}")
        bodies.append(body)
        assert request.headers["authorization"] == "Bearer test-token"
        path = request.url.path.rsplit("/", 1)[-1]
        if path == "set":
            return httpx.Response(200, json={"code": "SET_TASK_OK", "task_id": 42, "cost": 50})
        if path == "check":
            return httpx.Response(200, json={"code": "TASK_STATUS", "status": "finish", "progress": 100})
        return httpx.Response(
            200,
            json={
                "code": "TASK_RESULT",
                "task_id": 42,
                "result": {
                    "total": 2,
                    "result": {
                        "1": {"word": "купить диван", "ws": 128, "pos": 32, "isquest": 0},
                        "2": {"word": "как выбрать диван", "ws": 0, "pos": 0, "isquest": 1},
                    },
                    "toponims": ["москва"],
                },
            },
        )

    async with _client(handler) as client:
        found = await arsenkin.collect(client, "test-token", "https://a.example/", region="msk", poll_interval=0)

    assert found["task_id"] == 42 and found["cost"] == 50
    phrases = {query["phrase"]: query for query in found["queries"]}
    assert phrases["купить диван"]["position"] == 32 and phrases["купить диван"]["ws"] == 128
    assert phrases["как выбрать диван"]["position"] is None  # 0 is "not in the top"
    assert phrases["как выбрать диван"]["ws_absent"] is True
    assert phrases["как выбрать диван"]["question"] is True
    assert bodies[0] == {
        "tools_name": "semantics",
        "data": {"mode": "url", "queries": ["https://a.example/"], "region": "msk", "stopwords": []},
    }


async def test_arsenkin_reports_a_rate_limit_instead_of_guessing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(arsenkin, "RATE_LIMIT_PAUSE", 0)
    monkeypatch.setattr(arsenkin, "RATE_LIMIT_TRIES", 2)
    async with _client(lambda request: httpx.Response(429, json={})) as client:
        with pytest.raises(arsenkin.ArsenkinError, match="rate limited"):
            await arsenkin.collect(client, "t", "https://a.example/")
