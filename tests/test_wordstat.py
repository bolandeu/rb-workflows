"""`wf wordstat`: Arsenkin's Wordstat API — type 1 frequencies, type 2 phrase parsing — from their task or our CLI.

Rules these tests pin:
- the request is accepted exactly as Arsenkin documents it (`{"tools_name": "wordstat", "data": {...}}` or the bare
  `data`), validated before any paid call, and defaulted (`type` 1, `ws` base, the three booleans true, all devices);
- the answer keeps their layout — `result.data.result[query][region][ws]` for type 1, `result.data[]` rows with `freq`,
  `left`, `right` for type 2 — whichever source produced it;
- our CLI is asked only for the frequencies named, with Arsenkin's names translated (`overal` is `!фраза`, `exact` is
  `[!фраза]`), and its similar phrases become the left column, its associations the right one;
- what our CLI cannot do (a single device, the whole country) is refused or explained, never silently answered.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from rb_workflows import wordstat

FREQUENCY: dict = {
    "tools_name": "wordstat",
    "data": {"type": 1, "queries": ["ремонт киа"], "regions": [213, 225], "ws": ["base", "exact"], "device": ""},
}
PHRASES: dict = {
    "tools_name": "wordstat",
    "data": {"type": 2, "queries": ["ремонт киа"], "region": 213, "minus_words": ["дилер"], "is_right": True},
}


def _fake_cli(monkeypatch, *, popular=None, associations=None) -> list[list[str]]:
    """A stand-in for `wordstat search --json`: one row per query and frequency type."""
    calls: list[list[str]] = []

    def run(argv, **_):
        calls.append(list(argv))
        kinds = argv[argv.index("--freq") + 1].split(",")
        queries = argv[2 : argv.index("--freq")]
        results = [
            {
                "query": query,
                "freq_type": kind,
                "total_value": 100 if kind == "broad" else 7,
                "popular": popular if popular is not None else [{"text": query, "value": 100}],
                "associations": associations or [],
            }
            for query in queries
            for kind in kinds
        ]
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps({"ok": True, "results": results}), stderr="")

    monkeypatch.setattr(subprocess, "run", run)
    return calls


def _run(coroutine):
    import asyncio

    return asyncio.run(coroutine)


def test_the_documented_body_the_bare_data_and_the_defaults_are_the_same() -> None:
    from_body = wordstat.normalise(FREQUENCY)
    assert from_body == wordstat.normalise(FREQUENCY["data"])
    defaulted = wordstat.normalise({"queries": ["q"], "regions": [213]})
    assert (defaulted["type"], defaulted["ws"], defaulted["device"]) == (1, ["base"], "")
    parsed = wordstat.normalise({"queries": ["q"], "region": 213})
    assert (parsed["type"], parsed["is_clear_minus"], parsed["is_right"], parsed["is_clear"]) == (2, True, True, True)


def test_what_the_documented_request_forbids_is_refused_before_any_call() -> None:
    with pytest.raises(ValueError, match="unknown type '3'"):
        wordstat.normalise({"type": 3, "queries": ["q"], "regions": [213]})
    with pytest.raises(ValueError, match="no queries"):
        wordstat.normalise({"queries": [], "regions": [213]})
    with pytest.raises(ValueError, match="at most 4 regions"):
        wordstat.normalise({"queries": ["q"], "regions": [213, 225, 2, 35, 65]})
    with pytest.raises(ValueError, match="unknown device 'tv'"):
        wordstat.normalise({"queries": ["q"], "regions": [213], "device": "tv"})
    with pytest.raises(ValueError, match="unknown frequency 'wide'"):
        wordstat.normalise({"queries": ["q"], "regions": [213], "ws": ["wide"]})
    with pytest.raises(ValueError, match="no region"):
        wordstat.normalise({"type": 2, "queries": ["q"]})


def test_type_1_asks_our_cli_for_every_named_frequency_in_every_region(monkeypatch) -> None:
    calls = _fake_cli(monkeypatch)

    report = _run(wordstat.collect(FREQUENCY, source="wordstat", cache=True))

    assert [call[call.index("--lr") + 1] for call in calls] == ["213", "225"]
    assert calls[0][calls[0].index("--freq") + 1] == "broad,exact-ord"  # base is broad, exact is `[!фраза]`
    assert "--cache" in calls[0]
    numbers = report["result"]["data"]["result"]
    assert numbers["ремонт киа"]["213"] == {"base": 100, "exact": 7}
    assert report["summary"]["frequencies"] == 4


def test_type_2_parses_phrases_and_applies_the_minus_words_the_right_column_and_the_clear_mode(monkeypatch) -> None:
    _fake_cli(
        monkeypatch,
        popular=[
            {"text": "ремонт киа", "value": 100},
            {"text": "ремонт киа дилер", "value": 40},
            {"text": "+ремонт киа", "value": 5},
        ],
        associations=[{"text": "kia k3", "value": 90}],
    )

    report = _run(wordstat.collect(PHRASES, source="wordstat"))
    row = report["result"]["data"][0]

    assert row["freq"] == 100
    assert row["left"] == {"ремонт киа": 105}  # `+ремонт киа` clears to the same phrase: the numbers add up
    assert "ремонт киа дилер" not in row["left"]  # the minus-word
    assert all("+" not in phrase for phrase in row["left"])  # `is_clear`
    assert row["right"] == {"kia k3": 90}

    quiet = _run(wordstat.collect({**PHRASES["data"], "is_right": False}, source="wordstat"))
    assert quiet["result"]["data"][0]["right"] == {}


def test_the_device_and_the_depth_go_to_our_cli(monkeypatch) -> None:
    calls = _fake_cli(monkeypatch)

    report = _run(wordstat.collect({**PHRASES["data"], "device": "desktop", "limit": 500}, source="wordstat"))

    argv = calls[0]
    assert argv[argv.index("--device") + 1] == "desktop"
    assert argv[argv.index("--limit") + 1] == "500"
    assert report["result"]["data"][0]["freq"] == 100


def test_the_arsenkin_source_sends_the_documented_body_for_each_type(monkeypatch) -> None:
    sent: list[dict] = []
    answer: dict = {}

    async def fake_post(_client, _token, path, body):
        if path == "set":
            sent.append(body)
            return {"code": "SET_TASK_OK", "task_id": 21, "cost": 2}
        if path == "check":
            return {"code": "TASK_STATUS", "status": "finish"}
        return {"code": "TASK_RESULT", "task_id": 21, "result": answer}

    monkeypatch.setattr(wordstat.arsenkin, "_post", fake_post)

    answer = {
        "type": 1,
        "queries": ["ремонт киа"],
        "device": None,
        "data": {"result": {"ремонт киа": {"213": {"base": 3338}}}},
    }
    report = _run(wordstat.collect(FREQUENCY["data"], source="arsenkin", token="t"))
    assert sent[0]["tools_name"] == "wordstat"
    assert sent[0]["data"] == {
        "type": 1,
        "queries": ["ремонт киа"],
        "regions": [213, 225],
        "ws": ["base", "exact"],
        "device": "",
    }
    assert report["result"]["data"]["result"]["ремонт киа"]["213"] == {"base": 3338}

    answer = {
        "type": 2,
        "queries": ["ремонт киа"],
        "is_clear": True,
        "device": None,
        "minus": [],
        "data": [{"freq": 3338, "left": {"ремонт киа": 3338}, "right": {"kia k3": 2149}}],
    }
    report = _run(wordstat.collect(PHRASES["data"], source="arsenkin", token="t"))
    assert sent[1]["data"]["region"] == 213
    assert sent[1]["data"]["minus_words"] == ["дилер"]
    assert report["result"]["data"][0]["right"] == {"kia k3": 2149}
    assert report["summary"]["left_total"] == 1


def test_their_real_frequency_answer_is_read_into_the_documented_layout(monkeypatch) -> None:
    async def fake_post(_client, _token, path, _body):
        if path == "set":
            return {"code": "SET_TASK_OK", "task_id": 7}
        if path == "check":
            return {"code": "TASK_STATUS", "status": "finish"}
        return {
            "code": "TASK_RESULT",
            "task_id": 7,
            "result": {
                "type": 1,
                "task_id": "7",
                "data": {
                    "device": None,
                    "task_id": "7",
                    "queries": ["ремонт киа", "сервис киа"],
                    "regions": {"213": "Москва [213]"},
                    "result": {
                        "ремонт киа": {"213": {"base": 3338, "exact": 21}},
                        "сервис киа": {"213": {"base": 1466, "exact": 49}},
                    },
                },
            },
        }

    monkeypatch.setattr(wordstat.arsenkin, "_post", fake_post)

    report = _run(
        wordstat.collect(
            {"type": 1, "queries": ["ремонт киа", "сервис киа"], "regions": [213], "ws": ["base", "exact"]},
            source="arsenkin",
            token="t",
        )
    )

    assert report["result"]["data"]["result"]["сервис киа"]["213"] == {"base": 1466, "exact": 49}
    assert report["result"]["type"] == 1


def test_the_cli_builds_the_body_from_flags(monkeypatch, capsys) -> None:
    from rb_workflows import cli

    seen: list[dict] = []

    async def fake_collect(request, **_):
        seen.append(dict(request))
        return {"ok": True, "summary": {"frequencies": 1}, "result": {"type": 1, "queries": [], "data": {"result": {}}}}

    monkeypatch.setattr(wordstat, "collect", fake_collect)

    code = cli.main(
        ["wordstat", "--type", "1", "--queries", "ремонт киа", "--regions", "213,225", "--ws", "base", "--json"]
    )

    assert code == 0
    assert seen == [{"type": 1, "queries": ["ремонт киа"], "regions": ["213", "225"], "ws": ["base"]}]
    assert json.loads(capsys.readouterr().out)["result"]["type"] == 1


def test_the_cli_refuses_a_call_without_queries(capsys) -> None:
    from rb_workflows import cli

    with pytest.raises(SystemExit) as exit_info:
        cli.main(["wordstat", "--json"])
    assert exit_info.value.code == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "INVALID_ARGUMENTS"


def test_a_frequency_our_cli_spells_with_an_underscore_is_still_read(monkeypatch) -> None:
    def run(argv, **_):
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout=json.dumps(
                {
                    "ok": True,
                    "results": [{"query": "таможенное оформление", "freq_type": "exact_ord", "total_value": 100}],
                }
            ),
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", run)

    report = _run(
        wordstat.collect(
            {"type": 1, "queries": ["таможенное оформление"], "regions": [213], "ws": ["exact"]}, source="wordstat"
        )
    )
    assert report["result"]["data"]["result"]["таможенное оформление"]["213"] == {"exact": 100}
