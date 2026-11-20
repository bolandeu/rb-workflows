"""The one shape every step returns: a JSON document, never an object.

A step's result is checkpointed in Windmill, so it has to be JSON: `datetime` comes back as a string, a tuple as a
list. Keeping the document plain also means the local run hands back exactly the shape a deployed run does.
"""

from __future__ import annotations

import hashlib

__all__ = ["MAX_TEXT", "document"]

MAX_TEXT = 200_000  # characters kept per page: a checkpoint must not carry a megabyte of text


def document(
    url: str,
    *,
    source: str,
    ok: bool,
    text: str | None = None,
    final_url: str | None = None,
    status: int | None = None,
    title: str | None = None,
    via: str | None = None,
    cost_usd: float | None = None,
    reason: str | None = None,
    truncated: bool = False,
) -> dict:
    """One page as JSON: where it came from, how much it cost, and — for a failure — why.

    A failure is a value, not an exception: `ok: false` with a `reason` (`blocked`, `empty_text`, `error`) is what
    lets the cascade move to the next step.
    """
    kept = (text or "")[:MAX_TEXT]
    return {
        "url": url,
        "final_url": final_url or url,
        "ok": ok,
        "status": status,
        "title": title,
        "text": kept,
        "text_sha256": hashlib.sha256(kept.encode()).hexdigest() if kept else None,
        "text_truncated": truncated or len(text or "") > len(kept),
        "source": source,
        "via": via,
        "cost_usd": cost_usd,
        "reason": reason,
    }
