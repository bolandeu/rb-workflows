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
    sem.add_argument("--json", action="store_true", help="the report as JSON (the contract)")
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
  semantics a keyword set for one URL or a batch, from an external service
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


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


def _semantics(args: argparse.Namespace) -> int:
    from .semantics import MAX_CONCURRENT, collect

    words = [word.strip() for word in (args.stopwords or "").split(",") if word.strip()]
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
