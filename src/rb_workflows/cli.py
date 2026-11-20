"""Local entry point: run a workflow here, with the same code that would deploy to Windmill.

The output is JSON on stdout (the contract), and a short human summary when `--json` is absent.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from .flows.web_fetch import web_fetch

__all__ = ["main", "run"]

WORKFLOWS = {"web-fetch": web_fetch}


def run(name: str, payload: dict) -> dict:
    """Run one workflow with `payload` as its keyword input, synchronously."""
    workflow = WORKFLOWS.get(name)
    if workflow is None:
        raise SystemExit(f"unknown workflow '{name}'; known: {', '.join(WORKFLOWS)}")
    return asyncio.run(workflow(**payload))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="rb-workflows", description="Run a workflow locally (Windmill-as-code).")
    sub = parser.add_subparsers(dest="command", required=True)
    go = sub.add_parser("run", help="run one workflow")
    go.add_argument("workflow", choices=sorted(WORKFLOWS))
    go.add_argument("--input", help='JSON object with the workflow input, e.g. {"urls": ["https://example.com"]}')
    go.add_argument("--urls", nargs="+", help="URLs to fetch (a shortcut for --input)")
    go.add_argument("--budget-usd", type=float, help="spend ceiling for the whole run")
    go.add_argument("--proxy", help="proxy for every step (overrides the key's own binding)")
    go.add_argument("--steps", nargs="+", help="limit the cascade, e.g. --steps exa direct")
    go.add_argument("--json", action="store_true", help="print the result as JSON (the contract)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
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
