"""CLI for an independent BORG coordination installation."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from typing import Any, Iterable

from comms.hub.client import ClientError
from comms.hub.service import HubError

from .portable import (
    DEFAULT_AGENT_ACTIONS,
    DEFAULT_PORT,
    PortableError,
    beads_init_command,
    bootstrap,
    enroll,
    init_beads,
    exec_beads,
    load_root_config,
    policy_pin,
    start,
    status,
)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="borg-coordination")
    commands = parser.add_subparsers(dest="command", required=True)

    bootstrap_parser = commands.add_parser("bootstrap")
    bootstrap_parser.add_argument("--home", required=True)
    bootstrap_parser.add_argument("--owner", required=True)
    bootstrap_parser.add_argument("--port", type=int, default=DEFAULT_PORT)

    enroll_parser = commands.add_parser("enroll")
    enroll_parser.add_argument("--home", required=True)
    enroll_parser.add_argument("--agent", required=True)
    enroll_parser.add_argument("--runtime", required=True)
    enroll_parser.add_argument("--machine", required=True)
    enroll_parser.add_argument(
        "--grant-action", action="append", dest="actions", default=None
    )
    enroll_parser.add_argument("--scope", default="/")
    enroll_parser.add_argument("--delegable", action="store_true")

    for name in ("start", "status", "policy-pin", "beads-command", "init-beads", "exec-beads"):
        child = commands.add_parser(name)
        child.add_argument("--home", required=True)
        if name in {"beads-command", "init-beads", "exec-beads"}:
            child.add_argument("--bd", default="bd")
        if name == "exec-beads":
            child.add_argument("arguments", nargs=argparse.REMAINDER)
    return parser


def run(args: argparse.Namespace) -> dict[str, Any] | None:
    if args.command == "bootstrap":
        return bootstrap(args.home, args.owner, args.port)
    if args.command == "enroll":
        return enroll(
            args.home,
            args.agent,
            args.runtime,
            args.machine,
            actions=args.actions or DEFAULT_AGENT_ACTIONS,
            scope=args.scope,
            delegable=args.delegable,
        )
    if args.command == "start":
        start(args.home)
        return None
    if args.command == "status":
        return status(args.home)
    if args.command == "policy-pin":
        return policy_pin(args.home)
    if args.command == "beads-command":
        config = load_root_config(args.home)
        return {"cwd": config["beads_dir"], "command": beads_init_command(args.home, args.bd)}
    if args.command == "init-beads":
        return init_beads(args.home, args.bd)
    if args.command == "exec-beads":
        arguments = args.arguments[1:] if args.arguments[:1] == ["--"] else args.arguments
        exec_beads(args.home, args.bd, arguments)
        return None
    raise PortableError("invalid_command", "Unknown command")


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    try:
        result = run(args)
        if result is not None:
            print(_json(result))
        return 0
    except (PortableError, ClientError, HubError) as exc:
        error = {
            "code": getattr(exc, "code", "request_error"),
            "message": getattr(exc, "message", "Request rejected"),
        }
        print(_json({"ok": False, "error": error}), file=sys.stderr)
        return 1
    except (OSError, subprocess.SubprocessError, ValueError):
        print(
            _json(
                {
                    "ok": False,
                    "error": {
                        "code": "internal_error",
                        "message": "Coordination command failed",
                    },
                }
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
