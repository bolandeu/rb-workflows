"""`wf highlights`: the bolded words of the snippets and the topic terms, in Arsenkin's `sp` shape."""

from __future__ import annotations

import json
import subprocess

import pytest

from rb_workflows import highlights

DATA: dict = {"queries": ["ремонт киа"], "se": 1, "region": 213, "depth": 50}
REQUEST: dict[str, object] = {"tools_name": "sp", "data": DATA}

ROWS = [
    {
        "serp_position": 1,
        "query": "ремонт киа",
        "url": "https://a.ru/kia",
        "domain": "a.ru",
        "title": "Ремонт киа в Москве",
        "snippet": "Ремонт киа недорого, запчасти киа в наличии",
        "highlights": ["ремонт киа", "Москве"],
    },
    {
        "serp_position": 2,
        "query": "ремонт киа",
        "url": "https://b.ru/kia",
        "domain": "b.ru",
        "title": "Сервис киа",
        "snippet": "Ремонт киа и запчасти киа",
        "highlights": ["ремонт киа"],
    },
]


def _fake_serp(monkeypatch, *, questions: list[dict] | None = None) -> list[list[str]]:
    calls: list[list[str]] = []

    def run(argv, **_):
        calls.append(list(argv))
        if len(argv) > 1 and argv[1] == "related":
            return subprocess.CompletedProcess(argv, 0, stdout=json.dumps({"ok": True, "results": questions or []}))
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps({"ok": True, "results": ROWS}))

    monkeypatch.setattr(subprocess, "run", run)
    return calls


def _run(coroutine):
    import asyncio

    return asyncio.run(coroutine)


def test_the_sp_body_is_accepted_as_is_and_defaulted() -> None:
    assert highlights.normalise(REQUEST)["depth"] == 50
    defaulted = highlights.normalise({"queries": ["q"], "se": "2", "region": "1011969"})
    assert (defaulted["se"], defaulted["depth"], defaulted["with"]) == (2, 10, [])


def test_what_the_sp_contract_forbids_is_refused_before_any_call() -> None:
    with pytest.raises(ValueError, match="unknown search engine '3'"):
        highlights.normalise({"queries": ["q"], "se": 3, "region": 213})
    with pytest.raises(ValueError, match="no region"):
        highlights.normalise({"queries": ["q"], "se": 1})
    with pytest.raises(ValueError, match="depth must be one of"):
        highlights.normalise({"queries": ["q"], "se": 1, "region": 213, "depth": 30})
    with pytest.raises(ValueError, match="unknown block 'paa'"):
        highlights.normalise({"queries": ["q"], "se": 1, "region": 213, "with": ["paa"]})


def test_the_lsi_counts_a_term_once_per_document_and_drops_stopwords() -> None:
    found = highlights.lsi(["ремонт киа и запчасти киа", "ремонт киа в москве"])

    words = {row["term"]: row["documents"] for row in found if row["kind"] == "word"}
    assert words["ремонт"] == 2 and words["киа"] == 2
    assert words["москве"] == 1
    assert "и" not in words and "в" not in words
    phrases = {row["term"]: row["documents"] for row in found if row["kind"] == "phrase"}
    assert phrases["ремонт киа"] == 2
    assert found[0]["share"] == 1.0


def test_our_source_bolds_the_words_and_counts_the_topic(monkeypatch) -> None:
    calls = _fake_serp(monkeypatch, questions=[{"query": "ремонт киа", "kind": "question", "text": "сколько стоит?"}])

    report = _run(highlights.collect({**DATA, "with": ["questions"]}, source="serp", cache=True))

    snippets = calls[0]
    assert snippets[:3] == ["serp", "snippets", "ремонт киа"]
    assert snippets[snippets.index("--top") + 1] == "50"
    assert snippets[snippets.index("--yandex-lr") + 1] == "213"
    assert "--cache" in snippets
    related = next(call for call in calls[1:] if call[1] == "related")
    assert "--kind" not in related  # `serp related` has no such flag: both kinds come back, the label tells them apart

    found = report["result"]["result"]["ремонт киа"]
    assert [row["url"] for row in found["results"]] == ["https://a.ru/kia", "https://b.ru/kia"]
    assert found["highlights"] == [{"phrase": "ремонт киа", "results": 2}, {"phrase": "Москве", "results": 1}]
    top_terms = [row["term"] for row in found["lsi"][:5]]
    assert {"ремонт", "киа"} <= set(top_terms)  # the words every result uses stay at the top
    assert report["result"]["questions"]["results"][0]["text"] == "сколько стоит?"
    assert report["summary"]["results"] == 2


def test_a_serp_refusal_is_a_warning_not_a_crash(monkeypatch) -> None:
    def run(argv, **kwargs):
        return subprocess.CompletedProcess(
            argv, 5, stdout=json.dumps({"ok": False, "error": {"code": "PROVIDER_ERROR"}})
        )

    monkeypatch.setattr(subprocess, "run", run)

    report = _run(highlights.collect(DATA, source="serp"))

    assert report["ok"] is True and "PROVIDER_ERROR" in report["warning"]
    assert report["summary"]["results"] == 0


def test_the_arsenkin_source_sends_the_sp_body_and_is_cached(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("WORKFLOWS_CACHE_PATH", str(tmp_path))
    monkeypatch.delenv("WORKFLOWS_DATABASE_URL", raising=False)
    sent: list[dict] = []

    async def fake_post(_client, _token, path, body):
        if path == "set":
            sent.append(body)
            return {"code": "SET_TASK_OK", "task_id": 3}
        if path == "check":
            return {"code": "TASK_STATUS", "status": "finish"}
        return {"code": "TASK_RESULT", "task_id": 3, "result": {"result": {"ремонт киа": [{"url": "https://a.ru"}]}}}

    monkeypatch.setattr(highlights.arsenkin, "_post", fake_post)

    first = _run(highlights.collect(DATA, source="arsenkin", token="t"))
    second = _run(highlights.collect(DATA, source="arsenkin", token="t", cache=True))

    assert sent[0] == {"tools_name": "sp", "data": {"queries": ["ремонт киа"], "se": 1, "region": 213, "depth": 50}}
    assert first.get("cached") is None and second["cached"] is True
    assert second["result"]["remote"]["result"]["ремонт киа"][0]["url"] == "https://a.ru"


def test_the_cli_builds_the_sp_body_from_flags(monkeypatch, capsys) -> None:
    from rb_workflows import cli

    seen: list[dict] = []

    async def fake_collect(request, **kwargs):
        seen.append(dict(request))
        return {"ok": True, "summary": {}, "result": {"result": {}}}

    monkeypatch.setattr(highlights, "collect", fake_collect)

    code = cli.main(
        ["highlights", "--queries", "ремонт киа", "--se", "1", "--region", "213", "--with", "questions", "--json"]
    )

    assert code == 0
    assert seen == [{"queries": ["ремонт киа"], "se": 1, "region": "213", "with": ["questions"]}]
