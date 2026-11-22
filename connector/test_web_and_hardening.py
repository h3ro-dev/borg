"""Focused high-value tests for universal web gateway, Hindsight backend, and hardening."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

import httpx
from fastmcp.exceptions import ToolError
from fastmcp.server.auth.providers.jwt import JWTVerifier, RSAKeyPair
from starlette.testclient import TestClient

from borg_context_server import Settings, build_server, PRINCIPAL
from cloudflare_gateway import GatewaySettings, build_gateway
from computer_tools import (
    BoundaryMiddleware,
    MAX_COMPLETED_PROCESSES,
    MAX_SEARCH_SESSIONS,
    NativeComputer,
    SafeContractError,
)
from fleet_tools import Fleet
from job_tools import JobStore
from native_browser import CDP
from native_ui import NativeUI
from operation_receipts import OperationLedger
from remote_tools import RemoteStore


class UniversalWebGatewayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.keys = RSAKeyPair.generate()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cred = Path(self.tmp.name).resolve() / "authorization"
        self.cred.write_text("Bearer " + "local-test-credential-" * 3)
        self.cred.chmod(0o600)
        self.forwarded = []

    def _make_oidc_client(self, *, allowed_origins=(), allowed_subjects=(), owner="owner@example.test", upstream_fn=None):
        settings = GatewaySettings(
            "https://borg.example.test",
            "https://idp.example.test",
            "borg-mcp",
            owner,
            self.cred,
            auth_mode="oidc_bearer",
            jwks_uri="https://idp.example.test/.well-known/jwks.json",
            allowed_subjects=allowed_subjects,
            allowed_origins=allowed_origins,
        )

        def default_upstream(request):
            self.forwarded.append(request)
            return httpx.Response(200, content=b'{"ok":true}', headers={
                "Content-Type": "application/json",
                "Mcp-Session-Id": "sess-1",
            })

        verifier = JWTVerifier(
            public_key=self.keys.public_key,
            issuer=settings.issuer,
            audience=settings.audience,
            algorithm="RS256",
        )
        app = build_gateway(settings, verifier=verifier, transport=httpx.MockTransport(upstream_fn or default_upstream))
        return self.enterContext(TestClient(app)), settings

    def _token(self, settings, **claims):
        return self.keys.create_token(
            issuer=settings.issuer,
            audience=settings.audience,
            additional_claims={"email": "owner@example.test", "email_verified": True, **claims},
        )

    def test_oidc_bearer_mode_metadata_email_subject_and_cors(self):
        client, settings = self._make_oidc_client(
            allowed_origins=("https://app.example.test",),
            allowed_subjects=("svc-owner-1",),
        )
        meta = client.get("/.well-known/oauth-protected-resource", headers={"Origin": "https://app.example.test"})
        self.assertEqual(meta.status_code, 200)
        self.assertEqual(meta.json()["authorization_servers"], ["https://idp.example.test"])
        self.assertEqual(meta.headers["access-control-allow-origin"], "https://app.example.test")

        preflight = client.options("/mcp", headers={"Origin": "https://app.example.test"})
        self.assertEqual(preflight.status_code, 204)
        self.assertIn("Authorization", preflight.headers["access-control-allow-headers"])

        blocked = client.post("/mcp", headers={"Origin": "https://evil.example.test"})
        self.assertEqual(blocked.status_code, 403)

        tok = self._token(settings)
        ok = client.post("/mcp", headers={"Authorization": "Bearer " + tok, "Origin": "https://app.example.test"}, content=b"{}")
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(ok.headers["access-control-allow-origin"], "https://app.example.test")

        # Unverified email must not match owner_email when subject is not allowlisted.
        unverified = self._token(settings, sub="random-user", email_verified=False)
        self.assertEqual(client.post("/mcp", headers={"Authorization": "Bearer " + unverified}).status_code, 401)

        # Explicitly allowlisted subject succeeds without an email claim.
        svc_tok = self.keys.create_token(issuer=settings.issuer, audience=settings.audience, subject="svc-owner-1")
        self.assertEqual(client.post("/mcp", headers={"Authorization": "Bearer " + svc_tok}, content=b"{}").status_code, 200)

        # Cloudflare header is ignored in oidc_bearer mode.
        self.assertEqual(client.post("/mcp", headers={"Cf-Access-Jwt-Assertion": tok}).status_code, 401)

    def test_oidc_config_load_validation(self):
        cfg_path = Path(self.tmp.name).resolve() / "oidc.json"
        base = {
            "version": 1,
            "auth_mode": "oidc_bearer",
            "public_url": "https://borg.example.test",
            "issuer": "https://idp.example.test",
            "jwks_uri": "https://idp.example.test/.well-known/jwks.json",
            "audience": "borg-mcp",
            "owner_email": "owner@example.test",
            "allowed_origins": ["https://web.example.test"],
            "authorization_file": str(self.cred),
        }
        cfg_path.write_text(json.dumps(base))
        cfg_path.chmod(0o600)
        loaded = GatewaySettings.load(cfg_path)
        self.assertEqual(loaded.auth_mode, "oidc_bearer")
        self.assertEqual(loaded.allowed_origins, ("https://web.example.test",))
        for bad in (
            {"algorithm": "HS256"},
            {"jwks_uri": "http://idp.example.test/jwks"},
            {"allowed_origins": ["*"]},
            {"allowed_origins": ["http://insecure.example.test"]},
            {"owner_email": "", "allowed_subjects": []},
        ):
            cfg_path.write_text(json.dumps({**base, **bad}))
            with self.assertRaises(ValueError):
                GatewaySettings.load(cfg_path)

    def test_upstream_mid_stream_disconnect_closes_gracefully(self):
        class BrokenStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b"event: message\n"
                raise httpx.RemoteProtocolError("peer closed connection")

        client, settings = self._make_oidc_client(
            upstream_fn=lambda req: httpx.Response(200, stream=BrokenStream(), headers={"Content-Type": "text/event-stream"})
        )
        tok = self._token(settings)
        response = client.post("/mcp", headers={"Authorization": "Bearer " + tok}, content=b"{}")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"event: message", response.content)


class HindsightBackendTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=Path.home())
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.projects = self.root / "projects"
        self.projects.mkdir()
        self.inbound = self.root / "authorization"
        self.inbound.write_text("Bearer " + "a" * 48)
        self.inbound.chmod(0o600)
        self.config_path = self.root / "config.json"
        self.doc = {
            "version": 1,
            "access_mode": "owner_all",
            "mem0_principal": PRINCIPAL,
            "allowed_scopes": ["*"],
            "inbound_authorization_file": str(self.inbound),
            "project_roots": [str(self.projects)],
            "default_scope": "personal:owner",
            "state_root": str(self.root),
            "memory": {
                "backend": "hindsight",
                "url": "http://127.0.0.1:18891",
                "bank": "primary",
                "search_banks": ["primary", "secondary"],
                "budget": "low",
                "timeout_seconds": 10,
            },
        }
        self.config_path.write_text(json.dumps(self.doc))
        self.config_path.chmod(0o600)

    def test_hindsight_settings_and_crud_flow(self):
        settings = Settings.load(self.config_path)
        self.assertIsNone(settings.mem0_token_file)
        self.assertEqual(settings.memory["backend"], "hindsight")

        # Non-loopback Hindsight URL must be rejected.
        bad_doc = {**self.doc, "memory": {**self.doc["memory"], "url": "https://remote.example.test"}}
        self.config_path.write_text(json.dumps(bad_doc))
        with self.assertRaises(ValueError):
            Settings.load(self.config_path)

        mem_uuid = str(uuid.uuid4())
        requests_seen = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests_seen.append((request.method, request.url.path))
            if request.url.path == "/health":
                return httpx.Response(200, json={"status": "healthy"})
            if request.url.path == "/v1/default/banks":
                return httpx.Response(200, json={"banks": [
                    {"bank_id": "primary", "fact_count": 42},
                    {"bank_id": "secondary", "fact_count": 10},
                ]})
            if request.url.path.endswith("/memories/recall"):
                bank = request.url.path.split("/")[4]
                score = 0.9 if bank == "primary" else 0.7
                return httpx.Response(200, json={"results": [
                    {"id": mem_uuid if bank == "primary" else str(uuid.uuid4()),
                     "text": f"fact from {bank}", "scores": {"final": score},
                     "tags": ["scope=personal:owner"], "mentioned_at": "2026-11-21T00:00:00Z"}
                ]})
            if request.method == "POST" and request.url.path.endswith("/memories"):
                return httpx.Response(200, json={"success": True, "bank_id": "primary", "items_count": 1, "operation_id": "op-1"})
            if request.method == "PATCH" and request.url.path.endswith(f"/memories/{mem_uuid}"):
                return httpx.Response(200, json={"id": mem_uuid, "state": "invalidated"})
            return httpx.Response(404)

        def fake_client(timeout: float):
            return httpx.AsyncClient(transport=httpx.MockTransport(handler))

        server = build_server(settings)
        with patch("borg_context_server.BorgContext._hindsight_client", staticmethod(fake_client)):
            with TestClient(server.http_app(stateless_http=True, json_response=True)) as client:
                def call(name, args):
                    res = client.post(
                        "/mcp",
                        headers={"Authorization": "Bearer " + "a" * 48, "Accept": "application/json, text/event-stream"},
                        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args}},
                    )
                    self.assertEqual(res.status_code, 200)
                    return res.json()["result"]

                st = call("borg_status", {})["structuredContent"]
                self.assertEqual(st["mem0"]["status"], "PASS")
                self.assertEqual(st["capabilities"]["memory_backend"], "hindsight")
                self.assertTrue(st["capabilities"]["memory_read"])
                self.assertEqual(st["memory"]["bank_fact_count"], 42)

                search = call("borg_search", {"query": "test", "limit": 5})["structuredContent"]
                self.assertEqual(len(search["results"]), 2)
                self.assertEqual(search["results"][0]["bank"], "primary")

                rem = call("borg_remember", {"text": "Clean owner note", "scope": "personal:owner"})["structuredContent"]
                self.assertTrue(rem["success"])
                self.assertEqual(rem["operation_id"], "op-1")

                fgt = call("borg_forget", {"memory_id": mem_uuid})["structuredContent"]
                self.assertEqual(fgt["state"], "invalidated")


class HardeningTests(unittest.IsolatedAsyncioTestCase):
    def test_remote_cross_os_probe_and_custom_load_threshold(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            hosts = root / "hosts.json"
            hosts.write_text(json.dumps([
                {"name": "linux-box", "backend": "ssh", "ssh_alias": "linux-box", "enabled": True, "max_load_per_cpu": 3.0},
                {"name": "mac-box", "backend": "ssh", "ssh_alias": "mac-box", "enabled": True},
            ]))
            hosts.chmod(0o600)
            store = RemoteStore({"hosts_file": str(hosts), "jobs_root": str(root)})

            def fake_run(cmd, **kwargs):
                if "linux-box" in cmd:
                    return subprocess.CompletedProcess(cmd, 0, stdout="8\n{ 20.00 18.50 16.00 }\n", stderr="")
                return subprocess.CompletedProcess(cmd, 0, stdout="4\n{ 1.20 1.10 1.00 }\n", stderr="")

            with patch("remote_tools.subprocess.run", side_effect=fake_run):
                listed = store.list_hosts()["hosts"]
            by_name = {h["name"]: h for h in listed}
            self.assertEqual(by_name["linux-box"]["status"], "ready")
            self.assertEqual(by_name["linux-box"]["cpu_count"], 8)
            self.assertEqual(by_name["linux-box"]["load_1m"], 20.0)
            self.assertEqual(by_name["mac-box"]["status"], "ready")

    def test_job_read_output_seeks_bounded_slices(self):
        with tempfile.TemporaryDirectory() as directory:
            store = JobStore(Path(directory))
            try:
                cmd = shlex.join([sys.executable, "-c", "import sys; sys.stdout.write('0123456789'); sys.stderr.write('ABCDE')"])
                row = store.start(cmd, timeout_ms=50)
                store.processes[row["job_id"]].wait(timeout=5)
                part1 = store.read_output(row["job_id"], stream="combined", offset=4, length=9)
                self.assertEqual(part1["output"], "456789\nAB")
                self.assertEqual(part1["next_offset"], 13)
                self.assertEqual(part1["total_bytes"], 16)
                self.assertTrue(part1["truncated"])
                part2 = store.read_output(row["job_id"], stream="combined", offset=part1["next_offset"], length=10)
                self.assertEqual(part2["output"], "CDE")
                self.assertFalse(part2["truncated"])
            finally:
                store.close_all()

    def test_native_computer_bounds_completed_sessions_and_searches(self):
        native = NativeComputer()
        with tempfile.TemporaryDirectory() as directory:
            for _ in range(MAX_SEARCH_SESSIONS + 5):
                native.start_search(directory)
            self.assertEqual(len(native._searches), MAX_SEARCH_SESSIONS)
        for _ in range(MAX_COMPLETED_PROCESSES + 4):
            row = native.start_process("true", shell="/bin/sh", timeout_ms=0)
            native._processes[row["pid"]].wait(timeout=3)
            native._process_output[row["pid"]]["done"].wait(timeout=3)
        self.assertLessEqual(len(native._processes), MAX_COMPLETED_PROCESSES + 1)

    async def test_post_dispatch_error_marks_outcome_unknown_and_safe_contract_preserves_detail(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = OperationLedger(Path(directory))
            settings = SimpleNamespace(
                inbound_authorization_file=Path("/nonexistent"),
                mem0_token_file=None,
                computer={},
            )
            mw = BoundaryMiddleware(lambda: None, settings, ledger=ledger)

            ctx1 = SimpleNamespace(message=SimpleNamespace(name="computer_edit_block", arguments={}))
            with self.assertRaisesRegex(ToolError, r"BORG_INVALID_REQUEST: expected 1 match"):
                async def raise_contract(_):
                    raise SafeContractError("expected 1 match", mutated=False)
                await mw.on_call_tool(ctx1, raise_contract)
            self.assertEqual(ledger.recent(1)[0]["state"], "failed")

            ctx2 = SimpleNamespace(message=SimpleNamespace(name="computer_write_file", arguments={}))
            with self.assertRaisesRegex(ToolError, r"BORG_PERMISSION_DENIED:"):
                async def raise_post(_):
                    raise ToolError("permission denied mid-write")
                await mw.on_call_tool(ctx2, raise_post)
            self.assertEqual(ledger.recent(1)[0]["state"], "outcome_unknown")

    def test_cdp_loopback_and_ui_applescript_escaping(self):
        with self.assertRaises(ToolError):
            CDP("ws://198.51.100.1:9222/devtools/page/1")
        with self.assertRaises(ToolError):
            NativeUI._escape_applescript('Safari"\nbeep')
        self.assertEqual(NativeUI._escape_applescript(r'App\"One'), r'App\\\"One')

    async def test_fleet_evicts_broken_cached_client_and_reconnects(self):
        with tempfile.TemporaryDirectory(dir=Path.home()) as directory:
            root = Path(directory)
            reg = root / "fleet.json"
            row = {"id": "node1", "ssh_alias": "node1", "home": str(root),
                   "instance_id": str(uuid.uuid4()), "owner": "owner", "enabled": True}
            reg.write_text(json.dumps({"schema": "borg-fleet/v1", "hosts": [row]}))
            reg.chmod(0o600)
            created = []

            class FlakyClient:
                def __init__(self, fail):
                    self.fail = fail
                    self.closed = False
                async def __aenter__(self):
                    if self.fail:
                        raise ConnectionError("ssh dropped")
                    return self
                async def __aexit__(self, *args):
                    pass
                async def close(self):
                    self.closed = True
                async def call_tool(self, *args, **kwargs):
                    return SimpleNamespace(structured_content={**row, "server_generation": str(uuid.uuid4())})
                async def list_tools(self, **kwargs):
                    return [SimpleNamespace(name="borg_identity", model_dump=lambda **kw: {"name": "borg_identity"})]

            def factory(_row):
                client = FlakyClient(fail=len(created) == 0)
                created.append(client)
                return client

            fleet = Fleet(reg, factory)
            self.addAsyncCleanup(fleet.close)
            first = await fleet.fleet_hosts()
            self.assertEqual(first["hosts"][0]["state"], "unavailable")
            self.assertEqual(first["hosts"][0]["failure_stage"], "connection_failed")
            self.assertTrue(created[0].closed)
            second = await fleet.fleet_hosts()
            self.assertEqual(second["hosts"][0]["state"], "reachable")
            self.assertEqual(len(created), 2)


if __name__ == "__main__":
    unittest.main()
