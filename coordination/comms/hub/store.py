"""SQLite-backed inbox, discovery, assignment, and grant engine.

The store deliberately keeps the business boundary small: callers authenticate an
actor, then invoke :meth:`Store.call`.  Every authorization decision is made from
the live grant graph in the same database that contains the requested mutation.
"""

from __future__ import annotations

import base64
import contextlib
import datetime as _datetime
import hashlib
import hmac
import json
import os
import re
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Iterator


MAX_BODY_BYTES = 16 * 1024
MAX_REQUEST_BYTES = 128 * 1024
MAX_LIMIT = 100
LIST_SCAN_LIMIT = 1000
MAX_SUPERSEDES = 20
PENDING_SCAN_LIMIT = 10_000
PENDING_QUERY_STEPS = 2_000_000
DEFAULT_POLL_LIMIT = 20
DEFAULT_LEASE_SECONDS = 60
MAX_LEASE_SECONDS = 24 * 60 * 60
MAX_DELEGATION_DEPTH = 64
_NATIVE_ID = re.compile(r"^[A-Za-z0-9_.:@/-]{1,128}$")


class HubError(Exception):
    """A safe, serializable business error for transports and callers."""

    def __init__(self, code: str, message: str, status: int = 400):
        self.code = code
        self.message = message
        self.status = status
        self._commit_on_error = False
        super().__init__(message)

    def as_dict(self) -> dict[str, Any]:
        return {"error": {"code": self.code, "message": self.message}}


def _utc_now() -> _datetime.datetime:
    return _datetime.datetime.now(_datetime.timezone.utc)


def _iso_now() -> str:
    return _utc_now().isoformat(timespec="microseconds").replace("+00:00", "Z")


def _epoch_to_iso(value: float | None) -> str | None:
    if value is None:
        return None
    return (
        _datetime.datetime.fromtimestamp(float(value), _datetime.timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _json_dump(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise HubError("invalid_json", "value must be JSON-compatible") from exc


def _json_load(value: str | None, default: Any) -> Any:
    if value is None:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError) as exc:
        raise HubError("invalid_data", "stored JSON is invalid", 500) from exc


def _hash_params(params: dict[str, Any]) -> str:
    encoded = _json_dump(params).encode("utf-8")
    if len(encoded) > MAX_REQUEST_BYTES:
        raise HubError("request_too_large", "request exceeds 128 KiB", 413)
    return hashlib.sha256(encoded).hexdigest()


def _text(value: Any, field: str, *, required: bool = True, max_chars: int = 1024) -> str | None:
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value.strip():
        raise HubError("invalid_input", f"{field} must be a non-empty string")
    if len(value) > max_chars:
        raise HubError("invalid_input", f"{field} is too long")
    return value


def _native_identity(value: Any, field: str) -> str | None:
    """Validate an optional opaque native identifier without normalizing it."""

    if value is None:
        return None
    result = _text(value, field, max_chars=128)
    assert result is not None
    if _NATIVE_ID.fullmatch(result) is None:
        raise HubError("invalid_input", f"{field} is not a valid native identifier")
    return result


def _body(value: Any, field: str = "body") -> str:
    if not isinstance(value, str):
        raise HubError("invalid_input", f"{field} must be a string")
    if len(value.encode("utf-8")) > MAX_BODY_BYTES:
        raise HubError("body_too_large", f"{field} exceeds 16 KiB", 413)
    return value


def _string_list(value: Any, field: str, *, required: bool = True, max_items: int = 100) -> list[str]:
    if value is None and not required:
        return []
    if not isinstance(value, list):
        raise HubError("invalid_input", f"{field} must be a list of strings")
    if not value and required:
        raise HubError("invalid_input", f"{field} must not be empty")
    if len(value) > max_items:
        raise HubError("invalid_input", f"{field} has too many entries")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise HubError("invalid_input", f"{field} must contain non-empty strings")
        if len(item) > 2048:
            raise HubError("invalid_input", f"{field} contains an overly long value")
        result.append(item)
    return result


def _canonical_scope(value: Any, field: str = "scope") -> str:
    if not isinstance(value, str) or not value:
        raise HubError("invalid_scope", f"{field} must be an absolute slash-separated path")
    if value == "/":
        return value
    if not value.startswith("/") or value.endswith("/") or "//" in value or "\\" in value:
        raise HubError("invalid_scope", f"{field} is not a canonical scope")
    parts = value[1:].split("/")
    if any(not part or part in {".", ".."} for part in parts):
        raise HubError("invalid_scope", f"{field} contains an empty or dot path segment")
    if any(len(part) > 255 for part in parts):
        raise HubError("invalid_scope", f"{field} contains an overly long segment")
    if len(value) > 4096:
        raise HubError("invalid_scope", f"{field} is too long")
    return value


def _scope_contains(parent: str, child: str) -> bool:
    return parent == "/" or child == parent or child.startswith(parent + "/")


def _actions(value: Any, field: str = "actions") -> list[str]:
    result = _string_list(value, field, max_items=100)
    if any(action == "" or action.isspace() for action in result):
        raise HubError("invalid_input", f"{field} contains an empty action")
    return sorted(set(result))


def _action_allowed(actions: list[str], action: str) -> bool:
    return "*" in actions or action in actions


def _actions_subset(child: list[str], parent: list[str]) -> bool:
    return "*" in parent or all(action in parent for action in child)


def _timestamp(value: Any, field: str, *, required: bool = False) -> str | None:
    if value is None:
        if required:
            raise HubError("invalid_input", f"{field} is required")
        return None
    if not isinstance(value, str) or not value:
        raise HubError("invalid_input", f"{field} must be an ISO8601 timestamp")
    candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = _datetime.datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise HubError("invalid_input", f"{field} must be an ISO8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise HubError("invalid_input", f"{field} must include a timezone")
    parsed = parsed.astimezone(_datetime.timezone.utc)
    return parsed.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _is_expired(expires_at: str | None, now_iso: str) -> bool:
    return expires_at is not None and expires_at <= now_iso


def _uuid() -> str:
    return str(uuid.uuid4())


SCHEMA = """
CREATE TABLE IF NOT EXISTS hub_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS request_receipts (
    request_id TEXT PRIMARY KEY,
    actor TEXT NOT NULL,
    operation TEXT NOT NULL,
    params_hash TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agents (
    agent_id TEXT PRIMARY KEY,
    runtime TEXT NOT NULL,
    machine TEXT NOT NULL,
    display_name TEXT,
    capabilities_json TEXT NOT NULL,
    instance_id TEXT NOT NULL,
    session_id TEXT,
    thread_id TEXT,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_seen TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS grants (
    id TEXT PRIMARY KEY,
    issuer TEXT NOT NULL,
    grantee TEXT NOT NULL,
    scope TEXT NOT NULL,
    actions_json TEXT NOT NULL,
    delegable INTEGER NOT NULL,
    parent_grant_id TEXT,
    expires_at TEXT,
    revoked_at TEXT,
    reason TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY(parent_grant_id) REFERENCES grants(id)
);

CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    sender TEXT NOT NULL,
    kind TEXT NOT NULL,
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    scope TEXT NOT NULL,
    work_id TEXT,
    reply_to TEXT,
    artifacts_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT,
    authority_grant_id TEXT,
    authority_json TEXT,
    assignment_version INTEGER,
    assignment_assignee TEXT,
    assignment_binding INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY(authority_grant_id) REFERENCES grants(id)
);

CREATE TABLE IF NOT EXISTS deliveries (
    message_id TEXT NOT NULL,
    recipient TEXT NOT NULL,
    state TEXT NOT NULL,
    lease_id TEXT,
    lease_until REAL,
    attempts INTEGER NOT NULL DEFAULT 0,
    acknowledged_at TEXT,
    receipt_ref TEXT,
    rejection_reason TEXT,
    PRIMARY KEY(message_id, recipient),
    FOREIGN KEY(message_id) REFERENCES messages(id)
);

CREATE TABLE IF NOT EXISTS discoveries (
    id TEXT PRIMARY KEY,
    publisher TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    scope TEXT NOT NULL,
    topics_json TEXT NOT NULL,
    artifacts_json TEXT NOT NULL,
    work_id TEXT,
    expires_at TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS assignments (
    work_id TEXT PRIMARY KEY,
    assignee TEXT NOT NULL,
    scope TEXT NOT NULL,
    summary TEXT NOT NULL,
    grant_id TEXT,
    version INTEGER NOT NULL,
    assigned_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(grant_id) REFERENCES grants(id)
);

CREATE TABLE IF NOT EXISTS assignment_history (
    id TEXT PRIMARY KEY,
    work_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    prior_owner TEXT,
    new_owner TEXT NOT NULL,
    changed_by TEXT NOT NULL,
    scope TEXT NOT NULL,
    summary TEXT NOT NULL,
    grant_id TEXT,
    event TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(work_id) REFERENCES assignments(work_id),
    FOREIGN KEY(grant_id) REFERENCES grants(id)
);

CREATE INDEX IF NOT EXISTS idx_grants_grantee ON grants(grantee);
CREATE INDEX IF NOT EXISTS idx_grants_issuer ON grants(issuer);
CREATE INDEX IF NOT EXISTS idx_messages_sender ON messages(sender, created_at);
CREATE INDEX IF NOT EXISTS idx_deliveries_recipient_state ON deliveries(recipient, state, message_id);
CREATE INDEX IF NOT EXISTS idx_discoveries_scope ON discoveries(scope, created_at);
CREATE INDEX IF NOT EXISTS idx_assignment_history_work ON assignment_history(work_id, version);
"""


class Store:
    """Thread-safe-by-connection SQLite store for the hub business operations."""

    _MUTATIONS = {
        "agents.register",
        "agents.heartbeat",
        "messages.send",
        "messages.poll",
        "messages.ack",
        "discoveries.publish",
        "grants.issue",
        "grants.revoke",
        "assignments.assign",
        "assignments.reassign",
    }
    _OPERATIONS = _MUTATIONS | {
        "agents.list",
        "messages.list",
        "messages.get",
        "discoveries.search",
        "grants.list",
        "grants.get",
        "assignments.list",
        "owner.snapshot",
        "authorize",
    }

    def __init__(self, db_path: str | os.PathLike[str]):
        raw_path = os.fspath(db_path)
        if not isinstance(raw_path, str) or not raw_path:
            raise ValueError("db_path must be a path or :memory:")
        self.db_path = raw_path
        self._closed = False
        self._memory_anchor: sqlite3.Connection | None = None
        self._connect_uri = False
        if raw_path == ":memory:":
            self._connect_target = f"file:hub-{_uuid()}?mode=memory&cache=shared"
            self._connect_uri = True
            self._memory_anchor = self._open_connection()
        else:
            path = Path(raw_path)
            if path.parent and str(path.parent) != ".":
                path.parent.mkdir(parents=True, exist_ok=True)
            self._connect_target = raw_path
        self._initialize()

    def _open_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self._connect_target,
            timeout=30.0,
            isolation_level=None,
            check_same_thread=False,
            uri=self._connect_uri,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 30000")
        return conn

    def _initialize(self) -> None:
        conn = self._open_connection()
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
            conn.executescript(SCHEMA)
            self._migrate_schema(conn)
            conn.execute(
                "INSERT OR IGNORE INTO hub_meta(key, value) VALUES('list_cursor_key', ?)",
                (os.urandom(32).hex(),),
            )
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _migrate_schema(conn: sqlite3.Connection) -> None:
        """Add small additive fields needed by newer core receipts."""

        message_columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(messages)").fetchall()
        }
        message_migrations = {
            "assignment_version": "INTEGER",
            "assignment_assignee": "TEXT",
            "assignment_binding": "INTEGER NOT NULL DEFAULT 0",
            "supersedes_json": "TEXT",
        }
        for column, definition in message_migrations.items():
            if column not in message_columns:
                conn.execute(f"ALTER TABLE messages ADD COLUMN {column} {definition}")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_messages_assignment "
            "ON messages(work_id, assignment_binding, assignment_version)"
        )
        agent_columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(agents)").fetchall()
        }
        for column in ("session_id", "thread_id"):
            if column not in agent_columns:
                conn.execute(f"ALTER TABLE agents ADD COLUMN {column} TEXT")

        conn.execute("SAVEPOINT migrate_delivery_clocks")
        try:
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(deliveries)")}
            migrations = {
                "first_acknowledged_at": "TEXT",
                "resolved_at": "TEXT",
                "acknowledgment_time_uncertain": "INTEGER NOT NULL DEFAULT 0",
                "work_outcome_ref": "TEXT",
                "superseded_by": "TEXT",
            }
            for column, definition in migrations.items():
                if column not in columns:
                    conn.execute(f"ALTER TABLE deliveries ADD COLUMN {column} {definition}")
            if "first_acknowledged_at" not in columns:
                # Legacy acknowledged_at was rewritten on repeated acknowledgments
                # as well as resolution. Do not invent a first-ack timestamp.
                conn.execute(
                    "UPDATE deliveries SET acknowledgment_time_uncertain = 1 "
                    "WHERE acknowledged_at IS NOT NULL OR state IN ('acknowledged', 'resolved')"
                )
                conn.execute(
                    "UPDATE deliveries SET resolved_at = acknowledged_at WHERE state = 'resolved'"
                )
            # Old code can acknowledge rows during a rollback without writing
            # the additive first-ack column. Re-upgrade must retain uncertainty
            # rather than later mislabeling a resolution as the first ACK.
            conn.execute(
                "UPDATE deliveries SET acknowledgment_time_uncertain = 1 "
                "WHERE first_acknowledged_at IS NULL AND "
                "(acknowledged_at IS NOT NULL OR state IN ('acknowledged', 'resolved'))"
            )
        except BaseException:
            conn.execute("ROLLBACK TO migrate_delivery_clocks")
            raise
        finally:
            conn.execute("RELEASE migrate_delivery_clocks")

        # Connections use autocommit: the column and its one-time backfill must
        # commit together, so an interrupted upgrade can safely retry both.
        conn.execute("SAVEPOINT migrate_delivery_attempt")
        try:
            delivery_columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(deliveries)").fetchall()
            }
            if "last_attempt_at" not in delivery_columns:
                conn.execute("ALTER TABLE deliveries ADD COLUMN last_attempt_at TEXT")
                # Legacy retries have no recorded attempt time. Place them after
                # the existing unseen backlog once, without changing their leases.
                conn.execute(
                    "UPDATE deliveries SET last_attempt_at = ? WHERE attempts > 0",
                    (_iso_now(),),
                )
        except BaseException:
            conn.execute("ROLLBACK TO migrate_delivery_attempt")
            raise
        finally:
            conn.execute("RELEASE migrate_delivery_attempt")

    def close(self) -> None:
        """Close the memory anchor; file-backed calls use short-lived connections."""

        if self._closed:
            return
        self._closed = True
        if self._memory_anchor is not None:
            self._memory_anchor.close()
            self._memory_anchor = None

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, _exc_type: Any, _exc: Any, _tb: Any) -> None:
        self.close()

    @contextlib.contextmanager
    def _transaction(self, write: bool) -> Iterator[sqlite3.Connection]:
        if self._closed:
            raise HubError("store_closed", "store is closed", 500)
        conn = self._open_connection()
        try:
            conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield conn
            conn.commit()
        except Exception as exc:
            with contextlib.suppress(Exception):
                if getattr(exc, "_commit_on_error", False):
                    conn.commit()
                else:
                    conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def _validate_actor(actor: Any) -> str:
        if not isinstance(actor, str) or not actor.strip() or len(actor) > 256:
            raise HubError("invalid_actor", "actor must be a non-empty identifier")
        return actor

    @staticmethod
    def _validate_request_id(request_id: Any) -> str:
        if not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 512:
            raise HubError("invalid_request_id", "request_id must be a non-empty identifier")
        return request_id

    @staticmethod
    def _limit(value: Any, default: int = MAX_LIMIT) -> int:
        if value is None:
            return default
        if isinstance(value, bool) or not isinstance(value, int) or value < 1 or value > MAX_LIMIT:
            raise HubError("invalid_limit", f"limit must be an integer from 1 to {MAX_LIMIT}")
        return value

    @staticmethod
    def _meta(conn: sqlite3.Connection, key: str) -> str | None:
        row = conn.execute("SELECT value FROM hub_meta WHERE key = ?", (key,)).fetchone()
        return None if row is None else str(row["value"])

    @staticmethod
    def _set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
        conn.execute(
            "INSERT INTO hub_meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    def _is_owner(self, conn: sqlite3.Connection, actor: str) -> bool:
        return self._meta(conn, "owner_actor") == actor

    def _principal_exists(self, conn: sqlite3.Connection, actor: str) -> bool:
        if self._is_owner(conn, actor):
            return True
        row = conn.execute("SELECT 1 FROM agents WHERE agent_id = ?", (actor,)).fetchone()
        return row is not None

    @staticmethod
    def _agent_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "agent_id": row["agent_id"],
            "runtime": row["runtime"],
            "machine": row["machine"],
            "display_name": row["display_name"],
            "capabilities": _json_load(row["capabilities_json"], []),
            "instance_id": row["instance_id"],
            "session_id": row["session_id"],
            "thread_id": row["thread_id"],
            "status": row["status"],
            "created_at": row["created_at"],
            "last_seen": row["last_seen"],
        }

    @staticmethod
    def _grant_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "issuer": row["issuer"],
            "grantee": row["grantee"],
            "scope": row["scope"],
            "actions": _json_load(row["actions_json"], []),
            "delegable": bool(row["delegable"]),
            "parent_grant_id": row["parent_grant_id"],
            "expires_at": row["expires_at"],
            "revoked_at": row["revoked_at"],
            "reason": row["reason"],
            "created_at": row["created_at"],
        }

    @staticmethod
    def _delivery_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "message_id": row["message_id"],
            "recipient": row["recipient"],
            "state": row["state"],
            "lease_id": row["lease_id"],
            "lease_until": _epoch_to_iso(row["lease_until"]),
            "attempts": int(row["attempts"]),
            "acknowledged_at": row["acknowledged_at"],
            "first_acknowledged_at": row["first_acknowledged_at"],
            "resolved_at": row["resolved_at"],
            "acknowledgment_time_uncertain": bool(row["acknowledgment_time_uncertain"]),
            "work_outcome_ref": row["work_outcome_ref"],
            "work_acceptance": "not_asserted",
            "superseded_by": row["superseded_by"],
            "receipt_ref": row["receipt_ref"],
            "rejection_reason": row["rejection_reason"],
        }

    @staticmethod
    def _message_dict(row: sqlite3.Row) -> dict[str, Any]:
        authority = _json_load(row["authority_json"], None)
        return {
            "id": row["id"],
            "sender": row["sender"],
            "kind": row["kind"],
            "subject": row["subject"],
            "body": row["body"],
            "scope": row["scope"],
            "work_id": row["work_id"],
            "reply_to": row["reply_to"],
            "artifacts": _json_load(row["artifacts_json"], []),
            "created_at": row["created_at"],
            "expires_at": row["expires_at"],
            "authority_grant_id": row["authority_grant_id"],
            "authority": authority,
            "assignment_version": row["assignment_version"],
            "assignment_assignee": row["assignment_assignee"],
            "assignment_binding": bool(row["assignment_binding"]),
            "supersedes": _json_load(row["supersedes_json"], []),
        }

    @staticmethod
    def _discovery_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "publisher": row["publisher"],
            "title": row["title"],
            "body": row["body"],
            "scope": row["scope"],
            "topics": _json_load(row["topics_json"], []),
            "artifacts": _json_load(row["artifacts_json"], []),
            "work_id": row["work_id"],
            "expires_at": row["expires_at"],
            "created_at": row["created_at"],
        }

    @staticmethod
    def _assignment_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "work_id": row["work_id"],
            "assignee": row["assignee"],
            "scope": row["scope"],
            "summary": row["summary"],
            "grant_id": row["grant_id"],
            "version": int(row["version"]),
            "assigned_by": row["assigned_by"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def _grant_chain(
        self,
        conn: sqlite3.Connection,
        grant_id: str,
        now_iso: str,
        seen: set[str] | None = None,
    ) -> list[sqlite3.Row] | None:
        """Return a bounded root-to-leaf chain without recursive validation."""

        chain: list[sqlite3.Row] = []
        visited = set(seen or ())
        current_id = grant_id
        for _depth in range(MAX_DELEGATION_DEPTH):
            if current_id in visited:
                return None
            visited.add(current_id)
            row = conn.execute("SELECT * FROM grants WHERE id = ?", (current_id,)).fetchone()
            if row is None or row["revoked_at"] is not None or _is_expired(row["expires_at"], now_iso):
                return None
            try:
                scope = _canonical_scope(row["scope"])
                actions = _actions(_json_load(row["actions_json"], []))
            except HubError:
                return None
            chain.append(row)
            if row["parent_grant_id"] is None:
                owner = self._meta(conn, "owner_actor")
                if (
                    owner is None
                    or row["id"] != self._meta(conn, "seed_grant_id")
                    or row["issuer"] != owner
                    or row["grantee"] != owner
                    or scope != "/"
                    or actions != ["*"]
                    or not bool(row["delegable"])
                ):
                    return None
                return list(reversed(chain))
            parent = conn.execute(
                "SELECT * FROM grants WHERE id = ?", (row["parent_grant_id"],)
            ).fetchone()
            if parent is None:
                return None
            try:
                parent_scope = _canonical_scope(parent["scope"])
                parent_actions = _actions(_json_load(parent["actions_json"], []))
            except HubError:
                return None
            if parent["grantee"] != row["issuer"]:
                return None
            if not _scope_contains(parent_scope, scope):
                return None
            if not _actions_subset(actions, parent_actions):
                return None
            if bool(row["delegable"]) and not bool(parent["delegable"]):
                return None
            parent_expiry = parent["expires_at"]
            if parent_expiry is not None and (
                row["expires_at"] is None or row["expires_at"] > parent_expiry
            ):
                return None
            current_id = parent["id"]
        return None

    def _authorize_conn(
        self, conn: sqlite3.Connection, actor: str, action: str, scope: str
    ) -> dict[str, Any]:
        actor = self._validate_actor(actor)
        if not isinstance(action, str) or not action.strip():
            raise HubError("invalid_action", "action must be a non-empty string")
        scope = _canonical_scope(scope)
        now_iso = _iso_now()
        rows = conn.execute("SELECT * FROM grants WHERE grantee = ? ORDER BY created_at, id", (actor,)).fetchall()
        grant_ids: list[str] = []
        evidence: list[dict[str, Any]] = []
        for row in rows:
            chain = self._grant_chain(conn, row["id"], now_iso)
            if chain is None:
                continue
            leaf_scope = _canonical_scope(row["scope"])
            leaf_actions = _actions(_json_load(row["actions_json"], []))
            if _scope_contains(leaf_scope, scope) and _action_allowed(leaf_actions, action):
                grant_ids.append(row["id"])
                evidence.append(
                    {
                        "grant_id": row["id"],
                        "chain": [self._grant_dict(item) for item in chain],
                    }
                )
        result: dict[str, Any] = {
            "allowed": bool(grant_ids),
            "actor": actor,
            "action": action,
            "scope": scope,
            "grant_ids": grant_ids,
            "evidence": evidence,
        }
        if not grant_ids:
            result["reason"] = "no active grant permits this action and scope"
        return result

    def authorize(self, actor: str, action: str, scope: str) -> dict[str, Any]:
        """Return live grant evidence for an actor/action/scope tuple."""

        actor = self._validate_actor(actor)
        with self._transaction(False) as conn:
            return self._authorize_conn(conn, actor, action, scope)

    def _require_auth(
        self, conn: sqlite3.Connection, actor: str, action: str, scope: str
    ) -> dict[str, Any]:
        result = self._authorize_conn(conn, actor, action, scope)
        if not result["allowed"]:
            raise HubError("forbidden", f"actor is not authorized for {action} in {scope}", 403)
        return result

    def _select_authority(
        self,
        conn: sqlite3.Connection,
        actor: str,
        action: str,
        scope: str,
        requested_grant_id: str | None = None,
    ) -> dict[str, Any]:
        if requested_grant_id is None:
            return self._require_auth(conn, actor, action, scope)
        requested_grant_id = _text(requested_grant_id, "grant_id", max_chars=256)
        assert requested_grant_id is not None
        row = conn.execute("SELECT * FROM grants WHERE id = ?", (requested_grant_id,)).fetchone()
        now_iso = _iso_now()
        if row is None or row["grantee"] != actor:
            raise HubError("forbidden", "grant is not held by actor", 403)
        chain = self._grant_chain(conn, row["id"], now_iso)
        if chain is None:
            raise HubError("forbidden", "grant is not active", 403)
        grant_scope = _canonical_scope(row["scope"])
        grant_actions = _actions(_json_load(row["actions_json"], []))
        requested_scope = _canonical_scope(scope)
        if not _scope_contains(grant_scope, requested_scope) or not _action_allowed(grant_actions, action):
            raise HubError("forbidden", "grant does not cover requested authority", 403)
        return {
            "allowed": True,
            "actor": actor,
            "action": action,
            "scope": requested_scope,
            "grant_ids": [row["id"]],
            "evidence": [{"grant_id": row["id"], "chain": [self._grant_dict(item) for item in chain]}],
        }

    def _current_message_authority(
        self, conn: sqlite3.Connection, message: sqlite3.Row
    ) -> dict[str, Any] | None:
        if message["kind"] != "instruction":
            return _json_load(message["authority_json"], None)
        grant_id = message["authority_grant_id"]
        if grant_id is None:
            return {
                "allowed": False,
                "actor": message["sender"],
                "action": "instructions.issue",
                "scope": message["scope"],
                "grant_ids": [],
                "evidence": [],
                "reason": "instruction has no authority grant",
            }
        try:
            authority = self._select_authority(
                conn, message["sender"], "instructions.issue", message["scope"], grant_id
            )
        except HubError as exc:
            return {
                "allowed": False,
                "actor": message["sender"],
                "action": "instructions.issue",
                "scope": message["scope"],
                "grant_ids": [],
                "evidence": [],
                "reason": exc.message,
            }
        if bool(message["assignment_binding"]):
            assignment = conn.execute(
                "SELECT assignee, scope, version FROM assignments WHERE work_id = ?",
                (message["work_id"],),
            ).fetchone()
            if (
                assignment is None
                or message["assignment_version"] is None
                or int(assignment["version"]) != int(message["assignment_version"])
                or assignment["assignee"] != message["assignment_assignee"]
                or assignment["scope"] != message["scope"]
            ):
                authority = dict(authority)
                authority["allowed"] = False
                authority["grant_ids"] = []
                authority["reason"] = "assignment notice has been superseded"
        return authority

    def _message_output(
        self, conn: sqlite3.Connection, message: sqlite3.Row, viewer: str
    ) -> dict[str, Any]:
        result = self._message_dict(message)
        result["authority"] = self._current_message_authority(conn, message)
        direct = viewer == message["sender"] or self._authorize_conn(
            conn, viewer, "owner.read", message["scope"]
        )["allowed"]
        deliveries = conn.execute(
            "SELECT * FROM deliveries WHERE message_id = ? ORDER BY recipient", (message["id"],)
        ).fetchall()
        visible_deliveries = [
            self._delivery_dict(delivery)
            for delivery in deliveries
            if direct or delivery["recipient"] == viewer
        ]
        result["deliveries"] = visible_deliveries
        self._add_routing_context(conn, viewer, message, result)
        for delivery in visible_deliveries:
            delivery["recipient_lifecycle"] = self._recipient_lifecycle(conn, delivery["recipient"])
        for delivery in visible_deliveries:
            if delivery["recipient"] == viewer:
                result["delivery"] = delivery
                self._add_instruction_order_context(conn, viewer, message, result)
                break
        return result

    @staticmethod
    def _recipient_lifecycle(conn: sqlite3.Connection, recipient: str) -> dict[str, Any]:
        row = conn.execute("SELECT status, last_seen FROM agents WHERE agent_id = ?", (recipient,)).fetchone()
        # Registration/heartbeat cannot prove that a native consumer is available.
        return {"availability": "unknown", "registered_status": row["status"] if row else None,
                "last_seen": row["last_seen"] if row else None,
                "semantics": "registration_is_not_native_delivery_availability"}

    def _add_routing_context(self, conn: sqlite3.Connection, actor: str,
                             message: sqlite3.Row, result: dict[str, Any]) -> None:
        result["current_owner"] = {"state": "unknown"}
        if not message["work_id"]:
            return
        row = conn.execute("SELECT * FROM assignments WHERE work_id = ?", (message["work_id"],)).fetchone()
        if row is not None and self._assignment_visible(conn, actor, row):
            result["current_owner"] = {"state": "known", "assignee": row["assignee"],
                                       "assignment_version": row["version"],
                                       "recipient_lifecycle": self._recipient_lifecycle(conn, row["assignee"])}

    def _later_instruction_refs(
        self, conn: sqlite3.Connection, actor: str, message: sqlite3.Row, *, pending_only: bool = False
    ) -> tuple[list[dict[str, Any]], bool]:
        """Bounded references to later live instructions, never inferred cancellations."""
        rows = conn.execute(
            "SELECT m.*, d.state AS delivery_state FROM messages m "
            "JOIN deliveries d ON d.message_id = m.id WHERE d.recipient = ? "
            "AND m.kind = 'instruction' AND m.rowid > (SELECT rowid FROM messages WHERE id = ?) "
            "AND d.state IN ('queued', 'leased', 'acknowledged', 'resolved') "
            "AND (? = 0 OR d.state IN ('queued','leased')) "
            "AND (m.work_id = ? OR m.work_id IS NULL OR ? IS NULL OR m.scope != ?) "
            "AND (m.scope = '/' OR ? = '/' OR instr(? || '/', m.scope || '/') = 1 "
            "OR instr(m.scope || '/', ? || '/') = 1) "
            "ORDER BY m.rowid DESC LIMIT ?",
            (actor, message["id"], pending_only, message["work_id"], message["work_id"],
             message["scope"], message["scope"], message["scope"], message["scope"], MAX_LIMIT + 1),
        ).fetchall()
        refs: list[dict[str, Any]] = []
        truncated = len(rows) > MAX_LIMIT
        for row in rows[:MAX_LIMIT]:
            if not (_scope_contains(row["scope"], message["scope"])
                    or _scope_contains(message["scope"], row["scope"])):
                continue
            if _is_expired(row["expires_at"], _iso_now()):
                continue
            authority = self._current_message_authority(conn, row)
            if not authority or not authority.get("allowed"):
                continue
            if row["assignment_binding"] and row["assignment_assignee"] != actor:
                continue
            if len(refs) == 8:
                truncated = True
                break
            refs.append({
                "message_id": row["id"], "sender": row["sender"],
                "scope": row["scope"], "work_id": row["work_id"],
                "created_at": row["created_at"], "state": row["delivery_state"],
                "assignment_binding": bool(row["assignment_binding"]),
                "fetch_operation": "messages.get",
            })
        return refs, truncated

    def _assignment_checkpoint(
        self, conn: sqlite3.Connection, actor: str, work: dict[str, Any]
    ) -> dict[str, Any] | None:
        work_id = work.get("work_id")
        if not isinstance(work_id, str) or not work_id:
            return None
        row = conn.execute("SELECT * FROM assignments WHERE work_id = ?", (work_id,)).fetchone()
        if row is None:
            return {"work_id": work_id, "state": "unknown", "work_effect": "preserve_current"}
        visible = actor == row["assignee"] or self._is_owner(conn, actor)
        if not visible:
            visible = self._authorize_conn(conn, actor, "assignments.read", row["scope"])["allowed"]
        if not visible:
            visible = self._authorize_conn(conn, actor, "assignments.assign", row["scope"])["allowed"]
        if not visible:
            # A former owner may learn that its ownership ended, without
            # gaining access to the new owner's scope or assignment summary.
            prior = conn.execute(
                "SELECT 1 FROM assignment_history WHERE work_id = ? AND prior_owner = ? LIMIT 1",
                (work_id, actor),
            ).fetchone()
            if prior is None:
                return {"work_id": work_id, "state": "unknown", "work_effect": "preserve_current"}
            return {"work_id": work_id, "state": "ownership_changed",
                    "work_effect": "checkpoint_then_reconcile_ownership"}
        state = "current"
        effect = "preserve_current"
        if row["assignee"] != actor:
            state, effect = "ownership_changed", "checkpoint_then_reconcile_ownership"
        elif work.get("assignment_version") not in (None, int(row["version"])):
            state, effect = "assignment_changed", "checkpoint_then_reconcile_ownership"
        return {
            "work_id": work_id, "version": int(row["version"]), "assignee": row["assignee"],
            "scope": row["scope"], "assigned_by": row["assigned_by"], "updated_at": row["updated_at"],
            "state": state, "work_effect": effect, "fetch_operation": "assignments.list",
        }

    def _add_instruction_order_context(
        self, conn: sqlite3.Connection, actor: str, message: sqlite3.Row, result: dict[str, Any]
    ) -> None:
        if message["work_id"]:
            result["current_assignment"] = self._assignment_checkpoint(
                conn, actor, {"work_id": message["work_id"],
                              "assignment_version": message["assignment_version"]})
            if not message["assignment_binding"]:
                # An informational ownership view is not a reassignment of
                # the recipient's active work, even when it names another lead.
                result["current_assignment"]["work_effect"] = "preserve_current"
        if message["kind"] == "instruction":
            refs, truncated = self._later_instruction_refs(conn, actor, message)
            result["later_instruction_refs"] = refs
            result["later_instructions_truncated"] = truncated
            result["ownership_transfer"] = bool(message["assignment_binding"])

    def _resume_checkpoint(
        self, conn: sqlite3.Connection, actor: str, active_work: dict[str, Any]
    ) -> dict[str, Any]:
        head = conn.execute(
            "SELECT m.id FROM messages m JOIN deliveries d ON d.message_id = m.id "
            "WHERE d.recipient = ? AND m.kind = 'instruction' "
            "AND d.state IN ('queued','leased') ORDER BY m.rowid DESC LIMIT 1", (actor,),
        ).fetchone()
        return {
            "state": "current", "ordering": "newest_instruction_first",
            "pending_instruction_head": head["id"] if head else None,
            "active_work_input": active_work,
            "active_assignment": self._assignment_checkpoint(conn, actor, active_work),
            "ownership_transfer_operation": "assignments.assign or assignments.reassign",
            "instruction_message_transfers_ownership": False,
            "before_action": "Reconcile current ownership and later instruction references before older work. "
                             "Later valid holds and corrections govern overlapping earlier instructions. "
                             "An authorized message alone does not transfer ownership.",
        }

    def _ensure_principal(self, conn: sqlite3.Connection, actor: str, field: str) -> None:
        if not self._principal_exists(conn, actor):
            raise HubError("unknown_agent", f"{field} is not a registered agent", 404)

    def bootstrap_owner(self, actor: str, approval_ref: str) -> dict[str, Any]:
        """Create the installation owner's local seed authority exactly once."""

        actor = self._validate_actor(actor)
        approval_ref = _text(approval_ref, "approval_ref", max_chars=1024) or ""
        with self._transaction(True) as conn:
            existing = self._meta(conn, "owner_actor")
            if existing is not None:
                if existing != actor:
                    raise HubError("already_bootstrapped", "owner authority is already initialized", 409)
                seed_id = self._meta(conn, "seed_grant_id")
                seed = conn.execute("SELECT * FROM grants WHERE id = ?", (seed_id,)).fetchone()
                if seed is None:
                    raise HubError("invalid_data", "owner seed grant is missing", 500)
                return {"owner": existing, "approval_ref": self._meta(conn, "approval_ref"), "grant": self._grant_dict(seed)}
            now = _iso_now()
            seed_id = _uuid()
            self._set_meta(conn, "owner_actor", actor)
            self._set_meta(conn, "approval_ref", approval_ref)
            self._set_meta(conn, "seed_grant_id", seed_id)
            conn.execute(
                "INSERT INTO grants(id, issuer, grantee, scope, actions_json, delegable, parent_grant_id, "
                "expires_at, revoked_at, reason, created_at) VALUES(?, ?, ?, '/', ?, 1, NULL, NULL, NULL, ?, ?)",
                (seed_id, actor, actor, _json_dump(["*"]), approval_ref, now),
            )
            seed = conn.execute("SELECT * FROM grants WHERE id = ?", (seed_id,)).fetchone()
            assert seed is not None
            return {"owner": actor, "approval_ref": approval_ref, "grant": self._grant_dict(seed)}

    def _op_agents_register(
        self, conn: sqlite3.Connection, actor: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        agent_id = _text(params.get("agent_id"), "agent_id", max_chars=256)
        runtime = _text(params.get("runtime"), "runtime", max_chars=256)
        machine = _text(params.get("machine"), "machine", max_chars=256)
        display_name = _text(params.get("display_name"), "display_name", required=False, max_chars=512)
        capabilities = params.get("capabilities", [])
        session_id = _native_identity(params.get("session_id"), "session_id")
        thread_id = _native_identity(params.get("thread_id"), "thread_id")
        try:
            capabilities_json = _json_dump(capabilities)
        except HubError:
            raise
        if len(capabilities_json.encode("utf-8")) > MAX_BODY_BYTES:
            raise HubError("request_too_large", "capabilities exceed 16 KiB", 413)
        assert agent_id is not None and runtime is not None and machine is not None
        existing = conn.execute("SELECT * FROM agents WHERE agent_id = ?", (agent_id,)).fetchone()
        if actor != agent_id and not self._is_owner(conn, actor):
            self._require_auth(conn, actor, "agents.register", "/")
        instance_id = _text(params.get("instance_id"), "instance_id", required=False, max_chars=256)
        if instance_id is None:
            instance_id = _uuid()
        same_instance = bool(
            existing is not None
            and instance_id == existing["instance_id"]
            and runtime == existing["runtime"]
            and machine == existing["machine"]
        )
        if existing is not None:
            # Re-registration is an instance update, not a logical-agent reset.
            # Descriptive fields survive it. Native bindings survive only an
            # exact same runtime instance on the same machine.
            if "display_name" not in params:
                display_name = existing["display_name"]
            if "capabilities" not in params:
                capabilities = _json_load(existing["capabilities_json"], [])
            capabilities_json = _json_dump(capabilities)
            if same_instance and params.get("session_id") is None:
                session_id = _native_identity(existing["session_id"], "session_id")
            if same_instance and params.get("thread_id") is None:
                thread_id = _native_identity(existing["thread_id"], "thread_id")
        now = _iso_now()
        status = "online"
        if existing is None:
            conn.execute(
                "INSERT INTO agents(agent_id, runtime, machine, display_name, capabilities_json, instance_id, "
                "session_id, thread_id, status, created_at, last_seen) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (agent_id, runtime, machine, display_name, capabilities_json, instance_id,
                 session_id, thread_id, status, now, now),
            )
        else:
            conn.execute(
                "UPDATE agents SET runtime = ?, machine = ?, display_name = ?, capabilities_json = ?, "
                "instance_id = ?, session_id = ?, thread_id = ?, status = ?, last_seen = ? WHERE agent_id = ?",
                (runtime, machine, display_name, capabilities_json, instance_id, session_id,
                 thread_id, status, now, agent_id),
            )
        row = conn.execute("SELECT * FROM agents WHERE agent_id = ?", (agent_id,)).fetchone()
        assert row is not None
        return {"agent": self._agent_dict(row)}

    def _op_agents_list(
        self, conn: sqlite3.Connection, actor: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        """Bounded registry read: newest registrations first, one page per call.

        The registry grows with every native child session, so an unbounded
        listing no longer fits a client response.  Exact optional filters keep
        recipient resolution cheap; the shared cursor contract pages the rest.
        """
        query = {
            field: _text(params.get(field), field, required=False, max_chars=256)
            for field in ("runtime", "machine", "status", "agent_id")
        }
        rows, context = self._list_page(conn, actor, "agents", params, query)
        output: list[dict[str, Any]] = []
        examined = 0
        for row in rows[:LIST_SCAN_LIMIT]:
            examined += 1
            if any(value is not None and row[field] != value for field, value in query.items()):
                continue
            output.append(self._agent_dict(row))
            if len(output) >= context["limit"]:
                break
        return {"agents": output, "page": self._list_page_metadata(rows, examined, len(output), context)}

    def fleet_ownership_snapshot(self) -> dict[str, Any]:
        """Return the minimal current registry state used by fleet projection.

        The Hub authorizes ``fleet.context`` before calling this internal read.
        Summaries, grants, messages, credentials, and historical assignments
        are intentionally outside this snapshot.
        """

        with self._transaction(write=False) as conn:
            agent_rows = conn.execute(
                "SELECT agent_id, runtime, machine, instance_id, session_id, thread_id "
                "FROM agents ORDER BY agent_id"
            ).fetchall()
            assignment_rows = conn.execute(
                "SELECT work_id, assignee, version, updated_at "
                "FROM assignments ORDER BY work_id"
            ).fetchall()
            agents = [
                {
                    "agent_id": row["agent_id"],
                    "runtime": row["runtime"],
                    "machine": row["machine"],
                    "instance_id": row["instance_id"],
                    "session_id": row["session_id"],
                    "thread_id": row["thread_id"],
                }
                for row in agent_rows
            ]
            assignments = [
                {
                    "work_id": row["work_id"],
                    "assignee": row["assignee"],
                    "version": int(row["version"]),
                    "updated_at": row["updated_at"],
                }
                for row in assignment_rows
            ]
        return {
            "schema": "inbox-fleet-ownership/v1",
            "coverage": {"agents": len(agents), "assignments": len(assignments)},
            "agents": agents,
            "assignments": assignments,
        }

    def _op_agents_heartbeat(
        self, conn: sqlite3.Connection, actor: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        row = conn.execute("SELECT * FROM agents WHERE agent_id = ?", (actor,)).fetchone()
        if row is None:
            raise HubError("unknown_agent", "actor is not a registered agent", 404)
        instance_id = _text(params.get("instance_id"), "instance_id", required=False, max_chars=256)
        if instance_id is not None and instance_id != row["instance_id"]:
            raise HubError("stale_instance", "heartbeat instance is no longer current", 409)
        status = params.get("status", row["status"])
        status = _text(status, "status", max_chars=64)
        assert status is not None
        now = _iso_now()
        conn.execute("UPDATE agents SET status = ?, last_seen = ? WHERE agent_id = ?", (status, now, actor))
        updated = conn.execute("SELECT * FROM agents WHERE agent_id = ?", (actor,)).fetchone()
        assert updated is not None
        return {"agent": self._agent_dict(updated)}

    def _op_messages_send(
        self, conn: sqlite3.Connection, actor: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        self._ensure_principal(conn, actor, "sender")
        kind = _text(params.get("kind"), "kind", max_chars=32)
        assert kind is not None
        if kind not in {"information", "instruction", "request", "result"}:
            raise HubError("invalid_input", "kind is not a supported message kind")
        subject = _text(params.get("subject"), "subject", max_chars=2048)
        body = _body(params.get("body"))
        scope = _canonical_scope(params.get("scope"))
        work_id = _text(params.get("work_id"), "work_id", required=False, max_chars=512)
        reply_to = _text(params.get("reply_to"), "reply_to", required=False, max_chars=256)
        artifacts = _string_list(params.get("artifacts", []), "artifacts", required=False)
        expires_at = _timestamp(params.get("expires_at"), "expires_at")
        route_owner = params.get("to_current_owner", False)
        if not isinstance(route_owner, bool):
            raise HubError("invalid_input", "to_current_owner must be a boolean")
        assignment = None
        if route_owner:
            if "to" in params or reply_to is not None or work_id is None:
                raise HubError("invalid_input", "current-owner routing requires work_id and forbids to/reply_to")
            version = params.get("expected_assignment_version")
            if isinstance(version, bool) or not isinstance(version, int) or version < 1:
                raise HubError("invalid_input", "expected_assignment_version must be a positive integer")
            assignment = conn.execute("SELECT * FROM assignments WHERE work_id = ?", (work_id,)).fetchone()
            if assignment is None:
                raise HubError("not_found", "assignment not found", 404)
            self._require_auth(conn, actor, "assignments.assign", assignment["scope"])
            if not _scope_contains(assignment["scope"], scope):
                raise HubError("invalid_input", "message scope must be within current assignment scope")
            if assignment["version"] != version:
                raise HubError("version_conflict", "assignment version does not match", 409)
            recipients = [assignment["assignee"]]
        else:
            if "expected_assignment_version" in params:
                raise HubError("invalid_input", "expected_assignment_version requires to_current_owner")
            recipients = _string_list(params.get("to"), "to", max_items=MAX_LIMIT)
        if len(set(recipients)) != len(recipients):
            raise HubError("invalid_input", "to must not contain duplicate recipients")
        for recipient in recipients:
            self._ensure_principal(conn, recipient, "recipient")
        supersedes = _string_list(params.get("supersedes", []), "supersedes", required=False,
                                  max_items=MAX_SUPERSEDES)
        if len(set(supersedes)) != len(supersedes):
            raise HubError("invalid_input", "supersedes must not contain duplicates")
        if supersedes and (kind not in {"information", "result"} or work_id is None or reply_to is not None):
            raise HubError("invalid_input", "supersession requires a new passive work message")
        if supersedes and _is_expired(expires_at, _iso_now()):
            raise HubError("invalid_input", "superseding message must not already be expired")
        for old_id in supersedes:
            old = conn.execute("SELECT * FROM messages WHERE id = ?", (old_id,)).fetchone()
            if (old is None or old["sender"] != actor or old["work_id"] != work_id
                    or old["scope"] != scope or old["kind"] not in {"information", "result"}
                    or old["assignment_binding"]):
                raise HubError("invalid_supersession", "message is not an eligible passive predecessor", 409)
            deliveries = conn.execute("SELECT * FROM deliveries WHERE message_id = ?", (old_id,)).fetchall()
            if (sorted(row["recipient"] for row in deliveries) != sorted(recipients)
                    or any(row["state"] != "queued" or row["attempts"] > 0 for row in deliveries)
                    or _is_expired(old["expires_at"], _iso_now())):
                raise HubError("invalid_supersession", "predecessor must have only never-delivered queued recipients", 409)
        requested_grant_id = params.get("authority_grant_id")
        if requested_grant_id is not None:
            requested_grant_id = _text(requested_grant_id, "authority_grant_id", max_chars=256)
        send_authority = self._select_authority(
            conn, actor, "messages.send", scope,
            requested_grant_id if kind != "instruction" else None,
        )
        authority: dict[str, Any] | None = None
        authority_grant_id: str | None = None
        if kind == "instruction":
            authority = self._select_authority(
                conn, actor, "instructions.issue", scope, requested_grant_id
            )
            authority_grant_id = authority["grant_ids"][0]
        elif requested_grant_id is not None:
            authority_grant_id = send_authority["grant_ids"][0]
            authority = send_authority
        result = self._insert_message(
            conn,
            sender=actor,
            recipients=recipients,
            kind=kind,
            subject=subject,
            body=body,
            scope=scope,
            work_id=work_id,
            reply_to=reply_to,
            artifacts=artifacts,
            expires_at=expires_at,
            authority_grant_id=authority_grant_id,
            authority=authority,
            assignment_version=assignment["version"] if assignment is not None else None,
            assignment_assignee=assignment["assignee"] if assignment is not None else None,
        )

        message_id = result["message"]["id"]
        if supersedes:
            conn.execute("UPDATE messages SET supersedes_json = ? WHERE id = ?", (_json_dump(supersedes), message_id))
            for old_id in supersedes:
                conn.execute("UPDATE deliveries SET state = 'superseded', superseded_by = ?, "
                             "rejection_reason = ? WHERE message_id = ?",
                             (message_id, "explicit passive supersession", old_id))
            result["message"]["supersedes"] = supersedes
        return result

    def _insert_message(
        self,
        conn: sqlite3.Connection,
        *,
        sender: str,
        recipients: list[str],
        kind: str,
        subject: str,
        body: str,
        scope: str,
        work_id: str | None,
        reply_to: str | None,
        artifacts: list[str],
        expires_at: str | None,
        authority_grant_id: str | None,
        authority: dict[str, Any] | None,
        assignment_version: int | None = None,
        assignment_assignee: str | None = None,
        assignment_binding: bool = False,
    ) -> dict[str, Any]:
        message_id = _uuid()
        created_at = _iso_now()
        conn.execute(
            "INSERT INTO messages(id, sender, kind, subject, body, scope, work_id, reply_to, artifacts_json, "
            "created_at, expires_at, authority_grant_id, authority_json, assignment_version, "
            "assignment_assignee, assignment_binding) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                message_id,
                sender,
                kind,
                subject,
                body,
                scope,
                work_id,
                reply_to,
                _json_dump(artifacts),
                created_at,
                expires_at,
                authority_grant_id,
                _json_dump(authority) if authority is not None else None,
                assignment_version,
                assignment_assignee,
                int(assignment_binding),
            ),
        )
        for recipient in recipients:
            conn.execute(
                "INSERT INTO deliveries(message_id, recipient, state, lease_id, lease_until, attempts, "
                "acknowledged_at, receipt_ref, rejection_reason) VALUES(?, ?, 'queued', NULL, NULL, 0, NULL, NULL, NULL)",
                (message_id, recipient),
            )
        message = conn.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
        assert message is not None
        result_message = self._message_output(conn, message, sender)
        result_deliveries = [
            self._delivery_dict(row)
            for row in conn.execute(
                "SELECT * FROM deliveries WHERE message_id = ? ORDER BY recipient", (message_id,)
            ).fetchall()
        ]
        return {"message": result_message, "deliveries": result_deliveries}

    def _op_messages_poll(
        self, conn: sqlite3.Connection, actor: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        self._ensure_principal(conn, actor, "actor")
        limit = self._limit(params.get("limit"), DEFAULT_POLL_LIMIT)
        reconcile = params.get("reconcile", False)
        if not isinstance(reconcile, bool):
            raise HubError("invalid_input", "reconcile must be a boolean")
        active_work = params.get("active_work") or {}
        if not isinstance(active_work, dict):
            raise HubError("invalid_input", "active_work must be an object")
        active_work = {key: active_work[key] for key in ("work_id", "assignment_version") if key in active_work}
        lease_seconds = params.get("lease_seconds", DEFAULT_LEASE_SECONDS)
        if (
            isinstance(lease_seconds, bool)
            or not isinstance(lease_seconds, (int, float))
            or lease_seconds <= 0
            or lease_seconds > MAX_LEASE_SECONDS
        ):
            raise HubError("invalid_input", "lease_seconds must be greater than zero and bounded")
        now_epoch = time.time()
        now_iso = _iso_now()
        conn.execute(
            "UPDATE deliveries SET state = 'queued', lease_id = NULL, lease_until = NULL "
            "WHERE recipient = ? AND state = 'leased' AND lease_until <= ?",
            (actor, now_epoch),
        )
        conn.execute(
            "UPDATE deliveries SET state = 'expired', lease_id = NULL, lease_until = NULL "
            "WHERE recipient = ? AND state IN ('queued', 'leased') AND EXISTS ("
            "SELECT 1 FROM messages m WHERE m.id = deliveries.message_id AND m.expires_at IS NOT NULL "
            "AND m.expires_at <= ?)",
            (actor, now_iso),
        )
        result: list[dict[str, Any]] = []
        # One bounded transaction, not a drain loop. Native reconciliation
        # may re-present this same actor's live instruction lease, never
        # renew it or manufacture a second claim. Passive first deliveries use
        # creation order; a retry rejoins at its last attempt time. Later arrivals
        # cannot indefinitely overtake a queued retry, nor can one retry monopolize
        # the one-item native poll while older unseen deliveries are waiting.
        candidate_rows = conn.execute(
            "SELECT m.id FROM messages m JOIN deliveries d ON d.message_id = m.id "
            "WHERE d.recipient = ? AND (d.state = 'queued' OR "
            "(? AND m.kind = 'instruction' AND d.state = 'leased')) "
            "ORDER BY CASE WHEN m.kind = 'instruction' THEN 0 ELSE 1 END, "
            "CASE WHEN m.kind = 'instruction' THEN m.rowid END DESC, "
            "COALESCE(d.last_attempt_at, m.created_at), m.created_at, m.id LIMIT ?",
            (actor, reconcile, MAX_LIMIT),
        ).fetchall()
        if limit:
            for candidate in candidate_rows:
                if len(result) >= limit:
                    break
                message_id = candidate["id"]
                message = conn.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
                if message is None:
                    continue
                authority = self._current_message_authority(conn, message)
                binding_mismatch = bool(message["assignment_binding"]) and (
                    message["assignment_assignee"] != actor
                )
                if message["kind"] == "instruction" and (
                    authority is None or not authority["allowed"] or binding_mismatch
                ):
                    reason = (authority or {}).get("reason", "assignment notice recipient is not current")
                    conn.execute(
                        "UPDATE deliveries SET state = 'rejected', rejection_reason = ?, lease_id = NULL, "
                        "lease_until = NULL WHERE message_id = ? AND recipient = ? AND state IN ('queued','leased')",
                        (reason, message_id, actor),
                    )
                    continue
                delivery = conn.execute(
                    "SELECT * FROM deliveries WHERE message_id = ? AND recipient = ?", (message_id, actor)
                ).fetchone()
                if reconcile and delivery is not None and delivery["state"] == "leased":
                    item = self._message_dict(message)
                    self._add_routing_context(conn, actor, message, item)
                    item["delivery"] = self._delivery_dict(delivery)
                    item["delivery"]["recipient_lifecycle"] = self._recipient_lifecycle(conn, actor)
                    item["authority"] = authority
                    item["delivery_represented"] = True
                    self._add_instruction_order_context(conn, actor, message, item)
                    result.append(item)
                    continue
                lease_id = _uuid()
                lease_until = now_epoch + float(lease_seconds)
                updated = conn.execute(
                    "UPDATE deliveries SET state = 'leased', lease_id = ?, lease_until = ?, "
                    "attempts = attempts + 1, last_attempt_at = ? "
                    "WHERE message_id = ? AND recipient = ? AND state = 'queued'",
                    (lease_id, lease_until, now_iso, message_id, actor),
                )
                if updated.rowcount != 1:
                    continue
                delivery = conn.execute(
                    "SELECT * FROM deliveries WHERE message_id = ? AND recipient = ?", (message_id, actor)
                ).fetchone()
                assert delivery is not None
                item = self._message_dict(message)
                self._add_routing_context(conn, actor, message, item)
                item["delivery"] = self._delivery_dict(delivery)
                item["delivery"]["recipient_lifecycle"] = self._recipient_lifecycle(conn, actor)
                item["authority"] = authority
                self._add_instruction_order_context(conn, actor, message, item)
                result.append(item)
        return {"messages": result, "resume_reconciliation": self._resume_checkpoint(conn, actor, active_work)}

    def _op_messages_ack(
        self, conn: sqlite3.Connection, actor: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        message_id = _text(params.get("message_id"), "message_id", max_chars=256)
        state = _text(params.get("state"), "state", max_chars=32)
        assert message_id is not None and state is not None
        if state not in {"acknowledged", "resolved"}:
            raise HubError("invalid_input", "state must be acknowledged or resolved")
        lease_id = _text(params.get("lease_id"), "lease_id", required=False, max_chars=256)
        receipt_ref = _text(params.get("receipt_ref"), "receipt_ref", required=False, max_chars=2048)
        work_outcome_ref = _text(params.get("work_outcome_ref"), "work_outcome_ref", required=False, max_chars=2048)
        if work_outcome_ref is not None and state != "resolved":
            raise HubError("invalid_input", "work_outcome_ref requires resolved state")
        delivery = conn.execute(
            "SELECT * FROM deliveries WHERE message_id = ? AND recipient = ?", (message_id, actor)
        ).fetchone()
        if delivery is None:
            raise HubError("not_found", "recipient delivery not found", 404)
        now_epoch = time.time()
        current_state = delivery["state"]
        if current_state == "leased" and lease_id is None:
            raise HubError("lease_required", "active delivery lease must be supplied", 409)
        if current_state != "leased" and lease_id is not None:
            raise HubError("stale_lease", "lease is stale or no longer owned", 409)
        if lease_id is not None:
            if (
                current_state != "leased"
                or delivery["lease_id"] != lease_id
                or delivery["lease_until"] is None
                or float(delivery["lease_until"]) <= now_epoch
            ):
                if current_state == "leased" and delivery["lease_until"] is not None and float(delivery["lease_until"]) <= now_epoch:
                    conn.execute(
                        "UPDATE deliveries SET state = 'queued', lease_id = NULL, lease_until = NULL "
                        "WHERE message_id = ? AND recipient = ? AND state = 'leased'",
                        (message_id, actor),
                    )
                raise HubError("stale_lease", "lease is stale or no longer owned", 409)
        if current_state == "resolved":
            if state == "resolved":
                return {"delivery": self._delivery_dict(delivery)}
            raise HubError("invalid_transition", "resolved delivery cannot move backward", 409)
        if current_state in {"expired", "rejected", "superseded"}:
            raise HubError("invalid_transition", "delivery is no longer actionable", 409)
        message = conn.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
        if message is None:
            raise HubError("not_found", "message not found", 404)
        if message["kind"] == "instruction" and current_state in {"queued", "leased"}:
            later, truncated = self._later_instruction_refs(conn, actor, message, pending_only=True)
            if later or truncated:
                raise HubError(
                    "instruction_reconciliation_required",
                    "incorporate newer pending instructions before acknowledging older work; "
                    "use a fresh checkpoint or messages.poll with reconcile=true", 409,
                )
        if state == "resolved" and message["kind"] == "instruction":
            authority = self._current_message_authority(conn, message)
            binding_mismatch = bool(message["assignment_binding"]) and (
                message["assignment_assignee"] != actor
            )
            if authority is None or not authority["allowed"] or binding_mismatch:
                reason = (authority or {}).get("reason", "instruction authority is no longer valid")
                conn.execute(
                    "UPDATE deliveries SET state = 'rejected', lease_id = NULL, lease_until = NULL, "
                    "rejection_reason = ? WHERE message_id = ? AND recipient = ?",
                    (reason, message_id, actor),
                )
                error = HubError(
                    "authority_revoked",
                    f"instruction authority is no longer valid: {reason}",
                    409,
                )
                error._commit_on_error = True
                raise error
        now = _iso_now()
        conn.execute(
            "UPDATE deliveries SET state = ?, lease_id = NULL, lease_until = NULL, acknowledged_at = ?, "
            "first_acknowledged_at = CASE WHEN acknowledgment_time_uncertain = 0 "
            "THEN COALESCE(first_acknowledged_at, ?) ELSE first_acknowledged_at END, "
            "resolved_at = CASE WHEN ? = 'resolved' THEN COALESCE(resolved_at, ?) ELSE resolved_at END, "
            "work_outcome_ref = COALESCE(?, work_outcome_ref), "
            "receipt_ref = ? WHERE message_id = ? AND recipient = ?",
            (state, now, now, state, now, work_outcome_ref, receipt_ref, message_id, actor),
        )
        updated = conn.execute(
            "SELECT * FROM deliveries WHERE message_id = ? AND recipient = ?", (message_id, actor)
        ).fetchone()
        assert updated is not None
        return {"delivery": self._delivery_dict(updated)}

    def _message_access(self, conn: sqlite3.Connection, actor: str, message: sqlite3.Row) -> bool:
        if actor == message["sender"]:
            return True
        direct = conn.execute(
            "SELECT 1 FROM deliveries WHERE message_id = ? AND recipient = ?",
            (message["id"], actor),
        ).fetchone()
        if direct is not None:
            return True
        return self._authorize_conn(conn, actor, "messages.read", message["scope"])["allowed"] or self._authorize_conn(
            conn, actor, "owner.read", message["scope"]
        )["allowed"]

    def _list_page(self, conn: sqlite3.Connection, actor: str, table: str,
                   params: dict[str, Any], query: dict[str, Any]) -> tuple[list[sqlite3.Row], dict[str, Any]]:
        """Bound candidate scanning; never treat authorization filtering as exhaustion.

        Rowids are immutable creation sequences under supported Store operations.
        Unlike timestamps, they neither move on updates nor admit later backdated
        inserts. Offline rowid-rewriting maintenance must invalidate cursor keys.
        """
        limit = self._limit(params.get("limit"), MAX_LIMIT)
        binding = {"actor": actor, "table": table, "query": query, "limit": limit, "v": 1}
        key = self._meta(conn, "list_cursor_key")
        assert key is not None
        token = params.get("cursor")
        after = None
        watermark = conn.execute(f"SELECT COALESCE(MAX(rowid), 0) FROM {table}").fetchone()[0]
        if token is not None:
            try:
                if not isinstance(token, str) or len(token) > 8192:
                    raise ValueError()
                encoded, signature = token.split(".")
                if not hmac.compare_digest(signature, hmac.new(key.encode(), encoded.encode(), hashlib.sha256).hexdigest()):
                    raise ValueError()
                payload = json.loads(base64.urlsafe_b64decode(encoded.encode()))
                if payload["binding"] != binding:
                    raise ValueError()
                watermark, after = payload["watermark"], payload["after"]
                if (type(watermark) is not int or type(after) is not int
                        or not 0 < after <= watermark):
                    raise ValueError()
            except (ValueError, KeyError, TypeError, UnicodeError):
                raise HubError("invalid_cursor", "cursor is invalid or belongs to a different actor/query", 409) from None
        where, args = ["rowid <= ?"], [watermark]
        if after is not None:
            where.append("rowid < ?")
            args.append(after)
        # Assignment work_id is unique. Message work filters stay in the bounded
        # candidate scan so an absent work_id cannot cause an unbounded SQL scan.
        if table == "assignments" and query.get("work_id") is not None:
            where.append("work_id = ?")
            args.append(query["work_id"])
        # Agent IDs are unique too; an exact lookup must not depend on the
        # candidate window, or an old registration reads as an empty page.
        if table == "agents" and query.get("agent_id") is not None:
            where.append("agent_id = ?")
            args.append(query["agent_id"])
        rows = conn.execute(f"SELECT rowid AS creation_sequence, * FROM {table} WHERE {' AND '.join(where)} "
                            "ORDER BY rowid DESC LIMIT ?",
                            (*args, LIST_SCAN_LIMIT + 1)).fetchall()
        return rows, {"binding": binding, "watermark": watermark, "key": key,
                      "limit": limit}

    @staticmethod
    def _list_page_metadata(rows: list[sqlite3.Row], examined: int, returned: int,
                            context: dict[str, Any]) -> dict[str, Any]:
        more = examined < len(rows)
        token = None
        if more:
            last = rows[examined - 1]
            payload = {"binding": context["binding"], "watermark": context["watermark"],
                       "after": last["creation_sequence"]}
            encoded = base64.urlsafe_b64encode(_json_dump(payload).encode()).decode()
            signature = hmac.new(context["key"].encode(), encoded.encode(), hashlib.sha256).hexdigest()
            token = encoded + "." + signature
        return {"next_cursor": token, "has_more": more, "complete": not more,
                "returned": returned, "limit": context["limit"],
                "consistency": "creation_watermark_live_updates_and_authorization",
                "ordering": "creation_sequence_desc",
                "coverage": {"scope": "authorized_rows_within_creation_watermark",
                             "scanned": examined, "scan_limit": LIST_SCAN_LIMIT,
                             "scan_complete": not more, "eligible_total": None,
                             "has_more_semantics": "unscanned_candidates_may_be_ineligible"}}

    def _op_messages_list(
        self, conn: sqlite3.Connection, actor: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        if params.get("view") is not None:
            if params["view"] != "pending_metadata":
                raise HubError("invalid_input", "unsupported messages list view")
            return self._pending_metadata(conn, actor, params)
        limit = self._limit(params.get("limit"), MAX_LIMIT)
        state = params.get("state")
        if state is not None:
            state = _text(state, "state", max_chars=32)
            assert state is not None
            if state not in {
                "queued",
                "leased",
                "acknowledged",
                "resolved",
                "expired",
                "rejected",
                "superseded",
            }:
                raise HubError("invalid_input", "unsupported message state")
        work_id = _text(params.get("work_id"), "work_id", required=False, max_chars=512)
        scope = _canonical_scope(params["scope"]) if params.get("scope") is not None else None
        rows, context = self._list_page(conn, actor, "messages", params,
                                        {"state": state, "work_id": work_id, "scope": scope})
        output: list[dict[str, Any]] = []
        examined = 0
        for message in rows[:LIST_SCAN_LIMIT]:
            examined += 1
            if work_id is not None and message["work_id"] != work_id:
                continue
            if scope is not None and not _scope_contains(scope, message["scope"]):
                continue
            if not self._message_access(conn, actor, message):
                continue
            item = self._message_output(conn, message, actor)
            if state is not None and not any(row["state"] == state for row in item["deliveries"]):
                continue
            output.append(item)
            if len(output) >= limit:
                break
        return {"messages": output, "page": self._list_page_metadata(rows, examined, len(output), context)}

    def _pending_metadata(
        self, conn: sqlite3.Connection, actor: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        """A bounded own-recipient snapshot; never lease or reconcile messages."""
        self._ensure_principal(conn, actor, "actor")
        if set(params) - {"view", "limit"}:
            raise HubError("invalid_input", "pending metadata accepts only view and limit")
        limit = self._limit(params.get("limit"), DEFAULT_POLL_LIMIT)
        now_epoch = time.time()
        now_iso = _iso_now()
        base = {"schema": "inbox-pending/v1", "recipient": actor,
                "generated_at": now_iso, "read_only": True,
                "authority_semantics": "metadata_only_native_reconciliation_required"}
        steps = 0
        def budget() -> int:
            nonlocal steps
            steps += 1000
            return int(steps >= PENDING_QUERY_STEPS)
        conn.set_progress_handler(budget, 1000)
        try:
            rows = conn.execute(
                "SELECT m.id, m.kind, m.created_at, d.state, d.lease_until, d.attempts "
                "FROM deliveries d JOIN messages m ON m.id = d.message_id "
                "WHERE d.recipient = ? AND d.state IN ('queued', 'leased') "
                "AND (m.expires_at IS NULL OR m.expires_at > ?) "
                "ORDER BY m.id LIMIT ?",
                (actor, now_iso, PENDING_SCAN_LIMIT + 1),
            ).fetchall()
        except sqlite3.OperationalError as exc:
            if str(exc) != "interrupted":
                raise
            return {**base, "state": "UNKNOWN", "reason": "query_work_bound",
                    "counts": None, "counts_lower_bound": None, "digest": None,
                    "oldest_pending_at": None, "oldest_ready_at": None,
                    "messages": [], "coverage": {"complete": False, "returned": 0,
                        "scan_limit": PENDING_SCAN_LIMIT, "scanned_pending": None,
                        "ids_truncated": None}}
        finally:
            conn.set_progress_handler(None, 0)
        complete = len(rows) <= PENDING_SCAN_LIMIT
        rows = rows[:PENDING_SCAN_LIMIT]
        counts = {"pending": len(rows), "ready": 0, "live_leases": 0,
                  "instructions": 0, "ready_instructions": 0, "unknown_leases": 0}
        messages = []
        for row in rows:
            lease = row["lease_until"]
            state = ("queued" if row["state"] == "queued" else
                     "unknown_lease" if lease is None else
                     "expired_lease" if lease <= now_epoch else "live_lease")
            ready = state in {"queued", "expired_lease"}
            counts["ready"] += int(ready)
            counts["live_leases"] += int(state == "live_lease")
            counts["unknown_leases"] += int(state == "unknown_lease")
            counts["instructions"] += int(row["kind"] == "instruction")
            counts["ready_instructions"] += int(ready and row["kind"] == "instruction")
            messages.append({"id": row["id"], "kind": row["kind"],
                             "created_at": row["created_at"], "effective_state": state,
                             "attempts": row["attempts"], "lease_until": _epoch_to_iso(lease)})
        # SQL id order makes the full-set digest independent of the requested
        # display limit. The observation clock and growing ages are excluded.
        digest = hashlib.sha256(_json_dump(messages).encode("utf-8")).hexdigest() if complete else None
        oldest = min((m["created_at"] for m in messages), default=None)
        oldest_ready = min((m["created_at"] for m in messages
                            if m["effective_state"] in {"queued", "expired_lease"}), default=None)
        messages.sort(key=lambda m: (m["created_at"], m["id"]))
        return {**base, "state": "OK" if complete else "PARTIAL",
                "reason": None if complete else "recipient_pending_scan_bound",
                "counts": counts if complete else None,
                "counts_lower_bound": counts, "digest": digest,
                "oldest_pending_at": oldest if complete else None,
                "oldest_ready_at": oldest_ready if complete else None,
                "messages": messages[:limit],
                "coverage": {"complete": complete, "returned": min(limit, len(messages)),
                    "scan_limit": PENDING_SCAN_LIMIT, "scanned_pending": len(rows),
                    "ids_truncated": not complete or len(messages) > limit}}

    def _op_messages_get(
        self, conn: sqlite3.Connection, actor: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        message_id = _text(params.get("message_id"), "message_id", max_chars=256)
        assert message_id is not None
        message = conn.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
        if message is None:
            raise HubError("not_found", "message not found", 404)
        if not self._message_access(conn, actor, message):
            raise HubError("forbidden", "actor cannot access this message", 403)
        return {"message": self._message_output(conn, message, actor)}

    def _op_discoveries_publish(
        self, conn: sqlite3.Connection, actor: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        self._ensure_principal(conn, actor, "publisher")
        title = _text(params.get("title"), "title", max_chars=2048)
        body = _body(params.get("body"))
        scope = _canonical_scope(params.get("scope"))
        topics = _string_list(params.get("topics", []), "topics", required=False)
        artifacts = _string_list(params.get("artifacts", []), "artifacts", required=False)
        work_id = _text(params.get("work_id"), "work_id", required=False, max_chars=512)
        expires_at = _timestamp(params.get("expires_at"), "expires_at")
        self._require_auth(conn, actor, "discoveries.publish", scope)
        discovery_id = _uuid()
        now = _iso_now()
        conn.execute(
            "INSERT INTO discoveries(id, publisher, title, body, scope, topics_json, artifacts_json, work_id, "
            "expires_at, created_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                discovery_id,
                actor,
                title,
                body,
                scope,
                _json_dump(topics),
                _json_dump(artifacts),
                work_id,
                expires_at,
                now,
            ),
        )
        row = conn.execute("SELECT * FROM discoveries WHERE id = ?", (discovery_id,)).fetchone()
        assert row is not None
        return {"discovery": self._discovery_dict(row)}

    def _discovery_access(self, conn: sqlite3.Connection, actor: str, scope: str, publisher: str) -> bool:
        return actor == publisher or self._authorize_conn(conn, actor, "discoveries.search", scope)["allowed"] or self._authorize_conn(
            conn, actor, "discoveries.read", scope
        )["allowed"]

    def _op_discoveries_search(
        self, conn: sqlite3.Connection, actor: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        query = params.get("query")
        if query is not None:
            query = _text(query, "query", required=False, max_chars=2048)
            query_lower = (query or "").casefold()
        else:
            query_lower = ""
        requested_scope = params.get("scope")
        if requested_scope is not None:
            requested_scope = _canonical_scope(requested_scope)
        topics = [topic.casefold() for topic in _string_list(params.get("topics", []), "topics", required=False)]
        limit = self._limit(params.get("limit"), MAX_LIMIT)
        now = _iso_now()
        output: list[dict[str, Any]] = []
        cursor: tuple[str, str] | None = None
        while len(output) < limit:
            if cursor is None:
                rows = conn.execute(
                    "SELECT * FROM discoveries WHERE expires_at IS NULL OR expires_at > ? "
                    "ORDER BY created_at DESC, id DESC LIMIT ?",
                    (now, MAX_LIMIT),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM discoveries WHERE (expires_at IS NULL OR expires_at > ?) AND "
                    "(created_at < ? OR (created_at = ? AND id < ?)) "
                    "ORDER BY created_at DESC, id DESC LIMIT ?",
                    (now, cursor[0], cursor[0], cursor[1], MAX_LIMIT),
                ).fetchall()
            if not rows:
                break
            cursor = (rows[-1]["created_at"], rows[-1]["id"])
            for row in rows:
                scope = row["scope"]
                if requested_scope is not None and not _scope_contains(requested_scope, scope):
                    continue
                row_topics = [str(item).casefold() for item in _json_load(row["topics_json"], [])]
                if topics and not all(topic in row_topics for topic in topics):
                    continue
                if query_lower:
                    haystack = " ".join(
                        [row["title"], row["body"], row["scope"], row["work_id"] or "", *row_topics]
                    ).casefold()
                    if query_lower not in haystack:
                        continue
                if not self._discovery_access(conn, actor, scope, row["publisher"]):
                    continue
                output.append(self._discovery_dict(row))
                if len(output) >= limit:
                    break
            if len(rows) < MAX_LIMIT:
                break
        return {"discoveries": output}

    def _grant_visible(self, conn: sqlite3.Connection, actor: str, row: sqlite3.Row) -> bool:
        if self._is_owner(conn, actor) or actor in {row["issuer"], row["grantee"]}:
            return True
        return self._authorize_conn(conn, actor, "grants.read", row["scope"])["allowed"]

    def _op_grants_issue(
        self, conn: sqlite3.Connection, actor: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        grantee = _text(params.get("grantee"), "grantee", max_chars=256)
        scope = _canonical_scope(params.get("scope"))
        actions = _actions(params.get("actions"))
        delegable = params.get("delegable")
        if not isinstance(delegable, bool):
            raise HubError("invalid_input", "delegable must be boolean")
        parent_id = params.get("parent_grant_id")
        if parent_id is not None:
            parent_id = _text(parent_id, "parent_grant_id", max_chars=256)
        expires_at = _timestamp(params.get("expires_at"), "expires_at")
        reason = _text(params.get("reason"), "reason", required=False, max_chars=2048)
        assert grantee is not None
        self._ensure_principal(conn, grantee, "grantee")
        now_iso = _iso_now()
        parent: sqlite3.Row | None = None
        if parent_id is not None:
            parent = conn.execute("SELECT * FROM grants WHERE id = ?", (parent_id,)).fetchone()
            if parent is None or parent["grantee"] != actor:
                raise HubError("forbidden", "parent grant is not held by issuer", 403)
            chain = self._grant_chain(conn, parent["id"], now_iso)
            if chain is None:
                raise HubError("forbidden", "parent grant is not active", 403)
            parent_scope = _canonical_scope(parent["scope"])
            parent_actions = _actions(_json_load(parent["actions_json"], []))
            if not bool(parent["delegable"]):
                raise HubError("forbidden", "parent grant is not delegable", 403)
            if not _scope_contains(parent_scope, scope) or not _action_allowed(parent_actions, "grants.issue"):
                raise HubError("forbidden", "parent grant cannot issue this child grant", 403)
            if not _actions_subset(actions, parent_actions):
                raise HubError("forbidden", "child actions exceed parent authority", 403)
        else:
            candidates = conn.execute(
                "SELECT * FROM grants WHERE grantee = ? ORDER BY created_at, id", (actor,)
            ).fetchall()
            for candidate in candidates:
                chain = self._grant_chain(conn, candidate["id"], now_iso)
                if chain is None:
                    continue
                candidate_scope = _canonical_scope(candidate["scope"])
                candidate_actions = _actions(_json_load(candidate["actions_json"], []))
                if (
                    bool(candidate["delegable"])
                    and _scope_contains(candidate_scope, scope)
                    and _action_allowed(candidate_actions, "grants.issue")
                    and _actions_subset(actions, candidate_actions)
                ):
                    parent = candidate
                    parent_id = candidate["id"]
                    break
            if parent is None:
                raise HubError("forbidden", "issuer has no delegable grant for this child", 403)
        assert parent is not None and parent_id is not None
        parent_chain = self._grant_chain(conn, parent_id, now_iso)
        if parent_chain is None:
            raise HubError("forbidden", "parent grant is not active", 403)
        if len(parent_chain) >= MAX_DELEGATION_DEPTH:
            raise HubError("delegation_depth", "delegation chain exceeds the supported depth", 409)
        if expires_at is not None and expires_at <= now_iso:
            raise HubError("invalid_input", "expires_at must be in the future")
        if parent["expires_at"] is not None and (expires_at is None or expires_at > parent["expires_at"]):
            raise HubError("forbidden", "child grant cannot outlive its parent", 403)
        grant_id = _uuid()
        now = _iso_now()
        conn.execute(
            "INSERT INTO grants(id, issuer, grantee, scope, actions_json, delegable, parent_grant_id, expires_at, "
            "revoked_at, reason, created_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)",
            (
                grant_id,
                actor,
                grantee,
                scope,
                _json_dump(actions),
                int(delegable),
                parent_id,
                expires_at,
                reason,
                now,
            ),
        )
        row = conn.execute("SELECT * FROM grants WHERE id = ?", (grant_id,)).fetchone()
        assert row is not None
        return {"grant": self._grant_dict(row)}

    def _can_revoke(self, conn: sqlite3.Connection, actor: str, row: sqlite3.Row) -> bool:
        seed_id = self._meta(conn, "seed_grant_id")
        if row["id"] == seed_id:
            return False
        if self._is_owner(conn, actor) or row["issuer"] == actor:
            return True
        if self._authorize_conn(conn, actor, "grants.revoke", row["scope"])["allowed"]:
            return True
        chain = self._grant_chain(conn, row["id"], _iso_now())
        return bool(chain and any(item["issuer"] == actor for item in chain))

    def _op_grants_revoke(
        self, conn: sqlite3.Connection, actor: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        grant_id = _text(params.get("grant_id"), "grant_id", max_chars=256)
        reason = _text(params.get("reason"), "reason", required=False, max_chars=2048)
        assert grant_id is not None
        row = conn.execute("SELECT * FROM grants WHERE id = ?", (grant_id,)).fetchone()
        if row is None:
            raise HubError("not_found", "grant not found", 404)
        if not self._can_revoke(conn, actor, row):
            raise HubError("forbidden", "actor cannot revoke this grant", 403)
        if row["revoked_at"] is None:
            conn.execute(
                "UPDATE grants SET revoked_at = ?, reason = COALESCE(?, reason) WHERE id = ?",
                (_iso_now(), reason, grant_id),
            )
        updated = conn.execute("SELECT * FROM grants WHERE id = ?", (grant_id,)).fetchone()
        assert updated is not None
        return {"grant": self._grant_dict(updated)}

    def _op_grants_list(
        self, conn: sqlite3.Connection, actor: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        agent_id = params.get("agent_id")
        if agent_id is not None:
            agent_id = _text(agent_id, "agent_id", max_chars=256)
        rows = conn.execute("SELECT * FROM grants ORDER BY created_at, id").fetchall()
        output: list[dict[str, Any]] = []
        for row in rows:
            if agent_id is not None and row["grantee"] != agent_id and row["issuer"] != agent_id:
                continue
            if not self._grant_visible(conn, actor, row):
                continue
            output.append(self._grant_dict(row))
            if len(output) >= MAX_LIMIT:
                break
        if agent_id is not None and agent_id != actor and not output and not self._is_owner(conn, actor):
            # An empty result for an unknown target is not allowed to act as a disclosure oracle.
            if conn.execute("SELECT 1 FROM agents WHERE agent_id = ?", (agent_id,)).fetchone() is None:
                raise HubError("not_found", "agent not found", 404)
        return {"grants": output}

    def _op_grants_get(
        self, conn: sqlite3.Connection, actor: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        grant_id = _text(params.get("grant_id"), "grant_id", max_chars=256)
        assert grant_id is not None
        row = conn.execute("SELECT * FROM grants WHERE id = ?", (grant_id,)).fetchone()
        if row is None:
            raise HubError("not_found", "grant not found", 404)
        if not self._grant_visible(conn, actor, row):
            raise HubError("forbidden", "actor cannot access this grant", 403)
        return {"grant": self._grant_dict(row)}

    def _assignment_notice_grants(
        self, conn: sqlite3.Connection, actor: str, scope: str, assignment_grant_id: str | None
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        send_auth = self._select_authority(conn, actor, "messages.send", scope)
        instruction_auth = self._select_authority(conn, actor, "instructions.issue", scope)
        if assignment_grant_id is not None:
            # The assignment grant is independently checked by the operation caller;
            # notice authority remains explicit and may be a narrower sibling grant.
            _ = assignment_grant_id
        return send_auth, instruction_auth

    def _insert_assignment_notice(
        self,
        conn: sqlite3.Connection,
        *,
        actor: str,
        recipient: str,
        work_id: str,
        scope: str,
        summary: str,
        version: int,
        event: str,
        instruction_authority: dict[str, Any],
    ) -> dict[str, Any]:
        subject = f"Assignment {event}: {work_id}"
        body = f"Binding assignment for {work_id}\nScope: {scope}\nVersion: {version}\nSummary: {summary}"
        return self._insert_message(
            conn,
            sender=actor,
            recipients=[recipient],
            kind="instruction",
            subject=subject,
            body=body,
            scope=scope,
            work_id=work_id,
            reply_to=None,
            artifacts=[],
            expires_at=None,
            authority_grant_id=instruction_authority["grant_ids"][0],
            authority=instruction_authority,
            assignment_version=version,
            assignment_assignee=recipient,
            assignment_binding=True,
        )

    def _insert_assignment_change_notice(
        self,
        conn: sqlite3.Connection,
        *,
        actor: str,
        recipient: str,
        work_id: str,
        scope: str,
        summary: str,
        version: int,
        send_authority: dict[str, Any],
    ) -> dict[str, Any]:
        return self._insert_message(
            conn,
            sender=actor,
            recipients=[recipient],
            kind="information",
            subject=f"Assignment ownership changed: {work_id}",
            body=f"Ownership changed for {work_id}\nScope: {scope}\nVersion: {version}\nSummary: {summary}",
            scope=scope,
            work_id=work_id,
            reply_to=None,
            artifacts=[],
            expires_at=None,
            authority_grant_id=send_authority["grant_ids"][0],
            authority=send_authority,
            assignment_version=version,
            assignment_assignee=recipient,
        )

    @staticmethod
    def _supersede_assignment_notices(
        conn: sqlite3.Connection, work_id: str, current_version: int
    ) -> None:
        conn.execute(
            "UPDATE deliveries SET state = 'superseded', lease_id = NULL, lease_until = NULL, "
            "rejection_reason = ? WHERE state IN ('queued', 'leased') AND message_id IN ("
            "SELECT id FROM messages WHERE work_id = ? AND assignment_binding = 1 "
            "AND assignment_version < ?)",
            (
                f"superseded by assignment version {current_version}",
                work_id,
                current_version,
            ),
        )

    def _op_assignments_assign(
        self, conn: sqlite3.Connection, actor: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        work_id = _text(params.get("work_id"), "work_id", max_chars=512)
        assignee = _text(params.get("assignee"), "assignee", max_chars=256)
        scope = _canonical_scope(params.get("scope"))
        summary = _body(params.get("summary"), "summary")
        requested_grant = params.get("grant_id")
        if requested_grant is not None:
            requested_grant = _text(requested_grant, "grant_id", max_chars=256)
        assert work_id is not None and assignee is not None
        self._ensure_principal(conn, assignee, "assignee")
        assignment_auth = self._select_authority(conn, actor, "assignments.assign", scope, requested_grant)
        send_auth, instruction_auth = self._assignment_notice_grants(conn, actor, scope, requested_grant)
        existing = conn.execute("SELECT 1 FROM assignments WHERE work_id = ?", (work_id,)).fetchone()
        if existing is not None:
            raise HubError("already_assigned", "work_id already has an assignment", 409)
        now = _iso_now()
        grant_id = assignment_auth["grant_ids"][0]
        conn.execute(
            "INSERT INTO assignments(work_id, assignee, scope, summary, grant_id, version, assigned_by, created_at, updated_at) "
            "VALUES(?, ?, ?, ?, ?, 1, ?, ?, ?)",
            (work_id, assignee, scope, summary, grant_id, actor, now, now),
        )
        conn.execute(
            "INSERT INTO assignment_history(id, work_id, version, prior_owner, new_owner, changed_by, scope, summary, "
            "grant_id, event, created_at) VALUES(?, ?, 1, NULL, ?, ?, ?, ?, ?, 'assigned', ?)",
            (_uuid(), work_id, assignee, actor, scope, summary, grant_id, now),
        )
        notice = self._insert_assignment_notice(
            conn,
            actor=actor,
            recipient=assignee,
            work_id=work_id,
            scope=scope,
            summary=summary,
            version=1,
            event="assigned",
            instruction_authority=instruction_auth,
        )
        row = conn.execute("SELECT * FROM assignments WHERE work_id = ?", (work_id,)).fetchone()
        assert row is not None
        result = self._assignment_dict(row)
        result["history"] = self._assignment_history(conn, work_id)
        result["notice_message_ids"] = [notice["message"]["id"]]
        result["notice_send_grant_id"] = send_auth["grant_ids"][0]
        return {"assignment": result}

    def _op_assignments_reassign(
        self, conn: sqlite3.Connection, actor: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        work_id = _text(params.get("work_id"), "work_id", max_chars=512)
        assignee = _text(params.get("assignee"), "assignee", max_chars=256)
        scope = _canonical_scope(params.get("scope"))
        summary = _body(params.get("summary"), "summary")
        expected_version = params.get("expected_version")
        requested_grant = params.get("grant_id")
        if requested_grant is not None:
            requested_grant = _text(requested_grant, "grant_id", max_chars=256)
        if isinstance(expected_version, bool) or not isinstance(expected_version, int) or expected_version < 1:
            raise HubError("invalid_input", "expected_version must be a positive integer")
        assert work_id is not None and assignee is not None
        self._ensure_principal(conn, assignee, "assignee")
        row = conn.execute("SELECT * FROM assignments WHERE work_id = ?", (work_id,)).fetchone()
        if row is None:
            raise HubError("not_found", "assignment not found", 404)
        if int(row["version"]) != expected_version:
            raise HubError("version_conflict", "assignment version does not match", 409)
        old_scope = row["scope"]
        old_assignment_auth = self._select_authority(
            conn, actor, "assignments.reassign", old_scope, requested_grant
        )
        old_send_auth, old_instruction_auth = self._assignment_notice_grants(
            conn, actor, old_scope, requested_grant
        )
        if old_scope == scope:
            assignment_auth = old_assignment_auth
            send_auth = old_send_auth
            instruction_auth = old_instruction_auth
        else:
            # A reassignment can change the work boundary, but it may not use
            # authority from only the new sibling scope. Both scopes are live
            # authorization boundaries for this mutation and its notices.
            assignment_auth = self._select_authority(
                conn, actor, "assignments.reassign", scope, requested_grant
            )
            send_auth, instruction_auth = self._assignment_notice_grants(
                conn, actor, scope, requested_grant
            )
        prior_owner = row["assignee"]
        next_version = expected_version + 1
        now = _iso_now()
        grant_id = assignment_auth["grant_ids"][0]
        conn.execute(
            "UPDATE assignments SET assignee = ?, scope = ?, summary = ?, grant_id = ?, version = ?, assigned_by = ?, "
            "updated_at = ? WHERE work_id = ? AND version = ?",
            (assignee, scope, summary, grant_id, next_version, actor, now, work_id, expected_version),
        )
        conn.execute(
            "INSERT INTO assignment_history(id, work_id, version, prior_owner, new_owner, changed_by, scope, summary, "
            "grant_id, event, created_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, 'reassigned', ?)",
            (_uuid(), work_id, next_version, prior_owner, assignee, actor, scope, summary, grant_id, now),
        )
        self._supersede_assignment_notices(conn, work_id, next_version)
        notice_ids: list[str] = []
        notice = self._insert_assignment_notice(
            conn,
            actor=actor,
            recipient=assignee,
            work_id=work_id,
            scope=scope,
            summary=summary,
            version=next_version,
            event="reassigned",
            instruction_authority=instruction_auth,
        )
        notice_ids.append(notice["message"]["id"])
        if prior_owner != assignee and self._principal_exists(conn, prior_owner):
            old_notice = self._insert_assignment_change_notice(
                conn,
                actor=actor,
                recipient=prior_owner,
                work_id=work_id,
                scope=scope,
                summary=summary,
                version=next_version,
                send_authority=send_auth,
            )
            notice_ids.append(old_notice["message"]["id"])
        updated = conn.execute("SELECT * FROM assignments WHERE work_id = ?", (work_id,)).fetchone()
        assert updated is not None
        result = self._assignment_dict(updated)
        result["history"] = self._assignment_history(conn, work_id)
        result["notice_message_ids"] = notice_ids
        result["notice_send_grant_id"] = send_auth["grant_ids"][0]
        return {"assignment": result}

    @staticmethod
    def _assignment_history(conn: sqlite3.Connection, work_id: str) -> list[dict[str, Any]]:
        rows = conn.execute(
            "SELECT * FROM assignment_history WHERE work_id = ? ORDER BY version, created_at, id", (work_id,)
        ).fetchall()
        return [
            {
                "id": row["id"],
                "work_id": row["work_id"],
                "version": int(row["version"]),
                "prior_owner": row["prior_owner"],
                "new_owner": row["new_owner"],
                "changed_by": row["changed_by"],
                "scope": row["scope"],
                "summary": row["summary"],
                "grant_id": row["grant_id"],
                "event": row["event"],
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def _assignment_visible(self, conn: sqlite3.Connection, actor: str, row: sqlite3.Row) -> bool:
        return (actor == row["assignee"] or self._is_owner(conn, actor)
                or self._authorize_conn(conn, actor, "assignments.read", row["scope"])["allowed"]
                or self._authorize_conn(conn, actor, "assignments.assign", row["scope"])["allowed"])

    def _op_assignments_list(
        self, conn: sqlite3.Connection, actor: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        requested_work_id = _text(params.get("work_id"), "work_id", required=False, max_chars=512)
        requested_scope = params.get("scope")
        if requested_scope is not None:
            requested_scope = _canonical_scope(requested_scope)
        rows, context = self._list_page(conn, actor, "assignments", params,
                                        {"work_id": requested_work_id, "scope": requested_scope})
        output: list[dict[str, Any]] = []
        examined = 0
        for row in rows[:LIST_SCAN_LIMIT]:
            examined += 1
            if requested_scope is not None and not _scope_contains(requested_scope, row["scope"]):
                continue
            if not self._assignment_visible(conn, actor, row):
                continue
            item = self._assignment_dict(row)
            item["history"] = self._assignment_history(conn, row["work_id"])
            output.append(item)
            if len(output) >= context["limit"]:
                break
        return {"assignments": output, "page": self._list_page_metadata(rows, examined, len(output), context)}

    def _op_owner_snapshot(
        self, conn: sqlite3.Connection, actor: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        requested_scope = params.get("scope", "/")
        requested_scope = _canonical_scope(requested_scope)
        limit = self._limit(params.get("limit"), MAX_LIMIT)
        self._require_auth(conn, actor, "owner.read", requested_scope)
        now = _iso_now()
        agents = [
            self._agent_dict(row)
            for row in conn.execute("SELECT * FROM agents ORDER BY agent_id LIMIT ?", (limit,)).fetchall()
        ]
        messages: list[dict[str, Any]] = []
        message_cursor: tuple[str, str] | None = None
        while len(messages) < limit:
            if message_cursor is None:
                rows = conn.execute(
                    "SELECT * FROM messages ORDER BY created_at DESC, id DESC LIMIT ?",
                    (MAX_LIMIT,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM messages WHERE created_at < ? OR "
                    "(created_at = ? AND id < ?) ORDER BY created_at DESC, id DESC LIMIT ?",
                    (message_cursor[0], message_cursor[0], message_cursor[1], MAX_LIMIT),
                ).fetchall()
            if not rows:
                break
            message_cursor = (rows[-1]["created_at"], rows[-1]["id"])
            for row in rows:
                if _scope_contains(requested_scope, row["scope"]):
                    messages.append(self._message_output(conn, row, actor))
                    if len(messages) >= limit:
                        break
            if len(rows) < MAX_LIMIT:
                break

        discoveries: list[dict[str, Any]] = []
        discovery_cursor: tuple[str, str] | None = None
        while len(discoveries) < limit:
            if discovery_cursor is None:
                rows = conn.execute(
                    "SELECT * FROM discoveries WHERE expires_at IS NULL OR expires_at > ? "
                    "ORDER BY created_at DESC, id DESC LIMIT ?",
                    (now, MAX_LIMIT),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM discoveries WHERE (expires_at IS NULL OR expires_at > ?) AND "
                    "(created_at < ? OR (created_at = ? AND id < ?)) "
                    "ORDER BY created_at DESC, id DESC LIMIT ?",
                    (now, discovery_cursor[0], discovery_cursor[0], discovery_cursor[1], MAX_LIMIT),
                ).fetchall()
            if not rows:
                break
            discovery_cursor = (rows[-1]["created_at"], rows[-1]["id"])
            for row in rows:
                if _scope_contains(requested_scope, row["scope"]):
                    discoveries.append(self._discovery_dict(row))
                    if len(discoveries) >= limit:
                        break
            if len(rows) < MAX_LIMIT:
                break

        grants: list[dict[str, Any]] = []
        grant_cursor: tuple[str, str] | None = None
        while len(grants) < limit:
            if grant_cursor is None:
                rows = conn.execute(
                    "SELECT * FROM grants ORDER BY created_at ASC, id ASC LIMIT ?",
                    (MAX_LIMIT,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM grants WHERE created_at > ? OR "
                    "(created_at = ? AND id > ?) ORDER BY created_at ASC, id ASC LIMIT ?",
                    (grant_cursor[0], grant_cursor[0], grant_cursor[1], MAX_LIMIT),
                ).fetchall()
            if not rows:
                break
            grant_cursor = (rows[-1]["created_at"], rows[-1]["id"])
            for row in rows:
                if _scope_contains(requested_scope, row["scope"]):
                    grants.append(self._grant_dict(row))
                    if len(grants) >= limit:
                        break
            if len(rows) < MAX_LIMIT:
                break

        assignments: list[dict[str, Any]] = []
        assignment_cursor: tuple[str, str] | None = None
        while len(assignments) < limit:
            if assignment_cursor is None:
                rows = conn.execute(
                    "SELECT * FROM assignments ORDER BY updated_at DESC, work_id ASC LIMIT ?",
                    (MAX_LIMIT,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM assignments WHERE updated_at < ? OR "
                    "(updated_at = ? AND work_id > ?) ORDER BY updated_at DESC, work_id ASC LIMIT ?",
                    (assignment_cursor[0], assignment_cursor[0], assignment_cursor[1], MAX_LIMIT),
                ).fetchall()
            if not rows:
                break
            assignment_cursor = (rows[-1]["updated_at"], rows[-1]["work_id"])
            for row in rows:
                if _scope_contains(requested_scope, row["scope"]):
                    item = self._assignment_dict(row)
                    item["history"] = self._assignment_history(conn, row["work_id"])
                    assignments.append(item)
                    if len(assignments) >= limit:
                        break
            if len(rows) < MAX_LIMIT:
                break
        counts = {
            "agents": conn.execute("SELECT COUNT(*) AS n FROM agents").fetchone()["n"],
            "messages": conn.execute("SELECT COUNT(*) AS n FROM messages").fetchone()["n"],
            "queued": conn.execute("SELECT COUNT(*) AS n FROM deliveries WHERE state = 'queued'").fetchone()["n"],
            "leased": conn.execute("SELECT COUNT(*) AS n FROM deliveries WHERE state = 'leased'").fetchone()["n"],
            "acknowledged": conn.execute("SELECT COUNT(*) AS n FROM deliveries WHERE state = 'acknowledged'").fetchone()["n"],
            "resolved": conn.execute("SELECT COUNT(*) AS n FROM deliveries WHERE state = 'resolved'").fetchone()["n"],
            "discoveries": conn.execute("SELECT COUNT(*) AS n FROM discoveries").fetchone()["n"],
            "grants": conn.execute("SELECT COUNT(*) AS n FROM grants").fetchone()["n"],
            "assignments": conn.execute("SELECT COUNT(*) AS n FROM assignments").fetchone()["n"],
        }
        return {
            "generated_at": now,
            "principal": actor,
            "agents": agents,
            "messages": messages,
            "discoveries": discoveries,
            "grants": grants,
            "assignments": assignments,
            "counts": counts,
        }

    def _poll_result_current(
        self, conn: sqlite3.Connection, actor: str, result: dict[str, Any]
    ) -> bool:
        """Only replay a cached poll while every returned lease is still live."""

        messages = result.get("messages") if isinstance(result, dict) else None
        if not isinstance(messages, list):
            return False
        now_epoch = time.time()
        now_iso = _iso_now()
        reconciliation = result.get("resume_reconciliation")
        if isinstance(reconciliation, dict):
            active_work = reconciliation.get("active_work_input") or {}
            if self._resume_checkpoint(conn, actor, active_work) != reconciliation:
                return False
        for cached in messages:
            if not isinstance(cached, dict):
                return False
            message_id = cached.get("id")
            cached_delivery = cached.get("delivery")
            if not isinstance(message_id, str) or not isinstance(cached_delivery, dict):
                return False
            lease_id = cached_delivery.get("lease_id")
            if not isinstance(lease_id, str) or not lease_id:
                return False
            delivery = conn.execute(
                "SELECT * FROM deliveries WHERE message_id = ? AND recipient = ?",
                (message_id, actor),
            ).fetchone()
            if (
                delivery is None
                or delivery["state"] != "leased"
                or delivery["lease_id"] != lease_id
                or delivery["lease_until"] is None
                or float(delivery["lease_until"]) <= now_epoch
            ):
                return False
            message = conn.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
            if message is None or _is_expired(message["expires_at"], now_iso):
                return False
            # A newer correction can be acknowledged while this older lease
            # remains live. Its pending head then looks unchanged, but the
            # cached instruction must not lose that later correction.
            current_order: dict[str, Any] = {}
            self._add_instruction_order_context(conn, actor, message, current_order)
            if any(cached.get(key) != value for key, value in current_order.items()):
                return False
            if message["kind"] == "instruction":
                authority = self._current_message_authority(conn, message)
                binding_mismatch = bool(message["assignment_binding"]) and (
                    message["assignment_assignee"] != actor
                )
                if authority is None or not authority["allowed"] or binding_mismatch:
                    return False
        return True

    def _dispatch(
        self, conn: sqlite3.Connection, actor: str, operation: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        if operation == "agents.register":
            return self._op_agents_register(conn, actor, params)
        if operation == "agents.list":
            return self._op_agents_list(conn, actor, params)
        if operation == "agents.heartbeat":
            return self._op_agents_heartbeat(conn, actor, params)
        if operation == "messages.send":
            return self._op_messages_send(conn, actor, params)
        if operation == "messages.poll":
            return self._op_messages_poll(conn, actor, params)
        if operation == "messages.ack":
            return self._op_messages_ack(conn, actor, params)
        if operation == "messages.list":
            return self._op_messages_list(conn, actor, params)
        if operation == "messages.get":
            return self._op_messages_get(conn, actor, params)
        if operation == "discoveries.publish":
            return self._op_discoveries_publish(conn, actor, params)
        if operation == "discoveries.search":
            return self._op_discoveries_search(conn, actor, params)
        if operation == "grants.issue":
            return self._op_grants_issue(conn, actor, params)
        if operation == "grants.revoke":
            return self._op_grants_revoke(conn, actor, params)
        if operation == "grants.list":
            return self._op_grants_list(conn, actor, params)
        if operation == "grants.get":
            return self._op_grants_get(conn, actor, params)
        if operation == "assignments.assign":
            return self._op_assignments_assign(conn, actor, params)
        if operation == "assignments.reassign":
            return self._op_assignments_reassign(conn, actor, params)
        if operation == "assignments.list":
            return self._op_assignments_list(conn, actor, params)
        if operation == "owner.snapshot":
            return self._op_owner_snapshot(conn, actor, params)
        if operation == "authorize":
            target = params.get("agent_id", actor)
            target = self._validate_actor(target)
            if target != actor and not self._is_owner(conn, actor):
                raise HubError("forbidden", "only the owner may inspect another actor's authority", 403)
            action = _text(params.get("action"), "action", max_chars=256)
            scope = _canonical_scope(params.get("scope"))
            assert action is not None
            return self._authorize_conn(conn, target, action, scope)
        raise HubError("unknown_operation", f"unsupported operation: {operation}", 404)

    def call(
        self,
        actor: str,
        operation: str,
        params: dict[str, Any],
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """Execute one business operation in a short transaction.

        Mutation receipts are inserted in the same transaction as their mutation.
        The transport layer should require ``request_id`` for mutations; direct
        in-process callers may omit it when they do not need retry deduplication.
        """

        actor = self._validate_actor(actor)
        if not isinstance(operation, str) or operation not in self._OPERATIONS:
            raise HubError("unknown_operation", f"unsupported operation: {operation}", 404)
        if not isinstance(params, dict):
            raise HubError("invalid_input", "params must be an object")
        if request_id is not None:
            request_id = self._validate_request_id(request_id)
        params_hash = _hash_params(params)
        write = operation in self._MUTATIONS
        with self._transaction(write) as conn:
            if write and request_id is not None:
                receipt = conn.execute(
                    "SELECT * FROM request_receipts WHERE request_id = ?", (request_id,)
                ).fetchone()
                if receipt is not None:
                    if (
                        receipt["actor"] != actor
                        or receipt["operation"] != operation
                        or receipt["params_hash"] != params_hash
                    ):
                        raise HubError("request_conflict", "request_id was already used for different content", 409)
                    cached_result = _json_load(receipt["result_json"], {})
                    if operation == "messages.poll" and not self._poll_result_current(
                        conn, actor, cached_result
                    ):
                        raise HubError(
                            "stale_poll",
                            "cached poll leases are no longer current; retry with a new request_id",
                            409,
                        )
                    return cached_result
            result = self._dispatch(conn, actor, operation, params)
            if write and request_id is not None:
                conn.execute(
                    "INSERT INTO request_receipts(request_id, actor, operation, params_hash, result_json, created_at) "
                    "VALUES(?, ?, ?, ?, ?, ?)",
                    (request_id, actor, operation, params_hash, _json_dump(result), _iso_now()),
                )
            return result


__all__ = ["HubError", "Store"]
