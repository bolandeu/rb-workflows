"""`web-search`: cascade against parallel, the merge by URL, the budget, and the text through trafilatura."""

from __future__ import annotations

import json

import pytest

from rb_workflows import cli
from rb_workflows.document import document
from rb_workflows.flows import web_search as flow


def _row(url: str, position: int | None, snippet: str | None = None) -> dict:
    return {
        "position": position,
        "url": url,
        "domain": flow._domain(url),
        "title": None,
        "snippet": snippet,
        "engine": "yandex",
    }


def _answer(
    source: str, rows: list[dict], *, reason: str | None = None, answer: str | None = None, cost: float | None = None
) -> dict:
    return {
        "source": source,
        "ok": not reason,
        "results": rows,
        "answer": answer,
        "reason": reason,
        "cost_usd": cost,
    }


class FakeSource:
    """A search source that answers from a table and records the calls it got."""

    def __init__(self, name: str, found: dict, error: Exception | None = None) -> None:
        self.name = name
        self.found = found
        self.error = error
        self.calls: list[tuple[str, int, str | None]] = []

    async def __call__(self, query: str, *, top: int, engine: str = "yandex", proxy: str | None = None) -> dict:
        self.calls.append((query, top, proxy))
        if self.error is not None:
            raise self.error
        return self.found


@pytest.fixture
def sources(monkeypatch: pytest.MonkeyPatch) -> dict[str, FakeSource]:
    """`serp` answers two pages, one of them shared with `exa`, which costs 0.005 a call."""
    table = {
        "serp": FakeSource(
            "serp",
            _answer(
                "serp",
                [
                    _row("https://a.example/1", 1, "from serp"),
                    _row("https://b.example/2", 2, "skipped"),
                ],
            ),
        ),
        "exa": FakeSource(
            "exa",
            _answer("exa", [_row("https://a.example/1", 3), _row("https://c.example/3", 1, "from exa")], cost=0.005),
        ),
        "tavily": FakeSource("tavily", _answer("tavily", [], reason="no_key")),
    }
    monkeypatch.setattr(flow, "SOURCES", table)
    return table


async def test_the_cascade_stops_at_the_first_source_with_results(sources) -> None:
    entry = (await flow.web_search(["киа"], sources=["serp", "exa"]))["queries"][0]
    assert (entry["source"], len(entry["results"]), entry["tried"]) == ("serp", 2, [])
    assert sources["exa"].calls == []


@pytest.mark.usefixtures("sources")
async def test_the_cascade_goes_on_when_the_first_source_cannot_answer(monkeypatch) -> None:
    monkeypatch.setitem(flow.SOURCES, "serp", FakeSource("serp", _answer("serp", [], reason="no_key")))
    entry = (await flow.web_search(["киа"], sources=["serp", "exa"]))["queries"][0]
    assert (entry["source"], entry["results"][0]["url"]) == ("exa", "https://a.example/1")
    assert entry["tried"] == [{"source": "serp", "reason": "no_key"}]


@pytest.mark.usefixtures("sources")
async def test_parallel_mode_merges_by_url_and_names_every_source() -> None:
    entry = (await flow.web_search(["киа"], mode="parallel", sources=["serp", "exa"]))["queries"][0]
    merged = {row["url"]: row for row in entry["results"]}
    shared = merged["https://a.example/1"]
    assert (shared["sources"], shared["positions"], shared["best_position"]) == (
        ["serp", "exa"],
        {"serp": 1, "exa": 3},
        1,
    )
    assert len(entry["results"]) == 3  # three URLs, not four rows
    assert entry["results"][0]["url"] == "https://a.example/1"  # the URL both sources found comes first
    assert entry["by_source"]["exa"]["cost_usd"] == 0.005


@pytest.mark.usefixtures("sources")
async def test_an_answer_of_a_source_is_reported_next_to_the_list(monkeypatch) -> None:
    monkeypatch.setitem(
        flow.SOURCES, "tavily", FakeSource("tavily", _answer("tavily", [_row("https://d.example/4", 1)], answer="42"))
    )
    entry = (await flow.web_search(["киа"], mode="parallel", sources=["serp", "tavily"]))["queries"][0]
    assert entry["answer"] == "42"
    assert entry["answers"] == {"tavily": "42"}


@pytest.mark.usefixtures("sources")
async def test_a_raising_source_is_a_reason_not_a_failed_run(monkeypatch) -> None:
    monkeypatch.setitem(flow.SOURCES, "exa", FakeSource("exa", {}, error=RuntimeError("boom")))
    entry = (await flow.web_search(["киа"], mode="parallel", sources=["serp", "exa"]))["queries"][0]
    assert entry["ok"] is True
    assert entry["tried"][0]["reason"].startswith("raised:RuntimeError")
    assert entry["by_source"]["exa"]["ok"] is False


@pytest.mark.usefixtures("sources")
async def test_the_budget_stops_the_next_query() -> None:
    result = await flow.web_search(["киа", "киа"], mode="parallel", sources=["exa"], budget_usd=0.001)
    assert [entry.get("reason") for entry in result["queries"]] == [None, "budget_exhausted"]
    assert result["budget_spent_usd"] == 0.005


@pytest.mark.usefixtures("sources")
async def test_fetch_top_returns_the_main_text_through_trafilatura(monkeypatch) -> None:
    html = (
        "<html><body><nav>menu</nav><article><h1>Киа</h1>"
        + "<p>Ремонт Киа в Москве.</p>" * 20
        + "</article></body></html>"
    )

    async def page(*_args: object, **_kwargs: object) -> dict:
        return {"html": html, "title": "Киа", "source": "spider"}

    monkeypatch.setattr(flow, "_html", page)
    entry = (await flow.web_search(["киа"], mode="parallel", sources=["serp"], fetch_top=1))["queries"][0]
    found = entry["documents"][0]
    assert (found["ok"], found["via"], found["source"]) == (True, "trafilatura", "spider")
    assert "Ремонт Киа в Москве." in found["text"]
    assert "menu" not in found["text"]


@pytest.mark.usefixtures("sources")
async def test_an_empty_extraction_falls_back_to_the_markdown_of_spider(monkeypatch) -> None:
    async def page(*_args: object, **_kwargs: object) -> dict:
        return {"html": "<html></html>", "title": None, "source": "spider"}

    async def markdown(url: str, **_kwargs: object) -> dict:
        return document(url, source="spider", ok=True, text="markdown text")

    monkeypatch.setattr(flow, "_html", page)
    monkeypatch.setattr(flow.adapters, "spider", markdown)
    entry = (await flow.web_search(["киа"], mode="parallel", sources=["serp"], fetch_top=1))["queries"][0]
    assert (entry["documents"][0]["text"], entry["documents"][0]["via"]) == ("markdown text", "markdown")


@pytest.mark.usefixtures("sources")
async def test_an_unknown_source_or_mode_is_refused() -> None:
    with pytest.raises(ValueError, match="unknown source"):
        await flow.web_search(["киа"], sources=["nope"])
    with pytest.raises(ValueError, match="unknown mode"):
        await flow.web_search(["киа"], mode="both")
    with pytest.raises(ValueError, match="no queries"):
        await flow.web_search([])


def test_the_cli_prints_the_contract(capsys) -> None:
    code = cli.main(["web-search", "--queries", "киа", "--mode", "parallel", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert (code, payload["ok"], payload["queries"][0]["source"]) == (0, True, "serp")


def test_the_cli_refuses_an_empty_input() -> None:
    with pytest.raises(SystemExit, match="nothing to look up"):
        cli.main(["web-search", "--json"])


def test_the_cli_takes_the_sources_as_a_comma_list(capsys) -> None:
    cli.main(["web-search", "--queries", "киа", "--sources", "exa,serp", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["sources"] == ["exa", "serp"]
