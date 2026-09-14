"""Small, dependency-free canonicalization helpers for verified findings.

The source event is the identity boundary.  This module deliberately knows
nothing about Mem0, files, machines, sessions, HTTP requests, or receipts.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math
import re
import unicodedata
import uuid
from typing import Any


IDENTITY_SCHEMA = "borg.mem0.verified-finding.identity/v1"
UUID_NAMESPACE = uuid.UUID("2cd98d5c-269f-5bb6-8f67-0db9a1d36a1d")
ALLOWED_KINDS = frozenset({"fact", "decision", "preference", "procedure"})
ALLOWED_EVIDENCE_KINDS = frozenset({"user", "tool"})
ALLOWED_SOURCE_TYPES = frozenset({"inbox_record", "provider_event", "tool_receipt", "artifact"})
MAX_IDENTIFIER_BYTES = 240
MAX_FINDING_CHARS = 600
MAX_EVIDENCE_ITEMS = 46
MAX_EVIDENCE_TEXT_CHARS = 5000
MAX_EVIDENCE_CHARS = 26000
_REF_RE = re.compile(r"^[UT][0-9]{1,3}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_RFC3339_RE = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]+)?(?:Z|[+-][0-9]{2}:[0-9]{2})$"
)


def _reject_controls(value: str, field: str, *, allow_line_breaks: bool = False) -> None:
    allowed = {"\t", "\n", "\r"} if allow_line_breaks else set()
    if any(unicodedata.category(char) == "Cc" and char not in allowed for char in value):
        raise ValueError(f"{field} contains a control character")


def normalize_text(
    value: str,
    field: str,
    *,
    max_bytes: int = MAX_IDENTIFIER_BYTES,
    allow_line_breaks: bool = False,
) -> str:
    """NFC-normalize and boundary-trim one required text field."""
    if not isinstance(value, str):
        raise ValueError(f"{field} must be text")
    normalized = unicodedata.normalize("NFC", value).strip()
    if not normalized:
        raise ValueError(f"{field} must not be empty")
    _reject_controls(normalized, field, allow_line_breaks=allow_line_breaks)
    if len(normalized.encode("utf-8")) > max_bytes:
        raise ValueError(f"{field} is too large")
    return normalized


def _normalize_json(value: Any, *, allow_line_breaks: bool = False) -> Any:
    """Normalize JSON strings without trimming semantic content."""
    if isinstance(value, str):
        normalized = unicodedata.normalize("NFC", value)
        _reject_controls(normalized, "canonical JSON", allow_line_breaks=allow_line_breaks)
        return normalized
    if isinstance(value, dict):
        normalized = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("canonical JSON object keys must be text")
            key = unicodedata.normalize("NFC", key)
            _reject_controls(key, "canonical JSON key", allow_line_breaks=allow_line_breaks)
            if key in normalized:
                raise ValueError("canonical JSON has duplicate normalized keys")
            normalized[key] = _normalize_json(item, allow_line_breaks=allow_line_breaks)
        return normalized
    if isinstance(value, (list, tuple)):
        return [_normalize_json(item, allow_line_breaks=allow_line_breaks) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("canonical JSON does not allow non-finite numbers")
    if value is None or isinstance(value, (bool, int, float)):
        return value
    raise ValueError("canonical JSON contains an unsupported value")


def canonical_json(value: Any, *, allow_line_breaks: bool = False) -> str:
    """Return sorted, compact, UTF-8-ready canonical JSON."""
    return json.dumps(
        _normalize_json(value, allow_line_breaks=allow_line_breaks),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _sha256_canonical(value: Any, *, allow_line_breaks: bool = False) -> str:
    return hashlib.sha256(
        canonical_json(value, allow_line_breaks=allow_line_breaks).encode("utf-8")
    ).hexdigest()


def normalize_timestamp(value: str, field: str = "timestamp") -> str:
    """Require a zoned RFC3339 timestamp and return its UTC representation."""
    clean = normalize_text(value, field, max_bytes=64)
    if not _RFC3339_RE.fullmatch(clean):
        raise ValueError(f"{field} must be an RFC3339 timestamp with a zone")
    try:
        parsed = datetime.fromisoformat(clean.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} is not a valid timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat()


def identity_document(
    *,
    principal: str,
    scope: str,
    source_system: str,
    source_event_id: str,
    source_version: str,
) -> dict[str, Any]:
    """Build the exact v1 identity document."""
    return {
        "schema": IDENTITY_SCHEMA,
        "principal": normalize_text(principal, "principal"),
        "scope": normalize_text(scope, "scope"),
        "source": {
            "system": normalize_text(source_system, "source_system"),
            "event_id": normalize_text(source_event_id, "source_event_id"),
            "version": normalize_text(source_version, "source_version"),
        },
    }


def event_lock_key(
    *,
    principal: str,
    scope: str,
    source_system: str,
    source_event_id: str,
) -> str:
    """Hash the event identity, intentionally excluding source version."""
    document = {
        "principal": normalize_text(principal, "principal"),
        "scope": normalize_text(scope, "scope"),
        "source": {
            "system": normalize_text(source_system, "source_system"),
            "event_id": normalize_text(source_event_id, "source_event_id"),
        },
    }
    return _sha256_canonical(document)


def build_identity(
    *,
    principal: str,
    scope: str,
    source_system: str,
    source_event_id: str,
    source_version: str,
) -> dict[str, Any]:
    document = identity_document(
        principal=principal,
        scope=scope,
        source_system=source_system,
        source_event_id=source_event_id,
        source_version=source_version,
    )
    digest = _sha256_canonical(document)
    return {
        "document": document,
        "canonical": canonical_json(document),
        "identity_sha256": digest,
        "memory_id": str(uuid.uuid5(UUID_NAMESPACE, digest)),
        "event_lock_key": event_lock_key(
            principal=document["principal"],
            scope=document["scope"],
            source_system=document["source"]["system"],
            source_event_id=document["source"]["event_id"],
        ),
    }


def normalize_evidence(evidence: str | list[dict[str, Any]]) -> dict[str, Any]:
    """Validate evidence and return records plus text-free canonical pointers."""
    if isinstance(evidence, str):
        try:
            evidence = json.loads(evidence)
        except (TypeError, ValueError) as exc:
            raise ValueError("evidence_json must be a JSON list") from exc
    if not isinstance(evidence, list):
        raise ValueError("evidence_json must be a JSON list")
    if len(evidence) > MAX_EVIDENCE_ITEMS:
        raise ValueError("evidence has too many items")

    records: list[dict[str, str]] = []
    pointers: list[dict[str, Any]] = []
    seen_refs: set[str] = set()
    total_chars = 0
    for row in evidence:
        if not isinstance(row, dict) or set(row) != {"ref", "kind", "text", "source"}:
            raise ValueError("evidence item has an unsupported shape")
        ref = normalize_text(row["ref"], "evidence.ref", max_bytes=16)
        kind = normalize_text(row["kind"], "evidence.kind", max_bytes=16)
        if not _REF_RE.fullmatch(ref) or kind not in ALLOWED_EVIDENCE_KINDS:
            raise ValueError("evidence item has an invalid ref or kind")
        if (ref[0] == "U") != (kind == "user") or (ref[0] == "T") != (kind == "tool"):
            raise ValueError("evidence ref and kind do not agree")
        if ref in seen_refs:
            raise ValueError("evidence refs must be unique")
        text = normalize_text(
            row["text"],
            "evidence.text",
            max_bytes=MAX_EVIDENCE_TEXT_CHARS * 4,
            allow_line_breaks=True,
        )
        if len(text) > MAX_EVIDENCE_TEXT_CHARS:
            raise ValueError("evidence text is too large")
        total_chars += len(text)
        if total_chars > MAX_EVIDENCE_CHARS:
            raise ValueError("evidence exceeds the total size limit")

        source = row["source"]
        if not isinstance(source, dict):
            raise ValueError("evidence.source must be an object")
        if set(source) - {"type", "id", "version", "sha256"} or not {"type", "id"} <= set(source):
            raise ValueError("evidence.source has an unsupported shape")
        source_type = normalize_text(source["type"], "evidence.source.type", max_bytes=32)
        if source_type not in ALLOWED_SOURCE_TYPES:
            raise ValueError("evidence.source.type is unsupported")
        source_id = normalize_text(source["id"], "evidence.source.id")
        normalized_source: dict[str, str] = {"type": source_type, "id": source_id}
        if "version" in source:
            normalized_source["version"] = normalize_text(source["version"], "evidence.source.version")
        if "sha256" in source:
            digest = normalize_text(source["sha256"], "evidence.source.sha256", max_bytes=64)
            if not _SHA256_RE.fullmatch(digest):
                raise ValueError("evidence.source.sha256 must be lowercase SHA-256")
            normalized_source["sha256"] = digest

        seen_refs.add(ref)
        records.append({"ref": ref, "kind": kind, "text": text})
        pointers.append({"ref": ref, "kind": kind, "source": normalized_source})

    records.sort(key=lambda item: (item["ref"], item["kind"], item["text"]))
    pointers.sort(key=canonical_json)
    unique_pointers: list[dict[str, Any]] = []
    seen_pointers: set[str] = set()
    for pointer in pointers:
        encoded = canonical_json(pointer)
        if encoded not in seen_pointers:
            seen_pointers.add(encoded)
            unique_pointers.append(pointer)
    return {
        "records": records,
        "pointers": unique_pointers,
        "evidence_sha256": _sha256_canonical(records, allow_line_breaks=True),
    }


def payload_document(
    *,
    identity: dict[str, Any],
    finding: str,
    kind: str,
    observed_at: str,
    evidence: dict[str, Any],
    valid_at: str | None = None,
    invalid_at: str | None = None,
    supersedes_version: str | None = None,
) -> dict[str, Any]:
    """Build the canonical payload and its safe digests."""
    document = identity.get("document") if isinstance(identity, dict) else None
    if not isinstance(document, dict):
        raise ValueError("identity is invalid")
    clean_finding = normalize_text(finding, "finding", max_bytes=MAX_FINDING_CHARS * 4)
    if len(clean_finding) > MAX_FINDING_CHARS:
        raise ValueError("finding is too large")
    if kind not in ALLOWED_KINDS:
        raise ValueError("unsupported finding kind")
    if not isinstance(evidence, dict) or not isinstance(evidence.get("pointers"), list):
        raise ValueError("normalized evidence is required")
    document = {
        "identity": document,
        "finding": clean_finding,
        "kind": kind,
        "observed_at": normalize_timestamp(observed_at, "observed_at"),
        "evidence": evidence["pointers"],
        "evidence_sha256": normalize_text(evidence["evidence_sha256"], "evidence_sha256", max_bytes=64),
    }
    if not _SHA256_RE.fullmatch(document["evidence_sha256"]):
        raise ValueError("evidence_sha256 is invalid")
    if valid_at is not None:
        document["valid_at"] = normalize_timestamp(valid_at, "valid_at")
    if invalid_at is not None:
        document["invalid_at"] = normalize_timestamp(invalid_at, "invalid_at")
    if supersedes_version is not None:
        document["supersedes_version"] = normalize_text(supersedes_version, "supersedes_version")
    return {
        "document": document,
        "canonical": canonical_json(document),
        "payload_sha256": _sha256_canonical(document),
        "evidence_sha256": document["evidence_sha256"],
    }


# Explicit aliases keep the helper easy to consume from small callers.
canonical_identity = identity_document
canonical_payload = payload_document
