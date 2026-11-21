"""Local entry point: run a workflow here, with the same code that would deploy to Windmill.

The output is JSON on stdout (the contract), and a short human summary when `--json` is absent.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from typing import NoReturn

from .flows.web_fetch import web_fetch
from .version import version

__all__ = ["main", "run"]

WORKFLOWS = {"web-fetch": web_fetch}


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
    sem.add_argument("--urls", nargs="+", required=True, help="URLs to collect for (each one is a task of the service)")
    sem.add_argument("--engine", default="msk", choices=("msk", "gru"), help="msk — Yandex Moscow, gru — Google Moscow")
    sem.add_argument("--service", default="arsenkin", help="which service answers (default: arsenkin)")
    sem.add_argument("--stopwords", help="comma-separated minus-words")
    sem.add_argument("--concurrency", type=int, default=None, help="tasks in flight at once (the account allows 5)")
    sem.add_argument("--timeout", type=float, default=1800.0, help="seconds a single task may take")
    sem.add_argument("--poll-interval", type=float, default=15.0, help="seconds between status checks")
    sem.add_argument("--submit", action="store_true", help="place the job and answer with its id at once")
    sem.add_argument("--tasks-dir", help="where the task files live (default: the user state dir)")
    sem.add_argument("--json", action="store_true", help="the report as JSON (the contract)")
    tasks_cmd = sub.add_parser("task", help="a submitted job: its state, or the report once it is done")
    tasks_cmd.add_argument("id", nargs="?", metavar="ID", help="the task id: `local:…` or `arsenkin:…`")
    tasks_cmd.add_argument("--wait", action="store_true", help="block until the job finishes")
    tasks_cmd.add_argument("--cancel", action="store_true", help="ask the store to drop the job")
    tasks_cmd.add_argument("--run", metavar="ID", help=argparse.SUPPRESS)  # the detached worker of --submit
    tasks_cmd.add_argument("--tasks-dir", help="where the task files live (default: the user state dir)")
    tasks_cmd.add_argument("--json", action="store_true", help="the report as JSON (the contract)")
    top_cmd = sub.add_parser("top", help="positions in Arsenkin's check-top shape, from our serp or from Arsenkin")
    top_cmd.add_argument("--input", help="JSON file with the check-top body (or the bare data object)")
    top_cmd.add_argument("--submit", action="store_true", help="place the task and answer with its id at once")
    top_cmd.add_argument("--task", metavar="ID", help="read a submitted task: its state, or the report when it is done")
    top_cmd.add_argument("--wait", action="store_true", help="with --task: block until the task finishes")
    top_cmd.add_argument("--run", metavar="ID", help=argparse.SUPPRESS)  # the detached worker of --submit
    top_cmd.add_argument("--tasks-dir", help="where the task files live (default: the user state dir)")
    top_cmd.add_argument("--source", default="serp", choices=("serp", "arsenkin"), help="who answers (default: serp)")
    top_cmd.add_argument("--top-n", type=int, default=10, help="rows of the strength summary to show")
    top_cmd.add_argument("--concurrency", type=int, default=3, help="queries in flight at once (source=serp)")
    top_cmd.add_argument(
        "--timeout", type=float, default=3600.0, help="seconds the whole task may take (source=arsenkin)"
    )
    top_cmd.add_argument(
        "--poll-interval", type=float, default=15.0, help="seconds between status checks (source=arsenkin)"
    )
    top_cmd.add_argument("--json", action="store_true", help="the report as JSON (the contract)")
    go = sub.add_parser("run", help="run one workflow")
    go.add_argument("workflow", choices=sorted(WORKFLOWS))
    go.add_argument("--input", help='JSON object with the workflow input, e.g. {"urls": ["https://example.com"]}')
    go.add_argument("--urls", nargs="+", help="URLs to fetch (a shortcut for --input)")
    go.add_argument("--budget-usd", type=float, help="spend ceiling for the whole run")
    go.add_argument("--proxy", help="proxy for every step (overrides the key's own binding)")
    go.add_argument("--steps", nargs="+", help="limit the cascade, e.g. --steps exa direct")
    go.add_argument("--json", action="store_true", help="print the result as JSON (the contract)")
    return parser


BARE_HELP = """Usage: wf COMMAND

Commands:
  run       run one workflow
  semantics  a keyword set for one URL or a batch, from an external service
  top        positions in Arsenkin's check-top shape, from our serp or from Arsenkin
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
                "description": "run one workflow",
                "arguments": [_param("workflow", "choice")],
                "options": [
                    _param("--input", "str"),
                    _param("--urls", "str", multiple=True),
                    _param("--budget-usd", "float"),
                    _param("--proxy", "str"),
                    _param("--steps", "str", multiple=True),
                    _JSON_OPTION,
                ],
            },
            {
                "name": "semantics",
                "description": "a keyword set for one URL or a batch, from an external service",
                "options": [
                    _param("--urls", "str", multiple=True),
                    _param("--engine", "choice", "msk"),
                    _param("--service", "str", "arsenkin"),
                    _param("--stopwords", "str"),
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
                "options": [_param("--wait", "boolean", False), _param("--cancel", "boolean", False), _JSON_OPTION],
            },
            {
                "name": "top",
                "description": "positions in Arsenkin's check-top shape, from our serp or from Arsenkin",
                "options": [
                    _param("--input", "path"),
                    _param("--source", "choice", "serp"),
                    _param("--top-n", "int", 10),
                    _param("--concurrency", "int", 3),
                    _param("--timeout", "float", 3600.0),
                    _param("--poll-interval", "float", 15.0),
                    _JSON_OPTION,
                ],
            },
            {
                "name": "task",
                "description": "a submitted job: its state, or the report once it is done",
                "arguments": [_param("id", "str")],
                "options": [_param("--wait", "boolean", False), _param("--cancel", "boolean", False), _JSON_OPTION],
            },
            {
                "name": "top",
                "description": "positions in Arsenkin's check-top shape, from our serp or from Arsenkin",
                "options": [
                    _param("--input", "path"),
                    _param("--source", "choice", "serp"),
                    _param("--top-n", "int", 10),
                    _param("--concurrency", "int", 3),
                    _param("--timeout", "float", 3600.0),
                    _param("--poll-interval", "float", 15.0),
                    _JSON_OPTION,
                ],
            },
            {
                "name": "serp",
                "description": "positions in Yandex/Google for a batch of queries, plus the cluster summary",
                "options": [
                    _param("--queries", "str", multiple=True),
                    _param("--engine", "choice", "both"),
                    _param("--top", "int", 20),
                    _param("--concurrency", "int", 3),
                    _param("--provider", "str"),
                    _param("--cache", "boolean", False),
                    _param("--max-age", "str"),
                    _param("--yandex-lr", "int"),
                    _param("--google-loc", "str"),
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
    if args.command == "semantics":
        return _semantics(args)
    if args.command == "top":
        return _top(args)
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
    payload: dict = json.loads(args.input) if args.input else {}
    imports = {"urls": args.urls, "budget_usd": args.budget_usd, "proxy": args.proxy, "steps": args.steps}
    payload |= {key: value for key, value in imports.items() if value is not None}
    if "urls" not in payload:
        raise SystemExit("nothing to fetch: pass --urls or --input '{\"urls\": [...]}'")

    result = run(args.workflow, payload)
    if args.json:
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result["ok"] else 1
    for doc in result["documents"]:
        status = "ok" if doc["ok"] else f"{doc['reason']}"
        print(f"{doc['url']} — {status} ({doc['source']}, {len(doc['text'])} chars, ${doc['cost_usd'] or 0:.4f})")
    print(f"spent: ${result['budget_spent_usd']:.4f}", file=sys.stderr)
    return 0 if result["ok"] else 1


def _tasks(args: argparse.Namespace) -> int:
    from pathlib import Path

    from . import tasks as tasks_module

    tasks_dir = Path(args.tasks_dir) if args.tasks_dir else None
    if args.run:
        tasks_module.run_task(args.run, tasks_dir)
        return 0
    if not args.id:
        print(
            json.dumps(
                {"ok": False, "error": {"code": "INVALID_ARGUMENTS", "message": "nothing to read: pass --task ID"}},
                ensure_ascii=False,
            )
        )
        raise SystemExit(2)
    store = tasks_module.FileStore(tasks_dir)
    if args.cancel:
        answer = tasks_module.cancel(args.id, store=store)
    else:
        answer = tasks_module.wait(args.id, store=store) if args.wait else tasks_module.get(args.id, store=store)
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
            print(f"{args.id}: {answer.get('state')}")
        return 0 if answer.get("ok") and answer.get("state") != "error" else 1
    if not args.input:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": {
                        "code": "INVALID_ARGUMENTS",
                        "message": "nothing to do: pass --input (to run or submit) or --task ID (to read a submitted task)",
                    },
                },
                ensure_ascii=False,
            )
        )
        raise SystemExit(2)
    request = json.loads(Path(args.input).read_text(encoding="utf-8"))
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
    from .semantics import MAX_CONCURRENT, collect

    words = [word.strip() for word in (args.stopwords or "").split(",") if word.strip()]
    if args.submit:
        from pathlib import Path

        from . import tasks as tasks_module

        answer = asyncio.run(
            tasks_module.submit(
                "semantics",
                {
                    "urls": list(args.urls),
                    "service": args.service,
                    "engine": args.engine,
                    "stopwords": words,
                    "concurrency": args.concurrency or 5,
                    "timeout": args.timeout,
                    "poll_interval": args.poll_interval,
                },
                store=tasks_module.FileStore(Path(args.tasks_dir) if args.tasks_dir else None),
            )
        )
        if args.json:
            print(json.dumps(answer, ensure_ascii=False))
        else:
            print(f"{answer['task_id']}: {answer['state']}; {answer['poll']}")
        return 0
    report = asyncio.run(
        collect(
            args.urls,
            name=args.service,
            region=args.engine,
            stopwords=words,
            concurrency=args.concurrency or MAX_CONCURRENT,
            timeout=args.timeout,
            poll_interval=args.poll_interval,
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
