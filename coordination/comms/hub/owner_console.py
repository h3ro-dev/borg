"""Local owner console: credentials stay in the local process, never the page.

Like the local admin CLI, this trusts programs under the owner's OS account.
It binds IPv4 loopback only. Do not expose this listener through a public proxy.
The separate central hub continues to authenticate every remote principal.
"""
import argparse
import copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .client import Client, ClientError
from .projection import sanitized_projection

WEB = Path(__file__).with_name("web")
MAX_BYTES = 128 * 1024


class OwnerServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, client, port=0):
        self.client = client
        self.projection_client = copy.copy(client)
        if hasattr(self.projection_client, "timeout"):
            self.projection_client.timeout = min(self.projection_client.timeout, 2.0)
        super().__init__(("127.0.0.1", port), OwnerHandler)
        self.origin = "http://127.0.0.1:" + str(self.server_port)


class OwnerHandler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def log_message(self, *_):
        pass

    def respond(self, status, value, content_type="application/json"):
        body = value if isinstance(value, bytes) else json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; "
                         "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
        self.end_headers()
        self.wfile.write(body)

    def error(self, status, code):
        self.respond(status, {"error": {"code": code, "message": code.replace("_", " ")}})

    def valid_host(self):
        expected = "127.0.0.1:" + str(self.server.server_port)
        if self.headers.get_all("Host", []) != [expected]:
            self.error(403, "local_console_only")
            return False
        return True

    def do_GET(self):
        if not self.valid_host():
            return
        path = unquote(urlsplit(self.path).path)
        if path == "/health":
            self.respond(200, {"owner_console": True})
            return
        if path == "/projection":
            if (self.headers.get("Origin") not in (None, self.server.origin)
                    or self.headers.get("Sec-Fetch-Site") not in (None, "none", "same-origin")):
                self.error(403, "foreign_origin")
                return
            try:
                snapshot = self.server.projection_client.call("owner.snapshot", {"scope": "/", "limit": 100})
                self.respond(200, sanitized_projection(snapshot))
            except Exception:
                # Never manufacture a fresh empty snapshot or expose source errors.
                self.error(503, "projection_unavailable")
            return
        if path == "/realm.json":
            candidate = WEB.parent / "realm.json"
        else:
            relative = "index.html" if path in ("/", "/ui/", "/ui") else path.removeprefix("/ui/").lstrip("/")
            candidate = (WEB / relative).resolve()
            try:
                candidate.relative_to(WEB.resolve())
            except ValueError:
                self.error(404, "not_found")
                return
        if not candidate.is_file():
            self.error(404, "not_found")
            return
        self.respond(200, candidate.read_bytes(), mimetypes.guess_type(candidate.name)[0] or "application/octet-stream")

    def do_POST(self):
        if not self.valid_host():
            return
        if self.headers.get("Origin") != self.server.origin:
            self.error(403, "foreign_origin")
            return
        if self.path != "/v1/call":
            self.error(404, "not_found")
            return
        lengths = self.headers.get_all("Content-Length", [])
        if len(lengths) != 1 or not lengths[0].isdigit() or self.headers.get("Transfer-Encoding"):
            self.error(400, "invalid_framing")
            return
        if self.headers.get_content_type() != "application/json":
            self.error(415, "json_required")
            return
        length = int(lengths[0])
        if length > MAX_BYTES:
            self.error(413, "request_too_large")
            return
        try:
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict) or not isinstance(body.get("operation"), str):
                raise ValueError("invalid request")
            result = self.server.client.call(body["operation"], body.get("params", {}),
                                             request_id=body.get("request_id"))
            self.respond(200, result)
        except (ValueError, TypeError):
            self.error(400, "invalid_request")
        except ClientError as exc:
            self.respond(exc.status if 400 <= (exc.status or 0) <= 599 else 503,
                         {"error": {"code": exc.code, "message": exc.message}})

    def do_OPTIONS(self):
        self.error(403, "foreign_origin")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args()
    server = OwnerServer(Client.from_config(args.config), args.port)
    print(json.dumps({"url": server.origin + "/ui/", "scope": "local owner console"}), flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
