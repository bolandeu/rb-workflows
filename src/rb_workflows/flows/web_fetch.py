"""`web_fetch`: one URL or a list, through a cascade of services, under a spend ceiling.

Deterministic by construction: the order of steps and the branch on every result are fixed for the same input, so a
Windmill replay reaches the same calls. No clock, no randomness, no environment reads — the only variable is what a
service answers, and that is either a step result or the workflow's input.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from wmill import workflow

from ..adapters import direct, exa, spider, tavily
from ..document import document

__all__ = ["ADAPTERS", "web_fetch"]

Step = Callable[..., Awaitable[dict]]

# Order matters: cheapest and best-quality text first, our own stack when a service refuses the page, plain HTTP last.
ADAPTERS: tuple[tuple[str, Step], ...] = (
    ("exa", exa),
    ("tavily", tavily),
    ("spider", spider),
    ("direct", direct),
)

# A failure worth another service. `blocked` and `empty_text` are the page's answer; `no_key` is ours (a service that
# is not configured must not stop the cascade), and a 5xx or a transport error may well pass on the next one.
RETRY_NEXT = frozenset({"blocked", "empty_text", "no_key", "timeout", "not_installed", "not_json"})


@workflow
async def web_fetch(
    urls: list[str],
    *,
    budget_usd: float | None = None,
    proxy: str | None = None,
    steps: list[str] | None = None,
) -> dict:
    """Fetch every URL, trying the steps in order until one returns text, and report what was spent.

    `steps` narrows the cascade by name (`["exa", "direct"]`) — useful for testing a single service and for pinning a
    workflow to one provider. `budget_usd` is a ceiling on the sum of `cost_usd`: once reached, the remaining URLs are
    reported as `budget_exhausted` instead of being fetched.
    """
    chosen = [(name, step) for name, step in ADAPTERS if steps is None or name in steps]
    if not chosen:
        raise ValueError(f"no step of {steps!r} exists; known: {[name for name, _ in ADAPTERS]}")

    documents: list[dict] = []
    spent = 0.0
    for url in urls:
        if budget_usd is not None and spent >= budget_usd:
            documents.append(document(url, source="none", ok=False, reason="budget_exhausted"))
            continue
        documents.append(await _fetch_one(url, chosen, proxy=proxy))
        spent += documents[-1]["cost_usd"] or 0.0
    return {"documents": documents, "budget_spent_usd": round(spent, 6), "ok": any(doc["ok"] for doc in documents)}


async def _fetch_one(url: str, steps: list[tuple[str, Step]], *, proxy: str | None) -> dict:
    """The first step that returns text wins; the reasons of the rest are kept in `tried`."""
    tried: list[dict] = []
    for name, step in steps:
        try:
            found = await step(url, proxy=proxy)
        except Exception as exc:  # unexpected: the step's own contract is `ok: false`, so this is a defect or an outage
            tried.append({"step": name, "reason": f"raised:{type(exc).__name__}: {exc}"[:200]})
            continue
        if found["ok"]:
            found["tried"] = tried
            return found
        tried.append({"step": name, "reason": found["reason"]})
        if found["reason"] not in RETRY_NEXT and not str(found["reason"]).startswith(("http:", "error:")):
            return {**found, "tried": tried}  # e.g. `no_such_step`: asking more services changes nothing
    failed = document(url, source="none", ok=False, reason="all_steps_failed")
    failed["tried"] = tried
    return failed
