"""Durable native BORG jobs and bounded artifacts.

Jobs are detached from the MCP adapter so their output survives a client
disconnect. A process that outlives the adapter is reported as ``unknown``
when its terminal status cannot be proven after restart.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import signal
import subprocess
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from functools import wraps
from pathlib import Path

from fastmcp.exceptions import ToolError

from computer_tools import MAX_OUTPUT_BYTES, _path, contains_secret

MAX_COMMAND_BYTES = 16_000
MAX_ARTIFACT_BYTES = 25_000_000
MAX_JOBS = 200
TOOL_NAMES = ["job_start", "job_status", "job_read_output", "job_cancel", "job_list",
              "artifact_put", "artifact_read", "artifact_list"]


def _job_command(command: str) -> list[str]:
    """Preserve the macOS login shell; support POSIX hosts without zsh."""
    for shell, option in (("/bin/zsh", "-lc"), ("/bin/sh", "-c")):
        if os.path.isfile(shell) and os.access(shell, os.X_OK):
            return [shell, option, command]
    raise ToolError("BORG capability unavailable: no supported executable job shell")


def _now() -> float:
    return time.time()


def _id(value: str) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except (TypeError, ValueError, AttributeError):
        raise ToolError("BORG job or artifact ID is invalid") from None


def _scrub(text: str) -> str:
    """Reduce accidental secret disclosure in persisted command output."""
    if not text:
        return ""
    if contains_secret(text):
        return "[BORG output withheld: credential-like material detected]"
    return text[:MAX_OUTPUT_BYTES]


def _serialized_job(method):
    @wraps(method)
    def call(self, job_id, *args, **kwargs):
        with self._job_lock(_id(job_id)):
            return method(self, job_id, *args, **kwargs)
    return call


class JobStore:
    def __init__(self, root: Path):
        self.root = root
        self.jobs = root / "jobs"
        self.artifacts = root / "artifacts"
        for directory in (self.root, self.jobs, self.artifacts):
            if directory.is_symlink():
                raise ValueError("BORG durable job root cannot be a symlink")
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(directory, 0o700)
        self.processes: dict[str, subprocess.Popen] = {}
        self._locks_guard = threading.Lock()
        self._locks = {}

    @contextmanager
    def _job_lock(self, key):
        with self._locks_guard:
            lock, users = self._locks.get(key, (threading.RLock(), 0))
            self._locks[key] = (lock, users + 1)
        try:
            with lock:
                yield
        finally:
            with self._locks_guard:
                lock, users = self._locks[key]
                if users == 1:
                    del self._locks[key]
                else:
                    self._locks[key] = (lock, users - 1)

    def close_all(self) -> None:
        """Release resident child handles without changing durable job truth."""
        for proc in list(self.processes.values()):
            if proc.poll() is None:
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                except (ProcessLookupError, OSError):
                    pass
            try:
                proc.wait(timeout=2)
            except (subprocess.TimeoutExpired, OSError):
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except (ProcessLookupError, OSError):
                    pass
                try:
                    proc.wait(timeout=2)
                except (subprocess.TimeoutExpired, OSError):
                    pass
        self.processes.clear()

    def _job_path(self, job_id: str) -> Path:
        return self.jobs / (_id(job_id) + ".json")

    def _artifact_path(self, artifact_id: str) -> Path:
        return self.artifacts / (_id(artifact_id) + ".bin")

    def _write(self, path: Path, row: dict) -> None:
        fd, temp_name = tempfile.mkstemp(prefix=".borg-", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(row, handle, sort_keys=True, separators=(",", ":"))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temp_name, 0o600)
            os.replace(temp_name, path)
        finally:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass

    def _read(self, path: Path) -> dict:
        try:
            if path.is_symlink() or path.stat().st_size > 32_000:
                raise ValueError("unsafe job metadata")
            row = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            raise ToolError("BORG durable job metadata is unavailable") from None
        if not isinstance(row, dict):
            raise ToolError("BORG durable job metadata is invalid")
        return row

    @staticmethod
    def _identity(pid: int) -> str | None:
        try:
            result = subprocess.run(["/bin/ps", "-p", str(pid), "-o", "lstart="],
                                    capture_output=True, text=True, timeout=2, check=False)
        except (OSError, subprocess.TimeoutExpired):
            return None
        value = result.stdout.strip()
        return value or None

    def _alive(self, row: dict) -> bool:
        pid = row.get("pid")
        if not isinstance(pid, int) or pid <= 0:
            return False
        identity = self._identity(pid)
        return bool(identity and identity == row.get("pid_start"))

    def start(self, command: str, cwd: str | None = None, timeout_ms: int = 1000) -> dict:
        if not isinstance(command, str) or not command.strip() or len(command.encode()) > MAX_COMMAND_BYTES:
            raise ToolError("BORG job command is empty or too long")
        working = _path(cwd) if cwd else Path.home()
        if not working.is_dir():
            raise ToolError("BORG job working directory is unavailable")
        shell_command = _job_command(command)
        job_id = str(uuid.uuid4())
        job_dir = self.jobs / job_id
        job_dir.mkdir(mode=0o700)
        stdout_path, stderr_path = job_dir / "stdout.log", job_dir / "stderr.log"
        for path in (stdout_path, stderr_path):
            path.touch(mode=0o600)
        stdout = stdout_path.open("ab", buffering=0)
        stderr = stderr_path.open("ab", buffering=0)
        try:
            proc = subprocess.Popen(shell_command, cwd=str(working),
                                    stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
                                    start_new_session=True)
        except Exception:
            stdout.close(); stderr.close()
            raise
        stdout.close(); stderr.close()
        start_delay = min(max(int(timeout_ms), 0), 5000) / 1000
        if start_delay:
            time.sleep(start_delay)
        row = {
            "version": 1, "job_id": job_id, "state": "running", "pid": proc.pid,
            "pid_start": self._identity(proc.pid), "cwd": str(working),
            "command_sha256": hashlib.sha256(command.encode()).hexdigest(),
            "stdout": str(stdout_path), "stderr": str(stderr_path),
            "started_at": _now(), "finished_at": None, "returncode": None,
        }
        self._write(self._job_path(job_id), row)
        self.processes[job_id] = proc
        return {k: row[k] for k in ("job_id", "state", "pid", "started_at", "command_sha256")}

    def _observed_status(self, row: dict) -> dict:
        row = dict(row)
        key = row["job_id"]
        proc = self.processes.get(key)
        if proc is not None and proc.poll() is not None:
            row["state"] = "succeeded" if proc.returncode == 0 else "failed"
            row["returncode"] = proc.returncode
            row["finished_at"] = row.get("finished_at") or _now()
        elif row.get("state") == "running" and not self._alive(row):
            # After an adapter restart the process may have finished, but its
            # exit status is not provable without a resident waiter.
            row["state"] = "outcome_unknown"
            row["finished_at"] = row.get("finished_at") or _now()
            row["returncode"] = None
        return row

    @_serialized_job
    def status(self, job_id: str) -> dict:
        key = _id(job_id)
        original = self._read(self._job_path(key))
        row = self._observed_status(original)
        if row != original:
            self._write(self._job_path(key), row)
        return {k: row.get(k) for k in ("job_id", "state", "pid", "started_at", "finished_at", "returncode", "command_sha256")}

    def read_output(self, job_id: str, stream: str = "combined", offset: int = 0, length: int = MAX_OUTPUT_BYTES) -> dict:
        row = self._read(self._job_path(job_id))
        if stream not in {"stdout", "stderr", "combined"}:
            raise ToolError("BORG job stream must be stdout, stderr or combined")
        offset = max(0, int(offset)); length = max(0, min(int(length), MAX_OUTPUT_BYTES))
        paths = [Path(row[stream])] if stream != "combined" else [Path(row["stdout"]), Path(row["stderr"])]
        chunks = []
        for path in paths:
            try:
                if path.is_symlink():
                    raise ValueError("symlink")
                chunks.append(path.read_bytes())
            except (OSError, ValueError):
                raise ToolError("BORG job output is unavailable") from None
        data = b"\n".join(chunks)[offset:offset + length]
        text = data.decode("utf-8", "replace")
        return {"job_id": _id(job_id), "stream": stream, "offset": offset,
                "output": _scrub(text), "bytes": len(data), "truncated": len(data) >= length}

    @_serialized_job
    def cancel(self, job_id: str) -> dict:
        key = _id(job_id); row = self._read(self._job_path(key))
        if row.get("state") != "running":
            return self.status(key)
        pid = row.get("pid")
        if not isinstance(pid, int) or self._identity(pid) != row.get("pid_start"):
            raise ToolError("BORG job process identity is no longer provable; reconcile before retrying")
        try:
            os.killpg(pid, signal.SIGTERM)
        except ProcessLookupError:
            row["state"] = "outcome_unknown"
        else:
            row["state"] = "cancelled"
        row["finished_at"] = _now()
        self._write(self._job_path(key), row)
        return self.status(key)

    def list(self, limit: int = 20) -> dict:
        limit = max(1, min(int(limit), 50)); rows = []
        try:
            paths = sorted(self.jobs.glob("*.json"), key=lambda path: path.stat().st_mtime, reverse=True)
        except OSError:
            paths = []
        for path in paths[:limit]:
            try:
                # Discovery reads atomic snapshots and never joins a native job
                # writer's lock while occupying a shared execution slot.
                row = self._observed_status(self._read(path))
                rows.append({k: row.get(k) for k in ("job_id", "state", "pid", "started_at", "finished_at", "returncode", "command_sha256")})
            except ToolError:
                continue
        return {"jobs": rows}

    def put_artifact(self, path: str) -> dict:
        source = _path(path)
        if not source.is_file() or source.is_symlink():
            raise ToolError("BORG artifact source is unavailable")
        size = source.stat().st_size
        if size > MAX_ARTIFACT_BYTES:
            raise ToolError("BORG artifact exceeds the bounded size")
        artifact_id = str(uuid.uuid4()); target = self._artifact_path(artifact_id)
        digest = hashlib.sha256(); total = 0
        fd, temp_name = tempfile.mkstemp(prefix=".borg-artifact-", dir=str(self.artifacts))
        try:
            with os.fdopen(fd, "wb") as handle, source.open("rb") as source_handle:
                while True:
                    chunk = source_handle.read(1024 * 1024)
                    if not chunk: break
                    digest.update(chunk); total += len(chunk); handle.write(chunk)
                handle.flush(); os.fsync(handle.fileno())
            os.chmod(temp_name, 0o600); os.replace(temp_name, target)
        finally:
            try: os.unlink(temp_name)
            except FileNotFoundError: pass
        metadata = {"version": 1, "artifact_id": artifact_id, "bytes": total,
                    "sha256": digest.hexdigest(), "name": source.name, "created_at": _now()}
        self._write(self.artifacts / (artifact_id + ".json"), metadata)
        return metadata

    def read_artifact(self, artifact_id: str, offset: int = 0, length: int = MAX_OUTPUT_BYTES) -> dict:
        key = _id(artifact_id); metadata = self._read(self.artifacts / (key + ".json"))
        offset = max(0, int(offset)); length = max(0, min(int(length), MAX_OUTPUT_BYTES))
        try:
            with self._artifact_path(key).open("rb") as handle:
                handle.seek(offset); data = handle.read(length)
        except OSError:
            raise ToolError("BORG artifact is unavailable") from None
        return {**metadata, "offset": offset, "content_base64": base64.b64encode(data).decode(),
                "bytes_returned": len(data), "has_more": offset + len(data) < metadata["bytes"]}

    def list_artifacts(self, limit: int = 20) -> dict:
        limit = max(1, min(int(limit), 50)); rows = []
        try:
            paths = sorted(self.artifacts.glob("*.json"), key=lambda path: path.stat().st_mtime, reverse=True)
        except OSError:
            paths = []
        for path in paths[:limit]:
            try: rows.append(self._read(path))
            except ToolError: continue
        return {"artifacts": rows}


def mount_jobs(server, config, handoff=None):
    from runtime_paths import borg_home
    root = Path(config.get("jobs_root") or (borg_home() / "borg-context"))
    store = JobStore(root)
    annotations = {
        "job_start": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True},
        "job_status": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
        "job_read_output": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
        "job_cancel": {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True, "openWorldHint": False},
        "job_list": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
        "artifact_put": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False},
        "artifact_read": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
        "artifact_list": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    }
    descriptions = {
        "job_start": "Start a durable native BORG command job. Returns a stable job ID; command text is never returned.",
        "job_status": "Read durable BORG job state and bounded metadata by job ID.",
        "job_read_output": "Read bounded, credential-scrubbed output from a durable BORG job.",
        "job_cancel": "Cancel one owned durable BORG job after process identity verification.",
        "job_list": "List recent durable BORG jobs for reconciliation.",
        "artifact_put": "Copy one local artifact into the BORG bounded artifact store and return its checksum.",
        "artifact_read": "Read a bounded base64 chunk from one BORG artifact by ID.",
        "artifact_list": "List recent BORG artifacts and checksums.",
    }
    method_names = {
        "job_start": "start", "job_status": "status", "job_read_output": "read_output",
        "job_cancel": "cancel", "job_list": "list", "artifact_put": "put_artifact",
        "artifact_read": "read_artifact", "artifact_list": "list_artifacts",
    }
    for name in TOOL_NAMES:
        function = getattr(store, method_names[name])
        if handoff:
            function = handoff.store_function(store, name, function)
        server.tool(name=name, annotations=annotations[name], description=descriptions[name])(function)
    return store
