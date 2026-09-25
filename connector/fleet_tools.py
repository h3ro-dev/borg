"""Explicit, identity-pinned native MCP routing over the owner's existing SSH.

SSH bridges are reusable transports. Native state and credentials stay in the
resident connector on each target. No action is automatically retried.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shlex
import stat
import uuid

from fastmcp import Client
from fastmcp.client.transports import StdioTransport
from fastmcp.exceptions import ToolError
from fastmcp.tools import ToolResult

SCHEMA = "borg-fleet/v1"
MAX_REGISTRY_BYTES = 262144
READ = {"readOnlyHint": True, "destructiveHint": False,
        "idempotentHint": True, "openWorldHint": True}


class FleetPreflightError(ToolError):
    """A local refusal before dispatch, not an interpretation of target text."""
    def __init__(self, code: str):
        if code not in {"bad_request", "identity_mismatch", "capability_unavailable"}:
            raise ValueError("invalid fleet preflight code")
        self.code = code
        super().__init__(f"BORG_{code.upper()}: fleet preflight refused; no operation was started")


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def identity_config(value: dict) -> dict:
    if not isinstance(value, dict) or set(value) != {"instance_id", "owner", "home"}:
        raise ValueError("BORG identity requires instance_id, owner and home")
    if str(uuid.UUID(value["instance_id"])) != value["instance_id"]:
        raise ValueError("BORG instance identity must be a canonical UUID")
    if not isinstance(value["owner"], str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,47}", value["owner"]):
        raise ValueError("BORG owner identity is invalid")
    absolute_path(value["home"])
    return dict(value)


def absolute_path(value: str) -> str:
    if (not isinstance(value, str) or len(value) > 4096 or not value.startswith("/")
            or value == "/" or any(ord(c) < 32 or ord(c) == 127 for c in value)
            or any(part in {".", "..", ""} for part in value[1:].split("/"))):
        raise ValueError("Fleet paths must be absolute, normalized paths without controls")
    return value


def validate_registry(doc: dict) -> dict:
    if (not isinstance(doc, dict) or set(doc) != {"schema", "hosts"} or doc["schema"] != SCHEMA
            or not isinstance(doc["hosts"], list) or len(doc["hosts"]) > 128):
        raise ValueError("Invalid BORG fleet registry")
    ids, targets = set(), set()
    for row in doc["hosts"]:
        required = {"id", "ssh_alias", "home", "instance_id", "owner", "enabled"}
        if not isinstance(row, dict) or not required <= set(row) or set(row) - required - {"label", "roles"}:
            raise ValueError("Invalid fleet host fields")
        if not isinstance(row["id"], str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,47}", row["id"]):
            raise ValueError("Invalid fleet host ID")
        if not isinstance(row["ssh_alias"], str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", row["ssh_alias"]):
            raise ValueError("Fleet SSH target must be a configured alias, not options or a command")
        identity_config({k: row[k] for k in ("instance_id", "owner", "home")})
        if type(row["enabled"]) is not bool:
            raise ValueError("Fleet enabled must be a boolean")
        label = row.get("label", row["id"])
        if not isinstance(label, str) or not 1 <= len(label) <= 80 or any(ord(c) < 32 for c in label):
            raise ValueError("Invalid fleet label")
        roles = row.get("roles", [])
        if (not isinstance(roles, list) or len(roles) > 16 or any(
                not isinstance(role, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,47}", role) for role in roles)):
            raise ValueError("Fleet roles must be short owner-defined labels")
        target = (row["ssh_alias"], row["home"])
        if row["id"] in ids or target in targets:
            raise ValueError("Duplicate fleet host ID or target")
        ids.add(row["id"])
        targets.add(target)
    return doc


def read_registry(path: Path) -> dict:
    if not path.is_absolute() or any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError("Fleet registry must be an absolute private file without symlinks")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_nlink != 1 or info.st_size > MAX_REGISTRY_BYTES):
            raise ValueError("Fleet registry ownership, permissions or size is unsafe")
        raw = handle.read(MAX_REGISTRY_BYTES + 1)
    if len(raw) > MAX_REGISTRY_BYTES:
        raise ValueError("Fleet registry is too large")
    return validate_registry(json.loads(raw))


def ssh_arguments(row: dict) -> list[str]:
    command = shlex.join([row["home"] + "/bin/borg", "mcp-stdio", "--home", row["home"]])
    return ["-T", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
            "-o", "ConnectTimeout=5", "-o", "ServerAliveInterval=15",
            "-o", "ServerAliveCountMax=2", "--", row["ssh_alias"], command]


class Fleet:
    def __init__(self, registry: Path, client_factory=None):
        self.registry = registry
        self._clients = {}
        self._factory = client_factory or self._new_client

    @staticmethod
    def _new_client(row):
        # Protocol stdout is private to MCP. Do not forward arbitrary SSH or
        # target startup diagnostics, which can include local consumer paths.
        return Client(StdioTransport("/usr/bin/ssh", ssh_arguments(row),
                                     keep_alive=True, log_file=Path(os.devnull)), timeout=100,
                      init_timeout=15, name="borg-fleet-" + row["id"])

    def host(self, host: str) -> dict:
        rows = read_registry(self.registry)["hosts"]
        row = next((r for r in rows if r["id"] == host), None)
        if row is None or not row["enabled"]:
            raise FleetPreflightError("capability_unavailable")
        return row

    @asynccontextmanager
    async def connection(self, row):
        key = json.dumps(row, sort_keys=True)
        peer = self._clients.get(row["id"])
        if peer is None or peer["key"] != key:
            old = peer
            peer = {"key": key, "client": self._factory(row), "users": 0}
            self._clients[row["id"]] = peer
            if old and old["users"] == 0:
                await old["client"].close()
        peer["users"] += 1
        try:
            async with peer["client"] as client:
                yield client
        finally:
            peer["users"] -= 1
            if self._clients.get(row["id"]) is not peer and peer["users"] == 0:
                await peer["client"].close()

    async def close(self):
        await asyncio.gather(*(p["client"].close() for p in self._clients.values()), return_exceptions=True)
        self._clients.clear()

    @staticmethod
    async def verify(client, row) -> dict:
        result = await client.call_tool("borg_identity", {}, timeout=15)
        identity = result.structured_content
        if not isinstance(identity, dict):
            raise FleetPreflightError("capability_unavailable")
        if any(identity.get(k) != row[k] for k in ("instance_id", "owner", "home")):
            raise FleetPreflightError("identity_mismatch")
        return identity

    @staticmethod
    async def schemas(client) -> list[dict]:
        tools = await client.list_tools(max_pages=8)
        if len(tools) > 512:
            raise ToolError("Fleet target tool manifest exceeds the supported size")
        rows = [t.model_dump(mode="json", by_alias=True, exclude_none=True)
                for t in tools if not t.name.startswith("fleet_")]
        if len(json.dumps(rows).encode()) > 2_000_000:
            raise ToolError("Fleet target tool manifest exceeds the supported size")
        return rows

    async def fleet_hosts(self, offset: int = 0, limit: int = 20) -> dict:
        """List explicitly enrolled machines with fresh identity and native-tool discovery.

        Roles are owner labels, not proof of readiness or agent admission.
        Unavailable machines remain visible. No SSH aliases are auto-enrolled.
        """
        if offset < 0 or not 1 <= limit <= 20:
            raise ToolError("Use a nonnegative offset and a limit from 1 to 20")
        rows = read_registry(self.registry)["hosts"]
        semaphore = asyncio.Semaphore(4)

        async def probe(row):
            result = {"id": row["id"], "label": row.get("label", row["id"]),
                      "roles": row.get("roles", []), "enabled": row["enabled"], "observed_at": now()}
            if not row["enabled"]:
                return {**result, "state": "disabled", "identity": None}
            try:
                async with semaphore, asyncio.timeout(20), self.connection(row) as client:
                    identity = await self.verify(client, row)
                    schemas = await self.schemas(client)
                    return {**result, "state": "reachable", "identity": identity,
                            "tool_count": len(schemas),
                            "domains": sorted({s["name"].split("_")[0] for s in schemas})}
            except Exception:
                return {**result, "state": "unavailable", "identity": None,
                        "notice": "SSH, connector liveness or pinned identity was not verified"}

        return {"schema": SCHEMA, "hosts": await asyncio.gather(*(probe(r) for r in rows[offset:offset + limit])),
                "total": len(rows), "next_offset": offset + limit if offset + limit < len(rows) else None,
                "notice": "Reachable proves identity and tool discovery. Use native permission, dependency and conductor admission checks before work."}

    async def fleet_tools(self, host: str, prefix: str = "", offset: int = 0, limit: int = 20) -> dict:
        """Discover actual tool schemas on one identity-pinned machine. Paths and IDs belong to that target."""
        if offset < 0 or not 1 <= limit <= 20 or len(prefix) > 128:
            raise ToolError("Invalid fleet tool page")
        row = self.host(host)
        try:
            async with asyncio.timeout(20), self.connection(row) as client:
                identity = await self.verify(client, row)
                schemas = [s for s in await self.schemas(client) if s["name"].startswith(prefix)]
                return {"host": host, "identity": identity, "observed_at": now(),
                        "tools": schemas[offset:offset + limit], "total": len(schemas),
                        "next_offset": offset + limit if offset + limit < len(schemas) else None}
        except ToolError:
            raise
        except Exception:
            raise ToolError("Fleet discovery unavailable; no operation was started") from None

    async def fleet_call(self, host: str, tool: str, arguments: dict,
                         timeout_ms: int = 90000) -> ToolResult:
        """Call one native tool on an explicitly selected, identity-pinned machine.

        Discover its schema with fleet_tools first. Target credentials remain on
        that machine. No automatic retry: after an unknown result inspect native
        state and target borg_operations_recent before repeating a change.
        """
        from computer_tools import classify_failure
        try:
            valid = (isinstance(tool, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,127}", tool)
                     and not tool.startswith("fleet_") and type(timeout_ms) is int
                     and 1000 <= timeout_ms <= 90000 and isinstance(arguments, dict)
                     and len(json.dumps(arguments, allow_nan=False).encode()) <= 262144)
        except (TypeError, ValueError):
            valid = False
        if not valid:
            raise FleetPreflightError("bad_request")
        row = self.host(host)
        dispatched = False
        identity = None
        try:
            async with self.connection(row) as client:
                async with asyncio.timeout(20):
                    identity = await self.verify(client, row)
                    schemas = await self.schemas(client)
                    if tool not in {s["name"] for s in schemas}:
                        raise FleetPreflightError("capability_unavailable")
                dispatched = True
                async with asyncio.timeout(timeout_ms / 1000):
                    result = await client.call_tool_mcp(tool, arguments, timeout=timeout_ms / 1000,
                        meta={"borg_target_identity": {k: identity[k] for k in
                            ("instance_id", "owner", "home", "server_generation")}})
                if len(result.model_dump_json().encode()) > 16_000_000:
                    raise ValueError("Fleet result is too large")
                return ToolResult(content=result.content, structured_content=result.structuredContent,
                                  is_error=result.isError,
                                  meta={**(result.meta or {}), "borg_fleet": {
                                      "host": host, "tool": tool, "identity": identity, "observed_at": now(),
                                      "state": "outcome_unknown" if result.isError else "succeeded",
                                      "phase": "result",
                                      "failure_code": classify_failure(result.model_dump()) if result.isError else None,
                                      "target_receipt": (result.meta or {}).get("borg_operation_receipt")}})
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            state = "outcome_unknown" if dispatched else "not_started"
            code = exc.code if isinstance(exc, FleetPreflightError) else classify_failure(exc)
            return ToolResult(content=f"Fleet call {state}. " + (
                "The target may still be working. Inspect its native state and borg_operations_recent before retrying."
                if dispatched else "Verify the enrolled identity, SSH route and target tool before retrying."),
                is_error=True, meta={"borg_fleet": {
                    "host": host, "tool": tool, "identity": identity, "state": state,
                    "phase": "dispatch" if dispatched else "preflight", "failure_code": code}})


def mount_fleet(server, fleet: Fleet):
    server.tool(annotations=READ)(fleet.fleet_hosts)
    server.tool(annotations=READ)(fleet.fleet_tools)
    server.tool(annotations={"readOnlyHint": False, "destructiveHint": True,
                             "idempotentHint": False, "openWorldHint": True})(fleet.fleet_call)
