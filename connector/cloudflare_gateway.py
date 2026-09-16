#!/usr/bin/env python3
"""Validate Cloudflare Access owner identity before forwarding BORG MCP traffic.

Cloudflare Managed OAuth owns discovery, registration, consent and token issuance.
This loopback origin accepts only the signed application assertion from that edge.
"""
from __future__ import annotations

import json
import logging
import math
import re
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import uvicorn
from fastmcp.server.auth.providers.jwt import JWTVerifier
from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

from borg_context_server import private_text, secret
from runtime_paths import borg_home, loopback_mcp_url

UPSTREAM = "http://127.0.0.1:8770/mcp"
MAX_BODY = 4 * 1024 * 1024
REQUEST_HEADERS = ("accept", "content-type", "mcp-session-id", "mcp-protocol-version", "last-event-id")
RESPONSE_HEADERS = ("content-type", "mcp-session-id", "mcp-protocol-version", "allow")


def owner_email(value: object) -> str:
    if (not isinstance(value, str)
            or not re.fullmatch(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+", value)):
        raise ValueError("BORG gateway requires an exact owner email address")
    return value.casefold()


@dataclass(frozen=True)
class GatewaySettings:
    public_url: str
    issuer: str
    audience: str
    owner_email: str
    authorization_file: Path
    additional_owner_emails: tuple[str, ...] = ()
    upstream_url: str = UPSTREAM

    @classmethod
    def load(cls, path: Path) -> "GatewaySettings":
        doc = json.loads(private_text(path))
        public = urlsplit(doc["public_url"])
        issuer = urlsplit(doc["issuer"])
        if (doc.get("version") != 1 or public.scheme != "https"
                or not public.hostname or public.netloc != public.hostname
                or public.path or public.query or public.fragment
                or issuer.scheme != "https" or not issuer.hostname
                or not issuer.hostname.endswith(".cloudflareaccess.com")
                or issuer.netloc != issuer.hostname or issuer.path
                or issuer.query or issuer.fragment):
            raise ValueError("BORG gateway requires explicit HTTPS origin and Cloudflare issuer")
        audience = doc["audience"]
        if (not isinstance(audience, str) or len(audience) != 64
                or any(c not in "0123456789abcdef" for c in audience)):
            raise ValueError("BORG gateway requires its application audience and owner identity")
        email = owner_email(doc["owner_email"])
        additional = doc.get("additional_owner_emails", [])
        if not isinstance(additional, list):
            raise ValueError("BORG additional owner identities must be a list of exact emails")
        additional = tuple(owner_email(value) for value in additional)
        credential = Path(doc["authorization_file"])
        secret(credential, authorization=True)
        return cls(doc["public_url"], doc["issuer"], audience, email, credential, additional,
                   loopback_mcp_url(doc.get("upstream_url", UPSTREAM)))


def build_gateway(settings: GatewaySettings, *, verifier: JWTVerifier | None = None,
                  transport: httpx.AsyncBaseTransport | None = None) -> Starlette:
    verifier = verifier or JWTVerifier(
        jwks_uri=settings.issuer + "/cdn-cgi/access/certs",
        issuer=settings.issuer, audience=settings.audience, algorithm="RS256")
    # The library can log token claims on rejection. Keep authentication logs value-blind.
    verifier.logger = logging.getLogger("borg.gateway.jwt")
    verifier.logger.disabled = True
    owners = {settings.owner_email, *settings.additional_owner_emails}

    @asynccontextmanager
    async def lifespan(app: Starlette):
        async with httpx.AsyncClient(
                transport=transport, trust_env=False, follow_redirects=False,
                timeout=httpx.Timeout(180, connect=5)) as client:
            app.state.upstream = client
            yield

    def denied() -> JSONResponse:
        return JSONResponse({"error": "BORG owner authentication required"}, status_code=401,
            headers={"Cache-Control": "no-store", "WWW-Authenticate":
                     'Bearer resource_metadata="' + settings.public_url
                     + '/.well-known/oauth-protected-resource"'})

    async def mcp(request: Request):
        assertions = request.headers.getlist("cf-access-jwt-assertion")
        if len(assertions) != 1 or not 32 <= len(assertions[0]) <= 16384:
            return denied()
        try:
            access = await verifier.verify_token(assertions[0])
            claims = access.claims if access else {}
            now = time.time()
            exp, nbf = claims.get("exp"), claims.get("nbf", 0)
            if (not access or type(exp) not in (int, float) or not math.isfinite(exp)
                    or exp <= now or type(nbf) not in (int, float)
                    or not math.isfinite(nbf) or nbf > now
                    or not isinstance(claims.get("sub"), str) or not claims["sub"]
                    or claims.get("type") != "app"
                    or owner_email(claims.get("email")) not in owners):
                return denied()
        except Exception:
            return denied()

        # Path/query/header input never selects an upstream URL or forwards credentials.
        if request.url.query:
            return JSONResponse({"error": "MCP query parameters are unsupported"}, status_code=400)
        body = bytearray()
        async for part in request.stream():
            body.extend(part)
            if len(body) > MAX_BODY:
                return JSONResponse({"error": "MCP request too large"}, status_code=413)
        headers = {name: request.headers[name] for name in REQUEST_HEADERS if name in request.headers}
        try:
            headers["authorization"] = "Bearer " + secret(settings.authorization_file, authorization=True)
            client = request.app.state.upstream
            outgoing = client.build_request(request.method, settings.upstream_url, content=bytes(body), headers=headers)
            # httpx maintains a response cookie jar; MCP authority is only the local bearer.
            outgoing.headers.pop("cookie", None)
            upstream = await client.send(outgoing, stream=True)
        except Exception:
            return JSONResponse({"error": "BORG upstream unavailable; verify writes before retrying"},
                                status_code=502)

        headers = {name: upstream.headers[name] for name in RESPONSE_HEADERS if name in upstream.headers}
        headers["Cache-Control"] = "no-store"

        async def response_body():
            try:
                async for part in upstream.aiter_bytes():
                    yield part
            finally:
                await upstream.aclose()

        return StreamingResponse(response_body(), status_code=upstream.status_code,
                                 headers=headers, background=BackgroundTask(upstream.aclose))

    return Starlette(routes=[Route("/mcp", mcp, methods=["GET", "POST", "DELETE"])],
                     lifespan=lifespan)


def server_config(app: Starlette, port: int = 8772) -> uvicorn.Config:
    return uvicorn.Config(app, host="127.0.0.1", port=port, access_log=False,
        log_level="warning", proxy_headers=False, server_header=False,
        timeout_graceful_shutdown=5)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=borg_home() / "borg-context/cloudflare/config.json")
    parser.add_argument("--http", type=int, default=8772)
    args = parser.parse_args()
    from process_resources import configure_descriptor_limit
    configure_descriptor_limit()
    settings = GatewaySettings.load(args.config)
    uvicorn.Server(server_config(build_gateway(settings), args.http)).run()
