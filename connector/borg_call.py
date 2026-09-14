#!/usr/bin/env python3
from __future__ import annotations
import asyncio, json, sys
from borg_connection_selfcheck import DEFAULT_URL, DEFAULT_AUTH, token_from_file
from fastmcp import Client
from fastmcp.client.auth import BearerAuth

async def main():
    if len(sys.argv) != 3:
        raise SystemExit("usage: borg_call.py TOOL JSON_ARGS")
    tool, raw = sys.argv[1], sys.argv[2]
    args = json.loads(raw)
    async with Client(DEFAULT_URL, auth=BearerAuth(token_from_file(DEFAULT_AUTH)), timeout=120) as client:
        result = await client.call_tool(tool, args)
        text = "\n".join(part.text for part in result.content if hasattr(part, "text"))
        print(text)
        if result.is_error:
            raise SystemExit(2)

if __name__ == "__main__":
    asyncio.run(main())
