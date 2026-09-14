"""Bounded, passive consumer for optional owner-supplied fleet state files.

This module deliberately has no network, subprocess, cache, scheduler, or
caller-selected path. The portable default has no fleet sources and reports
UNKNOWN. An embedding owner may inject explicit fixture or integration paths.
"""

from __future__ import annotations

import datetime as _datetime
import json
import os
from pathlib import Path
import re
import stat
import time
from collections.abc import Mapping, Sequence
from typing import Any, Callable


FLEET_CONTEXT_MAX_BYTES = 12 * 1024
FLEET_CONTEXT_REF_MAX_BYTES = 1024
MAX_SOURCE_BYTES = 16 * 1024 * 1024
DEFAULT_READ_BUDGET_SECONDS = 0.75
DEFAULT_FRESH_SECONDS = 600.0
# Native Beads launch interval is 900 seconds; its collector has a 60-second
# maximum budget. An on-schedule collection must not expire before it finishes.
SOURCE_FRESH_SECONDS = {"beads": 960.0}
EXPECTED_MACHINES = ()
SOURCE_NAMES = (
    "headroom",
    "seats",
    "account_config",
    "claude_seats",
    "claude_usage",
    "agents",
    "beads",
)

# No deployment paths ship as product defaults. Tests and optional integrations
# inject source paths without exposing them over the Hub operation.
DEFAULT_SOURCES: dict[str, Path] = {}

_SAFE_CODE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
_SAFE_NATIVE_ID = re.compile(r"^[A-Za-z0-9_.:@/-]{1,128}$")
_SAFE_SCOPE = "/"
_UNKNOWN = "UNKNOWN"
_UTC_MINUS_SIX = _datetime.timezone(_datetime.timedelta(hours=-6), name="MDT")
_VERDICT_REASON_LIMIT = 8
_VERDICT_ID_LIMIT = 32
_OWNERSHIP_ROW_LIMIT = 12
_OWNED_WORK_LIMIT = 8
_LEGACY_ESTATE_WORK_ID_PREFIXES = ("eco-",)
_OWNERSHIP_SCHEMA = "beads-ownership/1"


def compact_json(value: Any) -> str:
    """Encode JSON without whitespace, rejecting non-finite values."""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def fleet_context_ref() -> dict[str, Any]:
    """Return the small passive pointer safe to include in native context."""

    return {
        "schema": "fleet-context-ref/v1",
        "operation": "fleet.context",
        "mcp_tool": "inbox_fleet_context",
        "scope": _SAFE_SCOPE,
        "max_bytes": FLEET_CONTEXT_MAX_BYTES,
        "fetch": "on_demand",
        "content_role": "passive_data",
        "work_effect": "preserve_current",
    }


class _SourceFailure(Exception):
    """Internal safe reason code; raw paths and exception prose never escape."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _safe_code(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if _SAFE_CODE.fullmatch(value) else None


def _safe_text(value: Any, limit: int = 128) -> str | None:
    if not isinstance(value, str):
        return None
    value = " ".join(value.split())
    if not value:
        return None
    return value[:limit]


def _safe_native_id(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if _SAFE_NATIVE_ID.fullmatch(value) else None


def _safe_codes(values: Any) -> list[str]:
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes, bytearray)):
        return []
    return sorted({code for item in values if (code := _safe_code(item)) is not None})


def _number(value: Any, *, lower: float | None = None, upper: float | None = None) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    numeric = float(value)
    if numeric != numeric or numeric in (float("inf"), float("-inf")):
        return None
    if lower is not None and numeric < lower:
        return None
    if upper is not None and numeric > upper:
        return None
    return int(value) if isinstance(value, int) or numeric.is_integer() else value


def _timestamp_epoch(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        numeric = float(value)
        if numeric > 100_000_000_000:
            numeric /= 1000.0
        return numeric if numeric >= 0 else None
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = _datetime.datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=_datetime.timezone.utc)
        return parsed.timestamp()
    except ValueError:
        pass
    # The approved Claude snapshots use a human-readable MDT suffix.
    for pattern in ("%Y-%m-%d %H:%M:%S %Z", "%a %d %b %Y %H:%M:%S %Z"):
        try:
            parsed = _datetime.datetime.strptime(value.strip(), pattern)
            if value.strip().endswith("MDT"):
                parsed = parsed.replace(tzinfo=_UTC_MINUS_SIX)
            else:
                parsed = parsed.replace(tzinfo=_datetime.timezone.utc)
            return parsed.timestamp()
        except ValueError:
            continue
    return None


def _freshness(value: Any, now: float, threshold: float = DEFAULT_FRESH_SECONDS) -> str:
    epoch = _timestamp_epoch(value)
    if epoch is None:
        return _UNKNOWN
    return "current" if max(0.0, now - epoch) <= threshold else "stale"


def _generated_at(data: Mapping[str, Any]) -> Any:
    for key in ("generated_iso", "generated_at", "generated", "observed_at", "stamp"):
        value = data.get(key)
        if isinstance(value, (str, int, float)) and not isinstance(value, bool):
            return value
    return None


def _source_meta(name: str, data: Mapping[str, Any] | None, error: str | None, now: float) -> dict[str, Any]:
    if data is None:
        result: dict[str, Any] = {"state": _UNKNOWN, "freshness": _UNKNOWN}
        if error:
            result["error"] = error
        return result
    generated = _generated_at(data)
    native_freshness = data.get("freshness")
    state = _safe_code(data.get("state")) or _safe_code(data.get("status")) or "OK"
    max_age = SOURCE_FRESH_SECONDS.get(name, DEFAULT_FRESH_SECONDS)
    freshness = _freshness(generated, now, max_age)
    if isinstance(native_freshness, str) and native_freshness.lower() == "stale":
        freshness = "stale"
    if state.upper() == "STALE":
        freshness = "stale"
    if (isinstance(native_freshness, str) and native_freshness.upper() == _UNKNOWN) or state.upper() in ("ERROR", _UNKNOWN):
        freshness = _UNKNOWN
    result = {"state": state, "freshness": freshness, "freshness_max_age_seconds": max_age}
    if generated is not None:
        result["generated_at"] = generated
    return result


def _read_json_file(path: Path, deadline: float) -> Mapping[str, Any]:
    descriptor: int | None = None
    try:
        descriptor = os.open(os.fspath(path), os.O_RDONLY | os.O_NONBLOCK)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise _SourceFailure("not_regular")
        if info.st_size > MAX_SOURCE_BYTES:
            raise _SourceFailure("oversize")
        data = bytearray()
        while len(data) <= MAX_SOURCE_BYTES:
            if time.monotonic() > deadline:
                raise _SourceFailure("timeout")
            chunk = os.read(descriptor, min(64 * 1024, MAX_SOURCE_BYTES + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
        if len(data) > MAX_SOURCE_BYTES or time.monotonic() > deadline:
            raise _SourceFailure("timeout" if time.monotonic() > deadline else "oversize")
        try:
            value = json.loads(bytes(data))
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise _SourceFailure("invalid_json") from exc
        if not isinstance(value, Mapping):
            raise _SourceFailure("invalid_shape")
        return value
    except _SourceFailure:
        raise
    except FileNotFoundError as exc:
        raise _SourceFailure("missing") from exc
    except PermissionError as exc:
        raise _SourceFailure("permission") from exc
    except (OSError, UnicodeError) as exc:
        raise _SourceFailure("unreadable") from exc
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _source_observation(
    name: str,
    data: Mapping[str, Any] | None,
    source_info: Mapping[str, Any],
    now: float,
) -> dict[str, Any]:
    result = dict(source_info)
    if data is None:
        return result
    if name == "headroom":
        result["machine_rows"] = len(data.get("machines", [])) if isinstance(data.get("machines"), list) else 0
    elif name == "seats":
        machines = data.get("machines")
        result["machine_rows"] = len(machines) if isinstance(machines, Mapping) else 0
        result["replica_rows"] = (
            sum(
                len(value.get("seats", []))
                for value in machines.values()
                if isinstance(value, Mapping) and isinstance(value.get("seats"), list)
            )
            if isinstance(machines, Mapping)
            else 0
        )
    elif name == "claude_seats":
        result["seat_rows"] = len(data.get("seats", [])) if isinstance(data.get("seats"), list) else 0
    elif name == "claude_usage":
        result["account_rows"] = len(data.get("accounts", [])) if isinstance(data.get("accounts"), list) else 0
    elif name == "agents":
        result["group_rows"] = len(data.get("groups", [])) if isinstance(data.get("groups"), list) else 0
    elif name == "beads":
        result["issue_rows"] = len(data.get("issues", [])) if isinstance(data.get("issues"), list) else 0
    return result


def _empty_coverage(denominator: int) -> dict[str, Any]:
    return {"observed": 0, "denominator": denominator, "missing": denominator}


def _expected_codex_accounts(data: Mapping[str, Any] | None) -> set[str] | None:
    """Use configured identities, never infer the roster from observations."""

    rows = data.get("codexAccounts") if data is not None else None
    if not isinstance(rows, list) or len(rows) > 64:
        return None
    accounts: set[str] = set()
    for row in rows:
        value = row.get("expectedEmail") if isinstance(row, Mapping) else None
        account = _safe_native_id(value)
        if account is None or account.count("@") != 1 or not all(account.split("@")):
            return None
        account = account.casefold()
        if account in accounts:
            return None
        accounts.add(account)
    return accounts


def _codex_coverage(observed: set[str], expected: set[str] | None) -> dict[str, Any]:
    coverage: dict[str, Any] = {
        "observed": len(observed),
        "denominator": None,
        "missing": None,
        "expected_observed": None,
        "expectation_state": _UNKNOWN,
        "expectation_source": "account_config",
    }
    if expected is not None:
        coverage.update({
            "denominator": len(expected),
            "missing": len(expected - observed),
            "expected_observed": len(expected & observed),
            "expectation_state": "OK",
            "missing_accounts": sorted(expected - observed),
            "unexpected_accounts": sorted(observed - expected),
        })
    return coverage


def _machine_projection(
    data: Mapping[str, Any] | None,
    source_info: Mapping[str, Any],
    now: float,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows: dict[str, Mapping[str, Any]] = {}
    if data is not None and isinstance(data.get("machines"), list):
        for row in data["machines"]:
            if isinstance(row, Mapping) and isinstance(row.get("machine"), str):
                rows[row["machine"]] = row
    projected: list[dict[str, Any]] = []
    observed = 0
    for machine in EXPECTED_MACHINES:
        row = rows.get(machine)
        item: dict[str, Any] = {"machine": machine}
        if row is None:
            item.update({"state": _UNKNOWN, "freshness": _UNKNOWN, "missing": ["machine_row"]})
            projected.append(item)
            continue
        observed += 1
        observed_at = row.get("observed_at") or source_info.get("generated_at")
        item["state"] = _safe_code(row.get("state")) or _UNKNOWN
        item["freshness"] = _freshness(observed_at, now)
        if observed_at is not None:
            item["observed_at"] = observed_at
        for output_key, source_key in (
            ("reachable", "reachable"),
            ("load_per_core", "load_per_core"),
            ("logical_cores", "logical_cores"),
            ("physical_ram_bytes", "physical_ram_bytes"),
        ):
            value = row.get(source_key)
            if output_key == "reachable":
                if isinstance(value, bool):
                    item[output_key] = value
            else:
                numeric = _number(value, lower=0)
                if numeric is not None:
                    item[output_key] = numeric
        missing = set(_safe_codes(row.get("missing")))
        for field in ("reachable", "load_per_core", "observed_at"):
            if field not in item:
                missing.add(field)
        if missing:
            item["missing"] = sorted(missing)
        errors = _safe_codes(row.get("errors"))
        if errors:
            item["errors"] = errors
        projected.append(item)
    return projected, {
        "observed": observed,
        "denominator": len(EXPECTED_MACHINES),
        "missing": len(EXPECTED_MACHINES) - observed,
    }


def _valid_seat(row: Mapping[str, Any]) -> bool:
    issues = _safe_codes(row.get("issues"))
    if any(issue in issues for issue in ("STATUS_READ_FAILED", "READ_FAILED", "UNREADABLE")):
        return False
    return (
        _number(row.get("remainingPercent"), lower=0, upper=100) is not None
        and _number(row.get("usedPercent"), lower=0, upper=100) is not None
        and isinstance(row.get("resetAt"), str)
        and bool(row.get("resetAt"))
    )


def _codex_projection(
    data: Mapping[str, Any] | None,
    source_info: Mapping[str, Any],
    now: float,
    expected: set[str] | None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    replicas: dict[str, list[dict[str, Any]]] = {}
    if data is not None and isinstance(data.get("machines"), Mapping):
        for machine, machine_data in data["machines"].items():
            if not isinstance(machine, str) or not isinstance(machine_data, Mapping):
                continue
            seats = machine_data.get("seats")
            if not isinstance(seats, list):
                continue
            for row in seats:
                if isinstance(row, Mapping) and isinstance(row.get("account"), str) and row.get("account"):
                    replicas.setdefault(row["account"], []).append(
                        {
                            "machine": machine,
                            "row": row,
                            "observed_at": machine_data.get("observed"),
                        }
                    )
    projected: list[dict[str, Any]] = []
    observed: set[str] = set()
    for account in sorted(replicas):
        rows = replicas[account]
        valid = [candidate for candidate in rows if _valid_seat(candidate["row"])]
        # Machine order is the stable primary order.  Most importantly, skip
        # failed/null replicas rather than letting one erase a valid reading.
        valid.sort(key=lambda candidate: (EXPECTED_MACHINES.index(candidate["machine"]) if candidate["machine"] in EXPECTED_MACHINES else 99, candidate["machine"]))
        primary = valid[0] if valid else rows[0]
        primary_row = primary["row"]
        item: dict[str, Any] = {
            "account": account[:128],
            "primary": {"machine": _safe_text(primary["machine"], 64) or _UNKNOWN},
            "replicas": {
                "total": len(rows),
                "valid": len(valid),
                "missing": len(rows) - len(valid),
            },
        }
        primary_state = _safe_code(primary_row.get("state"))
        if primary_state:
            item["primary"]["state"] = primary_state
        issues = sorted(
            {
                code
                for candidate in rows
                for code in _safe_codes(candidate["row"].get("issues"))
            }
        )
        if issues:
            item["issues"] = issues
        quota: dict[str, Any] = {}
        if _valid_seat(primary_row):
            observed.add(account.strip().casefold())
            quota.update(
                {
                    "remaining_percent": _number(primary_row.get("remainingPercent"), lower=0, upper=100),
                    "used_percent": _number(primary_row.get("usedPercent"), lower=0, upper=100),
                    "reset_at": _safe_text(primary_row.get("resetAt"), 64),
                }
            )
            observed_at = primary.get("observed_at") or source_info.get("generated_at")
            if observed_at is not None:
                quota["observed_at"] = observed_at
                quota["freshness"] = _freshness(observed_at, now)
            else:
                quota["missing"] = ["observed_at"]
        else:
            quota.update(
                {
                    "state": _UNKNOWN,
                    "missing": ["remaining_percent", "used_percent", "reset_at", "observed_at"],
                }
            )
        item["quota"] = quota
        projected.append(item)
    return projected, _codex_coverage(observed, expected)


def _claude_projection(
    usage: Mapping[str, Any] | None,
    usage_info: Mapping[str, Any],
    seats: Mapping[str, Any] | None,
    now: float,
) -> dict[str, Any]:
    meters: list[dict[str, Any]] = []
    accounts = usage.get("accounts", []) if usage is not None else []
    if isinstance(accounts, list):
        for account in accounts:
            if not isinstance(account, Mapping):
                continue
            account_name = _safe_text(account.get("key") or account.get("email"), 128)
            usage_data = account.get("usage")
            limits = usage_data.get("limits_seen") if isinstance(usage_data, Mapping) else None
            if not account_name or not isinstance(limits, list):
                continue
            for limit in limits:
                if not isinstance(limit, Mapping):
                    continue
                model = _safe_text(limit.get("model_scope"), 64)
                used = _number(limit.get("used_percent"), lower=0, upper=100)
                reset = _safe_text(limit.get("resets_at"), 64)
                if model is None or used is None or reset is None:
                    continue
                observed_at = limit.get("observed_at") or usage_info.get("generated_at")
                meters.append(
                    {
                        "account": account_name,
                        "model": model,
                        "model_scope": model,
                        "window": _safe_text(limit.get("window_label"), 32) or _UNKNOWN,
                        "used_percent": used,
                        "reset_at": reset,
                        "observed_at": observed_at if observed_at is not None else _UNKNOWN,
                        "freshness": _freshness(observed_at, now),
                    }
                )
    seat_rows = seats.get("seats", []) if isinstance(seats, Mapping) else []
    seat_count = len(seat_rows) if isinstance(seat_rows, list) else 0
    account_count = len(accounts) if isinstance(accounts, list) else 0
    return {
        "source_state": usage_info.get("state", _UNKNOWN),
        "source_freshness": usage_info.get("freshness", _UNKNOWN),
        "coverage": {
            "accounts": account_count,
            "seat_rows": seat_count,
            "native_model_meters": len(meters),
        },
        # Only explicit usage.limits_seen model meters are emitted.  Token
        # counts and burn/cost estimates are intentionally not quota fields.
        "native_model_meters": meters,
    }


_NATIVE_ID_FIELDS = ("agent_id", "instance_id", "session_id", "thread_id", "turn_id", "work_id")


def _native_ids(group: Mapping[str, Any]) -> list[dict[str, str]]:
    """Retain explicitly emitted native IDs without deriving ownership."""

    records: list[dict[str, str]] = []
    candidates: list[Mapping[str, Any]] = [group]
    items = group.get("items")
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, Mapping):
                continue
            candidates.append(item)
            threads = item.get("threads")
            if isinstance(threads, list):
                candidates.extend(thread for thread in threads if isinstance(thread, Mapping))
    seen: set[tuple[tuple[str, str], ...]] = set()
    for candidate in candidates:
        record = {
            field: native_id
            for field in _NATIVE_ID_FIELDS
            if (native_id := _safe_native_id(candidate.get(field))) is not None
        }
        if record:
            marker = tuple(sorted(record.items()))
            if marker not in seen:
                seen.add(marker)
                records.append(record)
    return records[:32]


def _bounded_reason_counts(reasons: list[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for reason in reasons:
        counts[reason] = min(999, counts.get(reason, 0) + 1)
    return dict(sorted(counts.items())[:_VERDICT_REASON_LIMIT])


def _exact_work_ids(beads: Mapping[str, Any] | None) -> tuple[set[str] | None, list[str]]:
    if beads is None:
        return None, []
    issues = beads.get("issues") if isinstance(beads, Mapping) else None
    if not isinstance(issues, list):
        return None, ["BEADS_SOURCE_INVALID"]
    work_ids: set[str] = set()
    reasons: list[str] = []
    for issue in issues:
        if not isinstance(issue, Mapping):
            reasons.append("BEADS_ISSUE_INVALID")
            continue
        status = issue.get("status")
        if isinstance(status, str) and status.lower() in {"closed", "done", "resolved"}:
            continue
        work_id = _safe_native_id(issue.get("id"))
        if work_id is None:
            reasons.append("BEADS_WORK_ID_INVALID")
            continue
        if work_id in work_ids:
            reasons.append("DUPLICATE_BEADS_WORK_ID")
        work_ids.add(work_id)
    return work_ids, reasons


def _is_ok(value: Any) -> bool:
    return isinstance(value, str) and value.upper() == "OK"


def _is_current(value: Any) -> bool:
    return isinstance(value, str) and value.upper() in {"CURRENT", "FRESH"}


def _canonical_ownership_index(
    beads: Mapping[str, Any] | None,
    beads_info: Mapping[str, Any],
    now: float,
) -> dict[str, Any]:
    """Index only fixed-feed canonical rows; perform no source discovery."""

    if not isinstance(beads, Mapping):
        return {"invalid": True, "routes": [], "sources": {}, "id_counts": {}}
    ownership = beads.get("ownership")
    if ownership is None:
        issues = beads.get("issues")
        current = (
            _is_ok(beads_info.get("state"))
            and _is_current(beads_info.get("freshness"))
            and isinstance(issues, list)
        )
        rows: dict[str, list[Mapping[str, Any]]] = {}
        valid = isinstance(issues, list)
        for issue in issues if isinstance(issues, list) else []:
            if not isinstance(issue, Mapping):
                valid = False
                continue
            work_id = _safe_native_id(issue.get("id"))
            if work_id is None:
                valid = False
                continue
            rows.setdefault(work_id, []).append(issue)
        return {
            "invalid": False,
            "routes": [
                (prefix, "__legacy_estate")
                for prefix in _LEGACY_ESTATE_WORK_ID_PREFIXES
            ],
            "sources": {
                "__legacy_estate": {"current": current and valid, "rows": rows}
            },
            "id_counts": {work_id: len(matches) for work_id, matches in rows.items()},
        }

    if (
        not isinstance(ownership, Mapping)
        or ownership.get("schema") != _OWNERSHIP_SCHEMA
        or not isinstance(ownership.get("sources"), list)
        or not ownership.get("sources")
    ):
        return {"invalid": True, "routes": [], "sources": {}, "id_counts": {}}

    routes: list[tuple[str, str]] = []
    sources: dict[str, dict[str, Any]] = {}
    id_counts: dict[str, int] = {}
    seen_source_ids: set[str] = set()
    invalid = False
    for position, source in enumerate(ownership["sources"]):
        if not isinstance(source, Mapping):
            invalid = True
            continue
        source_id = _safe_code(source.get("source_id"))
        prefixes = source.get("work_id_prefixes")
        if source_id is None or source_id in seen_source_ids or not isinstance(prefixes, list) or not prefixes:
            invalid = True
            continue
        seen_source_ids.add(source_id)
        source_key = "%s:%d" % (source_id, position)
        clean_prefixes = []
        for prefix in prefixes:
            clean = _safe_native_id(prefix)
            if clean is None:
                invalid = True
                continue
            clean_prefixes.append(clean)
            routes.append((clean, source_key))
        if not clean_prefixes:
            invalid = True
        issues = source.get("issues")
        issue_count = source.get("issue_count")
        rows: dict[str, list[Mapping[str, Any]]] = {}
        source_valid = isinstance(issues, list)
        for issue in issues if isinstance(issues, list) else []:
            if not isinstance(issue, Mapping):
                source_valid = False
                continue
            work_id = _safe_native_id(issue.get("id"))
            if work_id is None:
                source_valid = False
                continue
            rows.setdefault(work_id, []).append(issue)
            id_counts[work_id] = id_counts.get(work_id, 0) + 1
        if (
            isinstance(issue_count, bool)
            or not isinstance(issue_count, int)
            or issue_count < 0
            or not isinstance(issues, list)
            or issue_count != len(issues)
        ):
            source_valid = False
        sources[source_key] = {
            "current": (
                source_valid
                and _is_ok(source.get("state"))
                and _freshness(
                    source.get("checked_at"), now, SOURCE_FRESH_SECONDS["beads"]
                ) == "current"
            ),
            "rows": rows,
        }
    return {
        "invalid": invalid,
        "routes": routes,
        "sources": sources,
        "id_counts": id_counts,
    }


def _resolve_canonical_ownership(
    index: Mapping[str, Any], work_id: str
) -> tuple[Mapping[str, Any] | None, str | None]:
    """Resolve one assignment by exactly one literal configured prefix."""

    if index.get("invalid"):
        return None, "BEADS_SOURCE_NOT_CURRENT"
    matches = [source_key for prefix, source_key in index.get("routes", [])
               if work_id.startswith(prefix)]
    if not matches:
        return None, "BEADS_SOURCE_UNMAPPED"
    if len(matches) != 1:
        return None, "BEADS_SOURCE_AMBIGUOUS"
    source = index.get("sources", {}).get(matches[0])
    if not isinstance(source, Mapping) or not source.get("current"):
        return None, "BEADS_SOURCE_NOT_CURRENT"
    canonical = source.get("rows", {}).get(work_id, [])
    if index.get("id_counts", {}).get(work_id, 0) > 1 or len(canonical) > 1:
        return None, "BEADS_WORK_AMBIGUOUS"
    if not canonical:
        return None, "BEADS_WORK_NOT_FOUND"
    return canonical[0], None


def _native_ownership_projection(
    data: Mapping[str, Any] | None,
    beads: Mapping[str, Any] | None,
    beads_info: Mapping[str, Any],
    ownership_snapshot: Mapping[str, Any] | None,
    now: float,
) -> dict[str, Any]:
    """Join active Codex threads to exact registered and canonical owners.

    A row's ``owned_work`` is the set of current assignments held by the
    exactly matched agent and confirmed by the canonical Beads owner. It is
    deliberately not a claim that any one assignment is the executing subtask.
    Launcher ``work_id`` values are retained only as native task metadata.
    """

    aggregate_reasons: list[str] = []
    row_global_reasons: list[str] = []
    groups = data.get("groups") if isinstance(data, Mapping) else None
    joined_groups = [
        group
        for group in groups or []
        if isinstance(group, Mapping) and group.get("id") == "joined_instances"
    ] if isinstance(groups, list) else []
    joined_group: Mapping[str, Any] | None
    if len(joined_groups) != 1:
        aggregate_reasons.append(
            "NATIVE_GROUP_MISSING" if not joined_groups else "NATIVE_GROUP_AMBIGUOUS"
        )
        joined_group = None
    else:
        joined_group = joined_groups[0]

    group_reasons: list[str] = []
    if joined_group is not None:
        if not _is_ok(joined_group.get("status")):
            group_reasons.append("NATIVE_SOURCE_NOT_OK")
        native_freshness = joined_group.get("freshness")
        observed_freshness = _freshness(joined_group.get("observed_at"), now)
        if (
            isinstance(native_freshness, str)
            and native_freshness.upper() == "STALE"
        ) or observed_freshness == "stale":
            group_reasons.append("NATIVE_SOURCE_STALE")
        elif not _is_current(native_freshness) or observed_freshness != "current":
            group_reasons.append("NATIVE_SOURCE_FRESHNESS_UNKNOWN")

    rows: list[dict[str, Any]] = []
    native_denominator = 0
    native_item_denominator = 0
    native_items_observed = 0
    unavailable_native_items = 0
    unknown_item_denominators = 0
    if joined_group is not None:
        items = joined_group.get("items")
        if not isinstance(items, list):
            aggregate_reasons.append("NATIVE_ITEMS_INVALID")
            items = []
        elif not items:
            aggregate_reasons.append("NATIVE_ITEMS_EMPTY")
        native_item_denominator = len(items)
        for item in items:
            if not isinstance(item, Mapping):
                aggregate_reasons.append("NATIVE_ITEM_INVALID")
                unavailable_native_items += 1
                continue
            item_reasons = list(group_reasons)
            item_available = True
            if not _is_ok(item.get("native_status_state")):
                item_reasons.append("NATIVE_STATUS_NOT_CURRENT")
                item_available = False
            item_freshness = item.get("native_status_freshness")
            if item_freshness is not None and not _is_current(item_freshness):
                item_reasons.append("NATIVE_STATUS_NOT_CURRENT")
                item_available = False
            native_count = item.get("native_active_threads")
            if (
                isinstance(native_count, bool)
                or not isinstance(native_count, int)
                or native_count < 0
            ):
                item_reasons.append("NATIVE_DENOMINATOR_INVALID")
                unknown_item_denominators += 1
                item_available = False
                native_count = 0
            native_denominator += native_count
            threads = item.get("threads")
            if not isinstance(threads, list):
                aggregate_reasons.extend(item_reasons)
                aggregate_reasons.append("NATIVE_THREADS_INVALID")
                unavailable_native_items += 1
                continue
            if item_available:
                native_items_observed += 1
            else:
                unavailable_native_items += 1
            if len(threads) != native_count:
                item_reasons.append("NATIVE_THREAD_COUNT_MISMATCH")
            if not threads:
                aggregate_reasons.extend(item_reasons)
            for thread in threads:
                reasons = list(item_reasons)
                task: dict[str, Any] = {"runtime": "codex"}
                if not isinstance(thread, Mapping):
                    reasons.append("NATIVE_TASK_INVALID")
                    rows.append({"state": _UNKNOWN, "native_task": task, "owned_work": [],
                                 "_reasons": reasons})
                    continue
                machine = _safe_native_id(item.get("machine"))
                native_instance = _safe_native_id(item.get("instance_id"))
                thread_id = _safe_native_id(thread.get("thread_id"))
                turn_id = _safe_native_id(thread.get("turn_id"))
                attempt_id = _safe_native_id(thread.get("attempt_id"))
                launcher_work_id = _safe_native_id(thread.get("work_id"))
                join_state = _safe_code(thread.get("join_state"))
                for key, value in (
                    ("machine", machine),
                    ("instance_id", native_instance),
                    ("thread_id", thread_id),
                    ("turn_id", turn_id),
                    ("attempt_id", attempt_id),
                ):
                    if value is None:
                        reasons.append(f"NATIVE_{key.upper()}_INVALID")
                    else:
                        task[key] = value
                if thread.get("work_id") is not None and launcher_work_id is None:
                    reasons.append("NATIVE_LAUNCHER_WORK_ID_INVALID")
                elif launcher_work_id is not None:
                    task["launcher_work_id"] = launcher_work_id
                if join_state is None:
                    reasons.append("NATIVE_JOIN_STATE_INVALID")
                else:
                    task["join_state"] = join_state
                rows.append(
                    {
                        "state": _UNKNOWN,
                        "native_task": task,
                        "owned_work": [],
                        "_reasons": reasons,
                    }
                )

    task_keys: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for row in rows:
        task = row["native_task"]
        machine = task.get("machine")
        thread_id = task.get("thread_id")
        if isinstance(machine, str) and isinstance(thread_id, str):
            task_keys.setdefault(("codex", machine, thread_id), []).append(row)
    for matching_rows in task_keys.values():
        if len(matching_rows) > 1:
            for row in matching_rows:
                row["_reasons"].append("NATIVE_TASK_AMBIGUOUS")

    snapshot_agents = (
        ownership_snapshot.get("agents")
        if isinstance(ownership_snapshot, Mapping)
        else None
    )
    snapshot_assignments = (
        ownership_snapshot.get("assignments")
        if isinstance(ownership_snapshot, Mapping)
        else None
    )
    registry_available = isinstance(snapshot_agents, list) and isinstance(
        snapshot_assignments, list
    )
    if not registry_available:
        row_global_reasons.append("REGISTRY_SNAPSHOT_UNAVAILABLE")
        snapshot_agents = []
        snapshot_assignments = []

    canonical_index = _canonical_ownership_index(beads, beads_info, now)

    matched_agents = 0
    verified_rows = 0
    owned_work_truncated_rows = 0
    all_reasons: list[str] = list(aggregate_reasons)
    for row in rows:
        reasons: list[str] = row.pop("_reasons")
        reasons.extend(row_global_reasons)
        task = row["native_task"]
        machine = task.get("machine")
        native_thread = task.get("thread_id")
        candidates: list[Mapping[str, Any]] = []
        invalid_candidate = False
        if registry_available and isinstance(machine, str) and isinstance(native_thread, str):
            for agent in snapshot_agents:
                if not isinstance(agent, Mapping):
                    continue
                if agent.get("runtime") != "codex" or agent.get("machine") != machine:
                    continue
                raw_bindings = [
                    agent.get(field)
                    for field in ("session_id", "thread_id")
                    if agent.get(field) is not None
                ]
                bindings = {_safe_native_id(value) for value in raw_bindings}
                if native_thread not in bindings:
                    continue
                agent_id = _safe_native_id(agent.get("agent_id"))
                instance_id = _safe_native_id(agent.get("instance_id"))
                if (
                    agent_id is None
                    or instance_id is None
                    or None in bindings
                    or len(bindings) != 1
                ):
                    invalid_candidate = True
                    continue
                candidates.append(agent)
        if invalid_candidate:
            reasons.append("AGENT_IDENTITY_MISMATCH")
        if not candidates:
            reasons.append("AGENT_IDENTITY_NOT_FOUND")
        elif len(candidates) > 1:
            reasons.append("AGENT_IDENTITY_AMBIGUOUS")
        else:
            matched_agents += 1
            agent = candidates[0]
            agent_id = str(agent["agent_id"])
            row["registered_agent"] = {
                "agent_id": agent_id,
                "instance_id": str(agent["instance_id"]),
            }
            current_assignments = [
                assignment
                for assignment in snapshot_assignments
                if isinstance(assignment, Mapping) and assignment.get("assignee") == agent_id
            ]
            if not current_assignments:
                reasons.append("NO_CURRENT_ASSIGNMENT")
            assignment_groups: dict[str, list[Mapping[str, Any]]] = {}
            invalid_assignments = 0
            for assignment in current_assignments:
                work_id = _safe_native_id(assignment.get("work_id"))
                version = assignment.get("version")
                if (
                    work_id is None
                    or isinstance(version, bool)
                    or not isinstance(version, int)
                    or version < 1
                ):
                    invalid_assignments += 1
                    reasons.append("ASSIGNMENT_INVALID")
                    continue
                assignment_groups.setdefault(work_id, []).append(assignment)
            verified_work: list[dict[str, Any]] = []
            active_assignments = 0
            inactive_assignments = 0
            for work_id in sorted(assignment_groups):
                assignments = assignment_groups[work_id]
                if len(assignments) != 1:
                    reasons.append("ASSIGNMENT_AMBIGUOUS")
                    continue
                assignment = assignments[0]
                issue, canonical_reason = _resolve_canonical_ownership(
                    canonical_index, work_id)
                if canonical_reason is not None:
                    reasons.append(canonical_reason)
                    continue
                status = _safe_code(issue.get("status"))
                if status is None:
                    reasons.append("BEADS_STATUS_INVALID")
                    continue
                if status.lower() in {"closed", "done", "resolved"}:
                    inactive_assignments += 1
                    continue
                active_assignments += 1
                beads_owner = _safe_native_id(issue.get("assignee"))
                if beads_owner is None:
                    reasons.append("BEADS_OWNER_MISSING")
                    continue
                if beads_owner != agent_id:
                    reasons.append("BEADS_OWNER_MISMATCH")
                    continue
                verified_work.append(
                    {
                        "work_id": work_id,
                        "assignment_version": int(assignment["version"]),
                        "assignment_assignee": agent_id,
                        "beads_owner": beads_owner,
                        "beads_status": status,
                    }
                )
            if (
                current_assignments
                and active_assignments == 0
                and inactive_assignments == len(current_assignments)
            ):
                reasons.append("NO_ACTIVE_CURRENT_ASSIGNMENT")
            emitted_work = verified_work[:_OWNED_WORK_LIMIT]
            work_truncated = len(emitted_work) < len(verified_work)
            if work_truncated:
                owned_work_truncated_rows += 1
            row["owned_work"] = emitted_work
            row["owned_work_coverage"] = {
                "verified": len(verified_work),
                "denominator": len(current_assignments),
                "returned": len(emitted_work),
                "truncated": work_truncated,
                "invalid": invalid_assignments,
                "active": active_assignments,
                "inactive": inactive_assignments,
                "unresolved": max(
                    0,
                    len(current_assignments)
                    - active_assignments
                    - inactive_assignments,
                ),
            }
        reason_codes = sorted(set(reasons))
        if reason_codes:
            row["state"] = _UNKNOWN
            row["reason_code"] = reason_codes[0]
            row["reason_codes"] = reason_codes[:_VERDICT_REASON_LIMIT]
            all_reasons.extend(reason_codes)
        else:
            row["state"] = "OK"
            verified_rows += 1

    if not rows:
        all_reasons.extend(group_reasons)
        all_reasons.extend(row_global_reasons)
        if not all_reasons:
            all_reasons.append("NATIVE_TASKS_EMPTY")
    reason_counts = _bounded_reason_counts(all_reasons)
    rows.sort(
        key=lambda row: (
            row.get("state") != "OK",
            row.get("native_task", {}).get("machine", ""),
            row.get("native_task", {}).get("thread_id", ""),
        )
    )
    returned_rows = rows[:_OWNERSHIP_ROW_LIMIT]
    row_truncated = len(returned_rows) < len(rows)
    observed_tasks = len(rows)
    return {
        "state": (
            "OK"
            if observed_tasks > 0
            and observed_tasks == native_denominator
            and verified_rows == observed_tasks
            and not reason_counts
            else _UNKNOWN
        ),
        "semantics": "current_assignments_owned_not_executing",
        "reason_code": next(iter(reason_counts), None),
        "reason_counts": reason_counts,
        "coverage": {
            "native_tasks": {
                "observed": observed_tasks,
                "denominator": native_denominator,
                "missing": max(0, native_denominator - observed_tasks),
                "unknown_item_denominators": unknown_item_denominators,
            },
            "native_items": {
                "observed": native_items_observed,
                "denominator": native_item_denominator,
                "unavailable": unavailable_native_items,
            },
            "registered_agents": {
                "observed": matched_agents,
                "denominator": observed_tasks,
                "missing": max(0, observed_tasks - matched_agents),
            },
            "canonical_owners": {
                "observed": verified_rows,
                "denominator": observed_tasks,
                "missing": max(0, observed_tasks - verified_rows),
            },
            "rows": {
                "total": observed_tasks,
                "returned": len(returned_rows),
                "truncated": row_truncated,
            },
            "owned_work_truncated_rows": owned_work_truncated_rows,
        },
        "rows": returned_rows,
    }


def process_work_verdict(
    data: Mapping[str, Any] | None,
    beads: Mapping[str, Any] | None = None,
    source_info: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a bounded exact-ID verdict for the joined conductor projection."""

    reasons: list[str] = []
    if source_info is not None and (
            not isinstance(source_info, Mapping)
            or source_info.get("state") != "OK"
            or source_info.get("freshness") not in ("FRESH", "current")
    ):
        reasons.append("AGENTS_SOURCE_NOT_CURRENT")
    groups = data.get("groups") if isinstance(data, Mapping) else None
    joined_groups = [
        group for group in groups or []
        if isinstance(group, Mapping) and group.get("id") == "joined_instances"
    ] if isinstance(groups, list) else []
    if len(joined_groups) != 1:
        reasons.append("JOIN_GROUP_MISSING" if not joined_groups else "JOIN_GROUP_AMBIGUOUS")
        joined_group = None
    else:
        joined_group = joined_groups[0]
    native_total = 0
    joined_total = 0
    work_ids: list[str] = []
    if joined_group is not None:
        if joined_group.get("status") != "OK" or joined_group.get("freshness") not in ("FRESH", "current"):
            reasons.append("JOIN_GROUP_NOT_CURRENT")
        items = joined_group.get("items")
        if not isinstance(items, list):
            reasons.append("JOIN_ITEMS_INVALID")
            items = []
        elif not items:
            reasons.append("JOIN_ITEMS_EMPTY")
    instance_ids: set[str] = set()
    thread_identities: dict[tuple[str, str], tuple[str, str]] = {}
    thread_turns: dict[str, str] = {}
    if joined_group is not None:
        for item in items:
            if not isinstance(item, Mapping):
                reasons.append("JOIN_ITEM_INVALID")
                continue
            instance_id = item.get("instance_id")
            if instance_id is not None:
                safe_instance_id = _safe_native_id(instance_id)
                if safe_instance_id is None:
                    reasons.append("INSTANCE_ID_INVALID")
                elif safe_instance_id in instance_ids:
                    reasons.append("DUPLICATE_INSTANCE_ID")
                else:
                    instance_ids.add(safe_instance_id)
            if item.get("native_status_state") != "OK":
                reasons.append("NATIVE_STATUS_NOT_CURRENT")
                continue
            native_count = item.get("native_active_threads")
            if (isinstance(native_count, bool) or not isinstance(native_count, int)
                    or native_count < 0):
                reasons.append("NATIVE_DENOMINATOR_INVALID")
                continue
            native_total += native_count
            threads = item.get("threads")
            if not isinstance(threads, list):
                reasons.append("THREADS_INVALID")
                continue
            if len(threads) != native_count:
                reasons.append("NATIVE_THREAD_COUNT_MISMATCH")
            explicit_joined = 0
            for thread in threads:
                if not isinstance(thread, Mapping):
                    reasons.append("THREAD_RECORD_INVALID")
                    continue
                if thread.get("join_state") != "JOINED":
                    continue
                thread_id = _safe_native_id(thread.get("thread_id"))
                turn_id = _safe_native_id(thread.get("turn_id"))
                work_id = _safe_native_id(thread.get("work_id"))
                attempt_id = _safe_native_id(thread.get("attempt_id"))
                if not all((thread_id, turn_id, work_id, attempt_id)):
                    reasons.append("JOINED_IDENTITY_MISSING")
                    continue
                thread_identity = (thread_id, turn_id)
                prior = thread_identities.get(thread_identity)
                identity_payload = (work_id, attempt_id)
                if prior is not None:
                    reasons.append(
                        "DUPLICATE_THREAD_IDENTITY"
                        if prior == identity_payload else "AMBIGUOUS_THREAD_IDENTITY"
                    )
                else:
                    thread_identities[thread_identity] = identity_payload
                prior_turn = thread_turns.get(thread_id)
                if prior_turn is not None and prior_turn != turn_id:
                    reasons.append("AMBIGUOUS_THREAD_ID")
                else:
                    thread_turns[thread_id] = turn_id
                explicit_joined += 1
                work_ids.append(work_id)
            reported_joined = item.get("joined_threads")
            if (isinstance(reported_joined, bool) or not isinstance(reported_joined, int)
                    or reported_joined != explicit_joined):
                reasons.append("JOINED_COUNT_MISMATCH")
            joined_total += explicit_joined
            if explicit_joined != native_count:
                reasons.append("UNJOINED_THREADS")

    bead_ids, bead_reasons = _exact_work_ids(beads)
    reasons.extend(bead_reasons)
    if bead_ids is not None:
        for work_id in work_ids:
            if work_id not in bead_ids:
                reasons.append("BEADS_WORK_ID_MISMATCH")
    reason_counts = _bounded_reason_counts(reasons)
    return {
        "state": "OK" if not reason_counts and joined_total == native_total else _UNKNOWN,
        "reason_code": next(iter(reason_counts), None),
        "reason_counts": reason_counts,
        "native_active_threads": native_total,
        "joined_threads": joined_total,
        "work_ids": sorted(set(work_ids))[:_VERDICT_ID_LIMIT],
        "beads_checked": len(bead_ids) if bead_ids is not None else 0,
    }


def _active_agents_projection(
    data: Mapping[str, Any] | None,
    source_info: Mapping[str, Any],
    beads: Mapping[str, Any] | None = None,
    beads_info: Mapping[str, Any] | None = None,
    ownership_snapshot: Mapping[str, Any] | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    groups: list[dict[str, Any]] = []
    raw_groups = data.get("groups", []) if data is not None else []
    if isinstance(raw_groups, list):
        for group in raw_groups:
            if not isinstance(group, Mapping):
                continue
            group_id = _safe_text(group.get("id"), 64)
            if group_id is None:
                continue
            item: dict[str, Any] = {
                "id": group_id,
                "status": _safe_code(group.get("status")) or _UNKNOWN,
                "freshness": _safe_code(group.get("freshness")) or _UNKNOWN,
                "item_count": len(group.get("items", [])) if isinstance(group.get("items"), list) else 0,
                "observed_at": group.get("observed_at") if isinstance(group.get("observed_at"), (str, int, float)) else _UNKNOWN,
            }
            label = _safe_text(group.get("label"), 96)
            if label:
                item["label"] = label
            native_ids = _native_ids(group)
            if native_ids:
                item["native_ids"] = native_ids
            metric = group.get("metric")
            if isinstance(metric, Mapping):
                safe_metric: dict[str, Any] = {}
                for key in ("status", "value"):
                    value = metric.get(key)
                    if isinstance(value, str):
                        safe_metric[key] = _safe_code(value) or _UNKNOWN
                    elif isinstance(value, (int, float)) and not isinstance(value, bool):
                        numeric = _number(value, lower=0)
                        if numeric is not None:
                            safe_metric[key] = numeric
                    elif isinstance(value, bool):
                        safe_metric[key] = value
                denominator = metric.get("denominator")
                if isinstance(denominator, Mapping):
                    value = _number(denominator.get("value"), lower=0)
                    if value is not None:
                        safe_metric["denominator"] = value
                if safe_metric:
                    item["metric"] = safe_metric
            groups.append(item)
    join = process_work_verdict(data, beads, source_info)
    native_ownership = _native_ownership_projection(
        data,
        beads,
        beads_info or {},
        ownership_snapshot,
        float(time.time() if now is None else now),
    )
    return {
        "source_state": source_info.get("state", _UNKNOWN),
        "source_freshness": source_info.get("freshness", _UNKNOWN),
        "aggregate_status": (_safe_code(data.get("status")) or _UNKNOWN) if data is not None else _UNKNOWN,
        "aggregate_freshness": (_safe_code(data.get("freshness")) or _UNKNOWN) if data is not None else _UNKNOWN,
        "aggregate_observed_at": (
            data.get("observed_at")
            if data is not None and isinstance(data.get("observed_at"), (str, int, float))
            else _UNKNOWN
        ),
        "groups": groups,
        "process_work_join": join,
        "native_ownership": native_ownership,
    }


def _beads_projection(data: Mapping[str, Any] | None, source_info: Mapping[str, Any]) -> dict[str, Any]:
    assigned: list[dict[str, Any]] = []
    issues = data.get("issues", []) if data is not None else []
    if isinstance(issues, list):
        for issue in issues:
            if not isinstance(issue, Mapping):
                continue
            work_id = _safe_text(issue.get("id"), 64)
            assignee = _safe_text(issue.get("assignee"), 96)
            if work_id is None or assignee is None:
                continue
            status = _safe_code(issue.get("status")) or _UNKNOWN
            if status.lower() in {"closed", "done", "resolved"}:
                continue
            item: dict[str, Any] = {"work_id": work_id, "assignee": assignee, "status": status}
            title = _safe_text(issue.get("title"), 96)
            if title:
                item["summary"] = title
            scope = _safe_text(issue.get("scope"), 96)
            if scope:
                item["scope"] = scope
            assigned.append(item)
    assigned.sort(key=lambda item: (item.get("status", ""), item.get("work_id", "")))
    # Keep the normal response comfortably below the compact bound even when
    # a transport adds its own response envelope.
    limit = 16
    returned = assigned[:limit]
    return {
        "source_state": source_info.get("state", _UNKNOWN),
        "source_freshness": source_info.get("freshness", _UNKNOWN),
        "coverage": {
            "assigned_total": len(assigned),
            "returned": len(returned),
            "truncated": len(returned) < len(assigned),
        },
        "assigned_work": returned,
        "content_role": "passive_data",
        "work_effect": "preserve_current",
    }


def unknown_context(scope: str = _SAFE_SCOPE, reason: str = "source_unavailable") -> dict[str, Any]:
    """Return a bounded fail-open envelope with all material denominators."""

    sources = {name: {"state": _UNKNOWN, "freshness": _UNKNOWN} for name in SOURCE_NAMES}
    return {
        "schema": "fleet-context/v1",
        "scope": scope,
        "state": _UNKNOWN,
        "content_role": "passive_data",
        "instruction_actionable": False,
        "work_effect": "preserve_current",
        "reason_code": _safe_code(reason) or "source_unavailable",
        "coverage": {
            "machines": _empty_coverage(len(EXPECTED_MACHINES)),
            "codex_accounts": _codex_coverage(set(), None),
        },
        "sources": sources,
        "machines": [],
        "codex_accounts": [],
        "claude": {"coverage": {"accounts": 0, "seat_rows": 0, "native_model_meters": 0}, "native_model_meters": []},
        "active_agents": {
            "groups": [],
            "process_work_join": {"state": _UNKNOWN},
            "native_ownership": {
                "state": _UNKNOWN,
                "semantics": "current_assignments_owned_not_executing",
                "reason_code": "source_unavailable",
                "reason_counts": {"source_unavailable": 1},
                "coverage": {
                    "native_tasks": {
                        **_empty_coverage(0),
                        "unknown_item_denominators": 0,
                    },
                    "native_items": {
                        "observed": 0,
                        "denominator": 0,
                        "unavailable": 0,
                    },
                    "registered_agents": _empty_coverage(0),
                    "canonical_owners": _empty_coverage(0),
                    "rows": {"total": 0, "returned": 0, "truncated": False},
                    "owned_work_truncated_rows": 0,
                },
                "rows": [],
            },
        },
        "beads": {"assigned_work": [], "coverage": {"assigned_total": 0, "returned": 0, "truncated": False}},
        "truncation": {"truncated": False, "truthful": True},
    }


def _fit_context(context: dict[str, Any]) -> dict[str, Any]:
    """Keep output under the hard byte bound while retaining coverage facts."""

    context.setdefault("truncation", {"truncated": False, "truthful": True})
    if len(compact_json(context).encode("utf-8")) <= FLEET_CONTEXT_MAX_BYTES:
        return context
    # Generic Beads rows duplicate a subset of ownership data. Remove them
    # before the exact native-to-owner evidence that this interface serves.
    beads = context.get("beads")
    if isinstance(beads, dict) and isinstance(beads.get("assigned_work"), list):
        original = len(beads["assigned_work"])
        for limit in (12, 8, 4, 0):
            beads["assigned_work"] = beads["assigned_work"][:limit]
            coverage = beads.get("coverage")
            returned = len(beads["assigned_work"])
            if isinstance(coverage, dict):
                coverage["returned"] = returned
                coverage["truncated"] = returned < coverage.get("assigned_total", original)
            truncation = context.setdefault("truncation", {})
            truncation.update(
                {
                    "truncated": True,
                    "truthful": True,
                    "dropped_beads": max(
                        0,
                        int(coverage.get("assigned_total", original)) - returned
                        if isinstance(coverage, dict)
                        else original - returned,
                    ),
                }
            )
            if len(compact_json(context).encode("utf-8")) <= FLEET_CONTEXT_MAX_BYTES:
                return context
    active_agents = context.get("active_agents")
    legacy_join = active_agents.get("process_work_join") if isinstance(active_agents, dict) else None
    if isinstance(legacy_join, dict):
        dropped_ids = {}
        for key in ("native_ids", "work_ids"):
            values = legacy_join.get(key)
            if isinstance(values, list) and values:
                dropped_ids[key] = len(values)
                legacy_join[key] = []
        if dropped_ids:
            context["truncation"].update({
                "truncated": True,
                "truthful": True,
                "dropped_legacy_join_id_entries": dropped_ids,
            })
            if len(compact_json(context).encode("utf-8")) <= FLEET_CONTEXT_MAX_BYTES:
                return context
    groups = active_agents.get("groups") if isinstance(active_agents, dict) else None
    if isinstance(groups, list):
        dropped_group_ids = []
        for group in groups:
            values = group.get("native_ids") if isinstance(group, dict) else None
            if isinstance(values, list) and values:
                dropped_group_ids.append({"group_id": group.get("id"), "entries": len(values)})
                group["native_ids"] = []
        if dropped_group_ids:
            context["truncation"].update({
                "truncated": True,
                "truthful": True,
                "dropped_group_native_id_entries": dropped_group_ids,
            })
            if len(compact_json(context).encode("utf-8")) <= FLEET_CONTEXT_MAX_BYTES:
                return context
    ownership = (
        active_agents.get("native_ownership")
        if isinstance(active_agents, dict)
        else None
    )
    if isinstance(ownership, dict) and isinstance(ownership.get("rows"), list):
        original_returned = len(ownership["rows"])
        ownership_coverage = ownership.get("coverage")
        row_coverage = (
            ownership_coverage.get("rows")
            if isinstance(ownership_coverage, dict)
            else None
        )
        total = (
            row_coverage.get("total", original_returned)
            if isinstance(row_coverage, dict)
            else original_returned
        )
        has_verified = any(
            isinstance(row, Mapping) and row.get("state") == "OK"
            for row in ownership["rows"]
        )
        for limit in ((8, 4, 1) if has_verified else (8, 4, 0)):
            ownership["rows"] = ownership["rows"][:limit]
            returned = len(ownership["rows"])
            if isinstance(row_coverage, dict):
                row_coverage["returned"] = returned
                row_coverage["truncated"] = returned < total
            truncation = context.setdefault("truncation", {})
            truncation.update(
                {
                    "truncated": True,
                    "truthful": True,
                    "dropped_native_ownership_rows": max(0, total - returned),
                }
            )
            if len(compact_json(context).encode("utf-8")) <= FLEET_CONTEXT_MAX_BYTES:
                return context
    # Source-derived scalar fields have already been bounded and sanitized;
    # this final envelope retains every denominator and freshness state even if
    # an unexpectedly large producer shape was supplied.
    minimal = {
        "schema": context.get("schema", "fleet-context/v1"),
        "scope": context.get("scope", _SAFE_SCOPE),
        "state": context.get("state", "OK"),
        "content_role": "passive_data",
        "instruction_actionable": False,
        "work_effect": "preserve_current",
        "coverage": context.get("coverage", {}),
        "sources": context.get("sources", {}),
        "machines": [
            {
                key: item[key]
                for key in ("machine", "state", "freshness", "observed_at", "missing")
                if isinstance(item, Mapping) and key in item
            }
            for item in context.get("machines", [])
            if isinstance(item, Mapping)
        ],
        "codex_accounts": context.get("codex_accounts", []),
        "claude": context.get("claude", {}),
        "active_agents": context.get("active_agents", {}),
        "beads": {
            "coverage": context.get("beads", {}).get("coverage", {}) if isinstance(context.get("beads"), Mapping) else {},
            "assigned_work": [],
            "content_role": "passive_data",
            "work_effect": "preserve_current",
        },
        "truncation": {"truncated": True, "truthful": True, "dropped_optional_fields": True},
    }
    if len(compact_json(minimal).encode("utf-8")) <= FLEET_CONTEXT_MAX_BYTES:
        return minimal
    # Aggregate group detail can grow independently of the machine/account and
    # exact-owner rows. Truncate that optional list before discarding all known
    # sources. Preserve its denominator and report every omitted row explicitly.
    active = minimal.get("active_agents")
    groups = active.get("groups") if isinstance(active, dict) else None
    if isinstance(groups, list):
        total = len(groups)
        for limit in (8, 4, 0):
            active["groups"] = groups[:limit]
            returned = len(active["groups"])
            active["group_coverage"] = {
                "total": total,
                "returned": returned,
                "truncated": returned < total,
            }
            minimal["truncation"]["dropped_agent_groups"] = total - returned
            if len(compact_json(minimal).encode("utf-8")) <= FLEET_CONTEXT_MAX_BYTES:
                return minimal
    return unknown_context(scope=str(context.get("scope", _SAFE_SCOPE)), reason="output_bound")


class FleetContextReader:
    """Read fixed source files once and compose a bounded passive snapshot."""

    def __init__(
        self,
        sources: Mapping[str, str | os.PathLike[str]] | None = None,
        *,
        clock: Callable[[], float] | None = None,
        read_budget_seconds: float = DEFAULT_READ_BUDGET_SECONDS,
    ) -> None:
        selected = DEFAULT_SOURCES if sources is None else sources
        self.sources = {name: Path(selected[name]) for name in SOURCE_NAMES if name in selected}
        self.clock = clock or time.time
        self.read_budget_seconds = max(0.05, min(float(read_budget_seconds), 5.0))

    def read(
        self,
        scope: str = _SAFE_SCOPE,
        *,
        ownership_snapshot: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if scope != _SAFE_SCOPE:
            raise ValueError("fleet context scope must be /")
        now = float(self.clock())
        deadline = time.monotonic() + self.read_budget_seconds
        loaded: dict[str, Mapping[str, Any] | None] = {}
        source_info: dict[str, dict[str, Any]] = {}
        for name in SOURCE_NAMES:
            path = self.sources.get(name)
            if path is None:
                loaded[name] = None
                source_info[name] = {"state": _UNKNOWN, "freshness": _UNKNOWN, "error": "missing"}
                continue
            try:
                data = _read_json_file(path, deadline)
            except _SourceFailure as failure:
                loaded[name] = None
                source_info[name] = {"state": _UNKNOWN, "freshness": _UNKNOWN, "error": failure.code}
            except Exception:
                loaded[name] = None
                source_info[name] = {"state": _UNKNOWN, "freshness": _UNKNOWN, "error": "unavailable"}
            else:
                loaded[name] = data
                source_info[name] = _source_meta(name, data, None, now)
            if time.monotonic() > deadline:
                # Do not start another expensive source read after the bound;
                # remaining source denominators remain explicitly UNKNOWN.
                for remaining in SOURCE_NAMES[SOURCE_NAMES.index(name) + 1 :]:
                    loaded.setdefault(remaining, None)
                    source_info.setdefault(remaining, {"state": _UNKNOWN, "freshness": _UNKNOWN, "error": "timeout"})
                break

        for name in SOURCE_NAMES:
            loaded.setdefault(name, None)
            source_info.setdefault(name, {"state": _UNKNOWN, "freshness": _UNKNOWN, "error": "unavailable"})

        machine_rows, machine_coverage = _machine_projection(loaded["headroom"], source_info["headroom"], now)
        expected_accounts = _expected_codex_accounts(loaded["account_config"])
        source_info["account_config"]["source_kind"] = "configured_expectation"
        if loaded["account_config"] is not None and expected_accounts is None:
            source_info["account_config"].update({"state": _UNKNOWN, "error": "invalid_codex_roster"})
        account_rows, account_coverage = _codex_projection(
            loaded["seats"], source_info["seats"], now, expected_accounts
        )
        claude = _claude_projection(loaded["claude_usage"], source_info["claude_usage"], loaded["claude_seats"], now)
        active_agents = _active_agents_projection(
            loaded["agents"],
            source_info["agents"],
            loaded["beads"],
            source_info["beads"],
            ownership_snapshot,
            now,
        )
        beads = _beads_projection(loaded["beads"], source_info["beads"])
        context = {
            "schema": "fleet-context/v1",
            "scope": _SAFE_SCOPE,
            "state": "OK" if any(info.get("state") != _UNKNOWN for info in source_info.values()) else _UNKNOWN,
            "content_role": "passive_data",
            "instruction_actionable": False,
            "work_effect": "preserve_current",
            "generated_at": _datetime.datetime.now(_datetime.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "coverage": {"machines": machine_coverage, "codex_accounts": account_coverage},
            "sources": {
                name: _source_observation(name, loaded[name], source_info[name], now)
                for name in SOURCE_NAMES
            },
            "machines": machine_rows,
            "codex_accounts": account_rows,
            "claude": claude,
            "active_agents": active_agents,
            "beads": beads,
            "truncation": {
                "truncated": bool(beads.get("coverage", {}).get("truncated"))
                or bool(
                    active_agents.get("native_ownership", {})
                    .get("coverage", {})
                    .get("rows", {})
                    .get("truncated")
                )
                or bool(
                    active_agents.get("native_ownership", {})
                    .get("coverage", {})
                    .get("owned_work_truncated_rows")
                ),
                "truthful": True,
            },
        }
        return _fit_context(context)


def read_fleet_context(
    sources: Mapping[str, str | os.PathLike[str]] | None = None,
    *,
    scope: str = _SAFE_SCOPE,
    clock: Callable[[], float] | None = None,
) -> dict[str, Any]:
    """Small functional entry point used by tests and the authenticated Hub."""

    return FleetContextReader(sources, clock=clock).read(scope=scope)


read_context = read_fleet_context


__all__ = [
    "DEFAULT_SOURCES",
    "EXPECTED_MACHINES",
    "FLEET_CONTEXT_MAX_BYTES",
    "FLEET_CONTEXT_REF_MAX_BYTES",
    "FleetContextReader",
    "compact_json",
    "fleet_context_ref",
    "process_work_verdict",
    "read_context",
    "read_fleet_context",
    "unknown_context",
]
