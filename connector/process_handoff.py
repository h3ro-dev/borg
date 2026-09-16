"""Preserve old resident process/search handles during an explicit rolling update.

The owner config pins a loopback predecessor of the same installation. Only
existing handle operations are forwarded; new work always uses the new runtime.
There is no automatic retry, process takeover, or alternative credential.
"""
from __future__ import annotations

import asyncio
from functools import wraps
import inspect
from pathlib import Path
import uuid

from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
import httpx
from fastmcp.exceptions import ToolError
from fastmcp.tools import ToolResult
from runtime_paths import loopback_mcp_url

PROCESS = {"read_process_output", "interact_with_process", "force_terminate"}
SEARCH = {"get_more_search_results", "stop_search"}
LISTS = {"list_sessions": "sessions", "list_searches": "searches"}


def local_http_client(**kwargs):
    """The owner bearer must go directly to the pinned loopback endpoint."""
    kwargs.update(trust_env=False, follow_redirects=False)
    return httpx.AsyncClient(**kwargs)


class ComputerHandoff:
    def __init__(self, native, config, identity, authorization_file, client_factory=None):
        required = {"url", "instance_id", "owner", "home", "server_generation"}
        if not isinstance(config, dict) or set(config) != required:
            raise ValueError("Computer handoff requires an exact predecessor URL and identity")
        self.url = loopback_mcp_url(config["url"])
        self.identity = {k: config[k] for k in required - {"url"}}
        if not identity or any(config[k] != identity.get(k) for k in ("instance_id", "owner", "home")):
            raise ValueError("Computer handoff must preserve this installation's owner and identity")
        if str(uuid.UUID(config["server_generation"])) != config["server_generation"]:
            raise ValueError("Computer handoff requires a canonical predecessor generation")
        self.native = native
        self.authorization_file = Path(authorization_file)
        self._factory = client_factory
        # Keep the old, descriptor-constrained process responsive while it drains.
        self._slots = asyncio.Semaphore(4)

    async def _call(self, name, arguments):
        return await self._forward("computer_" + name, arguments)

    async def _forward(self, tool_name, arguments):
        from borg_context_server import secret
        factory = self._factory or (lambda: Client(StreamableHttpTransport(self.url,
            auth=secret(self.authorization_file, authorization=True),
            httpx_client_factory=local_http_client), timeout=30))
        dispatched = False
        try:
            async with self._slots, asyncio.timeout(35), factory() as client:
                observed = (await client.call_tool("borg_identity", {})).structured_content
                if not isinstance(observed, dict) or any(observed.get(k) != v for k, v in self.identity.items()):
                    raise ToolError("BORG predecessor identity changed; operation was not started")
                dispatched = True
                result = await client.call_tool_mcp(tool_name, arguments,
                    meta={"borg_target_identity": self.identity})
                return ToolResult(content=result.content, structured_content=result.structuredContent,
                    is_error=result.isError, meta=result.meta)
        except asyncio.CancelledError:
            raise
        except Exception:
            state = "outcome_unknown" if dispatched else "not_started"
            return ToolResult(content="BORG predecessor call " + state +
                "; preserve the old runtime and inspect the original operation before retrying.",
                is_error=True, meta={"borg_handoff": {"state": state}})

    def store_function(self, store, tool_name, function):
        """Retain the resident owner of job and browser handles across rollout."""
        signature = inspect.signature(function)
        key = "session_id" if tool_name.startswith("browser_") else "job_id"
        if key not in signature.parameters and tool_name != "job_list":
            return function
        processes = store.jobs.processes if tool_name.startswith("remote_") else store.processes

        @wraps(function)
        async def call(*args, **kwargs):
            arguments = dict(signature.bind(*args, **kwargs).arguments)
            if tool_name == "job_list":
                # Both stores see the same files, but only their resident Popen
                # can prove the exit status. Never persist the other owner's state.
                current = await asyncio.to_thread(function, *args, **kwargs)
                previous = await self._forward(tool_name, arguments)
                rows = [row for row in current["jobs"] if row["job_id"] in processes]
                if previous.is_error:
                    return {"jobs": rows, "predecessor_state": "unavailable"}
                rows += [dict(row, predecessor=True) for row in
                         (previous.structured_content or {}).get("jobs", [])
                         if row["job_id"] not in processes]
                limit = max(1, min(int(arguments.get("limit", 20)), 50))
                rows.sort(key=lambda row: row.get("started_at") or "", reverse=True)
                return {"jobs": rows[:limit], "predecessor_state": "connected"}
            # Native stores accept equivalent UUID spellings. Resolve ownership
            # using the same canonical key before choosing a resident runtime.
            try:
                handle = str(uuid.UUID(arguments[key]))
            except (ValueError, TypeError, AttributeError):
                handle = arguments[key]
            if handle in processes:
                return await asyncio.to_thread(function, *args, **kwargs)
            return await self._forward(tool_name, arguments)

        return call

    def __getattr__(self, name):
        function = getattr(self.native, name)
        if name not in PROCESS | SEARCH | LISTS.keys():
            return function
        signature = inspect.signature(function)

        @wraps(function)
        async def call(*args, **kwargs):
            arguments = dict(signature.bind(*args, **kwargs).arguments)
            if name in LISTS:
                current = await asyncio.to_thread(function, *args, **kwargs)
                previous = await self._call(name, arguments)
                if previous.is_error:
                    return {**current, "predecessor_state": "unavailable",
                            "notice": "Older sessions may still be running on the preserved predecessor."}
                key = LISTS[name]
                identity_key = "pid" if key == "sessions" else "search_id"
                old = (previous.structured_content or {}).get(key, [])
                seen = {row[identity_key] for row in current[key]}
                return {key: current[key] + [dict(row, predecessor=True) for row in old
                        if row[identity_key] not in seen], "predecessor_state": "connected"}
            registry, key = (self.native._processes, "pid") if name in PROCESS else (self.native._searches, "search_id")
            with self.native._registry_lock:
                owned = arguments[key] in registry
            if owned:
                return await asyncio.to_thread(function, *args, **kwargs)
            return await self._call(name, arguments)

        return call
