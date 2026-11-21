"""Arsenkin TOOLS: `semantics` for one URL, through the task API (set → check → get).

The API is asynchronous: a task costs limits, answers `{"code": "SET_TASK_OK", "task_id": …, "cost": …}`, then the
status is polled until `finish`, and the result is fetched. Limits of the account: at most 5 tasks at once (the
wrapper keeps that) and 30 requests a minute across all statuses (the wrapper's rate limiter keeps that).

A phrase without a position is still a phrase: `position` is `None` and the summary counts the URL it came from.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable

import httpx

__all__ = ["BASE", "REGIONS", "collect"]

BASE = "https://arsenkin.ru/api/tools"
# What `--engine` names, as Arsenkin calls the region.
REGIONS = {"msk": "msk", "gru": "gru"}
# The task statuses that mean "wait"; anything else is either the result or a failure we report as is.
PENDING = frozenset({"queue", "process", "processing", "wait"})


class ArsenkinError(RuntimeError):
    """The service refused the task or ended it without a result."""


RATE_LIMIT_PAUSE = 20.0  # seconds to wait when the account says 30 requests a minute are spent
RATE_LIMIT_TRIES = 6


async def _post(client: httpx.AsyncClient, token: str, path: str, body: dict) -> dict:
    """One call of the task API (`set`, `check`, `get`, `info`), with every refusal reduced to one sentence.

    Retries: HTTP 429 means the account spent its 30 requests a minute; the call waits `RATE_LIMIT_PAUSE` and tries
    again, up to `RATE_LIMIT_TRIES` times, and only then raises. A 429 that clears is not an error for the caller.

    Raises `ArsenkinError` (never a bare httpx or json exception, because the caller shows `str(exc)` to a person):
    - transport failure — `"ConnectError while calling /set: …"`;
    - HTTP 4xx/5xx — `"HTTP 500 for /set: <first 300 chars of the body>"`;
    - a JSON body with `code` ending in `ERROR` — `"/set: AUTH_ERROR token is revoked"`;
    - a body that is not JSON at all (captcha page, proxy error page) — `"/set answered not JSON: <200 chars>"`.

    Returns the parsed JSON body of a successful call.
    """
    for attempt in range(RATE_LIMIT_TRIES):
        try:
            response = await client.post(f"{BASE}/{path}", json=body, headers={"Authorization": f"Bearer {token}"})
        except httpx.HTTPError as exc:  # the transport is the service's failure too, not the caller's
            raise ArsenkinError(f"{type(exc).__name__} while calling /{path}: {exc}") from exc
        if response.status_code != 429:
            break
        if attempt == RATE_LIMIT_TRIES - 1:
            raise ArsenkinError("rate limited (HTTP 429): the account allows 30 requests a minute")
        await asyncio.sleep(RATE_LIMIT_PAUSE)
    if response.status_code >= 400:
        raise ArsenkinError(f"HTTP {response.status_code} for /{path}: {response.text[:300]}")
    try:
        payload = response.json()
    except ValueError as exc:  # a captcha page or a proxy error page: say so, do not leak a JSONDecodeError
        raise ArsenkinError(f"/{path} answered not JSON: {response.text[:200]}") from exc
    if isinstance(payload.get("code"), str) and payload["code"].endswith("ERROR"):
        raise ArsenkinError(f"/{path}: {payload.get('code')} {payload.get('msg') or payload.get('error')}")
    return payload


async def collect(
    client: httpx.AsyncClient,
    token: str,
    url: str,
    *,
    region: str = "msk",
    stopwords: Iterable[str] = (),
    poll_interval: float = 15.0,
    timeout: float = 1800.0,
) -> dict:
    """One URL through `semantics`: the task id, its cost, and the phrases with their positions.

    The API is a small state machine: `set` creates a task (it costs limits and returns `task_id`), `check` is polled
    every `poll_interval` seconds until the status leaves the pending set, `get` returns the phrases. The whole wait is
    bounded by `timeout`; the status is polled, not the result, so a finished task is fetched exactly once.

    Parameters:
        region: `msk` (Yandex Moscow) or `gru` (Google Moscow); anything else is passed through as the service's name.
        stopwords: minus-words; the service caps them at 10 000.
        poll_interval: seconds between status checks; the wrapper's 30-a-minute limit covers the sum of the polls.
        timeout: seconds a single task may take before `ArsenkinError`, so a stuck task cannot hold a batch forever.

    Returns a document in the report shape: `url`, `service`, `region`, `task_id`, `cost` (in limits), `queries`
    (each `phrase`, `position`, `ws`, `ws_absent`, `toponym`, `question`, `extra`) and `toponims`.

    Raises `ArsenkinError` for everything the service refuses — see `_post` for the wording of each case — including
    a missing `task_id`, a task that ended in a non-success status (the status name and the service's message go into
    the text) and a task that never left the pending state (`"still 'process' after N s"`).
    """
    body = {"mode": "url", "queries": [url], "region": REGIONS.get(region, region), "stopwords": list(stopwords)}
    started = await _post(client, token, "set", {"tools_name": "semantics", "data": body})
    task_id = started.get("task_id")
    if task_id is None:
        raise ArsenkinError(f"no task_id in the answer: {started}")
    cost = started.get("cost")

    waited = 0.0
    while True:
        status = await _post(client, token, "check", {"task_id": task_id})
        state = str(status.get("status") or "").lower()
        if state not in PENDING:
            break
        if waited >= timeout:
            raise ArsenkinError(f"task {task_id} still '{state}' after {timeout:g} s")
        await asyncio.sleep(poll_interval)
        waited += poll_interval
    if state not in {"finish", "finished", "done", "complete", "completed"}:
        raise ArsenkinError(f"task {task_id} ended as '{state}': {status.get('msg') or status}")

    payload = await _post(client, token, "get", {"task_id": task_id})
    result = payload.get("result") or {}
    toponims = frozenset(result.get("toponims") or [])
    return {
        "url": url,
        "service": "arsenkin",
        "region": REGIONS.get(region, region),
        "task_id": task_id,
        "cost": cost,
        "queries": [_query(item, toponims) for item in _items(result)],
        "toponims": sorted(toponims),
    }


def _items(result: dict) -> list[dict]:
    """The phrases of a result, whatever container the service chose this time (mapping by id, or a plain list)."""
    items = result.get("result") or []
    if isinstance(items, dict):
        return [item for item in items.values() if isinstance(item, dict)]
    return [item for item in items if isinstance(item, dict)]


def _query(item: dict, toponims: frozenset[str]) -> dict:
    """One phrase as the report wants it. The service sends `{"word", "ws", "pos", "isquest", …}` per phrase id."""
    word = str(item.get("word") or "")
    position = item.get("pos") or None  # 0 is "not in the top", not a position
    frequency = item.get("ws")
    return {
        "phrase": word,
        "position": position if isinstance(position, int) else None,
        "ws": frequency if isinstance(frequency, int) else None,
        "ws_absent": frequency == 0,
        "toponym": word in toponims,
        "question": bool(item.get("isquest")),
        "extra": {k: v for k, v in item.items() if k not in _KNOWN},
    }


_KNOWN = frozenset({"word", "ws", "pos", "wsk", "superwsk", "isquest", "docs", "serpf", "qid", "url"})
