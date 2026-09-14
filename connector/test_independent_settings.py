"""Independent owners must never fall back to another instance's state or URL."""
import dataclasses
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from borg_context_server import Settings
from runtime_paths import borg_home, loopback_mcp_url
import test_connector as fixtures


class IndependentSettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path.home())
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def configuration(self, owner):
        home = self.root / owner
        home.mkdir()
        projects = home / "projects"
        projects.mkdir()
        inbound, upstream = home / "authorization", home / "upstream"
        for path, value in [(inbound, "Bearer " + "a" * 48), (upstream, "b" * 48)]:
            path.write_text(value)
            path.chmod(0o600)
        doc = {"version": 1, "access_mode": "owner_all", "mem0_principal": "borg-" + owner,
               "allowed_scopes": ["*"], "inbound_authorization_file": str(inbound),
               "mem0_token_file": str(upstream), "project_roots": [str(projects)],
               "mem0_url": "http://127.0.0.1:18765/mcp", "default_scope": "personal:" + owner,
               "state_root": str(home / "state")}
        path = home / "config.json"
        path.write_text(json.dumps(doc))
        path.chmod(0o600)
        return path, doc

    def test_independent_owners_keep_explicit_identity_and_state(self):
        first_path, _ = self.configuration("first")
        second_path, _ = self.configuration("second")
        first, second = Settings.load(first_path), Settings.load(second_path)
        self.assertEqual(first.mem0_principal, "borg-first")
        self.assertEqual(second.default_scope, "personal:second")
        self.assertNotEqual(first.state_root, second.state_root)
        self.assertNotEqual(first.project_roots, second.project_roots)
        self.assertEqual(first.mem0_url, "http://127.0.0.1:18765/mcp")

    def test_invalid_identity_scope_roots_and_remote_upstream_are_rejected(self):
        path, doc = self.configuration("owner")
        for key, value in [("mem0_principal", None), ("mem0_principal", ""),
                           ("default_scope", "*"), ("project_roots", str(self.root)),
                           ("project_roots", [None]), ("state_root", "relative"),
                           ("mem0_url", "https://example.invalid/mcp")]:
            with self.subTest(key=key, value=value):
                path.write_text(json.dumps({**doc, key: value}))
                with self.assertRaises(ValueError):
                    Settings.load(path)

    def test_local_credentials_only_reach_explicit_loopback_mcp(self):
        for value in [None, "http://localhost:8765/mcp", "http://127.0.0.1/mcp",
                      "http://127.0.0.1:0/mcp", "http://127.0.0.1:8765/mcp?target=x",
                      "http://user:pass@127.0.0.1:8765/mcp", "http://127.0.0.1:8765/other"]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                loopback_mcp_url(value)
        self.assertEqual(loopback_mcp_url("http://[::1]:18765/mcp"), "http://[::1]:18765/mcp")

    def test_explicit_root_is_not_derived_from_os_user(self):
        with patch.dict("os.environ", {"BORG_HOME": str(self.root)}):
            self.assertEqual(borg_home(), self.root)

    def test_independent_settings_reject_legacy_owner_token_and_missing_scope(self):
        path, doc = self.configuration("independent")
        with patch.dict("os.environ", {"BORG_HOME": str(self.root)}):
            for change in [{"mem0_token_file": str(Path.home() / "Library/Memory/mem0/data/mcp-token")},
                           {"mem0_token_file": str(self.root / "mem0/data/owner-token")}]:
                path.write_text(json.dumps({**doc, **change}))
                with self.assertRaisesRegex(ValueError, "dedicated"):
                    Settings.load(path)
            doc.pop("default_scope")
            path.write_text(json.dumps(doc))
            with self.assertRaisesRegex(ValueError, "explicit"):
                Settings.load(path)

    def test_backend_defaults_follow_each_independent_home(self):
        from native_browser import BrowserStore
        from remote_tools import RemoteStore
        from service_handles import CredentialStore
        for owner in ["one", "two"]:
            home = self.root / owner
            home.mkdir()
            with patch.dict("os.environ", {"BORG_HOME": str(home)}):
                self.assertEqual(BrowserStore({}).root, home / "borg-context/browser")
                self.assertEqual(RemoteStore({}).root, home / "borg-context/remote")
                self.assertEqual(CredentialStore({}).registry, home / "borg-context/credentials.json")

    def test_mounted_subsystems_use_each_configured_state_root(self):
        import asyncio
        from borg_context_server import build_server
        for owner in ["mounted-one", "mounted-two"]:
            path, doc = self.configuration(owner)
            settings = dataclasses.replace(Settings.load(path), computer={"backend": "native"},
                browser={"backend": "native"}, remote={"backend": "ssh"},
                credentials={"backend": "value_blind"})
            mounted = build_server(settings)
            job = asyncio.run(mounted.get_tool("job_start")).fn.__self__
            browser = asyncio.run(mounted.get_tool("browser_start_session")).fn.__self__
            remote = asyncio.run(mounted.get_tool("remote_start")).fn.__self__
            credentials = asyncio.run(mounted.get_tool("credential_list")).fn.__self__
            self.assertEqual(job.root, settings.state_root)
            self.assertEqual(job.artifacts, settings.state_root / "artifacts")
            self.assertEqual(browser.root, settings.state_root / "browser")
            self.assertEqual(remote.root, settings.state_root / "remote")
            self.assertEqual(remote.hosts_path, settings.state_root / "hosts.json")
            self.assertEqual(credentials.registry, settings.state_root / "credentials.json")


class IndependentMemoryTests(unittest.TestCase):
    setUp = fixtures.ConnectorTests.setUp
    tearDown = fixtures.ConnectorTests.tearDown
    rpc = fixtures.ConnectorTests.rpc
    call = fixtures.ConnectorTests.call

    def test_fingerprint_matches_the_computer_contract_before_and_after_listing(self):
        from capabilities import schema_fingerprint
        self.server.tool(name="computer_read_file")(lambda path: "synthetic")
        first = self.call("borg_capabilities", {})["structuredContent"]
        wire = self.rpc("tools/list").json()["result"]["tools"]
        second = self.call("borg_capabilities", {})["structuredContent"]
        self.assertEqual(first["tool_schema_sha256"], schema_fingerprint(wire))
        self.assertEqual(first["tool_schema_sha256"], second["tool_schema_sha256"])

    def test_screenshot_write_is_annotated_and_receipted_as_a_mutation(self):
        from native_ui import mount_ui
        from computer_tools import BoundaryMiddleware
        mount_ui(self.server, {"backend": "native_os"})
        wire = {t["name"]: t for t in self.rpc("tools/list").json()["result"]["tools"]}
        self.assertFalse(wire["ui_capture"]["annotations"]["readOnlyHint"])
        self.assertFalse(wire["ui_capture"]["annotations"]["idempotentHint"])
        self.assertTrue(wire["ui_capture"]["annotations"]["destructiveHint"])
        self.assertTrue(BoundaryMiddleware._receipt_worthy("ui_capture"))

    def test_omitted_scope_uses_this_owner_and_principal(self):
        # The context object is retained by registered bound methods.
        import asyncio
        tool = asyncio.run(self.server.get_tool("borg_remember"))
        context = tool.fn.__self__
        context.settings = dataclasses.replace(self.settings, mem0_principal="borg-independent",
                                               default_scope="personal:independent")
        fixtures.FakeUpstream.principal = "borg-independent"
        result = self.call("borg_remember", {"text": "An independent owner's test fact"})
        self.assertFalse(result.get("isError", False), result)
        self.assertEqual(fixtures.FakeUpstream.calls[-1][1]["scope"], "personal:independent")
        self.assertEqual(fixtures.FakeUpstream.calls[-1][1]["agent"], "borg-independent")


if __name__ == "__main__":
    unittest.main()
