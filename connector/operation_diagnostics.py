"""Bounded operation metadata, never arguments, paths, payloads or error text.

A failure class does not prove the absence of effects. Only execution phase or
local fleet preflight evidence may establish that a call was not dispatched.
"""
from __future__ import annotations

import re
import uuid

SCHEMA = "borg-operation-diagnostics/v1"
CAUSES = frozenset({
    "credential_withheld", "authentication_required", "busy", "permission_denied",
    "policy_refused", "admission_required", "capability_unavailable", "rate_limited",
    "timeout", "provider_unavailable", "resource_exhausted", "downstream_error",
    "bad_request", "identity_mismatch", "request_cancelled", "process_interrupted",
})
PHASES = frozenset({"admission", "preflight", "dispatch", "result", "output_check", "complete", "unknown"})
EFFECTS = frozenset({"not_started", "outcome_unknown", "completed"})
_TRANSIENT = frozenset({"busy", "rate_limited", "timeout", "provider_unavailable"})
_HOST = re.compile(r"[a-z][a-z0-9_-]{0,47}\Z")
_TOOL = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,127}\Z")


def _uuid(value):
    if not isinstance(value, str):
        return None
    try:
        return str(uuid.UUID(value))
    except (ValueError, AttributeError):
        return None


def sanitize(value: dict | None) -> dict:
    """Closed schema, applied again at the durable write boundary."""
    value = value if isinstance(value, dict) else {}
    phase = value.get("phase")
    effect = value.get("effect_state")
    cause = value.get("cause")
    phase = phase if isinstance(phase, str) and phase in PHASES else "unknown"
    effect = effect if isinstance(effect, str) and effect in EFFECTS else "outcome_unknown"
    cause = cause if isinstance(cause, str) and cause in CAUSES else None
    out = {"schema": SCHEMA, "phase": phase, "effect_state": effect, "cause": cause,
           "retryable": effect == "not_started" and cause in _TRANSIENT,
           "retry_without_change": False}
    target = value.get("target")
    if isinstance(target, dict):
        safe = {}
        for key, pattern in (("host", _HOST), ("tool", _TOOL)):
            item = target.get(key)
            if isinstance(item, str) and pattern.fullmatch(item):
                safe[key] = item
        for key in ("instance_id", "server_generation", "receipt_id"):
            item = _uuid(target.get(key))
            if item is not None:
                safe[key] = item
        if safe:
            out["target"] = safe
    return out


def for_call(name: str, arguments: dict, *, phase: str, effect_state: str,
             cause: str | None = None, fleet: dict | None = None) -> dict:
    target = {}
    if name == "fleet_call":
        arguments = arguments if isinstance(arguments, dict) else {}
        target = {"host": arguments.get("host"), "tool": arguments.get("tool")}
        # Only the local Fleet wrapper establishes identity before dispatch.
        if isinstance(fleet, dict):
            identity = fleet.get("identity")
            if isinstance(identity, dict):
                target.update({k: identity.get(k) for k in ("instance_id", "server_generation")})
            target["receipt_id"] = fleet.get("target_receipt")
    return sanitize({"phase": phase, "effect_state": effect_state, "cause": cause, "target": target})
