"""The version this CLI reports: the installed distribution's, or a dev marker when run from a source tree."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _version


def version() -> str:
    try:
        return _version("rb-workflows")
    except PackageNotFoundError:  # a source checkout without an install
        return "0.0.0.dev0"
