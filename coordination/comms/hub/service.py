"""HTTP transport and local provisioning for the BORG coordination Inbox.

The transport deliberately has a small surface area.  Authentication is
performed here, and every authenticated business request is handed to the
frozen :class:`Store` API.  No transport code implements inbox or authority
semantics of its own.
"""

from __future__ import annotations

import contextlib
import datetime as _datetime
import hashlib
import hmac
import http.server
import json
import mimetypes
import os
from pathlib import Path
import secrets
import socket
import sys
import tempfile
import threading
import time
from collections.abc import Mapping as ABCMapping
from typing import Any, Iterator, Mapping
from urllib.parse import unquote, urlsplit
import uuid

try:  # The transport lane is testable before the core lane lands.
    from .store import HubError, Store
except ModuleNotFoundError as exc:  # pragma: no cover - exercised by integration
    if exc.name not in {"comms.hub.store", f"{__package__}.store"}:
        raise

    class HubError(Exception):
        """Small compatibility error used only while the core is absent."""

        def __init__(self, code: str, message: str, status: int = 400) -> None:
            super().__init__(message)
            self.code = code
            self.message = message
            self.status = status

    Store = None  # type: ignore[assignment,misc]

from .fleet_context import FLEET_CONTEXT_MAX_BYTES, FleetContextReader, compact_json, unknown_context


MAX_REQUEST_BYTES = 128 * 1024
MAX_BODY_BYTES = 16 * 1024
MAX_LIMIT = 100
MAX_REQUEST_ID_BYTES = 256
MAX_CREDENTIAL_BYTES = 4096
DEFAULT_OWNER_ACTOR = "owner"
DEFAULT_PORT = 8795
DEFAULT_ENDPOINT = f"http://127.0.0.1:{DEFAULT_PORT}"
DEFAULT_DB_NAME = "hub.sqlite3"
DEFAULT_CREDENTIALS_NAME = "credentials.json"
DEFAULT_CONFIG_NAME = "config.json"
DEFAULT_OWNER_CREDENTIAL_NAME = "owner.credential.json"


# Estate read model response bounds.  The model publishes per-action
# ``bounds.<action>.max_response_bytes`` through its ``capabilities`` action;
# the hub honours that contract instead of a private, smaller table, while a
# hard ceiling keeps every response well inside the 4 MiB client limit.
ESTATE_DEFAULT_RESPONSE_BOUNDS = {"context": 98304, "history": 524288}
ESTATE_FALLBACK_RESPONSE_BOUND = 262144
ESTATE_MAX_RESPONSE_BOUND = 1024 * 1024


class _EstateOutputBound(Exception):
    def __init__(self, action: Any, size: int, maximum: int) -> None:
        super().__init__("estate output bound")
        self.action = action
        self.size = size
        self.maximum = maximum


def _published_estate_bound(reader: Any, action: Any) -> int:
    """Return the model's published bound for ``action`` or the hub fallback.

    A missing, malformed, or failing capabilities read never raises here; the
    caller keeps its default bound.  The result never exceeds the hub ceiling.
    """
    published = 0
    try:
        capabilities = reader.read("capabilities")
        bounds = capabilities.get("bounds") if isinstance(capabilities, ABCMapping) else None
        entry = bounds.get(action) if isinstance(bounds, ABCMapping) else None
        value = entry.get("max_response_bytes") if isinstance(entry, ABCMapping) else None
        if type(value) is int and value > 0:
            published = value
    except Exception:
        published = 0
    return min(published, ESTATE_MAX_RESPONSE_BOUND)


def _log_estate_event(event: str, action: Any, **fields: Any) -> None:
    """Write one bounded JSON diagnostic line to the service log (stderr)."""
    record: dict[str, Any] = {"estate_read": event}
    if isinstance(action, str) and len(action) <= 64:
        record["action"] = action
    for key, value in fields.items():
        if isinstance(value, (int, str)) and len(str(value)) <= 128:
            record[key] = value
    try:
        sys.stderr.write(json.dumps(record, ensure_ascii=True) + "\n")
        sys.stderr.flush()
    except Exception:
        pass

BROWSER_OPERATIONS = frozenset(
    "browser." + name for name in
    ("open", "act", "renew", "close", "status", "stop", "resume", "reconcile")
) | frozenset("browser.desktop." + name for name in
              ("open", "act", "renew", "close", "status", "stop", "resume"))


def is_browser_operation(operation: Any) -> bool:
    """Reserve the entire browser namespace against durable replay."""
    return isinstance(operation, str) and operation.startswith("browser.")


_BROWSER_ERROR_MESSAGES = {
    "resource_busy": "Browser resource is busy",
    "resource_stopped": "Browser resource is stopped",
    "work_not_owned": "Caller does not own this work",
    "lease_not_found": "Browser lease is not owned by this caller",
    "lease_inactive": "Browser lease is inactive",
    "stale_generation": "Browser lease generation is stale",
    "request_mismatch": "Browser request ID does not match the original request",
    "capacity_unavailable": "Browser workload capacity is unavailable",
    "action_unknown": "Browser action outcome is unknown; reconciliation is required",
    "action_failed": "Browser action failed",
    "operator_required": "Operator authority is required",
    "authorization_unavailable": "Browser authorization is unavailable",
    "invalid_input": "Browser request fields are invalid",
    "invalid_url": "Browser navigation URL is invalid",
    "unsupported_operation": "Browser operation is unsupported",
    "unsupported_action": "Browser action is unsupported",
    "request_too_large": "Browser request exceeds its size bound",
    "profile_unavailable": "Browser profile is unavailable",
    "slot_unavailable": "Desktop slot is unavailable",
    "gateway_stopping": "Browser gateway is stopping",
    "renewal_failed": "Browser lease renewal could not be confirmed",
    "launch_failed": "Browser launch failed; inspect cleanup status",
    "invalid_worker_result": "Browser worker returned an invalid result",
    "result_too_large": "Browser result exceeds its size bound",
    "shutdown_pending": "Browser shutdown is pending",
    "shutdown_incomplete": "Browser shutdown is incomplete",
    "unsafe_state_path": "Browser state path is unavailable",
    "invalid_config": "Browser configuration is invalid",
    "controller_running": "Browser controller is already running",
}


def _browser_error(exc: Exception) -> HubError:
    # Import only on failure: the source-only transport works without browser core.
    try:
        from fleet_browser.store import ResourceError
    except ImportError:
        ResourceError = ()
    if isinstance(exc, ResourceError):
        code = getattr(exc, "code", None)
        if isinstance(code, str) and code in _BROWSER_ERROR_MESSAGES:
            return HubError(code, _BROWSER_ERROR_MESSAGES[code], 400)
    return HubError("browser_unavailable", "Browser gateway is unavailable", 503)


READ_ONLY_OPERATIONS = frozenset(
    {
        "agents.list",
        "messages.list",
        "messages.get",
        "discoveries.search",
        "grants.list",
        "grants.get",
        "assignments.list",
        "owner.snapshot",
        "authorize",
        "fleet.context",
        "estate.read",
    }
)

_CREDENTIAL_DOMAIN = b"utlyze-ecosystem/inbox-service-credential\0"


def utc_now() -> str:
    """Return a compact UTC ISO-8601 timestamp used by transport metadata."""

    return (
        _datetime.datetime.now(_datetime.timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def operation_requires_request_id(operation: str) -> bool:
    """Whether an operation can change state and therefore needs a receipt ID."""

    return operation not in READ_ONLY_OPERATIONS


class TransportConfigurationError(HubError):
    """A local configuration or credential-file error safe to show to callers."""

    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(code, message, status)


def _json_load(path: Path) -> Any:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise TransportConfigurationError(
            "invalid_state_file", f"Unable to read state file: {path.name}", 400
        ) from exc


def _json_dump_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise TransportConfigurationError(
            "invalid_json_value", "The response is not JSON compatible", 500
        ) from exc


def _ensure_directory(path: Path, mode: int = 0o700) -> Path:
    try:
        existed = path.exists()
        path.mkdir(parents=True, exist_ok=True, mode=mode)
        if not existed:
            os.chmod(path, mode)
    except OSError as exc:
        raise TransportConfigurationError(
            "state_unavailable", "Unable to create the local service directory", 500
        ) from exc
    return path


def _atomic_write(path: Path, data: bytes, mode: int = 0o600) -> None:
    """Atomically write a private state file without exposing partial JSON."""

    parent = _ensure_directory(path.parent)
    temporary_name: str | None = None
    try:
        fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(parent))
        with os.fdopen(fd, "wb") as handle:
            os.fchmod(handle.fileno(), mode)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
        temporary_name = None
        os.chmod(path, mode)
    except OSError as exc:
        raise TransportConfigurationError(
            "state_unavailable", f"Unable to write state file: {path.name}", 500
        ) from exc
    finally:
        if temporary_name is not None:
            with contextlib.suppress(OSError):
                os.unlink(temporary_name)


def _atomic_write_json(path: Path, value: Any, mode: int = 0o600) -> None:
    _atomic_write(path, _json_dump_bytes(value) + b"\n", mode=mode)


def _validate_principal(principal: str) -> str:
    if not isinstance(principal, str) or not principal or len(principal) > 256:
        raise TransportConfigurationError(
            "invalid_principal", "Credential principal must be a non-empty identifier", 400
        )
    return principal


def _validate_credential_value(token: str) -> str:
    if not isinstance(token, str) or not token or len(token.encode("utf-8")) > MAX_CREDENTIAL_BYTES:
        raise TransportConfigurationError("invalid_credential", "Credential is invalid", 400)
    return token


def credential_digest(token: str) -> str:
    """Hash a credential for server-side storage; the raw value never enters state."""

    token = _validate_credential_value(token)
    return hashlib.sha256(_CREDENTIAL_DOMAIN + token.encode("utf-8")).hexdigest()


@contextlib.contextmanager
def _exclusive_file_lock(path: Path) -> Iterator[None]:
    """Use an advisory lock when available, while remaining portable in tests."""

    _ensure_directory(path.parent)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            import fcntl  # Unix stdlib; unavailable on a few test platforms.

            fcntl.flock(fd, fcntl.LOCK_EX)
        except (ImportError, OSError):
            pass
        yield
    finally:
        with contextlib.suppress(Exception):
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def write_credential_file(path: str | os.PathLike[str], token: str, principal: str) -> Path:
    """Write a client credential file with mode 0600.

    The function returns only the path.  The token is intentionally accepted
    as an argument so callers can issue it once, write it, and discard it.
    """

    target = Path(path).expanduser()
    _validate_principal(principal)
    _validate_credential_value(token)
    _atomic_write_json(
        target,
        {"version": 1, "principal": principal, "credential": token},
        mode=0o600,
    )
    return target


def read_credential_file(path: str | os.PathLike[str]) -> str:
    """Read either the generated JSON credential format or a private plain file."""

    target = Path(path).expanduser()
    try:
        raw = target.read_bytes()
    except (OSError, UnicodeError) as exc:
        raise TransportConfigurationError(
            "credential_unavailable", "Unable to read the credential file", 400
        ) from exc
    if len(raw) > MAX_CREDENTIAL_BYTES:
        raise TransportConfigurationError("invalid_credential", "Credential is invalid", 400)
    try:
        text = raw.decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise TransportConfigurationError("invalid_credential", "Credential is invalid", 400) from exc
    if not text:
        raise TransportConfigurationError("invalid_credential", "Credential is invalid", 400)
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        value = text
    if isinstance(value, dict):
        token = value.get("credential")
        if token is None:
            token = value.get("token")
        if not isinstance(token, str):
            raise TransportConfigurationError("invalid_credential", "Credential is invalid", 400)
    elif isinstance(value, str):
        token = value
    else:
        raise TransportConfigurationError("invalid_credential", "Credential is invalid", 400)
    return _validate_credential_value(token)


class IssuedCredential:
    """In-memory result of issuance; repr deliberately redacts the secret."""

    __slots__ = ("principal", "credential_id", "token", "path")

    def __init__(
        self,
        principal: str,
        credential_id: str,
        token: str,
        path: Path | None = None,
    ) -> None:
        self.principal = principal
        self.credential_id = credential_id
        self.token = token
        self.path = path

    def __repr__(self) -> str:  # pragma: no cover - defensive secret hygiene
        return (
            f"IssuedCredential(principal={self.principal!r}, "
            f"credential_id={self.credential_id!r}, token=<redacted>, path={self.path!r})"
        )


class CredentialStore:
    """Durable hash-only credential registry for service principals."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path).expanduser()
        self._lock_path = self.path.with_name(f".{self.path.name}.lock")
        self._thread_lock = threading.RLock()
        _ensure_directory(self.path.parent)
        if self.path.exists():
            os.chmod(self.path, 0o600)

    def _read_unlocked(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"version": 1, "credentials": []}
        value = _json_load(self.path)
        if not isinstance(value, dict) or not isinstance(value.get("credentials"), list):
            raise TransportConfigurationError(
                "invalid_credentials_state", "Credential registry is invalid", 500
            )
        if "requests" not in value:
            value["requests"] = []
        if not isinstance(value.get("requests"), list):
            raise TransportConfigurationError(
                "invalid_credentials_state", "Credential registry is invalid", 500
            )
        return value

    def _write_unlocked(self, value: dict[str, Any]) -> None:
        # The assertion is intentionally narrow: no raw token may be serialized.
        serialized = _json_dump_bytes(value)
        _atomic_write(self.path, serialized + b"\n", mode=0o600)

    def _records(self) -> list[dict[str, Any]]:
        value = self._read_unlocked()
        records = value.get("credentials", [])
        return [record for record in records if isinstance(record, dict)]

    def _write_records_unlocked(self, records: list[dict[str, Any]]) -> None:
        value = self._read_unlocked()
        value["version"] = 1
        value["credentials"] = records
        self._write_unlocked(value)

    def issue(
        self,
        principal: str,
        credential_file: str | os.PathLike[str] | None = None,
    ) -> IssuedCredential:
        """Create or validate a credential and optionally put it in a 0600 file."""

        principal = _validate_principal(principal)
        target = Path(credential_file).expanduser() if credential_file is not None else None
        if target is not None:
            with contextlib.suppress(OSError):
                if target.resolve() == self.path.resolve():
                    raise TransportConfigurationError(
                        "credential_path_conflict",
                        "Credential file cannot be the server registry",
                        400,
                    )
        with self._thread_lock, _exclusive_file_lock(self._lock_path):
            if target is not None and target.exists():
                token = read_credential_file(target)
                with contextlib.suppress(OSError):
                    os.chmod(target, 0o600)
                records = self._records()
                found_record = self._record_for_token_unlocked(token, records)
                if found_record is not None and found_record.get("principal") != principal:
                    raise TransportConfigurationError(
                        "credential_principal_conflict",
                        "Credential file belongs to another principal",
                        409,
                    )
                if found_record is not None and found_record.get("revoked_at"):
                    raise TransportConfigurationError(
                        "credential_revoked", "Credential has been revoked", 409
                    )
                if found_record is None:
                    credential_id = str(uuid.uuid4())
                    records.append(
                        {
                            "id": credential_id,
                            "principal": principal,
                            "digest": credential_digest(token),
                            "created_at": utc_now(),
                            "revoked_at": None,
                        }
                    )
                    self._write_records_unlocked(records)
                else:
                    credential_id = found_record.get("id") or str(uuid.uuid4())
                return IssuedCredential(principal, credential_id, token, target)

            token = secrets.token_urlsafe(32)
            credential_id = str(uuid.uuid4())
            records = self._records()
            records.append(
                {
                    "id": credential_id,
                    "principal": principal,
                    "digest": credential_digest(token),
                    "created_at": utc_now(),
                    "revoked_at": None,
                }
            )
            self._write_records_unlocked(records)
            if target is not None:
                write_credential_file(target, token, principal)
            return IssuedCredential(principal, credential_id, token, target)

    create = issue

    def register_token(self, principal: str, token: str) -> str:
        """Register an existing in-memory test credential without persisting it raw."""

        principal = _validate_principal(principal)
        token = _validate_credential_value(token)
        with self._thread_lock, _exclusive_file_lock(self._lock_path):
            records = self._records()
            existing_record = self._record_for_token_unlocked(token, records)
            if existing_record is not None:
                if existing_record.get("principal") != principal:
                    raise TransportConfigurationError(
                        "credential_principal_conflict",
                        "Credential is already assigned to another principal",
                        409,
                    )
                if existing_record.get("revoked_at"):
                    raise TransportConfigurationError(
                        "credential_revoked", "Credential has been revoked", 409
                    )
                existing = existing_record.get("id")
                if isinstance(existing, str) and existing:
                    return existing
            credential_id = str(uuid.uuid4())
            records.append(
                {
                    "id": credential_id,
                    "principal": principal,
                    "digest": credential_digest(token),
                    "created_at": utc_now(),
                    "revoked_at": None,
                }
            )
            self._write_records_unlocked(records)
            return credential_id

    add_token = register_token

    @staticmethod
    def _validate_digest(value: str) -> str:
        if (
            not isinstance(value, str)
            or len(value) != hashlib.sha256().digest_size * 2
            or any(character not in "0123456789abcdefABCDEF" for character in value)
        ):
            raise TransportConfigurationError(
                "invalid_credential_digest", "Credential digest is invalid", 400
            )
        return value.lower()

    def register_digest(
        self,
        principal: str,
        digest: str,
        issuer: str,
        request_id: str,
    ) -> dict[str, Any]:
        """Register a client-generated hash without receiving its raw token."""

        principal = _validate_principal(principal)
        issuer = _validate_principal(issuer)
        digest = self._validate_digest(digest)
        if not isinstance(request_id, str) or not request_id or len(request_id.encode("utf-8")) > MAX_REQUEST_ID_BYTES:
            raise TransportConfigurationError("invalid_request_id", "Request ID is invalid", 400)
        with self._thread_lock, _exclusive_file_lock(self._lock_path):
            value = self._read_unlocked()
            requests = [record for record in value.get("requests", []) if isinstance(record, dict)]
            for receipt in requests:
                if receipt.get("request_id") != request_id:
                    continue
                if (
                    receipt.get("agent_id") != principal
                    or receipt.get("credential_digest") != digest
                    or receipt.get("issuer") != issuer
                ):
                    raise TransportConfigurationError(
                        "request_id_conflict",
                        "Request ID is already used for different content",
                        409,
                    )
                result = receipt.get("result")
                if isinstance(result, dict):
                    return json.loads(json.dumps(result, allow_nan=False))
                raise TransportConfigurationError(
                    "invalid_credentials_state", "Credential registry receipt is invalid", 500
                )

            record: dict[str, Any] | None = None
            for existing in value.get("credentials", []):
                if not isinstance(existing, dict) or existing.get("digest") != digest:
                    continue
                record = existing
                break
            if record is not None:
                existing_principal = record.get("principal")
                if existing_principal != principal:
                    raise TransportConfigurationError(
                        "credential_digest_conflict",
                        "Credential digest is already assigned to another principal",
                        409,
                    )
                if record.get("revoked_at"):
                    raise TransportConfigurationError(
                        "credential_revoked", "Credential has been revoked", 409
                    )
            else:
                record = {
                    "id": str(uuid.uuid4()),
                    "principal": principal,
                    "digest": digest,
                    "created_at": utc_now(),
                    "issuer": issuer,
                    "revoked_at": None,
                }
                value["credentials"].append(record)

            result = {
                "credential": {
                    "id": record.get("id"),
                    "principal": principal,
                    "created_at": record.get("created_at"),
                    "issuer": record.get("issuer", issuer),
                }
            }
            requests.append(
                {
                    "request_id": request_id,
                    "agent_id": principal,
                    "credential_digest": digest,
                    "issuer": issuer,
                    "created_at": utc_now(),
                    "result": result,
                }
            )
            value["version"] = 1
            value["requests"] = requests
            self._write_unlocked(value)
            return result

    register_credential_digest = register_digest

    def _record_for_digest_unlocked(
        self, digest: str, records: list[dict[str, Any]] | None = None
    ) -> dict[str, Any] | None:
        for record in records if records is not None else self._records():
            stored = record.get("digest")
            if isinstance(stored, str) and hmac.compare_digest(stored, digest):
                return record
        return None

    def _record_for_token_unlocked(
        self, token: str, records: list[dict[str, Any]] | None = None
    ) -> dict[str, Any] | None:
        return self._record_for_digest_unlocked(credential_digest(token), records)

    def _principal_for_token_unlocked(
        self, token: str, records: list[dict[str, Any]] | None = None
    ) -> str | None:
        record = self._record_for_token_unlocked(token, records)
        if record is not None and not record.get("revoked_at"):
            principal = record.get("principal")
            if isinstance(principal, str):
                return principal
        return None

    def _credential_id_for_token_unlocked(
        self, token: str, records: list[dict[str, Any]] | None = None
    ) -> str | None:
        record = self._record_for_token_unlocked(token, records)
        if record is not None and not record.get("revoked_at"):
            identifier = record.get("id")
            if isinstance(identifier, str):
                return identifier
        return None

    def principal_for_token(self, token: str) -> str | None:
        token = _validate_credential_value(token)
        with self._thread_lock, _exclusive_file_lock(self._lock_path):
            return self._principal_for_token_unlocked(token)

    authenticate = principal_for_token

    def credential_id_for_token(self, token: str) -> str | None:
        token = _validate_credential_value(token)
        with self._thread_lock, _exclusive_file_lock(self._lock_path):
            return self._credential_id_for_token_unlocked(token)

    def principal_for_file(self, path: str | os.PathLike[str]) -> str | None:
        token = read_credential_file(path)
        return self.principal_for_token(token)

    def revoke(self, credential_id: str) -> bool:
        with self._thread_lock, _exclusive_file_lock(self._lock_path):
            records = self._records()
            changed = False
            for record in records:
                if record.get("id") == credential_id and not record.get("revoked_at"):
                    record["revoked_at"] = utc_now()
                    changed = True
            if changed:
                self._write_records_unlocked(records)
            return changed

    def metadata(self) -> dict[str, Any]:
        """Return non-secret registry metadata for diagnostics and tests."""

        with self._thread_lock, _exclusive_file_lock(self._lock_path):
            records = self._records()
            return {
                "count": len(records),
                "active": sum(1 for record in records if not record.get("revoked_at")),
            }


class InMemoryCredentialStore:
    """Hash-only credential adapter for injected transport tests.

    Production state uses :class:`CredentialStore`; this adapter avoids making
    transport-only tests depend on a filesystem registry while retaining the
    same constant-time digest comparison behavior.
    """

    def __init__(self, credentials: Mapping[str, str]) -> None:
        self._records: list[tuple[str, str]] = []
        for principal, token in credentials.items():
            principal = _validate_principal(principal)
            token = _validate_credential_value(token)
            self._records.append((credential_digest(token), principal))

    def principal_for_token(self, token: str) -> str | None:
        digest = credential_digest(token)
        for stored, principal in self._records:
            if hmac.compare_digest(stored, digest):
                return principal
        return None

    authenticate = principal_for_token


def _new_store(db_path: Path) -> Any:
    if Store is None:
        raise TransportConfigurationError(
            "core_unavailable", "The core Store implementation is not installed", 503
        )
    try:
        return Store(str(db_path))
    except TypeError:
        # A small accommodation for test doubles accepting Path objects.
        return Store(db_path)


def _state_config_path(state_dir: Path) -> Path:
    return state_dir / DEFAULT_CONFIG_NAME


def load_service_config(path: str | os.PathLike[str]) -> dict[str, Any]:
    target = Path(path).expanduser()
    value = _json_load(target)
    if not isinstance(value, dict):
        raise TransportConfigurationError("invalid_config", "Service config must be an object", 400)
    return value


def _write_service_config(
    state_dir: Path,
    *,
    db_path: Path,
    credentials_path: Path,
    owner_actor: str,
    endpoint: str,
    owner_credential_file: Path,
    approval_ref: str,
) -> Path:
    path = _state_config_path(state_dir)
    _atomic_write_json(
        path,
        {
            "version": 1,
            "state_dir": str(state_dir.resolve()),
            "db_path": str(db_path.resolve()),
            "credentials_path": str(credentials_path.resolve()),
            "endpoint": endpoint,
            "owner_actor": owner_actor,
            "agent_id": owner_actor,
            "owner_credential_file": str(owner_credential_file.resolve()),
            "credential_file": str(owner_credential_file.resolve()),
            "outbox_path": str(
                owner_credential_file.with_name(
                    f".{owner_credential_file.name}.outbox.json"
                ).resolve()
            ),
            "approval_ref": approval_ref,
        },
        mode=0o600,
    )
    return path


def _config_value_path(config: Mapping[str, Any], key: str, state_dir: Path, fallback: Path) -> Path:
    value = config.get(key)
    if not isinstance(value, str) or not value:
        return fallback
    target = Path(value).expanduser()
    if not target.is_absolute():
        target = state_dir / target
    return target


def initialize_state(
    state_dir: str | os.PathLike[str],
    *,
    owner_actor: str = DEFAULT_OWNER_ACTOR,
    approval_ref: str = "local-init",
    endpoint: str = DEFAULT_ENDPOINT,
    store: Any | None = None,
    credentials: CredentialStore | None = None,
) -> dict[str, Any]:
    """Create the local service state and bootstrap the owner exactly locally."""

    state = _ensure_directory(Path(state_dir).expanduser())
    owner_actor = _validate_principal(owner_actor)
    if not isinstance(approval_ref, str) or not approval_ref:
        raise TransportConfigurationError("invalid_approval_ref", "Approval reference is required", 400)
    if not isinstance(endpoint, str) or not endpoint:
        raise TransportConfigurationError("invalid_endpoint", "Endpoint is required", 400)

    config_path = _state_config_path(state)
    existing: dict[str, Any] = {}
    if config_path.exists():
        existing = load_service_config(config_path)
        configured_owner = existing.get("owner_actor")
        if isinstance(configured_owner, str) and configured_owner:
            owner_actor = configured_owner
        configured_endpoint = existing.get("endpoint")
        if isinstance(configured_endpoint, str) and configured_endpoint:
            endpoint = configured_endpoint
        configured_approval = existing.get("approval_ref")
        if isinstance(configured_approval, str) and configured_approval:
            approval_ref = configured_approval

    owner_actor = _validate_principal(owner_actor)

    db_path = _config_value_path(existing, "db_path", state, state / DEFAULT_DB_NAME)
    credentials_path = _config_value_path(
        existing, "credentials_path", state, state / DEFAULT_CREDENTIALS_NAME
    )
    owner_file = _config_value_path(
        existing,
        "owner_credential_file",
        state,
        state / DEFAULT_OWNER_CREDENTIAL_NAME,
    )
    if credentials is None:
        credentials = CredentialStore(credentials_path)
    if store is None:
        store = _new_store(db_path)
    credentials_registry_path = Path(getattr(credentials, "path", credentials_path))

    # This call is intentionally local-only: initialize_state is not exposed
    # through HTTP and enrollment below repeats it idempotently for local admin.
    store.bootstrap_owner(owner_actor, approval_ref)
    issued = credentials.issue(owner_actor, owner_file)
    config_path = _write_service_config(
        state,
        db_path=db_path,
        credentials_path=credentials_registry_path,
        owner_actor=owner_actor,
        endpoint=endpoint,
        owner_credential_file=owner_file,
        approval_ref=approval_ref,
    )
    return {
        "state_dir": str(state.resolve()),
        "config_path": str(config_path.resolve()),
        "db_path": str(db_path.resolve()),
        "credentials_path": str(credentials_registry_path.resolve()),
        "credential_file": str((issued.path or owner_file).resolve()),
        "owner_credential_file": str((issued.path or owner_file).resolve()),
        "principal": owner_actor,
        "endpoint": endpoint,
    }


initialize = initialize_state


def _stable_enrollment_id(agent_id: str, runtime: str, machine: str, purpose: str) -> str:
    value = f"utlyze-ecosystem/enroll/{purpose}/{agent_id}/{runtime}/{machine}"
    return str(uuid.uuid5(uuid.NAMESPACE_URL, value))


def _required_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise TransportConfigurationError(
            "invalid_enrollment", f"Enrollment field '{field}' is required", 400
        )
    return value


def enroll_agent(
    state_dir: str | os.PathLike[str],
    agent_id: str,
    runtime: str,
    machine: str,
    credential_file: str | os.PathLike[str],
    *,
    display_name: str | None = None,
    capabilities: list[str] | None = None,
    owner_actor: str | None = None,
    grant_scope: str = "/",
    grant_actions: list[str] | None = None,
    grant_delegable: bool | None = None,
    store: Any | None = None,
    credentials: CredentialStore | None = None,
) -> dict[str, Any]:
    """Enroll an agent through a local trusted-admin path.

    Enrollment is real authority provisioning: after registration it issues an
    explicit grant from the bootstrapped owner. Existing callers retain the
    historical root/all-actions/delegable defaults. The credential file is
    written only after both core mutations succeed.
    """

    state = Path(state_dir).expanduser()
    config_path = _state_config_path(state)
    if not config_path.exists():
        raise TransportConfigurationError(
            "uninitialized_state", "Run init before enrolling an agent", 400
        )
    config = load_service_config(config_path)
    configured_owner = config.get("owner_actor")
    if isinstance(configured_owner, str) and configured_owner:
        if owner_actor is not None and owner_actor != configured_owner:
            raise TransportConfigurationError(
                "owner_conflict",
                "Enrollment owner does not match the initialized service owner",
                403,
            )
        effective_owner = configured_owner
    else:
        effective_owner = owner_actor or DEFAULT_OWNER_ACTOR
    effective_owner = _validate_principal(effective_owner)
    agent_id = _required_string(agent_id, "agent")
    runtime = _required_string(runtime, "runtime")
    machine = _required_string(machine, "machine")
    credential_target = Path(credential_file).expanduser()
    if not credential_target.is_absolute():
        credential_target = Path.cwd() / credential_target
    if capabilities is not None and (
        not isinstance(capabilities, list)
        or any(not isinstance(capability, str) for capability in capabilities)
    ):
        raise TransportConfigurationError(
            "invalid_enrollment", "Capabilities must be a list of strings", 400
        )
    grant_scope = _required_string(grant_scope, "grant_scope")
    if grant_actions is None:
        grant_actions = ["*"]
    if (
        not isinstance(grant_actions, list)
        or not grant_actions
        or any(not isinstance(action, str) or not action for action in grant_actions)
    ):
        raise TransportConfigurationError(
            "invalid_enrollment", "Grant actions must be a non-empty list of strings", 400
        )
    if grant_delegable is None:
        grant_delegable = True
    if not isinstance(grant_delegable, bool):
        raise TransportConfigurationError(
            "invalid_enrollment", "Grant delegable must be boolean", 400
        )

    db_path = _config_value_path(config, "db_path", state, state / DEFAULT_DB_NAME)
    credentials_path = _config_value_path(
        config, "credentials_path", state, state / DEFAULT_CREDENTIALS_NAME
    )
    approval_ref = config.get("approval_ref")
    if not isinstance(approval_ref, str) or not approval_ref:
        approval_ref = "local-init"
    if credentials is None:
        credentials = CredentialStore(credentials_path)
    if store is None:
        store = _new_store(db_path)
    credentials_registry_path = Path(getattr(credentials, "path", credentials_path))

    # Idempotent bootstrap is safe and proves this route is bound to the
    # approved local owner rather than a network-supplied actor name.
    store.bootstrap_owner(effective_owner, approval_ref)

    register_request_id = _stable_enrollment_id(agent_id, runtime, machine, "register")
    register_params: dict[str, Any] = {
        "agent_id": agent_id,
        "runtime": runtime,
        "machine": machine,
    }
    if display_name is not None:
        register_params["display_name"] = display_name
    if capabilities is not None:
        register_params["capabilities"] = capabilities
    registered = store.call(
        effective_owner,
        "agents.register",
        register_params,
        request_id=register_request_id,
    )

    grant_request_id = _stable_enrollment_id(agent_id, runtime, machine, "seed-grant")
    grant_params = {
        "grantee": agent_id,
        "scope": grant_scope,
        "actions": grant_actions,
        "delegable": grant_delegable,
        "reason": "approved local enrollment seed authority",
    }
    grant_result = store.call(
        effective_owner,
        "grants.issue",
        grant_params,
        request_id=grant_request_id,
    )
    if not isinstance(grant_result, dict):
        raise TransportConfigurationError(
            "invalid_core_response", "Core did not return an enrollment grant", 500
        )
    grant = grant_result.get("grant")
    if not isinstance(grant, dict) or not isinstance(grant.get("id"), str):
        raise TransportConfigurationError(
            "invalid_core_response", "Core did not return an enrollment grant", 500
        )

    issued = credentials.issue(agent_id, credential_target)
    endpoint = config.get("endpoint")
    if not isinstance(endpoint, str) or not endpoint:
        endpoint = DEFAULT_ENDPOINT
    agent_config_path = credential_target.with_name(
        f"{credential_target.stem}.config.json"
    )
    _atomic_write_json(
        agent_config_path,
        {
            "version": 1,
            "state_dir": str(state.resolve()),
            "db_path": str(db_path.resolve()),
            "credentials_path": str(credentials_registry_path.resolve()),
            "endpoint": endpoint,
            "credential_file": str(credential_target.resolve()),
            "agent_id": agent_id,
            "principal": agent_id,
            "outbox_path": str(
                credential_target.with_name(
                    f".{credential_target.name}.outbox.json"
                ).resolve()
            ),
            "service_config_path": str(config_path.resolve()),
        },
        mode=0o600,
    )
    return {
        "agent_id": agent_id,
        "credential_file": str((issued.path or credential_target).resolve()),
        "config_path": str(config_path.resolve()),
        "agent_config_path": str(agent_config_path.resolve()),
        "owner_actor": effective_owner,
        "grant_id": grant["id"],
        "grant": grant,
        "agent": registered.get("agent") if isinstance(registered, dict) else registered,
        "request_ids": {
            "register": register_request_id,
            "grant": grant_request_id,
        },
    }


enroll = enroll_agent


class HubService:
    """Authenticated transport facade around a frozen Store instance."""

    def __init__(
        self,
        store: Any | None = None,
        credentials: CredentialStore | None = None,
        *,
        credential_store: CredentialStore | None = None,
        state_dir: str | os.PathLike[str] | None = None,
        web_root: str | os.PathLike[str] | None = None,
        started_at: float | None = None,
        request_timeout: float = 10.0,
        fleet_context_reader: Any | None = None,
        estate_reader_factory: Any | None = None,
        browser_gateway_factory: Any | None = None,
    ) -> None:
        if credential_store is not None:
            credentials = credential_store
        self.state_dir = Path(state_dir).expanduser() if state_dir is not None else None
        config: dict[str, Any] = {}
        if self.state_dir is not None:
            config_path = _state_config_path(self.state_dir)
            if config_path.exists():
                config = load_service_config(config_path)
            db_path = _config_value_path(
                config, "db_path", self.state_dir, self.state_dir / DEFAULT_DB_NAME
            )
            credentials_path = _config_value_path(
                config,
                "credentials_path",
                self.state_dir,
                self.state_dir / DEFAULT_CREDENTIALS_NAME,
            )
            if store is None:
                store = _new_store(db_path)
            if credentials is None:
                credentials = CredentialStore(credentials_path)
        if isinstance(credentials, ABCMapping):
            credentials = InMemoryCredentialStore(credentials)
        elif isinstance(credentials, (str, os.PathLike)):
            credentials = CredentialStore(credentials)
        if store is None:
            raise TransportConfigurationError("core_unavailable", "A Store is required", 503)
        if credentials is None:
            raise TransportConfigurationError(
                "credentials_unavailable", "A credential registry is required", 503
            )
        self._browser_gateway_factory = browser_gateway_factory
        self._browser_gateway_lock = threading.Lock()
        self._browser_gateway = None
        self._browser_gateway_initialized = False
        self._browser_gateway_closed = False
        self.store = store
        self.credentials = credentials
        self.web_root = (
            Path(web_root).expanduser()
            if web_root is not None
            else Path(__file__).with_name("web")
        )
        self.started_at = started_at if started_at is not None else time.monotonic()
        try:
            self.request_timeout = max(0.1, float(request_timeout))
        except (TypeError, ValueError) as exc:
            raise TransportConfigurationError(
                "invalid_request_timeout", "Request timeout is invalid", 400
            ) from exc
        self.config = config
        configured_owner = config.get("owner_actor")
        self.owner_actor = (
            configured_owner
            if isinstance(configured_owner, str) and configured_owner
            else DEFAULT_OWNER_ACTOR
        )
        self.fleet_context_reader = fleet_context_reader or FleetContextReader()
        self.estate_reader_factory = estate_reader_factory

    @classmethod
    def from_state_dir(
        cls,
        state_dir: str | os.PathLike[str],
        *,
        store: Any | None = None,
        web_root: str | os.PathLike[str] | None = None,
    ) -> "HubService":
        return cls(store=store, state_dir=state_dir, web_root=web_root)

    def authenticate(self, token: str) -> str | None:
        try:
            return self.credentials.principal_for_token(token)
        except AttributeError:
            return self.credentials.authenticate(token)

    def register_credential(
        self,
        actor: str,
        params: dict[str, Any],
        request_id: str,
    ) -> dict[str, Any]:
        """Register a locally generated child credential after live authority check."""

        agent_id = params.get("agent_id")
        digest = params.get("credential_digest")
        if not isinstance(agent_id, str) or not agent_id:
            raise TransportConfigurationError(
                "invalid_credential_registration", "agent_id is required", 400
            )
        if not isinstance(digest, str):
            raise TransportConfigurationError(
                "invalid_credential_registration", "credential_digest is required", 400
            )
        if agent_id == self.owner_actor and actor != self.owner_actor:
            raise TransportConfigurationError(
                "identity_escalation", "A child credential cannot replace the owner identity", 403
            )
        authorize = getattr(self.store, "authorize", None)
        if not callable(authorize):
            raise TransportConfigurationError(
                "core_unavailable", "Core authorization is required for credential registration", 503
            )
        evidence = authorize(actor, "credentials.issue", "/")
        if not isinstance(evidence, dict) or evidence.get("allowed") is not True:
            raise TransportConfigurationError(
                "not_authorized",
                "Current authority cannot issue child credentials",
                403,
            )
        register_digest = getattr(self.credentials, "register_digest", None)
        if not callable(register_digest):
            raise TransportConfigurationError(
                "credentials_unavailable", "Credential registry cannot register digests", 503
            )
        result = register_digest(agent_id, digest, actor, request_id)
        if not isinstance(result, dict):
            raise TransportConfigurationError(
                "invalid_credentials_state", "Credential registry returned an invalid result", 500
            )
        response = dict(result)
        response["authority"] = evidence
        return response

    def call(
        self,
        actor: str,
        operation: str,
        params: dict[str, Any],
        request_id: str | None = None,
    ) -> Any:
        _validate_operation_params(operation, params, request_id)
        if is_browser_operation(operation):
            return self.browser_call(actor, operation, params, request_id)
        if operation == "fleet.context":
            return self.fleet_context(actor, params)
        if operation == "estate.read":
            return self.estate_read(actor, params)
        if operation == "credentials.register":
            assert request_id is not None
            return self.register_credential(actor, params, request_id)
        return self.store.call(actor, operation, params, request_id=request_id)

    def browser_call(self, actor: str, operation: str, params: dict[str, Any],
                     request_id: str | None) -> Any:
        try:
            with self._browser_gateway_lock:
                if self._browser_gateway_closed:
                    raise RuntimeError()
                if not self._browser_gateway_initialized:
                    # Failed initialization is terminal too: never retry an ambiguous launch.
                    self._browser_gateway_initialized = True
                    if self._browser_gateway_factory is not None:
                        self._browser_gateway = self._browser_gateway_factory(self)
                gateway = self._browser_gateway
                if gateway is None:
                    raise RuntimeError()
            # Gateway owns concurrent calls and shutdown cancellation; never hold
            # the initialization lock over a potentially waiting browser action.
            return gateway.call(actor, operation, params, request_id)
        except Exception as exc:
            raise _browser_error(exc) from None

    def close_browser_gateway(self) -> None:
        """Prevent new initialization and close cached gateway; safe to retry cleanup."""
        with self._browser_gateway_lock:
            self._browser_gateway_closed = True
            gateway = self._browser_gateway
            if gateway is not None:
                try:
                    gateway.close()
                except Exception as exc:
                    raise _browser_error(exc) from None
                self._browser_gateway = None

    def estate_read(self, actor: str, params: dict[str, Any]) -> dict[str, Any]:
        authorize = getattr(self.store, "authorize", None)
        if not callable(authorize):
            raise TransportConfigurationError("core_unavailable", "Core authorization is required", 503)
        evidence = authorize(actor, "estate.read", "/")
        if not isinstance(evidence, ABCMapping) or evidence.get("allowed") is not True:
            raise TransportConfigurationError("not_authorized", "Current authority cannot read estate", 403)
        if self.estate_reader_factory is None:
            raise TransportConfigurationError(
                "integration_disabled", "Estate integration is not configured", 503
            )
        try:
            factory = self.estate_reader_factory
            reader = factory(lambda: self.fleet_context(actor, {"scope": "/"}))
            arguments = dict(params)
            action = arguments.pop("action")
            result = reader.read(action, **arguments)
            if not isinstance(result, dict) or result.get("schema") != "eco-estate/v1":
                raise ValueError("invalid estate response")
            size = len(compact_json(result).encode("utf-8"))
            maximum = ESTATE_DEFAULT_RESPONSE_BOUNDS.get(action, ESTATE_FALLBACK_RESPONSE_BOUND)
            if size > maximum:
                # Only an oversized result pays for the capabilities read; the
                # model's published per-action bound governs, under the hub ceiling.
                maximum = max(maximum, _published_estate_bound(reader, action))
            if size > maximum:
                raise _EstateOutputBound(action, size, maximum)
            return result
        except _EstateOutputBound as exc:
            _log_estate_event("output_bound", exc.action, size=exc.size, maximum=exc.maximum)
            raise TransportConfigurationError(
                "output_bound", "Estate response exceeds its published bound", 413
            ) from None
        except Exception as exc:
            # Preserve the model's public typed error, never a raw exception.
            if type(exc).__name__ == "EstateError" and getattr(exc, "code", None) in {
                "invalid_request", "not_found", "output_bound", "source_unavailable",
                "history_unavailable", "history_timeout", "response_too_large",
                "source_invalid", "invalid_output"
            }:
                code = exc.code
                status = getattr(exc, "status", 400)
                if status not in {400, 404, 413, 422, 503}:
                    status = 503
                raise TransportConfigurationError(code, "Estate request could not be served", status) from None
            # The exception class name is diagnosable in the service log; the
            # message text may hold private paths or prose and is never logged.
            _log_estate_event("unavailable", params.get("action"), error_type=type(exc).__name__)
            raise TransportConfigurationError("estate_unavailable", "Estate source is unavailable", 503) from None

    def fleet_context(self, actor: str, params: dict[str, Any]) -> dict[str, Any]:
        """Serve one bounded passive snapshot after the live Store grant check."""

        if any(key != "scope" for key in params) or params.get("scope", "/") != "/":
            raise TransportConfigurationError(
                "invalid_params",
                "fleet.context accepts only scope /",
                400,
            )
        authorize = getattr(self.store, "authorize", None)
        if not callable(authorize):
            raise TransportConfigurationError(
                "core_unavailable",
                "Core authorization is required for fleet context",
                503,
            )
        evidence = authorize(actor, "fleet.context", "/")
        if not isinstance(evidence, ABCMapping) or evidence.get("allowed") is not True:
            raise TransportConfigurationError(
                "not_authorized",
                "Current authority cannot read fleet context",
                403,
            )
        try:
            if isinstance(self.fleet_context_reader, FleetContextReader):
                ownership_snapshot = None
                snapshotter = getattr(self.store, "fleet_ownership_snapshot", None)
                if callable(snapshotter):
                    try:
                        ownership_snapshot = snapshotter()
                    except Exception:
                        # Fleet observation remains available; only ownership
                        # reports its missing authoritative input.
                        ownership_snapshot = None
                result = self.fleet_context_reader.read(
                    scope="/", ownership_snapshot=ownership_snapshot
                )
            else:
                # Preserve the existing injected-reader test/extension seam.
                result = self.fleet_context_reader.read(scope="/")
            if not isinstance(result, dict):
                raise ValueError("invalid result")
            if len(compact_json(result).encode("utf-8")) > FLEET_CONTEXT_MAX_BYTES:
                return unknown_context("/", "output_bound")
            return result
        except Exception:
            # A passive observer must fail open so normal agent work continues.
            return unknown_context("/", "source_unavailable")

    def health(self) -> dict[str, Any]:
        return {
            "status": "ok",
            "service": "borg-coordination-inbox",
            "version": 1,
            "generated_at": utc_now(),
            "uptime_seconds": round(max(0.0, time.monotonic() - self.started_at), 3),
        }

    def make_server(
        self,
        host: str = "127.0.0.1",
        port: int = DEFAULT_PORT,
        request_timeout: float | None = None,
    ) -> "HubHTTPServer":
        timeout = self.request_timeout if request_timeout is None else request_timeout
        return HubHTTPServer((host, int(port)), self, request_timeout=timeout)

    create_server = make_server


def _validate_operation_params(
    operation: Any,
    params: Any,
    request_id: Any,
) -> tuple[str, dict[str, Any], str | None]:
    if not isinstance(operation, str) or not operation or len(operation) > 256:
        raise TransportConfigurationError("invalid_operation", "Operation is required", 400)
    if not isinstance(params, dict):
        raise TransportConfigurationError("invalid_params", "Params must be an object", 400)
    if request_id is not None:
        if not isinstance(request_id, str) or not request_id:
            raise TransportConfigurationError("invalid_request_id", "Request ID is invalid", 400)
        if len(request_id.encode("utf-8")) > MAX_REQUEST_ID_BYTES:
            raise TransportConfigurationError("invalid_request_id", "Request ID is invalid", 400)
    if operation_requires_request_id(operation) and request_id is None:
        raise TransportConfigurationError(
            "request_id_required", "Mutating operations require request_id", 400
        )
    if is_browser_operation(operation):
        if operation not in BROWSER_OPERATIONS:
            raise TransportConfigurationError("invalid_operation", "Unsupported browser operation", 400)
        if "actor" in params:
            raise TransportConfigurationError("invalid_params", "Browser actor comes from authentication", 400)
        # Operation fields belong to the gateway; enforce JSON and total byte
        # bounds here without Inbox-specific body/limit semantics.
        try:
            raw = json.dumps({"operation": operation, "params": params, "request_id": request_id},
                             ensure_ascii=False, allow_nan=False).encode("utf-8")
        except (ValueError, TypeError, UnicodeError):
            raise TransportConfigurationError("invalid_params", "Params must be JSON compatible", 400) from None
        if len(raw) > MAX_REQUEST_BYTES:
            raise TransportConfigurationError("request_too_large", "Request exceeds 128 KiB", 413)
        return operation, params, request_id
    if operation == "estate.read":
        from .estate import validate_params
        validate_params(params, TransportConfigurationError)
        # Only the validated top-level estate limit has operation-specific bounds.
        _validate_json_limits({key: value for key, value in params.items() if key != "limit"})
    else:
        _validate_json_limits(params)
    return operation, params, request_id


def _validate_json_limits(value: Any, key: str | None = None) -> None:
    if isinstance(value, dict):
        for child_key, child_value in value.items():
            if not isinstance(child_key, str):
                raise TransportConfigurationError("invalid_params", "JSON object keys must be strings", 400)
            _validate_json_limits(child_value, child_key)
    elif isinstance(value, list):
        for child in value:
            _validate_json_limits(child, key)
    elif isinstance(value, str) and key == "body":
        if len(value.encode("utf-8")) > MAX_BODY_BYTES:
            raise TransportConfigurationError("body_too_large", "Message body exceeds 16 KiB", 413)
    elif isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
        raise TransportConfigurationError("invalid_params", "Non-finite numbers are not supported", 400)
    elif value is not None and not isinstance(value, (str, int, float, bool)):
        raise TransportConfigurationError("invalid_params", "Params are not JSON compatible", 400)
    if key == "limit" and isinstance(value, int) and not isinstance(value, bool):
        if value < 0 or value > MAX_LIMIT:
            raise TransportConfigurationError("limit_too_large", "Limit must be between 0 and 100", 400)


def _error_details(exc: BaseException, default_status: int = 500) -> tuple[int, dict[str, str]]:
    code = getattr(exc, "code", None)
    message = getattr(exc, "message", None)
    if not isinstance(code, str) or not code:
        code = "internal_error" if default_status >= 500 else "request_error"
    if not isinstance(message, str) or not message:
        message = "Internal service error" if default_status >= 500 else "Request rejected"
    status = getattr(exc, "status", default_status)
    if not isinstance(status, int) or status < 400 or status > 599:
        status = default_status
    return status, {"code": code, "message": message}


class HubHTTPServer(http.server.ThreadingHTTPServer):
    """Threaded stdlib server carrying one HubService instance."""

    allow_reuse_address = True
    daemon_threads = True

    def __init__(
        self,
        server_address: tuple[str, int],
        service: HubService,
        *,
        request_timeout: float = 10.0,
    ) -> None:
        self.service = service
        try:
            self.request_timeout = max(0.1, float(request_timeout))
        except (TypeError, ValueError) as exc:
            raise ValueError("request timeout is invalid") from exc
        super().__init__(server_address, HubRequestHandler)


class HubRequestHandler(http.server.BaseHTTPRequestHandler):
    """HTTP/JSON endpoint; deliberately no permissive CORS behavior."""

    protocol_version = "HTTP/1.1"
    server_version = "utlyze-inbox"
    sys_version = ""

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(self.server.request_timeout)  # type: ignore[attr-defined]

    @property
    def service(self) -> HubService:
        return self.server.service  # type: ignore[attr-defined,no-any-return]

    def log_message(self, format: str, *args: Any) -> None:
        # Do not inherit the default logger: request bodies and authorization
        # headers must never be placed in ordinary service logs.
        return

    def send_error(
        self,
        code: int,
        message: str | None = None,
        explain: str | None = None,
    ) -> None:
        # BaseHTTPRequestHandler otherwise emits an HTML page containing the
        # request target.  Keep all transport failures JSON and metadata-only.
        self.close_connection = True
        self._send_json(
            code,
            {"error": {"code": "http_error", "message": "HTTP request was rejected"}},
        )

    def _send_json(
        self,
        status: int,
        payload: Any,
        extra_headers: Mapping[str, str] | None = None,
    ) -> None:
        try:
            body = _json_dump_bytes(payload)
        except TransportConfigurationError:
            status = 500
            body = b'{"error":{"code":"internal_error","message":"Internal service error"}}'
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if self.close_connection:
            self.send_header("Connection", "close")
        if extra_headers:
            for name, value in extra_headers.items():
                self.send_header(name, value)
        self.end_headers()
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            self.wfile.write(body)

    def _send_error(self, exc: BaseException, default_status: int = 400) -> None:
        status, details = _error_details(exc, default_status)
        self._send_json(status, {"error": details})

    def _reject_origin(self) -> bool:
        origin = self.headers.get("Origin")
        if origin is not None:
            parsed = urlsplit(origin.strip())
            host = self.headers.get("Host", "")
            same_origin = (
                parsed.scheme == "http"
                and bool(parsed.netloc)
                and not parsed.path
                and not parsed.query
                and not parsed.fragment
                and parsed.netloc.lower() == host.lower()
            )
            if same_origin:
                return False
            self.close_connection = True
            self._send_json(403, {"error": {"code": "cors_rejected", "message": "Cross-origin requests are not supported"}})
            return True
        return False

    def _method_not_allowed(self, allow: str) -> None:
        self.close_connection = True
        self._send_json(
            405,
            {"error": {"code": "method_not_allowed", "message": "Method is not supported"}},
            {"Allow": allow},
        )

    def _authorization_actor(self) -> str | None:
        header = self.headers.get("Authorization")
        if not isinstance(header, str) or len(header) > MAX_CREDENTIAL_BYTES + 16:
            return None
        scheme, separator, token = header.partition(" ")
        if scheme.lower() != "bearer" or not separator or not token or " " in token:
            return None
        try:
            token = _validate_credential_value(token)
            return self.service.authenticate(token)
        except (HubError, UnicodeError, ValueError):
            return None

    def _read_request(self) -> dict[str, Any]:
        content_lengths = self.headers.get_all("Content-Length", [])
        transfer_encodings = self.headers.get_all("Transfer-Encoding", [])
        if len(content_lengths) > 1 or len(transfer_encodings) > 1:
            self.close_connection = True
            raise TransportConfigurationError(
                "duplicate_framing_header",
                "Duplicate request framing headers are not supported",
                400,
            )
        if content_lengths and transfer_encodings:
            self.close_connection = True
            raise TransportConfigurationError(
                "ambiguous_framing",
                "Content-Length and Transfer-Encoding cannot both be sent",
                400,
            )
        transfer_encoding = transfer_encodings[0] if transfer_encodings else None
        if transfer_encoding and transfer_encoding.lower() != "identity":
            self.close_connection = True
            raise TransportConfigurationError(
                "unsupported_transfer_encoding", "Chunked requests are not supported", 400
            )
        content_length = content_lengths[0] if content_lengths else None
        if content_length is None:
            self.close_connection = True
            raise TransportConfigurationError("content_length_required", "Content-Length is required", 411)
        try:
            length = int(content_length)
        except ValueError as exc:
            self.close_connection = True
            raise TransportConfigurationError("invalid_content_length", "Content-Length is invalid", 400) from exc
        if length < 0:
            self.close_connection = True
            raise TransportConfigurationError("invalid_content_length", "Content-Length is invalid", 400)
        if length > MAX_REQUEST_BYTES:
            self.close_connection = True
            raise TransportConfigurationError("request_too_large", "Request exceeds 128 KiB", 413)
        content_type = self.headers.get("Content-Type")
        media_type = content_type.split(";", 1)[0].strip().lower() if content_type else ""
        if media_type != "application/json" and not (
            media_type.startswith("application/") and media_type.endswith("+json")
        ):
            self.close_connection = True
            raise TransportConfigurationError(
                "json_content_type_required",
                "Content-Type application/json is required",
                415,
            )
        try:
            raw = self.rfile.read(length)
        except (socket.timeout, TimeoutError) as exc:
            self.close_connection = True
            raise TransportConfigurationError(
                "request_timeout", "Request body read timed out", 408
            ) from exc
        except OSError as exc:
            self.close_connection = True
            raise TransportConfigurationError(
                "request_read_failed", "Request body could not be read", 400
            ) from exc
        if len(raw) != length:
            self.close_connection = True
            raise TransportConfigurationError("incomplete_request", "Request body is incomplete", 400)
        try:
            text = raw.decode("utf-8")
            value = json.loads(text, parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise TransportConfigurationError("invalid_json", "Request body must be valid JSON", 400) from exc
        if not isinstance(value, dict):
            raise TransportConfigurationError("invalid_request", "Request body must be an object", 400)
        return value

    def do_GET(self) -> None:
        if self._reject_origin():
            return
        path = unquote(urlsplit(self.path).path)
        if path == "/health":
            self._send_json(200, self.service.health())
            return
        if path == "/v1/call":
            self._method_not_allowed("POST")
            return
        self._serve_static(path)

    def do_POST(self) -> None:
        if self._reject_origin():
            return
        path = urlsplit(self.path).path
        if path != "/v1/call":
            self.close_connection = True
            self._send_error(
                TransportConfigurationError("not_found", "Resource was not found", 404), 404
            )
            return
        actor = self._authorization_actor()
        if actor is None:
            self.close_connection = True
            self._send_json(401, {"error": {"code": "unauthorized", "message": "Authentication required"}})
            return
        try:
            request = self._read_request()
            operation = request.get("operation")
            if is_browser_operation(operation) and set(request) - {"operation", "params", "request_id"}:
                raise TransportConfigurationError("invalid_request", "Unsupported browser envelope fields", 400)
            params = request.get("params", {})
            request_id = request.get("request_id")
            operation, params, request_id = _validate_operation_params(operation, params, request_id)
            result = self.service.call(actor, operation, params, request_id)
            self._send_json(200, result)
        except HubError as exc:
            self._send_error(exc, 400)
        except (OSError, ValueError, TypeError) as exc:
            self._send_error(exc, 400)
        except Exception:
            self._send_json(500, {"error": {"code": "internal_error", "message": "Internal service error"}})

    def do_OPTIONS(self) -> None:
        if self._reject_origin():
            return
        self._method_not_allowed("GET, POST")

    def do_PUT(self) -> None:
        if self._reject_origin():
            return
        self._method_not_allowed("GET, POST")

    def do_DELETE(self) -> None:
        if self._reject_origin():
            return
        self._method_not_allowed("GET, POST")

    def do_HEAD(self) -> None:
        if self._reject_origin():
            return
        self._method_not_allowed("GET, POST")

    def _serve_static(self, request_path: str) -> None:
        if "\x00" in request_path:
            self._send_error(
                TransportConfigurationError("invalid_path", "Path is invalid", 400), 400
            )
            return
        if request_path == "/realm.json":
            # The realm manifest is owned by the UI peer and deliberately has
            # one fixed public route; it is not looked up inside web assets.
            root = self.service.web_root.parent
            relative = Path("realm.json")
        elif request_path in {"", "/", "/ui", "/ui/", "/web", "/web/"}:
            root = self.service.web_root
            relative = Path("index.html")
        elif request_path.startswith("/ui/"):
            root = self.service.web_root
            relative = Path(request_path[4:])
        elif request_path.startswith("/web/"):
            root = self.service.web_root
            relative = Path(request_path[5:])
        elif request_path.startswith("/"):
            root = self.service.web_root
            relative = Path(request_path[1:])
        else:
            root = self.service.web_root
            relative = Path(request_path)
        try:
            root_resolved = root.resolve()
            candidate = (root / relative).resolve()
            candidate.relative_to(root_resolved)
        except (OSError, ValueError):
            self._send_error(
                TransportConfigurationError("not_found", "Resource was not found", 404), 404
            )
            return
        if not candidate.is_file():
            self._send_error(
                TransportConfigurationError("not_found", "Resource was not found", 404), 404
            )
            return
        try:
            body = candidate.read_bytes()
        except OSError:
            self._send_error(
                TransportConfigurationError("static_unavailable", "Resource is unavailable", 500), 500
            )
            return
        content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            self.wfile.write(body)


def serve(
    state_dir: str | os.PathLike[str],
    *,
    host: str = "127.0.0.1",
    port: int = DEFAULT_PORT,
    store: Any | None = None,
    web_root: str | os.PathLike[str] | None = None,
) -> None:
    """Run the blocking HTTP service for a local state directory."""

    service = HubService.from_state_dir(state_dir, store=store, web_root=web_root)
    server = service.make_server(host, port)
    try:
        server.serve_forever()
    finally:
        server.server_close()


def create_server(
    service: HubService,
    host: str = "127.0.0.1",
    port: int = DEFAULT_PORT,
) -> HubHTTPServer:
    return service.make_server(host, port)


TransportServer = HubHTTPServer
HubServer = HubHTTPServer
RequestHandler = HubRequestHandler
Service = HubService
run_server = serve


__all__ = [
    "CredentialStore",
    "DEFAULT_ENDPOINT",
    "DEFAULT_PORT",
    "DEFAULT_OWNER_ACTOR",
    "HubError",
    "HubHTTPServer",
    "HubRequestHandler",
    "HubServer",
    "HubService",
    "InMemoryCredentialStore",
    "IssuedCredential",
    "MAX_BODY_BYTES",
    "MAX_LIMIT",
    "MAX_REQUEST_BYTES",
    "READ_ONLY_OPERATIONS",
    "RequestHandler",
    "Service",
    "TransportConfigurationError",
    "TransportServer",
    "credential_digest",
    "create_server",
    "enroll",
    "enroll_agent",
    "initialize",
    "initialize_state",
    "load_service_config",
    "operation_requires_request_id",
    "read_credential_file",
    "run_server",
    "serve",
    "utc_now",
    "write_credential_file",
]
