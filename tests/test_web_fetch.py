"""`web-fetch` locally: the cascade order, the budget ceiling, and the JSON shape that deploys to Windmill."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rb_workflows import cli, document
from rb_workflows.flows import web_fetch as flow


class FakeStep:
    """A step that answers from a table and records the calls it got."""

    def __init__(self, results: dict[str, dict] | None = None) -> None:
        self.results = results or {}
        self.calls: list[tuple[str, str | None]] = []

    async def __call__(self, url: str, *, proxy: str | None = None) -> dict:
        self.calls.append((url, proxy))
        return self.results.get(url.rsplit("/", 1)[-1], document(url, source="fake", ok=False, reason="empty_text"))


def fake_step(results: dict[str, dict] | None = None) -> FakeStep:
    return FakeStep(results)


@pytest.fixture
def cascade(monkeypatch: pytest.MonkeyPatch):
    """exa answers for `good`, fails with `blocked` for `blocked`; direct answers for anything else."""
    exa = fake_step(
        {
            "good": document("https://a.example/good", source="exa", ok=True, text="hello from exa", cost_usd=0.001),
            "blocked": document("https://a.example/blocked", source="exa", ok=False, reason="blocked", cost_usd=0.0),
        }
    )
    tavily = fake_step({})
    spider = fake_step({"blocked": document("https://a.example/blocked", source="spider", ok=True, text="from spider")})
    direct = fake_step({"blocked": document("https://a.example/blocked", source="direct", ok=True, text="from direct")})
    monkeypatch.setattr(flow, "ADAPTERS", (("exa", exa), ("tavily", tavily), ("spider", spider), ("direct", direct)))
    return {"exa": exa, "tavily": tavily, "spider": spider, "direct": direct}


async def test_the_first_working_step_wins_and_the_rest_is_not_asked(cascade) -> None:
    result = await flow.web_fetch(["https://a.example/good"])
    assert result["ok"] is True
    assert (result["documents"][0]["source"], result["documents"][0]["text"]) == ("exa", "hello from exa")
    assert [call[0].rsplit("/", 1)[-1] for call in cascade["tavily"].calls] == []  # never reached


async def test_a_blocked_page_moves_down_the_cascade(cascade) -> None:
    result = await flow.web_fetch(["https://a.example/blocked"])
    found = result["documents"][0]
    assert (found["source"], found["text"]) == ("spider", "from spider")
    assert found["tried"] == [{"step": "exa", "reason": "blocked"}, {"step": "tavily", "reason": "empty_text"}]


async def test_every_step_failing_is_a_value_not_an_exception(cascade) -> None:
    result = await flow.web_fetch(["https://a.example/nothing"])
    assert result["ok"] is False
    assert result["documents"][0]["reason"] == "all_steps_failed"


async def test_the_proxy_reaches_every_step(cascade) -> None:
    await flow.web_fetch(["https://a.example/good"], proxy="socks5://p:1")
    assert cascade["exa"].calls == [("https://a.example/good", "socks5://p:1")]


async def test_the_budget_stops_the_next_url(cascade) -> None:
    result = await flow.web_fetch(["https://a.example/good", "https://a.example/good"], budget_usd=0.0005)
    assert [doc["reason"] for doc in result["documents"]] == [None, "budget_exhausted"]
    assert result["budget_spent_usd"] == 0.001


async def test_steps_narrow_the_cascade_and_an_unknown_name_is_refused(cascade) -> None:
    result = await flow.web_fetch(["https://a.example/blocked"], steps=["direct"])
    assert (result["documents"][0]["source"], result["documents"][0]["tried"]) == ("direct", [])
    with pytest.raises(ValueError, match="no step of"):
        await flow.web_fetch(["https://a.example/x"], steps=["nope"])


async def test_a_raising_step_does_not_kill_the_workflow(cascade, monkeypatch: pytest.MonkeyPatch) -> None:
    async def broken(url: str, *, proxy: str | None = None) -> dict:
        raise RuntimeError("service exploded")

    monkeypatch.setattr(flow, "ADAPTERS", (("broken", broken), ("direct", cascade["direct"])))
    found = (await flow.web_fetch(["https://a.example/blocked"]))["documents"][0]
    assert found["source"] == "direct"
    assert found["tried"][0]["reason"].startswith("raised:RuntimeError")


def test_the_cli_prints_the_contract_and_exits_by_the_result(cascade, capsys, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(cli.WORKFLOWS, "web-fetch", flow.web_fetch)
    code = cli.main(["run", "web-fetch", "--urls", "https://a.example/good", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert (code, payload["ok"], payload["documents"][0]["source"]) == (0, True, "exa")


def test_the_cli_refuses_an_empty_input(cascade) -> None:
    with pytest.raises(SystemExit, match="nothing to fetch"):
        cli.main(["run", "web-fetch", "--json"])


def test_a_document_is_json_and_carries_its_identity() -> None:
    doc = document("https://a.example/x", source="exa", ok=True, text="a" * 10)
    assert json.loads(json.dumps(doc)) == doc
    assert doc["text_sha256"] and doc["text_truncated"] is False
    big = document("https://a.example/x", source="exa", ok=True, text="b" * (document.__globals__["MAX_TEXT"] + 1))
    assert (big["text_truncated"], len(big["text"])) == (True, document.__globals__["MAX_TEXT"])


def test_the_package_ships_the_flow_it_documents() -> None:
    root = Path(__file__).resolve().parents[1]
    assert (root / "src/rb_workflows/flows/web_fetch.py").is_file()
