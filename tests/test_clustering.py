"""`wf clustering`: the request in Arsenkin's `clustering` shape, two sources behind it, the answer in their fields.

Rules these tests pin:
- the request is accepted exactly as Arsenkin documents it (`{"tools_name": "clustering", "data": {...}}` or the bare
  `data`), validated before any paid call, and defaulted (`group` soft, `count` 3, `depth` 10);
- the `serp` source groups the phrases by the URLs their tops share, after the stoplist and, with `main`, without the
  main pages, and asks Wordstat only for the frequencies named in `ws`;
- the cluster rows carry Arsenkin's field names (`clustered`, `words`, `wssumm`, `topurl`, `main`, `agregators`, `geo`)
  whichever source produced them;
- `--source arsenkin` sends their documented body and reads their answer back into the same report.
"""

from __future__ import annotations

import json
import subprocess
from typing import Any

import pytest

from rb_workflows import clustering

ROWS: dict[str, list[str]] = {
    "ремонт киа": ["https://a.ru/1", "https://b.ru/1", "https://auto.ru/x", "https://c.ru/"],
    "сервис киа": ["https://a.ru/1", "https://b.ru/1", "https://d.ru/2"],
    "автосервис киа": ["https://e.ru/9"],
}
REQUEST: dict[str, Any] = {
    "tools_name": "clustering",
    "data": {"queries": list(ROWS), "se": 1, "region": 213, "group": "soft", "count": 3, "depth": 10},
}


def _fake_tools(monkeypatch, *, frequencies: bool = False) -> list[list[str]]:
    """A stand-in for both CLIs: `serp search` answers the tops of ROWS, `wordstat search` the frequencies."""
    calls: list[list[str]] = []

    def run(argv, **_):
        calls.append(list(argv))
        if argv[0] == "wordstat":
            return subprocess.CompletedProcess(
                argv,
                0,
                stdout=json.dumps(
                    {
                        "ok": True,
                        "results": [
                            {"query": query, "freq_type": kind, "total_value": value}
                            for query in argv[2 : argv.index("--freq")]
                            for kind, value in (("broad", 500), ("exact", 50))
                            if frequencies
                        ],
                    }
                ),
                stderr="",
            )
        rows = [
            {"serp_position": position, "url": url, "searchengine": "yandex", "title": "t"}
            for position, url in enumerate(ROWS[argv[2]], start=1)
        ]
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout=json.dumps({"ok": True, "results": rows, "sources": [], "billable_requests_total": 1}),
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", run)
    return calls


def _run(coroutine):
    import asyncio

    return asyncio.run(coroutine)


def test_the_input_is_the_documented_body_the_bare_data_or_the_defaults() -> None:
    from_body = clustering.normalise(REQUEST)
    from_data = clustering.normalise(REQUEST["data"])
    assert from_body == from_data
    assert (from_body["se"], from_body["region"]) == (1, 213)
    assert clustering.normalise({"queries": ["q"], "se": "2", "region": "1011969"})["se"] == 2


def test_what_the_documented_request_forbids_is_refused_before_any_call() -> None:
    with pytest.raises(ValueError, match="unknown search engine '3'"):
        clustering.normalise({"queries": ["q"], "se": 3, "region": 213})
    with pytest.raises(ValueError, match="no region"):
        clustering.normalise({"queries": ["q"], "se": 1})
    with pytest.raises(ValueError, match="unknown group 'middle'"):
        clustering.normalise({"queries": ["q"], "se": 1, "region": 213, "group": "middle"})
    with pytest.raises(ValueError, match=r"count must be 2\.\.10"):
        clustering.normalise({"queries": ["q"], "se": 1, "region": 213, "count": 11})
    with pytest.raises(ValueError, match="depth must be one of"):
        clustering.normalise({"queries": ["q"], "se": 1, "region": 213, "depth": 15})
    with pytest.raises(ValueError, match="unknown frequency 'wide'"):
        clustering.normalise({"queries": ["q"], "se": 1, "region": 213, "ws": ["wide"]})


def test_a_merge_needs_both_the_count_and_a_share_of_the_smaller_top() -> None:
    tops = {"a": ["u1", "u2"], "b": ["u1", "u3"], "c": ["u4"]}
    assert clustering.clusters(tops, group="soft", count=1) == [["a", "b"], ["c"]]  # one shared URL is half of a/b
    assert clustering.clusters(tops, group="hard", count=1) == [["a", "b"], ["c"]]
    assert clustering.clusters(tops, group="hard", count=2) == [["a"], ["b"], ["c"]]  # not enough shared URLs
    few = {"a": ["u1", "u2", "u3", "u4"], "b": ["u1", "u9", "u10", "u11"]}
    assert clustering.clusters(few, group="hard", count=1) == [["a"], ["b"]]  # a quarter is too little
    assert clustering.clusters(few, group="soft", count=1) == [["a"], ["b"]]


def test_the_serp_source_groups_after_the_stoplist_and_without_main_pages(monkeypatch) -> None:
    calls = _fake_tools(monkeypatch)
    request = {**REQUEST["data"], "stoplist": ["http://auto.ru"], "main": True}

    report = _run(clustering.collect(request, source="serp"))

    assert [list(call[:2]) for call in calls] == [["serp", "search"]] * 3
    assert calls[0][calls[0].index("--yandex-lr") + 1] == "213"
    clusters = report["result"]["result"]
    assert [row["words"] for row in clusters] == [["ремонт киа", "сервис киа"], ["автосервис киа"]]
    first = clusters[0]
    assert first["clustered"] == "ремонт киа"
    assert first["topurl"] == "https://a.ru/1"
    assert first["main"] == 1  # `https://c.ru/` of the first query: excluded from the grouping, still counted
    assert first["wssumm"] is None and first["agregators"] is None and first["geo"] is None
    assert report["summary"]["clusters_total"] == 2


def test_wordstat_is_asked_only_when_ws_names_a_frequency_and_feeds_wssumm(monkeypatch) -> None:
    calls = _fake_tools(monkeypatch, frequencies=True)
    request = {**REQUEST["data"], "ws": ["base", "exact"]}

    report = _run(clustering.collect(request, source="serp"))

    wordstat = [call for call in calls if call[0] == "wordstat"]
    assert len(wordstat) == 1
    assert wordstat[0][wordstat[0].index("--freq") + 1] == "broad,exact"
    first = report["result"]["result"][0]
    assert first["wssumm"] == 1000  # two phrases, 500 each
    assert first["queries"][0]["ws"] == 500
    assert first["queries"][0]["ws_strict"] == 50


def test_the_arsenkin_source_sends_the_documented_body_and_reads_their_answer(monkeypatch) -> None:
    sent: list[tuple[str, dict]] = []

    async def fake_post(_client, _token, path, body):
        sent.append((path, body))
        if path == "set":
            return {"code": "SET_TASK_OK", "task_id": 11, "cost": 2}
        if path == "check":
            return {"code": "TASK_STATUS", "status": "finish"}
        return {
            "code": "TASK_RESULT",
            "task_id": 11,
            "result": {
                "request": {"queries": ["ремонт киа"], "se": 1, "region": 213, "group": "soft", "count": 3},
                "result": [
                    {"clustered": "ремонт киа", "words": ["ремонт киа"], "wssumm": 500, "topurl": "https://a.ru"}
                ],
            },
        }

    monkeypatch.setattr(clustering.arsenkin, "_post", fake_post)

    report = _run(clustering.collect({**REQUEST["data"], "queries": ["ремонт киа"]}, source="arsenkin", token="t"))

    assert sent[0][0] == "set"
    assert sent[0][1]["tools_name"] == "clustering"
    assert sent[0][1]["data"]["queries"] == ["ремонт киа"]
    assert sent[0][1]["data"]["se"] == 1
    assert sent[0][1]["data"]["depth"] == 10
    assert report["summary"]["clusters"][0]["topurl"] == "https://a.ru"


def test_the_cli_builds_the_body_from_flags_and_prints_the_report(monkeypatch, capsys) -> None:
    from rb_workflows import cli

    seen: list[dict] = []

    async def fake_collect(request, **_):
        seen.append(dict(request))
        return {"ok": True, "summary": {"clusters_total": 1, "queries_total": 1}, "result": {"result": []}}

    monkeypatch.setattr(clustering, "collect", fake_collect)

    code = cli.main(["clustering", "--queries", "ремонт киа", "--se", "1", "--region", "213", "--ws", "base", "--json"])

    assert code == 0
    assert seen == [{"queries": ["ремонт киа"], "se": 1, "region": "213", "ws": ["base"]}]
    assert json.loads(capsys.readouterr().out)["summary"]["clusters_total"] == 1


def test_the_cli_refuses_a_call_without_queries_or_a_task(capsys) -> None:
    from rb_workflows import cli

    with pytest.raises(SystemExit) as exit_info:
        cli.main(["clustering", "--json"])
    assert exit_info.value.code == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "INVALID_ARGUMENTS"


def test_one_page_under_two_spellings_is_one_url_in_the_overlap() -> None:
    assert clustering.canonical("http://www.A.ru/1/?utm_source=x") == "a.ru/1"
    assert clustering.canonical("https://a.ru/1") == "a.ru/1"
    assert clustering.canonical("https://m.a.ru/1?b=2&a=1") == "a.ru/1?a=1&b=2"  # a real query still tells pages apart
    assert clustering.canonical("https://a.ru/index.html") == "a.ru"
    tops = {"a": ["https://a.ru/1", "https://a.ru/2"], "b": ["http://a.ru/1/", "https://www.a.ru/2"]}
    assert clustering.clusters(tops, group="hard", count=2) == [["a", "b"]]


def test_a_main_page_is_recognised_under_its_other_addresses() -> None:
    assert clustering._is_main("https://a.ru/")
    assert clustering._is_main("https://www.a.ru/index.html")
    assert not clustering._is_main("https://a.ru/1")


def test_their_real_answer_is_read_into_clusters_with_the_single_phrases_too() -> None:
    payload = {
        "task_id": 5,
        "result": {
            "clustering": {
                "clustered": {
                    "аутсорсинг вэд": {
                        "words": {
                            "аутсорсинг вэд": {"ws": 129, "main": 0, "agregators": 0, "toponim": "-"},
                            "услуги вэд москва": {"ws": 25, "main": 3, "agregators": 10, "toponim": "москва"},
                        },
                        "wssumm": 154,
                        "topurl": "https://a.ru/1",
                        "topurls": {"https://a.ru/1": 2},
                    }
                },
                "single": {
                    "траско смоленск": {
                        "words": {"траско смоленск": {"ws": 3, "main": 1, "agregators": 0, "toponim": "-"}},
                        "wssumm": 3,
                        "topurl": "https://b.ru/2",
                        "topurls": {"https://b.ru/2": 1},
                    }
                },
            },
            "stats": {"words": 3, "single": 1, "singlePersent": 33, "domains": {"hosts": {"a.ru": {"cnt": 2}}}},
        },
    }

    report = clustering.report_from_remote(payload)

    assert report["summary"]["clusters_total"] == 2
    assert (report["summary"]["single"], report["summary"]["single_percent"]) == (1, 33)
    first = report["result"]["result"][0]
    assert (first["clustered"], first["words"], first["wssumm"], first["main"]) == (
        "аутсорсинг вэд",
        ["аутсорсинг вэд", "услуги вэд москва"],
        154,
        3,
    )
    assert first["agregators"] == 10  # the worst share among the phrases
    assert first["queries"][1]["toponim"] == "москва"
    assert first["queries"][0]["toponim"] is None  # their `-` is “no toponym”


def test_one_popular_url_does_not_collapse_the_whole_batch() -> None:
    tops = {
        "a": ["u1", "a2", "a3", "a4"],
        "b": ["u1", "b2", "b3", "b4"],
        "c": ["u1", "c2", "c3", "c4"],
    }
    assert clustering.clusters(tops, group="soft", count=1) == [["a"], ["b"], ["c"]]
