"""Changed Inbox boundaries tested through real HTTP/client/MCP code.

Release-local copy of the upstream estate bridge test with the typed 413
output-bound expectation.
"""
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from comms.hub.service import HubService, TransportConfigurationError, write_credential_file
from comms.hub.client import InboxClient, ClientError
from comms.hub.cli import _mcp_tools, _handle_mcp


class BoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.allowed = True
        self.calls = []
        self.authorizations = []
        owner = self

        class Store:
            def authorize(self, actor, operation, scope):
                owner.authorizations.append((actor, operation, scope))
                return {"allowed": owner.allowed and actor == "test-caller"}
            def call(self, *args, **kwargs):
                raise AssertionError("estate reads must not enter Store.call")

        class Fleet:
            def read(self, scope):
                return {"schema": "fleet-context/v1", "active_agents": {
                    "native_ownership": {"state": "UNKNOWN", "coverage": {
                        "rows": {"total": 7, "returned": 1, "truncated": True}}}}}

        def factory(ownership):
            class Reader:
                def read(self, action, **params):
                    owner.calls.append((action, params))
                    if params.get("id") == "fail":
                        raise RuntimeError("private exception text")
                    if params.get("id") == "oversize":
                        return {"schema": "eco-estate/v1", "value": "x" * 262145}
                    return {"schema": "eco-estate/v1", "action": action,
                            "params": params, "ownership": ownership()}
            return Reader()

        self.service = HubService(store=Store(), credentials={"test-caller": "local-test-token-value-only"},
                                  fleet_context_reader=Fleet(), estate_reader_factory=factory)
        self.server = self.service.make_server(port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        credential = write_credential_file(self.root / "credential.json", "local-test-token-value-only", "test-caller")
        self.client = InboxClient(endpoint=f"http://127.0.0.1:{self.server.server_port}",
                                  credential_file=credential, outbox_path=self.root / "outbox.json")

    def tearDown(self):
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()
        self.temp.cleanup()

    def test_native_client_read_preserves_owned_work_truncation_without_outbox(self):
        response = self.client.call("estate.read", {"action": "context"})
        rows = response["ownership"]["active_agents"]["native_ownership"]["coverage"]["rows"]
        self.assertEqual(rows, {"total": 7, "returned": 1, "truncated": True})
        self.assertFalse((self.root / "outbox.json").exists())
        self.assertEqual(self.authorizations, [("test-caller", "estate.read", "/"),
                                              ("test-caller", "fleet.context", "/")])

    def test_current_and_revoked_authority_checked_each_call(self):
        self.client.call("estate.read", {"action": "context"})
        self.allowed = False
        with self.assertRaises(ClientError) as result:
            self.client.call("estate.read", {"action": "context"})
        self.assertEqual(result.exception.status, 403)
        self.assertEqual(len(self.calls), 1)

    def test_topology_200_is_scoped_to_estate_and_both_validators(self):
        self.assertEqual(self.client.call("estate.read", {"action": "topology", "limit": 200})["params"]["limit"], 200)
        for operation, params in [("agents.list", {"limit": 200}),
                                   ("estate.read", {"action": "changes", "limit": 200}),
                                   ("estate.read", {"action": "topology", "limit": 201})]:
            with self.assertRaises(ClientError):
                self.client.call(operation, params)
            with self.assertRaises(TransportConfigurationError):
                self.service.call("test-caller", operation, params)

    def test_invalid_input_does_not_reach_reader(self):
        for params in [{"action": "context", "actor": "owner"},
                       {"action": "context", "path": "/private"},
                       {"action": "history", "metrics": ["x"] * 5},
                       {"action": "history", "step": True},
                       {"action": "topology", "limit": -1},
                       {"action": "topology", "offset": {"limit": 200}},
                       {"action": "write"}]:
            with self.subTest(params=params), self.assertRaises(ClientError):
                self.client.call("estate.read", params)
        self.assertEqual(self.calls, [])

    def test_failures_are_bounded_and_sanitized(self):
        # A reader failure stays 503 estate_unavailable; an oversized result
        # is the typed 413 output_bound contract.
        expected = {"fail": (503, "estate_unavailable"), "oversize": (413, "output_bound")}
        for entity, (status, code) in expected.items():
            with self.assertRaises(ClientError) as result:
                self.client.call("estate.read", {"action": "entity", "id": entity})
            self.assertEqual(result.exception.status, status)
            self.assertEqual(result.exception.code, code)
            self.assertNotIn("private", str(result.exception))

    def test_mcp_discovery_and_generic_tool_use_same_client(self):
        self.assertIn("estate.read", {tool["name"] for tool in _mcp_tools()})
        with patch("comms.hub.cli._session_client", return_value=self.client):
            response = _handle_mcp({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                   "params": {"name": "inbox_call", "arguments": {
                                       "operation": "estate.read", "params": {"action": "topology", "limit": 200}}}}, self.client)
        value = response["result"]["structuredContent"]
        self.assertEqual(value["schema"], "eco-estate/v1")
        self.assertEqual(value["params"]["limit"], 200)
        self.assertFalse((self.root / "outbox.json").exists())


if __name__ == "__main__":
    unittest.main()
