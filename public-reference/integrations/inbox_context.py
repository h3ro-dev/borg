#!/usr/bin/env python3
"""Read-only coordination bridge to an existing Agent Inbox installation.

No Hub, database, queue, identity enrollment or credential fallback is created.
The installed InboxClient owns authentication and current scope enforcement.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from typing import Any, Protocol

class NativeClient(Protocol):
    def call_sync(self, operation: str, params: dict[str, Any]) -> Any: ...

class ContractError(ValueError):
    pass

OPERATIONS = {"assignments": "assignments.list", "messages": "messages.list"}
FIELDS = {
    "assignments": ("work_id", "scope", "assignee", "version", "assigned_by", "created_at", "updated_at"),
    "messages": ("id", "work_id", "scope", "kind", "sender", "created_at", "expires_at",
                 "assignment_version", "assignment_assignee", "assignment_binding"),
}
DELIVERY_FIELDS = ("recipient", "state", "attempts", "acknowledged_at", "first_acknowledged_at",
                   "resolved_at", "acknowledgment_time_uncertain", "work_outcome_ref")
PAGE_FIELDS = ("next_cursor", "has_more", "complete", "returned", "limit", "consistency", "ordering")
COVERAGE_FIELDS = ("scope", "scanned", "scan_limit", "scan_complete", "eligible_total", "has_more_semantics")


def _text(value: Any, maximum: int) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum or any(ord(c) < 32 for c in value):
        raise ContractError("Invalid bounded string")
    return value


def _scope(value: Any) -> str:
    value = _text(value, 2048)
    if value == "/":
        return value
    if not value.startswith("/") or any(p in ("", ".", "..") for p in value[1:].split("/")):
        raise ContractError("Explicit canonical Inbox scope required")
    return value


def _pick(row: dict, fields: tuple[str, ...]) -> dict:
    out = {}
    for key in fields:
        if key not in row:
            continue
        value = row[key]
        if value is not None and (type(value) not in (str, int, bool) or isinstance(value, str) and len(value) > 16384):
            raise ContractError("Invalid metadata field")
        out[key] = value
    return out


def _page(value: Any) -> dict:
    if not isinstance(value, dict):
        raise ContractError("Missing native pagination metadata")
    if type(value.get("complete")) is not bool or type(value.get("has_more")) is not bool:
        raise ContractError("Missing page completeness")
    if value["complete"] == value["has_more"]:
        raise ContractError("Contradictory page completeness")
    cursor = value.get("next_cursor")
    if value["has_more"]:
        _text(cursor, 16384)
    elif cursor is not None:
        raise ContractError("Unexpected continuation cursor")
    out = _pick(value, PAGE_FIELDS)
    if "coverage" in value:
        if not isinstance(value["coverage"], dict):
            raise ContractError("Invalid native coverage")
        out["coverage"] = _pick(value["coverage"], COVERAGE_FIELDS)
    return out


class InboxContextReader:
    """Single-attempt metadata reads. Never poll, acknowledge, resolve or dispatch."""
    def __init__(self, client: NativeClient):
        self.client = client

    def read(self, component: str, *, work_id: str, scope: str, limit: int = 5,
             cursor: str | None = None) -> dict:
        if component not in OPERATIONS:
            raise ContractError("Only assignment and message listing are supported")
        work_id, scope = _text(work_id, 512), _scope(scope)
        if type(limit) is not int or not 1 <= limit <= 20:
            raise ContractError("Limit must be an integer between 1 and 20")
        params: dict[str, Any] = {"work_id": work_id, "scope": scope, "limit": limit}
        if cursor is not None:
            params["cursor"] = _text(cursor, 16384)
        operation = OPERATIONS[component]
        try:
            response = self.client.call_sync(operation, params)
        except Exception:
            # No payload-bearing exception text, background retries or queue flush.
            return {"status": "UNAVAILABLE", "operation": operation, "items": None, "page": None,
                    "complete": False, "retry_performed": False}
        try:
            if not isinstance(response, dict) or not isinstance(response.get(component), list):
                raise ContractError("Invalid native response")
            rows = response[component]
            if len(rows) > limit:
                raise ContractError("Native response exceeded requested limit")
            page = _page(response.get("page"))
            out = []
            for row in rows:
                if not isinstance(row, dict) or row.get("work_id") != work_id:
                    raise ContractError("Cross-work response rejected")
                row_scope = _scope(row.get("scope"))
                if scope != "/" and row_scope != scope and not row_scope.startswith(scope + "/"):
                    raise ContractError("Cross-scope response rejected")
                item = _pick(row, FIELDS[component])
                if component == "messages":
                    deliveries = row.get("deliveries", [])
                    if not isinstance(deliveries, list) or len(deliveries) > 100:
                        raise ContractError("Invalid delivery metadata")
                    if any(not isinstance(d, dict) for d in deliveries):
                        raise ContractError("Invalid delivery metadata")
                    item["deliveries"] = [_pick(d, DELIVERY_FIELDS) for d in deliveries]
                    item["work_acceptance"] = "not_asserted"
                    item["instruction_incorporation"] = "not_performed_by_metadata_read"
                out.append(item)
            return {"status": "OBSERVED", "operation": operation, "items": out,
                    "page": page, "complete": page["complete"], "retry_performed": False}
        except (ContractError, TypeError, ValueError):
            return {"status": "INVALID_RESPONSE", "operation": operation, "items": None,
                    "page": None, "complete": False, "retry_performed": False}

    def snapshot(self, *, work_id: str, scope: str, limit: int = 5) -> dict:
        assignments = self.read("assignments", work_id=work_id, scope=scope, limit=limit)
        messages = self.read("messages", work_id=work_id, scope=scope, limit=limit)
        good = sum(section["status"] == "OBSERVED" for section in (assignments, messages))
        return {"schema_version": 1, "status": ("OBSERVED" if good == 2 else "PARTIAL" if good else "UNAVAILABLE"),
                "observed_at": datetime.now(timezone.utc).isoformat(), "work_id": work_id,
                "scope": scope, "assignments": assignments, "messages": messages,
                "work_acceptance": "not_asserted", "memory_write_performed": False,
                "notice": "Inbox metadata is not task completion, an instruction payload, or permission to execute. "
                          "Native Inbox authorizes reads; Beads tracks work acceptance and the conductor tracks runs. "
                          "Follow returned cursors for additional pages; this snapshot does not claim atomic cross-read consistency."}


def open_native_client():
    """Use the caller's enrolled identity. Never read or copy credential values here."""
    config = os.environ.get("AGENT_INBOX_CLIENT_CONFIG")
    if not config or not Path(config).is_absolute():
        raise ContractError("An enrolled absolute AGENT_INBOX_CLIENT_CONFIG reference is required")
    from comms.hub.client import InboxClient  # Installed dependency; deliberately not bundled.
    return InboxClient.from_config(config, timeout=8)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-id", required=True)
    parser.add_argument("--scope", required=True)
    parser.add_argument("--limit", type=int, default=5)
    args = parser.parse_args()
    try:
        with open_native_client() as client:
            result = InboxContextReader(client).snapshot(work_id=args.work_id, scope=args.scope, limit=args.limit)
        print(json.dumps(result, indent=2))
        return 0 if result["status"] == "OBSERVED" else 2
    except Exception:
        print(json.dumps({"status": "UNAVAILABLE", "reason": "Native Inbox client, enrolled config or request unavailable; no fallback identity or retry used."}))
        return 2

if __name__ == "__main__":
    raise SystemExit(main())
