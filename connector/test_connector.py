"""Focused boundary and reversible computer acceptance checks (stdlib unittest)."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from starlette.testclient import TestClient
from borg_context_server import Settings, build_server, PRINCIPAL, _activity
from computer_tools import DESCRIPTIONS, classify_failure, failure_meta


class FakeUpstream:
    principal = PRINCIPAL
    calls = []
    memory_text = "Harmless candidate context"

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def call_tool(self, name, args):
        self.calls.append((name, args))
        data = {"principal": self.principal, "allowed_scopes": ["*"],
                "effective_read_scopes": "ALL (including unscoped)"}
        if name == "memory_search":
            data = [{"id": "11111111-1111-4111-8111-111111111111", "memory": self.memory_text, "scope": "personal:james"}]
        elif name == "memory_add":
            data = {"events": [{"id": "11111111-1111-4111-8111-111111111111", "event": "ADD"}], "scope": args["scope"]}
        elif name == "memory_graph_stats":
            data = {"status": "ok", "graphs": {}}
        from types import SimpleNamespace
        return SimpleNamespace(is_error=False, content=[SimpleNamespace(text=json.dumps(data))])


class ConnectorTests(unittest.TestCase):
    def setUp(self):
        # Avoid macOS /var -> /private/var symlinks in private credential paths.
        self.tmp = tempfile.TemporaryDirectory(dir=Path.home())
        self.root = Path(self.tmp.name)
        self.project_root = self.root / "projects"
        self.project_root.mkdir()
        self.inbound = self.root / "authorization"
        self.upstream = self.root / "upstream"
        self.inbound.write_text("Bearer " + "a" * 48)
        self.upstream.write_text("b" * 48)
        for p in (self.inbound, self.upstream):
            p.chmod(0o600)
        self.settings = Settings(self.inbound, self.upstream, (self.project_root,), {}, state_root=self.root)
        FakeUpstream.principal = PRINCIPAL
        FakeUpstream.calls = []
        FakeUpstream.memory_text = "Harmless candidate context"
        self.upstream_patch = patch("borg_context_server.Client", FakeUpstream)
        self.upstream_patch.start()
        self.server = build_server(self.settings)
        self.client = TestClient(self.server.http_app(stateless_http=True, json_response=True))
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.upstream_patch.stop()
        self.tmp.cleanup()

    def rpc(self, method, params=None, credential="a" * 48):
        headers = {"Accept": "application/json, text/event-stream"}
        if credential is not None:
            headers["Authorization"] = "Bearer " + credential
        return self.client.post("/mcp", headers=headers,
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}})

    def call(self, name, args):
        response = self.rpc("tools/call", {"name": name, "arguments": args})
        self.assertEqual(response.status_code, 200)
        return response.json()["result"]

    def test_http_rejects_absent_invalid_and_upstream_tokens(self):
        for token in (None, "wrong", "b" * 48):
            self.assertEqual(self.rpc("tools/list", credential=token).status_code, 401)
        self.assertEqual(FakeUpstream.calls, [])

    def test_http_tool_contract_and_annotations(self):
        rows = self.rpc("tools/list").json()["result"]["tools"]
        tools = {t["name"]: t for t in rows}
        self.assertEqual(set(tools), {"borg_status", "borg_search", "borg_projects", "borg_project_context", "borg_remember", "borg_forget", "borg_tool_search", "borg_capabilities", "borg_operation_status", "borg_operations_recent", "borg_identity"})
        self.assertFalse(tools["borg_remember"]["annotations"]["readOnlyHint"])
        self.assertTrue(tools["borg_forget"]["annotations"]["destructiveHint"])

    def test_wrong_upstream_principal_prevents_local_and_memory_reads(self):
        FakeUpstream.principal = "james"
        with patch("borg_context_server._repos_matching") as repos:
            result = self.call("borg_project_context", {"project": "borg"})
            self.assertTrue(result["isError"])
            repos.assert_not_called()
        self.assertEqual([name for name, _ in FakeUpstream.calls], ["memory_whoami"])

    def test_catalog_search_preserves_invocations_freshness_and_pagination(self):
        catalog = self.root / "tools.json"
        entries = [{"id": str(n), "name": f"Tool {n}", "category": "documents",
                    "status": "historical installed claim", "invoke": {"cli": "/example/tool"}}
                   for n in range(3)]
        catalog.write_text(json.dumps({"version": "test-edition", "tools": entries,
                                       "machines": {"everywhere": ["studio0"]}}))
        with patch("system_tools.CATALOG", catalog):
            first = self.call("borg_tool_search", {"query": "documents", "limit": 2})
            data = first["structuredContent"]
            self.assertEqual(data["tools"], entries[:2])
            self.assertEqual(data["next_offset"], 2)
            self.assertEqual(data["catalog_version"], "test-edition")
            self.assertIn("dated claim", data["notice"])
            second = self.call("borg_tool_search", {"query": "documents", "limit": 2, "offset": 2})
            self.assertEqual(second["structuredContent"]["tools"], entries[2:])
            self.assertIsNone(second["structuredContent"]["next_offset"])
            self.assertEqual(data["local_sources"]["capability_map"], str(catalog))
            sources = {"runtime_instructions": "/example/existing/AGENTS.md",
                       "capability_map": "/example/existing/capabilities.md",
                       "skills": ["/example/existing/skills"]}
            configured = json.loads(catalog.read_text())
            configured["local_sources"] = sources
            catalog.write_text(json.dumps(configured))
            updated = self.call("borg_tool_search", {"query": "documents", "limit": 2})
            self.assertEqual(updated["structuredContent"]["local_sources"], sources)
            self.assertEqual(updated["structuredContent"]["tools"], entries[:2])
            catalog.unlink()
            missing = self.call("borg_tool_search", {})
            self.assertTrue(missing["isError"])

    def test_capability_manifest_is_versioned_and_secret_free(self):
        result = self.call("borg_capabilities", {})
        self.assertFalse(result.get("isError", False), result)
        manifest = result["structuredContent"]
        self.assertEqual(manifest["manifest_version"], "borg-capabilities/v1")
        self.assertIn("borg_capabilities", {tool["name"] for tool in manifest["tools"]})
        self.assertEqual(manifest["domains"]["computer"]["status"], "configured" if self.settings.computer else "disabled")
        self.assertEqual(manifest["domains"]["memory"]["status"], "configured")
        from capabilities import schema_fingerprint
        actual_tools = self.rpc("tools/list").json()["result"]["tools"]
        self.assertEqual(manifest["tool_schema_sha256"], schema_fingerprint(actual_tools))
        self.assertNotIn("authorization", json.dumps(manifest).lower())

    def test_scope_and_memory_write_use_native_contract(self):
        result = self.call("borg_remember", {"text": "Disposable BORG acceptance fact", "scope": "personal:james"})
        self.assertFalse(result.get("isError", False))
        self.assertEqual(FakeUpstream.calls[-1], ("memory_add", {"text": "Disposable BORG acceptance fact", "scope": "personal:james", "agent": PRINCIPAL, "raw": True}))

    def test_credentials_are_withheld_before_mutation(self):
        result = self.call("borg_remember", {"text": "a" * 48})
        self.assertTrue(result["isError"])
        self.assertNotIn("a" * 48, json.dumps(result))
        self.assertEqual(FakeUpstream.calls, [])

    def test_quoted_credential_in_arguments_is_rejected_before_execution(self):
        result = self.call("borg_projects", {"query": 'API_KEY="' + "d" * 40 + '"'})
        self.assertTrue(result["isError"])
        self.assertEqual(FakeUpstream.calls, [])

    def test_quoted_credential_in_result_is_withheld(self):
        FakeUpstream.memory_text = 'password="' + "d" * 40 + '"'
        result = self.call("borg_search", {"query": "harmless"})
        self.assertTrue(result["isError"])
        self.assertNotIn("d" * 40, json.dumps(result))

    def test_failure_classifier_is_payload_free_and_actionable(self):
        cases = {
            "blocked by policy while invoking local command": "policy_refused",
            "permission denied by Accessibility": "permission_denied",
            "current fleet admission required": "admission_required",
            "driver unavailable for this target": "capability_unavailable",
            "provider unavailable": "provider_unavailable",
            "operation timed out after deadline": "timeout",
            "Bearer " + "x" * 48: "credential_withheld",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                code = classify_failure(raw)
                self.assertEqual(code, expected)
                meta = failure_meta(code)
                self.assertEqual(meta["code"], expected)
                self.assertFalse(meta["retry_without_change"])
                self.assertNotIn(raw, json.dumps(meta))

    def test_slow_repository_does_not_block_independent_computer_request(self):
        import concurrent.futures
        import threading
        import time
        entered = threading.Event()

        def slow(*args):
            entered.set()
            time.sleep(0.6)
            return []

        self.server.tool(name="computer_probe")(lambda: "probe completed")
        with patch("borg_context_server._repos_matching", slow), concurrent.futures.ThreadPoolExecutor() as pool:
            pending = pool.submit(self.call, "borg_projects", {})
            self.assertTrue(entered.wait(2))
            started = time.monotonic()
            result = self.call("computer_probe", {})
            self.assertFalse(result.get("isError", False))
            self.assertLess(time.monotonic() - started, 0.35)
            self.assertFalse(pending.result().get("isError", False))

    def test_repository_inspection_honors_total_budget(self):
        import time
        import subprocess
        from borg_context_server import _repos_matching
        repo = self.root / "one"
        (repo / ".git").mkdir(parents=True)

        def timeout(*args, **kwargs):
            time.sleep(kwargs["timeout"])
            raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])

        with patch("borg_context_server.subprocess.run", timeout):
            started = time.monotonic()
            result = _repos_matching("one", (self.root,), 1, budget_seconds=0.05)
            self.assertLess(time.monotonic() - started, 0.3)
            self.assertTrue(result[0]["inspection_budget_exhausted"])

    def test_rotating_local_credential_revokes_old_header_immediately(self):
        self.inbound.write_text("Bearer " + "c" * 48)
        self.assertEqual(self.rpc("tools/list").status_code, 401)
        self.assertEqual(self.rpc("tools/list", credential="c" * 48).status_code, 200)

    def test_private_configuration_denies_world_readable_and_owner_fallback(self):
        config = self.root / "config.json"
        doc = {"version": 1, "access_mode": "owner_all", "mem0_principal": PRINCIPAL,
               "allowed_scopes": ["*"], "inbound_authorization_file": str(self.inbound),
               "mem0_token_file": str(self.upstream), "project_roots": [str(self.project_root)]}
        config.write_text(json.dumps(doc))
        config.chmod(0o644)
        with self.assertRaises(ValueError):
            Settings.load(config)
        config.chmod(0o600)
        self.assertEqual(Settings.load(config).mem0_token_file, self.upstream)
        doc["mem0_token_file"] = str(Path.home() / "Library/Memory/mem0/data/mcp-token")
        config.write_text(json.dumps(doc))
        with self.assertRaises(ValueError):
            Settings.load(config)

    def test_status_activity_zero_limit_reads_no_records(self):
        with patch("borg_context_server.AGENT_LEDGER", self.root / "agents"), patch("borg_context_server.DISPATCH_LEDGER", self.root / "dispatch"):
            (self.root / "agents").write_text('{"tracked": {}}')
            (self.root / "dispatch").write_text('{"work_id": "should-not-return"}\n')
            result = _activity("", 0)
            self.assertEqual(result["recent_dispatches"], [])


@unittest.skipUnless(os.environ.get("BORG_TEST_COMPUTER_CONFIG"), "set BORG_TEST_COMPUTER_CONFIG for real stdio acceptance")
class ComputerTests(ConnectorTests):
    def setUp(self):
        super().setUp()
        self.client.__exit__(None, None, None)
        self.settings = Settings(self.inbound, self.upstream, (), {"backend": "native"}, state_root=self.root)
        self.server = build_server(self.settings)
        self.client = TestClient(self.server.http_app(stateless_http=True, json_response=True))
        self.client.__enter__()

    def test_http_tool_contract_and_annotations(self):
        tools = {t["name"]: t for t in self.rpc("tools/list").json()["result"]["tools"]}
        self.assertEqual({n.removeprefix("computer_") for n in tools if n.startswith("computer_")}, set(DESCRIPTIONS))
        self.assertTrue(tools["computer_start_process"]["annotations"]["destructiveHint"])
        self.assertFalse(tools["computer_write_file"]["annotations"]["readOnlyHint"])
        for method, key in (("resources/list", "resources"), ("prompts/list", "prompts")):
            self.assertEqual(self.rpc(method).json()["result"][key], [])

    def test_reversible_files_and_command_session_survive_separate_requests(self):
        path = str(self.root / "connector-canary.txt")
        initial = self.call("computer_write_file", {"path": path, "content": "BORG canary original\n", "mode": "rewrite"})
        self.assertFalse(initial.get("isError", False), initial)
        edited = self.call("computer_edit_block", {"file_path": path, "old_string": "original", "new_string": "verified"})
        self.assertFalse(edited.get("isError", False), edited)
        read = self.call("computer_read_file", {"path": path})
        self.assertIn("BORG canary verified", json.dumps(read))
        process = self.call("computer_start_process", {"command": "/usr/bin/python3 -u -c 'import time; print(\"BORG_COMMAND_STARTED\", flush=True); time.sleep(2); print(\"BORG_COMMAND_FINISHED\", flush=True)'", "timeout_ms": 500})
        self.assertFalse(process.get("isError", False), process)
        import re
        match = re.search(r"(?:PID|process ID|pid)[: ]+(\d+)", json.dumps(process), re.IGNORECASE)
        self.assertIsNotNone(match, process)
        import time
        output = ""
        deadline = time.monotonic() + 8
        while "BORG_COMMAND_FINISHED" not in output and time.monotonic() < deadline:
            output += json.dumps(self.call("computer_read_process_output", {"pid": int(match.group(1)), "timeout_ms": 4000}))
            if "BORG_COMMAND_FINISHED" not in output:
                time.sleep(0.25)
        self.assertIn("BORG_COMMAND_FINISHED", output)
        self.assertEqual(Path(path).read_text(), "BORG canary verified\n")


if __name__ == "__main__":
    unittest.main(verbosity=2)
