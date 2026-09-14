"""Metadata projection consumed by Eco's existing navigation and adjudication."""
import datetime
import hashlib
import json


def _pick(record, keys):
    return {key: record[key] for key in keys if key in record}


def _clock(value):
    try:
        result = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
        return result if result.tzinfo is not None else None
    except (AttributeError, TypeError, ValueError):
        return None


def _expired(value, observed):
    clock = _clock(value)
    return clock is not None and observed is not None and clock <= observed


def _coverage(rows, total):
    known = isinstance(total, int) and not isinstance(total, bool) and total >= len(rows)
    return {"returned": len(rows), "source_total": total if known else None,
            "denominator_scope": "global", "complete": known and len(rows) == total,
            "status": ("COMPLETE" if len(rows) == total else "PARTIAL") if known else "UNKNOWN"}


def sanitized_projection(snapshot):
    """Project source facts and case candidates; never store a second case ledger."""
    observed = _clock(snapshot.get("generated_at"))
    observed_at = snapshot.get("generated_at") if observed is not None else None
    messages, cases = [], []
    for row in snapshot.get("messages", []):
        message = _pick(row, ("id", "work_id", "sender", "kind", "created_at", "expires_at",
                              "authority_grant_id", "assignment_binding", "assignment_version", "assignment_assignee"))
        deliveries = row.get("deliveries")
        message["deliveries"] = [_pick(delivery, ("message_id", "recipient", "state", "lease_until", "attempts", "acknowledged_at"))
                                 for delivery in (deliveries if isinstance(deliveries, list) else [])]
        authority = row.get("authority") or {}
        message["authority"] = {"allowed": authority.get("allowed") if isinstance(authority.get("allowed"), bool) else None}
        messages.append(message)
        for delivery in message["deliveries"]:
            state = delivery.get("state")
            if state not in ("queued", "leased", "acknowledged", "rejected", "expired"):
                continue
            reasons = []
            if state == "rejected":
                reasons.append("DELIVERY_REJECTED")
            if state == "expired" or _expired(message.get("expires_at"), observed):
                reasons.append("MESSAGE_EXPIRED_UNRESOLVED")
            if state == "leased" and _expired(delivery.get("lease_until"), observed):
                reasons.append("DELIVERY_LEASE_EXPIRED")
            if (message.get("kind") == "instruction" and message["authority"]["allowed"] is False
                    and authority.get("reason") != "assignment notice has been superseded"):
                reasons.append("INSTRUCTION_AUTHORITY_ENDED")
            if not reasons:
                continue
            identity = [message["id"], delivery["recipient"]]
            cases.append({"case_id": "inbox-delivery-" + hashlib.sha256(json.dumps(identity, separators=(",", ":")).encode()).hexdigest()[:32],
                          "needs_adjudication": True, "observed_at": observed_at, "reason_codes": reasons,
                          "source": dict(_pick(message, ("work_id", "assignment_version", "assignment_assignee", "authority_grant_id")),
                                         message_id=message["id"], recipient=delivery["recipient"])})
    counts = {key: value for key, value in snapshot.get("counts", {}).items()
              if isinstance(value, int) and not isinstance(value, bool) and value >= 0}
    coverage = {name: _coverage(snapshot.get(name, []), counts.get(name))
                for name in ("agents", "messages", "grants", "assignments")}
    valid_case_source = observed is not None and all(isinstance(row.get("deliveries"), list) for row in snapshot.get("messages", []))
    complete_cases = valid_case_source and coverage["messages"]["complete"]
    coverage["cases"] = {"returned": len(cases), "complete": complete_cases,
                         "messages_observed": len(messages), "messages_total_global": coverage["messages"]["source_total"],
                         "status": coverage["messages"]["status"] if valid_case_source else "UNKNOWN",
                         "absence_can_close_unseen_cases": complete_cases}
    return {
        "schema": "eco-inbox-health/1",
        "source": "agent-inbox",
        "generated_at": observed_at,
        "observed_at": observed_at,
        "visibility": "tailnet_only",
        "adjudication_url": "/ops/#adjudication",
        "counts": counts,
        "coverage": coverage,
        "cases": cases,
        "agents": [_pick(row, ("agent_id", "id", "runtime", "machine", "last_seen", "status"))
                   for row in snapshot.get("agents", [])],
        "messages": messages,
        "grants": [_pick(row, ("id", "issuer", "grantee", "parent_grant_id", "created_at",
                               "expires_at", "revoked_at"))
                   for row in snapshot.get("grants", [])],
        "assignments": [_pick(row, ("work_id", "assignee", "version", "grant_id",
                                    "created_at", "updated_at"))
                        for row in snapshot.get("assignments", [])],
    }
