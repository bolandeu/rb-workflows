"""`serp`: a batch of queries through the `serp` CLI, with the cluster-strength summary.

The `serp` CLI answers one query per call, so a batch is a fan-out: up to `concurrency` calls at once, each one with
exactly the parameters the caller gave — the wrapper adds nothing and hides nothing. A failed query is an entry with
`ok: false`, never an exception out of the batch.

The summary answers the question a cluster is collected for: which URLs hold the cluster. A URL's `strength` is its
places in the results, normalised so that 1.0 means "first in every engine for every query".
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import time
from collections import defaultdict
from collections.abc import Iterable, Sequence

__all__ = ["ENGINES", "collect", "summary"]

ENGINES = {"yandex": ("yandex",), "google": ("google",), "both": ("yandex", "google")}
SUMMARY_SIZE = 10
TIMEOUT = 600.0  # one `serp search` call, seconds: a provider that hangs must not hold the batch


def _argv(
    query: str,
    *,
    engine: str,
    top: int,
    provider: str | None,
    cache: bool,
    max_age: str | None,
    yandex_lr: int | None,
    google_loc: str | None,
) -> list[str]:
    argv = ["serp", "search", query, "--engine", engine, "--top", str(top)]
    if provider:
        argv += ["--provider", provider]
    if cache:
        argv.append("--cache")
    if max_age:
        argv += ["--max-age", max_age]
    if yandex_lr is not None:
        argv += ["--yandex-lr", str(yandex_lr)]
    if google_loc is not None:
        argv += ["--google-loc", google_loc]
    argv.append("--json")
    return argv


def _call(argv: Sequence[str]) -> tuple[int, str]:
    """One `serp search` run; (exit code, stdout). A missing binary or a timeout is a failed entry, not a crash."""
    try:
        done = subprocess.run(  # noqa: S603 - the command is built here, not taken from input
            list(argv), capture_output=True, text=True, timeout=TIMEOUT, check=False
        )
    except FileNotFoundError as exc:
        raise RuntimeError(f"the serp CLI is not installed or not on PATH: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"serp did not answer in {TIMEOUT:g} s") from exc
    return done.returncode, done.stdout


def _rows(envelope: dict) -> list[dict]:
    return [
        {
            "position": row.get("serp_position"),
            "url": row.get("url"),
            "domain": row.get("domain"),
            "title": row.get("title"),
            "engine": row.get("searchengine"),
        }
        for row in envelope.get("results") or []
    ]


def _entries(query: str, envelope: dict, engines: Sequence[str]) -> list[dict]:
    """One entry per requested engine: the rows of that engine, even when it returned nothing."""
    rows = _rows(envelope)
    return [
        {
            "query": query,
            "serp_engine": engine,
            "ok": True,
            "rows": [row for row in rows if (row["engine"] or _engine_of(envelope)) == engine],
            "error": None,
        }
        for engine in engines
    ]


def _engine_of(envelope: dict) -> str | None:
    """The engine of a single-engine answer, when the rows themselves do not carry it."""
    for source in envelope.get("sources") or []:
        if isinstance(source.get("engine"), str):
            return source["engine"]
    return None


async def collect(
    queries: Iterable[str],
    *,
    engine: str = "both",
    top: int = 20,
    concurrency: int = 3,
    provider: str | None = None,
    cache: bool = False,
    max_age: str | None = None,
    yandex_lr: int | None = None,
    google_loc: str | None = None,
) -> dict:
    """Ask `serp search` for every query, up to `concurrency` at once, and roll the answers up.

    Parameters:
        queries: any number of queries; each one is its own `serp search` call.
        engine: `yandex`, `google` or `both` — passed to the CLI as is and used as the set the summary normalises by.
        top: how deep each answer goes (10..100, the CLI's own range).
        concurrency: calls in flight at once; the CLI itself works one query at a time, so this is the only parallelism.
        provider, cache, max_age, yandex_lr, google_loc: forwarded to `serp search` unchanged.

    Returns `{"ok", "engine", "top", "queries", "elapsed_s", "results", "sources", "billable_requests_total",
    "cost_total", "cost_currency", "summary"}`: `results` holds one entry per (query x engine) in the order of the
    queries, a failure being `{"ok": false, "rows": [], "error": "..."}`; `summary` is the cluster roll-up (see
    `summary`). `ok` is true only when every entry succeeded.
    """
    engines = ENGINES.get(engine)
    if engines is None:
        raise ValueError(f"unknown engine '{engine}'; known: {', '.join(ENGINES)}")
    wanted = list(queries)
    gate = asyncio.Semaphore(max(1, concurrency))
    started = time.monotonic()
    sources: list[dict] = []
    billable = 0
    cost = 0.0
    currency: str | None = None

    async def one(query: str) -> list[dict]:
        nonlocal billable, cost, currency
        argv = _argv(
            query,
            engine=engine,
            top=top,
            provider=provider,
            cache=cache,
            max_age=max_age,
            yandex_lr=yandex_lr,
            google_loc=google_loc,
        )
        async with gate:
            try:
                code, out = await asyncio.to_thread(_call, argv)
            except RuntimeError as exc:  # a missing CLI or a hung one: this query failed, the batch goes on
                return [
                    {"query": query, "serp_engine": engine_name, "ok": False, "rows": [], "error": str(exc)}
                    for engine_name in engines
                ]
        try:
            envelope = json.loads(out)
        except ValueError:
            return [
                {"query": query, "serp_engine": engine_name, "ok": False, "rows": [],
                 "error": f"serp answered not JSON (exit {code}): {out.strip()[:200]}"}
                for engine_name in engines
            ]  # fmt: skip
        if envelope.get("ok") is False:
            error = envelope.get("error") or {}
            message = f"{error.get('code', 'ERROR')}: {error.get('message', 'no message')}"
            return [
                {"query": query, "serp_engine": engine_name, "ok": False, "rows": [], "error": message}
                for engine_name in engines
            ]
        sources.extend(envelope.get("sources") or [])
        billable += int(envelope.get("billable_requests_total") or 0)
        cost += float(envelope.get("cost_total") or 0.0)
        currency = envelope.get("cost_currency") or currency
        return _entries(query, envelope, engines)

    results = [entry for entries in await asyncio.gather(*(one(query) for query in wanted)) for entry in entries]
    return {
        "ok": all(entry["ok"] for entry in results),
        "engine": engine,
        "top": top,
        "queries": wanted,
        "elapsed_s": round(time.monotonic() - started, 1),
        "results": results,
        "sources": sources,
        "billable_requests_total": billable,
        "cost_total": round(cost, 4),
        "cost_currency": currency,
        "summary": summary(results, queries=wanted, engines=engines, top=top),
    }


def summary(results: list[dict], *, queries: Sequence[str], engines: Sequence[str], top: int) -> dict:
    """Which URLs hold the cluster: places per query and engine, and one `strength` for the whole set.

    `strength` weights a hit by its place — `(top - position + 1) / top` — and divides the sum by the number of
    query x engine pairs asked for, so 1.0 is "first everywhere" and a URL that ranks nowhere in part of the cluster
    is weaker than a URL that ranks in all of it. A failed entry contributes nothing.

    Rows: `url`, `domain`, `queries_count` (queries it appears in), `appearances` (hits over query x engine),
    `best_position`, `strength`, `by_query` (one dict per query: the query and its position per engine, `null` when
    absent). Ordered by `strength`, then `queries_count`, then `best_position`; `SUMMARY_SIZE` rows are kept.
    """
    denominator = max(1, len(queries) * len(engines))
    hits: dict[str, dict] = defaultdict(lambda: {"domain": None, "by_query": defaultdict(dict), "positions": []})
    for entry in results:
        if not entry.get("ok"):
            continue
        for row in entry.get("rows") or []:
            url = row.get("url")
            if not url:
                continue
            found = hits[url]
            found["domain"] = found["domain"] or row.get("domain")
            found["by_query"][entry["query"]][entry["serp_engine"]] = row.get("position")
            if isinstance(row.get("position"), int):
                found["positions"].append(row["position"])

    rows = []
    for url, found in hits.items():
        positions = found["positions"]
        weighted = sum((top - position + 1) / top for position in positions if position <= top)
        by_query = [
            {"query": query, **{engine: found["by_query"].get(query, {}).get(engine) for engine in engines}}
            for query in queries
            if query in found["by_query"]
        ]
        rows.append(
            {
                "url": url,
                "domain": found["domain"],
                "queries_count": len(found["by_query"]),
                "appearances": len(positions),
                "best_position": min(positions) if positions else None,
                "strength": round(weighted / denominator, 4),
                "by_query": by_query,
            }
        )

    def rank(row: dict) -> tuple[float, int, int]:
        return (-float(row["strength"]), -int(row["queries_count"]), int(row["best_position"] or 10**6))

    rows.sort(key=rank)
    return {
        "engines": len(engines),
        "queries": len(queries),
        "urls_total": len(rows),
        "top": rows[:SUMMARY_SIZE],
    }
