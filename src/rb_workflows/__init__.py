"""Workflows as code that run locally and deploy to Windmill unchanged.

A workflow is an `@workflow`-decorated `async def`; a step is an `@task`-decorated function returning a JSON dict.
Outside a Windmill job the same code runs here: `@task` executes the body, `step()` runs inline, `sleep()` falls back
to `asyncio.sleep` (that is Windmill's own behaviour, not a shim of ours).

First workflow: `web_fetch` — one URL or a list, obtained through a cascade of services (exa → tavily → spider →
direct), with a spend ceiling and a per-step provenance record.
"""

from __future__ import annotations

from .document import document
from .flows.web_fetch import ADAPTERS, web_fetch

__all__ = ["ADAPTERS", "document", "web_fetch"]
