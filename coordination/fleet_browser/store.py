"""Atomic ownership and action receipts; no browser data or credentials.

Authentication and operator authorization belong to the existing Inbox host.
Only the trusted gateway may invoke supervisor/reconciliation methods here.
Expiry and revocation reserve resources until cleanup has been established.
"""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import threading
import time
from typing import Any
import uuid


class ResourceError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


class SystemClock:
    def __init__(self):
        try:
            self.boot_id = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        except OSError:
            try:
                result = subprocess.run(
                    ["/usr/sbin/sysctl", "-n", "kern.boottime"],
                    capture_output=True, text=True, timeout=3, check=True,
                )
                self.boot_id = hashlib.sha256(result.stdout.encode()).hexdigest()
            except (OSError, subprocess.SubprocessError) as exc:
                raise ResourceError("clock_unavailable", "Stable host boot identity unavailable") from exc

    def sample(self):
        return time.time(), time.monotonic(), self.boot_id


_ID = re.compile(r"[A-Za-z0-9_.:@+-]{1,200}\Z")
_SEGMENT = re.compile(r"[A-Za-z0-9_.:@+-]{1,128}\Z")
_DIGEST = re.compile(r"[a-f0-9]{64}\Z")
_KEY_LENGTHS = {"browser": 3, "profile": 3, "desktop": 3, "account": 4, "object": 4}
_METADATA = {"host", "profile_id", "tenant_id", "account_id", "adapter", "controller_id", "request_fingerprint", "work_revision"}
_RECEIPT = {
    "code", "clean", "verified_at", "evidence_ref", "resource_count", "process_count",
    "worker_id", "supervisor_id", "lost_control", "mutation_resolved", "outcome",
}


def _identifier(value, name):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ResourceError("invalid_input", f"Invalid {name}")
    return value


def _ttl(value):
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        raise ResourceError("invalid_input", "Invalid lease duration")
    if not math.isfinite(value) or not 1 <= value <= 1800:
        raise ResourceError("invalid_input", "Lease duration must be 1 to 1800 seconds")
    return float(value)


def _resources(values):
    if not isinstance(values, (list, tuple)) or not 1 <= len(values) <= 32:
        raise ResourceError("invalid_input", "Expected 1 to 32 canonical resources")
    for value in values:
        parts = value.split("/") if isinstance(value, str) else []
        if not parts or len(parts) != _KEY_LENGTHS.get(parts[0]):
            raise ResourceError("invalid_input", "Invalid resource kind or shape")
        if any(not _SEGMENT.fullmatch(part) or part in {".", ".."} for part in parts):
            raise ResourceError("invalid_input", "Invalid resource identifier")
    return sorted(set(values))


def _bounded_metadata(value, allowed):
    if value is None:
        return {}
    if not isinstance(value, dict) or set(value) - allowed:
        raise ResourceError("invalid_input", "Unsupported metadata field")
    result = {}
    for key, item in value.items():
        if item is None or isinstance(item, bool):
            result[key] = item
        elif isinstance(item, (int, float)) and math.isfinite(item):
            result[key] = item
        elif isinstance(item, str) and len(item) <= 512 and "\x00" not in item:
            result[key] = item
        else:
            raise ResourceError("invalid_input", "Invalid receipt metadata")
    return result


def _encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


class ResourceStore:
    def __init__(self, path, clock=None):
        self.path = Path(path).absolute()
        parent = self.path.parent
        if parent.is_symlink() or self.path.is_symlink():
            raise ResourceError("unsafe_state_path", "State paths must not be symlinks")
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if parent.stat().st_uid != os.getuid() or parent.stat().st_mode & 0o077:
            raise ResourceError("unsafe_state_path", "State directory must be private and owned")
        if self.path.exists() and (not self.path.is_file() or self.path.stat().st_uid != os.getuid()):
            raise ResourceError("unsafe_state_path", "State file is not owned")
        self.clock = clock or SystemClock()
        self._lock = threading.RLock()
        with self._transaction(safety=False) as (db, sample):
            db.executescript("""
                CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS leases(
                    lease_id TEXT PRIMARY KEY, actor TEXT NOT NULL, work_id TEXT NOT NULL,
                    attempt_id TEXT NOT NULL, request_id TEXT NOT NULL, spec TEXT NOT NULL,
                    generation INTEGER NOT NULL UNIQUE, status TEXT NOT NULL,
                    created_at REAL NOT NULL, expires_at REAL NOT NULL, deadline_mono REAL NOT NULL,
                    boot_id TEXT NOT NULL, metadata TEXT NOT NULL, reason TEXT,
                    cleanup_verified INTEGER NOT NULL DEFAULT 0, cleanup_receipt TEXT,
                    UNIQUE(actor,request_id)
                );
                CREATE TABLE IF NOT EXISTS resources(
                    resource_key TEXT PRIMARY KEY, generation INTEGER NOT NULL,
                    lease_id TEXT REFERENCES leases(lease_id)
                );
                CREATE INDEX IF NOT EXISTS resource_lease ON resources(lease_id);
                CREATE TABLE IF NOT EXISTS lease_resources(
                    lease_id TEXT NOT NULL REFERENCES leases(lease_id), resource_key TEXT NOT NULL,
                    PRIMARY KEY(lease_id,resource_key)
                );
                CREATE TABLE IF NOT EXISTS actions(
                    lease_id TEXT NOT NULL REFERENCES leases(lease_id), request_id TEXT NOT NULL,
                    operation TEXT NOT NULL, payload_digest TEXT NOT NULL,
                    mutating INTEGER NOT NULL, status TEXT NOT NULL,
                    started_at REAL NOT NULL, finished_at REAL, receipt TEXT,
                    PRIMARY KEY(lease_id,request_id)
                );
                CREATE INDEX IF NOT EXISTS action_pending ON actions(lease_id,status);
                CREATE TABLE IF NOT EXISTS holds(
                    hold_key TEXT NOT NULL, lease_id TEXT NOT NULL,
                    reason TEXT NOT NULL, created_at REAL NOT NULL,
                    PRIMARY KEY(hold_key,lease_id)
                );
                CREATE TABLE IF NOT EXISTS controls(
                    actor TEXT NOT NULL, request_id TEXT NOT NULL, spec TEXT NOT NULL,
                    result TEXT NOT NULL, PRIMARY KEY(actor,request_id)
                );
                INSERT OR IGNORE INTO meta(key,value) VALUES('generation','0');
            """)

    @contextmanager
    def _transaction(self, safety=True):
        with self._lock:
            db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
            os.chmod(self.path, 0o600)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA busy_timeout=10000")
            try:
                db.execute("BEGIN IMMEDIATE")
                sample = self._expire(db) if safety else None
                if safety:
                    db.execute("SAVEPOINT request_writes")
                try:
                    yield db, sample
                except BaseException:
                    if safety:
                        # Keep the safety transition even when request validation
                        # rejects. Partial request writes still roll back.
                        db.execute("ROLLBACK TO request_writes")
                        db.commit()
                    raise
                db.commit()
            except BaseException:
                db.rollback()
                raise
            finally:
                db.close()

    @staticmethod
    def _meta(db, key, value):
        db.execute("INSERT INTO meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                   (key, str(value)))

    def _expire(self, db):
        wall, mono, boot = self.clock.sample()
        if not all(isinstance(t, (int, float)) and math.isfinite(t) for t in (wall, mono)):
            raise ResourceError("clock_unavailable", "Invalid clock sample")
        old = db.execute("SELECT value FROM meta WHERE key='clock'").fetchone()
        unsafe = False
        if old:
            previous = json.loads(old[0])
            unsafe = boot != previous[2] or wall < previous[0] - 1 or mono < previous[1] - 0.01
        if unsafe:
            db.execute("UPDATE leases SET status='revoking',reason='clock_changed' WHERE status='active'")
        db.execute("""UPDATE leases SET status='revoking',reason='lease_expired'
                      WHERE status='active' AND (expires_at<=? OR deadline_mono<=? OR boot_id<>?)""",
                   (wall, mono, boot))
        self._meta(db, "clock", _encoded([wall, mono, boot]))
        return wall, mono, boot, unsafe

    def _lease(self, db, lease_id, actor=None, generation=None, active=False):
        _identifier(lease_id, "lease ID")
        row = db.execute("SELECT * FROM leases WHERE lease_id=?", (lease_id,)).fetchone()
        if row is None or (actor is not None and row["actor"] != actor):
            raise ResourceError("lease_not_found", "Lease is not owned by this caller")
        if generation is not None and (type(generation) is not int or generation != row["generation"]):
            raise ResourceError("stale_generation", "Lease generation is no longer current")
        if active and row["status"] != "active":
            raise ResourceError("lease_inactive", "Lease is expired, revoked or quarantined")
        if active:
            expected = db.execute("SELECT COUNT(*) FROM lease_resources WHERE lease_id=?", (lease_id,)).fetchone()[0]
            actual = db.execute("SELECT COUNT(*) FROM resources WHERE lease_id=? AND generation=?",
                                (lease_id, row["generation"])).fetchone()[0]
            if expected != actual:
                raise ResourceError("ownership_mismatch", "Resource ownership requires reconciliation")
        return row

    @staticmethod
    def _view(db, row):
        fields = ("lease_id", "actor", "work_id", "attempt_id", "generation", "status",
                  "created_at", "expires_at", "reason", "cleanup_verified")
        result = {key: row[key] for key in fields}
        result["metadata"] = json.loads(row["metadata"])
        result["resources"] = [r[0] for r in db.execute(
            "SELECT resource_key FROM lease_resources WHERE lease_id=? ORDER BY resource_key", (row["lease_id"],))]
        result["pending_actions"] = db.execute(
            "SELECT COUNT(*) FROM actions WHERE lease_id=? AND status IN ('in_flight','unknown')", (row["lease_id"],)).fetchone()[0]
        if row["cleanup_receipt"]:
            result["cleanup_receipt"] = json.loads(row["cleanup_receipt"])
        return result

    @staticmethod
    def _action_view(row, dispatch=False):
        return {"request_id": row["request_id"], "operation": row["operation"], "status": row["status"],
                "dispatch": dispatch, "started_at": row["started_at"], "finished_at": row["finished_at"],
                "receipt": json.loads(row["receipt"]) if row["receipt"] else None}

    def acquire(self, actor, work_id, attempt_id, request_id, resources, ttl_seconds=300, metadata=None):
        for value, field in ((actor, "actor"), (work_id, "work ID"), (attempt_id, "attempt ID"), (request_id, "request ID")):
            _identifier(value, field)
        resources, ttl = _resources(resources), _ttl(ttl_seconds)
        metadata = _bounded_metadata(metadata, _METADATA)
        spec = _encoded([work_id, attempt_id, resources, ttl, metadata])
        with self._transaction() as (db, sample):
            wall, mono, boot, unsafe = sample
            prior = db.execute("SELECT * FROM leases WHERE actor=? AND request_id=?", (actor, request_id)).fetchone()
            if prior:
                if prior["spec"] != spec:
                    raise ResourceError("request_mismatch", "Request ID was already used for different resources")
                result = self._view(db, prior)
                result["replayed"] = True
                return result
            if unsafe:
                raise ResourceError("clock_changed", "Clock changed; reconcile existing resources first")
            for hold in [f"work:{work_id}", *resources]:
                if db.execute("SELECT 1 FROM holds WHERE hold_key=?", (hold,)).fetchone():
                    raise ResourceError("resource_stopped", "Operator stopped this work or resource; explicit resume required")
            for resource in resources:
                row = db.execute("SELECT lease_id FROM resources WHERE resource_key=?", (resource,)).fetchone()
                if row and row[0] is not None:
                    raise ResourceError("resource_busy", "A requested resource is owned or awaiting cleanup")
            generation = int(db.execute("SELECT value FROM meta WHERE key='generation'").fetchone()[0]) + 1
            self._meta(db, "generation", generation)
            lease_id = str(uuid.uuid4())
            db.execute("""INSERT INTO leases(lease_id,actor,work_id,attempt_id,request_id,spec,generation,
                          status,created_at,expires_at,deadline_mono,boot_id,metadata)
                          VALUES(?,?,?,?,?,?,?,'active',?,?,?,?,?)""",
                       (lease_id, actor, work_id, attempt_id, request_id, spec, generation, wall, wall+ttl, mono+ttl, boot, _encoded(metadata)))
            for resource in resources:
                db.execute("INSERT INTO resources VALUES(?,?,?) ON CONFLICT(resource_key) DO UPDATE SET generation=excluded.generation,lease_id=excluded.lease_id",
                           (resource, generation, lease_id))
                db.execute("INSERT INTO lease_resources VALUES(?,?)", (lease_id, resource))
            return self._view(db, self._lease(db, lease_id))

    def renew(self, actor, lease_id, generation, ttl_seconds=300):
        _identifier(actor, "actor")
        ttl = _ttl(ttl_seconds)
        with self._transaction() as (db, sample):
            wall, mono, boot, _ = sample
            self._lease(db, lease_id, actor, generation, active=True)
            db.execute("UPDATE leases SET expires_at=?,deadline_mono=?,boot_id=? WHERE lease_id=?", (wall+ttl, mono+ttl, boot, lease_id))
            return self._view(db, self._lease(db, lease_id))

    def begin_action(self, actor, lease_id, generation, request_id, operation, payload_digest, mutating=True):
        _identifier(actor, "actor")
        _identifier(request_id, "request ID")
        _identifier(operation, "operation")
        if not isinstance(payload_digest, str) or not _DIGEST.fullmatch(payload_digest) or type(mutating) is not bool:
            raise ResourceError("invalid_input", "Invalid action fingerprint or classification")
        with self._transaction() as (db, sample):
            wall, _, _, _ = sample
            self._lease(db, lease_id, actor, generation)
            prior = db.execute("SELECT * FROM actions WHERE lease_id=? AND request_id=?", (lease_id, request_id)).fetchone()
            if prior:
                if (prior["operation"], prior["payload_digest"], bool(prior["mutating"])) != (operation, payload_digest, mutating):
                    raise ResourceError("request_mismatch", "Action request ID was already used")
                return self._action_view(prior)
            self._lease(db, lease_id, actor, generation, active=True)
            if db.execute("SELECT 1 FROM actions WHERE lease_id=? AND status IN ('in_flight','unknown')", (lease_id,)).fetchone():
                raise ResourceError("action_pending", "Prior action requires completion or reconciliation")
            db.execute("INSERT INTO actions(lease_id,request_id,operation,payload_digest,mutating,status,started_at) VALUES(?,?,?,?,?,'in_flight',?)",
                       (lease_id, request_id, operation, payload_digest, int(mutating), wall))
            return self._action_view(db.execute("SELECT * FROM actions WHERE lease_id=? AND request_id=?", (lease_id, request_id)).fetchone(), dispatch=True)

    def finish_action(self, actor, lease_id, request_id, outcome, receipt=None):
        _identifier(actor, "actor")
        if outcome not in {"completed", "failed", "unknown"}:
            raise ResourceError("invalid_input", "Invalid action outcome")
        receipt = _bounded_metadata(receipt, _RECEIPT)
        with self._transaction() as (db, sample):
            wall, _, _, _ = sample
            self._lease(db, lease_id, actor)
            row = db.execute("SELECT * FROM actions WHERE lease_id=? AND request_id=?", (lease_id, request_id)).fetchone()
            if row is None:
                raise ResourceError("action_not_found", "Action was not dispatched")
            if row["status"] != "in_flight":
                if row["status"] != outcome:
                    raise ResourceError("outcome_final", "Action outcome requires explicit reconciliation")
                return self._action_view(row)
            db.execute("UPDATE actions SET status=?,finished_at=?,receipt=? WHERE lease_id=? AND request_id=?",
                       (outcome, wall, _encoded(receipt), lease_id, request_id))
            if outcome == "unknown":
                db.execute("UPDATE leases SET status='quarantined',reason='action_unknown' WHERE lease_id=?", (lease_id,))
            return self._action_view(db.execute("SELECT * FROM actions WHERE lease_id=? AND request_id=?", (lease_id, request_id)).fetchone())

    def revoke(self, actor, lease_id, generation, reason, *, control=None):
        _identifier(actor, "actor")
        _identifier(reason, "reason code")
        with self._transaction() as (db, sample):
            replay = self._control_replay(db, control)
            if replay is not None:
                return replay
            row = self._lease(db, lease_id, actor, generation)
            if row["status"] == "active":
                db.execute("UPDATE leases SET status='revoking',reason=? WHERE lease_id=?", (reason, lease_id))
            return self._control_result(db, control, self._view(db, self._lease(db, lease_id)))

    def expired(self):
        with self._transaction() as (db, sample):
            rows = db.execute("SELECT * FROM leases WHERE status='revoking' ORDER BY created_at LIMIT 100").fetchall()
            return [self._view(db, row) for row in rows]

    def cleanup_result(self, lease_id, generation, clean, receipt):
        if type(clean) is not bool:
            raise ResourceError("invalid_input", "Cleanup proof must be boolean")
        receipt = _bounded_metadata(receipt, _RECEIPT)
        with self._transaction() as (db, sample):
            row = self._lease(db, lease_id, generation=generation)
            if row["status"] == "active":
                raise ResourceError("lease_active", "Revoke the lease before cleanup")
            if row["status"] == "closed":
                return self._view(db, row)
            pending = db.execute("SELECT COUNT(*) FROM actions WHERE lease_id=? AND status IN ('in_flight','unknown')", (lease_id,)).fetchone()[0]
            status, reason = ("closed", "cleanup_verified") if clean and not pending else ("quarantined", "action_unknown" if pending else "cleanup_failed")
            db.execute("UPDATE leases SET status=?,reason=?,cleanup_verified=?,cleanup_receipt=? WHERE lease_id=?",
                       (status, reason, int(clean), _encoded(receipt), lease_id))
            if status == "closed":
                db.execute("UPDATE resources SET lease_id=NULL WHERE lease_id=? AND generation=?", (lease_id, generation))
            return self._view(db, self._lease(db, lease_id))

    def reconcile(self, actor, lease_id, generation, evidence, *, control=None):
        _identifier(actor, "actor")
        evidence = _bounded_metadata(evidence, _RECEIPT)
        if evidence.get("mutation_resolved") is not True or not evidence.get("evidence_ref"):
            raise ResourceError("evidence_required", "Explicit mutation reconciliation evidence is required")
        with self._transaction() as (db, sample):
            replay = self._control_replay(db, control)
            if replay is not None:
                return replay
            wall, _, _, _ = sample
            row = self._lease(db, lease_id, actor, generation)
            if row["status"] != "quarantined" or not row["cleanup_verified"]:
                raise ResourceError("cleanup_unproved", "Verified cleanup is required before reconciliation")
            db.execute("UPDATE actions SET status='reconciled',finished_at=?,receipt=? WHERE lease_id=? AND status IN ('in_flight','unknown')",
                       (wall, _encoded(evidence), lease_id))
            db.execute("UPDATE leases SET status='closed',reason='reconciled' WHERE lease_id=?", (lease_id,))
            db.execute("UPDATE resources SET lease_id=NULL WHERE lease_id=? AND generation=?", (lease_id, generation))
            return self._control_result(db, control, self._view(db, self._lease(db, lease_id)))

    def recover(self, controller_id):
        _identifier(controller_id, "controller ID")
        with self._transaction() as (db, sample):
            wall, _, _, _ = sample
            old = db.execute("SELECT value FROM meta WHERE key='controller'").fetchone()
            if old and old[0] != controller_id:
                db.execute("UPDATE leases SET status='revoking',reason='controller_restarted' WHERE status='active'")
                db.execute("UPDATE actions SET status=CASE WHEN mutating=1 THEN 'unknown' ELSE 'failed' END,finished_at=? WHERE status='in_flight'", (wall,))
            self._meta(db, "controller", controller_id)
            return [self._view(db, row) for row in db.execute("SELECT * FROM leases WHERE status IN ('revoking','quarantined') ORDER BY created_at LIMIT 100").fetchall()]

    def get(self, actor, lease_id):
        _identifier(actor, "actor")
        with self._transaction() as (db, sample):
            return self._view(db, self._lease(db, lease_id, actor))

    def list_leases(self, actor=None, limit=100):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ResourceError("invalid_input", "Limit must be 1 to 100")
        with self._transaction() as (db, sample):
            rows = db.execute("SELECT * FROM leases WHERE (? IS NULL OR actor=?) ORDER BY generation DESC LIMIT ?", (actor, actor, limit)).fetchall()
            return [self._view(db, row) for row in rows]

    def count_live(self):
        with self._transaction() as (db, sample):
            return db.execute("SELECT COUNT(*) FROM leases WHERE status<>'closed'").fetchone()[0]

    def hold(self, actor, lease_id, generation, reason="operator_stop", *, control=None):
        """Trusted operator path: prevent automatic reacquisition after takeover."""
        _identifier(actor, "actor")
        _identifier(reason, "reason code")
        with self._transaction() as (db, sample):
            replay = self._control_replay(db, control)
            if replay is not None:
                return replay
            wall, _, _, _ = sample
            row = self._lease(db, lease_id, actor, generation)
            keys = [f"work:{row['work_id']}"]
            keys += [r[0] for r in db.execute("SELECT resource_key FROM lease_resources WHERE lease_id=?", (lease_id,))]
            for key in keys:
                db.execute("INSERT INTO holds VALUES(?,?,?,?) ON CONFLICT(hold_key,lease_id) DO UPDATE SET reason=excluded.reason,created_at=excluded.created_at",
                           (key, lease_id, reason, wall))
            db.execute("UPDATE leases SET status='revoking',reason=? WHERE lease_id=? AND status='active'", (reason, lease_id))
            return self._control_result(db, control, self._view(db, self._lease(db, lease_id)))

    def resume(self, actor, lease_id, generation, *, control=None):
        """Trusted operator path; does not resurrect or renew the old lease."""
        _identifier(actor, "actor")
        with self._transaction() as (db, sample):
            replay = self._control_replay(db, control)
            if replay is not None:
                return replay
            row = self._lease(db, lease_id, actor, generation)
            if row["status"] != "closed":
                raise ResourceError("cleanup_unproved", "Resolve cleanup before resuming stopped work")
            keys = [f"work:{row['work_id']}"]
            keys += [r[0] for r in db.execute("SELECT resource_key FROM lease_resources WHERE lease_id=?", (lease_id,))]
            for key in keys:
                db.execute("DELETE FROM holds WHERE hold_key=? AND lease_id=?", (key, lease_id))
            return self._control_result(db, control, {"resumed": True, "lease_id": lease_id, "old_lease_status": "closed"})

    @staticmethod
    def _control_replay(db, control):
        if control is None:
            return None
        actor, request_id, spec = control
        _identifier(actor, "control actor")
        _identifier(request_id, "control request ID")
        prior = db.execute("SELECT spec,result FROM controls WHERE actor=? AND request_id=?",
                           (actor, request_id)).fetchone()
        if prior:
            if prior["spec"] != spec:
                raise ResourceError("request_mismatch", "Control request ID was already used")
            return {**json.loads(prior["result"]), "replayed": True}
        return None

    @staticmethod
    def _control_result(db, control, result):
        if control is not None:
            actor, request_id, spec = control
            db.execute("INSERT INTO controls VALUES(?,?,?,?)", (actor, request_id, spec, _encoded(result)))
        return result
