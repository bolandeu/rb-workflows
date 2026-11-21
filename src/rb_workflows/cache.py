"""The answers of the external services, kept so a second run costs nothing.

A task at Arsenkin costs limits, so its `get` payload is stored as it came: the key is the tool and the request body
(exactly what decides the answer), the value the service's own reply. `--cache` reads that row instead of placing the
task again, and `--max-age` refuses a row that is older than asked.

Where the rows live:

- `WORKFLOWS_DATABASE_URL` set (PostgreSQL) — table `workflows_answers`, so every machine and the server share one
  cache. The table is created on first use; nothing else in the database is touched.
- the variable unset — a local SQLite file in the user cache dir, which is what tests and an offline laptop use.

Nothing here knows what an Arsenkin payload looks like: the module that owns a tool turns the payload into a report,
the cache only remembers it.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path

import platformdirs

__all__ = ["ENV_VAR", "key", "load", "save"]

APP = "rb-workflows"
ENV_VAR = "WORKFLOWS_DATABASE_URL"
PATH_ENV_VAR = "WORKFLOWS_CACHE_PATH"


def key(kind: str, request: Mapping) -> str:
    """The row key: the tool and its request, canonicalised, so a reordered JSON body is the same question."""
    body = json.dumps({"kind": kind, "request": request}, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def age(value: str) -> timedelta:
    """`7d`, `12h`, `30m`, `45s` — the same spellings the `--max-age` flags take."""
    text = str(value).strip().lower()
    units = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}
    if text and text[-1] in units:
        try:
            return timedelta(**{units[text[-1]]: float(text[:-1])})
        except ValueError:
            pass
    raise ValueError(f"--max-age expects a number with s, m, h or d, got '{value}'")


def _sqlite_path() -> Path:
    directory = Path(os.environ.get(PATH_ENV_VAR) or platformdirs.user_cache_path(APP, appauthor=False))
    directory.mkdir(parents=True, exist_ok=True)
    return directory / "answers.sqlite"


def save(kind: str, request: Mapping, payload: Mapping) -> bool:
    """Store the answer of one task; False when the cache could not be written (a cache must not fail a run)."""
    try:
        if url := os.environ.get(ENV_VAR):
            import psycopg  # imported here: a laptop without the driver still runs every other command

            with psycopg.connect(url) as connection, connection.cursor() as cursor:
                cursor.execute(
                    "create table if not exists public.workflows_answers (key text primary key, kind text not null, request jsonb not null, payload jsonb not null, fetched_at timestamptz not null default now())"
                )
                cursor.execute(
                    "insert into public.workflows_answers (key, kind, request, payload) values (%s, %s, %s, %s)"
                    " on conflict (key) do update set payload = excluded.payload, fetched_at = now()",
                    (key(kind, request), kind, json.dumps(request, default=str), json.dumps(payload, default=str)),
                )
            return True
        import sqlite3

        with sqlite3.connect(_sqlite_path()) as base:
            base.execute(
                "create table if not exists workflows_answers (key text primary key, kind text not null,"
                " request text not null, payload text not null, fetched_at text not null)"
            )
            base.execute(
                "insert into workflows_answers (key, kind, request, payload, fetched_at) values (?, ?, ?, ?, ?)"
                " on conflict (key) do update set payload = excluded.payload, fetched_at = excluded.fetched_at",
                (
                    key(kind, request),
                    kind,
                    json.dumps(request, default=str),
                    json.dumps(payload, default=str),
                    datetime.now(UTC).isoformat(),
                ),
            )
        return True
    except Exception:  # a broken cache is not a broken task: the caller just pays for the call again
        return False


def load(kind: str, request: Mapping, *, max_age: str | None = None) -> dict | None:
    """The stored answer for this exact request, or None when there is none (or it is older than `max_age`)."""
    limit = age(max_age) if max_age else None
    try:
        if url := os.environ.get(ENV_VAR):
            import psycopg

            with psycopg.connect(url) as connection, connection.cursor() as cursor:
                cursor.execute(
                    "select payload, fetched_at from public.workflows_answers where key = %s", (key(kind, request),)
                )
                row = cursor.fetchone()
            if row is None:
                return None
            payload, fetched_at = row
            if limit is not None and datetime.now(UTC) - fetched_at > limit:
                return None
            return payload if isinstance(payload, dict) else json.loads(payload)
        import sqlite3

        path = _sqlite_path()
        if not path.is_file():
            return None
        with sqlite3.connect(path) as base:
            base.row_factory = sqlite3.Row
            row = base.execute(
                "select payload, fetched_at from workflows_answers where key = ?", (key(kind, request),)
            ).fetchone()
        if row is None:
            return None
        if limit is not None:
            fetched_at = datetime.fromisoformat(str(row["fetched_at"]))
            if datetime.now(UTC) - fetched_at > limit:
                return None
        return json.loads(row["payload"])
    except Exception:  # an unreadable cache is a miss, never an error for the caller
        return None
