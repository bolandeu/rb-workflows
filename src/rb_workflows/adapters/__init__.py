"""Steps: each one gets a URL and returns a `document()` — JSON, never an object, never an exception for a bad page.

A step is an `@task`: in Windmill it becomes its own job (with `retry`, `timeout` and cache), locally it is a plain
coroutine. `ok: false` with a `reason` is the signal for the cascade to try the next step; an exception is for the
unexpected and is retried by Windmill.
"""

from __future__ import annotations

import hashlib
import json
import subprocess

import httpx
from wmill import task

from ..credentials import binding
from ..document import MAX_TEXT, document

__all__ = ["direct", "exa", "spider", "tavily"]

TIMEOUT = 30.0
UA = "rb-workflows/0.1 (+https://github.com/bolandeu)"

# `blocked` — the service refused this page (403/429): another service may still get it.
BLOCKED = frozenset({401, 403, 429})


def _empty(service: str, url: str, *, reason: str, status: int | None = None, cost: float | None = None) -> dict:
    return document(url, source=service, ok=False, reason=reason, status=status, cost_usd=cost)


@task
async def exa(url: str, *, proxy: str | None = None) -> dict:
    """Exa `/contents`: the best text for an LLM, billed per page (`costDollars.total`)."""
    api_key, proxy = binding("exa", proxy=proxy)
    if not api_key:
        return _empty("exa", url, reason="no_key")
    payload = {"urls": [url], "text": {"maxCharacters": MAX_TEXT}}
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, proxy=proxy, trust_env=False) as client:
            response = await client.post(
                "https://api.exa.ai/contents", json=payload, headers={"x-api-key": api_key, "User-Agent": UA}
            )
    except httpx.HTTPError as exc:
        return _empty("exa", url, reason=f"error:{type(exc).__name__}")
    cost = ((response.json().get("costDollars") or {}).get("total")) if response.text else None
    if response.status_code in BLOCKED:
        return _empty("exa", url, reason="blocked", status=response.status_code, cost=cost)
    if response.status_code >= 400:
        return _empty("exa", url, reason=f"http:{response.status_code}", status=response.status_code, cost=cost)
    found = (response.json().get("results") or [None])[0]
    if not found or not found.get("text"):
        return _empty("exa", url, reason="empty_text", status=response.status_code, cost=cost)
    return document(
        url,
        source="exa",
        ok=True,
        text=found.get("text"),
        final_url=found.get("url"),
        title=found.get("title"),
        status=response.status_code,
        via="proxy" if proxy else "direct",
        cost_usd=cost,
    )


@task
async def tavily(url: str, *, proxy: str | None = None) -> dict:
    """Tavily `/extract`: `raw_content` of the page, billed per successful extraction."""
    api_key, proxy = binding("tavily", proxy=proxy)
    if not api_key:
        return _empty("tavily", url, reason="no_key")
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, proxy=proxy, trust_env=False) as client:
            response = await client.post(
                "https://api.tavily.com/extract",
                json={"urls": [url]},
                headers={"Authorization": f"Bearer {api_key}", "User-Agent": UA},
            )
    except httpx.HTTPError as exc:
        return _empty("tavily", url, reason=f"error:{type(exc).__name__}")
    if response.status_code in BLOCKED:
        return _empty("tavily", url, reason="blocked", status=response.status_code)
    if response.status_code >= 400:
        return _empty("tavily", url, reason=f"http:{response.status_code}", status=response.status_code)
    body = response.json()
    found = (body.get("results") or [None])[0]
    if not found or not found.get("raw_content"):
        return _empty("tavily", url, reason="empty_text", status=response.status_code)
    return document(
        url,
        source="tavily",
        ok=True,
        text=found.get("raw_content"),
        final_url=found.get("url"),
        status=response.status_code,
        via="proxy" if proxy else "direct",
        cost_usd=1.0 / 1000 if body.get("usage", {}).get("credits") else None,  # credits, not USD: keep it honest
    )


@task
async def spider(url: str, *, proxy: str | None = None, render: bool = False) -> dict:
    """`spider fetch --json`: our own stack (fingerprint, proxies, browser) — the step that survives blocks."""
    argv = ["spider", "fetch", url, "--json", "--include", "markdown,text"]
    if render:
        argv.append("--render")
    if proxy:
        argv += ["--set", f"crawl.proxy_urls={proxy}"]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=TIMEOUT * 3, check=False)  # noqa: S603
    except FileNotFoundError:
        return _empty("spider", url, reason="not_installed")
    except subprocess.TimeoutExpired:
        return _empty("spider", url, reason="timeout")
    try:
        page = json.loads(proc.stdout)
    except ValueError:
        return _empty("spider", url, reason=f"not_json:{proc.returncode}")
    if proc.returncode != 0 or page.get("error"):
        reason = "blocked" if page.get("status") in BLOCKED else f"error:{page.get('error') or proc.returncode}"
        return _empty("spider", url, reason=reason, status=page.get("status"))
    body = page.get("markdown") or page.get("text")
    if not body:
        return _empty("spider", url, reason="empty_text", status=page.get("status"))
    return document(
        url,
        source="spider",
        ok=True,
        text=body,
        final_url=page.get("final_url"),
        status=page.get("status"),
        title=(page.get("seo") or {}).get("title"),
        via=page.get("via"),
    )


@task
async def direct(url: str, *, proxy: str | None = None) -> dict:
    """Plain HTTP GET: the last resort, no key, no cost — and no way around a JS-only page."""
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, proxy=proxy, follow_redirects=True, trust_env=False) as client:
            response = await client.get(url, headers={"User-Agent": UA})
    except httpx.HTTPError as exc:
        return _empty("direct", url, reason=f"error:{type(exc).__name__}")
    if response.status_code in BLOCKED:
        return _empty("direct", url, reason="blocked", status=response.status_code)
    if response.status_code >= 400:
        return _empty("direct", url, reason=f"http:{response.status_code}", status=response.status_code)
    body = response.text
    if not body.strip():
        return _empty("direct", url, reason="empty_text", status=response.status_code)
    return document(
        url,
        source="direct",
        ok=True,
        text=body,
        final_url=str(response.url),
        status=response.status_code,
        via="proxy" if proxy else "direct",
        cost_usd=0.0,
    )


def text_sha256(text: str) -> str:
    """Content identity, for deduplicating the same page fetched twice."""
    return hashlib.sha256(text.encode()).hexdigest()
