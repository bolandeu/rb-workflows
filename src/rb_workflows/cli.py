"""Local entry point: run a workflow here, with the same code that would deploy to Windmill.

The output is JSON on stdout (the contract), and a short human summary when `--json` is absent.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import NoReturn

from .flows.web_fetch import web_fetch
from .flows.web_search import web_search
from .version import version

__all__ = ["main", "run"]

WORKFLOWS = {"web-fetch": web_fetch, "web-search": web_search}


def run(name: str, payload: dict) -> dict:
    """Run one workflow with `payload` as its keyword input, synchronously."""
    workflow = WORKFLOWS.get(name)
    if workflow is None:
        raise SystemExit(f"unknown workflow '{name}'; known: {', '.join(WORKFLOWS)}")
    return asyncio.run(workflow(**payload))


class _Parser(argparse.ArgumentParser):
    """The Standard's error envelope: an unknown option prints JSON with `ok: false` and exits 2."""

    def error(self, message: str) -> NoReturn:
        print(json.dumps({"ok": False, "error": {"code": "INVALID_ARGUMENTS", "message": message}}, ensure_ascii=False))
        raise SystemExit(2)


def _add_fetch_args(cmd: argparse.ArgumentParser) -> None:
    """The input of `web-fetch`."""
    cmd.add_argument("--input", help='JSON object with the workflow input, e.g. {"urls": ["https://example.com"]}')
    cmd.add_argument("--urls", nargs="+", help="URLs to fetch (a shortcut for --input)")
    cmd.add_argument("--budget-usd", type=float, help="spend ceiling for the whole run")
    cmd.add_argument("--proxy", help="proxy for every step (overrides the key's own binding)")
    cmd.add_argument("--steps", nargs="+", help="limit the cascade, e.g. --steps exa direct")
    cmd.add_argument("--json", action="store_true", help="print the result as JSON (the contract)")


def _add_search_args(cmd: argparse.ArgumentParser) -> None:
    """The input of `web-search`: the sources, the mode, and whether to bring the pages' text back."""
    cmd.add_argument("--input", help='JSON object with the workflow input, e.g. {"queries": ["your topic"]}')
    cmd.add_argument("--queries", nargs="+", help="what to look up (a shortcut for --input)")
    cmd.add_argument(
        "--mode",
        choices=("cascade", "parallel"),
        help="cascade (default): the first source with results wins; parallel: ask all and merge by URL",
    )
    cmd.add_argument("--sources", help="which sources to ask, in order: serp, exa, tavily (default: all three)")
    cmd.add_argument("--top", type=int, help="results per source per query (default: 10)")
    cmd.add_argument("--engine", choices=("yandex", "google", "both"), help="the engine of serp (default: yandex)")
    cmd.add_argument("--fetch-top", type=int, help="also bring back the main text of the first N pages")
    cmd.add_argument("--budget-usd", type=float, help="spend ceiling over the USD the sources charge")
    cmd.add_argument("--proxy", help="proxy for every call (overrides the key's own binding)")
    cmd.add_argument("--json", action="store_true", help="the report as JSON (the contract)")


WORKFLOW_ARGS = {"web-fetch": _add_fetch_args, "web-search": _add_search_args}
# One line each in `wf --help` and in `describe`.
WORKFLOW_HELP = {
    "web-fetch": "fetch pages through the waterfall of services",
    "web-search": "information on a topic from several search sources",
}


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="wf", description="Run a workflow locally (Windmill-as-code).")
    parser.add_argument("--json", action="store_true", help="print the result as JSON (the contract)")
    parser.add_argument("--version", action="version", version=version())
    sub = parser.add_subparsers(dest="command")  # no command: the Standard wants the list and exit 0
    contract = sub.add_parser("describe", help="machine-readable contract (OpenCLI): the workflows and their input")
    contract.add_argument("--json", action="store_true", help="the contract (always JSON)")
    doctor = sub.add_parser("doctor", help="readiness of this machine: keys, proxies, the workflow engine")
    doctor.add_argument("--json", action="store_true", help="the report as JSON (the contract)")
    auth = sub.add_parser("auth", help="credentials of the services", add_help=False)  # its own help: a group's shape
    auth.add_argument("--help", "-h", action="store_true", help="list the auth commands")
    auth_status = auth.add_subparsers(dest="auth_command")
    status_parser = auth_status.add_parser("status", help="where every service's key comes from (never the key)")
    status_parser.add_argument("--json", action="store_true", help="the report as JSON (the contract)")
    sem = sub.add_parser("semantics", help="a keyword set for one URL or a batch, from an external service")
    sem.add_argument("--urls", nargs="+", help="URLs to collect for (each one is a task of the service)")
    sem.add_argument(
        "--input",
        help="JSON file with the semantics body (or the bare data). Required keys: queries; "
        "optional: mode (url), region (msk), stopwords ([]), concurrency (5), timeout (1800), poll_interval (15)",
    )
    sem.add_argument("--engine", default="msk", choices=("msk", "gru"), help="msk — Yandex Moscow, gru — Google Moscow")
    sem.add_argument("--service", default="arsenkin", help="which service answers (default: arsenkin)")
    sem.add_argument("--stopwords", help="comma-separated minus-words")
    sem.add_argument("--concurrency", type=int, default=None, help="tasks in flight at once (the account allows 5)")
    sem.add_argument("--timeout", type=float, default=1800.0, help="seconds a single task may take")
    sem.add_argument("--poll-interval", type=float, default=15.0, help="seconds between status checks")
    sem.add_argument("--cache", action="store_true", help="answer from the stored task payload when there is one")
    sem.add_argument("--max-age", help="with --cache: the oldest stored answer to accept, e.g. 7d")
    sem.add_argument("--submit", action="store_true", help="place the job and answer with its id at once")
    sem.add_argument("--tasks-dir", help="where the task files live (default: the user state dir)")
    sem.add_argument("--json", action="store_true", help="the report as JSON (the contract)")
    tasks_cmd = sub.add_parser("task", help="a submitted job: its state, or the report once it is done")
    tasks_cmd.add_argument("id", nargs="?", metavar="ID", help="the task id: `local:…` or `arsenkin:…`")
    tasks_cmd.add_argument("--wait", action="store_true", help="block until the job finishes")
    tasks_cmd.add_argument("--cancel", action="store_true", help="stop the job and its worker")
    tasks_cmd.add_argument("--gc", action="store_true", help="remove finished local tasks older than --older-than")
    tasks_cmd.add_argument("--older-than", type=float, default=7.0, metavar="DAYS", help="with --gc: the age to keep")
    tasks_cmd.add_argument("--run", metavar="ID", help=argparse.SUPPRESS)  # the detached worker of --submit
    tasks_cmd.add_argument("--tasks-dir", help="where the task files live (default: the user state dir)")
    tasks_cmd.add_argument("--json", action="store_true", help="the report as JSON (the contract)")
    top_cmd = sub.add_parser("top", help="positions for a batch of queries, in the check-top shape")
    top_cmd.add_argument(
        "--input",
        help="JSON file with the check-top body (or the bare data). Required keys: queries, se; "
        "optional: depth (10), is_snippet (false), noreask (false). Flags below build the same body",
    )
    top_cmd.add_argument("--queries", nargs="+", help="queries to check (instead of --input)")
    top_cmd.add_argument(
        "--se",
        help="search engines as type:region, e.g. 1:213,11:1011969 — 1/2/3 Yandex, 11/12 Google, 20/21 YouTube",
    )
    top_cmd.add_argument("--depth", type=int, default=10, help="results per query (5, 10, 20, 30, 50 or 100)")
    top_cmd.add_argument("--is-snippet", action="store_true", help="collect snippets from the results")
    top_cmd.add_argument("--noreask", action="store_true", help="do not let the engine rewrite the query")
    top_cmd.add_argument("--submit", action="store_true", help="place the task and answer with its id at once")
    top_cmd.add_argument("--task", metavar="ID", help="read a submitted task: its state, or the report when it is done")
    top_cmd.add_argument("--wait", action="store_true", help="with --task: block until the task finishes")
    top_cmd.add_argument("--run", metavar="ID", help=argparse.SUPPRESS)  # the detached worker of --submit
    top_cmd.add_argument("--tasks-dir", help="where the task files live (default: the user state dir)")
    top_cmd.add_argument("--source", default="serp", choices=("serp", "arsenkin"), help="who answers (default: serp)")
    top_cmd.add_argument("--provider", help="with --source serp: the provider queue, e.g. xmlriver,xmlstock")
    top_cmd.add_argument("--cache", action="store_true", help="with --source serp: take the answer from the journal")
    top_cmd.add_argument("--max-age", help="with --cache: the oldest journal answer to accept, e.g. 7d")
    top_cmd.add_argument("--top-n", type=int, default=10, help="rows of the strength summary to show")
    top_cmd.add_argument("--concurrency", type=int, default=3, help="queries in flight at once (source=serp)")
    top_cmd.add_argument(
        "--timeout", type=float, default=3600.0, help="seconds the whole task may take (source=arsenkin)"
    )
    top_cmd.add_argument(
        "--poll-interval", type=float, default=15.0, help="seconds between status checks (source=arsenkin)"
    )
    top_cmd.add_argument("--json", action="store_true", help="the report as JSON (the contract)")
    cluster = sub.add_parser("clustering", help="keywords grouped by the URLs they share, in the clustering shape")
    cluster.add_argument(
        "--input",
        help="JSON file with the clustering body (or the bare data). Required keys: queries, se, region; optional: "
        "group (soft), count (3), depth (10), stoplist ([]), ws ([]), main (false). Flags below build the same body",
    )
    cluster.add_argument("--queries", nargs="+", help="phrases to group (instead of --input)")
    cluster.add_argument("--se", type=int, choices=(1, 2), help="1 Yandex, 2 Google")
    cluster.add_argument("--region", help="Yandex region code (213) or Google location id (1011969)")
    cluster.add_argument("--group", choices=("hard", "soft"), help="how strictly phrases are merged (default: soft)")
    cluster.add_argument("--count", type=int, help="degree of grouping, 2..10 (default: 3)")
    cluster.add_argument("--depth", type=int, help="results per query: 10, 20 or 30 (default: 10)")
    cluster.add_argument("--stoplist", help="comma-separated domains to drop, e.g. auto.ru,yandex.ru")
    cluster.add_argument("--ws", help="frequencies to collect: base, quoted, overal, exact")
    cluster.add_argument("--main", action="store_true", help="drop the main pages of the sites")
    cluster.add_argument("--source", default="serp", choices=("serp", "arsenkin"), help="who answers (default: serp)")
    cluster.add_argument("--provider", help="with --source serp: the serp provider queue, e.g. xmlriver,xmlstock")
    cluster.add_argument(
        "--cache", action="store_true", help="with --source serp: take the serp answer from the journal"
    )
    cluster.add_argument("--max-age", help="with --cache: the oldest serp answer to accept, e.g. 7d")
    cluster.add_argument("--concurrency", type=int, default=3, help="queries in flight at once (source=serp)")
    cluster.add_argument(
        "--timeout", type=float, default=3600.0, help="seconds the whole task may take (source=arsenkin)"
    )
    cluster.add_argument(
        "--poll-interval", type=float, default=15.0, help="seconds between status checks (source=arsenkin)"
    )
    cluster.add_argument("--submit", action="store_true", help="place the task and answer with its id at once")
    cluster.add_argument("--task", metavar="ID", help="read a submitted task: its state, or the report when it is done")
    cluster.add_argument("--wait", action="store_true", help="with --task: block until the task finishes")
    cluster.add_argument("--run", metavar="ID", help=argparse.SUPPRESS)  # the detached worker of --submit
    cluster.add_argument("--tasks-dir", help="where the task files live (default: the user state dir)")
    cluster.add_argument("--json", action="store_true", help="the report as JSON (the contract)")
    word = sub.add_parser("wordstat", help="Wordstat: type 1 collects frequencies, type 2 parses phrases")
    word.add_argument(
        "--input",
        help="JSON file with the wordstat body (or the bare data). Required keys: queries and regions (type 1) or "
        "region (type 2); optional: type (1), ws ([base]), device (''), minus_words, is_clear_minus, is_right, is_clear",
    )
    word.add_argument("--type", type=int, choices=(1, 2), help="1 frequencies, 2 phrase parsing (default: 1)")
    word.add_argument("--queries", nargs="+", help="phrases to collect (instead of --input)")
    word.add_argument("--regions", help="with --type 1: comma-separated Yandex `lr` codes, at most 4, e.g. 213,225")
    word.add_argument("--region", help="with --type 2: one Yandex `lr` code, e.g. 213")
    word.add_argument("--ws", help="with --type 1: frequencies to collect — base, quoted, overal, exact")
    word.add_argument("--device", help="desktop, mobile, phone, tablet; empty means all devices")
    word.add_argument("--limit", help="with --source wordstat, type 2: how many similar phrases to ask (1..2000)")
    word.add_argument("--minus-words", help="with --type 2: comma-separated minus-words")
    word.add_argument(
        "--is-clear-minus",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="with --type 2: drop phrases with minus-words (default: true)",
    )
    word.add_argument(
        "--is-right",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="with --type 2: collect the right column of Wordstat (default: true)",
    )
    word.add_argument(
        "--is-clear",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="with --type 2: drop the '+' from the phrases (default: true)",
    )
    word.add_argument(
        "--source", default="wordstat", choices=("wordstat", "arsenkin"), help="who answers (default: our CLI)"
    )
    word.add_argument("--provider", help="with --source wordstat: the provider queue, e.g. xmlriver,xmlstock")
    word.add_argument("--cache", action="store_true", help="with --source wordstat: take the answer from the journal")
    word.add_argument("--max-age", help="with --cache: the oldest journal answer to accept, e.g. 7d")
    word.add_argument("--concurrency", type=int, default=5, help="queries in flight at once (source=wordstat, type 2)")
    word.add_argument("--timeout", type=float, default=3600.0, help="seconds the whole task may take (source=arsenkin)")
    word.add_argument(
        "--poll-interval", type=float, default=15.0, help="seconds between status checks (source=arsenkin)"
    )
    word.add_argument("--submit", action="store_true", help="place the task and answer with its id at once")
    word.add_argument("--task", metavar="ID", help="read a submitted task: its state, or the report when it is done")
    word.add_argument("--wait", action="store_true", help="with --task: block until the task finishes")
    word.add_argument("--run", metavar="ID", help=argparse.SUPPRESS)  # the detached worker of --submit
    word.add_argument("--tasks-dir", help="where the task files live (default: the user state dir)")
    word.add_argument("--json", action="store_true", help="the report as JSON (the contract)")
    high = sub.add_parser("highlights", help="bolded words of the snippets and the topic terms (LSI), in the sp shape")
    high.add_argument(
        "--input",
        help="JSON file with the sp body (or the bare data). Required keys: queries, se, region; optional: "
        "depth (10, 20 or 50) and with (questions, ads, ai). Flags below build the same body",
    )
    high.add_argument("--queries", nargs="+", help="phrases to look at (instead of --input)")
    high.add_argument("--se", type=int, choices=(1, 2), help="1 Yandex, 2 Google")
    high.add_argument("--region", help="Yandex `lr` code (213) or Google location id (1011969)")
    high.add_argument("--depth", type=int, choices=(10, 20, 50), help="results per query (default: 10)")
    high.add_argument("--with", dest="with_blocks", help="extra blocks: questions, ads, ai")
    high.add_argument("--source", default="serp", choices=("serp", "arsenkin"), help="who answers (default: serp)")
    high.add_argument("--cache", action="store_true", help="take the answer from the journal or the stored task")
    high.add_argument("--max-age", help="with --cache: the oldest stored answer to accept, e.g. 7d")
    high.add_argument("--timeout", type=float, default=3600.0, help="seconds the whole task may take (source=arsenkin)")
    high.add_argument(
        "--poll-interval", type=float, default=15.0, help="seconds between status checks (source=arsenkin)"
    )
    high.add_argument("--submit", action="store_true", help="place the task and answer with its id at once")
    high.add_argument("--task", metavar="ID", help="read a submitted task: its state, or the report when it is done")
    high.add_argument("--wait", action="store_true", help="with --task: block until the task finishes")
    high.add_argument("--run", metavar="ID", help=argparse.SUPPRESS)  # the detached worker of --submit
    high.add_argument("--tasks-dir", help="where the task files live (default: the user state dir)")
    high.add_argument("--json", action="store_true", help="the report as JSON (the contract)")
    for name, add in WORKFLOW_ARGS.items():  # every workflow is a command of its own, next to the generic `run`
        add(sub.add_parser(name, help=WORKFLOW_HELP[name]))
    go = sub.add_parser("run", help="run one workflow: its input as one JSON object, the result as JSON")
    go.add_argument("workflow", choices=sorted(WORKFLOWS))
    go.add_argument("--input", help='JSON object with the workflow input, e.g. {"urls": ["https://example.com"]}')
    go.add_argument("--json", action="store_true", help="the result as JSON (always JSON)")
    return parser


BARE_HELP = """Usage: wf COMMAND

Commands:
  run       run one workflow
  semantics  a keyword set for one URL or a batch, from an external service
  top        positions for a batch of queries, in the check-top shape
  clustering keywords grouped by the URLs they share, in the clustering shape
  wordstat  Wordstat: type 1 frequencies, type 2 phrase parsing
  highlights bolded words of the snippets and the topic terms (LSI), in the sp shape
  web-fetch fetch pages through the waterfall of services
  web-search information on a topic from several search sources
  task       a submitted job: its state, or the report once it is done
  doctor    readiness of this machine: keys, proxies, the workflow engine
  auth      credentials of the services
  describe  this contract
"""


def _param(name: str, kind: str, default: object = None, *, multiple: bool = False) -> dict:
    """One `describe` entry: the name, its type, its default and — only when true — its repeatability."""
    metadata = [{"name": "x-rb-type", "value": kind}, {"name": "x-rb-default", "value": default}]
    if multiple:
        metadata.append({"name": "x-rb-multiple", "value": True})
    return {"name": name, "metadata": metadata}


_JSON_OPTION = _param("--json", "boolean", False)

# The options of every workflow, shared by the workflow command and by the generic `run`.
_FETCH_OPTIONS = [
    _param("--input", "path"),
    _param("--urls", "str", multiple=True),
    _param("--budget-usd", "float"),
    _param("--proxy", "str"),
    _param("--steps", "str", multiple=True),
    _JSON_OPTION,
]

DESCRIBE = {
    "ok": True,
    "opencli": "0.1",
    "info": {
        "title": "wf",
        "summary": "Workflows as code: run locally, deploy to Windmill unchanged",
    },
    "command": {
        "name": "wf",
        "options": [
            {
                "name": "--version",
                "metadata": [{"name": "x-rb-type", "value": "boolean"}, {"name": "x-rb-default", "value": False}],
            },
            _JSON_OPTION,
        ],
        "commands": [
            {
                "name": "run",
                "description": "run one workflow: its input as one JSON object, the result as JSON",
                "arguments": [_param("workflow", "choice")],
                "options": [_param("--input", "path"), _JSON_OPTION],
            },
            {
                "name": "web-fetch",
                "description": "fetch pages through the waterfall of services, under a spend ceiling",
                "options": _FETCH_OPTIONS,
            },
            {
                "name": "web-search",
                "description": "information on a topic from several search sources: mode (cascade), sources "
                "(serp, exa, tavily), top (10), engine (yandex), fetch_top (0) — the pages' text through trafilatura",
                "options": [
                    _param("--input", "path"),
                    _param("--queries", "str", multiple=True),
                    _param("--mode", "choice", "cascade"),
                    _param("--sources", "str", "serp,exa,tavily"),
                    _param("--top", "int", 10),
                    _param("--engine", "choice", "yandex"),
                    _param("--fetch-top", "int", 0),
                    _param("--budget-usd", "float"),
                    _param("--proxy", "str"),
                    _JSON_OPTION,
                ],
            },
            {
                "name": "semantics",
                "description": "a keyword set for one URL or a batch. Input required: queries (or urls); "
                "optional: mode (url), region (msk), stopwords ([]), concurrency (5) — as a JSON file or as flags",
                "options": [
                    _param("--urls", "str", multiple=True),
                    _param("--input", "path"),
                    _param("--engine", "choice", "msk"),
                    _param("--service", "str", "arsenkin"),
                    _param("--stopwords", "str"),
                    _param("--cache", "boolean", False),
                    _param("--max-age", "str"),
                    _param("--concurrency", "int"),
                    _param("--timeout", "float", 1800.0),
                    _param("--poll-interval", "float", 15.0),
                    _JSON_OPTION,
                ],
            },
            {
                "name": "task",
                "description": "a submitted job: its state, or the report once it is done",
                "arguments": [_param("id", "str")],
                "options": [
                    _param("--wait", "boolean", False),
                    _param("--cancel", "boolean", False),
                    _param("--gc", "boolean", False),
                    _param("--older-than", "float", 7.0),
                    _JSON_OPTION,
                ],
            },
            {
                "name": "top",
                "description": "positions in the check-top shape. Input required: queries, se; "
                "optional: depth (10), is_snippet (false), noreask (false) — as a JSON file or as flags",
                "options": [
                    _param("--input", "path"),
                    _param("--queries", "str", multiple=True),
                    _param("--se", "str"),
                    _param("--depth", "int", 10),
                    _param("--is-snippet", "boolean", False),
                    _param("--noreask", "boolean", False),
                    _param("--source", "choice", "serp"),
                    _param("--provider", "str"),
                    _param("--cache", "boolean", False),
                    _param("--max-age", "str"),
                    _param("--top-n", "int", 10),
                    _param("--concurrency", "int", 3),
                    _param("--timeout", "float", 3600.0),
                    _param("--poll-interval", "float", 15.0),
                    _JSON_OPTION,
                ],
            },
            {
                "name": "clustering",
                "description": "keywords grouped by the URLs they share, in the clustering shape. Input required: "
                "queries, se (1 Yandex, 2 Google), region; optional: group (soft), count (3), depth (10), "
                "stoplist ([]), ws ([]), main (false) — as a JSON file or as flags",
                "options": [
                    _param("--input", "path"),
                    _param("--queries", "str", multiple=True),
                    _param("--se", "int", 1),
                    _param("--region", "str"),
                    _param("--group", "choice", "soft"),
                    _param("--count", "int", 3),
                    _param("--depth", "int", 10),
                    _param("--stoplist", "str"),
                    _param("--ws", "str"),
                    _param("--main", "boolean", False),
                    _param("--source", "choice", "serp"),
                    _param("--provider", "str"),
                    _param("--cache", "boolean", False),
                    _param("--max-age", "str"),
                    _param("--concurrency", "int", 3),
                    _param("--timeout", "float", 3600.0),
                    _param("--poll-interval", "float", 15.0),
                    _JSON_OPTION,
                ],
            },
            {
                "name": "wordstat",
                "description": "Wordstat: type 1 collects frequencies (queries, regions, ws), type 2 "
                "parses phrases (queries, region, minus_words, is_right) — as a JSON file or as flags",
                "options": [
                    _param("--input", "path"),
                    _param("--type", "int", 1),
                    _param("--queries", "str", multiple=True),
                    _param("--regions", "str"),
                    _param("--region", "str"),
                    _param("--ws", "str"),
                    _param("--device", "str"),
                    _param("--limit", "str"),
                    _param("--minus-words", "str"),
                    _param("--is-clear-minus", "boolean", True),
                    _param("--is-right", "boolean", True),
                    _param("--is-clear", "boolean", True),
                    _param("--source", "choice", "wordstat"),
                    _param("--provider", "str"),
                    _param("--cache", "boolean", False),
                    _param("--max-age", "str"),
                    _param("--concurrency", "int", 5),
                    _param("--timeout", "float", 3600.0),
                    _param("--poll-interval", "float", 15.0),
                    _JSON_OPTION,
                ],
            },
            {
                "name": "highlights",
                "description": "bolded words of the snippets and the topic terms (LSI), in the sp shape. Input "
                "required: queries, se (1 Yandex, 2 Google), region; optional: depth (10, 20 or 50) and with "
                "(questions, ads, ai) — as a JSON file or as flags",
                "options": [
                    _param("--input", "path"),
                    _param("--queries", "str", multiple=True),
                    _param("--se", "int", 1),
                    _param("--region", "str"),
                    _param("--depth", "int", 10),
                    _param("--with", "str"),
                    _param("--source", "choice", "serp"),
                    _param("--cache", "boolean", False),
                    _param("--max-age", "str"),
                    _param("--timeout", "float", 3600.0),
                    _param("--poll-interval", "float", 15.0),
                    _JSON_OPTION,
                ],
            },
            {
                "name": "doctor",
                "description": "readiness of this machine: keys, proxies, the workflow engine",
                "options": [_JSON_OPTION],
            },
            {
                "name": "auth",
                "description": "credentials of the services",
                "options": [
                    {
                        "name": "--help",
                        "metadata": [
                            {"name": "x-rb-type", "value": "boolean"},
                            {"name": "x-rb-default", "value": False},
                        ],
                    }
                ],
                "commands": [
                    {
                        "name": "status",
                        "description": "where every service's key comes from",
                        "options": [_JSON_OPTION],
                    },
                ],
            },
            {"name": "describe", "description": "this contract", "options": [_JSON_OPTION]},
        ],
    },
}


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.command is None:
        print(BARE_HELP, end="")  # the Standard's shape: a Usage line with COMMAND, then a Commands section
        return 0
    if args.command == "describe":
        print(json.dumps(DESCRIBE, ensure_ascii=False))
        return 0
    if args.command in WORKFLOWS:
        return _workflow(args.command, args)
    if args.command == "semantics":
        return _semantics(args)
    if args.command == "top":
        return _top(args)
    if args.command == "clustering":
        return _clustering(args)
    if args.command == "wordstat":
        return _wordstat(args)
    if args.command == "highlights":
        return _highlights(args)
    if args.command == "task":
        return _tasks(args)
    if args.command == "doctor":
        return _doctor()
    if args.command == "auth":
        if getattr(args, "help", False):
            print(BARE_AUTH, end="")
            return 0
        if args.auth_command != "status":
            print(BARE_AUTH, end="")
            return 0
        return _auth_status()
    return _run_generic(args)


def _workflow(name: str, args: argparse.Namespace) -> int:
    """The command of one workflow: the flags of its own parser, or `--input`, and a human summary unless `--json`."""
    return _fetch_cmd(args) if name == "web-fetch" else _search_cmd(args)


def _invalid(message: str) -> NoReturn:
    """A caller mistake: the Standard's envelope on stdout, exit 2."""
    print(json.dumps({"ok": False, "error": {"code": "INVALID_ARGUMENTS", "message": message}}, ensure_ascii=False))
    raise SystemExit(2)


def _payload(args: argparse.Namespace) -> dict:
    """`--input` as a JSON object; broken JSON is the caller's mistake, said as the Standard's envelope."""
    if not args.input:
        return {}
    try:
        payload = json.loads(args.input)
    except ValueError as exc:
        print(
            json.dumps(
                {"ok": False, "error": {"code": "INVALID_ARGUMENTS", "message": f"--input: {exc}"}},
                ensure_ascii=False,
            )
        )
        raise SystemExit(2) from None
    if not isinstance(payload, dict):
        raise SystemExit("--input must be a JSON object")
    return payload


def _run_generic(args: argparse.Namespace) -> int:
    """`run <workflow>`: the input as one JSON object, the result as JSON — the machine entry of any workflow."""
    result = run(args.workflow, _payload(args))
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["ok"] else 1


def _search_cmd(args: argparse.Namespace) -> int:
    """`web-search`: the flags of `_add_search_args` build the payload, `--input` merges over them."""
    payload: dict = _payload(args)
    flags = {
        "queries": args.queries,
        "mode": args.mode,
        "top": args.top,
        "engine": args.engine,
        "fetch_top": args.fetch_top,
        "budget_usd": args.budget_usd,
        "proxy": args.proxy,
    }
    payload |= {key: value for key, value in flags.items() if value is not None}
    if args.sources:
        payload["sources"] = [name.strip() for name in args.sources.split(",") if name.strip()]
    if not payload.get("queries"):
        _invalid("nothing to look up: pass --queries or --input '{\"queries\": [...]}'")

    result = run("web-search", payload)
    if args.json:
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result["ok"] else 1
    for entry in result["queries"]:
        print(f"{entry['query']}: {len(entry['results'])} results from {entry.get('source') or 'no source'}")
        for row in entry["results"][:10]:
            print(f"  {row['position'] or '-'}  {row['url']}")
        for doc in entry.get("documents") or []:
            print(f"  text {len(doc['text'] or '')} chars ({doc['source']}, {doc['via'] or '-'}): {doc['url']}")
    print(f"spent: ${result['budget_spent_usd']:.4f}", file=sys.stderr)
    return 0 if result["ok"] else 1


def _fetch_cmd(args: argparse.Namespace) -> int:
    """`web-fetch`: the flags of `_add_fetch_args` build the payload, `--input` merges over them."""
    payload: dict = _payload(args)
    imports = {"urls": args.urls, "budget_usd": args.budget_usd, "proxy": args.proxy, "steps": args.steps}
    payload |= {key: value for key, value in imports.items() if value is not None}
    if "urls" not in payload:
        _invalid("nothing to fetch: pass --urls or --input '{\"urls\": [...]}'")

    result = run("web-fetch", payload)
    if args.json:
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result["ok"] else 1
    for doc in result["documents"]:
        status = "ok" if doc["ok"] else f"{doc['reason']}"
        print(f"{doc['url']} — {status} ({doc['source']}, {len(doc['text'])} chars, ${doc['cost_usd'] or 0:.4f})")
    print(f"spent: ${result['budget_spent_usd']:.4f}", file=sys.stderr)
    return 0 if result["ok"] else 1


def _read_json(path: Path) -> dict:
    """A request file; a missing or broken one is the caller's mistake, said as the Standard's envelope."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(
            json.dumps(
                {"ok": False, "error": {"code": "INVALID_ARGUMENTS", "message": f"{path}: {exc}"}},
                ensure_ascii=False,
            )
        )
        raise SystemExit(2) from None


def _check_top_body(args: argparse.Namespace) -> dict:
    """`--queries … --se 1:213,11:1011969 --depth 10` → the check-top body."""
    se = []
    for part in (args.se or "1:213").split(","):
        kind, _, region = part.strip().partition(":")
        try:
            se.append({"type": int(kind), "region": int(region)})
        except ValueError:
            print(
                json.dumps(
                    {
                        "ok": False,
                        "error": {"code": "INVALID_ARGUMENTS", "message": f"--se expects type:region, got '{part}'"},
                    },
                    ensure_ascii=False,
                )
            )
            raise SystemExit(2) from None
    return {
        "queries": list(args.queries),
        "se": se,
        "depth": args.depth,
        "is_snippet": args.is_snippet,
        "noreask": args.noreask,
    }


def _clustering_body(args: argparse.Namespace) -> dict:
    """`--queries … --se 1 --region 213 --group soft --count 3` → the clustering body."""
    body: dict = {"queries": list(args.queries), "se": args.se, "region": args.region}
    for key in ("group", "count", "depth"):
        if getattr(args, key) is not None:
            body[key] = getattr(args, key)
    if args.stoplist:
        body["stoplist"] = [entry.strip() for entry in args.stoplist.split(",") if entry.strip()]
    if args.ws:
        body["ws"] = [entry.strip() for entry in args.ws.split(",") if entry.strip()]
    if args.main:
        body["main"] = True
    return body


def _clustering(args: argparse.Namespace) -> int:
    from pathlib import Path

    from . import clustering as clustering_source

    tasks_dir = Path(args.tasks_dir) if args.tasks_dir else None
    if args.run:  # the worker of a submitted `clustering` job goes through the shared runner
        from . import tasks as tasks_module

        tasks_module.run_task(args.run, tasks_dir)
        return 0
    if args.task:
        answer = asyncio.run(
            clustering_source.wait(args.task, tasks_dir=tasks_dir)
            if args.wait
            else clustering_source.poll(args.task, tasks_dir=tasks_dir)
        )
        if args.json:
            print(json.dumps(answer, ensure_ascii=False))
        else:
            print(f"{args.task}: {answer.get('state')}")
        return 0 if answer.get("ok") and answer.get("state") != "error" else 1
    if args.input:
        request = _read_json(Path(args.input))
    elif args.queries and args.se and args.region:
        request = _clustering_body(args)
    else:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": {
                        "code": "INVALID_ARGUMENTS",
                        "message": "nothing to do: pass --input, or --queries with --se and --region, or --task ID",
                    },
                },
                ensure_ascii=False,
            )
        )
        raise SystemExit(2)
    if args.submit:
        answer = asyncio.run(clustering_source.submit(request, source=args.source, tasks_dir=tasks_dir))
        if args.json:
            print(json.dumps(answer, ensure_ascii=False))
        else:
            print(f"{answer['task_id']}: {answer['state']}; {answer['poll']}")
        return 0
    report = asyncio.run(
        clustering_source.collect(
            request,
            source=args.source,
            concurrency=args.concurrency,
            provider=args.provider,
            cache=args.cache,
            max_age=args.max_age,
            poll_interval=args.poll_interval,
            timeout=args.timeout,
        )
    )
    if args.json:
        print(json.dumps(report, ensure_ascii=False))
        return 0 if report["ok"] else 1
    for row in report["result"]["result"]:
        print(f"{row['clustered']}: {len(row['words'])} words, wssumm {row['wssumm']}, top {row['topurl']}")
    print(f"clusters: {report['summary']['clusters_total']} of {report['summary']['queries_total']} queries")
    return 0 if report["ok"] else 1


def _wordstat_body(args: argparse.Namespace) -> dict:
    """`--type 1 --queries … --regions 213,225 --ws base` → the wordstat body."""
    kind = args.type or 1
    body: dict = {"type": kind, "queries": list(args.queries)}
    if kind == 1 and args.regions:
        body["regions"] = [entry.strip() for entry in args.regions.split(",") if entry.strip()]
    if kind == 2 and args.region:
        body["region"] = args.region
    for key, value in (("ws", args.ws), ("device", args.device), ("minus_words", args.minus_words)):
        if value:
            body[key] = [entry.strip() for entry in value.split(",") if entry.strip()] if key != "device" else value
    if args.limit:
        body["limit"] = args.limit
    for key in ("is_clear_minus", "is_right", "is_clear"):
        if getattr(args, key) is not None:
            body[key] = getattr(args, key)
    return body


def _wordstat(args: argparse.Namespace) -> int:
    from pathlib import Path

    from . import wordstat as wordstat_source

    tasks_dir = Path(args.tasks_dir) if args.tasks_dir else None
    if args.run:  # the worker of a submitted `wordstat` job goes through the shared runner
        from . import tasks as tasks_module

        tasks_module.run_task(args.run, tasks_dir)
        return 0
    if args.task:
        answer = asyncio.run(
            wordstat_source.wait(args.task, tasks_dir=tasks_dir)
            if args.wait
            else wordstat_source.poll(args.task, tasks_dir=tasks_dir)
        )
        if args.json:
            print(json.dumps(answer, ensure_ascii=False))
        else:
            print(f"{args.task}: {answer.get('state')}")
        return 0 if answer.get("ok") and answer.get("state") != "error" else 1
    if args.input:
        request = _read_json(Path(args.input))
    elif args.queries:
        request = _wordstat_body(args)
    else:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": {
                        "code": "INVALID_ARGUMENTS",
                        "message": "nothing to do: pass --input, or --queries with --regions/--region, or --task ID",
                    },
                },
                ensure_ascii=False,
            )
        )
        raise SystemExit(2)
    if args.submit:
        answer = asyncio.run(wordstat_source.submit(request, source=args.source, tasks_dir=tasks_dir))
        if args.json:
            print(json.dumps(answer, ensure_ascii=False))
        else:
            print(f"{answer['task_id']}: {answer['state']}; {answer['poll']}")
        return 0
    report = asyncio.run(
        wordstat_source.collect(
            request,
            source=args.source,
            provider=args.provider,
            cache=args.cache,
            max_age=args.max_age,
            concurrency=args.concurrency,
            poll_interval=args.poll_interval,
            timeout=args.timeout,
        )
    )
    if args.json:
        print(json.dumps(report, ensure_ascii=False))
        return 0 if report["ok"] else 1
    result = report["result"]
    if result.get("type") == 1:
        for query, per_region in (result["data"].get("result") or {}).items():
            print(f"{query}: " + ", ".join(f"{region} {values}" for region, values in per_region.items()))
    else:
        for query, row in zip(result["queries"], result.get("data") or [], strict=False):
            print(f"{query}: {len(row.get('left') or {})} left, {len(row.get('right') or {})} right")
    return 0 if report["ok"] else 1


def _highlights_body(args: argparse.Namespace) -> dict:
    """`--queries … --se 1 --region 213 --with questions` → the `sp` body."""
    body: dict = {"queries": list(args.queries), "se": args.se, "region": args.region}
    if args.depth is not None:
        body["depth"] = args.depth
    if args.with_blocks:
        body["with"] = [entry.strip() for entry in args.with_blocks.split(",") if entry.strip()]
    return body


def _highlights(args: argparse.Namespace) -> int:
    from pathlib import Path

    from . import highlights as highlights_source

    tasks_dir = Path(args.tasks_dir) if args.tasks_dir else None
    if args.run:  # the worker of a submitted `highlights` job goes through the shared runner
        from . import tasks as tasks_module

        tasks_module.run_task(args.run, tasks_dir)
        return 0
    if args.task:
        answer = asyncio.run(
            highlights_source.wait(args.task, tasks_dir=tasks_dir)
            if args.wait
            else highlights_source.poll(args.task, tasks_dir=tasks_dir)
        )
        if args.json:
            print(json.dumps(answer, ensure_ascii=False))
        else:
            print(f"{args.task}: {answer.get('state')}")
        return 0 if answer.get("ok") and answer.get("state") != "error" else 1
    if args.input:
        request = _read_json(Path(args.input))
    elif args.queries and args.se and args.region:
        request = _highlights_body(args)
    else:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": {
                        "code": "INVALID_ARGUMENTS",
                        "message": "nothing to do: pass --input, or --queries with --se and --region, or --task ID",
                    },
                },
                ensure_ascii=False,
            )
        )
        raise SystemExit(2)
    if args.submit:
        answer = asyncio.run(highlights_source.submit(request, source=args.source, tasks_dir=tasks_dir))
        if args.json:
            print(json.dumps(answer, ensure_ascii=False))
        else:
            print(f"{answer['task_id']}: {answer['state']}; {answer['poll']}")
        return 0
    report = asyncio.run(
        highlights_source.collect(
            request,
            source=args.source,
            cache=args.cache,
            max_age=args.max_age,
            poll_interval=args.poll_interval,
            timeout=args.timeout,
        )
    )
    if args.json:
        print(json.dumps(report, ensure_ascii=False))
        return 0 if report["ok"] else 1
    for query, found in report["result"]["result"].items():
        bold = ", ".join(item["phrase"] for item in found["highlights"][:5])
        lsi = ", ".join(item["term"] for item in found["lsi"][:8])
        print(f"{query}: {len(found['results'])} results; bold: {bold or '-'}; lsi: {lsi or '-'}")
    return 0 if report["ok"] else 1


def _tasks(args: argparse.Namespace) -> int:
    from pathlib import Path

    from . import clustering, highlights, wordstat  # noqa: F401 - importing them registers their task kinds
    from . import tasks as tasks_module

    tasks_dir = Path(args.tasks_dir) if args.tasks_dir else None
    if args.run:
        tasks_module.run_task(args.run, tasks_dir)
        return 0
    if args.gc:
        collected = tasks_module.gc(tasks_dir, older_than_days=args.older_than)
        answer = {"ok": True, "task_id": None, **collected}
        if args.json:
            print(json.dumps(answer, ensure_ascii=False))
        else:
            print(f"removed {len(collected['removed'])}, kept {collected['kept']}")
        return 0
    if not args.id:
        print(
            json.dumps(
                {"ok": False, "error": {"code": "INVALID_ARGUMENTS", "message": "nothing to read: pass --task ID"}},
                ensure_ascii=False,
            )
        )
        raise SystemExit(2)
    store = tasks_module.default_store(tasks_dir)
    try:
        if args.cancel:
            answer = asyncio.run(tasks_module.cancel(args.id, store=store))
        else:
            answer = asyncio.run(
                tasks_module.wait(args.id, store=store) if args.wait else tasks_module.get(args.id, store=store)
            )
    except (FileNotFoundError, ValueError) as exc:  # an id nobody knows: the caller's mistake, said plainly
        print(
            json.dumps({"ok": False, "error": {"code": "INVALID_ARGUMENTS", "message": str(exc)}}, ensure_ascii=False)
        )
        raise SystemExit(2) from None
    if args.json:
        print(json.dumps(answer, ensure_ascii=False))
    else:
        print(f"{args.id}: {answer.get('state')}")
    return 0 if answer.get("ok") and answer.get("state") != "error" else 1


def _top(args: argparse.Namespace) -> int:
    from pathlib import Path

    from . import top as top_source

    tasks_dir = Path(args.tasks_dir) if args.tasks_dir else None
    if args.run:  # the worker of a submitted `top` job goes through the shared runner
        from . import tasks as tasks_module

        tasks_module.run_task(args.run, tasks_dir)
        return 0
    if args.task:
        answer = asyncio.run(
            top_source.wait(args.task, tasks_dir=tasks_dir)
            if args.wait
            else top_source.poll(args.task, tasks_dir=tasks_dir)
        )
        if args.json:
            print(json.dumps(answer, ensure_ascii=False))
        else:
            print(f"{args.task}: {answer.get('state')}")
        return 0 if answer.get("ok") and answer.get("state") != "error" else 1
    if args.input:
        request = _read_json(Path(args.input))
    elif args.queries:
        request = _check_top_body(args)
    else:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": {
                        "code": "INVALID_ARGUMENTS",
                        "message": "nothing to do: pass --input or --queries/--se, or --task ID to read a submitted task",
                    },
                },
                ensure_ascii=False,
            )
        )
        raise SystemExit(2)
    if args.submit:
        answer = asyncio.run(top_source.submit(request, source=args.source, tasks_dir=tasks_dir))
        if args.json:
            print(json.dumps(answer, ensure_ascii=False))
        else:
            print(f"{answer['task_id']}: {answer['state']}; {answer['poll']}")
        return 0
    report = asyncio.run(
        top_source.collect(
            request,
            source=args.source,
            concurrency=args.concurrency,
            poll_interval=args.poll_interval,
            timeout=args.timeout,
            top_n=args.top_n,
        )
    )
    if args.json:
        print(json.dumps(report, ensure_ascii=False))
        return 0 if report["ok"] else 1
    collect = report["result"]["result"]["collect"]
    for query, per_se in zip(report["result"]["request"]["queries"], collect, strict=True):
        print(f"{query}: " + ", ".join(f"{len(urls)} urls" for urls in per_se))
    for row in report["summary"]["top"]:
        print(f"{row['strength']:.3f}  {row['url']}  (best {row['best_position']}, queries {row['queries_count']})")
    return 0 if report["ok"] else 1


def _semantics(args: argparse.Namespace) -> int:
    from pathlib import Path

    from . import semantics as semantics_source
    from .semantics import MAX_CONCURRENT, collect

    words = [word.strip() for word in (args.stopwords or "").split(",") if word.strip()]
    if args.input:
        request = semantics_source.normalise(json.loads(Path(args.input).read_text(encoding="utf-8")))
    elif args.urls:
        request = semantics_source.normalise(
            {"queries": list(args.urls), "engine": args.engine, "stopwords": words, "urls": list(args.urls)}
        )
    else:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": {"code": "INVALID_ARGUMENTS", "message": "nothing to collect: pass --urls or --input"},
                },
                ensure_ascii=False,
            )
        )
        raise SystemExit(2)
    if args.submit:
        from pathlib import Path

        from . import tasks as tasks_module

        answer = asyncio.run(
            tasks_module.submit(
                "semantics",
                {
                    **request,
                    "service": args.service,
                    "concurrency": args.concurrency or 5,
                    "cache": args.cache,
                    "max_age": args.max_age,
                },
                store=tasks_module.default_store(Path(args.tasks_dir) if args.tasks_dir else None),
            )
        )
        if args.json:
            print(json.dumps(answer, ensure_ascii=False))
        else:
            print(f"{answer['task_id']}: {answer['state']}; {answer['poll']}")
        return 0
    report = asyncio.run(
        collect(
            request,
            name=args.service,
            concurrency=args.concurrency or MAX_CONCURRENT,
            cache=args.cache,
            max_age=args.max_age,
        )
    )
    if args.json:
        print(json.dumps(report, ensure_ascii=False))
        return 0 if report["ok"] else 1
    for document in report["urls"]:
        state = f"{len(document.get('queries') or [])} phrases" if document.get("ok") else document.get("error")
        print(f"{document['url']} — {state}")
    print(f"phrases in the summary: {len(report['summary'])}; waited {report['requests_waited_s']} s", file=sys.stderr)
    return 0 if report["ok"] else 1


def _doctor() -> int:
    """`doctor --json`: what this machine can and cannot do, as the family's envelope."""
    from .credentials import binding

    checks = []
    for service in SERVICES:
        api_key, proxy = binding(service)
        checks.append(
            {
                "name": f"credential:{service}",
                "ok": True,  # a missing key is not a broken machine: the cascade just skips the step
                "detail": ("key present" if api_key else "no key") + (", proxy bound" if proxy else ""),
            }
        )
    payload = {"ok": True, "checks": checks}
    print(json.dumps(payload, ensure_ascii=False))
    return 0


def _auth_status() -> int:
    """`auth status --json`: the family's envelope — where a key comes from, never the key."""
    from rb_credentials.status import entry, exit_code, problems, report

    from .credentials import binding, in_windmill

    source = (
        "windmill" if in_windmill() else ("9router" if os.environ.get("RB_WORKFLOWS_SOURCE") == "9router" else "store")
    )
    records = []
    for service in SERVICES:
        api_key = binding(service)[0]
        records.append(
            entry(
                service,
                required=False,  # a service without a key is skipped by the waterfall, not a broken install
                fields=("api_key",),
                source=source if api_key else "missing",
                source_ref=f"{source}:{service}" if api_key else None,
                status="unchecked" if api_key else "missing",
                message=None if api_key else f"run `wf auth login --provider {service}`",
            )
        )
    payload = report("wf", version(), records)
    payload["ok"] = not problems(payload)
    print(json.dumps(payload, ensure_ascii=False))
    return exit_code(payload)


BARE_AUTH = """Usage: wf auth COMMAND

Commands:
  status    where every service's key comes from (never the key)
"""

SERVICES = ("exa", "tavily")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
