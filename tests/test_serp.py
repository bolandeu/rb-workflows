"""`wf serp`: a batch of queries through the `serp` CLI, and the cluster-strength summary.

Behaviour first, code after: these tests name what the command promises — the shape of the report, the parallel
fan-out, the isolation of a failed query, and the strength formula a URL gets for its places in the cluster.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable

import pytest

from rb_workflows import serp as serp_source


def _envelope(query: str, rows: list[tuple[int, str, str, str]], *, engine: str = "yandex") -> dict:
    """The `serp search --json` envelope for one query, as the CLI prints it."""
    return {
        "ok": True,
        "count": len(rows),
        "results": [
            {
                "serp_position": position,
                "query": query,
                "searchengine": engine,
                "url": url,
                "domain": domain,
                "title": title,
            }
            for position, url, domain, title in rows
        ],
        "sources": [{"query": query, "engine": engine, "provider": "xmlriver", "cost": 0.3, "from_cache": False}],
        "billable_requests_total": 1,
        "cost_total": 0.3,
        "cost_currency": "RUB",
    }


def _fake_serp(
    answers: dict[str, dict], calls: list[list[str]] | None = None
) -> Callable[..., subprocess.CompletedProcess]:
    """A stand-in for `serp search`: the answer is chosen by the last positional argument (the query)."""

    def run(argv, **kwargs):
        calls.append(list(argv)) if calls is not None else None
        query = argv[2]  # serp search <query> …
        answer = answers.get(query)
        if answer is None:
            return subprocess.CompletedProcess(
                argv,
                5,
                stdout=json.dumps(
                    {"ok": False, "error": {"code": "PROVIDER_ERROR", "message": "all providers refused"}}
                ),
                stderr="",
            )
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(answer), stderr="")

    return run


async def test_one_query_one_engine_gives_one_entry_and_the_leader_is_strength_one(monkeypatch) -> None:
    answers = {"seo": _envelope("seo", [(1, "https://a.ru/x", "a.ru", "A"), (2, "https://b.ru/y", "b.ru", "B")])}
    monkeypatch.setattr(serp_source.subprocess, "run", _fake_serp(answers))
    report = await serp_source.collect(["seo"], engine="yandex", top=10)

    assert report["ok"] is True
    assert [(entry["query"], entry["serp_engine"], entry["ok"]) for entry in report["results"]] == [
        ("seo", "yandex", True)
    ]
    assert [row["position"] for row in report["results"][0]["rows"]] == [1, 2]
    leader = report["summary"]["top"][0]
    assert (leader["url"], leader["strength"], leader["best_position"]) == ("https://a.ru/x", 1.0, 1)


async def test_both_engines_give_two_entries_per_query_and_count_each_position(monkeypatch) -> None:
    rows = [
        {
            "serp_position": 1,
            "query": "seo",
            "searchengine": "yandex",
            "url": "https://a.ru/x",
            "domain": "a.ru",
            "title": "A",
        },
        {
            "serp_position": 5,
            "query": "seo",
            "searchengine": "google",
            "url": "https://a.ru/x",
            "domain": "a.ru",
            "title": "A",
        },
    ]
    envelope = {
        "ok": True,
        "results": rows,
        "sources": [],
        "billable_requests_total": 2,
        "cost_total": 0.6,
        "cost_currency": "RUB",
    }
    monkeypatch.setattr(serp_source.subprocess, "run", _fake_serp({"seo": envelope}))
    report = await serp_source.collect(["seo"], engine="both", top=10)

    assert sorted(entry["serp_engine"] for entry in report["results"]) == ["google", "yandex"]
    url = report["summary"]["top"][0]
    assert (url["appearances"], url["queries_count"], url["best_position"]) == (2, 1, 1)
    assert url["by_query"] == [{"query": "seo", "yandex": 1, "google": 5}]


async def test_the_batch_runs_in_parallel_and_never_more_than_the_limit(monkeypatch) -> None:
    running = {"now": 0, "peak": 0}

    def run(argv, **kwargs):
        running["now"] += 1
        running["peak"] = max(running["peak"], running["now"])
        running["now"] -= 1
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(_envelope(argv[-2], [])), stderr="")

    monkeypatch.setattr(serp_source.subprocess, "run", run)
    report = await serp_source.collect([f"q{n}" for n in range(6)], engine="yandex", concurrency=2)

    assert [entry["query"] for entry in report["results"]] == [f"q{n}" for n in range(6)]
    assert running["peak"] <= 2  # the limit is respected
    assert report["ok"] is True


async def test_a_failed_query_is_reported_and_the_others_still_count(monkeypatch) -> None:
    answers = {"good": _envelope("good", [(1, "https://a.ru/x", "a.ru", "A")])}
    monkeypatch.setattr(serp_source.subprocess, "run", _fake_serp(answers))
    report = await serp_source.collect(["good", "broken"], engine="yandex", top=10)

    failed = next(entry for entry in report["results"] if entry["query"] == "broken")
    assert (failed["ok"], failed["rows"]) == (False, [])
    assert "PROVIDER_ERROR" in failed["error"]
    assert report["ok"] is False
    assert [row["url"] for row in report["summary"]["top"]] == ["https://a.ru/x"]


async def test_strength_is_normalised_over_queries_and_engines(monkeypatch) -> None:
    # `both` URLs: the first is 1st in Yandex and 1st in Google for one query; the second is 10th in Yandex only.
    rows = [
        {
            "serp_position": 1,
            "query": "q1",
            "searchengine": "yandex",
            "url": "https://a.ru/1",
            "domain": "a.ru",
            "title": "A",
        },
        {
            "serp_position": 1,
            "query": "q1",
            "searchengine": "google",
            "url": "https://a.ru/1",
            "domain": "a.ru",
            "title": "A",
        },
        {
            "serp_position": 10,
            "query": "q1",
            "searchengine": "yandex",
            "url": "https://b.ru/2",
            "domain": "b.ru",
            "title": "B",
        },
    ]
    envelope = {
        "ok": True,
        "results": rows,
        "sources": [],
        "billable_requests_total": 2,
        "cost_total": 0.6,
        "cost_currency": "RUB",
    }
    monkeypatch.setattr(serp_source.subprocess, "run", _fake_serp({"q1": envelope}))
    report = await serp_source.collect(["q1"], engine="both", top=10)

    by_url = {row["url"]: row for row in report["summary"]["top"]}
    assert by_url["https://a.ru/1"]["strength"] == 1.0
    assert by_url["https://b.ru/2"]["strength"] == pytest.approx(0.05)  # (10-10+1)/10 over 1 query x 2 engines
    assert [row["url"] for row in report["summary"]["top"]] == ["https://a.ru/1", "https://b.ru/2"]


async def test_the_summary_keeps_ten_urls_and_orders_by_strength_then_breadth(monkeypatch) -> None:
    rows = [
        {
            "serp_position": n,
            "query": "q1",
            "searchengine": "yandex",
            "url": f"https://a.ru/{n}",
            "domain": "a.ru",
            "title": "A",
        }
        for n in range(1, 15)
    ]
    envelope = {
        "ok": True,
        "results": rows,
        "sources": [],
        "billable_requests_total": 1,
        "cost_total": 0.3,
        "cost_currency": "RUB",
    }
    monkeypatch.setattr(serp_source.subprocess, "run", _fake_serp({"q1": envelope}))
    report = await serp_source.collect(["q1"], engine="yandex", top=20)

    top = report["summary"]["top"]
    assert len(top) == 10
    assert [row["best_position"] for row in top] == list(range(1, 11))
    assert report["summary"]["urls_total"] == 14


async def test_every_parameter_reaches_the_serp_cli(monkeypatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(serp_source.subprocess, "run", _fake_serp({"seo": _envelope("seo", [])}, calls))
    await serp_source.collect(
        ["seo"],
        engine="both",
        top=20,
        provider="xmlriver,xmlstock",
        cache=True,
        max_age="7d",
        yandex_lr=213,
        google_loc="1011963",
    )

    argv = calls[0]
    assert argv[:3] == ["serp", "search", "seo"]
    assert argv[argv.index("--engine") : argv.index("--engine") + 2] == ["--engine", "both"]
    assert argv[argv.index("--top") : argv.index("--top") + 2] == ["--top", "20"]
    assert argv[argv.index("--provider") : argv.index("--provider") + 2] == ["--provider", "xmlriver,xmlstock"]
    assert "--cache" in argv
    assert argv[argv.index("--max-age") : argv.index("--max-age") + 2] == ["--max-age", "7d"]
    assert argv[argv.index("--yandex-lr") : argv.index("--yandex-lr") + 2] == ["--yandex-lr", "213"]
    assert argv[argv.index("--google-loc") : argv.index("--google-loc") + 2] == ["--google-loc", "1011963"]
    assert argv[-1] == "--json"


async def test_a_non_json_answer_is_an_entry_error_not_a_traceback(monkeypatch) -> None:
    monkeypatch.setattr(
        serp_source.subprocess,
        "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, stdout="<html>captcha</html>", stderr=""),
    )
    report = await serp_source.collect(["seo"], engine="yandex")
    assert report["results"][0]["ok"] is False
    assert "not JSON" in report["results"][0]["error"]


async def test_a_missing_serp_binary_is_an_entry_error(monkeypatch) -> None:
    def run(argv, **kwargs):
        raise FileNotFoundError("serp")

    monkeypatch.setattr(serp_source.subprocess, "run", run)
    report = await serp_source.collect(["seo"], engine="yandex")
    assert report["results"][0]["ok"] is False
    assert "serp" in report["results"][0]["error"]


def test_the_cli_exits_by_the_result_and_prints_the_report(monkeypatch, capsys) -> None:
    from rb_workflows import cli

    async def fake_collect(queries, **kwargs):
        return {
            "ok": True,
            "engine": "yandex",
            "top": 10,
            "queries": list(queries),
            "elapsed_s": 0.0,
            "results": [],
            "sources": [],
            "billable_requests_total": 0,
            "cost_total": 0.0,
            "cost_currency": "RUB",
            "summary": {"engines": 1, "queries": len(queries), "urls_total": 0, "top": []},
        }

    monkeypatch.setattr(serp_source, "collect", fake_collect)
    assert cli.main(["serp", "--queries", "seo", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["queries"] == ["seo"]


def test_the_cli_refuses_an_unknown_engine(capsys) -> None:
    from rb_workflows import cli

    with pytest.raises(SystemExit) as exit_info:
        cli.main(["serp", "--queries", "seo", "--engine", "bing"])
    assert exit_info.value.code == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "INVALID_ARGUMENTS"
