"""`highlights`: the words a search engine bolds in its snippets and the terms that shape the topic (LSI).

The request is Arsenkin's `sp` tool, so a task written for their API works here unchanged:

    {"tools_name": "sp", "data": {"queries": ["продвижение сайта"], "se": 1, "region": 213, "depth": 50}}

`se` is 1 for Yandex and 2 for Google; `depth` is 10, 20 or 50.

Two sources answer it:

- `serp` (the default) — our own stack: `serp snippets` for the results with their bolded words, and then the counting
  is ours. What we add on top of the contract: `--with questions,ads,ai` asks `serp related --kind question`,
  `serp ads` and `serp summary` in the same run.
- `arsenkin` — their `sp` task, `set` → `check` → `get`; the payload is cached in the shared database and comes back
  free with `--cache`.

The answer per query: `results` (position, URL, title and the bolded `highlights` of each), `highlights` (every bolded
phrase with how many results carry it) and `lsi` (the words and two-word terms of the titles and snippets, by the
number of results they appear in — the topic, not the query).
"""

# ruff: noqa: RUF001 — the Russian stopword list is Cyrillic on purpose, letter by letter.
from __future__ import annotations

import asyncio
import json
import re
import subprocess
import time
from collections import Counter
from collections.abc import Iterable, Mapping
from itertools import pairwise
from pathlib import Path

import httpx

from . import cache as cache_store
from . import tasks
from .credentials import binding
from .services import arsenkin

__all__ = ["build_report", "collect", "normalise", "report_from_remote"]

TOOLS_NAME = "sp"
SOURCES = ("serp", "arsenkin")
ENGINES = {1: "yandex", 2: "google"}
DEPTHS = (10, 20, 50)
EXTRAS = ("questions", "ads", "ai")
TIMEOUT = 600.0  # one `serp` call, seconds
SLEEP = 2.0  # seconds between polls inside `wait`
TOP_TERMS = 100  # how many LSI terms the answer keeps
# Words that say nothing about a topic: they are in every snippet and only hide the terms that do.
# A text block, not a list literal: the words are Cyrillic, and a literal list is what RUF001 cannot read.
STOPWORDS = frozenset(
    [
        "а",
        "без",
        "более",
        "бы",
        "был",
        "была",
        "были",
        "было",
        "быть",
        "в",
        "вам",
        "вас",
        "весь",
        "во",
        "вот",
        "все",
        "всего",
        "всех",
        "вы",
        "где",
        "да",
        "даже",
        "для",
        "до",
        "его",
        "ее",
        "если",
        "есть",
        "еще",
        "же",
        "за",
        "здесь",
        "и",
        "из",
        "или",
        "им",
        "их",
        "к",
        "как",
        "ко",
        "когда",
        "кто",
        "ли",
        "либо",
        "мне",
        "может",
        "мы",
        "на",
        "надо",
        "наш",
        "не",
        "него",
        "нее",
        "нет",
        "ни",
        "них",
        "но",
        "ну",
        "о",
        "об",
        "однако",
        "он",
        "она",
        "они",
        "оно",
        "от",
        "очень",
        "по",
        "под",
        "при",
        "с",
        "со",
        "так",
        "также",
        "такой",
        "там",
        "те",
        "тем",
        "то",
        "того",
        "тоже",
        "той",
        "только",
        "том",
        "ты",
        "у",
        "уже",
        "чего",
        "чей",
        "чем",
        "что",
        "чтобы",
        "чье",
        "чья",
        "эта",
        "эти",
        "это",
        "я",
        "the",
        "a",
        "an",
        "and",
        "or",
        "of",
        "to",
        "in",
        "for",
        "on",
        "with",
        "at",
        "by",
        "from",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "this",
        "that",
        "these",
        "those",
        "it",
        "its",
        "as",
        "not",
        "no",
        "you",
        "your",
        "we",
        "our",
    ]
)
WORD = re.compile(r"[a-zA-Zа-яА-ЯёЁ0-9]+")

# --- the request -------------------------------------------------------------------------------------------------


def normalise(request: Mapping) -> dict:
    """The `data` object of an `sp` request, from the full body or from the bare data; validated.

    Raises `ValueError` for what the documented request forbids: no queries, a search engine other than 1 or 2, a depth
    other than 10/20/50, no region. These are caller mistakes, so they surface before any paid call.
    """
    raw = request.get("data")
    data: Mapping = raw if isinstance(raw, Mapping) else request
    queries = list(dict.fromkeys(str(query).strip() for query in data.get("queries") or [] if str(query).strip()))
    if not queries:
        raise ValueError("no queries: 'queries' must be a non-empty list")
    se = _as_int(data.get("se"))
    if se not in ENGINES:
        raise ValueError(f"unknown search engine '{data.get('se')}': 1 is Yandex, 2 is Google")
    region = _as_int(data.get("region"))
    if region is None:
        raise ValueError("no region: 'region' is a Yandex `lr` code (213) or a Google location id (1011969)")
    depth = _as_int(data.get("depth")) or 10
    if depth not in DEPTHS:
        raise ValueError(f"depth must be one of {', '.join(str(value) for value in DEPTHS)}")
    extras = [str(name).lower() for name in data.get("with") or []]
    unknown = [name for name in extras if name not in EXTRAS]
    if unknown:
        raise ValueError(f"unknown block '{unknown[0]}'; known: {', '.join(EXTRAS)}")
    return {
        "queries": queries,
        "se": se,
        "region": region,
        "depth": depth,
        "with": [name for name in EXTRAS if name in extras],
    }


def _as_int(value: object) -> int | None:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _echo(data: Mapping) -> dict:
    return {
        "queries": list(data["queries"]),
        "se": data["se"],
        "region": data["region"],
        "depth": data["depth"],
    }


# --- our own source: serp snippets, and the counting is ours ------------------------------------------------------


def _serp(command: str, args: Iterable[str]) -> dict:
    """One `serp <command>` call; a refusal is a RuntimeError with the service's own words, never a parse error."""
    argv = ["serp", command, *args, "--json"]
    try:
        done = subprocess.run(  # noqa: S603 - the command is built here, not taken from input
            list(argv), capture_output=True, text=True, timeout=TIMEOUT, check=False
        )
    except FileNotFoundError as exc:
        raise RuntimeError(f"the serp CLI is not installed or not on PATH: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"serp did not answer in {TIMEOUT:g} s") from exc
    try:
        payload = json.loads(done.stdout)
    except ValueError:
        raise RuntimeError(f"serp answered not JSON (exit {done.returncode}): {done.stdout.strip()[:200]}") from None
    if payload.get("ok") is False:
        error = payload.get("error") or {}
        raise RuntimeError(f"serp: {error.get('code', 'ERROR')}: {error.get('message', 'no message')}")
    return payload


def _shape(args: list[str], *, engine: str, region: int) -> list[str]:
    """The common tail of every `serp` call: the engine and its region."""
    argv = [*args, "--engine", engine]
    argv += ["--yandex-lr", str(region)] if engine == "yandex" else ["--google-loc", str(region)]
    return argv


def terms(text: str) -> list[str]:
    """The words of a text that can say what the topic is: lowercase, no stopwords, no one-letter noise."""
    return [
        word
        for word in (match.group(0).lower() for match in WORD.finditer(text))
        if len(word) > 2 and word not in STOPWORDS
    ]


def lsi(documents: Iterable[str], *, top: int = TOP_TERMS) -> list[dict]:
    """The terms of the documents by how many of them carry each: the topic shared by the results.

    A word counts once per document and so does a two-word term, so a phrase repeated inside one snippet cannot outrank
    a term that every result uses. Rows: `term`, `documents`, `kind` (`word` or `phrase`), `share`, ordered by
    `documents`.
    """
    word_docs: Counter[str] = Counter()
    phrase_docs: Counter[str] = Counter()
    total = 0
    for text in documents:
        total += 1
        words = terms(text)
        word_docs.update(set(words))
        phrase_docs.update({" ".join(pair): 1 for pair in pairwise(words)})
    found: list[dict] = [{"term": term, "documents": count, "kind": "word"} for term, count in word_docs.items()] + [
        {"term": term, "documents": count, "kind": "phrase"} for term, count in phrase_docs.items()
    ]
    found.sort(key=lambda row: (-int(row["documents"]), str(row["kind"]), str(row["term"])))
    return [{**row, "share": round(int(row["documents"]) / total, 3) if total else 0.0} for row in found[:top]]


def collect_serp(data: Mapping, *, cache: bool = False, max_age: str | None = None) -> dict:
    """Our `serp` stack: the results with their bolded words, plus whatever `--with` asks for."""
    engine = ENGINES[data["se"]]
    shared = ["--cache"] if cache else []
    if cache and max_age:
        shared += ["--max-age", max_age]
    answers: dict[str, object] = {}
    warning = None
    try:
        call = _shape([*data["queries"], "--top", str(data["depth"])], engine=engine, region=data["region"])
        answers["snippets"] = _serp("snippets", [*call, *shared])
    except RuntimeError as exc:
        warning = str(exc)[:300]
    for block, command, extra in (
        # `serp related` returns both kinds in one answer, each row labelled: the questions are picked out here
        ("questions", "related", _shape([*data["queries"]], engine=engine, region=data["region"])),
        ("ads", "ads", _shape([*data["queries"]], engine="yandex", region=data["region"])),
        ("ai", "summary", _shape([*data["queries"]], engine=engine, region=data["region"])),
    ):
        if block not in data["with"]:
            continue
        try:
            answer = _serp(command, [*extra, *shared])
        except RuntimeError as exc:  # a block the provider does not answer is a note, not a failed task
            warning = warning or f"{block}: {exc}"[:300]
            continue
        if block == "questions":
            rows = answer.get("results") or []
            answer = {**answer, "results": [row for row in rows if row.get("kind") in (None, "question")]}
        answers[block] = answer
    return {"answers": answers, "warning": warning}


# --- the report -------------------------------------------------------------------------------------------------


def build_report(
    *,
    request: Mapping,
    source: str,
    results: Mapping[str, list[dict]],
    extras: Mapping[str, object] | None = None,
    warning: str | None = None,
    task_id: object = None,
) -> dict:
    """Per query: the results with their bolded words, the bolded phrases with their counts, and the LSI terms."""
    data = normalise(request)
    per_query = {}
    for query in data["queries"]:
        rows = results.get(query) or []
        bolds = Counter(word for row in rows for word in row.get("highlights") or [])
        documents = [f"{row.get('title') or ''} {row.get('snippet') or ''}" for row in rows]
        per_query[query] = {
            "results": rows,
            "highlights": [{"phrase": phrase, "results": count} for phrase, count in bolds.most_common(TOP_TERMS)],
            "lsi": lsi(documents),
        }
    report = {
        "ok": True,
        "source": source,
        "tools_name": TOOLS_NAME,
        **({"task_id": task_id} if task_id is not None else {}),
        "result": {"request": _echo(data), "result": per_query, **dict(extras or {})},
        "summary": {
            "queries": len(data["queries"]),
            "results": sum(len(rows) for rows in results.values()),
            "highlighted": sum(1 for rows in results.values() for row in rows if row.get("highlights")),
            "lsi_terms": sum(len(found["lsi"]) for found in per_query.values()),
        },
    }
    if warning:
        report["warning"] = warning
    return report


def report_from_remote(payload: dict, request: Mapping | None = None) -> dict:
    """Their `sp` answer → our report: their payload is kept under `result.remote`, our summary around it."""
    result = payload.get("result") or {}
    body = result.get("request") or request or {}
    rows: dict[str, list[dict]] = {}
    raw = result.get("result") or result.get("data") or {}
    if isinstance(raw, Mapping):
        for query, value in raw.items():
            if isinstance(value, list):
                rows[str(query)] = [
                    {**item, "highlights": item.get("highlights") or item.get("bold") or []}
                    for item in value
                    if isinstance(item, dict)
                ]
    report = build_report(request=body, source="arsenkin", results=rows, task_id=payload.get("task_id"))
    report["result"]["remote"] = result
    return report


# --- one call --------------------------------------------------------------------------------------------------


async def collect(
    request: Mapping,
    *,
    source: str = "serp",
    token: str | None = None,
    cache: bool = False,
    max_age: str | None = None,
    poll_interval: float = 15.0,
    timeout: float = 3600.0,
) -> dict:
    """Run one `sp` task through `source` and return the report."""
    if isinstance(request, Mapping) and "request" in request:  # a submitted job carries the task's envelope
        source = str(request.get("source") or source)
        request = request["request"]
    if source not in SOURCES:
        raise ValueError(f"unknown source '{source}'; known: {', '.join(SOURCES)}")
    data = normalise(request)
    started = time.monotonic()
    if source == "arsenkin":
        payload = cache_store.load(TOOLS_NAME, data, max_age=max_age) if cache else None
        cached = payload is not None
        if payload is None:
            payload = await _payload_from_arsenkin(data, token=token, poll_interval=poll_interval, timeout=timeout)
            cache_store.save(TOOLS_NAME, data, payload)
        report = report_from_remote(payload, data)
        if cached:
            report["cached"] = True
    else:
        found = await asyncio.to_thread(collect_serp, data, cache=cache, max_age=max_age)
        answers = found["answers"]
        extras = {block: answers[block] for block in EXTRAS if block in answers}
        report = build_report(
            request=data,
            source=source,
            results=_group(answers.get("snippets") or {}),
            extras=extras,
            warning=found["warning"],
        )
    report["elapsed_s"] = round(time.monotonic() - started, 1)
    return report


def _group(answer: Mapping) -> dict[str, list[dict]]:
    """The `serp snippets` rows by their query, in the order the engine returned them."""
    grouped: dict[str, list[dict]] = {}
    for row in answer.get("results") or []:
        grouped.setdefault(str(row.get("query")), []).append(
            {
                "position": row.get("serp_position"),
                "url": row.get("url"),
                "domain": row.get("domain"),
                "title": row.get("title"),
                "snippet": row.get("snippet"),
                "highlights": row.get("highlights") or [],
            }
        )
    return grouped


async def _payload_from_arsenkin(data: Mapping, *, token: str | None, poll_interval: float, timeout: float) -> dict:
    secret = token or binding("arsenkin")[0]
    if not secret:
        raise RuntimeError("no token for 'arsenkin': `token set arsenkin`")
    async with httpx.AsyncClient(timeout=60.0, trust_env=False) as client:
        return await arsenkin.collect_task(
            client, secret, "sp", dict(_echo(data)), poll_interval=poll_interval, timeout=timeout
        )


# --- a task that outlives the call -----------------------------------------------------------------------------

tasks.KINDS[TOOLS_NAME] = "rb_workflows.highlights:collect"
tasks.REPORTS[TOOLS_NAME] = report_from_remote


async def submit(
    request: Mapping,
    *,
    source: str = "serp",
    tasks_dir: Path | None = None,
    **kwargs: object,
) -> dict:
    """Place the work and answer at once: `task_id`, the state, and the call that reads the result later."""
    if isinstance(request, Mapping) and "request" in request:
        source = str(request.get("source") or source)
        request = request["request"]
    if source not in SOURCES:
        raise ValueError(f"unknown source '{source}'; known: {', '.join(SOURCES)}")
    data = normalise(request)
    store = "arsenkin" if source == "arsenkin" else "local"
    return await tasks.submit(
        TOOLS_NAME, {"request": data, **kwargs, "store": store}, store=tasks.default_store(tasks_dir)
    )


async def poll(task_id: str, *, tasks_dir: Path | None = None) -> dict:
    return await tasks.get(task_id, store=tasks.default_store(tasks_dir))


async def wait(task_id: str, *, tasks_dir: Path | None = None, poll_interval: float | None = None) -> dict:
    step = SLEEP if poll_interval is None else poll_interval
    while True:
        answer = await poll(task_id, tasks_dir=tasks_dir)
        if answer.get("state") in {"done", "error"}:
            return answer
        await asyncio.sleep(step)
