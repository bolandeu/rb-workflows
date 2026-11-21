"""`semantics`: a keyword set for one URL or a batch, from any service in the registry.

The wrapper owns what every service shares: the task limits, the rate limit, the batch fan-out and the report. It
knows nothing about a service's protocol — that lives behind `services.<name>.collect`.

A batch does not wait for the first URL to finish: up to `concurrency` URLs are in flight at once (the account allows
5 tasks), and the account's 30 requests a minute are shared by all of them. A URL that fails is reported as such and
does not stop the rest.
"""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from collections.abc import Iterable

import httpx

from .credentials import binding
from .services import Service, service

__all__ = ["RateLimiter", "collect", "summary"]

MAX_CONCURRENT = 5  # tasks at once, per the account's own limit
PER_MINUTE = 30  # requests of any kind, per the account's own limit


class RateLimiter:
    """At most `limit` calls per `window` seconds, shared by every request of the run."""

    def __init__(self, limit: int = PER_MINUTE, window: float = 60.0) -> None:
        self.limit, self.window = limit, window
        self._stamps: list[float] = []
        self._lock = asyncio.Lock()
        self.waited = 0.0

    async def __aenter__(self) -> RateLimiter:
        while True:
            async with self._lock:
                now = time.monotonic()
                self._stamps = [stamp for stamp in self._stamps if now - stamp < self.window]
                if len(self._stamps) < self.limit:
                    self._stamps.append(now)
                    return self
                delay = self.window - (now - self._stamps[0])
            self.waited += delay
            await asyncio.sleep(max(delay, 0.01))

    async def __aexit__(self, *_exc: object) -> None:
        return None


async def _one(
    client: httpx.AsyncClient,
    token: str,
    module: Service,
    url: str,
    *,
    region: str,
    stopwords: tuple[str, ...],
    timeout: float,
    poll_interval: float,
) -> dict:
    try:
        async with _LIMITER:
            found = await module.collect(
                client, token, url, region=region, stopwords=stopwords, timeout=timeout, poll_interval=poll_interval
            )
        return {"ok": True, **found}
    except Exception as exc:  # a failed URL is a value: the batch goes on
        return {"url": url, "ok": False, "error": f"{type(exc).__name__}: {exc}"[:400]}


_LIMITER = RateLimiter()


async def collect(
    urls: Iterable[str],
    *,
    name: str = "arsenkin",
    region: str = "msk",
    stopwords: Iterable[str] = (),
    concurrency: int = MAX_CONCURRENT,
    timeout: float = 1800.0,
    poll_interval: float = 15.0,
    token: str | None = None,
) -> dict:
    """Every URL through one service, up to `concurrency` at a time; the report and its summary."""
    module = service(name)
    token = token or binding(name)[0]
    if not token:
        raise RuntimeError(f"no token for '{name}': `token set {name}`")
    words = tuple(stopwords)
    gate = asyncio.Semaphore(max(1, concurrency))
    started = time.monotonic()

    async def run(url: str) -> dict:
        async with gate:
            return await _one(
                client, token, module, url,
                region=region, stopwords=words, timeout=timeout, poll_interval=poll_interval,
            )  # fmt: skip

    async with httpx.AsyncClient(timeout=60.0, trust_env=False) as client:
        documents = await asyncio.gather(*(run(url) for url in urls))

    return {
        "ok": all(doc.get("ok") for doc in documents),
        "service": name,
        "region": region,
        "requests_waited_s": round(_LIMITER.waited, 1),
        "elapsed_s": round(time.monotonic() - started, 1),
        "urls": list(documents),
        "summary": summary(documents),
    }


def summary(documents: list[dict]) -> list[dict]:
    """One row per phrase: how many URLs it came from, and where it stands in each of them."""
    seen: dict[str, dict] = defaultdict(
        lambda: {"phrase": "", "urls": {}, "urls_count": 0, "ws": None, "toponym": False, "question": False}
    )
    for document in documents:
        if not document.get("ok"):
            continue
        url = document["url"]
        for query in document.get("queries") or []:
            row = seen[query["phrase"]]
            row["phrase"] = query["phrase"]
            row["urls"][url] = query["position"]
            row["urls_count"] = len(row["urls"])
            row["ws"] = max(row["ws"] or 0, query["ws"] or 0) or None
            row["toponym"] = row["toponym"] or query["toponym"]
            row["question"] = row["question"] or query["question"]
    return sorted(seen.values(), key=lambda row: (-row["urls_count"], -(row["ws"] or 0), row["phrase"]))
