"""Native computer operations and value-blind connector audit.

The shell is owner-level authority, not a sandbox. Output checks reduce accidental
credential disclosure; they cannot make arbitrary programs safe to run.
"""
from __future__ import annotations

import asyncio
import base64
import fnmatch
import hashlib
import json
import logging
import os
import re
import select
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Literal

from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import Middleware
from mcp.types import ToolAnnotations

LOG = logging.getLogger("borg.audit")
DESCRIPTIONS = {
    "read_file": "Read a local file, including images and supported document formats. Use absolute paths and bounded offsets. Never read credential or browser authentication stores.",
    "read_multiple_files": "Read several named local files. Never read credential or browser authentication stores.",
    "write_file": "Write or append a local text file. Inspect existing content and preserve a recovery copy before replacing it; verify the resulting file.",
    "write_pdf": "Create a PDF at an absolute local path from the supplied content. Verify the resulting artifact.",
    "create_directory": "Create a directory at an absolute local path.",
    "list_directory": "List a local directory with bounded depth.",
    "move_file": "Move or rename a local file or directory. Inspect source and destination first and preserve a recovery path.",
    "start_search": "Start a bounded local filename or content search. Prefer specific roots and patterns; exclude credential stores.",
    "get_more_search_results": "Read the next page from a search session started by this connector.",
    "stop_search": "Stop a search session started by this connector.",
    "list_searches": "List this connector's current search sessions.",
    "get_file_info": "Read a local file's metadata.",
    "edit_block": "Replace an exact text block in a local file. Read the current content, make the smallest requested change, then verify it.",
    "start_process": "Run a local command as the macOS user, or start an interactive process. Use existing installed tools. Inspect current state and applicable AGENTS.md, preserve other agents' work, and verify changes. Never print secrets or environments; use vault/hub pipes into local consumers. Login, MFA and consent belong in the normal provider interface. OS permissions still apply. An uncertain write result must be checked before retrying.",
    "read_process_output": "Read bounded output from a command session started by this connector. Length and offset are bytes; omit offset to continue from the previous read. Explicit offsets replay retained output (up to 2 MB). Never request credential output.",
    "interact_with_process": "Send input to a command session started by this connector. Never send credentials in tool arguments.",
    "force_terminate": "Terminate a command session started by this connector after checking its ownership and current state.",
    "list_sessions": "List command sessions started by this connector.",
}
READS = {"read_file", "read_multiple_files", "list_directory", "get_file_info",
         "get_more_search_results", "list_searches", "read_process_output", "list_sessions"}
DESTRUCTIVE = {"write_file", "write_pdf", "move_file", "edit_block", "start_process",
               "interact_with_process", "force_terminate"}
SECRET_PATTERN = re.compile(
    r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----|"
    r"\b(?:sk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{24,}|gh[pousr]_[A-Za-z0-9]{24,}|github_pat_[A-Za-z0-9_]{24,})\b|"
    r"\bBearer\s+[A-Za-z0-9._~+/=-]{24,}|"
    r"\b(?:api[_-]?key|access[_-]?token|password|client[_-]?secret)\s*[:=]\s*['\"]?[A-Za-z0-9_./+=-]{16,}",
    re.IGNORECASE,
)


def contains_secret(value: str) -> bool:
    return bool(SECRET_PATTERN.search(value))


FAILURE_MESSAGES = {
    "credential_withheld": "Credential material was withheld. Verify any preceding change before retrying.",
    "authentication_required": "Authentication or a user-owned sign-in step is required.",
    "busy": "BORG host capacity or the selected resource is busy. Check concurrency in borg_status; reconcile any uncertain write before retrying.",
    "permission_denied": "The native operation lacks a required operating-system or provider permission.",
    "policy_refused": "The downstream execution policy refused this operation. Do not bypass it; use a narrower supported operation or inspect admission requirements.",
    "admission_required": "The operation needs current ownership, lease, claim, or fleet admission before execution.",
    "capability_unavailable": "The requested capability is unavailable on the selected backend or target.",
    "rate_limited": "The downstream provider or backend is rate limited.",
    "timeout": "The downstream operation timed out; verify whether it changed state before retrying.",
    "provider_unavailable": "The downstream provider or backend is currently unavailable.",
    "downstream_error": "The downstream operation failed. Verify any possible state change before retrying.",
}


def classify_failure(value) -> str:
    """Classify a failure without returning or logging payload-bearing backend text."""
    try:
        text = json.dumps(value, default=str, ensure_ascii=True).casefold()
    except Exception:
        text = type(value).__name__.casefold()
    if contains_secret(text) or "credential" in text or "secret" in text or "token material" in text:
        return "credential_withheld"
    if "borg_busy" in text or "capability is busy" in text or "queue is full" in text:
        return "busy"
    if any(term in text for term in ("authentication required", "login required", "sign-in", "sign in", "unauthenticated")):
        return "authentication_required"
    if any(term in text for term in ("permission denied", "not permitted", "accessibility permission", "screen recording permission")):
        return "permission_denied"
    if any(term in text for term in ("blocked by policy", "policy refused", "policy denial", "safety policy", "execution policy")):
        return "policy_refused"
    if any(term in text for term in ("admission", "lease lost", "lease_lost", "ownership", "work claim", "claim required")):
        return "admission_required"
    if any(term in text for term in ("not supported", "unsupported", "capability", "tool unavailable", "driver unavailable")):
        return "capability_unavailable"
    if "rate limit" in text or "rate_limited" in text or "too many requests" in text:
        return "rate_limited"
    if "timeout" in text or "timed out" in text or "deadline exceeded" in text:
        return "timeout"
    if any(term in text for term in ("provider unavailable", "backend unavailable", "connection refused", "service unavailable")):
        return "provider_unavailable"
    return "downstream_error"


def failure_meta(code: str) -> dict:
    return {
        "version": 2,
        "code": code,
        "message": FAILURE_MESSAGES.get(code, FAILURE_MESSAGES["downstream_error"]),
        "retry_without_change": False,
    }


class FrameworkMetadataOnly(logging.Filter):
    """Discard payload-bearing framework diagnostics before any log handler."""
    def filter(self, record):
        record.msg = "BORG framework event; payload details withheld"
        record.args = ()
        record.exc_info = record.exc_text = record.stack_info = None
        return True


def protect_framework_logs():
    for name in ("fastmcp.server.server", "fastmcp.server.providers.aggregate"):
        logger = logging.getLogger(name)
        if not any(isinstance(f, FrameworkMetadataOnly) for f in logger.filters):
            logger.addFilter(FrameworkMetadataOnly())


def describe_wire_tool(tool):
    """Apply the same public contract to tools/list and capability fingerprints."""
    if tool.name.startswith("desktop_"):
        from desktop_tools import describe
        describe(tool)
    name = tool.name.removeprefix("computer_")
    if tool.name.startswith("computer_") and name in DESCRIPTIONS:
        tool.description = DESCRIPTIONS[name]
        tool.annotations = ToolAnnotations(
            readOnlyHint=name in READS, destructiveHint=name in DESTRUCTIVE,
            idempotentHint=name in {"read_file", "read_multiple_files", "list_directory", "get_file_info"},
            openWorldHint=name in {"read_file", "start_process", "interact_with_process"})
        tool.meta = None


class BoundaryMiddleware(Middleware):
    def __init__(self, authorize, settings, ledger=None):
        self.authorize = authorize
        self.settings = settings
        self.ledger = ledger
        from concurrency import CallScheduler
        self.scheduler = CallScheduler(settings.computer.get("concurrency"))

    @staticmethod
    def _lane_name(tool_name: str) -> str | None:
        for prefix in ("desktop_", "computer_", "job_", "browser_", "remote_", "ui_", "credential_", "fleet_"):
            if tool_name.startswith(prefix):
                return prefix[:-1]
        return None

    @staticmethod
    def _receipt_worthy(tool_name: str) -> bool:
        if tool_name.startswith("computer_"):
            return tool_name.removeprefix("computer_") in DESTRUCTIVE
        if tool_name.startswith("desktop_"):
            from desktop_tools import READS as DESKTOP_READS
            return tool_name.removeprefix("desktop_") not in DESKTOP_READS
        if tool_name in {"fleet_call", "job_start", "job_cancel", "artifact_put", "remote_start", "remote_cancel",
                         "browser_start_session", "browser_navigate", "browser_click", "browser_type",
                         "browser_close_session", "ui_launch", "ui_quit", "ui_capture", "ui_click", "ui_type"}:
            return True
        return False

    @staticmethod
    def _fingerprint(tool_name: str, arguments) -> str:
        payload = json.dumps({"tool": tool_name, "arguments": arguments or {}},
                             sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(payload.encode()).hexdigest()

    def _check(self, value) -> None:
        from borg_context_server import secret, private_text
        known = []
        for path, authorization in ((self.settings.inbound_authorization_file, True),
                                    (self.settings.mem0_token_file, False)):
            try:
                known.append(secret(path, authorization=authorization))
            except (OSError, ValueError, UnicodeError):
                pass
        inbox_path = self.settings.computer.get("inbox_client_config")
        if inbox_path:
            try:
                inbox_config = json.loads(private_text(Path(inbox_path)))
                credential = json.loads(private_text(Path(inbox_config["credential_file"])))
                token = credential.get("credential")
                if isinstance(token, str) and len(token) >= 32:
                    known.append(token)
            except (OSError, ValueError, KeyError, TypeError, UnicodeError):
                pass

        def scan(item):
            if isinstance(item, str):
                if any(token in item for token in known) or contains_secret(item):
                    raise ToolError("Credential material was withheld. Verify any preceding change before retrying.")
            elif isinstance(item, dict):
                for key, nested in item.items():
                    scan(key)
                    scan(nested)
            elif isinstance(item, (list, tuple)):
                for nested in item:
                    scan(nested)
        scan(value)

    async def on_list_tools(self, context, call_next):
        self.authorize()
        result = await call_next(context)
        for tool in result:
            describe_wire_tool(tool)
        return result

    async def on_call_tool(self, context, call_next):
        self.authorize()
        # Verify at the actual target, including after a resident service restart.
        # This is an identity guard, not an exactly-once or replay guarantee.
        request_context = getattr(getattr(context, "fastmcp_context", None), "request_context", None)
        metadata = getattr(request_context, "meta", None) or getattr(context.message, "meta", None)
        metadata = metadata.model_dump() if hasattr(metadata, "model_dump") else metadata
        expected = metadata.get("borg_target_identity") if isinstance(metadata, dict) else None
        if expected is not None:
            actual = {**getattr(self.settings, "identity", {}),
                      "server_generation": self.ledger.generation if self.ledger else None}
            if not isinstance(expected, dict) or expected != actual or not actual.get("instance_id"):
                raise ToolError("BORG_POLICY_REFUSED: target identity changed; operation was not started")
        name = context.message.name
        arguments = context.message.arguments or {}
        started = time.monotonic()
        outcome = "error:downstream_error"
        receipt = None
        final_receipt_state = None
        receipt_failure = None
        try:
            await asyncio.to_thread(self._check, arguments)
            if self.ledger is not None and self._receipt_worthy(name):
                receipt = await asyncio.to_thread(self.ledger.start, name, self._fingerprint(name, arguments))
            lane_name = self._lane_name(name)
            if lane_name:
                result = await self.scheduler.run(name, arguments, lambda: call_next(context))
            else:
                result = await call_next(context)
            dumped = result.model_dump()
            await asyncio.to_thread(self._check, dumped)
            if result.is_error:
                code = classify_failure(dumped)
                result.meta = {**(result.meta or {}), "borg_failure": failure_meta(code)}
                final_receipt_state = "failed" if code in {
                    "authentication_required", "busy", "permission_denied", "policy_refused",
                    "admission_required", "capability_unavailable", "rate_limited"
                } else "outcome_unknown"
                if (result.meta or {}).get("borg_fleet", {}).get("state") == "not_started":
                    final_receipt_state = "failed"
                receipt_failure = code
                outcome = f"error:{code}"
            else:
                final_receipt_state = "succeeded"
                outcome = "ok"
            if receipt is not None:
                await asyncio.to_thread(self.ledger.finish, receipt["receipt_id"], final_receipt_state, receipt_failure)
                result.meta = {**(result.meta or {}), "borg_operation_receipt": receipt["receipt_id"]}
            return result
        except asyncio.CancelledError:
            outcome = "error:request_cancelled"
            if receipt is not None:
                await asyncio.to_thread(self.ledger.finish, receipt["receipt_id"], "outcome_unknown", "request_cancelled")
            raise
        except ToolError as exc:
            code = classify_failure(str(exc))
            outcome = f"error:{code}"
            if receipt is not None:
                state = "failed" if code in {
                    "authentication_required", "busy", "permission_denied", "policy_refused",
                    "admission_required", "capability_unavailable", "rate_limited"
                } else "outcome_unknown"
                await asyncio.to_thread(self.ledger.finish, receipt["receipt_id"], state, code)
            if code == "credential_withheld":
                raise
            suffix = f" receipt={receipt['receipt_id']}" if receipt is not None else ""
            raise ToolError(f"BORG_{code.upper()}: {FAILURE_MESSAGES[code]}{suffix}") from None
        except Exception as exc:
            code = classify_failure(str(exc))
            outcome = f"error:{code}"
            if receipt is not None:
                await asyncio.to_thread(self.ledger.finish, receipt["receipt_id"], "outcome_unknown", code)
            suffix = f" receipt={receipt['receipt_id']}" if receipt is not None else ""
            raise ToolError(f"BORG_{code.upper()}: {FAILURE_MESSAGES[code]}{suffix}") from None
        finally:
            LOG.info("tool=%s outcome=%s duration_ms=%d", re.sub(r"[^A-Za-z0-9_]", "_", name)[:80], outcome,
                     round((time.monotonic() - started) * 1000))


MAX_FILE_BYTES = 2_000_000
MAX_OUTPUT_BYTES = 256_000
MAX_SEARCH_RESULTS = 500
MAX_SCAN_ENTRIES = 10_000
SCAN_SECONDS = 5.0


def _walk(root: Path, depth: int | None = None):
    """Traverse only requested levels, without following directory symlinks."""
    stack = [(root, 1)]
    while stack:
        directory, level = stack.pop()
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    yield entry
                    if (depth is None or level < depth) and entry.is_dir(follow_symlinks=False):
                        stack.append((Path(entry.path), level + 1))
        except OSError:
            if directory == root:
                raise


def _path(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute() or any(part == ".." for part in path.parts):
        raise ToolError("BORG paths must be absolute and stay within the supplied path")
    return path


def _jsonable(value):
    return json.loads(json.dumps(value, default=str))


class NativeComputer:
    """BORG-owned local file, search and process tools.

    This implementation deliberately uses only Python and the host operating
    system. It has no Desktop Commander, Peekaboo or other MCP backend.
    """

    def __init__(self):
        self._searches: dict[str, dict] = {}
        self._processes: dict[int, subprocess.Popen] = {}
        self._process_output: dict[int, dict] = {}
        self._registry_lock = threading.Lock()

    def read_file(self, path: str, offset: int = 0, length: int = MAX_FILE_BYTES,
                  origin: Literal["llm", "ui"] = "llm", isUrl: bool = False,
                  options: dict | None = None, range: str | None = None,
                  sheet: str | None = None) -> dict:
        # Origin is legacy caller metadata, never an authorization grant.
        if isUrl or options or range or sheet:
            raise ToolError("BORG capability unavailable: native read_file supports local byte ranges only")
        target = _path(path)
        offset = max(0, int(offset))
        length = max(0, min(int(length), MAX_FILE_BYTES))
        with target.open("rb") as handle:
            handle.seek(offset)
            data = handle.read(length + 1)
        truncated = len(data) > length
        data = data[:length]
        try:
            content = data.decode("utf-8")
            encoding = "utf-8"
        except UnicodeDecodeError:
            content = base64.b64encode(data).decode("ascii")
            encoding = "base64"
        return {"path": str(target), "offset": offset, "content": content,
                "encoding": encoding, "truncated": truncated, "bytes": len(data)}

    def read_multiple_files(self, paths: list[str]) -> dict:
        return {"files": [self.read_file(path) for path in list(paths)[:50]]}

    def write_file(self, path: str, content: str, mode: str = "rewrite",
                   origin: Literal["llm", "ui"] = "llm") -> dict:
        target = _path(path)
        if len(content.encode()) > MAX_FILE_BYTES:
            raise ToolError("BORG file content exceeds the bounded write size")
        target.parent.mkdir(parents=True, exist_ok=True)
        if mode not in {"rewrite", "append"}:
            raise ToolError("BORG write mode must be rewrite or append")
        if mode == "append":
            with target.open("a", encoding="utf-8") as handle:
                handle.write(content)
        else:
            fd, temp_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=str(target.parent))
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(content)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temp_name, target)
            finally:
                if os.path.exists(temp_name):
                    os.unlink(temp_name)
        return {"path": str(target), "bytes": target.stat().st_size, "mode": mode}

    def write_pdf(self, path: str, content: str) -> dict:
        # Minimal self-contained PDF writer for text artifacts; no third-party
        # renderer or remote document service is involved.
        text = content.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        stream = f"BT /F1 12 Tf 72 720 Td ({text[:4000]}) Tj ET".encode()
        objects = [b"1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n",
                   b"2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj\n",
                   b"3 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >> endobj\n",
                   b"4 0 obj << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> endobj\n",
                   b"5 0 obj << /Length " + str(len(stream)).encode() + b" >> stream\n" + stream + b"\nendstream endobj\n"]
        body = bytearray(b"%PDF-1.4\n")
        offsets = []
        for obj in objects:
            offsets.append(len(body)); body.extend(obj)
        xref = len(body)
        body.extend(b"xref\n0 6\n0000000000 65535 f \n")
        body.extend(b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets))
        body.extend(b"trailer << /Size 6 /Root 1 0 R >>\nstartxref\n" + str(xref).encode() + b"\n%%EOF\n")
        target = _path(path); target.parent.mkdir(parents=True, exist_ok=True); target.write_bytes(body)
        return {"path": str(target), "bytes": len(body), "format": "pdf"}

    def create_directory(self, path: str) -> dict:
        target = _path(path); target.mkdir(parents=True, exist_ok=True)
        return {"path": str(target), "created": True}

    def list_directory(self, path: str, depth: int = 1,
                       origin: Literal["llm", "ui"] = "llm") -> dict:
        root = _path(path); depth = max(1, min(int(depth), 5)); rows = []
        deadline = time.monotonic() + SCAN_SECONDS
        truncated = False
        for entry in _walk(root, depth):
            if len(rows) >= 1000 or time.monotonic() >= deadline:
                truncated = True
                break
            rows.append({"path": entry.path, "name": entry.name,
                         "type": "directory" if entry.is_dir(follow_symlinks=False) else "file"})
        return {"path": str(root), "entries": sorted(rows, key=lambda row: row["path"]),
                "truncated": truncated}

    def move_file(self, source: str, destination: str) -> dict:
        src, dst = _path(source), _path(destination); dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst)); return {"source": str(src), "destination": str(dst)}

    def get_file_info(self, path: str) -> dict:
        target = _path(path); info = target.stat()
        return {"path": str(target), "type": "directory" if target.is_dir() else "file",
                "bytes": info.st_size, "mode": oct(info.st_mode & 0o777),
                "modified": info.st_mtime}

    def edit_block(self, file_path: str, old_string: str | None = None,
                   new_string: str | None = None, expected_replacements: int = 1,
                   origin: Literal["llm", "ui"] = "llm", content: object = None,
                   options: dict | None = None, range: str | None = None) -> dict:
        if content is not None or options or range:
            raise ToolError("BORG capability unavailable: edit_block supports exact text replacements only")
        if not old_string or new_string is None or expected_replacements < 1:
            raise ToolError("BORG edit requires old_string, new_string and a positive expected_replacements")
        target = _path(file_path); text = target.read_text(encoding="utf-8")
        count = text.count(old_string)
        if count != expected_replacements:
            raise ToolError("BORG edit requires exactly the expected number of matching text blocks")
        return self.write_file(str(target), text.replace(old_string, new_string), "rewrite") | {"replacements": count}

    def start_search(self, path: str, pattern: str = "*", search_term: str = "",
                     max_results: int = 100) -> dict:
        root = _path(path); max_results = max(1, min(int(max_results), MAX_SEARCH_RESULTS)); rows = []
        deadline = time.monotonic() + SCAN_SECONDS
        scanned = 0
        truncated = False
        content_truncated = 0
        for entry in _walk(root):
            if len(rows) >= MAX_SEARCH_RESULTS or scanned >= MAX_SCAN_ENTRIES or time.monotonic() >= deadline:
                truncated = True
                break
            scanned += 1
            candidate = Path(entry.path)
            if not fnmatch.fnmatch(candidate.name, pattern): continue
            if search_term and candidate.is_file():
                try:
                    with candidate.open("rb") as handle:
                        data = handle.read(MAX_FILE_BYTES + 1)
                    content_truncated += int(len(data) > MAX_FILE_BYTES)
                    if search_term not in data[:MAX_FILE_BYTES].decode("utf-8", "ignore"):
                        continue
                except OSError: continue
            rows.append({"path": str(candidate), "type": "directory" if candidate.is_dir() else "file"})
        search_id = str(uuid.uuid4())
        with self._registry_lock:
            self._searches[search_id] = {"rows": rows, "offset": max_results}
        return {"search_id": search_id, "results": rows[:max_results], "has_more": len(rows) > max_results,
                "scan_truncated": truncated, "scanned_entries": scanned,
                "content_truncated_files": content_truncated, "content_bytes_per_file": MAX_FILE_BYTES}

    def get_more_search_results(self, search_id: str, limit: int = 100) -> dict:
        state = self._searches.get(str(search_id));
        if state is None: raise ToolError("BORG search session not found")
        limit = max(1, min(int(limit), MAX_SEARCH_RESULTS)); start = state["offset"]; rows = state["rows"][start:start + limit]; state["offset"] += len(rows)
        return {"search_id": str(search_id), "results": rows, "has_more": state["offset"] < len(state["rows"])}

    def stop_search(self, search_id: str) -> dict:
        with self._registry_lock:
            existed = self._searches.pop(str(search_id), None) is not None
        return {"search_id": str(search_id), "stopped": existed}

    def list_searches(self) -> dict:
        with self._registry_lock:
            return {"searches": [{"search_id": key, "remaining": len(value["rows"]) - value["offset"]}
                                 for key, value in self._searches.items()]}

    def start_process(self, command: str, cwd: str | None = None, timeout_ms: int = 1000,
                      shell: str | None = None, origin: Literal["llm", "ui"] = "llm",
                      verbose_timing: bool = False) -> dict:
        started = time.monotonic()
        if not command or len(command) > 4000: raise ToolError("BORG command is empty or too long")
        executable = shell or ("/bin/zsh" if Path("/bin/zsh").is_file() else "/bin/sh")
        executable = shutil.which(executable)
        if executable is None or Path(executable).name not in {"sh", "bash", "zsh", "dash", "ksh"}:
            raise ToolError("BORG capability unavailable: requested shell must be an installed POSIX shell")
        working = str(_path(cwd)) if cwd else str(Path.home())
        proc = subprocess.Popen([executable, "-lc", command], cwd=working,
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                bufsize=0)
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            os.set_blocking(stream.fileno(), False)
        with self._registry_lock:
            self._processes[proc.pid] = proc
            self._process_output[proc.pid] = {"data": bytearray(), "base": 0, "cursor": 0}
        time.sleep(min(max(int(timeout_ms), 0), 250) / 1000)
        result = {"pid": proc.pid, "PID": proc.pid, "process ID": proc.pid,
                "message": f"process ID: {proc.pid}", "running": proc.poll() is None,
                "returncode": proc.poll()}
        if verbose_timing:
            result["elapsed_ms"] = round((time.monotonic() - started) * 1000)
        return result

    def read_process_output(self, pid: int, timeout_ms: int = 1000,
                            length: int = MAX_OUTPUT_BYTES, offset: int | None = None,
                            verbose_timing: bool = False) -> dict:
        started = time.monotonic()
        proc = self._processes.get(int(pid));
        if proc is None: raise ToolError("BORG process session not found")
        state = self._process_output[int(pid)]
        start = state["cursor"] if offset is None else int(offset)
        if length < 0 or start < 0:
            raise ToolError("BORG process output length and offset must be nonnegative bytes")
        if start < state["base"] or start > state["base"] + len(state["data"]):
            raise ToolError("BORG process output offset is outside retained output; use the returned next_offset")
        limit = min(int(length), MAX_OUTPUT_BYTES)
        needed = max(0, start + limit - state["base"] - len(state["data"]))
        deadline = time.monotonic() + min(max(int(timeout_ms), 0), 5000) / 1000
        streams = [stream for stream in (proc.stdout, proc.stderr) if not stream.closed]
        size = 0
        while streams and size < needed:
            ready, _, _ = select.select(streams, [], [], max(0, deadline - time.monotonic()))
            if not ready: break
            for stream in ready:
                try:
                    chunk = os.read(stream.fileno(), needed - size)
                except BlockingIOError:
                    continue
                if chunk:
                    state["data"].extend(chunk)
                    size += len(chunk)
                else:
                    streams.remove(stream)
                    stream.close()
                if size == needed: break
            if time.monotonic() >= deadline: break
        excess = max(0, len(state["data"]) - MAX_FILE_BYTES)
        if excess:
            del state["data"][:excess]
            state["base"] += excess
        data = bytes(state["data"][start - state["base"]:start - state["base"] + limit])
        next_offset = start + len(data)
        state["cursor"] = max(state["cursor"], next_offset)
        returncode = proc.poll()
        if returncode is not None and proc.stdin is not None and not proc.stdin.closed:
            proc.stdin.close()
        result = {"pid": proc.pid, "output": data.decode("utf-8", "replace"),
                  "running": returncode is None, "returncode": returncode,
                  "offset": start, "next_offset": next_offset, "retained_from": state["base"],
                  "bytes": len(data), "truncated": bool(limit and len(data) == limit)}
        if verbose_timing:
            result["elapsed_ms"] = round((time.monotonic() - started) * 1000)
        return result

    def interact_with_process(self, pid: int, input: str = "") -> dict:
        proc = self._processes.get(int(pid));
        if proc is None or proc.stdin is None: raise ToolError("BORG process session not found")
        data = input.encode()
        if len(data) > MAX_OUTPUT_BYTES:
            raise ToolError("BORG process input exceeds the bounded write size")
        sent = 0
        deadline = time.monotonic() + 1.0
        while sent < len(data):
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([], [proc.stdin], [], max(0, remaining))[1]:
                raise ToolError(f"BORG process input timed out after {sent} bytes; inspect the process before retrying")
            try:
                sent += os.write(proc.stdin.fileno(), data[sent:])
            except BlockingIOError:
                continue
        return {"pid": proc.pid, "sent": sent}

    def force_terminate(self, pid: int) -> dict:
        proc = self._processes.get(int(pid));
        if proc is None: return {"pid": int(pid), "terminated": False}
        if proc.poll() is None: proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill(); proc.wait(timeout=2)
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            if stream is not None:
                stream.close()
        return {"pid": proc.pid, "terminated": True}

    def list_sessions(self) -> dict:
        with self._registry_lock:
            processes = list(self._processes.items())
        return {"sessions": [{"pid": pid, "running": proc.poll() is None, "returncode": proc.poll()}
                             for pid, proc in processes]}


def mount_computer(server, config):
    native = NativeComputer()
    for name in DESCRIPTIONS:
        function = getattr(native, name)
        server.tool(name=f"computer_{name}")(function)
