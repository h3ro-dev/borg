"""Signed-token and HTTP-boundary checks, with no live credentials or data."""
import asyncio
import json
import socket
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path

import httpx
import uvicorn
from fastmcp.server.auth.providers.jwt import JWTVerifier, RSAKeyPair
from starlette.testclient import TestClient

from cloudflare_gateway import GatewaySettings, build_gateway, server_config, MAX_BODY, UPSTREAM


class GatewayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.keys = RSAKeyPair.generate()
        cls.wrong_keys = RSAKeyPair.generate()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        credential = Path(self.tmp.name).resolve() / "authorization"
        credential.write_text("Bearer " + "local-test-credential-" * 3)
        credential.chmod(0o600)
        self.settings = GatewaySettings("https://borg.example.com",
            "https://owner.cloudflareaccess.com", "a" * 64, "owner@example.com", credential)
        self.forwarded = []

        def upstream(request):
            self.forwarded.append(request)
            return httpx.Response(200, content=b'event: message\ndata: {"ok":true}\n\n', headers={
                "Content-Type": "text/event-stream", "Mcp-Session-Id": "test-session",
                "Set-Cookie": "upstream-private=not-forwarded", "X-Private": "not-forwarded"})

        self.upstream = upstream
        self.client = self.make_client(self.settings)

    def make_client(self, settings):
        verifier = JWTVerifier(public_key=self.keys.public_key, issuer=settings.issuer,
                               audience=settings.audience, algorithm="RS256")
        app = build_gateway(settings, verifier=verifier, transport=httpx.MockTransport(self.upstream))
        return self.enterContext(TestClient(app))

    def token(self, **claims):
        return self.keys.create_token(issuer=self.settings.issuer, audience=self.settings.audience,
            additional_claims={"email": self.settings.owner_email, "type": "app", **claims})

    def headers(self, **claims):
        return {"Cf-Access-Jwt-Assertion": self.token(**claims)}

    def test_identity_signature_issuer_audience_and_time_are_required(self):
        for claims in [{"email": "someone-else@example.com"}, {"iss": "https://evil.example"},
                {"aud": "other-app"}, {"exp": int(time.time()) - 1}, {"exp": None},
                {"exp": True}, {"nbf": int(time.time()) + 600}, {"sub": ""},
                {"email": None}, {"type": "service"}]:
            with self.subTest(claims=claims):
                self.assertEqual(self.client.post("/mcp", headers=self.headers(**claims)).status_code, 401)
        wrong = self.wrong_keys.create_token(issuer=self.settings.issuer, audience=self.settings.audience,
            additional_claims={"email": self.settings.owner_email, "type": "app"})
        self.assertEqual(self.client.post("/mcp", headers={"Cf-Access-Jwt-Assertion": wrong}).status_code, 401)
        self.assertEqual(self.forwarded, [])

    def test_bearer_or_identity_header_is_not_sufficient(self):
        for headers in [{}, {"Authorization": self.settings.authorization_file.read_text()},
                        {"Cf-Access-Authenticated-User-Email": self.settings.owner_email},
                        {"Cf-Access-Jwt-Assertion": "invalid" * 10}]:
            self.assertEqual(self.client.post("/mcp", headers=headers).status_code, 401)
        token = self.token()
        self.assertEqual(self.client.post("/mcp", headers=[("Cf-Access-Jwt-Assertion", token),
            ("Cf-Access-Jwt-Assertion", token)]).status_code, 401)
        self.assertEqual(self.forwarded, [])

    def test_independent_gateway_forwards_only_to_its_configured_upstream(self):
        endpoint = "http://127.0.0.1:18770/mcp"
        client = self.make_client(replace(self.settings, upstream_url=endpoint))
        response = client.post("/mcp", headers=self.headers(), content=b"{}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(str(self.forwarded[-1].url), endpoint)

    def test_additional_owner_requires_explicit_config_and_valid_signed_identity(self):
        secondary = "secondary@example.com"
        self.assertEqual(self.client.post("/mcp", headers=self.headers(email=secondary)).status_code, 401)
        client = self.make_client(replace(self.settings, additional_owner_emails=(secondary,)))
        for email in [self.settings.owner_email, secondary, secondary.upper()]:
            self.assertEqual(client.post("/mcp", headers=self.headers(email=email)).status_code, 200)
        forwarded = len(self.forwarded)
        for email in ["other@example.com", "secondary@example.com.evil", "secondary+tag@example.com",
                      " secondary@example.com", [secondary], {"email": secondary}, True, None]:
            with self.subTest(email=email):
                self.assertEqual(client.post("/mcp", headers=self.headers(email=email)).status_code, 401)
        for claims in [{"iss": "https://evil.example"}, {"aud": "other-app"},
                       {"exp": int(time.time()) - 1}, {"nbf": int(time.time()) + 600},
                       {"sub": ""}, {"type": "service"}]:
            with self.subTest(claims=claims):
                self.assertEqual(client.post("/mcp", headers=self.headers(email=secondary, **claims)).status_code, 401)
        wrong = self.wrong_keys.create_token(issuer=self.settings.issuer, audience=self.settings.audience,
            additional_claims={"email": secondary, "type": "app"})
        self.assertEqual(client.post("/mcp", headers={"Cf-Access-Jwt-Assertion": wrong}).status_code, 401)
        self.assertEqual(len(self.forwarded), forwarded)

    def test_mcp_body_sessions_and_stream_are_preserved_and_credentials_replaced(self):
        body = b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
        headers = self.headers()
        headers.update({"Authorization": "Bearer attacker-controlled", "Cookie": "private=discard",
            "X-Forwarded-Host": "evil.example", "Content-Type": "application/json",
            "Accept": "text/event-stream", "Mcp-Session-Id": "previous-session",
            "Mcp-Protocol-Version": "2025-11-25", "Last-Event-Id": "event-7"})
        for method in ["POST", "GET", "DELETE"]:
            r = self.client.request(method, "/mcp", content=body, headers=headers)
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.content, b'event: message\ndata: {"ok":true}\n\n')
            self.assertEqual(r.headers["mcp-session-id"], "test-session")
            self.assertNotIn("set-cookie", r.headers)
            sent = self.forwarded[-1]
            self.assertEqual(str(sent.url), UPSTREAM)
            self.assertEqual(sent.method, method)
            self.assertEqual(sent.content, body)
            self.assertEqual(sent.headers["authorization"], self.settings.authorization_file.read_text())
            self.assertEqual(sent.headers["mcp-session-id"], "previous-session")
            self.assertEqual(sent.headers["last-event-id"], "event-7")
            for name in ["cookie", "cf-access-jwt-assertion", "x-forwarded-host"]:
                self.assertNotIn(name, sent.headers)

    def test_unicode_casefold_lookalikes_cannot_match_either_owner(self):
        client = self.make_client(replace(self.settings, owner_email="james@example.com",
                                         additional_owner_emails=("secondary@example.com",)))
        for email in ["james@example.com", "JAMES@EXAMPLE.COM",
                      "secondary@example.com", "SECONDARY@EXAMPLE.COM"]:
            self.assertEqual(client.post("/mcp", headers=self.headers(email=email)).status_code, 200)
        forwarded = len(self.forwarded)
        for email in ["jame\u017f@example.com", "\u017fecondary@example.com"]:
            with self.subTest(email=email):
                self.assertEqual(client.post("/mcp", headers=self.headers(email=email)).status_code, 401)
        self.assertEqual(len(self.forwarded), forwarded)

    def test_arbitrary_paths_queries_and_large_bodies_do_not_reach_origin(self):
        for path in ["/", "/other", "/mcp/extra"]:
            self.assertEqual(self.client.post(path, headers=self.headers()).status_code, 404)
        self.assertEqual(self.client.post("/mcp?target=http://elsewhere", headers=self.headers()).status_code, 400)
        self.assertEqual(self.client.post("/mcp", content=b"x" * (MAX_BODY + 1),
                                         headers=self.headers()).status_code, 413)
        self.assertEqual(self.forwarded, [])

    def test_origin_credential_rotation_and_unavailability_fail_closed(self):
        self.settings.authorization_file.write_text("Bearer " + "rotated-test-credential" * 3)
        r = self.client.post("/mcp", headers=self.headers())
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.forwarded[-1].headers["authorization"], self.settings.authorization_file.read_text())
        self.settings.authorization_file.chmod(0o644)
        self.assertEqual(self.client.post("/mcp", headers=self.headers()).status_code, 502)
        self.assertEqual(len(self.forwarded), 1)

    def test_configuration_requires_explicit_owner_origin_and_private_file(self):
        path = Path(self.tmp.name).resolve() / "config.json"
        config = {"version": 1, "public_url": self.settings.public_url, "issuer": self.settings.issuer,
            "audience": self.settings.audience, "owner_email": self.settings.owner_email,
            "authorization_file": str(self.settings.authorization_file)}
        path.write_text(json.dumps(config)); path.chmod(0o600)
        self.assertEqual(GatewaySettings.load(path), self.settings)
        path.write_text(json.dumps({**config, "owner_email": "OWNER@EXAMPLE.COM",
                                   "additional_owner_emails": ["SECONDARY@EXAMPLE.COM"]}))
        self.assertEqual(GatewaySettings.load(path),
                         replace(self.settings, additional_owner_emails=("secondary@example.com",)))
        for overrides in [{"issuer": "http://owner.cloudflareaccess.com"},
                {"issuer": "https://evil.example"}, {"public_url": "https://user@borg.example.com"},
                {"owner_email": ""}, {"owner_email": "@example.com"}, {"owner_email": "owner@"},
                {"owner_email": "*@example.com"}, {"owner_email": "owner @example.com"},
                {"additional_owner_emails": "secondary@example.com"}, {"additional_owner_emails": None},
                {"additional_owner_emails": {}}, {"additional_owner_emails": [True]},
                {"additional_owner_emails": [None]}, {"additional_owner_emails": [""]},
                {"additional_owner_emails": ["*@example.com"]},
                {"additional_owner_emails": ["secondary@example.com "]},
                {"additional_owner_emails": ["secondary@@example.com"]},
                {"audience": ""}, {"version": 0}]:
            path.write_text(json.dumps({**config, **overrides}))
            with self.assertRaises(ValueError):
                GatewaySettings.load(path)


class GatewayStreamTests(unittest.IsolatedAsyncioTestCase):
    async def test_shutdown_closes_active_stream_within_service_deadline(self):
        with tempfile.TemporaryDirectory() as directory:
            credential = Path(directory).resolve() / "authorization"
            credential.write_text("Bearer " + "synthetic-stream-test-" * 3)
            credential.chmod(0o600)
            settings = GatewaySettings("https://borg.example.com",
                "https://owner.cloudflareaccess.com", "a" * 64, "owner@example.com", credential)
            keys = RSAKeyPair.generate()
            token = keys.create_token(issuer=settings.issuer, audience=settings.audience,
                additional_claims={"email": settings.owner_email, "type": "app"})
            closed = asyncio.Event()

            class Stream(httpx.AsyncByteStream):
                async def __aiter__(self):
                    while True:
                        yield b'event: message\ndata: {"synthetic":true}\n\n'
                        await asyncio.sleep(0.05)

                async def aclose(self):
                    closed.set()

            def upstream(request):
                return httpx.Response(200, stream=Stream(), headers={
                    "content-type": "text/event-stream", "mcp-session-id": "synthetic-session"})

            verifier = JWTVerifier(public_key=keys.public_key, issuer=settings.issuer,
                audience=settings.audience, algorithm="RS256")
            app = build_gateway(settings, verifier=verifier, transport=httpx.MockTransport(upstream))
            sock = socket.socket()
            sock.bind(("127.0.0.1", 0))
            sock.listen(16)
            server = uvicorn.Server(server_config(app))
            task = asyncio.create_task(server.serve(sockets=[sock]))
            try:
                async with asyncio.timeout(3):
                    while not server.started:
                        await asyncio.sleep(0.01)
                async with httpx.AsyncClient(trust_env=False) as client:
                    async with client.stream("GET", f"http://127.0.0.1:{sock.getsockname()[1]}/mcp",
                            headers={"Cf-Access-Jwt-Assertion": token}) as response:
                        self.assertEqual(response.headers["mcp-session-id"], "synthetic-session")
                        chunks = response.aiter_bytes()
                        async with asyncio.timeout(1):
                            self.assertIn(b"synthetic", await anext(chunks))
                        self.assertFalse(closed.is_set())
                        server.should_exit = True
                        await asyncio.wait_for(asyncio.shield(task), 8)
                        await asyncio.wait_for(closed.wait(), 1)
                self.assertTrue(task.done())
            finally:
                server.should_exit = True
                if not task.done():
                    server.force_exit = True
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                sock.close()


if __name__ == "__main__":
    unittest.main()
