"""Authentication, native schema and conservative desktop permission checks."""
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from mcp.types import Tool
from desktop_tools import TOOLS, READS, NOTICE, describe
from borg_context_server import Settings, build_server
from test_connector import ConnectorTests
from starlette.testclient import TestClient


class DesktopContractTests(unittest.TestCase):
    def test_annotation_never_advertises_conditional_or_capture_tools_as_reads(self):
        schema = {"type": "object", "properties": {"action": {"type": "string"}}}
        for name in TOOLS:
            tool = Tool(name="desktop_" + name, description="Native contract", inputSchema=schema)
            describe(tool)
            describe(tool)
            self.assertEqual(tool.inputSchema, schema)
            self.assertEqual(tool.annotations.readOnlyHint, name in READS)
            self.assertEqual(tool.annotations.destructiveHint, name not in READS)
            self.assertEqual(tool.description.count(NOTICE), 1)
        self.assertFalse(TOOLS & {"agent", "analyze", "clipboard", "sleep"})


class DesktopUnavailableTests(ConnectorTests):
    def test_unavailable_optional_driver_preserves_authenticated_core(self):
        self.client.__exit__(None, None, None)
        settings = Settings(self.inbound, self.upstream, (), {},
                            {"command": "/usr/bin/false", "bridge_socket": str(self.root / "missing.sock")})
        self.client = TestClient(build_server(settings).http_app(stateless_http=True, json_response=True))
        self.client.__enter__()
        self.assertEqual(self.rpc("tools/list", credential=None).status_code, 401)
        rows = self.rpc("tools/list").json()["result"]["tools"]
        self.assertIn("borg_search", {x["name"] for x in rows})
        self.assertFalse(self.call("borg_search", {"query": "canary"}).get("isError", False))


class SyntheticDesktopClient:
    fail_on = "discovery"
    sensitive = "a" * 48 + " ordinary private synthetic document text"

    def __init__(self, *args, **kwargs):
        # Match the native Client interface used for browser transport cleanup.
        self.transport = args[0]

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def list_tools(self):
        if self.fail_on == "discovery":
            raise RuntimeError(self.sensitive)
        return [Tool(name="click", inputSchema={"type": "object", "properties": {}, "additionalProperties": False})]

    async def call_tool_mcp(self, *args, **kwargs):
        raise RuntimeError(self.sensitive)


class DesktopErrorPrivacyTests(ConnectorTests):
    def _use_synthetic_backend(self, failure):
        self.client.__exit__(None, None, None)
        SyntheticDesktopClient.fail_on = failure
        replacement = patch("desktop_tools.Client", SyntheticDesktopClient)
        replacement.start()
        self.addCleanup(replacement.stop)
        settings = Settings(self.inbound, self.upstream, (), {},
                            {"command": "/usr/bin/false", "bridge_socket": str(self.root / "synthetic.sock")})
        self.client = TestClient(build_server(settings).http_app(stateless_http=True, json_response=True))
        self.client.__enter__()

    def test_discovery_exception_is_withheld_from_logs_and_http(self):
        self._use_synthetic_backend("discovery")
        with self.assertLogs("fastmcp", level="WARNING") as captured:
            response = self.rpc("tools/list")
        combined = response.text + "\n".join(captured.output)
        self.assertNotIn("a" * 48, combined)
        self.assertNotIn("ordinary private synthetic document text", combined)
        self.assertIn("borg_search", response.text)
        self.assertIn("payload details withheld", "\n".join(captured.output))

    def test_call_exception_traceback_is_withheld_from_logs_and_http(self):
        self._use_synthetic_backend("call")
        with self.assertLogs("fastmcp", level="ERROR") as captured:
            result = self.call("desktop_click", {})
        self.assertTrue(result.get("isError"))
        combined = json.dumps(result) + "\n".join(captured.output)
        self.assertNotIn("a" * 48, combined)
        self.assertNotIn("ordinary private synthetic document text", combined)
        self.assertTrue(all(record.exc_info is None and record.exc_text is None for record in captured.records))


@unittest.skipUnless(os.environ.get("BORG_TEST_DESKTOP_CONFIG"), "native desktop backend not selected")
class NativeDesktopTests(ConnectorTests):
    def setUp(self):
        super().setUp()
        self.client.__exit__(None, None, None)
        desktop = json.loads(Path(os.environ["BORG_TEST_DESKTOP_CONFIG"]).read_text())
        self.client = TestClient(build_server(Settings(self.inbound, self.upstream, (), {}, desktop))
                                .http_app(stateless_http=True, json_response=True))
        self.client.__enter__()

    def test_native_catalog_and_permissions(self):
        self.assertEqual(self.rpc("tools/list", credential=None).status_code, 401)
        rows = self.rpc("tools/list").json()["result"]["tools"]
        names = {x["name"].removeprefix("desktop_") for x in rows if x["name"].startswith("desktop_")}
        self.assertEqual(names, TOOLS)
        result = self.call("desktop_permissions", {})
        # Missing OS grants are an expected native result, not fabricated readiness.
        self.assertIn("permission", json.dumps(result).lower())
        self.assertEqual(self.rpc("resources/list").json()["result"]["resources"], [])


if __name__ == "__main__":
    unittest.main()
