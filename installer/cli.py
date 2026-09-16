"""BORG setup and service lifecycle."""
from __future__ import annotations

import argparse
import getpass
import json
import os
from pathlib import Path
import sys
import subprocess


def parser() -> argparse.ArgumentParser:
    cli = argparse.ArgumentParser(description=__doc__)
    commands = cli.add_subparsers(dest="command", required=True)
    for name in ["init", "dependencies", "install", "start", "stop", "status", "doctor", "auth", "hook", "mcp-stdio", "tools", "call", "onboard"]:
        command = commands.add_parser(name)
        command.add_argument("--home", type=Path, default=Path(os.environ.get("BORG_HOME", Path.home() / ".borg")))
        if name in {"init", "install"}:
            command.add_argument("--owner")
            command.add_argument("--port-base", type=int, default=18760)
            command.add_argument("--projects", action="append")
        if name in {"dependencies", "install"}:
            command.add_argument("--system-dependencies", action="store_true")
        if name == "install":
            command.add_argument("--no-start", action="store_true")
            command.add_argument("--blueprint", type=Path)
            command.add_argument("--machine")
            command.add_argument("--validate-only", action="store_true", help=argparse.SUPPRESS)
        if name == "auth":
            command.add_argument("provider", choices=["codex"])
        if name == "hook":
            command.add_argument("mode", choices=["prime", "start", "end"])
        if name in {"start", "stop"}:
            command.add_argument("components", nargs="*")
        if name == "tools":
            command.add_argument("prefix", nargs="?", default="")
        if name == "call":
            command.add_argument("tool")
            command.add_argument("--arguments", default="{}", help="JSON arguments; never pass credentials")
    blueprint = commands.add_parser("blueprint", help="validate and inspect a portable machine plan")
    blueprint.add_argument("operation", choices=["inspect"])
    blueprint.add_argument("file", type=Path)
    blueprint.add_argument("--machine")
    web = commands.add_parser("web", help="configure this owner's Cloudflare web connector")
    web.add_argument("--home", type=Path, default=Path(os.environ.get("BORG_HOME", Path.home() / ".borg")))
    for name in ["public-url", "issuer", "audience", "owner-email"]:
        web.add_argument("--" + name, required=True)
    web.add_argument("--tunnel-credentials", type=Path, required=True)
    adapters = commands.add_parser("adapters", help="inspect or prepare the included inactive MLX adapters")
    adapters.add_argument("operation", choices=["list", "prepare"])
    adapters.add_argument("name", nargs="?", default="all")
    adapters.add_argument("--home", type=Path, default=Path(os.environ.get("BORG_HOME", Path.home() / ".borg")))
    fleet = commands.add_parser("fleet", help="enroll and inspect this owner's machines")
    fleet.add_argument("--home", type=Path, default=Path(os.environ.get("BORG_HOME", Path.home() / ".borg")))
    fleet.add_argument("operation", choices=["list", "add", "disable"])
    fleet.add_argument("host", nargs="?")
    fleet.add_argument("--ssh-alias")
    fleet.add_argument("--remote-home", type=Path)
    fleet.add_argument("--owner")
    fleet.add_argument("--instance-id")
    fleet.add_argument("--label")
    fleet.add_argument("--role", action="append", default=[])
    return cli


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    os.umask(0o077)
    try:
        from installer import blueprint
        if args.command == "blueprint":
            registry = blueprint.catalog()
            print(json.dumps(blueprint.inspect(blueprint.load(args.file, registry), registry, args.machine), indent=2))
            return 0
        from installer import config
        selection = None
        if args.command == "install":
            if args.machine and not args.blueprint:
                raise ValueError("--machine requires --blueprint")
            if args.blueprint:
                value = blueprint.load(args.blueprint)
                row = blueprint.machine(value, args.machine)
                blueprint.check_runtime(row)
                selection = {"input": value, "machine_id": row["id"]}
            existing = blueprint.check_existing(args.home, selection)
            if existing and args.owner and existing["owner"] != args.owner:
                raise ValueError("Existing BORG belongs to another owner; use a separate home")
            from installer.installation import source_files
            source_files()  # Refuse an incomplete package before creating owner state.
            if existing:
                from installer.installation import preflight
                preflight(existing)
            if args.validate_only:
                return 0
        if args.command in {"init", "install"}:
            owner = args.owner
            if owner is None:
                owner = (config.load(args.home)["owner"] if (args.home / "config.json").exists()
                         else getpass.getuser().lower().replace(".", "-"))
            doc = config.initialize(args.home, owner, port_base=args.port_base, projects=args.projects, blueprint_selection=selection)
        else:
            doc = config.load(args.home)
        if args.command == "init":
            print(json.dumps({"state": "configured", "home": doc["home"], "owner": doc["owner"],
                              "instance_id": doc["instance_id"]}, indent=2))
        elif args.command == "dependencies":
            from installer.installation import preflight
            preflight(doc)
            from installer.dependencies import prepare
            prepare(doc, system_dependencies=args.system_dependencies)
            print("Dependencies installed; application source and native acceptance are separate steps.")
        elif args.command == "onboard":
            from installer.onboarding import plan
            print(json.dumps(plan(doc), indent=2))
        elif args.command == "fleet":
            from installer.fleet import manage
            print(json.dumps(manage(doc, args), indent=2))
        elif args.command in {"start", "stop"}:
            from installer import services
            result = getattr(services, args.command)(doc, args.components or None)
            print(json.dumps(result or {"state": "stopped"}, indent=2))
        elif args.command == "install":
            from installer.installation import install
            return install(doc, system_dependencies=args.system_dependencies, start=not args.no_start)
        elif args.command in {"status", "doctor"}:
            from installer.health import status
            result = status(doc)
            print(json.dumps(result, indent=2))
            return 0 if result["ready"] else 1
        elif args.command == "auth":
            from installer.installation import login
            return login(doc, args.provider)
        elif args.command == "adapters":
            from installer.adapters import list_adapters, prepare
            print(json.dumps(list_adapters(doc) if args.operation == "list" else prepare(doc, args.name), indent=2))
        elif args.command == "web":
            from installer.web import configure
            print(json.dumps(configure(doc, public_url=args.public_url, issuer=args.issuer,
                                      audience=args.audience, owner_email=args.owner_email,
                                      tunnel_credentials=args.tunnel_credentials), indent=2))
        elif args.command in {"hook", "mcp-stdio"}:
            from installer import clients
            if args.command == "hook":
                clients.hook(doc, args.mode)
            else:
                clients.stdio_proxy(doc)
        elif args.command in {"tools", "call"}:
            from installer.clients import tool_command
            return tool_command(doc, args.command, getattr(args, "prefix", ""),
                                getattr(args, "tool", ""), getattr(args, "arguments", "{}"))
        return 0
    except subprocess.CalledProcessError as exc:
        print(f"BORG: dependency or service command failed (exit {exc.returncode}); see its preceding output", file=sys.stderr)
        return 1
    except (ValueError, OSError, RuntimeError) as exc:
        print("BORG: " + str(exc), file=sys.stderr)
        return 1
