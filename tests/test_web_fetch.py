"""`web-fetch` locally: spider first, the paid waterfall after it, the budget ceiling, and the JSON that deploys."""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

import pytest

from rb_workflows import cli, document
from rb_workflows.document import MAX_TEXT
from rb_workflows.flows import web_fetch as flow


class FakeStep:
    """A step that answers from a table and records the calls it got."""

    def __init__(self, results: dict[str, dict] | None = None) -> None:
        self.results = results or {}
        self.calls: list[tuple[str, str | None]] = []

    async def __call__(self, url: str, *, proxy: str | None = None) -> dict:
        key = url.rsplit("/", 1)[-1]
        self.calls.append((url, proxy))
        return self.results.get(key, document(url, source="spider", ok=False, reason="blocked"))


@pytest.fixture
def cascade(monkeypatch: pytest.MonkeyPatch) -> dict[str, FakeStep]:
    """spider answers for `good` and is blocked for `paid`; exa answers `paid` and nothing else."""
    steps = {
        "spider": FakeStep(
            {
                "good": document("https://a.example/good", source="spider", ok=True, text="from spider", cost_usd=0.0),
                "paid": document("https://a.example/paid", source="spider", ok=False, reason="blocked"),
            }
        ),
        "exa": FakeStep(
            {
                "paid": document("https://a.example/paid", source="exa", ok=True, text="from exa", cost_usd=0.001),
            }
        ),
    }
    monkeypatch.setattr(flow, "ADAPTERS", (("spider", steps["spider"]), ("exa", steps["exa"])))
    return steps


def _asked(step: FakeStep) -> list[str]:
    return [url.rsplit("/", 1)[-1] for url, _ in step.calls]


async def test_spider_is_first_and_the_paid_services_are_not_asked(cascade) -> None:
    result = await flow.web_fetch(["https://a.example/good"])
    found = result["documents"][0]
    assert (found["source"], found["text"], found["tried"]) == ("spider", "from spider", [])
    assert _asked(cascade["exa"]) == []


@pytest.mark.usefixtures("cascade")
async def test_when_spider_returns_nothing_the_waterfall_goes_on() -> None:
    found = (await flow.web_fetch(["https://a.example/paid"]))["documents"][0]
    assert (found["source"], found["text"]) == ("exa", "from exa")
    assert found["tried"] == [{"step": "spider", "reason": "blocked"}]


async def test_every_step_failing_is_a_value_not_an_exception(cascade) -> None:
    result = await flow.web_fetch(["https://a.example/nothing"])
    found = result["documents"][0]
    assert (result["ok"], found["reason"], found["source"]) == (False, "all_steps_failed", "none")
    assert [item["step"] for item in found["tried"]] == ["spider", "exa"]


async def test_the_proxy_reaches_the_first_step(cascade) -> None:
    await flow.web_fetch(["https://a.example/good"], proxy="socks5://p:1")
    assert cascade["spider"].calls == [("https://a.example/good", "socks5://p:1")]


@pytest.mark.usefixtures("cascade")
async def test_the_budget_stops_the_next_url() -> None:
    result = await flow.web_fetch(["https://a.example/paid", "https://a.example/paid"], budget_usd=0.0005)
    assert [doc["reason"] for doc in result["documents"]] == [None, "budget_exhausted"]
    assert result["budget_spent_usd"] == 0.001


async def test_steps_narrow_the_waterfall_and_an_unknown_name_is_refused(cascade) -> None:
    result = await flow.web_fetch(["https://a.example/good"], steps=["exa"])
    assert result["documents"][0]["source"] == "none"  # exa has nothing for `good`
    assert _asked(cascade["spider"]) == []
    with pytest.raises(ValueError, match="no step of"):
        await flow.web_fetch(["https://a.example/x"], steps=["nope"])


async def test_a_raising_step_does_not_kill_the_workflow(cascade, monkeypatch: pytest.MonkeyPatch) -> None:
    async def broken(*_args: object, **_kwargs: object) -> dict:
        raise RuntimeError("service exploded")

    monkeypatch.setattr(flow, "ADAPTERS", (("broken", broken), ("exa", cascade["exa"])))
    found = (await flow.web_fetch(["https://a.example/paid"]))["documents"][0]
    assert found["source"] == "exa"
    assert found["tried"][0]["reason"].startswith("raised:RuntimeError")


@pytest.mark.usefixtures("cascade")
def test_the_cli_prints_the_contract_and_exits_by_the_result(capsys, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(cli.WORKFLOWS, "web-fetch", flow.web_fetch)
    code = cli.main(["web-fetch", "--urls", "https://a.example/good", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert (code, payload["ok"], payload["documents"][0]["source"]) == (0, True, "spider")


@pytest.mark.usefixtures("cascade")
def test_the_cli_refuses_an_empty_input(capsys) -> None:
    with pytest.raises(SystemExit) as stopped:
        cli.main(["web-fetch", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert (stopped.value.code, payload["error"]["code"]) == (2, "INVALID_ARGUMENTS")
    assert payload["error"]["message"].startswith("nothing to fetch")


def test_a_document_is_json_and_carries_its_identity() -> None:
    doc = document("https://a.example/x", source="exa", ok=True, text="a" * 10)
    assert json.loads(json.dumps(doc)) == doc
    assert doc["text_sha256"] and doc["text_truncated"] is False
    big = document("https://a.example/x", source="exa", ok=True, text="b" * (MAX_TEXT + 1))
    assert (big["text_truncated"], len(big["text"])) == (True, MAX_TEXT)


def test_the_package_ships_the_flow_it_documents() -> None:
    root = Path(__file__).resolve().parents[1]
    assert (root / "src/rb_workflows/flows/web_fetch.py").is_file()


def test_the_spider_step_asks_for_the_content_and_ignores_env_proxies(monkeypatch: pytest.MonkeyPatch) -> None:
    from rb_workflows import adapters

    seen: dict = {}

    def run(argv, **kwargs):
        seen.update(argv=argv, **kwargs)
        page = {"status": 200, "markdown": "text", "final_url": "https://a.example/x", "seo": {"title": "X"}}
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(page), stderr="")

    monkeypatch.setattr(adapters.subprocess, "run", run)
    found = asyncio.run(adapters.spider("https://a.example/x", proxy="socks5://p:1"))

    assert (found["source"], found["text"]) == ("spider", "text")
    assert seen["argv"][:5] == ["spider", "fetch", "https://a.example/x", "--json", "--include"]
    assert "crawl.proxy_urls=socks5://p:1" in seen["argv"]
    assert "env" not in seen  # inherited as is: spider itself keeps `trust_env` off and picks a writable cache
