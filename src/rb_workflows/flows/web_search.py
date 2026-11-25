"""`web_search`: information on a topic from several search sources — in cascade or in parallel.

`web_fetch` is about getting one page whatever it takes; this one is about coverage. `serp` gives the real SERP
(positions, snippets, highlights) through our own CLI and its XMLRiver/XMLStock accounts; `exa` and `tavily` give
their own pick plus a written answer.

`mode=cascade` stops at the first source that answers (the cheapest first, so `tried` explains why the next one was
asked). `mode=parallel` asks every chosen source at once and merges the lists: one row per URL, naming the sources it
came from and its best position. A source that is down or has no key never fails the run — it is a reason in the
report.

`fetch_top` adds the main text of the first N pages: HTML through `spider` then a plain GET, the text through
trafilatura (the page's own content, no menu, no footer, no ads). An empty extraction falls back to `spider`'s markdown.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from collections.abc import Awaitable, Callable

import httpx
import trafilatura
from wmill import task, workflow

from .. import adapters
from ..credentials import binding
from ..document import document

__all__ = ["SOURCES", "web_search"]

Search = Callable[..., Awaitable[dict]]

SOURCES_ORDER = ("serp", "exa", "tavily")
MODES = ("cascade", "parallel")
TIMEOUT = 30.0
SERP_TIMEOUT = 600.0  # the CLI tries its providers one by one; a hanging one must not hold the batch
UA = "rb-workflows/0.1 (+https://github.com/bolandeu)"
BLOCKED = frozenset({401, 403, 429})
SNIPPET_CHARS = 1000
SERP_MIN_TOP = 10  # the CLI's own floor for one page
MAX_TEXT = 200_000


def _failed(source: str, reason: str, *, cost_usd: float | None = None, cost_currency: str | None = None) -> dict:
    """A source that could not answer: the cascade moves on, the report keeps the reason."""
    return {
        "source": source,
        "ok": False,
        "results": [],
        "answer": None,
        "reason": reason,
        "cost_usd": cost_usd,
        "cost_currency": cost_currency,
    }


def _row(url: str, title: str | None, snippet: str | None, position: int | None, engine: str | None) -> dict:
    return {
        "position": position,
        "url": url,
        "domain": _domain(url),
        "title": title,
        "snippet": snippet,
        "engine": engine,
    }


def _domain(url: str) -> str | None:
    without_scheme = url.split("://", 1)[-1]
    host = without_scheme.split("/", 1)[0]
    return host or None


@task
async def serp(query: str, *, top: int, engine: str = "yandex", proxy: str | None = None) -> dict:
    """`serp snippets --json`: the real SERP with snippets, billed by the CLI in rubles out of its own accounts.

    The keys of XMLRiver/XMLStock are the CLI's business (it works standalone too), so this source has no key of ours
    and `cost_usd` stays empty: `cost` is what the CLI reports, in `cost_currency`.
    """
    argv = ["serp", "snippets", query, "--engine", engine, "--top", str(max(SERP_MIN_TOP, top)), "--json"]
    try:
        done = await asyncio.to_thread(
            subprocess.run,
            argv,
            capture_output=True,
            text=True,
            timeout=SERP_TIMEOUT,
            check=False,
        )
    except FileNotFoundError:
        return _failed("serp", "not_installed")
    except subprocess.TimeoutExpired:
        return _failed("serp", "timeout")
    try:
        envelope = json.loads(done.stdout)
    except ValueError:
        return _failed("serp", f"not_json:{done.returncode}")
    cost = envelope.get("cost_total")
    currency = next((source.get("cost_currency") for source in envelope.get("sources") or [] if source), None)
    if done.returncode != 0 or not envelope.get("ok"):
        reason = (envelope.get("error") or {}).get("code") or f"exit:{done.returncode}"
        return _failed("serp", reason, cost_currency=currency)
    rows = [
        _row(
            row["url"],
            row.get("title"),
            row.get("snippet"),
            row.get("serp_position"),
            row.get("searchengine") or engine,
        )
        for row in envelope.get("results") or []
        if row.get("url")
    ]
    if not rows:
        return _failed("serp", "empty", cost_currency=currency)
    return {
        "source": "serp",
        "ok": True,
        "results": rows,
        "answer": None,
        "reason": None,
        "cost_usd": None,
        "cost_currency": currency,
        "cost": cost,
    }


@task
async def exa(query: str, *, top: int, engine: str = "yandex", proxy: str | None = None) -> dict:
    """Exa `/search`: their own pick, billed per call (`costDollars.total`)."""
    api_key, proxy = binding("exa", proxy=proxy)
    if not api_key:
        return _failed("exa", "no_key")
    payload = {"query": query, "numResults": top, "contents": {"text": {"maxCharacters": SNIPPET_CHARS}}}
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, proxy=proxy, trust_env=False) as client:
            response = await client.post(
                "https://api.exa.ai/search", json=payload, headers={"x-api-key": api_key, "User-Agent": UA}
            )
    except httpx.HTTPError as exc:
        return _failed("exa", f"error:{type(exc).__name__}")
    body = _body(response)
    cost = (body.get("costDollars") or {}).get("total")
    if response.status_code in BLOCKED:
        return _failed("exa", "blocked", cost_usd=cost)
    if response.status_code >= 400:
        return _failed("exa", f"http:{response.status_code}", cost_usd=cost)
    rows = [
        _row(item["url"], item.get("title"), (item.get("text") or "")[:SNIPPET_CHARS] or None, number, "exa")
        for number, item in enumerate(body.get("results") or [], start=1)
        if item.get("url")
    ]
    if not rows:
        return _failed("exa", "empty", cost_usd=cost)
    return {"source": "exa", "ok": True, "results": rows, "answer": None, "reason": None, "cost_usd": cost}


@task
async def tavily(query: str, *, top: int, engine: str = "yandex", proxy: str | None = None) -> dict:
    """Tavily `/search`: their pick plus a written answer when the account returns one; billed in credits."""
    api_key, proxy = binding("tavily", proxy=proxy)
    if not api_key:
        return _failed("tavily", "no_key")
    payload = {"query": query, "max_results": top, "include_answer": True, "search_depth": "basic"}
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, proxy=proxy, trust_env=False) as client:
            response = await client.post(
                "https://api.tavily.com/search",
                json=payload,
                headers={"Authorization": f"Bearer {api_key}", "User-Agent": UA},
            )
    except httpx.HTTPError as exc:
        return _failed("tavily", f"error:{type(exc).__name__}")
    body = _body(response)
    credits = (body.get("usage") or {}).get("credits")
    cost = credits / 1000 if credits else None  # credits, not USD: the same honest note as the extract step
    if response.status_code in BLOCKED:
        return _failed("tavily", "blocked", cost_usd=cost)
    if response.status_code >= 400:
        return _failed("tavily", f"http:{response.status_code}", cost_usd=cost)
    rows = [
        _row(item["url"], item.get("title"), (item.get("content") or "")[:SNIPPET_CHARS] or None, number, "tavily")
        for number, item in enumerate(body.get("results") or [], start=1)
        if item.get("url")
    ]
    if not rows:
        return _failed("tavily", "empty", cost_usd=cost)
    answer = body.get("answer") if isinstance(body.get("answer"), str) else None
    return {"source": "tavily", "ok": True, "results": rows, "answer": answer, "reason": None, "cost_usd": cost}


def _body(response: httpx.Response) -> dict:
    """The JSON body, or an empty dict: a captcha page is a failure of the source, not of the run."""
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


SOURCES: dict[str, Search] = {"serp": serp, "exa": exa, "tavily": tavily}


def _merge(answers: list[dict]) -> list[dict]:
    """One row per URL: the sources it came from, the best position over them, and the first snippet found."""
    rows: dict[str, dict] = {}
    for answer in answers:
        for row in answer["results"]:
            key = row["url"].rstrip("/").lower()
            entry = rows.setdefault(
                key,
                {
                    "position": None,
                    "url": row["url"],
                    "domain": row["domain"],
                    "title": row["title"],
                    "snippet": row["snippet"],
                    "engine": row["engine"],
                    "sources": [],
                    "positions": {},
                    "best_position": None,
                },
            )
            entry["sources"].append(answer["source"])
            entry["positions"][answer["source"]] = row["position"]
            if entry["snippet"] is None:
                entry["snippet"] = row["snippet"]
            best = entry["best_position"]
            if row["position"] is not None and (best is None or row["position"] < best):
                entry["best_position"] = row["position"]
                entry["position"] = row["position"]
                entry["title"] = row["title"] or entry["title"]
    ordered = sorted(rows.values(), key=lambda entry: (-len(entry["sources"]), entry["best_position"] or 10**6))
    return ordered


async def _safe(name: str, query: str, *, top: int, engine: str, proxy: str | None) -> dict:
    """One source, with an exception of its own turned into a reason: one source must not stop the others."""
    try:
        return await SOURCES[name](query, top=top, engine=engine, proxy=proxy)
    except Exception as exc:
        return _failed(name, f"raised:{type(exc).__name__}: {exc}"[:200])


async def _search_one(query: str, names: list[str], *, mode: str, top: int, engine: str, proxy: str | None) -> dict:
    """One query: the first source that answers (cascade) or all of them merged (parallel)."""
    if mode == "cascade":
        tried: list[dict] = []
        spent = 0.0
        for name in names:
            found = await _safe(name, query, top=top, engine=engine, proxy=proxy)
            spent += found["cost_usd"] or 0.0
            if found["ok"]:
                return {
                    "query": query,
                    "ok": True,
                    "mode": mode,
                    "source": name,
                    "results": found["results"],
                    "answer": found["answer"],
                    "tried": tried,
                    "cost_usd": round(spent, 6),
                }
            tried.append({"source": name, "reason": found["reason"]})
        return {
            "query": query,
            "ok": False,
            "mode": mode,
            "source": None,
            "results": [],
            "answer": None,
            "reason": "all_sources_failed",
            "tried": tried,
            "cost_usd": round(spent, 6),
        }
    answers = await asyncio.gather(*(_safe(name, query, top=top, engine=engine, proxy=proxy) for name in names))
    answers = list(answers)
    winners = [answer for answer in answers if answer["ok"]]
    written = [answer["answer"] for answer in winners if answer["answer"]]
    return {
        "query": query,
        "ok": bool(winners),
        "mode": mode,
        "source": winners[0]["source"] if winners else None,
        "results": _merge(winners),
        "answer": written[0] if written else None,
        "answers": {answer["source"]: answer["answer"] for answer in winners if answer["answer"]},
        "tried": [{"source": answer["source"], "reason": answer["reason"]} for answer in answers if not answer["ok"]],
        "by_source": {
            answer["source"]: {
                "ok": answer["ok"],
                "reason": answer["reason"],
                "results": answer["results"],
                "cost_usd": answer["cost_usd"],
            }
            for answer in answers
        },
        "cost_usd": round(sum(answer["cost_usd"] or 0.0 for answer in answers), 6),
    }


async def _html(url: str, *, proxy: str | None) -> dict:
    """The page's HTML: `spider fetch --include html` first (its fingerprint and proxies), a plain GET after it."""
    argv = ["spider", "fetch", url, "--json", "--include", "html"]
    if proxy:
        argv += ["--set", f"crawl.proxy_urls={proxy}"]
    try:
        done = await asyncio.to_thread(
            subprocess.run,
            argv,
            capture_output=True,
            text=True,
            timeout=TIMEOUT * 3,
            check=False,
        )
        page = json.loads(done.stdout)
        if done.returncode == 0 and page.get("html"):
            return {"html": page["html"], "title": (page.get("seo") or {}).get("title"), "source": "spider"}
    except (FileNotFoundError, subprocess.TimeoutExpired, ValueError):
        pass  # no spider, no answer, no JSON: the plain GET below is the fallback
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, proxy=proxy, follow_redirects=True, trust_env=False) as client:
            response = await client.get(url, headers={"User-Agent": UA})
    except httpx.HTTPError:
        return {}
    return {"html": response.text, "title": None, "source": "direct"} if response.status_code < 400 else {}


@task
async def _main_text(url: str, *, proxy: str | None) -> dict:
    """The page's own text: HTML through trafilatura, `spider`'s markdown when the extraction comes back empty."""
    page = await _html(url, proxy=proxy)
    text = trafilatura.extract(page["html"], include_comments=False, include_tables=True, favor_precision=True)
    if text:
        return document(
            url, source=page["source"], ok=True, text=text[:MAX_TEXT], title=page["title"], via="trafilatura"
        )
    found = await adapters.spider(url, proxy=proxy)
    if found["ok"]:
        found["via"] = "markdown"
        return found
    return document(url, source="none", ok=False, reason=found["reason"] or "empty_text")


@workflow
async def web_search(
    queries: list[str],
    *,
    mode: str = "cascade",
    sources: list[str] | None = None,
    top: int = 10,
    engine: str = "yandex",
    fetch_top: int = 0,
    budget_usd: float | None = None,
    proxy: str | None = None,
) -> dict:
    """Search every query, merge or fall back as `mode` says, and optionally return the text of the first pages.

    Parameters:
        queries: what to look up; each one is its own set of source calls.
        mode: `cascade` — the first source with results wins; `parallel` — all of them, merged by URL.
        sources: which sources to ask, in order; `None` is `serp`, `exa`, `tavily`.
        top: results per source per query.
        engine: the search engine of `serp` (`yandex`, `google`, `both`).
        fetch_top: how many of the found pages to bring back as text (`0` — none).
        budget_usd: a ceiling on the USD the sources and the fetching may spend, checked per query.
        proxy: forced on every call, over the key's own binding.

    Returns `{"queries", "mode", "sources", "budget_spent_usd", "ok"}`; every query entry carries `results`,
    `tried`, `cost_usd` and — with `fetch_top` — `documents` with the extracted text.
    """
    if mode not in MODES:
        raise ValueError(f"unknown mode '{mode}'; known: {', '.join(MODES)}")
    names = list(sources or SOURCES_ORDER)
    unknown = [name for name in names if name not in SOURCES]
    if unknown:
        raise ValueError(f"unknown source {unknown[0]!r}; known: {', '.join(SOURCES)}")
    if not queries:
        raise ValueError("no queries")

    found: list[dict] = []
    spent = 0.0
    for query in queries:
        if budget_usd is not None and spent >= budget_usd:
            found.append({"query": query, "ok": False, "reason": "budget_exhausted", "results": [], "tried": []})
            continue
        entry = await _search_one(query, names, mode=mode, top=top, engine=engine, proxy=proxy)
        spent += entry["cost_usd"] or 0.0
        if fetch_top and entry["results"]:
            urls = [row["url"] for row in entry["results"][:fetch_top]]
            entry["documents"] = await asyncio.gather(*(_main_text(url, proxy=proxy) for url in urls))
            entry["documents"] = list(entry["documents"])
            spent += sum(doc["cost_usd"] or 0.0 for doc in entry["documents"])
        found.append(entry)
    return {
        "queries": found,
        "mode": mode,
        "sources": names,
        "budget_spent_usd": round(spent, 6),
        "ok": any(entry["ok"] for entry in found),
    }
