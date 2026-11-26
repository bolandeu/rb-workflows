"""`wf cache`: what the answer cache holds and dropping what is old — the one thing that keeps Neon from growing.

Rules these tests pin:
- `info` answers with where the cache lives and how many answers it holds, and says nothing about secrets;
- `clear` drops only what is older than the term asked (or everything with `--all`), and only with `--confirm`;
- `--dry-run` counts and deletes nothing; exactly one of `--older-than` and `--all` is required.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from rb_workflows import cache, cli


def _store(kind: str = "probe", request: dict | None = None) -> None:
    cache.save(kind, request or {"q": 1}, {"ok": True})


def _make_old(days: float) -> None:
    """Age the stored rows: the cutoff is `fetched_at`, so a test walks the rows back in time instead of waiting."""
    with sqlite3.connect(cache.stats()["location"]) as base:
        base.execute(
            "update workflows_answers set fetched_at = ?", ((datetime.now(UTC) - timedelta(days=days)).isoformat(),)
        )


def test_info_counts_the_stored_answers_and_keeps_the_password_out(capsys) -> None:
    assert cli.main(["cache", "info", "--json"]) == 0
    empty = json.loads(capsys.readouterr().out)
    _store()

    assert cli.main(["cache", "info", "--json"]) == 0
    found = json.loads(capsys.readouterr().out)

    assert (empty["entries"], empty["oldest"]) == (0, None)
    assert found["ok"] is True and found["entries"] == 1
    assert found["size_bytes"] > 0 and found["storage"] == "sqlite"


def test_clear_needs_a_term_and_a_confirmation_and_a_dry_run_deletes_nothing(capsys) -> None:
    _store()
    _make_old(40)

    assert cli.main(["cache", "clear", "--older-than", "30d", "--dry-run", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["would_delete"] == 1
    with pytest.raises(SystemExit):
        cli.main(["cache", "clear", "--older-than", "30d", "--json"])
    assert json.loads(capsys.readouterr().out)["ok"] is False
    assert cache.stats()["entries"] == 1  # nothing went away without --confirm

    assert cli.main(["cache", "clear", "--older-than", "30d", "--confirm", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["deleted"] == 1
    assert cache.stats()["entries"] == 0


def test_a_young_answer_survives_and_all_drops_everything(capsys) -> None:
    _store("probe", {"q": 1})
    _store("probe", {"q": 2})
    _make_old(1)  # both are a day old, well inside the term

    assert cli.main(["cache", "clear", "--older-than", "30d", "--confirm", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["deleted"] == 0
    assert cache.stats()["entries"] == 2

    assert cli.main(["cache", "clear", "--all", "--confirm", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["deleted"] == 2
    assert cache.stats()["entries"] == 0


def test_both_terms_at_once_is_the_envelope(capsys) -> None:
    with pytest.raises(SystemExit) as stopped:
        cli.main(["cache", "clear", "--older-than", "30d", "--all", "--confirm", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert (stopped.value.code, payload["error"]["code"]) == (2, "INVALID_ARGUMENTS")
