"""Every test gets a cache of its own.

`WORKFLOWS_DATABASE_URL` is set on a machine that shares one PostgreSQL cache between runs; a test must never write
there. The answer cache therefore becomes a SQLite file inside the test's `tmp_path`, fresh for each test.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _own_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKFLOWS_CACHE_PATH", str(tmp_path))
    monkeypatch.delenv("WORKFLOWS_DATABASE_URL", raising=False)
