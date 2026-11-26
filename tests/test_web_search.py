"""`web-search`: cascade against parallel, the merge by URL, the budget, and the text through trafilatura."""

from __future__ import annotations

import json
import subprocess

import httpx
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
        self.calls: list[dict] = []

    async def __call__(self, query: str, **kwargs: object) -> dict:
        self.calls.append({"query": query, **kwargs})
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
async def test_the_budget_stops_the_next_query(monkeypatch) -> None:
    monkeypatch.setitem(flow.SOURCES, "exa", FakeSource("exa", _answer("exa", [_row("https://a/1", 1)], cost=0.25)))
    # 0.25 is exact in binary: the ceiling is reached, not almost reached
    spent = await flow.web_search(["киа", "киа"], mode="parallel", sources=["exa"], budget_usd=0.25)
    assert [entry.get("reason") for entry in spent["queries"]] == [None, "budget_exhausted"]
    result = await flow.web_search(["киа", "киа"], mode="parallel", sources=["exa"], budget_usd=0.5)
    assert [entry.get("reason") for entry in result["queries"]] == [None, None]  # half the ceiling is not the ceiling
    assert result["budget_spent_usd"] == 0.5


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
    with pytest.raises(ValueError, match=r"^unknown source 'nope'; known: serp, exa, tavily$"):
        await flow.web_search(["киа"], sources=["nope"])
    with pytest.raises(ValueError, match=r"^unknown mode 'both'; known: cascade, parallel$"):
        await flow.web_search(["киа"], mode="both")
    with pytest.raises(ValueError, match=r"^no queries$"):
        await flow.web_search([])


def test_the_cli_prints_the_contract(capsys) -> None:
    code = cli.main(["web-search", "--queries", "киа", "--mode", "parallel", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert (code, payload["ok"], payload["queries"][0]["source"]) == (0, True, "serp")


def test_the_cli_refuses_an_empty_input(capsys) -> None:
    with pytest.raises(SystemExit) as stopped:
        cli.main(["web-search", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert (stopped.value.code, payload["error"]["code"]) == (2, "INVALID_ARGUMENTS")
    assert payload["error"]["message"].startswith("nothing to look up")


def test_the_cli_takes_the_sources_as_a_comma_list(capsys) -> None:
    cli.main(["web-search", "--queries", "киа", "--sources", "exa,serp", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["sources"] == ["exa", "serp"]


def test_the_domain_of_a_url() -> None:
    assert flow._domain("https://a.example/x") == "a.example"
    assert flow._domain("http://a.example:8080/x/y") == "a.example:8080"
    assert flow._domain("a.example/x") == "a.example"  # no scheme at all
    assert flow._domain("https://") is None


def test_a_failed_source_carries_no_results_and_no_cost() -> None:
    assert flow._failed("exa", "no_key") == {
        "source": "exa",
        "ok": False,
        "results": [],
        "answer": None,
        "reason": "no_key",
        "cost_usd": None,
        "cost_currency": None,
    }
    assert flow._failed("serp", "empty", cost_currency="RUB")["cost_currency"] == "RUB"


async def test_a_raising_source_is_cut_to_200_characters(monkeypatch) -> None:
    async def loud(*_args: object, **_kwargs: object) -> dict:
        raise RuntimeError("x" * 500)

    monkeypatch.setitem(flow.SOURCES, "exa", loud)
    found = await flow._safe("exa", "q", top=1, engine="yandex", proxy=None)
    assert found["ok"] is False
    assert found["reason"].startswith("raised:RuntimeError: xxx")
    assert len(found["reason"]) == 200  # the whole reason, not just the exception text


async def test_the_cascade_reports_the_cost_of_the_failed_sources_too(monkeypatch) -> None:
    failed = {**flow._failed("serp", "blocked", cost_usd=0.002)}
    monkeypatch.setitem(flow.SOURCES, "serp", FakeSource("serp", failed))
    monkeypatch.setitem(flow.SOURCES, "exa", FakeSource("exa", _answer("exa", [_row("https://a/1", 1)], cost=0.005)))
    entry = (await flow.web_search(["q"], sources=["serp", "exa"]))["queries"][0]
    assert (entry["source"], entry["cost_usd"]) == ("exa", 0.007)
    assert entry["tried"] == [{"source": "serp", "reason": "blocked"}]


async def test_the_cascade_asks_every_source_and_reports_all_failures(monkeypatch) -> None:
    for name in ("serp", "exa", "tavily"):
        monkeypatch.setitem(flow.SOURCES, name, FakeSource(name, flow._failed(name, "no_key")))
    entry = (await flow.web_search(["q"], sources=["serp", "exa", "tavily"]))["queries"][0]
    assert entry == {
        "query": "q",
        "ok": False,
        "mode": "cascade",
        "source": None,
        "results": [],
        "answer": None,
        "reason": "all_sources_failed",
        "tried": [{"source": name, "reason": "no_key"} for name in ("serp", "exa", "tavily")],
        "cost_usd": 0.0,
    }


async def test_the_parallel_answer_names_the_first_written_one(monkeypatch) -> None:
    monkeypatch.setitem(flow.SOURCES, "serp", FakeSource("serp", _answer("serp", [_row("https://a/1", 1)])))
    monkeypatch.setitem(
        flow.SOURCES, "tavily", FakeSource("tavily", _answer("tavily", [_row("https://b/2", 1)], answer="first"))
    )
    monkeypatch.setitem(
        flow.SOURCES, "exa", FakeSource("exa", _answer("exa", [_row("https://c/3", 1)], answer="second"))
    )
    entry = (await flow.web_search(["q"], mode="parallel", sources=["serp", "tavily", "exa"]))["queries"][0]
    assert (entry["answer"], entry["source"]) == ("first", "serp")
    assert entry["answers"] == {"tavily": "first", "exa": "second"}
    assert entry["tried"] == []


def test_the_merge_keeps_one_row_per_url_and_names_every_source() -> None:
    merged = flow._merge(
        [
            _answer("serp", [_row("https://A.example/1/", 4, "from serp"), _row("https://b.example/2", 1, None)]),
            _answer("exa", [_row("https://a.example/1", None, "from exa"), _row("https://c.example/3", 2, "x")]),
        ]
    )
    assert [row["url"] for row in merged] == ["https://A.example/1/", "https://b.example/2", "https://c.example/3"]
    shared = merged[0]
    assert (shared["sources"], shared["positions"], shared["best_position"]) == (
        ["serp", "exa"],
        {"serp": 4, "exa": None},
        4,
    )
    assert shared["snippet"] == "from serp"  # the first snippet found wins, the later one does not overwrite it


def test_the_merge_prefers_the_better_position_and_its_title() -> None:
    merged = flow._merge(
        [
            _answer("serp", [{**_row("https://a/1", 5), "title": "weak"}]),
            _answer("exa", [{**_row("https://a/1", 2), "title": "strong"}]),
        ]
    )
    assert (merged[0]["best_position"], merged[0]["position"], merged[0]["title"]) == (2, 2, "strong")
    assert merged[0]["sources"] == ["serp", "exa"]


def test_the_merge_orders_by_the_number_of_sources_then_by_position() -> None:
    merged = flow._merge(
        [
            _answer("serp", [_row("https://one/1", 1), _row("https://both/2", 9)]),
            _answer("exa", [_row("https://both/2", 3), _row("https://two/3", 1)]),
        ]
    )
    assert [row["url"] for row in merged] == ["https://both/2", "https://one/1", "https://two/3"]


def test_a_row_without_a_position_does_not_become_the_best_one() -> None:
    merged = flow._merge([_answer("exa", [_row("https://a/1", None), _row("https://a/1", 5)])])
    assert (merged[0]["best_position"], merged[0]["position"]) == (5, 5)


def test_the_domain_of_a_tricky_url() -> None:
    assert flow._domain("https://a.example/x?u=https://b.example") == "a.example"  # a second scheme in the query
    assert flow._domain("https://a.example//x") == "a.example"
    assert flow._domain("ftp://a.example:21/x") == "a.example:21"
    assert flow._domain("") is None
    assert flow._domain("/only/a/path") is None


async def test_the_cascade_entry_is_exactly_this(monkeypatch) -> None:
    serp = FakeSource("serp", _answer("serp", [_row("https://a/1", 1)], cost=0.004))
    monkeypatch.setitem(flow.SOURCES, "serp", serp)
    entry = (await flow.web_search(["q"], sources=["serp"], top=7, engine="google", proxy="socks5://p:1"))["queries"][0]
    assert entry == {
        "query": "q",
        "ok": True,
        "mode": "cascade",
        "source": "serp",
        "results": [_row("https://a/1", 1)],
        "answer": None,
        "tried": [],
        "cost_usd": 0.004,
    }
    assert serp.calls == [{"query": "q", "top": 7, "engine": "google", "proxy": "socks5://p:1"}]


async def test_the_parallel_entry_is_exactly_this(monkeypatch) -> None:
    serp = FakeSource("serp", _answer("serp", [_row("https://a/1", 2)], cost=0.004))
    monkeypatch.setitem(flow.SOURCES, "serp", serp)
    monkeypatch.setitem(flow.SOURCES, "exa", FakeSource("exa", flow._failed("exa", "no_key")))
    entry = (await flow.web_search(["q"], mode="parallel", sources=["serp", "exa"], top=3))["queries"][0]
    row = {**_row("https://a/1", 2), "sources": ["serp"], "positions": {"serp": 2}, "best_position": 2}
    assert entry == {
        "query": "q",
        "ok": True,
        "mode": "parallel",
        "source": "serp",
        "results": [row],
        "answer": None,
        "answers": {},
        "tried": [{"source": "exa", "reason": "no_key"}],
        "by_source": {
            "serp": {"ok": True, "reason": None, "results": [_row("https://a/1", 2)], "cost_usd": 0.004},
            "exa": {"ok": False, "reason": "no_key", "results": [], "cost_usd": None},
        },
        "cost_usd": 0.004,
    }
    assert [call["top"] for call in serp.calls] == [3]  # the arguments reach every source


async def test_parallel_with_no_winner_has_no_source(monkeypatch) -> None:
    monkeypatch.setitem(flow.SOURCES, "serp", FakeSource("serp", flow._failed("serp", "empty")))
    entry = (await flow.web_search(["q"], mode="parallel", sources=["serp"]))["queries"][0]
    assert (entry["ok"], entry["source"], entry["results"], entry["answers"]) == (False, None, [], {})
    assert entry["tried"] == [{"source": "serp", "reason": "empty"}]


async def test_the_cost_is_rounded_to_six_decimals(monkeypatch) -> None:
    monkeypatch.setitem(flow.SOURCES, "exa", FakeSource("exa", _answer("exa", [_row("https://a/1", 1)], cost=4e-07)))
    cascade = (await flow.web_search(["q"], sources=["exa"]))["queries"][0]
    parallel = (await flow.web_search(["q"], mode="parallel", sources=["exa"]))["queries"][0]
    assert (cascade["cost_usd"], parallel["cost_usd"]) == (0.0, 0.0)


def test_the_merge_row_is_exactly_this() -> None:
    merged = flow._merge(
        [
            _answer("serp", [_row("https://a.example/1", 1, "s")]),
            _answer("exa", [_row("https://a.example/1", 3, "e")]),
        ]
    )
    assert merged == [
        {
            "position": 1,
            "url": "https://a.example/1",
            "domain": "a.example",
            "title": None,
            "snippet": "s",
            "engine": "yandex",
            "sources": ["serp", "exa"],
            "positions": {"serp": 1, "exa": 3},
            "best_position": 1,
        }
    ]


def test_a_url_ending_with_an_x_is_not_the_same_as_the_one_without() -> None:
    merged = flow._merge([_answer("serp", [_row("https://a.example/1X", 1), _row("https://a.example/1", 2)])])
    assert len(merged) == 2  # only a trailing slash is trailing punctuation, not any character


def test_two_urls_that_fold_together_but_differ_stay_apart() -> None:
    merged = flow._merge([_answer("serp", [_row("https://a.example/ß", 1), _row("https://a.example/ss", 2)])])
    assert len(merged) == 2


def test_a_later_source_fills_a_snippet_the_first_one_did_not_have() -> None:
    merged = flow._merge(
        [
            _answer("serp", [_row("https://a/1", 1, None)]),
            _answer("exa", [_row("https://a/1", 2, "from exa")]),
        ]
    )
    assert merged[0]["snippet"] == "from exa"


def test_a_tie_in_position_keeps_the_first_title() -> None:
    merged = flow._merge(
        [
            _answer("serp", [{**_row("https://a/1", 2), "title": "first"}]),
            _answer("exa", [{**_row("https://a/1", 2), "title": "second"}]),
        ]
    )
    assert (merged[0]["title"], merged[0]["best_position"], merged[0]["sources"]) == ("first", 2, ["serp", "exa"])


def test_a_row_without_a_position_goes_last() -> None:
    merged = flow._merge(
        [_answer("serp", [_row("https://none/1", None), _row("https://deep/2", 100), _row("https://top/3", 1)])]
    )
    assert [row["url"] for row in merged] == ["https://top/3", "https://deep/2", "https://none/1"]


async def test_the_parallel_mode_passes_the_proxy_through(monkeypatch) -> None:
    serp = FakeSource("serp", _answer("serp", [_row("https://a/1", 1)]))
    monkeypatch.setitem(flow.SOURCES, "serp", serp)
    await flow.web_search(["q"], mode="parallel", sources=["serp"], proxy="socks5://p:1")
    assert serp.calls == [{"query": "q", "top": 10, "engine": "yandex", "proxy": "socks5://p:1"}]


async def test_the_cost_of_a_failed_cascade_is_rounded_too(monkeypatch) -> None:
    monkeypatch.setitem(flow.SOURCES, "exa", FakeSource("exa", flow._failed("exa", "empty", cost_usd=4e-07)))
    entry = (await flow.web_search(["q"], sources=["exa"]))["queries"][0]
    assert entry["cost_usd"] == 0.0  # six decimals, not whole cents
    monkeypatch.setitem(flow.SOURCES, "exa", FakeSource("exa", flow._failed("exa", "empty", cost_usd=0.004)))
    entry = (await flow.web_search(["q"], sources=["exa"]))["queries"][0]
    assert entry["cost_usd"] == 0.004


class FakeResponse:
    """An httpx response as the sources read it: a status, a text and a JSON body."""

    def __init__(self, status_code: int = 200, body: object = None, text: str = "") -> None:
        self.status_code = status_code
        self.body = body
        self.text = text if text or body is None else json.dumps(body)

    def json(self) -> object:
        if self.body is None:
            raise ValueError("not JSON")
        return self.body


class FakeClient:
    """Stands in for `httpx.AsyncClient`: records the constructor and the calls, answers with one response."""

    def __init__(self, response: FakeResponse, *, error: Exception | None = None, **init: object) -> None:
        self.response = response
        self.error = error
        self.init = init
        self.calls: list[dict] = []

    async def __aenter__(self) -> FakeClient:
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        return False

    async def post(self, url: str, **kwargs: object) -> FakeResponse:
        self.calls.append({"url": url, **kwargs})
        if self.error is not None:
            raise self.error
        return self.response

    async def get(self, url: str, **kwargs: object) -> FakeResponse:
        return await self.post(url, **kwargs)


class BindingRecorder:
    """`credentials.binding`: records what a source asked for, answers with a key and the proxy it got."""

    def __init__(self, key: str = "k", proxy: str | None = None) -> None:
        self.key = key
        self.proxy = proxy
        self.calls: list[dict] = []

    def __call__(self, service: str, **kwargs: object) -> tuple[str | None, str | None]:
        self.calls.append({"service": service, **kwargs})
        given = kwargs.get("proxy")
        return (self.key or None), (given if isinstance(given, str) else self.proxy)


def _client(monkeypatch, response: FakeResponse, *, error: Exception | None = None) -> FakeClient:
    """One client for every `AsyncClient(...)`: its constructor arguments are kept on `init`."""
    client = FakeClient(response, error=error)

    def factory(**kwargs: object) -> FakeClient:
        client.init = kwargs
        return client

    monkeypatch.setattr(flow.httpx, "AsyncClient", factory)
    return client


def _key(monkeypatch, key: str = "k") -> BindingRecorder:
    recorder = BindingRecorder(key)
    monkeypatch.setattr(flow, "binding", recorder)
    return recorder


class FakeProcess:
    """`subprocess.run` for the serp CLI: a return code and stdout, or an exception."""

    def __init__(self, returncode: int = 0, stdout: str = "", *, raises: Exception | None = None) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.raises = raises
        self.calls: list[dict] = []

    def __call__(self, argv: list[str], **kwargs: object) -> FakeProcess:
        self.calls.append({"argv": argv, **kwargs})
        if self.raises is not None:
            raise self.raises
        return self


async def test_serp_asks_its_own_cli_for_snippets(monkeypatch) -> None:
    envelope = {
        "ok": True,
        "results": [
            {
                "serp_position": 1,
                "url": "https://a/1",
                "domain": "a",
                "title": "t",
                "snippet": "s",
                "searchengine": "yandex",
            }
        ],
        "cost_total": 0.025,
        "sources": [{"provider": "xmlstock", "cost_currency": "RUB"}],
    }
    process = FakeProcess(0, json.dumps(envelope))
    monkeypatch.setattr(flow.subprocess, "run", process)
    found = await flow.serp("q", top=10)
    assert found == {
        "source": "serp",
        "ok": True,
        "results": [{**_row("https://a/1", 1, "s"), "title": "t"}],
        "answer": None,
        "reason": None,
        "cost_usd": None,
        "cost_currency": "RUB",
        "cost": 0.025,
    }
    assert process.calls == [
        {
            "argv": ["serp", "snippets", "q", "--engine", "yandex", "--top", "10", "--json"],
            "capture_output": True,
            "text": True,
            "timeout": flow.SERP_TIMEOUT,
            "check": False,
        }
    ]


async def test_serp_never_asks_for_fewer_than_ten(monkeypatch) -> None:
    process = FakeProcess(0, json.dumps({"ok": True, "results": [{"url": "https://a/1"}]}))
    monkeypatch.setattr(flow.subprocess, "run", process)
    await flow.serp("q", top=3, engine="both")
    assert process.calls[0]["argv"] == ["serp", "snippets", "q", "--engine", "both", "--top", "10", "--json"]


async def test_serp_reports_a_missing_cli_a_timeout_and_broken_json(monkeypatch) -> None:
    monkeypatch.setattr(flow.subprocess, "run", FakeProcess(raises=FileNotFoundError()))
    assert await flow.serp("q", top=10) == flow._failed("serp", "not_installed")
    monkeypatch.setattr(flow.subprocess, "run", FakeProcess(raises=subprocess.TimeoutExpired("serp", 1)))
    assert await flow.serp("q", top=10) == flow._failed("serp", "timeout")
    monkeypatch.setattr(flow.subprocess, "run", FakeProcess(0, "not json"))
    assert await flow.serp("q", top=10) == flow._failed("serp", "not_json:0")


async def test_serp_reports_the_error_code_of_the_cli(monkeypatch) -> None:
    envelope = {"ok": False, "error": {"code": "PROVIDER_ERROR"}}
    monkeypatch.setattr(flow.subprocess, "run", FakeProcess(5, json.dumps(envelope)))
    assert await flow.serp("q", top=10) == flow._failed("serp", "PROVIDER_ERROR")


async def test_serp_without_results_is_empty_not_broken(monkeypatch) -> None:
    monkeypatch.setattr(flow.subprocess, "run", FakeProcess(0, json.dumps({"ok": True, "results": []})))
    assert await flow.serp("q", top=10) == flow._failed("serp", "empty", cost_currency=None)


async def test_exa_without_a_key_asks_nobody(monkeypatch) -> None:
    _key(monkeypatch, "")
    assert await flow.exa("q", top=1) == flow._failed("exa", "no_key")


async def test_exa_answers_with_its_own_pick_and_cost(monkeypatch) -> None:
    binding_calls = _key(monkeypatch)
    body = {
        "results": [{"url": "https://a/1", "title": "t", "text": "x" * 5000}],
        "costDollars": {"total": 0.005},
    }
    client = _client(monkeypatch, FakeResponse(200, body))
    found = await flow.exa("q", top=1, proxy="socks5://p:1")
    assert found == {
        "source": "exa",
        "ok": True,
        "results": [{**_row("https://a/1", 1, "x" * flow.SNIPPET_CHARS), "title": "t", "engine": "exa"}],
        "answer": None,
        "reason": None,
        "cost_usd": 0.005,
    }
    assert binding_calls.calls == [{"service": "exa", "proxy": "socks5://p:1"}]
    assert client.calls == [
        {
            "url": "https://api.exa.ai/search",
            "json": {"query": "q", "numResults": 1, "contents": {"text": {"maxCharacters": flow.SNIPPET_CHARS}}},
            "headers": {"x-api-key": "k", "User-Agent": flow.UA},
        }
    ]


async def test_exa_reports_a_block_a_http_error_and_a_transport_failure(monkeypatch) -> None:
    _key(monkeypatch)
    _client(monkeypatch, FakeResponse(403, {"costDollars": {"total": 0.001}}))
    assert await flow.exa("q", top=1) == flow._failed("exa", "blocked", cost_usd=0.001)
    _client(monkeypatch, FakeResponse(401, {}))
    assert await flow.exa("q", top=1) == flow._failed("exa", "blocked")
    _client(monkeypatch, FakeResponse(500, {}))
    assert await flow.exa("q", top=1) == flow._failed("exa", "http:500")
    _client(monkeypatch, FakeResponse(200, {}), error=httpx.ConnectError("down"))
    assert await flow.exa("q", top=1) == flow._failed("exa", "error:ConnectError")
    _client(monkeypatch, FakeResponse(200, "not a dict"))  # a page instead of JSON: an empty body, not a crash
    assert await flow.exa("q", top=1) == flow._failed("exa", "empty")


async def test_exa_with_no_results_is_empty(monkeypatch) -> None:
    _key(monkeypatch)
    _client(monkeypatch, FakeResponse(200, {"results": []}))
    assert (await flow.exa("q", top=1))["reason"] == "empty"


async def test_tavily_answers_with_its_pick_and_the_written_answer(monkeypatch) -> None:
    _key(monkeypatch)
    body = {
        "results": [{"url": "https://b/2", "title": "t", "content": "text"}],
        "answer": "short answer",
        "usage": {"credits": 1},
    }
    client = _client(monkeypatch, FakeResponse(200, body))
    found = await flow.tavily("q", top=2)
    assert found == {
        "source": "tavily",
        "ok": True,
        "results": [{**_row("https://b/2", 1, "text"), "title": "t", "engine": "tavily"}],
        "answer": "short answer",
        "reason": None,
        "cost_usd": 0.001,
    }
    assert client.calls == [
        {
            "url": "https://api.tavily.com/search",
            "json": {"query": "q", "max_results": 2, "include_answer": True, "search_depth": "basic"},
            "headers": {"Authorization": "Bearer k", "User-Agent": flow.UA},
        }
    ]


async def test_tavily_without_a_key_a_block_and_a_rate_limit(monkeypatch) -> None:
    _key(monkeypatch, "")
    assert await flow.tavily("q", top=1) == flow._failed("tavily", "no_key")
    _key(monkeypatch)
    _client(monkeypatch, FakeResponse(429, {"usage": {"credits": 2}}))
    assert await flow.tavily("q", top=1) == flow._failed("tavily", "blocked", cost_usd=0.002)
    _client(monkeypatch, FakeResponse(400, {}))
    assert await flow.tavily("q", top=1) == flow._failed("tavily", "http:400")
    _client(monkeypatch, FakeResponse(200, {"results": []}))
    assert await flow.tavily("q", top=1) == flow._failed("tavily", "empty")


async def test_the_html_of_a_page_comes_from_spider_first(monkeypatch) -> None:
    page = {"html": "<html>ok</html>", "seo": {"title": "T"}}
    process = FakeProcess(0, json.dumps(page))
    monkeypatch.setattr(flow.subprocess, "run", process)
    client = _client(monkeypatch, FakeResponse(500, {}, text="fallback"))
    found = await flow._html("https://a/1", proxy="socks5://p:1")
    assert found == {"html": "<html>ok</html>", "title": "T", "source": "spider"}
    assert process.calls == [
        {
            "argv": [
                "spider",
                "fetch",
                "https://a/1",
                "--json",
                "--include",
                "html",
                "--set",
                "crawl.proxy_urls=socks5://p:1",
            ],
            "capture_output": True,
            "text": True,
            "timeout": flow.TIMEOUT * 3,
            "check": False,
        }
    ]
    assert client.calls == []  # the plain GET was not needed


async def test_the_html_asks_for_the_proxy_only_when_there_is_one(monkeypatch) -> None:
    process = FakeProcess(0, json.dumps({"html": "<html>ok</html>"}))
    monkeypatch.setattr(flow.subprocess, "run", process)
    _client(monkeypatch, FakeResponse(500, {}, text="fallback"))
    await flow._html("https://a/1", proxy=None)
    assert process.calls[0]["argv"] == ["spider", "fetch", "https://a/1", "--json", "--include", "html"]


async def test_a_spider_answer_without_html_falls_through(monkeypatch) -> None:
    monkeypatch.setattr(flow.subprocess, "run", FakeProcess(0, json.dumps({"html": ""})))
    _client(monkeypatch, FakeResponse(200, None, text="<html>direct</html>"))
    assert (await flow._html("https://a/1", proxy=None))["source"] == "direct"


async def test_the_html_falls_back_to_a_plain_get(monkeypatch) -> None:
    monkeypatch.setattr(flow.subprocess, "run", FakeProcess(1, ""))
    _client(monkeypatch, FakeResponse(200, None, text="<html>direct</html>"))
    assert (await flow._html("https://a/1", proxy=None))["source"] == "direct"


async def test_the_html_gives_up_when_both_ways_fail(monkeypatch) -> None:
    monkeypatch.setattr(flow.subprocess, "run", FakeProcess(raises=FileNotFoundError()))
    _client(monkeypatch, FakeResponse(404, None, text="gone"))
    assert await flow._html("https://a/1", proxy=None) == {}
    _client(monkeypatch, FakeResponse(200, None), error=httpx.ConnectError("down"))
    assert await flow._html("https://a/1", proxy=None) == {}


async def test_the_main_text_fails_with_the_reason_of_the_fetch(monkeypatch) -> None:
    async def page(*_args: object, **_kwargs: object) -> dict:
        return {}

    async def blocked(*_args: object, **_kwargs: object) -> dict:
        return {"ok": False, "reason": "blocked"}

    monkeypatch.setattr(flow, "_html", page)
    monkeypatch.setattr(flow.adapters, "spider", blocked)
    found = await flow._main_text("https://a/1", proxy=None)
    assert (found["ok"], found["source"], found["reason"]) == (False, "none", "blocked")


async def test_the_direct_fetch_carries_the_user_agent_and_the_proxy(monkeypatch) -> None:
    monkeypatch.setattr(flow.subprocess, "run", FakeProcess(raises=FileNotFoundError()))
    client = _client(monkeypatch, FakeResponse(200, None, text="<html>direct</html>"))
    found = await flow._html("https://a/1", proxy="socks5://p:1")
    assert found == {"html": "<html>direct</html>", "title": None, "source": "direct"}
    assert client.init == {
        "timeout": flow.TIMEOUT,
        "proxy": "socks5://p:1",
        "follow_redirects": True,
        "trust_env": False,
    }
    assert client.calls == [{"url": "https://a/1", "headers": {"User-Agent": flow.UA}}]


async def test_a_not_found_page_is_no_html(monkeypatch) -> None:
    monkeypatch.setattr(flow.subprocess, "run", FakeProcess(raises=FileNotFoundError()))
    _client(monkeypatch, FakeResponse(404, None, text="gone"))
    assert await flow._html("https://a/1", proxy=None) == {}


def _extract(text: str | None, seen: dict) -> object:
    def extract(html: str, **kwargs: object) -> str | None:
        seen.update({"html": html, **kwargs})
        return text

    return extract


async def test_the_main_text_asks_trafilatura_for_the_article(monkeypatch) -> None:
    seen: dict = {}
    monkeypatch.setattr(flow.trafilatura, "extract", _extract("article text", seen))
    calls: dict = {}

    async def page(url: str, *, proxy: str | None = None) -> dict:
        calls.update({"url": url, "proxy": proxy})
        return {"html": "<html>h</html>", "title": "T", "source": "spider"}

    monkeypatch.setattr(flow, "_html", page)
    found = await flow._main_text("https://a/1", proxy="socks5://p:1")
    assert seen == {
        "html": "<html>h</html>",
        "include_comments": False,
        "include_tables": True,
        "favor_precision": True,
    }
    assert calls == {"url": "https://a/1", "proxy": "socks5://p:1"}
    assert found == document("https://a/1", source="spider", ok=True, text="article text", title="T", via="trafilatura")


async def test_the_main_text_is_cut_at_the_ceiling(monkeypatch) -> None:
    monkeypatch.setattr(flow.trafilatura, "extract", _extract("y" * (flow.MAX_TEXT + 50), {}))

    async def page(*_args: object, **_kwargs: object) -> dict:
        return {"html": "<html>h</html>", "title": None, "source": "spider"}

    monkeypatch.setattr(flow, "_html", page)
    found = await flow._main_text("https://a/1", proxy=None)
    assert len(found["text"]) == flow.MAX_TEXT


async def test_the_markdown_fallback_gets_the_proxy_and_says_so(monkeypatch) -> None:
    monkeypatch.setattr(flow.trafilatura, "extract", _extract(None, {}))
    seen: dict = {}

    async def page(*_args: object, **_kwargs: object) -> dict:
        return {"html": "<html/>", "title": None, "source": "spider"}

    async def markdown(url: str, *, proxy: str | None = None) -> dict:
        seen.update({"url": url, "proxy": proxy})
        return document(url, source="spider", ok=True, text="md")

    monkeypatch.setattr(flow, "_html", page)
    monkeypatch.setattr(flow.adapters, "spider", markdown)
    found = await flow._main_text("https://a/1", proxy="socks5://p:1")
    assert (found["text"], found["via"], found["source"]) == ("md", "markdown", "spider")
    assert seen == {"url": "https://a/1", "proxy": "socks5://p:1"}


async def test_the_main_text_without_a_reason_says_empty_text(monkeypatch) -> None:
    async def page(*_args: object, **_kwargs: object) -> dict:
        return {}

    async def blocked(*_args: object, **_kwargs: object) -> dict:
        return {"ok": False, "reason": None}

    monkeypatch.setattr(flow, "_html", page)
    monkeypatch.setattr(flow.adapters, "spider", blocked)
    found = await flow._main_text("https://a/1", proxy=None)
    assert (found["ok"], found["reason"]) == (False, "empty_text")


async def test_the_budget_exhausted_entry_is_exactly_this(monkeypatch) -> None:
    monkeypatch.setitem(flow.SOURCES, "exa", FakeSource("exa", _answer("exa", [_row("https://a/1", 1)], cost=0.5)))
    result = await flow.web_search(["first", "second"], mode="parallel", sources=["exa"], budget_usd=0.5)
    assert result["queries"][1] == {
        "query": "second",
        "ok": False,
        "reason": "budget_exhausted",
        "results": [],
        "tried": [],
    }


async def test_fetch_top_counts_the_cost_of_the_pages(monkeypatch) -> None:
    monkeypatch.setitem(flow.SOURCES, "serp", FakeSource("serp", _answer("serp", [_row("https://a/1", 1)])))

    async def page(url: str, *, proxy: str | None = None) -> dict:
        return document(url, source="exa", ok=True, text="page", cost_usd=0.25)

    monkeypatch.setattr(flow, "_main_text", page)
    result = await flow.web_search(["q"], sources=["serp"], fetch_top=1)
    entry = result["queries"][0]
    assert [doc["url"] for doc in entry["documents"]] == ["https://a/1"]
    assert result["budget_spent_usd"] == 0.25


async def test_the_http_clients_of_exa_and_tavily_get_the_timeout_and_the_proxy(monkeypatch) -> None:
    _key(monkeypatch)
    exa_client = _client(monkeypatch, FakeResponse(200, {"results": [{"url": "https://a/1"}], "costDollars": {}}))
    await flow.exa("q", top=1, proxy="socks5://p:1")
    assert exa_client.init == {"timeout": flow.TIMEOUT, "proxy": "socks5://p:1", "trust_env": False}
    tavily_client = _client(monkeypatch, FakeResponse(200, {"results": [{"url": "https://a/1"}]}))
    await flow.tavily("q", top=1, proxy="socks5://p:2")
    assert tavily_client.init == {"timeout": flow.TIMEOUT, "proxy": "socks5://p:2", "trust_env": False}


async def test_tavily_asks_for_its_own_key_and_reports_a_transport_failure(monkeypatch) -> None:
    binding_calls = _key(monkeypatch)
    _client(monkeypatch, FakeResponse(200, {}), error=httpx.ConnectError("down"))
    assert await flow.tavily("q", top=1, proxy="socks5://p:1") == flow._failed("tavily", "error:ConnectError")
    assert binding_calls.calls == [{"service": "tavily", "proxy": "socks5://p:1"}]


async def test_serp_falls_back_to_the_asked_engine_when_a_row_has_none(monkeypatch) -> None:
    envelope = {"ok": True, "results": [{"url": "https://a/1", "title": "t"}]}
    monkeypatch.setattr(flow.subprocess, "run", FakeProcess(0, json.dumps(envelope)))
    found = await flow.serp("q", top=10, engine="google")
    assert found["results"][0]["engine"] == "google"


async def test_serp_takes_the_currency_of_its_sources_on_a_failure_too(monkeypatch) -> None:
    envelope = {"ok": False, "error": {"code": "PROVIDER_ERROR"}, "sources": [{"cost_currency": "RUB"}]}
    monkeypatch.setattr(flow.subprocess, "run", FakeProcess(5, json.dumps(envelope)))
    assert await flow.serp("q", top=10) == flow._failed("serp", "PROVIDER_ERROR", cost_currency="RUB")


async def test_serp_trusts_the_exit_code_even_when_the_body_says_ok(monkeypatch) -> None:
    envelope = {"ok": True, "results": [{"url": "https://a/1"}]}
    monkeypatch.setattr(flow.subprocess, "run", FakeProcess(5, json.dumps(envelope)))
    assert (await flow.serp("q", top=10))["ok"] is False


async def test_a_page_answered_with_a_client_error_is_no_html(monkeypatch) -> None:
    monkeypatch.setattr(flow.subprocess, "run", FakeProcess(raises=FileNotFoundError()))
    _client(monkeypatch, FakeResponse(400, None, text="bad request"))
    assert await flow._html("https://a/1", proxy=None) == {}


async def test_the_envelope_of_the_workflow_is_exactly_this(monkeypatch) -> None:
    monkeypatch.setitem(flow.SOURCES, "serp", FakeSource("serp", _answer("serp", [_row("https://a/1", 1)], cost=0.25)))
    result = await flow.web_search(["q"], mode="parallel", sources=["serp"], top=3, engine="google", proxy="socks5://p")
    assert set(result) == {"queries", "mode", "sources", "budget_spent_usd", "ok"}
    assert (result["mode"], result["sources"], result["budget_spent_usd"], result["ok"]) == (
        "parallel",
        ["serp"],
        0.25,
        True,
    )


async def test_serp_keeps_the_engine_the_row_names(monkeypatch) -> None:
    envelope = {"ok": True, "results": [{"url": "https://a/1", "searchengine": "google"}]}
    monkeypatch.setattr(flow.subprocess, "run", FakeProcess(0, json.dumps(envelope)))
    found = await flow.serp("q", top=10, engine="yandex")
    assert found["results"][0]["engine"] == "google"  # the row wins over the question


async def test_serp_empty_keeps_the_currency_of_the_answer(monkeypatch) -> None:
    envelope = {"ok": True, "results": [], "sources": [{"cost_currency": "RUB"}], "cost_total": 0.0}
    monkeypatch.setattr(flow.subprocess, "run", FakeProcess(0, json.dumps(envelope)))
    assert await flow.serp("q", top=10) == flow._failed("serp", "empty", cost_currency="RUB")


async def test_a_paid_answer_keeps_its_cost_on_the_failure_path(monkeypatch) -> None:
    _key(monkeypatch)
    _client(monkeypatch, FakeResponse(500, {"costDollars": {"total": 0.002}}))
    assert await flow.exa("q", top=1) == flow._failed("exa", "http:500", cost_usd=0.002)
    _client(monkeypatch, FakeResponse(200, {"results": [], "costDollars": {"total": 0.003}}))
    assert await flow.exa("q", top=1) == flow._failed("exa", "empty", cost_usd=0.003)
    _client(monkeypatch, FakeResponse(500, {"usage": {"credits": 2}}))
    assert await flow.tavily("q", top=1) == flow._failed("tavily", "http:500", cost_usd=0.002)
    _client(monkeypatch, FakeResponse(200, {"results": [], "usage": {"credits": 3}}))
    assert await flow.tavily("q", top=1) == flow._failed("tavily", "empty", cost_usd=0.003)


async def test_the_budget_skips_every_remaining_query(monkeypatch) -> None:
    monkeypatch.setitem(flow.SOURCES, "exa", FakeSource("exa", _answer("exa", [_row("https://a/1", 1)], cost=0.5)))
    result = await flow.web_search(["a", "b", "c"], mode="parallel", sources=["exa"], budget_usd=0.5)
    assert [entry.get("reason") for entry in result["queries"]] == [None, "budget_exhausted", "budget_exhausted"]


async def test_the_failed_fetch_is_a_document_of_its_url(monkeypatch) -> None:
    async def page(*_args: object, **_kwargs: object) -> dict:
        return {}

    async def blocked(*_args: object, **_kwargs: object) -> dict:
        return {"ok": False, "reason": "blocked"}

    monkeypatch.setattr(flow, "_html", page)
    monkeypatch.setattr(flow.adapters, "spider", blocked)
    found = await flow._main_text("https://a/1", proxy=None)
    assert found == document("https://a/1", source="none", ok=False, reason="blocked")


async def test_fetch_top_passes_the_proxy_and_counts_every_cost(monkeypatch) -> None:
    results = [_row("https://a/1", 1), _row("https://b/2", 2)]
    monkeypatch.setitem(flow.SOURCES, "serp", FakeSource("serp", _answer("serp", results, cost=0.25)))
    seen: list[dict] = []

    async def page(url: str, *, proxy: str | None = None) -> dict:
        seen.append({"url": url, "proxy": proxy})
        return document(url, source="exa", ok=True, text="page", cost_usd=0.5 if url.endswith("1") else None)

    monkeypatch.setattr(flow, "_main_text", page)
    result = await flow.web_search(["q"], sources=["serp"], fetch_top=2, proxy="socks5://p:1")
    entry = result["queries"][0]
    assert seen == [
        {"url": "https://a/1", "proxy": "socks5://p:1"},
        {"url": "https://b/2", "proxy": "socks5://p:1"},
    ]
    assert set(entry) == {"query", "ok", "mode", "source", "results", "answer", "tried", "cost_usd", "documents"}
    assert result["budget_spent_usd"] == 0.75  # the source plus the page, and a page without a cost adds nothing


async def test_fetch_top_rounds_the_total_like_the_rest(monkeypatch) -> None:
    monkeypatch.setitem(flow.SOURCES, "serp", FakeSource("serp", _answer("serp", [_row("https://a/1", 1)])))

    async def page(url: str, *, proxy: str | None = None) -> dict:
        return document(url, source="exa", ok=True, text="page", cost_usd=4e-07)

    monkeypatch.setattr(flow, "_main_text", page)
    result = await flow.web_search(["q"], sources=["serp"], fetch_top=1)
    assert result["budget_spent_usd"] == 0.0

async def test_exa_treats_400_as_an_http_failure(monkeypatch) -> None:
    _key(monkeypatch)
    _client(monkeypatch, FakeResponse(400, {}))
    assert await flow.exa("q", top=1) == flow._failed("exa", "http:400")

async def test_a_result_without_a_text_has_no_snippet(monkeypatch) -> None:
    _key(monkeypatch)
    _client(monkeypatch, FakeResponse(200, {"results": [{"url": "https://a/1"}]}))
    assert (await flow.exa("q", top=1))["results"][0]["snippet"] is None
    _client(monkeypatch, FakeResponse(200, {"results": [{"url": "https://a/1"}]}))
    assert (await flow.tavily("q", top=1))["results"][0]["snippet"] is None

async def test_a_non_textual_answer_is_no_answer(monkeypatch) -> None:
    _key(monkeypatch)
    _client(monkeypatch, FakeResponse(200, {"results": [{"url": "https://a/1"}], "answer": {"not": "text"}}))
    assert (await flow.tavily("q", top=1))["answer"] is None
