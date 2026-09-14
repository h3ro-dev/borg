"""Marker-only, deterministic retention candidate for an injected Qdrant API."""

from __future__ import annotations

import copy
from contextlib import contextmanager
from datetime import date, datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import time
from typing import Any, Callable, Iterable, Mapping

SCHEMA = "mem0-retention-marker-v1"
MARKER_KEY = "retention_marker"
RETIRED_STATUSES = frozenset({"tombstoned", "decayed", "retired"})
RETIREMENT_STATUS_FIELDS = (
    "status", "lifecycle_status", "memory_status", "retention_status",
)
RETIREMENT_FLAG_FIELDS = ("retired", "is_retired", "tombstoned")
EXPIRY_FIELDS = ("expiration_date", "expires_at", "valid_until")
MUTABLE_CLASSES = frozenset({"transient", "ephemeral", "noise", "unsupported"})
DURABLE_KINDS = frozenset({"decision", "preference", "constraint", "procedure"})
SUPPORTED_SOURCES = frozenset({"codex", "claude", "grok", "hermes"})
SOURCE_ID_FIELDS = (
    "source", "run_id", "identity_sha256", "source_system", "source_event_id",
    "source_version", "merged_from", "capture_digest", "transcript_path", "project",
    "thread_date",
)
COMPATIBILITY_FIELDS = (
    *RETIREMENT_STATUS_FIELDS, *RETIREMENT_FLAG_FIELDS, "retired_at", "kind", "role",
    "attributed_to", "authority", "authority_class", "retention_class",
    "retention_intent", "valid_at", "invalid_at", "supersedes_version", "merge_disputed",
)
METADATA_FIELDS = (
    "data", "hash", "scope", "user_id", "is_canary", "canary",
    *COMPATIBILITY_FIELDS, *EXPIRY_FIELDS, "agent_id", "actor_id", *SOURCE_ID_FIELDS,
    "provenance",
)
DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z", re.ASCII)
HEX64_RE = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
HTTP_TIMEOUT_S = 30.0
Http = Callable[[str, str, dict[str, Any], float], dict[str, Any]]
_MISSING = object()
_CONFLICT = object()
_INVALID = object()


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _containers(value: Any) -> Iterable[dict[str, Any]]:
    pending = [value]
    seen: set[int] = set()
    while pending:
        item = pending.pop(0)
        if not isinstance(item, dict) or id(item) in seen:
            continue
        seen.add(id(item))
        yield item
        for key in ("payload", "metadata"):
            nested = item.get(key)
            if isinstance(nested, dict):
                pending.append(nested)


def _field(value: dict[str, Any], key: str) -> tuple[Any, bool]:
    values = [container[key] for container in _containers(value) if key in container]
    if not values:
        return _MISSING, False
    first = values[0]
    return (_CONFLICT, True) if any(item != first for item in values[1:]) else (first, True)


def _payload(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    nested = value.get("payload")
    return nested if isinstance(nested, dict) else value


def _valid_digest(value: Any) -> bool:
    return isinstance(value, str) and HEX64_RE.fullmatch(value) is not None


def _scope_value(value: dict[str, Any]) -> str | None:
    scope, present = _field(value, "scope")
    return scope if present and isinstance(scope, str) and scope.strip() else None


def _user_value(value: dict[str, Any]) -> str | None:
    user, present = _field(value, "user_id")
    return user if present and isinstance(user, str) and user.strip() else None


def _data_value(value: dict[str, Any]) -> str | None:
    data, present = _field(value, "data")
    return data if present and isinstance(data, str) else None


def _data_fingerprint(value: dict[str, Any]) -> str | None:
    data = _data_value(value)
    return hashlib.sha256(data.encode("utf-8")).hexdigest() if data is not None else None


def _valid_point_id(value: Any) -> bool:
    return (
        (isinstance(value, int) and not isinstance(value, bool))
        or (isinstance(value, str) and bool(value.strip()))
    )


def _metadata(point: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(point.get("payload"), dict):
        return None
    result: dict[str, Any] = {}
    for key in METADATA_FIELDS:
        value, present = _field(point, key)
        if value is _CONFLICT:
            return None
        if present:
            result[key] = copy.deepcopy(value)
    try:
        _canonical(result)
    except (TypeError, ValueError):
        return None
    return result


def source_payload_fingerprint(payload: dict[str, Any]) -> str:
    """Hash all source payload bytes except this candidate's owned marker."""
    source = copy.deepcopy(_payload(payload))
    if source is None:
        raise TypeError("payload must be a mapping")
    source.pop(MARKER_KEY, None)
    return _digest(source)


def _valid_marker(marker: Any) -> bool:
    if not isinstance(marker, dict) or marker.get("schema") != SCHEMA:
        return False
    if set(marker) - {
        "schema", "reason", "source_fingerprint", "canonical_source_fingerprint",
        "data_fingerprint", "scope", "user_id", "canonical_id", "source_ids",
        "source_records", "created_at",
    }:
        return False
    if marker.get("reason") not in {"explicit_noise", "explicit_expiry", "exact_duplicate"}:
        return False
    if not _valid_digest(marker.get("source_fingerprint")):
        return False
    if not isinstance(marker.get("created_at"), str) or not marker["created_at"].strip():
        return False
    try:
        datetime.fromisoformat(marker["created_at"])
    except ValueError:
        return False
    source_ids = marker.get("source_ids")
    if (not isinstance(source_ids, list) or not source_ids
            or not all(_valid_point_id(value) for value in source_ids)
            or len({str(value) for value in source_ids}) != len(source_ids)):
        return False
    if not isinstance(marker.get("source_records"), list) or not marker["source_records"]:
        return False
    record_ids = []
    for record in marker["source_records"]:
        if not isinstance(record, dict) or not _valid_point_id(record.get("point_id")):
            return False
        try:
            _canonical(record)
        except (TypeError, ValueError):
            return False
        record_ids.append(str(record["point_id"]))
    if len(set(record_ids)) != len(record_ids) or set(map(str, source_ids)) != set(record_ids):
        return False
    scope = marker.get("scope")
    if not isinstance(scope, str) or not scope.strip():
        return False
    if "data_fingerprint" in marker and not _valid_digest(marker["data_fingerprint"]):
        return False
    if "user_id" in marker and (not isinstance(marker["user_id"], str) or not marker["user_id"].strip()):
        return False
    if marker["reason"] != "exact_duplicate":
        return not any(key in marker for key in ("canonical_id", "canonical_source_fingerprint"))
    return (
        _valid_point_id(marker.get("canonical_id"))
        and _valid_digest(marker.get("canonical_source_fingerprint"))
        and _valid_digest(marker.get("data_fingerprint"))
        and isinstance(marker.get("user_id"), str)
        and bool(marker["user_id"].strip())
        and len(source_ids) >= 2
        and str(marker["canonical_id"]) in set(map(str, source_ids))
    )


def _compatible_payloads(left: dict[str, Any], right: dict[str, Any]) -> bool:
    if _scope_value(left) is None or _scope_value(left) != _scope_value(right):
        return False
    if _user_value(left) is None or _user_value(left) != _user_value(right):
        return False
    left_data, right_data = _data_value(left), _data_value(right)
    if left_data is None or left_data != right_data:
        return False
    for field in COMPATIBILITY_FIELDS:
        left_value, left_present = _field(left, field)
        right_value, right_present = _field(right, field)
        if left_value is _CONFLICT or right_value is _CONFLICT:
            return False
        if left_present != right_present:
            return False
        if left_present:
            if isinstance(left_value, str):
                left_value = left_value.strip().casefold()
            if isinstance(right_value, str):
                right_value = right_value.strip().casefold()
            try:
                if _canonical(left_value) != _canonical(right_value):
                    return False
            except (TypeError, ValueError):
                return False
    left_has_expiry = any(_field(left, field)[1] for field in EXPIRY_FIELDS)
    right_has_expiry = any(_field(right, field)[1] for field in EXPIRY_FIELDS)
    if left_has_expiry != right_has_expiry:
        return False
    left_expiry = _expiry_compatibility(left)
    right_expiry = _expiry_compatibility(right)
    if left_expiry is _INVALID or right_expiry is _INVALID:
        return False
    return left_expiry is None or right_expiry is None or left_expiry == right_expiry


def marker_is_active(
    payload: dict[str, Any],
    canonical_payload: dict[str, Any] | None = None,
    *,
    canonical_lookup: Callable[[str], dict[str, Any] | None] | None = None,
    as_of: date | None = None,
) -> bool:
    """Return whether a marker is active, proving a duplicate canonical when required."""
    source = _payload(payload)
    if source is None:
        return False
    marker = source.get(MARKER_KEY)
    if not _valid_marker(marker):
        return False
    try:
        if marker["source_fingerprint"] != source_payload_fingerprint(source):
            return False
        if isinstance(payload, dict) and isinstance(payload.get("payload"), dict):
            point_id = payload.get("id")
            if not _valid_point_id(point_id) or str(point_id) not in {
                str(value) for value in marker["source_ids"]
            }:
                return False
            if marker["reason"] == "exact_duplicate" and str(point_id) == str(marker["canonical_id"]):
                return False
        if marker.get("scope") != _scope_value(source):
            return False
        if "data_fingerprint" in marker and marker["data_fingerprint"] != _data_fingerprint(source):
            return False
        if "user_id" in marker and marker["user_id"] != _user_value(source):
            return False
        if not _is_live(source):
            return False
        if marker["reason"] != "exact_duplicate":
            classification = _classification(source)
            digest, digest_present = _field(source, "hash")
            if (classification is None or not digest_present or not isinstance(digest, str)
                    or not digest.strip() or _kind_is_durable(source)):
                return False
            if marker["reason"] == "explicit_noise":
                if classification[0] not in {"noise", "unsupported"}:
                    return False
            elif (classification[0] not in {"transient", "ephemeral"}
                  or _expiry(source, as_of or date.today())[0] != "expired"):
                return False
            return True
        if canonical_payload is None and canonical_lookup is not None:
            canonical_payload = canonical_lookup(str(marker["canonical_id"]))
        canonical = _payload(canonical_payload)
        if canonical is None or MARKER_KEY in canonical:
            return False
        canonical_id = (
            canonical_payload.get("id")
            if isinstance(canonical_payload, dict) and isinstance(canonical_payload.get("payload"), dict)
            else None
        )
        if canonical_id is not None:
            if not _valid_point_id(canonical_id) or str(canonical_id) != str(marker["canonical_id"]):
                return False
        if not _is_live(canonical):
            return False
        if marker["canonical_source_fingerprint"] != source_payload_fingerprint(canonical):
            return False
        if marker["data_fingerprint"] != _data_fingerprint(canonical):
            return False
        return _compatible_payloads(source, canonical)
    except (TypeError, ValueError, KeyError):
        return False
    except Exception:
        return False


def _source_identity(payload: dict[str, Any]) -> tuple[Any, ...] | None:
    triple = [_field(payload, key) for key in ("source_system", "source_event_id", "source_version")]
    if any(present for _value, present in triple):
        values = [value for value, present in triple]
        if (all(isinstance(value, str) and value.strip() for value in values)
                and values[0] in SUPPORTED_SOURCES and HEX64_RE.fullmatch(values[2])):
            return ("source_event", *[value.strip() for value in values])
        return None
    identity, present = _field(payload, "identity_sha256")
    if present:
        if isinstance(identity, str) and HEX64_RE.fullmatch(identity):
            return ("identity", identity)
        return None
    source, source_present = _field(payload, "source")
    run_id, run_present = _field(payload, "run_id")
    if source_present or run_present:
        actor = []
        for key in ("user_id", "agent_id", "actor_id"):
            value, present = _field(payload, key)
            if present:
                if not isinstance(value, str) or not value.strip():
                    return None
                actor.append((key, value.strip()))
        if (isinstance(source, str) and source.strip() and isinstance(run_id, str)
                and run_id.strip() and actor):
            return ("legacy_run", source.strip(), run_id.strip(), tuple(actor))
        return None
    return None


def _classification(point: dict[str, Any]) -> tuple[str, str | None] | None:
    value, present = _field(point, "retention_class")
    if not present:
        return None
    if value is _CONFLICT or not isinstance(value, str):
        return None
    retention_class = value.strip().casefold()
    allowed_classes = MUTABLE_CLASSES | {"durable"}
    if retention_class not in allowed_classes:
        return None
    intent, intent_present = _field(point, "retention_intent")
    if intent is _CONFLICT or (intent_present and not isinstance(intent, str)):
        return None
    intent_value = intent.strip().casefold() if intent_present else None
    allowed = {
        "transient": {None, "", "transient", "expire", "expired"},
        "ephemeral": {None, "", "transient", "expire", "expired"},
        "noise": {None, "", "noise", "unsupported", "discard"},
        "unsupported": {None, "", "noise", "unsupported", "discard"},
        "durable": {None, "", "admission", "preserve"},
    }
    return (retention_class, intent_value) if intent_value in allowed[retention_class] else None


def _expiry(point: dict[str, Any], as_of: date) -> tuple[str, tuple[Any, ...]]:
    values: list[Any] = []
    key_values: list[Any] = []
    for field in EXPIRY_FIELDS:
        value, present = _field(point, field)
        if value is _CONFLICT:
            return "conflict", ()
        if present:
            values.append(value)
            key_values.append((field, value))
    if not values:
        return "unknown", ()
    if any(not isinstance(value, str) or not DATE_RE.fullmatch(value) for value in values):
        return "unknown", tuple(key_values)
    try:
        parsed = [date.fromisoformat(value) for value in values]
    except ValueError:
        return "unknown", tuple(key_values)
    if len(set(parsed)) != 1:
        return "conflict", tuple(key_values)
    return ("expired" if parsed[0] < as_of else "active"), tuple(key_values)


def _expiry_compatibility(point: dict[str, Any]) -> str | None | object:
    """Return one normalized validity date, no validity, or an invalid sentinel."""
    values: list[date] = []
    for field in EXPIRY_FIELDS:
        value, present = _field(point, field)
        if not present:
            continue
        if value is _CONFLICT or not isinstance(value, str) or not DATE_RE.fullmatch(value):
            return _INVALID
        try:
            values.append(date.fromisoformat(value))
        except ValueError:
            return _INVALID
    if not values:
        return None
    if len(set(values)) != 1:
        return _INVALID
    return values[0].isoformat()


def _is_live(point: dict[str, Any]) -> bool:
    for key in RETIREMENT_STATUS_FIELDS:
        status, present = _field(point, key)
        if status is _CONFLICT or (present and not isinstance(status, str)):
            return False
        if isinstance(status, str) and status.strip().casefold() in RETIRED_STATUSES:
            return False
    for key in RETIREMENT_FLAG_FIELDS:
        retired, present = _field(point, key)
        if retired is _CONFLICT or (present and not isinstance(retired, bool)):
            return False
        if retired is True:
            return False
    retired_at, present = _field(point, "retired_at")
    if retired_at is _CONFLICT or (present and retired_at not in (None, "", False)):
        return False
    for key in ("is_canary", "canary"):
        value, present = _field(point, key)
        if value is _CONFLICT or (present and (not isinstance(value, bool) or value)):
            return False
    return True


def _kind_is_durable(point: dict[str, Any]) -> bool:
    value, present = _field(point, "kind")
    return value is _CONFLICT or bool(
        present
        and (
            not isinstance(value, str)
            or value.strip().casefold() in DURABLE_KINDS
        )
    )


def _source_record(point: dict[str, Any]) -> dict[str, Any] | None:
    """Keep compact, complete source identifiers without copying source payloads."""
    raw_id = point.get("id") if isinstance(point, dict) else None
    if not _valid_point_id(raw_id):
        return None
    record: dict[str, Any] = {"point_id": str(raw_id)}
    for field in (*SOURCE_ID_FIELDS, "user_id", "agent_id", "actor_id"):
        value, present = _field(point, field)
        if not present:
            continue
        if value is _CONFLICT:
            return None
        try:
            _canonical(value)
        except (TypeError, ValueError):
            return None
        record[field] = copy.deepcopy(value)
    return record


def _source_quality(point: dict[str, Any]) -> int:
    """Lower ranks are preferred as duplicate canonicals."""
    payload = _payload(point) or {}
    identity = _source_identity(payload)
    if identity is not None and identity[0] in {"source_event", "identity"}:
        return 0
    transcript_path, transcript_present = _field(point, "transcript_path")
    source, source_present = _field(point, "source")
    transcript_hint = (
        transcript_present
        and isinstance(transcript_path, str)
        and transcript_path.strip()
    ) or (
        source_present
        and isinstance(source, str)
        and ("transcript" in source.casefold() or ".jsonl" in source.casefold())
    )
    if transcript_hint:
        return 1
    if source_present and isinstance(source, str) and source.strip().casefold() == "consolidate":
        return 3
    if identity is not None:
        return 2
    return 4


def _record(point: dict[str, Any], as_of: date) -> dict[str, Any] | None:
    payload = point.get("payload") if isinstance(point, dict) else None
    if not isinstance(payload, dict) or not _valid_point_id(point.get("id")) or not _is_live(point):
        return None
    if MARKER_KEY in payload:
        return None
    metadata = _metadata(point)
    if metadata is None:
        return None
    scope = _scope_value(point)
    digest, digest_present = _field(point, "hash")
    if scope is None or digest is _CONFLICT or (digest_present and
                                                (not isinstance(digest, str) or not digest.strip())):
        return None
    data, data_present = _field(point, "data")
    if data is _CONFLICT or (data_present and not isinstance(data, str)):
        return None
    user, user_present = _field(point, "user_id")
    if user is _CONFLICT or (user_present and not isinstance(user, str)):
        return None
    classification = _classification(point)
    expiry, expiry_key = _expiry(point, as_of)
    retention_class = classification[0] if classification else None
    class_value, class_present = _field(point, "retention_class")
    if class_present and classification is None:
        return None
    if expiry in {"conflict"} or (expiry == "unknown" and expiry_key):
        return None
    kind, kind_present = _field(point, "kind")
    if kind_present and (kind is _CONFLICT or not isinstance(kind, str)):
        return None
    source_record = _source_record(point)
    if source_record is None:
        return None
    try:
        source_fingerprint = source_payload_fingerprint(payload)
    except (TypeError, ValueError):
        return None
    return {
        "id": str(point["id"]), "scope": scope, "hash": digest.strip() if digest_present else None,
        "data": data if data_present else None, "data_fingerprint": _data_fingerprint(payload),
        "user_id": user if user_present else None,
        "class": retention_class,
        "intent": classification[1] if classification else None, "expiry": expiry,
        "expiry_key": expiry_key, "kind": kind if kind_present and isinstance(kind, str) else None,
        "metadata_sha256": _digest(metadata), "source_fingerprint": source_fingerprint,
        "source_record": source_record, "source_quality": _source_quality(point),
        "payload": copy.deepcopy(payload),
    }


def _proposal(record: dict[str, Any], reason: str, *, canonical_id: str | None = None,
              source_ids: list[str] | None = None, source_records: list[dict[str, Any]] | None = None,
              canonical_record: dict[str, Any] | None = None) -> dict[str, Any]:
    complete_source_records = copy.deepcopy(source_records or [record["source_record"]])
    complete_source_records.sort(key=lambda row: str(row["point_id"]))
    result = {
        "id": record["id"], "reason": reason, "canonical_id": canonical_id,
        "source_ids": sorted(source_ids or [record["id"]]),
        "expected_metadata_sha256": record["metadata_sha256"],
        "expected_source_fingerprint": record["source_fingerprint"],
        "scope": record["scope"], "data_fingerprint": record["data_fingerprint"],
        "user_id": record["user_id"],
        "source_records": complete_source_records,
    }
    if canonical_record is not None:
        result.update({
            "canonical_expected_metadata_sha256": canonical_record["metadata_sha256"],
            "canonical_expected_source_fingerprint": canonical_record["source_fingerprint"],
        })
    return result


def plan_retention(points: Iterable[dict[str, Any]], *, as_of: date) -> dict[str, Any]:
    direct: list[dict[str, Any]] = []
    eligible: list[dict[str, Any]] = []
    kept: list[str] = []
    reasons: dict[str, int] = {}
    for point in points:
        raw_id = point.get("id") if isinstance(point, dict) else None
        pid = str(raw_id) if _valid_point_id(raw_id) else None
        record = _record(point, as_of) if pid else None
        if record is None:
            if pid:
                kept.append(pid)
            continue
        classification = _classification(point)
        retention_class = record["class"]
        durable_kind = _kind_is_durable(point)
        if (classification and retention_class in {"noise", "unsupported"}
                and not durable_kind and record["hash"] is not None):
            direct.append(_proposal(record, "explicit_noise"))
        elif (classification and retention_class in {"transient", "ephemeral"}
              and record["expiry"] == "expired" and not durable_kind
              and record["hash"] is not None):
            direct.append(_proposal(record, "explicit_expiry"))
        elif record["data_fingerprint"] is not None and record["user_id"] is not None:
            eligible.append(record)
        else:
            kept.append(record["id"])
    groups: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    for record in eligible:
        key = (record["scope"], record["user_id"], record["data_fingerprint"])
        groups.setdefault(key, []).append(record)
    duplicate: list[dict[str, Any]] = []
    for candidates in groups.values():
        clusters: list[list[dict[str, Any]]] = []
        for record in sorted(candidates, key=lambda row: (row["source_quality"], row["id"])):
            for cluster in clusters:
                if all(_compatible_payloads(record["payload"], member["payload"])
                       for member in cluster):
                    cluster.append(record)
                    break
            else:
                clusters.append([record])
        for members in clusters:
            members.sort(key=lambda row: (row["source_quality"], row["id"]))
            winner = members[0]
            kept.append(winner["id"])
            if len(members) == 1:
                continue
            ids = [member["id"] for member in members]
            source_records = [member["source_record"] for member in members]
            for member in members[1:]:
                duplicate.append(_proposal(
                    member, "exact_duplicate", canonical_id=winner["id"], source_ids=ids,
                    source_records=source_records, canonical_record=winner,
                ))
    entries = sorted(direct + duplicate, key=lambda item: item["id"])
    for entry in entries:
        reasons[entry["reason"]] = reasons.get(entry["reason"], 0) + 1
    return {"schema": SCHEMA, "entries": entries, "kept": sorted(set(kept)), "reasons": reasons}


@contextmanager
def retention_lock(lock_path: Path):
    """Own one cross-process lock for marker operations only."""
    path = Path(lock_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _path(collection: str, suffix: str) -> str:
    if not collection or "/" in collection:
        raise ValueError("invalid collection")
    return f"/collections/{collection}/points{suffix}"


def _call(http: Http, method: str, path: str, body: dict[str, Any], deadline: float) -> dict[str, Any]:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("retention deadline")
    return http(method, path, body, min(HTTP_TIMEOUT_S, remaining))


def _retrieve(http: Http, collection: str, pid: str, deadline: float) -> dict[str, Any] | None:
    result = _call(http, "POST", _path(collection, ""),
                   {"ids": [pid], "with_payload": True, "with_vector": False}, deadline)
    points = result.get("result") if isinstance(result, dict) else None
    if not isinstance(points, list):
        raise ValueError("malformed-retrieve")
    return next((point for point in points if isinstance(point, dict) and str(point.get("id")) == pid
                 and isinstance(point.get("payload"), dict)), None)


def _append(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as handle:
        os.fchmod(handle.fileno(), 0o600)
        handle.write(_canonical(record) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _journal(path: Path) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    if not path.exists():
        return latest
    for line in path.read_text(encoding="utf-8").splitlines():
        record = json.loads(line)
        if not isinstance(record, dict) or record.get("schema") != SCHEMA:
            raise ValueError("malformed-journal")
        pid = str(record.get("point_id") or "")
        if not pid:
            raise ValueError("malformed-journal-point")
        if record.get("event") == "prepare":
            latest[pid] = record
        elif record.get("event") == "state" and pid in latest:
            latest[pid]["state"] = record.get("state")
    return latest


def _state(path: Path, pid: str, state: str, reason: str | None = None) -> None:
    record: dict[str, Any] = {"schema": SCHEMA, "event": "state", "point_id": pid, "state": state}
    if reason:
        record["reason"] = reason
    _append(path, record)


def _marker(
    entry: dict[str, Any],
    fingerprint: str,
    created_at: str,
    *,
    canonical_fingerprint: str | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "schema": SCHEMA, "reason": entry["reason"], "source_fingerprint": fingerprint,
        "source_ids": sorted(str(value) for value in (entry.get("source_ids") or [entry["point_id"]])),
        "source_records": copy.deepcopy(entry.get("source_records") or
                                         [{"point_id": str(entry["point_id"])}]),
        "scope": entry.get("scope"), "created_at": created_at,
    }
    if entry.get("data_fingerprint") is not None:
        result["data_fingerprint"] = entry["data_fingerprint"]
    if entry.get("user_id") is not None:
        result["user_id"] = entry["user_id"]
    if entry.get("canonical_id") is not None:
        result["canonical_id"] = entry["canonical_id"]
        result["canonical_source_fingerprint"] = (
            canonical_fingerprint or entry.get("canonical_expected_source_fingerprint")
        )
    return result


def _prepare_entry(
    proposal: dict[str, Any], point_id: str, fingerprint: str, marker: dict[str, Any]
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "schema": SCHEMA,
        "event": "prepare",
        "state": "prepared",
        "point_id": point_id,
        "reason": proposal.get("reason"),
        "source_fingerprint": fingerprint,
        "after_marker": copy.deepcopy(marker),
        "source_ids": copy.deepcopy(marker.get("source_ids")),
        "source_records": copy.deepcopy(marker.get("source_records")),
    }
    for key in ("scope", "data_fingerprint", "user_id", "canonical_id",
                "canonical_source_fingerprint"):
        if key in marker:
            entry[key] = copy.deepcopy(marker[key])
    return entry


def _apply_prepared(http: Http, collection: str, journal: Path, entry: dict[str, Any],
                    deadline: float, max_mutations: int, mutations: int) -> tuple[str, int]:
    pid = str(entry["point_id"])
    after_marker = entry.get("after_marker")
    if not _valid_marker(after_marker):
        _state(journal, pid, "conflict", "invalid-marker")
        return "conflict", mutations
    current = _retrieve(http, collection, pid, deadline)
    if current is None:
        _state(journal, pid, "conflict", "missing-point")
        return "conflict", mutations
    current_marker = current["payload"].get(MARKER_KEY)
    if current_marker == after_marker:
        if entry.get("reason") == "exact_duplicate":
            canonical = _retrieve(http, collection, str(after_marker["canonical_id"]), deadline)
            active = canonical is not None and marker_is_active(current, canonical)
        else:
            active = marker_is_active(current)
        if active:
            _state(journal, pid, "applied")
            return "applied", mutations
        _state(journal, pid, "conflict", "marker-not-active")
        return "conflict", mutations
    if current_marker is not None:
        _state(journal, pid, "conflict", "marker-changed")
        return "conflict", mutations
    if source_payload_fingerprint(current["payload"]) != entry["source_fingerprint"]:
        _state(journal, pid, "conflict", "source-changed-before-retry")
        return "conflict", mutations
    if entry.get("reason") == "exact_duplicate":
        marker = after_marker
        canonical_id = marker.get("canonical_id") if isinstance(marker, dict) else None
        canonical = (_retrieve(http, collection, str(canonical_id), deadline)
                     if _valid_point_id(canonical_id) else None)
        if canonical is None or not marker_is_active({**current, "payload": {
                **current["payload"], MARKER_KEY: marker}}, canonical):
            _state(journal, pid, "conflict", "canonical-not-proven")
            return "conflict", mutations
    if mutations >= max_mutations:
        return "limit", mutations
    mutations += 1
    _call(http, "POST", _path(collection, "/payload?wait=true"),
          {"points": [pid], "payload": {MARKER_KEY: after_marker}}, deadline)
    checked = _retrieve(http, collection, pid, deadline)
    if checked is not None and checked["payload"].get(MARKER_KEY) == after_marker:
        if entry.get("reason") == "exact_duplicate":
            canonical_id = after_marker["canonical_id"]
            canonical = (_retrieve(http, collection, str(canonical_id), deadline)
                         if _valid_point_id(canonical_id) else None)
            if canonical is not None and marker_is_active(checked, canonical):
                _state(journal, pid, "applied")
                return "applied", mutations
            _state(journal, pid, "conflict", "canonical-not-proven")
            return "conflict", mutations
        _state(journal, pid, "applied")
        return "applied", mutations
    _state(journal, pid, "conflict", "marker-after-mismatch")
    return "conflict", mutations


def validate_canonical_batch(
    marked_points: Iterable[dict[str, Any]],
    canonical_points: Mapping[str, dict[str, Any]],
) -> dict[str, bool]:
    """Validate duplicate markers using full Qdrant points from one lookup batch."""
    lookup: dict[str, dict[str, Any]] = {}
    if isinstance(canonical_points, Mapping):
        for point_id, point in canonical_points.items():
            if (isinstance(point, dict) and isinstance(point.get("payload"), dict)
                    and _valid_point_id(point.get("id"))
                    and str(point["id"]) == str(point_id)):
                lookup[str(point_id)] = point
    result: dict[str, bool] = {}
    for point in marked_points:
        if (not isinstance(point, dict) or not isinstance(point.get("payload"), dict)
                or not _valid_point_id(point.get("id"))):
            continue
        marker = point["payload"].get(MARKER_KEY)
        pid = str(point["id"])
        canonical = lookup.get(str(marker.get("canonical_id"))) if _valid_marker(marker) else None
        result[pid] = canonical is not None and marker_is_active(point, canonical)
    return result


def apply_plan(http: Http, collection: str, proposals: Iterable[dict[str, Any]], journal_path: Path,
               *, lock_path: Path | None, retired_at: str | None = None,
               max_mutations: int = 100, max_seconds: float = 30.0) -> dict[str, Any]:
    if lock_path is None:
        return {"schema": SCHEMA, "state": "NOT_RUN", "reason": "retention-lock-required", "mutations": 0}
    journal_path = Path(journal_path)
    deadline = time.monotonic() + max(0.01, float(max_seconds))
    created_at = retired_at or datetime.now(timezone.utc).isoformat()
    try:
        latest = _journal(journal_path)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return {"schema": SCHEMA, "state": "FAIL", "reason": "malformed-journal", "mutations": 0}
    work: list[dict[str, Any]] = [entry for entry in latest.values()
                                  if entry.get("state") == "prepared" and "after_marker" in entry]
    prepared_ids = {str(entry["point_id"]) for entry in work}
    for proposal in proposals:
        pid = str(proposal["id"])
        existing = latest.get(pid)
        if pid not in prepared_ids and not (existing and existing.get("state") == "applied"):
            work.append({"proposal": dict(proposal), "point_id": pid})
    applied = conflicts = skipped = mutations = 0
    try:
        for item in work:
            if deadline <= time.monotonic():
                return {"schema": SCHEMA, "state": "PARTIAL", "reason": "deadline", "mutations": mutations}
            with retention_lock(lock_path):
                if "after_marker" in item:
                    outcome, mutations = _apply_prepared(
                        http, collection, journal_path, item, deadline, max_mutations, mutations
                    )
                else:
                    proposal = item["proposal"]
                    current = _retrieve(http, collection, item["point_id"], deadline)
                    if current is None or MARKER_KEY in current["payload"]:
                        _state(journal_path, item["point_id"], "conflict", "point-not-live")
                        outcome = "conflict"
                    elif (_metadata(current) is None
                          or _digest(_metadata(current)) != proposal.get("expected_metadata_sha256")
                          or source_payload_fingerprint(current["payload"])
                          != proposal.get("expected_source_fingerprint")):
                        _state(journal_path, item["point_id"], "conflict", "plan-stale")
                        outcome = "conflict"
                    else:
                        fingerprint = source_payload_fingerprint(current["payload"])
                        if proposal["reason"] == "exact_duplicate":
                            canonical_id = proposal.get("canonical_id")
                            canonical = (_retrieve(http, collection, str(canonical_id), deadline)
                                         if _valid_point_id(canonical_id) else None)
                            canonical_metadata = _metadata(canonical) if canonical is not None else None
                            if (canonical is None or MARKER_KEY in canonical["payload"]
                                    or not _is_live(canonical)
                                    or canonical_metadata is None
                                    or _digest(canonical_metadata) != proposal.get(
                                        "canonical_expected_metadata_sha256")
                                    or source_payload_fingerprint(canonical["payload"])
                                    != proposal.get("canonical_expected_source_fingerprint")
                                    or proposal.get("scope") != _scope_value(current)
                                    or proposal.get("data_fingerprint") != _data_fingerprint(current["payload"])
                                    or proposal.get("user_id") != _user_value(current)
                                    or not _compatible_payloads(current["payload"], canonical["payload"])):
                                _state(journal_path, item["point_id"], "conflict", "canonical-not-equivalent")
                                outcome = "conflict"
                            else:
                                marker = _marker(
                                    {**proposal, "point_id": item["point_id"]}, fingerprint, created_at,
                                    canonical_fingerprint=source_payload_fingerprint(canonical["payload"]),
                                )
                                entry = _prepare_entry(proposal, item["point_id"], fingerprint, marker)
                                if not _valid_marker(marker):
                                    _state(journal_path, item["point_id"], "conflict", "invalid-proposal")
                                    outcome = "conflict"
                                else:
                                    _append(journal_path, entry)
                                    outcome, mutations = _apply_prepared(
                                        http, collection, journal_path, entry, deadline,
                                        max_mutations, mutations
                                    )
                        else:
                            marker = _marker({**proposal, "point_id": item["point_id"]}, fingerprint, created_at)
                            entry = _prepare_entry(proposal, item["point_id"], fingerprint, marker)
                            if not _valid_marker(marker):
                                _state(journal_path, item["point_id"], "conflict", "invalid-proposal")
                                outcome = "conflict"
                            else:
                                _append(journal_path, entry)
                                outcome, mutations = _apply_prepared(
                                    http, collection, journal_path, entry, deadline,
                                    max_mutations, mutations
                                )
            if outcome == "applied":
                applied += 1
            elif outcome == "conflict":
                conflicts += 1
            elif outcome == "limit":
                skipped += 1
                break
    except Exception:
        return {"schema": SCHEMA, "state": "PARTIAL", "reason": "interrupted", "mutations": mutations,
                "applied": applied, "conflicts": conflicts, "skipped": skipped, "model_calls": 0}
    return {"schema": SCHEMA, "state": "CONFLICT" if conflicts else "PASS", "applied": applied,
            "conflicts": conflicts, "skipped": skipped, "mutations": mutations, "model_calls": 0}


def _scroll(http: Http, collection: str, *, page_size: int, max_points: int, deadline: float) -> list[dict[str, Any]]:
    points: list[dict[str, Any]] = []
    offset: Any = None
    seen: set[str] = set()
    while len(points) < max_points:
        body: dict[str, Any] = {
            "limit": min(page_size, max_points - len(points)),
            # Full payload is required for the version-bound source fingerprint;
            # the marker is the only field this candidate ever writes.
            "with_payload": True,
            "with_vector": False, "filter": {"must_not": [
                {"key": "status", "match": {"value": status}} for status in sorted(RETIRED_STATUSES)
            ] + [{"key": "is_canary", "match": {"value": True}}, {"key": "canary", "match": {"value": True}}]},
        }
        if offset is not None:
            body["offset"] = offset
        result = _call(http, "POST", _path(collection, "/scroll"), body, deadline)
        result_body = result.get("result") if isinstance(result, dict) else None
        page = result_body.get("points") if isinstance(result_body, dict) else None
        if not isinstance(page, list):
            raise ValueError("malformed-scroll")
        points.extend(point for point in page if isinstance(point, dict))
        next_offset = result_body.get("next_page_offset")
        if next_offset is None or not page:
            return points[:max_points]
        marker = _canonical(next_offset)
        if marker in seen:
            raise ValueError("scroll-offset-loop")
        seen.add(marker)
        offset = next_offset
    return points[:max_points]


def apply_retention(http: Http, collection: str, journal_path: Path, *, lock_path: Path | None,
                    as_of: date, max_mutations: int = 100, max_seconds: float = 30.0,
                    page_size: int = 100, max_points: int = 200_000) -> dict[str, Any]:
    if lock_path is None:
        return {"schema": SCHEMA, "state": "NOT_RUN", "reason": "retention-lock-required", "mutations": 0}
    deadline = time.monotonic() + max(0.01, float(max_seconds))
    try:
        points = _scroll(http, collection, page_size=max(1, page_size), max_points=max(1, max_points), deadline=deadline)
        plan = plan_retention(points, as_of=as_of)
    except Exception:
        return {"schema": SCHEMA, "state": "FAIL", "reason": "scan-failed", "mutations": 0, "model_calls": 0}
    result = apply_plan(http, collection, plan["entries"], Path(journal_path), lock_path=lock_path,
                        max_mutations=max_mutations, max_seconds=max(0.01, deadline - time.monotonic()))
    result["planned"] = len(plan["entries"])
    result["plan_reasons"] = plan["reasons"]
    return result


def restore(http: Http, collection: str, journal_path: Path, *, lock_path: Path | None,
            max_seconds: float = 30.0) -> dict[str, Any]:
    if lock_path is None:
        return {"schema": SCHEMA, "state": "NOT_RUN", "reason": "retention-lock-required", "mutations": 0}
    try:
        latest = _journal(Path(journal_path))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return {"schema": SCHEMA, "state": "FAIL", "reason": "malformed-journal", "mutations": 0}
    deadline = time.monotonic() + max(0.01, float(max_seconds))
    restored = conflicts = mutations = 0
    try:
        for entry in sorted(latest.values(), key=lambda value: str(value.get("point_id"))):
            if entry.get("state") not in {"prepared", "applied"} or "after_marker" not in entry:
                continue
            if deadline <= time.monotonic():
                return {"schema": SCHEMA, "state": "PARTIAL", "reason": "deadline", "mutations": mutations}
            pid = str(entry["point_id"])
            with retention_lock(lock_path):
                current = _retrieve(http, collection, pid, deadline)
                if current is None:
                    _state(Path(journal_path), pid, "conflict", "missing-point")
                    conflicts += 1
                    continue
                marker = current["payload"].get(MARKER_KEY)
                if marker is None:
                    _state(Path(journal_path), pid, "restored")
                    restored += 1
                    continue
                if marker != entry["after_marker"]:
                    _state(Path(journal_path), pid, "conflict", "marker-changed")
                    conflicts += 1
                    continue
                mutations += 1
                _call(http, "POST", _path(collection, "/payload/delete?wait=true"),
                      {"points": [pid], "keys": [MARKER_KEY]}, deadline)
                checked = _retrieve(http, collection, pid, deadline)
                if checked is not None and MARKER_KEY not in checked["payload"]:
                    _state(Path(journal_path), pid, "restored")
                    restored += 1
                else:
                    _state(Path(journal_path), pid, "conflict", "marker-clear-mismatch")
                    conflicts += 1
    except Exception:
        return {"schema": SCHEMA, "state": "PARTIAL", "reason": "interrupted", "mutations": mutations,
                "restored": restored, "conflicts": conflicts}
    return {"schema": SCHEMA, "state": "CONFLICT" if conflicts else "PASS", "restored": restored,
            "conflicts": conflicts, "mutations": mutations}


__all__ = [
    "MARKER_KEY", "SCHEMA", "apply_plan", "apply_retention", "marker_is_active",
    "plan_retention", "restore", "retention_lock", "source_payload_fingerprint",
    "validate_canonical_batch",
]
