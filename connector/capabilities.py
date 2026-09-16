"""Versioned, secret-free capability discovery for the BORG connector."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

MANIFEST_VERSION = "borg-capabilities/v1"
SERVER_API_VERSION = "2026-09-16"


def schema_fingerprint(tool_schemas: list[dict[str, Any]]) -> str:
    """Hash the wire contract, including argument changes under the same name."""
    fields = ("name", "description", "inputSchema", "outputSchema", "annotations")
    rows = [{key: row[key] for key in fields if key in row} for row in tool_schemas]
    rows.sort(key=lambda row: row["name"])
    canonical = json.dumps(rows, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _tool_entry(name: str, *, read_only: bool, destructive: bool,
                auth: str = "borg:operate", timeout_ms: int = 15_000,
                open_world: bool = False) -> dict[str, Any]:
    return {
        "name": name,
        "read_only": read_only,
        "destructive": destructive,
        "auth": auth,
        "timeout_ms": timeout_ms,
        "open_world": open_world,
    }


def build_manifest(settings, tool_schemas: list[dict[str, Any]]) -> dict[str, Any]:
    """Build a bounded manifest from the actual server configuration.

    The manifest reports only capability metadata. It intentionally excludes
    paths to credential files, tokens, browser profiles and process arguments.
    """
    from computer_tools import DESCRIPTIONS, DESTRUCTIVE, READS
    from catalog_status import load_catalog_status

    exposed_tool_names = [row["name"] for row in tool_schemas]

    tools: list[dict[str, Any]] = []
    core_read = {"borg_status", "borg_search", "borg_projects", "borg_project_context",
                 "borg_tool_search", "borg_operation_status", "borg_operations_recent",
                 "borg_capabilities", "borg_identity"}
    for name in exposed_tool_names:
        if name in core_read:
            tools.append(_tool_entry(name, read_only=True, destructive=False,
                                     timeout_ms=30_000 if name.startswith("borg_") else 15_000,
                                     open_world=name in {"borg_search", "borg_projects", "borg_tool_search"}))
        elif name == "borg_remember":
            tools.append(_tool_entry(name, read_only=False, destructive=False, timeout_ms=30_000))
        elif name == "borg_forget":
            tools.append(_tool_entry(name, read_only=False, destructive=True, timeout_ms=30_000))
        elif name.startswith("computer_"):
            short = name.removeprefix("computer_")
            if short not in DESCRIPTIONS:
                continue
            tools.append(_tool_entry(name, read_only=short in READS,
                                     destructive=short in DESTRUCTIVE,
                                     timeout_ms=30_000 if short in {"start_process", "read_process_output"} else 15_000,
                                     open_world=short in {"read_file", "start_process", "interact_with_process"}))
        elif name.startswith("job_"):
            short = name.removeprefix("job_")
            tools.append(_tool_entry(name, read_only=short in {"status", "read_output", "list"},
                                     destructive=short == "cancel", timeout_ms=30_000,
                                     open_world=short == "start"))
        elif name.startswith("artifact_"):
            short = name.removeprefix("artifact_")
            tools.append(_tool_entry(name, read_only=short in {"read", "list"},
                                     destructive=False, timeout_ms=15_000))
        elif name.startswith("browser_"):
            short = name.removeprefix("browser_")
            tools.append(_tool_entry(name, read_only=short in {"list_sessions", "snapshot", "screenshot", "list_tabs"},
                                     destructive=short in {"click", "type", "upload", "close_session"},
                                     timeout_ms=30_000, open_world=short in {"navigate", "click", "type", "upload"}))
        elif name.startswith("remote_"):
            short = name.removeprefix("remote_")
            tools.append(_tool_entry(name, read_only=short in {"list_hosts", "status", "read_output"},
                                     destructive=short in {"start", "cancel"}, timeout_ms=30_000,
                                     open_world=short in {"start", "cancel"}))
        elif name.startswith("fleet_"):
            tools.append(_tool_entry(name, read_only=name != "fleet_call",
                                     destructive=name == "fleet_call", timeout_ms=120_000,
                                     open_world=True))
        elif name.startswith("ui_"):
            short = name.removeprefix("ui_")
            tools.append(_tool_entry(name, read_only=short in {"permissions", "list_apps", "list_windows", "capture"},
                                     destructive=short in {"launch", "quit", "click", "type"}, timeout_ms=30_000,
                                     open_world=short in {"launch", "click", "type"}))
        elif name.startswith("credential_"):
            short = name.removeprefix("credential_")
            tools.append(_tool_entry(name, read_only=True, destructive=False, timeout_ms=15_000,
                                     open_world=short == "handoff"))

    # The registered wire annotations are authoritative over category defaults.
    by_name = {row["name"]: row for row in tool_schemas}
    for tool in tools:
        annotations = by_name[tool["name"]].get("annotations") or {}
        for key, wire_key in (("read_only", "readOnlyHint"),
                              ("destructive", "destructiveHint"),
                              ("open_world", "openWorldHint")):
            if wire_key in annotations:
                tool[key] = annotations[wire_key]
    return {
        "manifest_version": MANIFEST_VERSION,
        "server_api_version": SERVER_API_VERSION,
        "tool_schema_sha256": schema_fingerprint(tool_schemas),
        "tool_schema_hash_version": "mcp-wire-contract/v1",
        "tool_count": len(exposed_tool_names),
        "tools": sorted(tools, key=lambda row: row["name"]),
        "domains": {
            "memory": {"status": "configured", "scope": "owner_all", "probe": "borg_status"},
            "computer": {"status": "configured" if settings.computer else "disabled", "backend": "native"},
            "browser": {"status": "configured" if getattr(settings, "browser", {}) else "disabled", "backend": "native"},
            "jobs": {"status": "configured" if settings.computer else "disabled", "backend": "native"},
            "remote": {"status": "configured" if getattr(settings, "remote", {}) else "not_configured", "backend": "ssh", "probe": "remote_list_hosts"},
            "fleet": {"status": "configured" if getattr(settings, "fleet", {}) else "not_configured", "backend": "ssh-stdio", "probe": "fleet_hosts"},
            "ui": {"status": "configured" if getattr(settings, "ui", {}) else "not_configured", "backend": "native_os", "probe": "ui_permissions"},
            "credentials": {"status": "configured" if getattr(settings, "credentials", {}) else "not_configured", "backend": "value_blind", "probe": "credential_list"},
        },
        "account_catalog": load_catalog_status(getattr(settings, "account_catalog", {})),
        "notices": [
            "Configured means tools are mounted, not that their dependency is healthy or an action has completed. Use the listed probe and verify the native result.",
            "Credential values, cookies, browser profiles and command arguments are never returned by this manifest.",
            "A web app may require an administrator tool-snapshot refresh after schema changes.",
        ],
    }
