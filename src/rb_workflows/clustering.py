"""`clustering`: keywords grouped by the URLs they share, in the shape Arsenkin's `clustering` API uses.

The contract is Arsenkin's, so a task written for their API works here unchanged:

    request  {"tools_name": "clustering",
              "data": {"queries": [...], "se": 1, "region": 213, "group": "soft", "count": 3,
                       "depth": 10, "stoplist": ["auto.ru"], "ws": ["base", "quoted"], "main": false}}
    answer   {"result": {"request": {...echo...}, "result": [<cluster>, ...]}}

Two sources answer the same question:

- `serp` (default) — our own stack: `serp search` for the tops of every query, then grouping by URL overlap, then
  `wordstat search` for the frequencies named in `ws`. Costs one `serp` call per query and one Wordstat call per
  (query x frequency type).
- `arsenkin` — their own task, `set` → `check` → `get`, the answer already in their shape.

Every field of the documented request is accepted as is, from the full body, from the bare `data` object, or from
the CLI's flags (`wf clustering --queries … --se 1 --region 213`).
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import time
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from . import serp as serp_source
from . import tasks
from .credentials import binding
from .services import arsenkin

__all__ = ["build_report", "clusters", "collect", "normalise", "report_from_remote"]

TOOLS_NAME = "clustering"
SOURCES = ("serp", "arsenkin")
GROUPS = ("hard", "soft")
DEPTHS = (10, 20, 30)
ENGINES = {1: "yandex", 2: "google"}
# Arsenkin's frequency names → the ones the `wordstat` CLI knows. `overal` is their other name for the broad number.
WS_TYPES = ("base", "quoted", "overal", "exact")
WS_TO_WORDSTAT = {"base": "broad", "overal": "broad", "quoted": "phrase", "exact": "exact"}
WORDSTAT_TIMEOUT = 600.0
SLEEP = 2.0  # seconds between polls inside `wait`

# --- the request -------------------------------------------------------------------------------------------------


def normalise(request: Mapping) -> dict:
    """The `data` object of a `clustering` request, from the full body or from the bare data; validated.

    Raises `ValueError` for anything the documented request forbids: no queries, a search engine other than 1 or 2,
    another group method, a `count` outside 2..10, a `depth` other than 10/20/30, an unknown frequency name. These are
    caller mistakes, so they surface before a paid call.
    """
    raw = request.get("data")
    data: Mapping = raw if isinstance(raw, Mapping) else request
    queries = [str(query).strip() for query in data.get("queries") or [] if str(query).strip()]
    if not queries:
        raise ValueError("no queries: 'queries' must be a non-empty list")
    se = _as_int(data.get("se"))
    if se not in ENGINES:
        raise ValueError(f"unknown search engine '{data.get('se')}': 1 is Yandex, 2 is Google")
    if data.get("region") in (None, ""):
        raise ValueError("no region: 'region' is a Yandex `lr` code (213) or a Google location id (1011969)")
    group = str(data.get("group") or "soft").lower()
    if group not in GROUPS:
        raise ValueError(f"unknown group '{group}'; known: {', '.join(GROUPS)}")
    count = _as_int(data.get("count")) or 3
    if not 2 <= count <= 10:
        raise ValueError(f"count must be 2..10, got {count}")
    depth = _as_int(data.get("depth")) or 10
    if depth not in DEPTHS:
        raise ValueError(f"depth must be one of {', '.join(str(value) for value in DEPTHS)}")
    ws = [str(kind).lower() for kind in data.get("ws") or []]
    unknown = [kind for kind in ws if kind not in WS_TYPES]
    if unknown:
        raise ValueError(f"unknown frequency '{unknown[0]}'; known: {', '.join(WS_TYPES)}")
    return {
        "queries": queries,
        "se": se,
        "region": _as_int(data.get("region")) or str(data.get("region")),
        "group": group,
        "count": count,
        "depth": depth,
        "stoplist": [str(entry).strip() for entry in data.get("stoplist") or [] if str(entry).strip()],
        "ws": ws,
        "main": bool(data.get("main")),
    }


def _as_int(value: object) -> int | None:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _echo(data: Mapping) -> dict:
    """The documented `data` object as it goes back to the caller: the same keys, the same order as the docs."""
    return {
        "queries": list(data["queries"]),
        "se": data["se"],
        "region": data["region"],
        "group": data["group"],
        "count": data["count"],
        "depth": data["depth"],
        "stoplist": list(data["stoplist"]),
        "ws": list(data["ws"]),
        "main": data["main"],
    }


# --- grouping ---------------------------------------------------------------------------------------------------


class _Union:
    """Union-find over the phrases: two phrases in one set are one cluster."""

    def __init__(self, items: Iterable[str]) -> None:
        self.parent = {item: item for item in items}

    def find(self, item: str) -> str:
        while self.parent[item] != item:
            self.parent[item] = self.parent[self.parent[item]]
            item = self.parent[item]
        return item

    def merge(self, left: str, right: str) -> None:
        self.parent[self.find(left)] = self.find(right)


def clusters(tops: Mapping[str, list[str]], *, group: str, count: int) -> list[list[str]]:
    """Group the phrases by the URLs their tops share; returns the clusters in the order of the first phrase.

    `hard` merges two phrases only when they share at least `count` URLs, `soft` merges on a single shared URL. The
    phrases keep the order they were asked in, and a phrase sharing nothing stays a cluster of its own.

    ponytail: Arsenkin's own meaning of `count` is theirs; ours is the overlap threshold, which is the same knob with
    a plain definition. Replace it here if their numbers must match theirs exactly.
    """
    phrases = list(tops)
    union = _Union(phrases)
    urls = {phrase: set(tops[phrase]) for phrase in phrases}
    for index, left in enumerate(phrases):
        for right in phrases[index + 1 :]:
            shared = len(urls[left] & urls[right])
            if shared >= count if group == "hard" else shared >= 1:
                union.merge(left, right)
    grouped: dict[str, list[str]] = defaultdict(list)
    for phrase in phrases:
        grouped[union.find(phrase)].append(phrase)
    return list(grouped.values())


# --- our own source: serp for the tops, wordstat for the frequencies ---------------------------------------------


def _host(url: str) -> str:
    """The bare host of a URL or of a stoplist entry (`http://www.auto.ru/x` → `auto.ru`)."""
    candidate = url if "//" in url else f"//{url}"
    host = (urlsplit(candidate).hostname or "").lower().rstrip(".")
    return host[4:] if host.startswith("www.") else host


def _is_main(url: str) -> bool:
    """A main page: the path is empty or `/`, whatever the query string."""
    return urlsplit(url).path in {"", "/"}


def _keep(urls: Iterable[str], data: Mapping) -> list[str]:
    """The top of one query after the stoplist and, when `main` is set, without the main pages."""
    stopped = {_host(entry) for entry in data["stoplist"]}
    kept = []
    for url in urls:
        host = _host(url)
        if any(host == entry or host.endswith(f".{entry}") for entry in stopped):
            continue
        if data["main"] and _is_main(url):
            continue
        kept.append(url)
    return kept


async def _from_serp(
    data: Mapping,
    *,
    concurrency: int,
    provider: str | None,
    cache: bool,
    max_age: str | None,
) -> tuple[dict[str, list[str]], dict[str, list[str]], list[str]]:
    """Our own `serp` CLI for every query: (kept tops, raw tops, the queries that failed)."""
    report = await serp_source.collect(
        data["queries"],
        engine=ENGINES[data["se"]],
        top=data["depth"],
        concurrency=concurrency,
        provider=provider,
        cache=cache,
        max_age=max_age,
        yandex_lr=data["region"] if data["se"] == 1 and isinstance(data["region"], int) else None,
        google_loc=str(data["region"]) if data["se"] == 2 else None,
    )
    tops: dict[str, list[str]] = {}
    raw: dict[str, list[str]] = {}
    failed: list[str] = []
    for entry in report.get("results") or []:
        if not entry.get("ok"):
            failed.append(entry["query"])
            continue
        urls = [row["url"] for row in entry.get("rows") or [] if row.get("url")]
        raw[entry["query"]] = urls
        tops[entry["query"]] = _keep(urls, data)
    return tops, raw, failed


def _frequencies(
    queries: Iterable[str],
    kinds: Iterable[str],
    region: object,
    *,
    cache: bool = True,
) -> tuple[dict[str, dict[str, int]], str | None]:
    """Wordstat frequencies for every query, one call: `{query: {wordstat type: value}}` and a warning or None.

    One `wordstat search` run for the whole batch: the CLI bills per (query x frequency type), not per call. A failed
    call is a warning, never an exception — the clusters are still worth having without the numbers.
    """
    wanted = sorted({WS_TO_WORDSTAT[kind] for kind in kinds})
    if not wanted or not list(queries):
        return {}, None
    argv = [
        "wordstat",
        "search",
        *queries,
        "--freq",
        ",".join(wanted),
        "--lr",
        str(region if isinstance(region, int) else 213),
    ]
    if cache:
        argv.append("--cache")
    argv.append("--json")
    try:
        done = subprocess.run(  # noqa: S603 - the command is built here, not taken from input
            argv, capture_output=True, text=True, timeout=WORDSTAT_TIMEOUT, check=False
        )
        payload = json.loads(done.stdout)
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        return {}, f"wordstat: {type(exc).__name__}: {exc}"[:300]
    if payload.get("ok") is False:
        error = payload.get("error") or {}
        return {}, f"wordstat: {error.get('code', 'ERROR')}: {error.get('message', 'no message')}"[:300]
    found: dict[str, dict[str, int]] = defaultdict(dict)
    for item in payload.get("results") or []:
        value = item.get("total_value")
        if isinstance(value, int):
            found[str(item.get("query"))][str(item.get("freq_type"))] = value
    return dict(found), None


def _words(frequencies: Mapping[str, Mapping[str, int]], query: str, kinds: Iterable[str]) -> dict:
    """One phrase as the report shows it: the broad number as `ws`, the strict one as `ws_strict`, all asked types."""
    found = frequencies.get(query) or {}
    return {
        "ws": found.get("broad"),
        "ws_strict": found.get("exact"),
        "ws_types": {kind: found.get(WS_TO_WORDSTAT[kind]) for kind in kinds},
    }


# --- the report -------------------------------------------------------------------------------------------------


def build_report(
    *,
    request: Mapping,
    source: str,
    tops: Mapping[str, list[str]],
    raw: Mapping[str, list[str]] | None = None,
    frequencies: Mapping[str, Mapping[str, int]] | None = None,
    skipped: Iterable[str] = (),
    warning: str | None = None,
    task_id: object = None,
) -> dict:
    """The clusters in Arsenkin's field names, plus our `summary`.

    Each cluster: `clustered` (its name — the strongest phrase), `words` (its phrases), `wssumm` (their frequencies
    added up), `topurl` (the URL that leads the cluster), `main` (main pages found in its tops), `agregators` and `geo`
    (both `null`: they need Arsenkin's own lists, which our source does not have) and `queries` (per phrase: `ws`,
    `ws_strict`, `ws_types`).
    """
    data = normalise(request)
    numbers = frequencies or {}
    found = clusters(tops, group=data["group"], count=data["count"])
    order = {query: index for index, query in enumerate(data["queries"])}
    rows = [
        _row(sorted(phrases, key=lambda phrase: order.get(phrase, len(order))), tops, raw or tops, numbers, data)
        for phrases in found
    ]
    rows.sort(key=lambda row: (-(row["wssumm"] or 0), -len(row["words"]), order.get(row["words"][0], 0)))
    report = {
        "ok": True,
        "source": source,
        "tools_name": TOOLS_NAME,
        **({"task_id": task_id} if task_id is not None else {}),
        "result": {"request": _echo(data), "result": rows},
        "summary": {
            "clusters_total": len(rows),
            "queries_total": len(data["queries"]),
            "skipped": list(skipped),
            "clusters": [
                {
                    "clustered": row["clustered"],
                    "words_count": len(row["words"]),
                    "wssumm": row["wssumm"],
                    "topurl": row["topurl"],
                }
                for row in rows
            ],
        },
    }
    if warning:
        report["warning"] = warning
    return report


def _row(
    words: list[str],
    tops: Mapping[str, list[str]],
    raw: Mapping[str, list[str]],
    frequencies: Mapping[str, Mapping[str, int]],
    data: Mapping,
) -> dict:
    """One cluster: its phrases, their frequencies, the URL that leads it, and how many main pages its tops hold."""
    urls = [url for phrase in words for url in tops.get(phrase) or []]
    counts = Counter(urls)
    topurl = None
    if counts:
        first = {
            url: min(
                position for phrase in words for position, found in enumerate(tops.get(phrase) or []) if found == url
            )
            for url in counts
        }
        topurl = min(counts, key=lambda url: (-counts[url], first[url]))
    wssumm = None
    if frequencies:
        values = [((frequencies.get(phrase) or {}).get("broad") or 0) for phrase in words]
        wssumm = sum(values) if any(values) else None
    name = max(
        words,
        key=lambda phrase: ((frequencies.get(phrase) or {}).get("broad") or 0, -words.index(phrase)),
    )
    return {
        "clustered": name,
        "words": words,
        "wssumm": wssumm,
        "topurl": topurl,
        "main": sum(1 for phrase in words for url in raw.get(phrase) or [] if _is_main(url)),
        "agregators": None,
        "geo": None,
        "queries": [{"query": phrase, **_words(frequencies, phrase, data["ws"])} for phrase in words],
    }


def report_from_remote(payload: dict) -> dict:
    """Arsenkin's `get` answer → our report: their clusters under `result.result`, our summary around them."""
    body = (payload.get("result") or {}).get("request") or {}
    raw = (payload.get("result") or {}).get("result") or []
    rows = raw if isinstance(raw, list) else (raw.get("clusters") or raw.get("result") or [])
    data = {
        key: body[key]
        for key in ("queries", "se", "region", "group", "count", "depth", "stoplist", "ws", "main")
        if key in body
    }
    overview = [
        {
            "clustered": row.get("clustered"),
            "words_count": len(row.get("words") or []),
            "wssumm": row.get("wssumm"),
            "topurl": row.get("topurl"),
        }
        for row in rows
        if isinstance(row, dict)
    ]
    return {
        "ok": True,
        "source": "arsenkin",
        "tools_name": TOOLS_NAME,
        "task_id": payload.get("task_id"),
        "result": {"request": body, "result": rows},
        "summary": {
            "clusters_total": len(rows),
            "queries_total": len(data.get("queries") or []),
            "skipped": [],
            "clusters": overview,
        },
    }


# --- one call --------------------------------------------------------------------------------------------------


async def collect(
    request: Mapping,
    *,
    source: str = "serp",
    token: str | None = None,
    concurrency: int = 3,
    provider: str | None = None,
    cache: bool = False,
    max_age: str | None = None,
    poll_interval: float = 15.0,
    timeout: float = 3600.0,
) -> dict:
    """Run one `clustering` task through `source` and return the report.

    Parameters:
        request: the full body or the bare `data`; see the module docstring for the accepted keys.
        source: `serp` (our CLI plus Wordstat, the default) or `arsenkin` (their task API).
        provider, cache, max_age: forwarded to our `serp` CLI unchanged (source `serp` only).
        poll_interval, timeout: Arsenkin's queue is polled this often, for at most this long (source `arsenkin`).

    A query our source cannot answer lands in `summary.skipped` and stays out of the clusters: one refused engine call
    must not lose the rest of the batch.
    """
    if isinstance(request, Mapping) and "request" in request:  # a submitted job carries the task's envelope
        source = str(request.get("source") or source)
        request = request["request"]
    if source not in SOURCES:
        raise ValueError(f"unknown source '{source}'; known: {', '.join(SOURCES)}")
    data = normalise(request)
    started = time.monotonic()
    if source == "arsenkin":
        return await _from_arsenkin(data, token=token, poll_interval=poll_interval, timeout=timeout)
    tops, raw, skipped = await _from_serp(
        data, concurrency=concurrency, provider=provider, cache=cache, max_age=max_age
    )
    frequencies: dict[str, dict[str, int]] = {}
    warning = None
    if data["ws"] and tops:
        frequencies, warning = await asyncio.to_thread(_frequencies, list(tops), data["ws"], data["region"])
    report = build_report(
        request=data, source=source, tops=tops, raw=raw, frequencies=frequencies, skipped=skipped, warning=warning
    )
    report["elapsed_s"] = round(time.monotonic() - started, 1)
    return report


async def _from_arsenkin(data: Mapping, *, token: str | None, poll_interval: float, timeout: float) -> dict:
    """Their own `clustering` task: `set` → `check` → `get`, the answer read by `report_from_remote`."""
    secret = token or binding("arsenkin")[0]
    if not secret:
        raise RuntimeError("no token for 'arsenkin': `token set arsenkin`")
    async with httpx.AsyncClient(timeout=60.0, trust_env=False) as client:
        payload = await arsenkin.collect_clustering(
            client, secret, _echo(data), poll_interval=poll_interval, timeout=timeout
        )
    return report_from_remote(payload)


# --- a task that outlives the call -----------------------------------------------------------------------------

tasks.KINDS[TOOLS_NAME] = "rb_workflows.clustering:collect"
tasks.REPORTS[TOOLS_NAME] = report_from_remote


async def submit(
    request: Mapping,
    *,
    source: str = "serp",
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
