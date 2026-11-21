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
from collections.abc import Iterable, Mapping

import httpx

from .credentials import binding
from .services import Service, service

__all__ = ["RateLimiter", "collect", "normalise", "summary"]

MODES = ("url", "domain")

def normalise(request: Mapping) -> dict:
    """The job's request from Arsenkin's `semantics` body (or from the bare `data` object), validated.

    Arsenkin's own keys are accepted as they are — `queries`, `region`, `stopwords`, `mode` — so a task written for
    their API runs here unchanged. `queries` is what gets collected: URLs in `url` mode, a domain in `domain` mode.
    """
    raw = request.get("data")
    data: Mapping = raw if isinstance(raw, Mapping) else request
    queries = [str(item) for item in data.get("queries") or data.get("urls") or []]
    if not queries:
        raise ValueError("no queries: 'queries' (Arsenkin) or 'urls' must be a non-empty list")
    mode = str(data.get("mode") or "url")
    if mode not in MODES:
        raise ValueError(f"unknown mode '{mode}'; known: {', '.join(MODES)}")
    return {
        "queries": queries,
        "mode": mode,
        "engine": str(data.get("engine") or data.get("region") or "msk"),
        "stopwords": [str(word) for word in data.get("stopwords") or []],
        "concurrency": int(data.get("concurrency") or MAX_CONCURRENT),
        "timeout": float(data.get("timeout") or 1800.0),
        "poll_interval": float(data.get("poll_interval") or 15.0),
    }

MAX_CONCURRENT = 5  # tasks at once, per the account's own limit
PER_MINUTE = 30  # requests of any kind, per the account's own limit


class RateLimiter:
    """At most `limit` calls per `window` seconds, shared by every request of the run.

    Sliding window, not fixed buckets: a burst of `limit` calls is allowed at once, and the next one waits until the
    oldest stamp leaves the window. Waiting is the point — the account answers 429 otherwise — so `waited` accumulates
    the seconds a run spent in this limiter and shows up in the report as `requests_waited_s`.
    """

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
    mode: str = "url",
) -> dict:
    try:
        async with _LIMITER:
            found = await module.collect(
                client, token, url, region=region, stopwords=stopwords, mode=mode, timeout=timeout,
                poll_interval=poll_interval,
            )
        return {"ok": True, **found}
    except Exception as exc:  # a failed URL is a value: the batch goes on
        return {"url": url, "ok": False, "error": f"{type(exc).__name__}: {exc}"[:400]}


_LIMITER = RateLimiter()


async def collect(
    request: Mapping | Iterable[str],
    *,
    urls: Iterable[str] | None = None,
    name: str = "arsenkin",
    region: str = "msk",
    stopwords: Iterable[str] = (),
    concurrency: int = MAX_CONCURRENT,
    timeout: float = 1800.0,
    poll_interval: float = 15.0,
    token: str | None = None,
) -> dict:
    """Every URL of `request` through one service, up to `concurrency` at a time; the report and its summary.

    `request` is the job's own schema: `{"urls": [...], "engine": "msk", "stopwords": [...],
    "concurrency": 5, "timeout": 1800, "poll_interval": 15}`. The keyword arguments stay for the CLI and for callers
    that already hold them: an argument named here wins over the same key in the request.
    """
    # a caller may pass the bare list of URLs (the CLI's own habit) or the job's request dict
    fields: Mapping = request if isinstance(request, Mapping) else {}
    urls = (
        urls if urls is not None else (fields.get("urls") or list(request) if not fields else fields.get("urls") or [])
    )
    name = str(fields.get("service") or name)
    region = str(fields.get("engine") or fields.get("region") or region)
    stopwords = fields.get("stopwords") or stopwords
    concurrency = int(fields.get("concurrency") or concurrency)
    timeout = float(fields.get("timeout") or timeout)
    poll_interval = float(fields.get("poll_interval") or poll_interval)
    token = str(fields["token"]) if fields.get("token") else token

    return await _collect(
        urls,
        name=name,
        region=region,
        stopwords=stopwords,
        concurrency=concurrency,
        timeout=timeout,
        poll_interval=poll_interval,
        token=token,
    )


async def _collect(
    urls: Iterable[str],
    *,
    name: str,
    region: str,
    stopwords: Iterable[str],
    concurrency: int,
    timeout: float,
    poll_interval: float,
    token: str | None,
    mode: str = "url",
) -> dict:
    """The batch itself: `concurrency` jobs in flight, one rate limiter, one summary.

    The batch never waits for the first URL: `concurrency` tasks are in flight (5 is the account's own limit), each one
    a full set → check → get pipeline, so a slow task only occupies its own slot. Every request of every task passes the
    shared rate limiter, because the service counts the requests of a user, not of a connection.

    Parameters:
        name: the service to ask (`arsenkin`); resolved through `services.SERVICES`, so a new service needs no change here.
        region: the search engine/region, `msk` or `gru`, passed to the service unchanged.
        stopwords: minus-words, forwarded as is.
        token: an explicit token; without it the token comes from the package store, then from `token get <name>`.
        timeout: how long a single task may take; it is the service's per-task bound, not the batch's.

    Returns `{"ok", "service", "region", "requests_waited_s", "elapsed_s", "urls", "summary"}`:
        - `urls` holds one document per URL in the order given; a failure is `{"url", "ok": false, "error"}` where
          `error` is `"<ExceptionType>: <message>"` — a failed URL never raises out of the batch;
        - `ok` is true only when every URL succeeded;
        - `summary` is the per-phrase cross-URL roll-up (see `summary`).

    Raises before any request for a mistake of the caller: `KeyError` for an unknown service, `RuntimeError` for a
    missing token — both name what to fix.
    """
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
                region=region, stopwords=words, mode=mode, timeout=timeout, poll_interval=poll_interval,
            )

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
    """One row per phrase: how many URLs it came from, and where it stands in each of them.

    A failed document contributes nothing, so the summary never mixes a service error with a phrase list. A phrase
    without a position is still a phrase of that URL: it appears with `position: null`, and `urls_count` includes it —
    that is how "the phrase exists, but the site does not rank for it" stays visible.

    Each row: `phrase`, `urls` (URL → position or None), `urls_count`, `ws` (the largest Wordstat frequency seen) and
    the `toponym`/`question` flags OR-ed over the URLs. Rows are ordered by how many URLs carry the phrase, then by `ws`.
    """
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
