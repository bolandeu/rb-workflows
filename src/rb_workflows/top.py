"""`top`: positions for a batch of queries in the shape Arsenkin's `check-top` API uses, from either source.

The contract is Arsenkin's, so a task written for their API works here unchanged and a report produced here can be
read by anything that reads theirs:

    request  {"tools_name": "check-top",
              "data": {"queries": [...], "se": [{"type": 1, "region": 213}], "depth": 10,
                       "is_snippet": true, "noreask": false}}
    answer   {"result": {"request": {...echo, `ss` instead of `se`...},
                         "result": {"collect": [[[url, url], [url]], ...]}}}

`collect[query][se]` is the list of URLs in the order of the results, so the two indices are the ones of the input.

Input — a JSON object (`wf top --input file.json`) or the same fields as flags (`--queries`, `--se`, `--depth`):
    required:  queries  list[str]                  what to check
               se       list[{type: int, region: int|str}]   1/2/3 Yandex, 11/12 Google, 20/21 YouTube
    optional:  depth        int   10   results per query, one of 5, 10, 20, 30, 50, 100
               is_snippet   bool  false  collect the snippets of the results
               noreask      bool  false  do not let the engine rewrite the query
The full Arsenkin body (`{"tools_name": "check-top", "data": {...}}`) is accepted as is, and the bare `data`
object too: a task written for their API runs here unchanged.

Two sources answer the same question: `serp` (our own CLI, the default) and `arsenkin` (their `check-top` task). A search
engine our CLI cannot ask (YouTube) is not a failure: its list is empty and the report says so in `skipped` — the rest of
the task is collected. On top of the contract the report carries `summary`: the strength of every URL and the top-N.
"""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from collections.abc import Iterable, Mapping
from pathlib import Path

import httpx

from . import cache as cache_store
from . import serp as serp_source
from . import tasks
from .credentials import binding
from .services import arsenkin

__all__ = ["ENGINE_TYPES", "build_report", "collect", "normalise", "summary"]

SOURCE_NAMES = ("serp", "arsenkin")
# Arsenkin's search engine codes: 1 Yandex XML, 2 Yandex Desktop, 3 Yandex Mobile, 11 Google Desktop, 12 Google Mobile,
# 20/21 YouTube. Only what our own CLI can ask is mapped; the rest is reported as skipped, not as an error.
ENGINE_TYPES: dict[int, str | None] = {
    1: "yandex",
    2: "yandex",
    3: "yandex",
    11: "google",
    12: "google",
    20: None,
    21: None,
}
SUMMARY_SIZE = 1000  # every URL by strength; `top_n` trims what the caller shows


def normalise(request: Mapping) -> dict:
    """The `data` object of a `check-top` request, from the full body or from the bare data; validated.

    Raises `ValueError` for an engine code this module does not know and for a request without queries: both are
    caller mistakes that must surface before any paid call.
    """
    raw = request.get("data")
    data: Mapping = raw if isinstance(raw, Mapping) else request
    queries = [str(query) for query in data.get("queries") or []]
    if not queries:
        raise ValueError("no queries: 'queries' must be a non-empty list")
    se = []
    for entry in data.get("se") or []:
        kind = entry.get("type")
        if kind not in ENGINE_TYPES:
            raise ValueError(f"unknown search engine type {kind}; known: {', '.join(str(key) for key in ENGINE_TYPES)}")
        se.append({"type": kind, "region": entry.get("region")})
    if not se:
        raise ValueError("no search engines: 'se' must name at least one")
    return {
        "queries": queries,
        "se": se,
        "depth": int(data.get("depth") or 10),
        "is_snippet": bool(data.get("is_snippet")),
        "noreask": bool(data.get("noreask")),
    }


def _as_int(value: object) -> int | None:
    """A region code as an int, or None when the caller put something that is not one there."""
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _echo(data: Mapping) -> dict:
    """The `request` object as Arsenkin echoes it back: `se` becomes `ss`, and their own defaults are filled in."""
    return {
        "queries": list(data["queries"]),
        "depth": data["depth"],
        "ss": [{"ss": entry["type"], "region": entry["region"]} for entry in data["se"]],
        "is_deny": False,
        "is_snippet": data["is_snippet"],
        "is_noreask": data["noreask"],
    }


def _empty(data: Mapping) -> list[list[list[str]]]:
    return [[[] for _ in data["se"]] for _ in data["queries"]]


def _truncate(collect: list[list[list[str]]], depth: int) -> list[list[list[str]]]:
    return [[[url for url in urls[:depth]] for urls in per_se] for per_se in collect]


async def _from_serp(
    data: Mapping,
    *,
    concurrency: int,
    token: str | None,
    provider: str | None = None,
    cache: bool = False,
    max_age: str | None = None,
) -> tuple[list[list[list[str]]], list[int]]:
    """Ask our own CLI once per query and place the answers by engine; returns the collect and the skipped se indices."""
    engines = [ENGINE_TYPES[entry["type"]] for entry in data["se"]]
    skipped = [index for index, engine in enumerate(engines) if engine is None]
    wanted = sorted({engine for engine in engines if engine})
    if not wanted:
        return _empty(data), skipped
    yandex_lr = _as_int(
        next((entry["region"] for entry in data["se"] if ENGINE_TYPES[entry["type"]] == "yandex"), None)
    )
    google_loc = next((entry["region"] for entry in data["se"] if ENGINE_TYPES[entry["type"]] == "google"), None)
    top = max(10, data["depth"])  # the CLI's own floor is 10; the answer is trimmed back to `depth` afterwards
    report = await serp_source.collect(
        data["queries"],
        engine="both" if len(wanted) > 1 else wanted[0],
        top=top,
        concurrency=concurrency,
        yandex_lr=yandex_lr,
        google_loc=str(google_loc) if google_loc is not None else None,
        provider=provider,
        cache=cache,
        max_age=max_age,
    )
    by_query: dict[str, dict[str, list[str]]] = {query: defaultdict(list) for query in data["queries"]}
    for entry in report.get("results") or []:
        if not entry.get("ok"):
            continue
        for row in entry.get("rows") or []:
            by_query[entry["query"]][entry["serp_engine"]].append(row.get("url"))
    collect = [[by_query[query].get(engine or "", []) for engine in engines] for query in data["queries"]]
    return collect, skipped


async def _from_arsenkin(data: Mapping, *, token: str | None, poll_interval: float, timeout: float) -> dict:
    """Their own `check-top` task: `set` → `check` → `get`, the answer already in their shape."""
    secret = token or binding("arsenkin")[0]
    if not secret:
        raise RuntimeError("no token for 'arsenkin': `token set arsenkin`")
    async with httpx.AsyncClient(timeout=60.0, trust_env=False) as client:
        return await arsenkin.collect_top(client, secret, dict(data), poll_interval=poll_interval, timeout=timeout)


async def collect(
    request: Mapping,
    *,
    source: str = "serp",
    token: str | None = None,
    concurrency: int = 3,
    poll_interval: float = 15.0,
    timeout: float = 3600.0,
    top_n: int = 10,
    provider: str | None = None,
    cache: bool = False,
    max_age: str | None = None,
) -> dict:
    """Run one `check-top` task through `source` and return the report in Arsenkin's shape plus the summary.

    Parameters:
        request: the full body or the bare `data`; see the module docstring for the accepted keys.
        source: `serp` (our CLI, the default) or `arsenkin` (their task API).
        top_n: how many rows of the summary to show; the full ranking stays in `summary.rows` order.

    A query or an engine the source cannot answer contributes an empty list, never an exception: the report says
    `ok: false` only when the whole call failed, and `skipped` names the engine indices nobody was asked about.
    """
    # a submitted job carries the task's own envelope: `{"request": <check-top body>, "source": "serp"}`
    if isinstance(request, Mapping) and "request" in request:
        source = str(request.get("source") or source)
        request = request["request"]
    if source not in SOURCE_NAMES:
        raise ValueError(f"unknown source '{source}'; known: {', '.join(SOURCE_NAMES)}")
    data = normalise(request)
    started = time.monotonic()
    skipped: list[int] = []
    task_id = None
    cached = False
    if source == "arsenkin":
        answer = cache_store.load("top", data, max_age=max_age) if cache else None
        cached = answer is not None
        if answer is None:
            answer = await _from_arsenkin(data, token=token, poll_interval=poll_interval, timeout=timeout)
            cache_store.save("top", data, answer)
        result = answer.get("result") or {}
        task_id = answer.get("task_id")
        collect_rows = _truncate(result.get("result", {}).get("collect") or _empty(data), data["depth"])
    else:
        collect_rows, skipped = await _from_serp(
            data, concurrency=concurrency, token=token, provider=provider, cache=cache, max_age=max_age
        )
        collect_rows = _truncate(collect_rows, data["depth"])
    report = build_report(request=data, source=source, collect=collect_rows, top_n=top_n, task_id=task_id)
    report["skipped"] = skipped
    if cached:
        report["cached"] = True
    report["elapsed_s"] = round(time.monotonic() - started, 1)
    return report


def report_from_remote(payload: dict) -> dict:
    """Arsenkin's `get` answer → our report: the check-top body is echoed, the collect is summarised."""
    request = (payload.get("result") or {}).get("request") or {}
    se = [{"type": entry.get("ss"), "region": entry.get("region")} for entry in request.get("ss") or []]
    body = {
        "queries": request.get("queries") or [],
        "se": se,
        "depth": request.get("depth") or 10,
        "is_snippet": request.get("is_snippet"),
        "noreask": request.get("is_noreask"),
    }
    collect = ((payload.get("result") or {}).get("result") or {}).get("collect") or []
    return build_report(request=body, source="arsenkin", collect=collect, task_id=payload.get("task_id"))


tasks.REPORTS["top"] = report_from_remote


def build_report(
    *,
    request: Mapping,
    source: str,
    collect: list[list[list[str]]],
    top_n: int = 10,
    task_id: object = None,
) -> dict:
    """The report in Arsenkin's layout: `result.request` echo, `result.result.collect`, and our `summary`."""
    data = normalise(request)
    return {
        "ok": True,
        "source": source,
        **({"task_id": task_id} if task_id is not None else {}),
        "result": {"request": _echo(data), "result": {"collect": collect}},
        "summary": summary(collect, queries=data["queries"], se=data["se"], depth=data["depth"], top_n=top_n),
    }


def summary(
    collect: list[list[list[str]]],
    *,
    queries: Iterable[str],
    se: list[dict],
    depth: int,
    top_n: int = 10,
) -> dict:
    """Every URL ranked by strength: places over the whole task, normalised by queries x search engines.

    A hit is worth `(depth - position + 1) / depth`, so the first place of every query in every engine gives 1.0 and a
    URL that only reaches the bottom of one list stays low. `queries_count` is the number of queries the URL appears in,
    `appearances` the number of hits, `best_position` the highest place. Rows are ordered by strength, then breadth,
    then the best place; `top` keeps `top_n` of them, `rows_total` says how many there are.
    """
    queries = list(queries)
    denominator = max(1, len(queries) * len(se))
    hits: dict[str, dict] = defaultdict(lambda: {"by_query": defaultdict(dict), "positions": []})
    for index, per_se in enumerate(collect):
        for se_index, urls in enumerate(per_se):
            for position, url in enumerate(urls, start=1):
                found = hits[url]
                found["by_query"][index][se_index] = position
                found["positions"].append(position)

    rows = []
    for url, found in hits.items():
        positions = found["positions"]
        weighted = sum((depth - position + 1) / depth for position in positions if position <= depth)
        rows.append(
            {
                "url": url,
                "queries_count": len(found["by_query"]),
                "appearances": len(positions),
                "best_position": min(positions),
                "strength": round(weighted / denominator, 4),
                "by_query": [
                    {
                        "query": queries[index],
                        "positions": {str(se[se_index]["type"]): position for se_index, position in per_se.items()},
                    }
                    for index, per_se in sorted(found["by_query"].items())
                ],
            }
        )

    def rank(row: dict) -> tuple[float, int, int]:
        return (-float(row["strength"]), -int(row["queries_count"]), int(row["best_position"]))

    rows.sort(key=rank)
    return {"rows_total": len(rows), "top_n": top_n, "top": rows[:top_n]}


# --- a task that outlives the call -------------------------------------------------------------------------------
# Both sources can be slow, so the caller may place the work and come back: `submit` answers with an id at once,
# `poll` answers with a state or the report. The source of truth about a running task is its own: our worker process
# for `serp`, the Arsenkin task for `arsenkin` — the local file only remembers which one to ask.

SLEEP = 2.0  # seconds between polls inside `wait`


async def set_remote(data: Mapping, *, token: str | None = None) -> int:
    """Place the task with Arsenkin and return their id."""
    secret = token or binding("arsenkin")[0]
    if not secret:
        raise RuntimeError("no token for 'arsenkin': `token set arsenkin`")
    async with httpx.AsyncClient(timeout=60.0, trust_env=False) as client:
        return await arsenkin.set_task(client, secret, dict(data))


async def remote_state(remote_id: int, *, token: str | None = None) -> dict:
    """Arsenkin's own view of the task (`status`, `progress`)."""
    secret = token or binding("arsenkin")[0]
    if not secret:
        raise RuntimeError("no token for 'arsenkin': `token set arsenkin`")
    async with httpx.AsyncClient(timeout=60.0, trust_env=False) as client:
        return await arsenkin.task_state(client, secret, remote_id)


async def remote_result(remote_id: int, *, token: str | None = None) -> dict:
    secret = token or binding("arsenkin")[0]
    if not secret:
        raise RuntimeError("no token for 'arsenkin': `token set arsenkin`")
    async with httpx.AsyncClient(timeout=60.0, trust_env=False) as client:
        return await arsenkin.task_result(client, secret, remote_id)


async def submit(
    request: Mapping,
    *,
    source: str = "serp",
    tasks_dir: Path | None = None,
    token: str | None = None,
    **kwargs: object,
) -> dict:
    """Place the work and answer at once: `task_id`, the state, and the call that reads the result later.

    `serp` is started as a detached worker of this CLI (it outlives the call), `arsenkin` only records their task id —
    the service is already doing the work. Either way the caller gets the same three things.
    """
    # a submitted job carries the task's own envelope: `{"request": <check-top body>, "source": "serp"}`
    if isinstance(request, Mapping) and "request" in request:
        source = str(request.get("source") or source)
        request = request["request"]
    if source not in SOURCE_NAMES:
        raise ValueError(f"unknown source '{source}'; known: {', '.join(SOURCE_NAMES)}")
    data = normalise(request)
    # `source` decides the store: `arsenkin` places their task, `serp` runs a worker of this CLI
    return await tasks.submit("top", {"request": data, **kwargs, "store": source}, store=tasks.default_store(tasks_dir))


async def poll(task_id: str, *, tasks_dir: Path | None = None, token: str | None = None) -> dict:
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
