"""Small BORG-owned Chrome DevTools browser bridge.

The bridge talks directly to a dedicated Chrome process over its local CDP
socket. It does not load Desktop Commander, Peekaboo, Playwright MCP or any
other computer MCP backend. Browser profiles are private to BORG and are never
returned in tool results.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import signal
import socket
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from fastmcp.exceptions import ToolError

MAX_TEXT = 120_000
MAX_INPUT = 12_000
MAX_SCREENSHOT_BYTES = 1_000_000
TOOL_NAMES = ["browser_start_session", "browser_list_sessions", "browser_list_tabs",
              "browser_navigate", "browser_snapshot", "browser_screenshot",
              "browser_click", "browser_type", "browser_close_session"]


def _id(value: str) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except (TypeError, ValueError, AttributeError):
        raise ToolError("BORG browser session ID is invalid") from None


def _url(value: str) -> str:
    if not isinstance(value, str) or len(value) > 4096:
        raise ToolError("BORG browser URL is invalid")
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme not in {"http", "https", "about"} or (parsed.scheme in {"http", "https"} and not parsed.hostname):
        raise ToolError("BORG browser accepts only http, https or about URLs")
    return value


class CDP:
    def __init__(self, websocket_url: str, timeout: float = 10.0):
        parsed = urllib.parse.urlsplit(websocket_url)
        if parsed.scheme != "ws" or parsed.hostname is None or parsed.port is None:
            raise ToolError("BORG browser returned an invalid CDP endpoint")
        self.sock = socket.create_connection((parsed.hostname, parsed.port), timeout=timeout)
        self.sock.settimeout(timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        request = (f"GET {path} HTTP/1.1\r\nHost: {parsed.hostname}:{parsed.port}\r\n"
                   f"Upgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
                   "Sec-WebSocket-Version: 13\r\n\r\n").encode()
        self.sock.sendall(request)
        response = self._read_until(b"\r\n\r\n", 16_384)
        if not response.startswith(b"HTTP/1.1 101"):
            self.sock.close()
            raise ToolError("BORG browser CDP handshake failed")
        self.next_id = 1

    def _read_until(self, marker: bytes, limit: int) -> bytes:
        data = bytearray()
        while marker not in data and len(data) < limit:
            chunk = self.sock.recv(4096)
            if not chunk:
                break
            data.extend(chunk)
        return bytes(data)

    def _frame(self, payload: bytes, opcode: int = 1) -> None:
        mask = os.urandom(4)
        masked = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
        length = len(masked)
        if length < 126:
            header = bytes([0x80 | opcode, 0x80 | length])
        elif length < 65_536:
            header = bytes([0x80 | opcode, 0x80 | 126]) + length.to_bytes(2, "big")
        else:
            header = bytes([0x80 | opcode, 0x80 | 127]) + length.to_bytes(8, "big")
        self.sock.sendall(header + mask + masked)

    def _read_frame(self) -> tuple[int, bytes]:
        header = self._recv_exact(2)
        first, second = header
        opcode = first & 0x0F
        length = second & 0x7F
        if length == 126:
            length = int.from_bytes(self._recv_exact(2), "big")
        elif length == 127:
            length = int.from_bytes(self._recv_exact(8), "big")
        if length > 8_000_000:
            raise ToolError("BORG browser frame exceeds the bounded response size")
        mask = self._recv_exact(4) if second & 0x80 else None
        payload = self._recv_exact(length)
        if mask:
            payload = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
        return opcode, payload

    def _recv_exact(self, length: int) -> bytes:
        data = bytearray()
        while len(data) < length:
            chunk = self.sock.recv(length - len(data))
            if not chunk:
                raise ToolError("BORG browser CDP connection closed")
            data.extend(chunk)
        return bytes(data)

    def call(self, method: str, params: dict | None = None) -> dict:
        request_id = self.next_id; self.next_id += 1
        self._frame(json.dumps({"id": request_id, "method": method, "params": params or {}}).encode())
        while True:
            opcode, payload = self._read_frame()
            if opcode == 0x9:
                self._frame(payload, opcode=0xA); continue
            if opcode == 0x8:
                raise ToolError("BORG browser CDP connection closed")
            if opcode != 0x1:
                continue
            message = json.loads(payload.decode("utf-8"))
            if message.get("id") != request_id:
                continue
            if "error" in message:
                raise ToolError("BORG browser operation failed")
            return message.get("result", {})

    def close(self) -> None:
        try:
            self._frame(b"", opcode=0x8)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


class BrowserStore:
    def __init__(self, config: dict):
        from runtime_paths import borg_home
        self.root = Path(config.get("root") or (borg_home() / "borg-context/browser"))
        if self.root.is_symlink():
            raise ValueError("BORG browser root cannot be a symlink")
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True); os.chmod(self.root, 0o700)
        self.chrome = str(config.get("chrome_path") or "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
        self.default_headless = bool(config.get("headless_default", True))
        self.processes: dict[str, subprocess.Popen] = {}

    def _dir(self, session_id: str) -> Path:
        return self.root / _id(session_id)

    def _meta(self, session_id: str) -> Path:
        return self._dir(session_id) / "session.json"

    @staticmethod
    def _pid_identity(pid: int) -> str | None:
        try:
            result = subprocess.run(["/bin/ps", "-p", str(pid), "-o", "lstart="], capture_output=True,
                                    text=True, timeout=2, check=False)
        except (OSError, subprocess.TimeoutExpired):
            return None
        value = result.stdout.strip(); return value or None

    def _write_meta(self, path: Path, row: dict) -> None:
        fd, temp_name = tempfile.mkstemp(prefix=".borg-browser-", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(row, handle, sort_keys=True, separators=(",", ":")); handle.write("\n"); handle.flush(); os.fsync(handle.fileno())
            os.chmod(temp_name, 0o600); os.replace(temp_name, path)
        finally:
            try: os.unlink(temp_name)
            except FileNotFoundError: pass

    def _read_meta(self, session_id: str) -> dict:
        path = self._meta(session_id)
        try:
            if path.is_symlink() or path.stat().st_size > 32_000: raise ValueError
            row = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            raise ToolError("BORG browser session metadata is unavailable") from None
        if not isinstance(row, dict) or row.get("session_id") != _id(session_id):
            raise ToolError("BORG browser session metadata is invalid")
        return row

    @staticmethod
    def _port() -> int:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0)); return int(sock.getsockname()[1])

    @staticmethod
    def _http_json(url: str, method: str = "GET") -> object:
        try:
            request = urllib.request.Request(url, method=method)
            with urllib.request.urlopen(request, timeout=3) as response:
                return json.loads(response.read(1_000_000).decode("utf-8"))
        except Exception:
            return None

    def _targets(self, port: int) -> list[dict]:
        value = self._http_json(f"http://127.0.0.1:{port}/json/list")
        return value if isinstance(value, list) else []

    def _connect(self, row: dict) -> CDP:
        port = int(row.get("port", 0)); targets = self._targets(port)
        target = next((item for item in targets if item.get("id") == row.get("target_id") and item.get("webSocketDebuggerUrl")), None)
        target = target or next((item for item in targets if item.get("type") == "page" and item.get("webSocketDebuggerUrl")), None)
        if target is None:
            raise ToolError("BORG browser page target is unavailable")
        return CDP(str(target["webSocketDebuggerUrl"]))

    def start_session(self, url: str = "about:blank", headless: bool | None = None) -> dict:
        url = _url(url); session_id = str(uuid.uuid4()); directory = self._dir(session_id); profile = directory / "profile"
        profile.mkdir(mode=0o700, parents=True); os.chmod(profile, 0o700)
        port = self._port(); headless = self.default_headless if headless is None else bool(headless)
        command = [self.chrome, f"--remote-debugging-port={port}", f"--user-data-dir={profile}",
                   "--no-first-run", "--no-default-browser-check", "--disable-sync", "--disable-background-networking",
                   "--remote-allow-origins=http://127.0.0.1"]
        if headless: command.extend(["--headless=new", "--disable-gpu"])
        command.append(url)
        try:
            proc = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, start_new_session=True)
        except OSError:
            shutil.rmtree(directory, ignore_errors=True)
            raise ToolError("BORG native browser executable is unavailable") from None
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            if self._http_json(f"http://127.0.0.1:{port}/json/version"):
                break
            time.sleep(0.2)
        else:
            proc.terminate(); shutil.rmtree(directory, ignore_errors=True)
            raise ToolError("BORG native browser did not become ready")
        targets = self._targets(port)
        target = next((item for item in targets if item.get("type") == "page" and item.get("webSocketDebuggerUrl")), None)
        if target is None:
            proc.terminate(); shutil.rmtree(directory, ignore_errors=True)
            raise ToolError("BORG native browser page target did not become ready")
        row = {"version": 1, "session_id": session_id, "pid": proc.pid,
               "pid_start": self._pid_identity(proc.pid), "port": port,
               "target_id": target.get("id"), "headless": headless, "state": "running", "created_at": time.time()}
        self._write_meta(self._meta(session_id), row); self.processes[session_id] = proc
        return {k: row[k] for k in ("session_id", "state", "headless", "created_at")}

    def list_sessions(self, limit: int = 20) -> dict:
        rows = []
        for path in sorted(self.root.glob("*/session.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:max(1, min(int(limit), 50))]:
            try:
                row = self._read_meta(path.parent.name); alive = self._pid_identity(int(row.get("pid", 0))) == row.get("pid_start")
                if row.get("state") == "running" and not alive:
                    # Discovery must not overwrite a concurrent close/update.
                    row["state"] = "outcome_unknown"
                rows.append({k: row.get(k) for k in ("session_id", "state", "headless", "created_at")})
            except ToolError:
                continue
        return {"sessions": rows}

    def list_tabs(self, session_id: str) -> dict:
        row = self._read_meta(session_id); targets = self._targets(int(row["port"]))
        return {"session_id": _id(session_id), "tabs": [{k: item.get(k) for k in ("id", "title", "url", "type")}
                for item in targets if item.get("type") == "page"]}

    def _eval(self, session_id: str, expression: str) -> object:
        row = self._read_meta(session_id); cdp = self._connect(row)
        try:
            result = cdp.call("Runtime.evaluate", {"expression": expression, "returnByValue": True, "awaitPromise": True})
            return (result.get("result") or {}).get("value")
        finally:
            cdp.close()

    def navigate(self, session_id: str, url: str) -> dict:
        url = _url(url); row = self._read_meta(session_id); cdp = self._connect(row)
        try: result = cdp.call("Page.navigate", {"url": url})
        finally: cdp.close()
        return {"session_id": _id(session_id), "url": url, "frame_id": result.get("frameId")}

    def snapshot(self, session_id: str) -> dict:
        expression = """(() => ({title: document.title, url: location.href,
          text: (document.body && document.body.innerText || '').slice(0, 120000),
          links: Array.from(document.querySelectorAll('a')).slice(0,100).map(a => ({text:(a.innerText||'').trim().slice(0,200), href:a.href})),
          controls: Array.from(document.querySelectorAll('input,button,select,textarea')).slice(0,100).map(e => ({tag:e.tagName.toLowerCase(),type:e.type||'',name:e.name||'',id:e.id||'',label:(e.innerText||e.getAttribute('aria-label')||'').trim().slice(0,200)}))}))()"""
        value = self._eval(session_id, expression)
        if not isinstance(value, dict): raise ToolError("BORG browser snapshot is unavailable")
        return {"session_id": _id(session_id), **value}

    def screenshot(self, session_id: str) -> dict:
        row = self._read_meta(session_id); cdp = self._connect(row)
        try: result = cdp.call("Page.captureScreenshot", {"format": "png", "fromSurface": True})
        finally: cdp.close()
        encoded = str(result.get("data", "")); raw_size = len(base64.b64decode(encoded)) if encoded else 0
        if raw_size > MAX_SCREENSHOT_BYTES: raise ToolError("BORG browser screenshot exceeds the bounded size")
        return {"session_id": _id(session_id), "mime_type": "image/png", "bytes": raw_size,
                "sha256": hashlib.sha256(base64.b64decode(encoded)).hexdigest(), "content_base64": encoded}

    def click(self, session_id: str, selector: str) -> dict:
        if not selector or len(selector) > 500: raise ToolError("BORG browser selector is invalid")
        expression = "(s => { const e=document.querySelector(s); if(!e) return false; e.click(); return true; })(" + json.dumps(selector) + ")"
        if self._eval(session_id, expression) is not True: raise ToolError("BORG browser selector did not match")
        return {"session_id": _id(session_id), "clicked": True}

    def type(self, session_id: str, selector: str, text: str) -> dict:
        if not selector or len(selector) > 500 or not isinstance(text, str) or len(text) > MAX_INPUT:
            raise ToolError("BORG browser input is invalid or too long")
        row = self._read_meta(session_id); cdp = self._connect(row)
        try:
            expression = "(s => { const e=document.querySelector(s); if(!e) return false; e.focus(); return true; })(" + json.dumps(selector) + ")"
            result = cdp.call("Runtime.evaluate", {"expression": expression, "returnByValue": True})
            if (result.get("result") or {}).get("value") is not True: raise ToolError("BORG browser selector did not match")
            cdp.call("Input.insertText", {"text": text})
        finally: cdp.close()
        return {"session_id": _id(session_id), "typed": len(text)}

    def close_session(self, session_id: str) -> dict:
        key = _id(session_id); row = self._read_meta(key); pid = int(row.get("pid", 0))
        if self._pid_identity(pid) != row.get("pid_start"):
            row["state"] = "outcome_unknown"
        else:
            try: os.killpg(pid, signal.SIGTERM)
            except ProcessLookupError: pass
            row["state"] = "closed"
        row["closed_at"] = time.time(); self._write_meta(self._meta(key), row)
        return {"session_id": key, "state": row["state"]}


def mount_browser(server, config, handoff=None):
    store = BrowserStore(config)
    annotations = {
        "browser_start_session": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True},
        "browser_list_sessions": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
        "browser_list_tabs": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
        "browser_navigate": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True},
        "browser_snapshot": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
        "browser_screenshot": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
        "browser_click": {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": True},
        "browser_type": {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": True},
        "browser_close_session": {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True, "openWorldHint": False},
    }
    descriptions = {
        "browser_start_session": "Start a BORG-owned dedicated Chrome session; profile and cookies stay local and are never returned.",
        "browser_list_sessions": "List BORG browser sessions by stable ID and lifecycle state.",
        "browser_list_tabs": "List page tabs in one owned BORG browser session.",
        "browser_navigate": "Navigate an owned BORG browser tab to an HTTP(S) or about URL.",
        "browser_snapshot": "Read a bounded page text, link and form-control snapshot from an owned browser tab.",
        "browser_screenshot": "Capture a bounded PNG screenshot from an owned browser tab.",
        "browser_click": "Click one CSS-selected element in an owned BORG browser tab.",
        "browser_type": "Focus one CSS-selected element and type text without returning the text.",
        "browser_close_session": "Close one owned BORG browser session after process identity verification.",
    }
    for name in TOOL_NAMES:
        function = getattr(store, name.removeprefix("browser_"))
        if handoff:
            function = handoff.store_function(store, name, function)
        server.tool(name=name, annotations=annotations[name], description=descriptions[name])(function)
    return store
