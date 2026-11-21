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
    for attempt in range(RATE_LIMIT_TRIES):
        response = await client.post(f"{BASE}/{path}", json=body, headers={"Authorization": f"Bearer {token}"})
        if response.status_code != 429:
            break
        if attempt == RATE_LIMIT_TRIES - 1:
            raise ArsenkinError("rate limited (HTTP 429): the account allows 30 requests a minute")
        await asyncio.sleep(RATE_LIMIT_PAUSE)
    if response.status_code >= 400:
        raise ArsenkinError(f"HTTP {response.status_code} for /{path}: {response.text[:300]}")
    payload = response.json()
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
    """One URL through `semantics`: the task id, its cost, and the phrases with their positions."""
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
        "queries": [_query(item, toponims) for item in (result.get("result") or {}).values()],
        "toponims": sorted(toponims),
    }


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
