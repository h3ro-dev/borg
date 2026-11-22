#!/usr/bin/env python3
"""Validate signed owner identity before forwarding BORG web MCP traffic.

Supports Cloudflare Access Managed OAuth (``cloudflare_access``) as well as
standard OAuth 2.1 / OIDC Bearer JWT providers (``oidc_bearer``) with optional
explicit HTTPS CORS origins for direct web clients.
"""
from __future__ import annotations

import asyncio
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
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from borg_context_server import private_text, secret
from runtime_paths import borg_home, loopback_mcp_url

UPSTREAM = "http://127.0.0.1:8770/mcp"
MAX_BODY = 4 * 1024 * 1024
REQUEST_HEADERS = ("accept", "content-type", "mcp-session-id", "mcp-protocol-version", "last-event-id")
RESPONSE_HEADERS = ("content-type", "mcp-session-id", "mcp-protocol-version", "allow")
AUTH_MODES = frozenset({"cloudflare_access", "oidc_bearer"})
ALLOWED_ALGORITHMS = frozenset({"RS256", "RS384", "RS512", "ES256", "ES384", "ES512", "EdDSA", "PS256"})
CORS_ALLOW_HEADERS = "Authorization, Content-Type, Accept, Mcp-Session-Id, Mcp-Protocol-Version, Last-Event-Id"
CORS_EXPOSE_HEADERS = "Mcp-Session-Id, Mcp-Protocol-Version"


def owner_email(value: object) -> str:
    if (not isinstance(value, str)
            or not re.fullmatch(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+", value)):
        raise ValueError("BORG gateway requires an exact owner email address")
    return value.casefold()


def https_origin(value: object, *, allow_port: bool = False) -> str:
    if not isinstance(value, str):
        raise ValueError("BORG gateway origin must be an HTTPS origin string")
    parsed = urlsplit(value)
    valid_netloc = (parsed.netloc == parsed.hostname if not allow_port
                    else bool(parsed.hostname and not parsed.username and not parsed.password))
    if (parsed.scheme != "https" or not parsed.hostname or not valid_netloc
            or parsed.path or parsed.query or parsed.fragment):
        raise ValueError("BORG gateway requires an exact HTTPS origin without path or query")
    return value


def https_url(value: object) -> str:
    if not isinstance(value, str) or len(value) > 2048:
        raise ValueError("BORG gateway URL must be an HTTPS URL string")
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment):
        raise ValueError("BORG gateway requires an explicit HTTPS URL without credentials or query")
    return value.rstrip("/")


@dataclass(frozen=True)
class GatewaySettings:
    public_url: str
    issuer: str
    audience: str
    owner_email: str
    authorization_file: Path
    additional_owner_emails: tuple[str, ...] = ()
    upstream_url: str = UPSTREAM
    auth_mode: str = "cloudflare_access"
    jwks_uri: str | None = None
    algorithm: str = "RS256"
    allowed_subjects: tuple[str, ...] = ()
    allowed_origins: tuple[str, ...] = ()

    @classmethod
    def load(cls, path: Path) -> "GatewaySettings":
        doc = json.loads(private_text(path))
        if doc.get("version") != 1:
            raise ValueError("BORG gateway requires configuration version 1")
        auth_mode = doc.get("auth_mode", "cloudflare_access")
        if auth_mode not in AUTH_MODES:
            raise ValueError("BORG gateway auth_mode must be cloudflare_access or oidc_bearer")
        algorithm = doc.get("algorithm", "RS256")
        if not isinstance(algorithm, str) or algorithm not in ALLOWED_ALGORITHMS:
            raise ValueError("BORG gateway requires an allowlisted asymmetric JWT algorithm")

        public_url = https_origin(doc.get("public_url"))
        issuer_raw = doc.get("issuer")
        if auth_mode == "cloudflare_access":
            issuer_parts = urlsplit(issuer_raw) if isinstance(issuer_raw, str) else None
            if (issuer_parts is None or issuer_parts.scheme != "https" or not issuer_parts.hostname
                    or not issuer_parts.hostname.endswith(".cloudflareaccess.com")
                    or issuer_parts.netloc != issuer_parts.hostname or issuer_parts.path
                    or issuer_parts.query or issuer_parts.fragment):
                raise ValueError("BORG gateway requires explicit HTTPS origin and Cloudflare issuer")
            issuer = issuer_raw
            jwks_uri = doc.get("jwks_uri")
            if jwks_uri is not None:
                jwks_uri = https_url(jwks_uri)
            audience = doc.get("audience")
            if (not isinstance(audience, str) or len(audience) != 64
                    or any(c not in "0123456789abcdef" for c in audience)):
                raise ValueError("BORG gateway requires its application audience and owner identity")
            email = owner_email(doc.get("owner_email"))
        else:
            issuer = https_url(issuer_raw)
            jwks_uri = https_url(doc.get("jwks_uri"))
            audience = doc.get("audience")
            if not isinstance(audience, str) or not 1 <= len(audience) <= 256 or any(c.isspace() for c in audience):
                raise ValueError("BORG OIDC gateway requires a non-empty audience")
            raw_email = doc.get("owner_email")
            email = owner_email(raw_email) if raw_email else ""

        additional = doc.get("additional_owner_emails", [])
        if not isinstance(additional, list):
            raise ValueError("BORG additional owner identities must be a list of exact emails")
        additional_emails = tuple(owner_email(value) for value in additional)

        raw_subjects = doc.get("allowed_subjects", [])
        if (not isinstance(raw_subjects, list) or len(raw_subjects) > 64
                or any(not isinstance(s, str) or not 1 <= len(s) <= 256 or any(ord(c) < 32 for c in s)
                       for s in raw_subjects)):
            raise ValueError("BORG allowed_subjects must be a list of non-empty subject identifiers")
        allowed_subjects = tuple(raw_subjects)
        if auth_mode == "oidc_bearer" and not email and not additional_emails and not allowed_subjects:
            raise ValueError("BORG OIDC gateway requires at least one owner email or allowed subject")

        raw_origins = doc.get("allowed_origins", [])
        if not isinstance(raw_origins, list) or len(raw_origins) > 32:
            raise ValueError("BORG allowed_origins must be a list of exact HTTPS origins")
        allowed_origins = tuple(https_origin(origin, allow_port=True) for origin in raw_origins)

        credential = Path(doc["authorization_file"])
        secret(credential, authorization=True)
        return cls(
            public_url, issuer, audience, email, credential, additional_emails,
            loopback_mcp_url(doc.get("upstream_url", UPSTREAM)),
            auth_mode=auth_mode, jwks_uri=jwks_uri, algorithm=algorithm,
            allowed_subjects=allowed_subjects, allowed_origins=allowed_origins,
        )


def build_gateway(settings: GatewaySettings, *, verifier: JWTVerifier | None = None,
                  transport: httpx.AsyncBaseTransport | None = None) -> Starlette:
    jwks_uri = settings.jwks_uri or (settings.issuer + "/cdn-cgi/access/certs")
    verifier = verifier or JWTVerifier(
        jwks_uri=jwks_uri,
        issuer=settings.issuer, audience=settings.audience, algorithm=settings.algorithm)
    # The library can log token claims on rejection. Keep authentication logs value-blind.
    verifier.logger = logging.getLogger("borg.gateway.jwt")
    verifier.logger.disabled = True
    owners = {e for e in (settings.owner_email, *settings.additional_owner_emails) if e}
    subjects = set(settings.allowed_subjects)
    allowed_origins = set(settings.allowed_origins)

    @asynccontextmanager
    async def lifespan(app: Starlette):
        async with httpx.AsyncClient(
                transport=transport, trust_env=False, follow_redirects=False,
                timeout=httpx.Timeout(180, connect=5)) as client:
            app.state.upstream = client
            yield

    def cors_headers(origin: str | None) -> dict[str, str]:
        if origin and origin in allowed_origins:
            return {
                "Access-Control-Allow-Origin": origin,
                "Access-Control-Expose-Headers": CORS_EXPOSE_HEADERS,
                "Vary": "Origin",
            }
        return {}

    def check_origin(request: Request) -> tuple[str | None, JSONResponse | None]:
        origin = request.headers.get("origin")
        if not allowed_origins:
            return None, None
        if origin is None:
            return None, None
        if origin not in allowed_origins:
            return None, JSONResponse(
                {"error": "BORG origin is not allowed"}, status_code=403,
                headers={"Cache-Control": "no-store", "Vary": "Origin"},
            )
        return origin, None

    def denied(origin: str | None = None) -> JSONResponse:
        return JSONResponse({"error": "BORG owner authentication required"}, status_code=401,
            headers={"Cache-Control": "no-store", "WWW-Authenticate":
                     'Bearer resource_metadata="' + settings.public_url
                     + '/.well-known/oauth-protected-resource"',
                     **cors_headers(origin)})

    def extract_token(request: Request) -> str | None:
        if settings.auth_mode == "cloudflare_access":
            assertions = request.headers.getlist("cf-access-jwt-assertion")
            if len(assertions) != 1 or not 32 <= len(assertions[0]) <= 16384:
                return None
            return assertions[0]
        auth_headers = request.headers.getlist("authorization")
        if len(auth_headers) != 1 or not auth_headers[0].startswith("Bearer "):
            return None
        token = auth_headers[0][7:]
        if not 32 <= len(token) <= 16384 or any(c.isspace() for c in token):
            return None
        return token

    def claims_authorized(claims: dict) -> bool:
        now = time.time()
        exp, nbf = claims.get("exp"), claims.get("nbf", 0)
        if (type(exp) not in (int, float) or not math.isfinite(exp) or exp <= now
                or type(nbf) not in (int, float) or not math.isfinite(nbf) or nbf > now
                or not isinstance(claims.get("sub"), str) or not claims["sub"]):
            return False
        if settings.auth_mode == "cloudflare_access":
            return claims.get("type") == "app" and owner_email(claims.get("email")) in owners
        # OIDC Bearer mode: match exact verified owner email or explicit allowed subject.
        if subjects and claims["sub"] in subjects:
            return True
        if owners and claims.get("email_verified") is not False:
            try:
                return owner_email(claims.get("email")) in owners
            except ValueError:
                return False
        return False

    async def protected_resource_metadata(request: Request):
        origin, blocked = check_origin(request)
        if blocked is not None:
            return blocked
        if request.method == "OPTIONS":
            if not origin:
                return JSONResponse({"error": "BORG origin is not allowed"}, status_code=403)
            return Response(status_code=204, headers={
                "Access-Control-Allow-Origin": origin,
                "Access-Control-Allow-Methods": "GET, OPTIONS",
                "Access-Control-Allow-Headers": "Accept",
                "Access-Control-Max-Age": "600",
                "Vary": "Origin",
                "Cache-Control": "no-store",
            })
        return JSONResponse({
            "resource": settings.public_url,
            "authorization_servers": [settings.issuer],
            "bearer_methods_supported": ["header"],
        }, headers={"Cache-Control": "max-age=300", **cors_headers(origin)})

    async def mcp(request: Request):
        origin, blocked = check_origin(request)
        if blocked is not None:
            return blocked
        if request.method == "OPTIONS":
            if not origin:
                return JSONResponse({"error": "BORG origin is not allowed"}, status_code=403)
            return Response(status_code=204, headers={
                "Access-Control-Allow-Origin": origin,
                "Access-Control-Allow-Methods": "GET, POST, DELETE, OPTIONS",
                "Access-Control-Allow-Headers": CORS_ALLOW_HEADERS,
                "Access-Control-Expose-Headers": CORS_EXPOSE_HEADERS,
                "Access-Control-Max-Age": "600",
                "Vary": "Origin",
                "Cache-Control": "no-store",
            })
        token = extract_token(request)
        if token is None:
            return denied(origin)
        try:
            access = await verifier.verify_token(token)
            claims = access.claims if access else {}
            if not access or not claims_authorized(claims):
                return denied(origin)
        except Exception:
            return denied(origin)

        # Path/query/header input never selects an upstream URL or forwards credentials.
        if request.url.query:
            return JSONResponse({"error": "MCP query parameters are unsupported"}, status_code=400,
                                headers=cors_headers(origin))
        body = bytearray()
        async for part in request.stream():
            body.extend(part)
            if len(body) > MAX_BODY:
                return JSONResponse({"error": "MCP request too large"}, status_code=413,
                                    headers=cors_headers(origin))
        headers = {name: request.headers[name] for name in REQUEST_HEADERS if name in request.headers}
        try:
            local_token = await asyncio.to_thread(secret, settings.authorization_file, authorization=True)
            headers["authorization"] = "Bearer " + local_token
            client = request.app.state.upstream
            outgoing = client.build_request(request.method, settings.upstream_url, content=bytes(body), headers=headers)
            # httpx maintains a response cookie jar; MCP authority is only the local bearer.
            outgoing.headers.pop("cookie", None)
            upstream = await client.send(outgoing, stream=True)
        except Exception:
            return JSONResponse({"error": "BORG upstream unavailable; verify writes before retrying"},
                                status_code=502, headers=cors_headers(origin))

        response_headers = {name: upstream.headers[name] for name in RESPONSE_HEADERS if name in upstream.headers}
        response_headers["Cache-Control"] = "no-store"
        response_headers.update(cors_headers(origin))

        async def response_body():
            try:
                async for part in upstream.aiter_bytes():
                    yield part
            except (httpx.StreamError, httpx.HTTPError):
                # Peer disconnect or upstream reset mid-stream; avoid unhandled task-group crashes.
                return
            finally:
                await upstream.aclose()

        return StreamingResponse(response_body(), status_code=upstream.status_code,
                                 headers=response_headers, background=BackgroundTask(upstream.aclose))

    mcp_methods = ["GET", "POST", "DELETE", "OPTIONS"] if allowed_origins else ["GET", "POST", "DELETE"]
    routes = [Route("/mcp", mcp, methods=mcp_methods)]
    if settings.auth_mode == "oidc_bearer":
        meta_methods = ["GET", "OPTIONS"] if allowed_origins else ["GET"]
        routes.append(Route("/.well-known/oauth-protected-resource", protected_resource_metadata, methods=meta_methods))
    return Starlette(routes=routes, lifespan=lifespan)


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
