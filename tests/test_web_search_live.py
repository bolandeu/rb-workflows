"""Live `web-search`: the real `serp` CLI and the real trafilatura. Run with `pytest -m live` — it spends money."""

from __future__ import annotations

import shutil
from collections.abc import Awaitable, Callable
from typing import cast

import pytest

from rb_workflows.flows import web_search

pytestmark = pytest.mark.live

# `@task` wraps the function, so the typed way in is the shape the workflow itself sees.
SERP: Callable[..., Awaitable[dict]] = web_search.SOURCES["serp"]
MAIN_TEXT = cast("Callable[..., Awaitable[dict]]", web_search._main_text)

needs_serp = pytest.mark.skipif(shutil.which("serp") is None, reason="the serp CLI is not installed")


@needs_serp
async def test_the_serp_source_answers_with_positions_and_snippets() -> None:
    found = await SERP("требования к метатегу title", top=10)
    assert found["ok"], found["reason"]
    assert found["results"][0]["position"] == 1
    assert found["results"][0]["domain"]
    assert any(row["snippet"] for row in found["results"]), "the snippets block of the answer is empty"
    assert found["cost_currency"] == "RUB"  # the CLI pays its own providers, in rubles


async def test_the_main_text_of_a_real_page_is_the_article() -> None:
    found = await MAIN_TEXT("https://ru.wikipedia.org/wiki/HTTP", proxy=None)
    assert found["ok"], found["reason"]
    assert found["via"] == "trafilatura"
    assert len(found["text"]) > 500
    assert "<script" not in found["text"]  # the page's own skin and scripts are gone
    assert "Перейти к содержимому" not in found["text"]  # the menu of the skin, not the article


@needs_serp
async def test_the_whole_workflow_end_to_end() -> None:
    result = await web_search.web_search(["требования к метатегу title"], top=10, fetch_top=1)
    entry = result["queries"][0]
    assert result["ok"] and entry["source"] == "serp"
    assert len(entry["results"]) >= 5
    documents = entry.get("documents") or []
    assert documents and documents[0]["ok"], documents


@needs_serp
def test_the_command_end_to_end(capsys, monkeypatch: pytest.MonkeyPatch) -> None:
    from rb_workflows import cli

    monkeypatch.setitem(cli.WORKFLOWS, "web-search", web_search.web_search)
    code = cli.main(["web-search", "--queries", "требования к метатегу title", "--json"])
    assert code == 0
    assert '"source": "serp"' in capsys.readouterr().out
