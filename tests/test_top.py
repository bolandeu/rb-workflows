"""`wf top`: the task in Arsenkin's `check-top` shape, the answer in Arsenkin's shape, the source selectable.

Rules these tests pin:
- the request is accepted exactly as Arsenkin documents it (`{"tools_name": "check-top", "data": {...}}`, or the bare
  `data` object) and is validated before any call;
- the answer keeps Arsenkin's layout — `result.request` echoes what was asked, `result.result.collect[query][se]` is the
  list of URLs in the order of the results — whichever source produced it;
- on top of that the report carries the strength of every URL and the top-N by strength, which is the reason to use
  this wrapper at all;
- `--source serp` (our own CLI, the default) and `--source arsenkin` produce the same shape from equivalent answers.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from typing import Any

import pytest

from rb_workflows import top

REQUEST: dict[str, Any] = {
    "tools_name": "check-top",
    "data": {
        "queries": ["ремонт киа", "автосервис киа"],
        "se": [{"type": 1, "region": 213}, {"type": 11, "region": 1011969}],
        "depth": 10,
        "is_snippet": True,
        "noreask": False,
    },
}


def _serp_answer(rows: list[tuple[str, str, int, str]]) -> Callable[..., subprocess.CompletedProcess]:
    """`serp search --json`: rows are (query, searchengine, position, url)."""
    by_query: dict[str, list[dict]] = {}
    for query, engine, position, url in rows:
        by_query.setdefault(query, []).append(
            {
                "serp_position": position,
                "query": query,
                "searchengine": engine,
                "url": url,
                "domain": url.split("/")[2],
                "title": url,
            }
        )

    def run(argv, **kwargs):
        query = argv[2]
        found = by_query.get(query, [])
        engine_flag = argv[argv.index("--engine") + 1]
        if engine_flag != "both":
            found = [row for row in found if row["searchengine"] == engine_flag]
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout=json.dumps(
                {"ok": True, "results": found, "sources": [], "billable_requests_total": 1, "cost_total": 0.3}
            ),
            stderr="",
        )

    return run


def test_the_input_is_accepted_as_the_full_body_or_the_bare_data() -> None:
    from_body = top.normalise(REQUEST)
    from_data = top.normalise(REQUEST["data"])
    assert from_body == from_data
    assert [se["type"] for se in from_body["se"]] == [1, 11]


def test_an_unknown_search_engine_type_is_refused_before_any_call() -> None:
    with pytest.raises(ValueError, match="unknown search engine type 99"):
        top.normalise({"queries": ["q"], "se": [{"type": 99, "region": 213}], "depth": 10})


def test_the_arsenkin_body_is_exactly_the_documented_one(monkeypatch) -> None:
    sent: list[tuple[str, dict]] = []

    async def fake_post(client, token, path, body):
        sent.append((path, body))
        if path == "set":
            return {"code": "SET_TASK_OK", "task_id": 7, "cost": 1}
        if path == "check":
            return {"code": "TASK_STATUS", "status": "finish"}
        return {
            "code": "TASK_RESULT",
            "task_id": 7,
            "result": {"result": {"collect": [[["https://a/1"], ["https://b/1"]]]}},
        }

    monkeypatch.setattr(top.arsenkin, "_post", fake_post)
    report = await_ok(top.collect(REQUEST, source="arsenkin", token="t"))

    assert sent[0] == (
        "set",
        {
            "tools_name": "check-top",
            "data": {
                "queries": ["ремонт киа", "автосервис киа"],
                "is_snippet": True,
                "noreask": False,
                "se": [{"type": 1, "region": 213}, {"type": 11, "region": 1011969}],
                "depth": 10,
            },
        },
    )
    assert report["result"]["result"]["collect"][0][0] == ["https://a/1"]
    assert report["result"]["request"]["ss"] == [{"ss": 1, "region": 213}, {"ss": 11, "region": 1011969}]


def await_ok(coroutine):
    import asyncio

    return asyncio.run(coroutine)


def test_the_serp_source_builds_the_same_shape_and_passes_regions_and_depth(monkeypatch) -> None:
    calls: list[list[str]] = []

    def run(argv, **kwargs):
        calls.append(list(argv))
        rows = [
            {
                "serp_position": 1,
                "query": argv[2],
                "searchengine": "yandex",
                "url": f"https://y/{argv[2]}",
                "domain": "y",
                "title": "t",
            },
            {
                "serp_position": 1,
                "query": argv[2],
                "searchengine": "google",
                "url": f"https://g/{argv[2]}",
                "domain": "g",
                "title": "t",
            },
        ]
        return subprocess.CompletedProcess(
            argv, 0, stdout=json.dumps({"ok": True, "results": rows, "sources": []}), stderr=""
        )

    monkeypatch.setattr(top.serp_source.subprocess, "run", run)
    report = await_ok(top.collect(REQUEST, source="serp"))

    first = calls[0]
    assert first[:3] == ["serp", "search", "ремонт киа"]
    assert first[first.index("--engine") : first.index("--engine") + 2] == ["--engine", "both"]
    assert first[first.index("--top") : first.index("--top") + 2] == ["--top", "10"]
    assert first[first.index("--yandex-lr") : first.index("--yandex-lr") + 2] == ["--yandex-lr", "213"]
    assert first[first.index("--google-loc") : first.index("--google-loc") + 2] == ["--google-loc", "1011969"]
    collect = report["result"]["result"]["collect"]
    assert collect[0][0] == ["https://y/ремонт киа"]  # query 0, se 0 (Yandex)
    assert collect[0][1] == ["https://g/ремонт киа"]  # query 0, se 1 (Google)


def test_a_depth_below_the_cli_minimum_is_raised_and_then_truncated(monkeypatch) -> None:
    seen: list[str] = []

    def run(argv, **kwargs):
        seen.append(argv[argv.index("--top") + 1])
        rows = [
            {
                "serp_position": n,
                "query": argv[2],
                "searchengine": "yandex",
                "url": f"https://a/{n}",
                "domain": "a",
                "title": "t",
            }
            for n in range(1, 11)
        ]
        return subprocess.CompletedProcess(
            argv, 0, stdout=json.dumps({"ok": True, "results": rows, "sources": []}), stderr=""
        )

    monkeypatch.setattr(top.serp_source.subprocess, "run", run)
    report = await_ok(top.collect({"queries": ["q"], "se": [{"type": 1, "region": 213}], "depth": 5}, source="serp"))

    assert seen == ["10"]  # the CLI's own minimum
    assert report["result"]["result"]["collect"][0][0] == [f"https://a/{n}" for n in range(1, 6)]  # truncated to depth


def test_a_search_engine_the_serp_cli_cannot_ask_is_an_empty_list_not_a_crash(monkeypatch) -> None:
    monkeypatch.setattr(
        top.serp_source.subprocess,
        "run",
        lambda argv, **kw: subprocess.CompletedProcess(argv, 0, stdout="{}", stderr=""),
    )
    request = {"queries": ["q"], "se": [{"type": 20, "region": "RU|ru"}, {"type": 1, "region": 213}], "depth": 10}
    report = await_ok(top.collect(request, source="serp"))

    assert report["ok"] is True  # a source that cannot answer an engine is not a failure
    assert report["skipped"] == [0]  # YouTube: not ours to ask
    assert report["result"]["result"]["collect"][0][0] == []


def test_both_sources_give_the_same_layout(monkeypatch) -> None:
    async def fake_post(client, token, path, body):
        if path == "set":
            return {"code": "SET_TASK_OK", "task_id": 1}
        if path == "check":
            return {"code": "TASK_STATUS", "status": "finish"}
        return {"code": "TASK_RESULT", "result": {"result": {"collect": [[["https://a/1"]], [["https://a/2"]]]}}}

    monkeypatch.setattr(top.arsenkin, "_post", fake_post)
    monkeypatch.setattr(
        top.serp_source.subprocess,
        "run",
        _serp_answer([("ремонт киа", "yandex", 1, "https://a/1"), ("автосервис киа", "yandex", 1, "https://a/2")]),
    )
    request = {"queries": ["ремонт киа", "автосервис киа"], "se": [{"type": 1, "region": 213}], "depth": 10}

    from_arsenkin = await_ok(top.collect(request, source="arsenkin", token="t"))
    from_serp = await_ok(top.collect(request, source="serp"))

    assert from_arsenkin["result"]["result"] == from_serp["result"]["result"]
    assert from_arsenkin["summary"]["top"][0]["url"] == from_serp["summary"]["top"][0]["url"]


def test_the_summary_carries_strength_and_the_top_n() -> None:
    report = top.build_report(
        request={"queries": ["q1", "q2"], "se": [{"type": 1, "region": 213}, {"type": 11, "region": 1}], "depth": 10},
        source="serp",
        collect=[[["https://a/1", "https://a/2"], ["https://a/1"]], [["https://a/1"], ["https://b/1"]]],
        top_n=2,
    )
    summary = report["summary"]
    assert summary["top_n"] == 2
    assert [row["url"] for row in summary["top"]] == ["https://a/1", "https://b/1"]  # 0.25 vs 0.225
    assert summary["top"][0]["queries_count"] == 2
    assert summary["top"][0]["appearances"] == 3
    # three first places (1.0 each) over 2 queries x 2 engines = 0.75
    assert summary["top"][0]["strength"] == pytest.approx(0.75)


def test_the_cli_reads_the_input_file_and_exits_by_the_result(monkeypatch, tmp_path, capsys) -> None:
    from rb_workflows import cli

    path = tmp_path / "top.json"
    path.write_text(json.dumps(REQUEST), encoding="utf-8")

    async def fake_collect(request, **kwargs):
        return top.build_report(
            request=top.normalise(request), source=kwargs.get("source", "serp"), collect=[], top_n=10
        )

    monkeypatch.setattr(top, "collect", fake_collect)
    assert cli.main(["top", "--input", str(path), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True


def test_the_cli_refuses_an_unknown_source(capsys) -> None:
    from rb_workflows import cli

    with pytest.raises(SystemExit) as exit_info:
        cli.main(["top", "--input", "x.json", "--source", "google"])
    assert exit_info.value.code == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "INVALID_ARGUMENTS"


def test_flags_build_the_same_body_the_input_file_carries(monkeypatch, capsys) -> None:
    from rb_workflows import cli

    seen: list[dict] = []

    async def fake_collect(request, **kwargs):
        seen.append(dict(request) if isinstance(request, dict) else request)
        return top.build_report(request=top.normalise(request), source="serp", collect=[])

    monkeypatch.setattr(top, "collect", fake_collect)
    code = cli.main(
        [
            "top",
            "--queries",
            "ремонт киа",
            "автосервис киа",
            "--se",
            "1:213,11:1011969",
            "--depth",
            "20",
            "--is-snippet",
            "--json",
        ]  # fmt: skip
    )

    assert code == 0
    assert seen[0] == {
        "queries": ["ремонт киа", "автосервис киа"],
        "se": [{"type": 1, "region": 213}, {"type": 11, "region": 1011969}],
        "depth": 20,
        "is_snippet": True,
        "noreask": False,
    }


def test_a_malformed_se_is_refused_in_the_envelope(capsys) -> None:
    from rb_workflows import cli

    with pytest.raises(SystemExit) as exit_info:
        cli.main(["top", "--queries", "q", "--se", "yandex:213"])
    assert exit_info.value.code == 2
    assert "--se expects type:region" in json.loads(capsys.readouterr().out)["error"]["message"]
