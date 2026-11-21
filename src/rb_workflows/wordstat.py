"""`wordstat`: Arsenkin's Wordstat API — `type` 1 is frequencies, `type` 2 is phrase parsing — from their task or ours.

The contract is Arsenkin's, so a task written for their API works here unchanged:

    type 1  {"tools_name": "wordstat",
             "data": {"type": 1, "queries": [...], "regions": [213, 225], "ws": ["base", "exact"], "device": ""}}
            → result.data.result[query][region] = {"base": 3338, "exact": 21}

    type 2  {"tools_name": "wordstat",
             "data": {"type": 2, "queries": [...], "region": 213, "device": "", "minus_words": ["дилер"],
                      "is_clear_minus": true, "is_right": true, "is_clear": true}}
            → result.data = [{"freq": 3338, "left": {phrase: n}, "right": {phrase: n}}]

Two sources answer it:

- `wordstat` (our CLI, the default) — `wordstat search` per region, one call for the whole batch of phrases. Frequencies
  map as Arsenkin names them: `base` is our `broad`, `quoted` our `phrase`, `overal` our `exact` (`!фраза`), `exact` our
  `exact-ord` (`[!фраза]`). Phrase parsing is the same `broad` answer: its similar phrases are the left column, its
  associations the right one.
- `arsenkin` — their task, `set` → `check` → `get`, the answer already in this shape.

What our CLI cannot do is said before a paid call rather than silently dropped: a `device` other than all devices, and
the whole-country region `0` is asked as `225` (Russia), because our CLI takes one `lr` code, not a set.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import time
from collections.abc import Iterable, Mapping
from pathlib import Path

import httpx

from . import tasks
from .credentials import binding
from .services import arsenkin

__all__ = ["collect", "normalise", "report_from_remote"]

TOOLS_NAME = "wordstat"
SOURCES = ("wordstat", "arsenkin")
TYPES = (1, 2)
WS_TYPES = ("base", "quoted", "overal", "exact")
# Arsenkin's frequency names → the ones our CLI knows: `overal` is `!фраза`, `exact` is `[!фраза]`.
WS_TO_CLI = {"base": "broad", "quoted": "phrase", "overal": "exact", "exact": "exact-ord"}
CLI_TO_WS = {cli: name for name, cli in WS_TO_CLI.items()}
DEVICES = ("", "desktop", "mobile", "phone", "tablet")
MAX_REGIONS = 4  # Arsenkin's own ceiling for one task
RUSSIA = 225  # their `0` is the whole country; our CLI wants the `lr` code of it
TIMEOUT = 600.0  # one `wordstat search` call, seconds: a provider that hangs must not hold the batch
SLEEP = 2.0  # seconds between polls inside `wait`

# --- the request -------------------------------------------------------------------------------------------------


def normalise(request: Mapping) -> dict:
    """The `data` object of a `wordstat` request, from the full body or from the bare data; validated.

    Raises `ValueError` for anything the documented request forbids: no queries, an unknown `type`, an unknown device,
    more than four regions, a frequency name the CLI cannot collect. These are caller mistakes, so they surface before
    a paid call.
    """
    raw = request.get("data")
    data: Mapping = raw if isinstance(raw, Mapping) else request
    # `type` is mandatory in their API; a caller who names one `region` instead of a list of them means type 2
    kind = _as_int(data.get("type")) or (2 if data.get("region") is not None else 1)
    if kind not in TYPES:
        raise ValueError(f"unknown type '{data.get('type')}': 1 is frequencies, 2 is phrase parsing")
    queries = list(dict.fromkeys(str(query).strip() for query in data.get("queries") or [] if str(query).strip()))
    if not queries:
        raise ValueError("no queries: 'queries' must be a non-empty list")
    device = str(data.get("device") or "").lower()
    if device not in DEVICES:
        raise ValueError(f"unknown device '{device}'; known: desktop, mobile, phone, tablet or '' for all")
    if kind == 1:
        regions = [_as_int(region) for region in data.get("regions") or []]
        if not all(region is not None for region in regions):
            raise ValueError("regions must be Yandex `lr` codes, e.g. [213, 225]")
        if not regions:
            raise ValueError("no regions: 'regions' must name at least one, e.g. [213]")
        if len(regions) > MAX_REGIONS:
            raise ValueError(f"at most {MAX_REGIONS} regions, got {len(regions)}")
        kinds = [str(name).lower() for name in data.get("ws") or ["base"]]
        unknown = [name for name in kinds if name not in WS_TYPES]
        if unknown:
            raise ValueError(f"unknown frequency '{unknown[0]}'; known: {', '.join(WS_TYPES)}")
        return {"type": 1, "queries": queries, "regions": regions, "ws": kinds, "device": device}
    region = _as_int(data.get("region"))
    if region is None:
        regions = data.get("regions") or []
        region = _as_int(regions[0]) if regions else None
    if region is None:
        raise ValueError("no region: 'region' must be a Yandex `lr` code (213 is Moscow)")
    return {
        "type": 2,
        "queries": queries,
        "region": region,
        "device": device,
        "minus_words": [str(word).strip() for word in data.get("minus_words") or [] if str(word).strip()],
        "is_clear_minus": _flag(data.get("is_clear_minus")),
        "is_right": _flag(data.get("is_right")),
        "is_clear": _flag(data.get("is_clear")),
    }


def _as_int(value: object) -> int | None:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _flag(value: object, default: bool = True) -> bool:
    """The documented booleans default to true; anything sent is taken as it is."""
    return default if value is None else bool(value)


# --- our own source: the `wordstat` CLI ---------------------------------------------------------------------------


def _search(
    queries: Iterable[str],
    kinds: Iterable[str],
    region: int,
    *,
    provider: str | None,
    cache: bool,
    max_age: str | None,
) -> dict:
    """One `wordstat search` call for the whole batch of phrases in one region; the parsed JSON answer."""
    argv = ["wordstat", "search", *queries, "--freq", ",".join(kinds), "--lr", str(region or RUSSIA)]
    if provider:
        argv += ["--provider", provider]
    if cache:
        argv.append("--cache")
    if max_age:
        argv += ["--max-age", max_age]
    argv.append("--json")
    try:
        done = subprocess.run(  # noqa: S603 - the command is built here, not taken from input
            argv, capture_output=True, text=True, timeout=TIMEOUT, check=False
        )
    except FileNotFoundError as exc:
        raise RuntimeError(f"the wordstat CLI is not installed or not on PATH: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"wordstat did not answer in {TIMEOUT:g} s") from exc
    try:
        payload = json.loads(done.stdout)
    except ValueError:
        raise RuntimeError(
            f"wordstat answered not JSON (exit {done.returncode}): {done.stdout.strip()[:200]}"
        ) from None
    if payload.get("ok") is False:
        error = payload.get("error") or {}
        raise RuntimeError(f"wordstat: {error.get('code', 'ERROR')}: {error.get('message', 'no message')}")
    return payload


def _need_device(data: Mapping) -> None:
    """Our CLI collects all devices only: anything else is refused here, not silently answered with the wrong number."""
    if data["device"]:
        raise ValueError(f"device '{data['device']}' is not supported by our wordstat CLI: only all devices ('')")


def _frequencies(
    data: Mapping, *, provider: str | None, cache: bool, max_age: str | None
) -> tuple[dict[str, dict[str, dict[str, int]]], str | None]:
    """`{query: {region: {arsenkin kind: value}}}` plus a warning for a region that failed; never an exception."""
    wanted = [WS_TO_CLI[name] for name in data["ws"]]
    found: dict[str, dict[str, dict[str, int]]] = {query: {} for query in data["queries"]}
    warnings = []
    for region in data["regions"]:
        try:
            payload = _search(data["queries"], wanted, region, provider=provider, cache=cache, max_age=max_age)
        except RuntimeError as exc:  # one region failed: the rest of the task is still worth having
            warnings.append(f"region {region}: {exc}"[:300])
            continue
        for item in payload.get("results") or []:
            query = str(item.get("query"))
            value = item.get("total_value")
            frequency = found.setdefault(query, {}).setdefault(str(region), {})
            name = CLI_TO_WS.get(str(item.get("freq_type")))
            if name is not None and name in data["ws"] and isinstance(value, int):
                frequency[name] = value
    return found, (warnings[0] if warnings else None)


def _phrases(data: Mapping, *, provider: str | None, cache: bool, max_age: str | None) -> tuple[list[dict], str | None]:
    """One row per query: its broad frequency, the similar phrases (left) and the associations (right)."""
    rows = []
    warning = None
    for query in data["queries"]:
        try:
            payload = _search([query], ["broad"], data["region"], provider=provider, cache=cache, max_age=max_age)
        except RuntimeError as exc:  # a failed query is a value: the batch goes on
            warning = warning or str(exc)[:300]
            rows.append({"freq": None, "left": {}, "right": {}})
            continue
        item = (payload.get("results") or [{}])[0]
        left = _column(item.get("popular") or [], data)
        right = _column(item.get("associations") or [], data) if data["is_right"] else {}
        rows.append({"freq": item.get("total_value"), "left": left, "right": right})
    return rows, warning


def _column(items: Iterable[Mapping], data: Mapping) -> dict[str, int]:
    """One Wordstat column as `{phrase: frequency}`, after the minus-words and the “+” of the clear mode.

    The clear mode can turn two entries into one phrase (`киа` and `+киа`); their frequencies are added up, so nothing
    is lost and no phrase appears twice in the answer.
    """
    words = [word.lower() for word in data["minus_words"]]
    column: dict[str, int] = {}
    for item in items:
        phrase = str(item.get("text") or "")
        value = item.get("value")
        if not phrase or not isinstance(value, int):
            continue
        if data["is_clear_minus"] and any(word in phrase.lower() for word in words):
            continue
        key = phrase.replace("+", "") if data["is_clear"] else phrase
        column[key] = column.get(key, 0) + value
    return column


# --- the report -------------------------------------------------------------------------------------------------


def build_report(
    *,
    request: Mapping,
    source: str,
    frequencies: Mapping[str, Mapping[str, Mapping[str, int]]] | None = None,
    rows: list[dict] | None = None,
    warning: str | None = None,
    task_id: object = None,
) -> dict:
    """The answer in Arsenkin's layout: `result.type` and `result.data`, whichever source produced it."""
    data = normalise(request)
    if data["type"] == 1:
        numbers = frequencies or {}
        result = {
            "type": 1,
            "queries": data["queries"],
            "data": {
                "device": data["device"] or None,
                "queries": data["queries"],
                "regions": {str(region): str(region) for region in data["regions"]},
                "result": {query: numbers.get(query, {}) for query in data["queries"]},
            },
        }
        summary = {
            "queries": len(data["queries"]),
            "regions": len(data["regions"]),
            "frequencies": sum(len(per_region) for per_query in numbers.values() for per_region in per_query.values()),
        }
    else:
        found = rows or []
        result = {
            "type": 2,
            "queries": data["queries"],
            "is_clear": data["is_clear"],
            "device": data["device"] or None,
            "minus": data["minus_words"],
            "data": found,
        }
        summary = {
            "queries": len(data["queries"]),
            "left_total": sum(len(row.get("left") or {}) for row in found),
            "right_total": sum(len(row.get("right") or {}) for row in found),
        }
    report = {
        "ok": True,
        "source": source,
        "tools_name": TOOLS_NAME,
        **({"task_id": task_id} if task_id is not None else {}),
        "result": result,
        "summary": summary,
    }
    if warning:
        report["warning"] = warning
    return report


def report_from_remote(payload: dict, request: Mapping | None = None) -> dict:
    """Their `get` answer → our report: their `result` is already the documented shape, the summary is ours."""
    result = payload.get("result") or {}
    kind = result.get("type")
    body = _request_of(result, request)
    numbers = (result.get("data") or {}).get("result") if isinstance(result.get("data"), Mapping) else None
    if kind == 1 and isinstance(numbers, Mapping):
        report = build_report(request=body, source="arsenkin", frequencies=numbers, task_id=payload.get("task_id"))
    elif kind == 2 and isinstance(result.get("data"), list):
        report = build_report(request=body, source="arsenkin", rows=result["data"], task_id=payload.get("task_id"))
    else:  # a shape this module does not know: hand it over rather than lose it
        return {
            "ok": True,
            "source": "arsenkin",
            "tools_name": TOOLS_NAME,
            "task_id": payload.get("task_id"),
            "result": result,
            "summary": {},
        }
    # their echo wins: the device and the region list are the service's own, not what we guessed
    report["result"] = {
        **report["result"],
        **{key: result[key] for key in ("type", "queries", "device", "minus", "is_clear") if key in result},
    }
    return report


def _request_of(result: Mapping, request: Mapping | None) -> dict:
    """The request this answer belongs to: our own body, corrected by the echo the service sent back."""
    source = request or {}
    body = dict((source.get("data") if isinstance(source.get("data"), Mapping) else source) or {})
    for key in ("type", "queries", "device"):
        if key in result:
            body[key] = result[key]
    if body.get("device") is None:
        body["device"] = ""
    return body


# --- one call --------------------------------------------------------------------------------------------------


async def collect(
    request: Mapping,
    *,
    source: str = "wordstat",
    token: str | None = None,
    provider: str | None = None,
    cache: bool = False,
    max_age: str | None = None,
    poll_interval: float = 15.0,
    timeout: float = 3600.0,
) -> dict:
    """Run one `wordstat` task through `source` and return the report in the documented shape.

    Parameters:
        request: the full body or the bare `data`; see the module docstring for both types.
        source: `wordstat` (our CLI, the default) or `arsenkin` (their task API).
        provider, cache, max_age: forwarded to our CLI unchanged (source `wordstat` only).

    A region or a query the source could not answer lands in `warning`; the rest of the batch is still a report.
    """
    if isinstance(request, Mapping) and "request" in request:  # a submitted job carries the task's envelope
        source = str(request.get("source") or source)
        request = request["request"]
    if source not in SOURCES:
        raise ValueError(f"unknown source '{source}'; known: {', '.join(SOURCES)}")
    data = normalise(request)
    if source == "arsenkin":
        return await _from_arsenkin(data, token=token, poll_interval=poll_interval, timeout=timeout)
    _need_device(data)
    started = time.monotonic()
    if data["type"] == 1:
        frequencies, warning = await asyncio.to_thread(
            _frequencies, data, provider=provider, cache=cache, max_age=max_age
        )
        report = build_report(request=data, source=source, frequencies=frequencies, warning=warning)
    else:
        rows, warning = await asyncio.to_thread(_phrases, data, provider=provider, cache=cache, max_age=max_age)
        report = build_report(request=data, source=source, rows=rows, warning=warning)
    report["elapsed_s"] = round(time.monotonic() - started, 1)
    return report


async def _from_arsenkin(data: Mapping, *, token: str | None, poll_interval: float, timeout: float) -> dict:
    secret = token or binding("arsenkin")[0]
    if not secret:
        raise RuntimeError("no token for 'arsenkin': `token set arsenkin`")
    body = {key: (value if key != "device" else (value or "")) for key, value in _echo(data).items()}
    async with httpx.AsyncClient(timeout=60.0, trust_env=False) as client:
        payload = await arsenkin.collect_wordstat(client, secret, body, poll_interval=poll_interval, timeout=timeout)
    return report_from_remote(payload, data)


def _echo(data: Mapping) -> dict:
    """The documented `data` object of one type, as it goes to the service."""
    if data["type"] == 1:
        return {
            "type": 1,
            "queries": list(data["queries"]),
            "regions": list(data["regions"]),
            "ws": list(data["ws"]),
            "device": data["device"],
        }
    return {
        "type": 2,
        "queries": list(data["queries"]),
        "region": data["region"],
        "device": data["device"],
        "minus_words": list(data["minus_words"]),
        "is_clear_minus": data["is_clear_minus"],
        "is_right": data["is_right"],
        "is_clear": data["is_clear"],
    }


# --- a task that outlives the call -----------------------------------------------------------------------------

tasks.KINDS[TOOLS_NAME] = "rb_workflows.wordstat:collect"
tasks.REPORTS[TOOLS_NAME] = report_from_remote


async def submit(
    request: Mapping,
    *,
    source: str = "wordstat",
    tasks_dir: Path | None = None,
    **kwargs: object,
) -> dict:
    """Place the work and answer at once: `task_id`, the state, and the call that reads the result later."""
    if isinstance(request, Mapping) and "request" in request:
        source = str(request.get("source") or source)
        request = request["request"]
    if source not in SOURCES:
        raise ValueError(f"unknown source '{source}'; known: {', '.join(SOURCES)}")
    data = normalise(request)
    store = "arsenkin" if source == "arsenkin" else "local"
    return await tasks.submit(
        TOOLS_NAME, {"request": data, **kwargs, "store": store}, store=tasks.default_store(tasks_dir)
    )


async def poll(task_id: str, *, tasks_dir: Path | None = None) -> dict:
    """The state of a task, or its report once it is done — the id names the store that answers."""
    return await tasks.get(task_id, store=tasks.default_store(tasks_dir))


async def wait(task_id: str, *, tasks_dir: Path | None = None, poll_interval: float | None = None) -> dict:
    """Poll until the task is done or failed; for the caller that wants one blocking call."""
    step = SLEEP if poll_interval is None else poll_interval
    while True:
        answer = await poll(task_id, tasks_dir=tasks_dir)
        if answer.get("state") in {"done", "error"}:
            return answer
        await asyncio.sleep(step)
