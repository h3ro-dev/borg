"""Bind native clients and lifecycle hooks to this installation only."""
from __future__ import annotations

import importlib.machinery
import asyncio
import json
import os
from pathlib import Path
import shlex
import sys

from installer import config, services


def tool_command(doc: dict, action: str, prefix: str = "", tool: str = "", arguments: str = "{}") -> int:
    """Discover and call the live MCP contract when a client's schemas are stale."""
    root = Path(doc["home"])
    sys.path.insert(0, str(root / "borg-context"))
    from borg_context_server import private_text
    from fastmcp import Client
    settings = config.read_private(root / "borg-context/config.json")
    authorization = private_text(Path(settings["inbound_authorization_file"]))
    if not authorization.startswith("Bearer "):
        raise ValueError("Invalid local connector authorization file")
    payload = json.loads(arguments)
    if not isinstance(payload, dict):
        raise ValueError("Tool arguments must be a JSON object")

    async def invoke():
        async with Client(f"http://127.0.0.1:{doc['ports']['connector']}/mcp",
                          auth=authorization.removeprefix("Bearer "), timeout=120) as client:
            if action == "tools":
                rows = await client.list_tools()
                print(json.dumps([r.model_dump(mode="json") for r in rows if r.name.startswith(prefix)]))
                return 0
            result = await client.call_tool(tool, payload, raise_on_error=False)
            print(json.dumps({"content": [item.model_dump(mode="json") for item in result.content],
                              "structuredContent": result.structured_content, "isError": result.is_error}))
            return 1 if result.is_error else 0
    try:
        return asyncio.run(invoke())
    except (ValueError, OSError, RuntimeError):
        raise RuntimeError("MCP request was not confirmed; check its operation receipt before repeating a write") from None


def hook(doc: dict, mode: str) -> None:
    root = Path(doc["home"])
    env = {**os.environ, **services.service_environment(doc),
           "MEM0_HOOK_SCOPES": doc["memory"]["default_scope"],
           "MEM0_CAPTURE_SCOPE": doc["memory"]["default_scope"]}
    python = str(root / "mem0/venv/bin/python")
    os.execve(python, [python, str(root / "mem0/bin/mem0-codex-hook"), mode], env)


def stdio_proxy(doc: dict) -> None:
    """Reuse FastMCP's native proxy without exposing a bearer in client config."""
    root = Path(doc["home"])
    sys.path.insert(0, str(root / "borg-context"))
    from borg_context_server import private_text
    from fastmcp import Client
    from fastmcp.server import create_proxy
    settings = config.read_private(root / "borg-context/config.json")
    authorization = private_text(Path(settings["inbound_authorization_file"]))
    if not authorization.startswith("Bearer "):
        raise ValueError("Invalid local connector authorization file")
    backend = Client(f"http://127.0.0.1:{doc['ports']['connector']}/mcp",
                     auth=authorization.removeprefix("Bearer "), timeout=120)
    create_proxy(backend, name="BORG").run(transport="stdio", show_banner=False)


def configure_clients(doc: dict) -> dict:
    root = Path(doc["home"])
    hub = config.read_private(root / "coordination/config.json")
    hub_client = Path(hub["connector_client_config"])
    if not hub_client.resolve().is_relative_to(root / "coordination/data"):
        raise ValueError("The connector Inbox identity must belong to this installation")
    connector_path = root / "borg-context/config.json"
    connector = config.read_private(connector_path)
    connector["computer"]["inbox_client_config"] = str(hub_client)
    config.write_private(connector_path, json.dumps(connector, indent=2) + "\n", replace=True)

    # Use the existing native app-server protocol and CAS provisioning helpers.
    native = importlib.machinery.SourceFileLoader(
        "borg_native_client_setup", str(root / "mem0/bin/mem0-fleet-configure")
    ).load_module()
    profile = root / "conductors/primary/profile"
    target = profile / "config.toml"
    if not target.exists():
        config.write_private(target, "# This profile belongs to this BORG installation.\n")
    elif target.is_symlink() or target.stat().st_mode & 0o077:
        raise ValueError("The dedicated provider profile config must be private")
    before = target.read_text()
    backup = root / "conductors/primary/config.before-borg.toml"
    if not backup.exists():
        config.write_private(backup, before)
    # NativeRPC inherits PATH for the native Codex launcher. No provider identity
    # or credentials are copied from any other profile.
    previous_path = os.environ.get("PATH")
    os.environ["PATH"] = services.service_environment(doc)["PATH"]
    rpc = None
    try:
        rpc = native.NativeRPC(profile, str(root / "runtime/npm/node_modules/.bin/codex"))
        layer = native.raw_user_layer(rpc.call("config/read", {"includeLayers": True}), target)
        current = layer["config"]
        command = [str(root / "mem0/venv/bin/python"), str(root / "app/borg.py")]
        mcp = {"command": command[0], "args": [command[1], "mcp-stdio", "--home", str(root)],
               "startup_timeout_sec": 30, "tool_timeout_sec": 120}
        existing = current.get("mcp_servers", {}).get("borg")
        if existing is not None and existing != mcp:
            raise ValueError("An owner-edited BORG client configuration needs reconciliation")
        edits = [{"keyPath": "mcp_servers.borg", "value": mcp, "mergeStrategy": "replace"}]
        commands = {}
        for event, mode in [("SessionStart", "prime"), ("UserPromptSubmit", "start"),
                            ("Stop", "end"), ("SessionEnd", "end")]:
            text = shlex.join([*command, "hook", "--home", str(root), mode])
            commands[event] = text
            child = {"type": "command", "command": text, "timeout": 3 if mode == "prime" else 20}
            if event == "UserPromptSubmit":
                child["additionalContextLimit"] = 900
            group = {"hooks": [child]}
            if event == "SessionStart":
                group["matcher"] = "startup|resume|clear|compact"
            groups = current.get("hooks", {}).get(event, [])
            if not isinstance(groups, list):
                raise ValueError("Existing native hook groups have an invalid shape")
            found = [g for g in groups if any(h.get("command") == text for h in g.get("hooks", []))]
            if not found:
                edits.append({"keyPath": "hooks." + event, "value": [*groups, group], "mergeStrategy": "replace"})
            elif len(found) != 1 or found[0] != group:
                raise ValueError("An owner-edited BORG hook needs reconciliation")
        result = rpc.call("config/batchWrite", {"edits": edits, "filePath": str(target),
                           "expectedVersion": layer.get("version"), "reloadUserConfig": True})
        if result.get("status") != "ok":
            raise RuntimeError("Native client configuration write was not accepted")
        rows = native.hooks_data(rpc.call("hooks/list", {"cwd": str(profile)}))
        selected = [r for r in rows if r.get("command") in commands.values()
                    and Path(str(r.get("sourcePath", ""))).resolve() == target.resolve()]
        if len(selected) != 4:
            raise RuntimeError("The native provider did not discover all four BORG lifecycle hooks")
        layer = native.raw_user_layer(rpc.call("config/read", {"includeLayers": True}), target)
        state = dict(layer["config"].get("hooks", {}).get("state", {}))
        for row in selected:
            if not row.get("key") or not str(row.get("currentHash", "")).startswith("sha256:"):
                raise RuntimeError("A native hook has no verifiable identity")
            state[row["key"]] = {"trusted_hash": row["currentHash"]}
        result = rpc.call("config/batchWrite", {"edits": [{"keyPath": "hooks.state", "value": state,
                            "mergeStrategy": "replace"}], "filePath": str(target),
                           "expectedVersion": layer.get("version"), "reloadUserConfig": True})
        if result.get("status") != "ok":
            raise RuntimeError("Native hook trust write was not accepted")
        rows = native.hooks_data(rpc.call("hooks/list", {"cwd": str(profile)}))
        for expected in selected:
            actual = next((r for r in rows if r.get("key") == expected["key"]), {})
            if actual.get("trustStatus") != "trusted" or actual.get("currentHash") != expected["currentHash"]:
                raise RuntimeError("Native hook trust readback failed")
        receipt = {"state": "configured", "profile": str(profile), "hooks": len(selected),
                   "native_trust_verified": True, "mcp_server": "borg", "account_credentials_imported": False}
        path = root / "conductors/primary/borg-client-receipt.json"
        config.write_private(path, json.dumps(receipt, indent=2) + "\n", replace=path.exists())
        return receipt
    finally:
        if rpc is not None:
            rpc.close()
        if previous_path is None:
            os.environ.pop("PATH", None)
        else:
            os.environ["PATH"] = previous_path
