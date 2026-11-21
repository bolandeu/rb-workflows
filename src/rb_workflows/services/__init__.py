"""External services that collect a keyword set for a URL.

The wrapper (`rb_workflows.semantics`) knows nothing about any of them: a service is one module with
`async def collect(client, url, *, region, stopwords, ...) -> dict`, plus a line in `SERVICES`. Adding a service
never touches the wrapper, the CLI or the report shape — only this registry.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol

import httpx

from . import arsenkin

__all__ = ["SERVICES", "Service", "service"]


class Service(Protocol):
    """What every service module offers: one URL in, its phrases out (already in the report shape)."""

    async def collect(
        self,
        client: httpx.AsyncClient,
        token: str,
        url: str,
        *,
        region: str = "msk",
        stopwords: Iterable[str] = (),
        poll_interval: float = 10.0,
        timeout: float = 1800.0,
    ) -> dict: ...


SERVICES: dict[str, Service] = {"arsenkin": arsenkin}


def service(name: str) -> Service:
    """The module of one service by name (`arsenkin`), or `KeyError` naming what exists."""
    try:
        return SERVICES[name]
    except KeyError:
        raise KeyError(f"unknown service '{name}'; known: {', '.join(sorted(SERVICES))}") from None
