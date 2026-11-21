"""A step's credential and proxy: the local store, 9router's database, or Windmill's own resources.

Which backend answers is decided by where the code runs, never by the workflow: locally there is no Windmill and no
`~/.9router` on the server. A workflow only says which service it is talking to.

- inside a Windmill job (`WM_TOKEN` set) → `wmill.get_resource` for `u/me/<service>`, then `f/rb_workflows/<service>`;
- `RB_WORKFLOWS_SOURCE=9router` → keys, proxies and rotation from `rb_ninerouter` (this machine only);
- otherwise → `rb_credentials.Store("workflows")`: `WORKFLOWS_<SERVICE>_API_KEY` and `WORKFLOWS_<SERVICE>_PROXY`.
"""

from __future__ import annotations

import contextlib
import importlib
import os
import shutil
import subprocess

__all__ = ["binding", "in_windmill"]

SERVICE_ENV = "RB_WORKFLOWS_SOURCE"


def in_windmill() -> bool:
    """Whether this code runs as a Windmill job (its client is then the only credential source)."""
    return bool(os.environ.get("WM_TOKEN") or os.environ.get("WM_JOB_ID"))


def _from_windmill(service: str) -> tuple[str | None, str | None]:
    wmill = importlib.import_module("wmill")
    for path in (f"u/me/{service}", f"f/rb_workflows/{service}"):
        with contextlib.suppress(Exception):  # the resource may not exist; the next path or the store may
            resource = wmill.get_resource(path)
            if isinstance(resource, dict):
                return resource.get("api_key"), resource.get("proxy")
    return None, None


def _from_ninerouter(service: str, key: str | None) -> tuple[str | None, str | None]:
    router = importlib.import_module("rb_ninerouter")
    if key is not None:
        for candidate in router.bindings(service):
            if candidate.name == key:
                return candidate.reveal(), candidate.proxy
        return None, None
    try:
        chosen = router.pick(service)
    except LookupError:  # every key of that service is dead or rate-limited: no key, not a crash
        return None, None
    return chosen.reveal(), chosen.proxy


def _from_token_cli(service: str) -> tuple[str | None, str | None]:
    """The generic token store of this machine: `token get <service>` (keychain, Doppler as the fallback)."""
    exe = shutil.which("token")
    if exe is None:
        return None, None
    try:
        found = subprocess.run(  # noqa: S603 - the path is resolved here, not built from input
            [exe, "get", service], capture_output=True, text=True, check=False, timeout=20
        )
    except (OSError, subprocess.SubprocessError):
        return None, None
    value = found.stdout.strip() if found.returncode == 0 else ""
    return (value or None), None


def _from_store(service: str) -> tuple[str | None, str | None]:
    from rb_credentials import Store

    store = Store("workflows")
    return store.get_secret(service), store.get_secret(service, "proxy")


def binding(service: str, *, key: str | None = None, proxy: str | None = None) -> tuple[str | None, str | None]:
    """(api_key, proxy) for a service. An explicit `proxy` wins; otherwise the backend's own binding is used."""
    api_key, found_proxy = (None, None)
    if in_windmill():
        api_key, found_proxy = _from_windmill(service)
    elif os.environ.get(SERVICE_ENV) == "9router":
        try:
            api_key, found_proxy = _from_ninerouter(service, key or os.environ.get("RB_WORKFLOWS_KEY"))
        except ModuleNotFoundError:  # the package is optional: fall back to the store
            api_key, found_proxy = _from_store(service)
    else:
        api_key, found_proxy = _from_store(service)
    if api_key is None:  # the package's own store said nothing: ask the machine-wide token store
        api_key, found_proxy = _from_token_cli(service)
    return api_key, proxy or found_proxy
