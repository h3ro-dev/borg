"""Fleet routing proves target isolation, lifecycle, custody and no replay."""
import asyncio
from copy import deepcopy
import dataclasses
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

from fastmcp import Client
from fastmcp.exceptions import ToolError
from borg_context_server import Settings, build_server
from fleet_tools import Fleet, read_registry, validate_registry, ssh_arguments
from computer_tools import BoundaryMiddleware


class FleetTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path.home())
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        self.auth = patch("borg_context_server.authorize", lambda: None)
        self.auth.start()
        self.addCleanup(self.auth.stop)
        self.rows, self.servers = [], {}
        for name in ("alpha", "beta"):
            home = self.root / name
            home.mkdir(mode=0o700)
            identity = {"instance_id": str(uuid.uuid4()), "owner": name, "home": str(home)}
            row = {"id": name, "ssh_alias": name, "enabled": True, **identity}
            self.rows.append(row)
            for file, text in [("authorization", "Bearer " + "a" * 48), ("upstream", "b" * 48)]:
                (home / file).write_text(text)
                (home / file).chmod(0o600)
            settings = Settings(home / "authorization", home / "upstream", (home,),
                                {"backend": "native"}, state_root=home, identity=identity)
            self.servers[name] = build_server(settings)
        self.registry = self.root / "registry.json"
        self.save()
        self.fleet = Fleet(self.registry, lambda row: Client(self.servers[row["id"]]))
        self.addAsyncCleanup(self.fleet.close)

    def save(self):
        self.registry.write_text(json.dumps({"schema": "borg-fleet/v1", "hosts": self.rows}))
        self.registry.chmod(0o600)

    async def test_actual_discovery_and_two_target_writes(self):
        page = await self.fleet.fleet_hosts()
        self.assertEqual([r["state"] for r in page["hosts"]], ["reachable", "reachable"])
        schemas = await self.fleet.fleet_tools("alpha", "computer_write")
        self.assertIn("computer_write_file", [r["name"] for r in schemas["tools"]])
        async def write(row):
            target = Path(row["home"]) / "canary.txt"
            result = await self.fleet.fleet_call(row["id"], "computer_write_file",
                                               {"path": str(target), "content": row["id"]})
            self.assertFalse(result.is_error, result.content)
            self.assertEqual(result.meta["borg_fleet"]["identity"]["instance_id"], row["instance_id"])
            self.assertTrue(result.meta["borg_fleet"]["target_receipt"])
            self.assertEqual(target.read_text(), row["id"])
        await asyncio.gather(*(write(row) for row in self.rows))

    async def test_wrong_identity_disabled_unknown_and_recursion_fail_without_write(self):
        self.rows[0]["instance_id"] = str(uuid.uuid4())
        self.rows[1]["enabled"] = False
        self.save()
        target = self.root / "must-not-exist"
        result = await self.fleet.fleet_call("alpha", "computer_write_file", {"path": str(target), "content": "bad"})
        self.assertTrue(result.is_error)
        self.assertEqual(result.meta["borg_fleet"]["state"], "not_started")
        for host, tool in [("beta", "computer_write_file"), ("missing", "computer_write_file"), ("alpha", "fleet_call")]:
            with self.assertRaises(ToolError):
                await self.fleet.fleet_call(host, tool, {})
        self.assertFalse(target.exists())

    async def test_target_restart_guard_refuses_stale_generation(self):
        row = self.rows[0]
        async with Client(self.servers["alpha"]) as client:
            identity = (await client.call_tool("borg_identity", {})).structured_content
            expected = {k: identity[k] for k in ("instance_id", "owner", "home", "server_generation")}
            expected["server_generation"] = str(uuid.uuid4())
            target = self.root / "guarded"
            result = await client.call_tool_mcp("computer_write_file", {"path": str(target), "content": "bad"},
                                                meta={"borg_target_identity": expected})
            self.assertTrue(result.isError)
            self.assertFalse(target.exists())

    async def test_native_process_survives_separate_transport_contexts(self):
        result = await self.fleet.fleet_call("alpha", "computer_start_process",
                                            {"command": "printf fleet-process", "timeout_ms": 1000})
        self.assertFalse(result.is_error, result.content)
        data = result.structured_content
        self.assertIn("pid", data)
        read = await self.fleet.fleet_call("alpha", "computer_read_process_output", {"pid": data["pid"]})
        self.assertFalse(read.is_error, read.content)

    async def test_transport_loss_reports_unknown_without_replay(self):
        calls = []
        row = self.rows[0]
        class LostClient:
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            async def close(self): pass
            async def call_tool(self, *args, **kwargs):
                return SimpleNamespace(structured_content={**row, "server_generation": str(uuid.uuid4())})
            async def list_tools(self, **kwargs):
                return [SimpleNamespace(name="computer_write_file", model_dump=lambda **kw: {"name": "computer_write_file"})]
            async def call_tool_mcp(self, *args, **kwargs):
                calls.append(args)
                raise ConnectionError("simulated lost response after dispatch")
        fleet = Fleet(self.registry, lambda row: LostClient())
        self.addAsyncCleanup(fleet.close)
        result = await fleet.fleet_call("alpha", "computer_write_file", {})
        self.assertEqual(result.meta["borg_fleet"]["state"], "outcome_unknown")
        self.assertEqual(len(calls), 1)

    async def test_registry_rejects_unsafe_routes_duplicates_and_custody(self):
        for change in [{"ssh_alias": "-oProxyCommand=bad"}, {"home": "/tmp/../target"},
                       {"enabled": 1}, {"token": "forbidden"}]:
            with self.subTest(change=change), self.assertRaises((ValueError, TypeError)):
                validate_registry({"schema": "borg-fleet/v1", "hosts": [{**self.rows[0], **change}]})
        with self.assertRaises(ValueError):
            validate_registry({"schema": "borg-fleet/v1", "hosts": [self.rows[0], self.rows[0]]})
        self.registry.chmod(0o644)
        with self.assertRaises(ValueError): read_registry(self.registry)
        self.registry.chmod(0o600)
        link = self.root / "link"
        link.symlink_to(self.registry)
        with self.assertRaises(ValueError): read_registry(link)
        fifo = self.root / "fifo"
        os.mkfifo(fifo, 0o600)
        with self.assertRaises(ValueError): read_registry(fifo)
        self.assertIn("StrictHostKeyChecking=yes", ssh_arguments(self.rows[0]))
        self.assertTrue(BoundaryMiddleware._receipt_worthy("fleet_call"))
        self.assertEqual(BoundaryMiddleware._lane_name("fleet_call"), "fleet")


if __name__ == "__main__":
    unittest.main()
