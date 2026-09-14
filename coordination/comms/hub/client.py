"""Stdlib HTTP client with a crash-safe local operation outbox."""

from __future__ import annotations

import contextlib
import http.client
import json
import os
from pathlib import Path
import secrets
import threading
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
import uuid

from .service import (
    MAX_LIMIT,
    MAX_REQUEST_BYTES,
    TransportConfigurationError,
    _atomic_write_json,
    _ensure_directory,
    _exclusive_file_lock,
    _json_load,
    _validate_operation_params,
    credential_digest,
    operation_requires_request_id,
    is_browser_operation,
    read_credential_file,
    utc_now,
    write_credential_file,
)


MAX_RESPONSE_BYTES = 4 * 1024 * 1024
DEFAULT_TIMEOUT_SECONDS = 10.0
BROWSER_TIMEOUT_SECONDS = 120.0
DEFAULT_FLUSH_ATTEMPTS = 3


class _NoRedirectHandler(HTTPRedirectHandler):
    """Keep bearer credentials on the configured origin only."""

    def redirect_request(self, *args: Any, **kwargs: Any):
        return None


_NO_REDIRECT_OPENER = build_opener(_NoRedirectHandler)
# Keep a patchable module-level seam for tests while making redirects explicit.
urlopen = _NO_REDIRECT_OPENER.open


class ClientError(Exception):
    """Safe, structured client-side failure."""

    def __init__(
        self,
        code: str,
        message: str,
        status: int = 0,
        *,
        request_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.request_id = request_id

    def as_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.status:
            value["status"] = self.status
        if self.request_id:
            value["request_id"] = self.request_id
        return value


class RemoteHubError(ClientError):
    """A JSON error returned by the authenticated hub."""


class TransportUnavailable(ClientError):
    """A failure where the server outcome is uncertain."""


class OutboxPending(ClientError):
    """A mutation was durably queued but could not yet be confirmed."""

    def __init__(self, request_id: str, outbox_path: Path) -> None:
        super().__init__(
            "outbox_pending",
            "Operation is durably queued for retry",
            request_id=request_id,
        )
        self.outbox_path = outbox_path


def _copy_json(value: Any) -> Any:
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ClientError("invalid_params", "Params must be JSON compatible") from exc


def _request_validation(
    operation: Any,
    params: Any,
    request_id: str | None,
) -> tuple[str, dict[str, Any], str | None]:
    try:
        operation, params, request_id = _validate_operation_params(operation, params, request_id)
    except Exception as exc:
        code = getattr(exc, "code", "invalid_request")
        message = getattr(exc, "message", "Request is invalid")
        status = getattr(exc, "status", 400)
        raise ClientError(str(code), str(message), int(status)) from exc
    payload: dict[str, Any] = {"operation": operation, "params": params}
    if request_id is not None:
        payload["request_id"] = request_id
    try:
        encoded_size = len(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode(
                "utf-8"
            )
        )
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ClientError("invalid_params", "Params must be JSON compatible") from exc
    if encoded_size > MAX_REQUEST_BYTES:
        raise ClientError("request_too_large", "Request exceeds 128 KiB")
    return operation, params, request_id


class Outbox:
    """A private JSON journal whose request IDs remain stable across restarts."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path).expanduser()
        self.lock_path = self.path.with_name(f".{self.path.name}.lock")
        self._thread_lock = threading.RLock()
        _ensure_directory(self.path.parent)
        if self.path.exists():
            with contextlib.suppress(OSError):
                os.chmod(self.path, 0o600)

    def _read_unlocked(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"version": 1, "entries": []}
        try:
            value = _json_load(self.path)
        except TransportConfigurationError as exc:
            raise ClientError("outbox_corrupt", "The local outbox is invalid") from exc
        if not isinstance(value, dict) or not isinstance(value.get("entries"), list):
            raise ClientError("outbox_corrupt", "The local outbox is invalid")
        entries = []
        for entry in value["entries"]:
            if isinstance(entry, dict) and isinstance(entry.get("request_id"), str):
                entries.append(entry)
        return {"version": 1, "entries": entries}

    def _write_unlocked(self, value: dict[str, Any]) -> None:
        try:
            _atomic_write_json(self.path, value, mode=0o600)
        except TransportConfigurationError as exc:
            raise ClientError("outbox_unavailable", "Unable to persist the local outbox") from exc

    @contextlib.contextmanager
    def _locked(self):
        with self._thread_lock, _exclusive_file_lock(self.lock_path):
            yield

    def enqueue(
        self,
        operation: str,
        params: dict[str, Any],
        request_id: str | None = None,
    ) -> str:
        if is_browser_operation(operation):
            raise ClientError("browser_sync_required", "Browser operations require synchronous dispatch")
        if request_id is None:
            request_id = str(uuid.uuid4())
        operation, params, request_id = _request_validation(operation, params, request_id)
        assert request_id is not None
        now = time.time()
        entry = {
            "request_id": request_id,
            "operation": operation,
            "params": _copy_json(params),
            "created_at": utc_now(),
            "attempts": 0,
            "next_attempt_at": now,
            "last_error": None,
        }
        with self._locked():
            value = self._read_unlocked()
            entries = value["entries"]
            for existing in entries:
                if existing.get("request_id") != request_id:
                    continue
                if (
                    existing.get("operation") == operation
                    and existing.get("params") == entry["params"]
                ):
                    return request_id
                raise ClientError(
                    "request_id_conflict",
                    "Request ID is already used for different content",
                    request_id=request_id,
                )
            entries.append(entry)
            self._write_unlocked(value)
        return request_id

    add = enqueue

    def pending(self, *, limit: int = MAX_LIMIT, now: float | None = None) -> list[dict[str, Any]]:
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 0:
            raise ClientError("invalid_limit", "Limit must be a non-negative integer")
        limit = min(limit, MAX_LIMIT)
        if limit == 0:
            return []
        now = time.time() if now is None else now
        with self._locked():
            value = self._read_unlocked()
            selected = []
            for entry in value["entries"]:
                next_attempt = entry.get("next_attempt_at", 0)
                if isinstance(next_attempt, (int, float)) and next_attempt > now:
                    continue
                selected.append(_copy_json(entry))
                if len(selected) >= limit:
                    break
            return selected

    def all(self) -> list[dict[str, Any]]:
        with self._locked():
            return [_copy_json(entry) for entry in self._read_unlocked()["entries"]]

    def record_attempt(
        self,
        request_id: str,
        *,
        error: dict[str, Any] | None = None,
        next_attempt_at: float | None = None,
        increment: bool = True,
    ) -> int:
        with self._locked():
            value = self._read_unlocked()
            for entry in value["entries"]:
                if entry.get("request_id") == request_id:
                    attempts = entry.get("attempts", 0)
                    if not isinstance(attempts, int) or attempts < 0:
                        attempts = 0
                    if increment:
                        attempts += 1
                    entry["attempts"] = attempts
                    entry["last_error"] = _copy_json(error) if error is not None else None
                    entry["next_attempt_at"] = (
                        time.time() if next_attempt_at is None else next_attempt_at
                    )
                    self._write_unlocked(value)
                    return attempts
        raise ClientError("outbox_missing", "Outbox entry was not found", request_id=request_id)

    def remove(self, request_id: str) -> bool:
        with self._locked():
            value = self._read_unlocked()
            before = len(value["entries"])
            value["entries"] = [
                entry for entry in value["entries"] if entry.get("request_id") != request_id
            ]
            if len(value["entries"]) != before:
                self._write_unlocked(value)
                return True
            return False

    def count(self) -> int:
        with self._locked():
            return len(self._read_unlocked()["entries"])


class InboxClient:
    """Authenticated client for the hub's single HTTP call endpoint."""

    def __init__(
        self,
        endpoint: str | None = None,
        credential_file: str | os.PathLike[str] | None = None,
        outbox_path: str | os.PathLike[str] | None = None,
        *,
        config_path: str | os.PathLike[str] | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        max_flush_attempts: int = DEFAULT_FLUSH_ATTEMPTS,
    ) -> None:
        config: dict[str, Any] = {}
        self.config_path = Path(config_path).expanduser().resolve() if config_path is not None else None
        config_parent = Path.cwd()
        if config_path is not None:
            target = Path(config_path).expanduser()
            try:
                value = _json_load(target)
            except (FileNotFoundError, TransportConfigurationError) as exc:
                raise ClientError("config_unavailable", "Unable to read client config") from exc
            if not isinstance(value, dict):
                raise ClientError("invalid_config", "Client config must be an object")
            config = value
            config_parent = target.parent
        self.config = config
        self.agent_id = config.get("agent_id") if isinstance(config.get("agent_id"), str) else None
        configured_state_dir = config.get("state_dir")
        if isinstance(configured_state_dir, str) and configured_state_dir:
            state_path = Path(configured_state_dir).expanduser()
            if not state_path.is_absolute():
                state_path = config_parent / state_path
            self.state_dir = state_path
        else:
            self.state_dir = None
        endpoint = endpoint or config.get("endpoint")
        if not isinstance(endpoint, str) or not endpoint:
            raise ClientError("invalid_endpoint", "Endpoint is required")
        self.endpoint = self._validate_endpoint(endpoint)
        credential_file = credential_file or config.get("credential_file") or config.get(
            "owner_credential_file"
        )
        if not isinstance(credential_file, (str, os.PathLike)):
            raise ClientError("credential_unavailable", "Credential file is required")
        credential_path = Path(credential_file).expanduser()
        if not credential_path.is_absolute():
            credential_path = config_parent / credential_path
        self.credential_file = credential_path
        self._token = read_credential_file(credential_path)
        configured_outbox = outbox_path or config.get("outbox_path")
        if configured_outbox is None:
            configured_outbox = credential_path.with_name(f".{credential_path.name}.outbox.json")
        outbox_target = Path(configured_outbox).expanduser()
        if not outbox_target.is_absolute():
            outbox_target = config_parent / outbox_target
        self.outbox = Outbox(outbox_target)
        try:
            self.timeout = max(0.1, float(timeout))
        except (TypeError, ValueError) as exc:
            raise ClientError("invalid_timeout", "Timeout is invalid") from exc
        try:
            self.max_flush_attempts = max(1, min(10, int(max_flush_attempts)))
        except (TypeError, ValueError) as exc:
            raise ClientError("invalid_retry_limit", "Retry limit is invalid") from exc

    @classmethod
    def from_config(
        cls,
        config_path: str | os.PathLike[str],
        **kwargs: Any,
    ) -> "InboxClient":
        return cls(config_path=config_path, **kwargs)

    @staticmethod
    def _validate_endpoint(endpoint: str) -> str:
        parsed = urlsplit(endpoint)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ClientError("invalid_endpoint", "Endpoint must be an HTTP(S) URL")
        if parsed.username is not None or parsed.password is not None:
            raise ClientError("invalid_endpoint", "Endpoint credentials are not supported")
        if parsed.query or parsed.fragment:
            raise ClientError("invalid_endpoint", "Endpoint query and fragment are not supported")
        path = parsed.path.rstrip("/")
        return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))

    @property
    def call_url(self) -> str:
        if self.endpoint.endswith("/v1/call"):
            return self.endpoint
        return f"{self.endpoint}/v1/call"

    def _post(
        self,
        operation: str,
        params: dict[str, Any],
        request_id: str | None,
    ) -> Any:
        operation, params, request_id = _request_validation(operation, params, request_id)
        payload: dict[str, Any] = {"operation": operation, "params": params}
        if request_id is not None:
            payload["request_id"] = request_id
        try:
            data = json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError, UnicodeError) as exc:
            raise ClientError("invalid_params", "Params must be JSON compatible") from exc
        if len(data) > MAX_REQUEST_BYTES:
            raise ClientError("request_too_large", "Request exceeds 128 KiB")
        request = Request(
            self.call_url,
            data=data,
            method="POST",
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json; charset=utf-8",
                "Authorization": f"Bearer {self._token}",
            },
        )
        try:
            with urlopen(request, timeout=BROWSER_TIMEOUT_SECONDS if is_browser_operation(operation) else self.timeout) as response:
                status = getattr(response, "status", None)
                if status is None:
                    status = response.getcode()
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except HTTPError as exc:
            raw = b""
            with contextlib.suppress(Exception):
                raw = exc.read(MAX_RESPONSE_BYTES + 1)
            with contextlib.suppress(Exception):
                exc.close()
            error = self._decode_error(raw, int(getattr(exc, "code", 0) or 0))
            if error.status in {408, 425, 429} or error.status >= 500:
                raise TransportUnavailable(
                    error.code,
                    error.message,
                    error.status,
                    request_id=request_id,
                ) from exc
            error.request_id = request_id
            raise error from exc
        except (URLError, TimeoutError, OSError, http.client.HTTPException, ValueError) as exc:
            raise TransportUnavailable(
                "transport_unavailable",
                "Unable to reach inbox service; operation remains retryable",
                request_id=request_id,
            ) from exc
        if len(raw) > MAX_RESPONSE_BYTES:
            raise TransportUnavailable(
                "response_too_large",
                "Inbox response exceeds the client limit",
                request_id=request_id,
            )
        if status < 200 or status >= 300:
            error = self._decode_error(raw, int(status))
            error.request_id = request_id
            raise error
        try:
            result = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise TransportUnavailable(
                "invalid_response",
                "Inbox returned an invalid JSON response",
                request_id=request_id,
            ) from exc
        return result

    @staticmethod
    def _decode_error(raw: bytes, status: int) -> RemoteHubError:
        code = "http_error"
        message = "Inbox service rejected the request"
        if raw:
            with contextlib.suppress(UnicodeDecodeError, json.JSONDecodeError):
                value = json.loads(raw.decode("utf-8"))
                if isinstance(value, dict) and isinstance(value.get("error"), dict):
                    detail = value["error"]
                    if isinstance(detail.get("code"), str) and detail["code"]:
                        code = detail["code"]
                    if isinstance(detail.get("message"), str) and detail["message"]:
                        message = detail["message"]
        return RemoteHubError(code, message, status)

    def call_sync(
        self,
        operation: str,
        params: dict[str, Any] | None = None,
        request_id: str | None = None,
    ) -> Any:
        """One authenticated attempt, without journaling or automatic retries."""
        params = {} if params is None else params
        if request_id is None and operation_requires_request_id(operation):
            request_id = str(uuid.uuid4())
        operation, params, request_id = _request_validation(operation, params, request_id)
        try:
            return self._post(operation, params, request_id)
        except TransportUnavailable as exc:
            raise TransportUnavailable(
                exc.code, "Synchronous operation outcome is uncertain; no automatic retry was made",
                exc.status, request_id=request_id,
            ) from None

    def call(
        self,
        operation: str,
        params: dict[str, Any] | None = None,
        request_id: str | None = None,
    ) -> Any:
        """Call an operation, journaling mutations before their first attempt."""

        if is_browser_operation(operation):
            return self.call_sync(operation, params, request_id)
        params = {} if params is None else params
        if not isinstance(operation, str):
            _request_validation(operation, params, request_id)
        if operation_requires_request_id(operation):
            if request_id is None:
                request_id = str(uuid.uuid4())
            request_id = self.outbox.enqueue(operation, params, request_id)
            report = self.flush(max_items=1, request_ids={request_id})
            for sent in report["sent"]:
                if sent["request_id"] == request_id:
                    return sent["result"]
            for failed in report["failed"]:
                if failed["request_id"] == request_id:
                    error = failed["error"]
                    raise RemoteHubError(
                        error["code"],
                        error["message"],
                        int(error.get("status", 0) or 0),
                        request_id=request_id,
                    )
            return {
                "queued": True,
                "request_id": request_id,
                "outbox_path": str(self.outbox.path),
            }
        return self._post(operation, params, request_id)

    send = call

    def enqueue(
        self,
        operation: str,
        params: dict[str, Any] | None = None,
        request_id: str | None = None,
    ) -> str:
        params = {} if params is None else params
        if request_id is None:
            request_id = str(uuid.uuid4())
        return self.outbox.enqueue(operation, params, request_id)

    queue = enqueue

    def register_child(
        self,
        agent_id: str,
        credential_file: str | os.PathLike[str],
        request_id: str | None = None,
    ) -> Any:
        """Create a local child token and send only its domain-separated digest."""

        if not isinstance(agent_id, str) or not agent_id:
            raise ClientError("invalid_agent_id", "agent_id is required")
        target = Path(credential_file).expanduser()
        if not target.is_absolute():
            target = Path.cwd() / target
        if target.exists():
            token = read_credential_file(target)
        else:
            token = secrets.token_urlsafe(32)
            write_credential_file(target, token, agent_id)
        digest = credential_digest(token)
        result = self.call(
            "credentials.register",
            {"agent_id": agent_id, "credential_digest": digest},
            request_id,
        )
        if isinstance(result, dict):
            response = dict(result)
            response.setdefault("agent_id", agent_id)
            response["credential_file"] = str(target.resolve())
            return response
        return result

    register_credential = register_child
    credentials_register = register_child
    issue_child_credential = register_child

    def flush(
        self,
        *,
        max_items: int = MAX_LIMIT,
        max_attempts: int | None = None,
        request_ids: set[str] | None = None,
    ) -> dict[str, Any]:
        """Retry a bounded batch; uncertain outcomes stay in the journal."""

        if not isinstance(max_items, int) or isinstance(max_items, bool) or max_items < 0:
            raise ClientError("invalid_limit", "max_items must be a non-negative integer")
        max_items = min(max_items, MAX_LIMIT)
        attempts_per_item = self.max_flush_attempts if max_attempts is None else max_attempts
        if not isinstance(attempts_per_item, int) or isinstance(attempts_per_item, bool):
            raise ClientError("invalid_retry_limit", "Retry limit is invalid")
        attempts_per_item = max(1, min(10, attempts_per_item))
        if request_ids is not None:
            # A targeted flush is used immediately after enqueue.  Do not let
            # unrelated older entries consume its bounded batch slot.
            now = time.time()
            entries = [
                entry
                for entry in self.outbox.all()
                if entry.get("request_id") in request_ids
                and not (
                    isinstance(entry.get("next_attempt_at"), (int, float))
                    and entry.get("next_attempt_at", 0) > now
                )
            ][:max_items]
        else:
            entries = self.outbox.pending(limit=max_items)
        sent: list[dict[str, Any]] = []
        failed: list[dict[str, Any]] = []
        pending: list[dict[str, Any]] = []
        for entry in entries:
            request_id = entry.get("request_id")
            operation = entry.get("operation")
            params = entry.get("params")
            if not isinstance(request_id, str) or not isinstance(operation, str) or not isinstance(params, dict):
                continue
            if is_browser_operation(operation):
                self.outbox.remove(request_id)
                failed.append({"request_id": request_id, "error": {
                    "code": "browser_sync_required",
                    "message": "Stale browser operation discarded without dispatch",
                }})
                continue
            resolved = False
            last_uncertain: ClientError | None = None
            for _ in range(attempts_per_item):
                try:
                    self.outbox.record_attempt(request_id)
                    result = self._post(operation, params, request_id)
                except RemoteHubError as exc:
                    # A non-retryable response is a known outcome.  Remove it
                    # so one bad operation cannot block later durable work.
                    self.outbox.remove(request_id)
                    failed.append({"request_id": request_id, "error": exc.as_dict()})
                    resolved = True
                    break
                except ClientError as exc:
                    last_uncertain = exc
                    self.outbox.record_attempt(request_id, error=exc.as_dict(), increment=False)
                    continue
                else:
                    self.outbox.remove(request_id)
                    sent.append({"request_id": request_id, "result": result})
                    resolved = True
                    break
            if not resolved:
                current = next(
                    (value for value in self.outbox.all() if value.get("request_id") == request_id),
                    None,
                )
                if current is not None:
                    pending.append(
                        {
                            "request_id": request_id,
                            "attempts": current.get("attempts", 0),
                            "error": last_uncertain.as_dict() if last_uncertain else None,
                        }
                    )
        return {
            "sent": sent,
            "failed": failed,
            "pending": pending,
            "remaining": self.outbox.count(),
        }

    retry = flush

    def pending(self) -> list[dict[str, Any]]:
        return self.outbox.all()

    def health(self) -> Any:
        health_base = self.endpoint.removesuffix("/v1/call")
        request = Request(f"{health_base}/health", method="GET", headers={"Accept": "application/json"})
        try:
            with urlopen(request, timeout=self.timeout) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except (URLError, TimeoutError, OSError, http.client.HTTPException) as exc:
            raise TransportUnavailable("transport_unavailable", "Unable to reach inbox service") from exc
        if len(raw) > MAX_RESPONSE_BYTES:
            raise TransportUnavailable("response_too_large", "Inbox response exceeds the client limit")
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise TransportUnavailable("invalid_response", "Inbox returned invalid health JSON") from exc

    def close(self) -> None:
        # Retained for context-manager parity; urlopen responses are scoped.
        return

    def __enter__(self) -> "InboxClient":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()


Client = InboxClient
HubClient = InboxClient


__all__ = [
    "Client",
    "ClientError",
    "DEFAULT_FLUSH_ATTEMPTS",
    "DEFAULT_TIMEOUT_SECONDS",
    "HubClient",
    "InboxClient",
    "MAX_RESPONSE_BYTES",
    "Outbox",
    "OutboxPending",
    "RemoteHubError",
    "TransportUnavailable",
]
