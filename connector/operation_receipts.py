"""Durable, payload-free operation receipts for BORG connector actions.

Receipts record lifecycle metadata only. They do not make arbitrary shell or UI
side effects exactly-once; an interrupted in-flight action is recovered as
``outcome_unknown`` and must be reconciled at its native target before retry.
"""
from __future__ import annotations

import json
import os
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastmcp.exceptions import ToolError
from operation_diagnostics import sanitize

FINAL = frozenset({"succeeded", "failed", "outcome_unknown"})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _uuid(value: str) -> str:
    try:
        return str(uuid.UUID(value))
    except (ValueError, TypeError, AttributeError):
        raise ToolError("Supply one exact BORG operation receipt UUID") from None


class OperationLedger:
    def __init__(self, root: Path):
        self.root = root
        self.generation = str(uuid.uuid4())
        self.started_at = _now()
        self._locks_guard = threading.Lock()
        self._locks: dict[str, tuple[threading.Lock, int]] = {}
        if self.root.is_symlink():
            raise ValueError("BORG operation receipt root cannot be a symlink")
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)
        self.recover_incomplete()

    def _path(self, receipt_id: str) -> Path:
        return self.root / (_uuid(receipt_id) + ".json")

    @contextmanager
    def _finalization_lock(self, receipt_id: str):
        # A cancelled to_thread await does not stop its writer. Count waiters
        # before acquiring the lock so every finalizer uses the same lock until
        # the last one leaves, without retaining a lock for every past receipt.
        with self._locks_guard:
            lock, users = self._locks.get(receipt_id, (threading.Lock(), 0))
            self._locks[receipt_id] = (lock, users + 1)
        try:
            with lock:
                yield
        finally:
            with self._locks_guard:
                lock, users = self._locks[receipt_id]
                if users == 1:
                    del self._locks[receipt_id]
                else:
                    self._locks[receipt_id] = (lock, users - 1)

    def _write(self, row: dict[str, Any]) -> None:
        path = self._path(row["receipt_id"])
        tmp = self.root / ("." + row["receipt_id"] + ".tmp")
        payload = json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, "w") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
        finally:
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass

    def start(self, tool: str, request_sha256: str, diagnostics: dict | None = None) -> dict[str, Any]:
        receipt_id = str(uuid.uuid4())
        row = {
            "version": 2,
            "diagnostics": sanitize(diagnostics),
            "receipt_id": receipt_id,
            "tool": tool,
            "request_sha256": request_sha256,
            "state": "running",
            "started_at": _now(),
            "finished_at": None,
            "failure_code": None,
            "server_generation": self.generation,
            "server_started_at": self.started_at,
            "pid": os.getpid(),
        }
        self._write(row)
        return row

    def finish(self, receipt_id: str, state: str, failure_code: str | None = None,
               diagnostics: dict | None = None) -> dict[str, Any]:
        if state not in FINAL:
            raise ValueError("invalid final operation state")
        receipt_id = _uuid(receipt_id)
        with self._finalization_lock(receipt_id):
            row = self.get(receipt_id)
            if row["state"] in FINAL:
                return row
            row["state"] = state
            row["finished_at"] = _now()
            row["failure_code"] = failure_code
            detail = sanitize(diagnostics if diagnostics is not None else row.get("diagnostics"))
            detail["cause"] = failure_code
            if state == "outcome_unknown":
                detail["effect_state"] = "outcome_unknown"
                if diagnostics is None:
                    detail["phase"] = "unknown"
            elif state == "succeeded":
                detail["effect_state"] = "completed"
                detail["phase"] = "complete"
            row["diagnostics"] = sanitize(detail)
            self._write(row)
            return row

    def get(self, receipt_id: str) -> dict[str, Any]:
        path = self._path(receipt_id)
        try:
            if path.is_symlink() or path.stat().st_size > 16384:
                raise ValueError("unsafe receipt")
            row = json.loads(path.read_text())
        except FileNotFoundError:
            raise ToolError("BORG operation receipt was not found") from None
        except (OSError, ValueError, TypeError):
            raise ToolError("BORG operation receipt is unavailable") from None
        if not isinstance(row, dict) or row.get("receipt_id") != _uuid(receipt_id):
            raise ToolError("BORG operation receipt is invalid")
        return row

    def recent(self, limit: int = 10) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 50))
        rows = []
        try:
            paths = sorted(self.root.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        except OSError:
            return []
        for path in paths[:limit]:
            try:
                rows.append(self.get(path.stem))
            except ToolError:
                continue
        return rows

    @staticmethod
    def _pid_alive(pid: object) -> bool:
        if not isinstance(pid, int) or pid <= 0:
            return False
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True

    def recover_incomplete(self) -> int:
        recovered = 0
        try:
            paths = list(self.root.glob("*.json"))
        except OSError:
            return 0
        for path in paths:
            try:
                row = self.get(path.stem)
                if row.get("state") == "running" and not self._pid_alive(row.get("pid")):
                    self.finish(path.stem, "outcome_unknown", "process_interrupted")
                    recovered += 1
            except ToolError:
                continue
        return recovered

    def health(self) -> dict[str, Any]:
        try:
            info = self.root.stat()
            writable = bool(info.st_mode & 0o200)
            recent = self.recent(1)
            latest = None
            if recent:
                latest = recent[0].get("finished_at") or recent[0].get("started_at")
            return {
                "status": "PASS" if writable else "DEGRADED",
                "root": str(self.root),
                "server_generation": self.generation,
                "latest_receipt_at": latest,
            }
        except OSError:
            return {"status": "UNAVAILABLE", "server_generation": self.generation}
