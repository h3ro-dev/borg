#!/usr/bin/env python3
"""Verify that a connection exposes the required BORG owner tool surface."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path

from fastmcp import Client
from fastmcp.client.auth import BearerAuth

from runtime_paths import borg_home, loopback_mcp_url

DEFAULT_URL = os.environ.get("BORG_MCP_URL", "http://127.0.0.1:18766/mcp")
DEFAULT_AUTH = Path(os.environ.get(
    "BORG_AUTHORIZATION_FILE",
    str(borg_home() / "borg-context/private/authorization"),
))
REQUIRED = {
    "borg_status",
    "borg_search",
    "borg_project_context",
    "borg_remember",
    "borg_tool_search",
    "computer_read_file",
    "computer_write_file",
    "computer_edit_block",
    "computer_start_process",
    "computer_read_process_output",
}


def token_from_file(path: Path) -> str:
    from borg_context_server import private_text
    value = private_text(path)
    if value.startswith("Bearer "):
        value = value[7:]
    if len(value) < 32 or any(ch.isspace() for ch in value):
        raise RuntimeError("BORG authorization file is invalid")
    return value


async def check(url: str, auth_file: Path) -> dict:
    loopback_mcp_url(url)
    token = token_from_file(auth_file)
    async with Client(url, auth=BearerAuth(token), timeout=20) as client:
        tools = await client.list_tools()
        names = {tool.name for tool in tools}
        missing = sorted(REQUIRED - names)
        status = await client.call_tool("borg_status", {}) if "borg_status" in names else None
        status_text = "\n".join(
            part.text for part in (status.content if status else []) if hasattr(part, "text")
        )
        return {
            "ok": not missing and bool(status) and not status.is_error,
            "url": url,
            "required_tool_count": len(REQUIRED),
            "exposed_tool_count": len(names),
            "missing_tools": missing,
            "borg_status_ok": bool(status) and not status.is_error,
            "borg_status": json.loads(status_text) if status_text else None,
        }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--authorization-file", type=Path, default=DEFAULT_AUTH)
    args = parser.parse_args()
    result = asyncio.run(check(args.url, args.authorization_file))
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
