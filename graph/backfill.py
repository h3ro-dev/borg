#!/usr/bin/env python3
"""Idempotent incremental Graphiti feed for scoped mem0 facts.

One coordinator consumes one Qdrant scroll page per invocation. Its durable
cursor is the server-provided ``next_page_offset``; reaching the end clears
the cursor and advances ``scan_epoch`` so a later pass starts at the
beginning. The coordinator assigns deterministic graph-key shards to bounded
workers, while only the coordinator commits feed state. Graph work is keyed
by point ID plus a canonical payload digest, while each deterministic episode
and projection-pending identity is derived from the scoped delta it contains.

The normal graph feed is default-off.  ``--canary`` is the only mode allowed
to write when ``GRAPH_FEED_LIVE=0`` and is restricted to the reserved Qdrant
collection and FalkorDB prefix from the GF-02 acceptance contract.
"""

from __future__ import annotations

import argparse
import asyncio
import atexit
from collections import deque
import contextlib
import hashlib
import json
import importlib.machinery
import os
import re
import tempfile
import time
import uuid
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable

from graph_scope import (
    DEFAULT_MAPPING_PATH,
    ScopeRegistry,
    is_valid_group_key,
    normalize_scope,
)


SOURCE_ROOT = Path(__file__).resolve().parent
CONFIG = importlib.machinery.SourceFileLoader(
    "borg_config_graph_feed", str(SOURCE_ROOT / "bin/borg_config.py")
).load_module().CONFIG
BASE = CONFIG.graph_root
MEMORY_BIN = CONFIG.mem0_root / "bin"
OWNER_ID = str(CONFIG.values["BORG_OWNER_ID"])
is_explicitly_expired = importlib.machinery.SourceFileLoader(
    "graph_feed_selection", str(MEMORY_BIN / "memory_selection.py")
).load_module().is_explicitly_expired
RETENTION = importlib.machinery.SourceFileLoader("graph_feed_retention", str(MEMORY_BIN / "retention.py")).load_module()
QDRANT = str(CONFIG.values["BORG_QDRANT_URL"]) if CONFIG.portable else os.environ.get("GRAPH_FEED_QDRANT_URL", "http://127.0.0.1:6333")
STATE_F = BASE / "data" / "backfill-state.json"
LOG_F = BASE / "data" / "backfill.log"
LOCK_F = BASE / "data" / "backfill.lock"
SCOPE_MAP_F = CONFIG.graph_data_root / "scope-graphs.json" if CONFIG.portable else DEFAULT_MAPPING_PATH
DEFAULT_COLLECTION = str(CONFIG.values["BORG_QDRANT_COLLECTION"])
DEFAULT_PAGE_LIMIT = 500
DEFAULT_SCAN_POINT_LIMIT = 150_000
DEFAULT_SCAN_PAGE_LIMIT = 32
DEFAULT_FACT_LIMIT = 96
DEFAULT_EPISODE_LIMIT = 12
DEFAULT_WORKERS = 4
DEFAULT_PROGRESS_INTERVAL_S = 60.0
MAX_EPISODE_TIMEOUT_S = 420.0
DEFAULT_EPISODE_TIMEOUT_S = MAX_EPISODE_TIMEOUT_S
DEFAULT_PASS_BUDGET_S = 600.0
DEFAULT_CLEANUP_MARGIN_S = 30.0
DEFAULT_NATIVE_REQUEST_TIMEOUT_S = 3 * 60.0
DEFAULT_NATIVE_MAX_RETRIES = 0
DEFAULT_CLIENT_CLOSE_TIMEOUT_S = 10.0
MAX_RECONCILIATION_ATTEMPTS = 3
CHECKPOINT_BATCH_SIZE = 1
MAX_FACTS_PER_EPISODE = 12
EPISODE_NAMESPACE = uuid.UUID("4f9676ba-2f7e-4d48-9ea1-9df0c77d0b11")
EPISODE_COMPLETION_SCHEMA = "graph-feed-complete/v1"
RECOVERY_SCHEMA = "recovery-v1"

CANARY_COLLECTION = "memory_one_door_graph_feed_canary"
CANARY_PREFIX = "memory_one_door_canary_"
CANARY_SCOPES = (
    "team:memory-one-door-canary-a",
    "team:memory-one-door-canary-b",
)
CANARY_STATE_F = BASE / "data" / "backfill-canary-state.json"
CANARY_SCOPE_MAP_F = BASE / "data" / "canary-scope-graphs.json"

ADMISSION_PRODUCTION = "production"
ADMISSION_ISOLATED_CANARY = "isolated_canary"
ADMISSION_REASONS = (
    "admitted",
    "invalid_point",
    "invalid_payload",
    "owner_mismatch",
    "corpus_seed",
    "retired",
    "expired",
    "canary_excluded",
    "canary_marker_required",
    "canary_scope_excluded",
    "empty_text",
    "invalid_scope",
)
_RETIRED_BOOLEAN_FIELDS = ("retired", "is_retired", "tombstoned")
_RETIRED_STATE_FIELDS = (
    "status",
    "lifecycle_status",
    "memory_status",
    "retention_status",
)
_RETIRED_STATES = {"retired", "tombstoned", "decayed"}
_CANARY_BOOLEAN_FIELDS = ("canary", "is_canary")
_CANARY_LABEL_FIELDS = ("kind", "source_kind", "source_system", "capture_mode")
_CANARY_TOKEN = re.compile(r"(?:^|[^a-z0-9])canary(?:$|[^a-z0-9])", re.IGNORECASE)


class EpisodeTimeout(TimeoutError):
    """One graph add exceeded the configured recoverable operation bound."""


class EpisodeCompletionUnproven(RuntimeError):
    """A deterministic physical episode exists without completion proof."""


def env_flag(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def env_float(name: str, default: float) -> float:
    value = os.environ.get(name)
    if value is None:
        return default
    try:
        return float(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc


def env_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc


def normalize_llm_url(value: str) -> str:
    """Normalize one OpenAI-compatible model base URL.

    ``Graphiti`` expects the base URL containing ``/v1``.  Keeping this
    normalization in the feed lets ``GRAPH_LLM_URLS`` accept the same host or
    host-plus-``/v1`` forms as the existing single-door setting.
    """

    url = str(value).strip().rstrip("/")
    if not url:
        raise ValueError("model URL cannot be empty")
    if not url.endswith("/v1"):
        url += "/v1"
    return url


def llm_url_pool(raw: str | None = None) -> list[str]:
    """Return the configured extraction-door pool in deterministic order."""

    if CONFIG.portable and raw is None:
        return [normalize_llm_url(str(CONFIG.values["BORG_GRAPH_LLM_URL"]))]
    configured = os.environ.get("GRAPH_LLM_URLS") if raw is None else raw
    if configured is not None and configured.strip():
        urls = [normalize_llm_url(item) for item in configured.split(",") if item.strip()]
        if not urls:
            raise ValueError("GRAPH_LLM_URLS contains no model URLs")
        return urls
    return [
        normalize_llm_url(
            os.environ.get("GRAPH_LLM_V6_URL", "http://127.0.0.1:11460/v1")
        )
    ]


def shard_index_for_graph_key(graph_key: str, worker_count: int = DEFAULT_WORKERS) -> int:
    """Map one actual Falkor graph key to one stable worker slot."""

    if worker_count <= 0:
        raise ValueError("worker_count must be positive")
    if not isinstance(graph_key, str) or not graph_key.strip():
        raise ValueError("graph_key must be a non-empty string")
    digest = hashlib.sha256(graph_key.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % worker_count


shard_for_graph_key = shard_index_for_graph_key


def assign_groups_to_shards(
    groups: list[dict[str, Any]], worker_count: int = DEFAULT_WORKERS
) -> list[list[dict[str, Any]]]:
    """Assign groups by graph key; each slot owns a disjoint graph-key set."""

    if worker_count <= 0:
        raise ValueError("worker_count must be positive")
    shards: list[list[dict[str, Any]]] = [[] for _ in range(worker_count)]
    for group in groups:
        if not isinstance(group, dict):
            raise ValueError("group must be an object")
        graph_key = group.get("group_id")
        slot = shard_index_for_graph_key(str(graph_key), worker_count)
        shards[slot].append(group)
    return shards


def now_mdt() -> datetime:
    return datetime.now().astimezone()


def now_mdt_text() -> str:
    return now_mdt().isoformat(timespec="seconds")


def log(message: str, *, echo: bool = True) -> str:
    """Append a human-readable lane line and optionally echo it."""

    line = f"{now_mdt():%Y-%m-%d %H:%M} {message}"
    LOG_F.parent.mkdir(parents=True, exist_ok=True)
    with LOG_F.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")
    if echo:
        print(line, flush=True)
    return line


def payload_digest(payload: dict[str, Any]) -> str:
    """Hash the complete canonical Qdrant payload, not a source-supplied hash."""

    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def ref_time(date_s: str) -> datetime:
    try:
        return datetime.strptime(date_s, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return datetime.now(timezone.utc)


def new_state(legacy_done: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return the schema-2 state shape without touching a state file."""

    return {
        "schema": 2,
        "scan_offset": None,
        "scan_epoch": 0,
        "selection_turn": 0,
        "last_complete_scan_mdt": None,
        "graph_processed": {},
        "projection_pending": {},
        "retry": {},
        "receipt_counters": {},
        "legacy_done": dict(legacy_done or {}),
    }


def _require_dict(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"state field {field!r} must be an object")
    return value


def migrate_state(raw: dict[str, Any]) -> dict[str, Any]:
    """Upgrade the old ``done`` state in memory to schema 2.

    The legacy map is retained for evidence only.  No code in the feed uses it
    as an idempotency key because a run ID can legitimately receive later
    point deltas.
    """

    if not isinstance(raw, dict):
        raise ValueError("backfill state must be a JSON object")
    if raw.get("schema") == 2:
        state = dict(raw)
        if "legacy_done" not in state:
            state["legacy_done"] = dict(raw.get("done") or {})
    else:
        state = new_state(dict(raw.get("done") or {}))

    defaults = new_state()
    for field, default in defaults.items():
        if field not in state:
            state[field] = default
    state.pop("done", None)
    for field in (
        "graph_processed",
        "projection_pending",
        "retry",
        "receipt_counters",
        "legacy_done",
    ):
        _require_dict(state[field], field)
    for point_id, entry in list(state["graph_processed"].items()):
        if not isinstance(entry, dict):
            continue
        recorded_point_id = entry.get("point_id")
        if recorded_point_id is not None and str(recorded_point_id) != str(point_id):
            raise ValueError("state graph_processed point_id does not match its key")
        if recorded_point_id is None:
            upgraded = dict(entry)
            upgraded["point_id"] = str(point_id)
            state["graph_processed"][point_id] = upgraded
    if not isinstance(state["scan_epoch"], int) or state["scan_epoch"] < 0:
        raise ValueError("state scan_epoch must be a non-negative integer")
    if not isinstance(state["selection_turn"], int) or state["selection_turn"] < 0:
        raise ValueError("state selection_turn must be a non-negative integer")
    return state


def _legacy_projection_view(state: dict[str, Any], state_path: Path) -> dict[str, Any]:
    """Expose old, timestamp-less pending snapshots through the new ledger.

    GF-02's earliest projection fixtures predate ``created_at_mdt``.  Keeping
    this read-only compatibility view lets those callers observe delivery
    results while the projector still leaves the feed JSON byte-for-byte
    untouched. Current feed entries include the timestamp and take no path
    through this compatibility adapter.
    """

    pending = state.get("projection_pending")
    if not isinstance(pending, dict) or not any(
        isinstance(item, dict) and "created_at_mdt" not in item
        for item in pending.values()
    ):
        return state
    ledger_path = state_path.with_name("graph-recall-projector-ledger.json")
    try:
        ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return state
    if not isinstance(ledger, dict):
        return state
    delivered = ledger.get("delivered") if isinstance(ledger.get("delivered"), dict) else {}
    resolved = ledger.get("resolved") if isinstance(ledger.get("resolved"), dict) else {}
    failures = ledger.get("failures") if isinstance(ledger.get("failures"), dict) else {}
    done = set(delivered) | set(resolved)
    for identity in done:
        state["projection_pending"].pop(identity, None)
        if identity in delivered:
            state.setdefault("projection_delivered", {})[identity] = delivered[identity]
    for identity, failure in failures.items():
        if identity in done or identity not in state["projection_pending"]:
            continue
        state["retry"].setdefault(
            f"projection:{identity}",
            {
                "kind": "projection",
                "identity": identity,
                **(failure if isinstance(failure, dict) else {}),
            },
        )
    return state


def load_state(path: str | Path = STATE_F) -> dict[str, Any]:
    target = Path(path)
    if not target.exists():
        return new_state()
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"cannot read backfill state {target}: {exc}") from exc
    return _legacy_projection_view(migrate_state(raw), target)


def save_state_atomic(path: str | Path, state: dict[str, Any]) -> None:
    """Write state through a same-directory fsynced temp file and replace."""

    target = Path(path)
    state = migrate_state(state)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, target)
    except Exception:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


write_state_atomic = save_state_atomic


def prune_projected_pending(
    state: dict[str, Any], ledger_path: str | Path
) -> int:
    """Drop projector-delivered/resolved identities from feed pending.

    The projector must not write feed state (GF-03 single writer). The
    coordinator therefore forgets work the projector ledger has already
    closed; otherwise projection_pending grows 1:1 with graph_processed and
    every projector tick reports a rising skip count.
    """

    pending = state.get("projection_pending")
    if not isinstance(pending, dict) or not pending:
        return 0
    path = Path(ledger_path)
    if not path.is_file():
        return 0
    try:
        ledger = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    if not isinstance(ledger, dict):
        return 0
    delivered = ledger.get("delivered") if isinstance(ledger.get("delivered"), dict) else {}
    resolved = ledger.get("resolved") if isinstance(ledger.get("resolved"), dict) else {}
    done = set(delivered) | set(resolved)
    if not done:
        return 0
    removed = 0
    for identity in list(pending):
        if identity in done:
            pending.pop(identity, None)
            removed += 1
    return removed


def _text_from_payload(payload: dict[str, Any]) -> str:
    value = payload.get("data") or payload.get("memory") or payload.get("text") or ""
    if isinstance(value, str):
        return value.strip()
    if value:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return ""


def _payload_containers(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Return lifecycle-bearing payload containers without trusting one layout."""

    pending = [payload]
    containers: list[dict[str, Any]] = []
    seen: set[int] = set()
    while pending:
        container = pending.pop(0)
        if not isinstance(container, dict) or id(container) in seen:
            continue
        seen.add(id(container))
        containers.append(container)
        for key in ("metadata", "payload"):
            nested = container.get(key)
            if isinstance(nested, dict):
                pending.append(nested)
    return containers


def _valid_past_or_present_date(value: Any) -> bool:
    """Recognize a valid retirement date; malformed values remain unknown."""

    if not isinstance(value, str) or not value.strip():
        return False
    raw = value.strip()
    try:
        observed = date.fromisoformat(raw[:10])
    except ValueError:
        return False
    return observed <= datetime.now(timezone.utc).date()


def _is_explicitly_retired(payload: dict[str, Any]) -> bool:
    for container in _payload_containers(payload):
        if any(container.get(field) is True for field in _RETIRED_BOOLEAN_FIELDS):
            return True
        for field in _RETIRED_STATE_FIELDS:
            value = container.get(field)
            if isinstance(value, str) and value.strip().casefold() in _RETIRED_STATES:
                return True
        if _valid_past_or_present_date(container.get("retired_at")):
            return True
    return False


def _is_explicit_canary(payload: dict[str, Any]) -> bool:
    for container in _payload_containers(payload):
        if any(container.get(field) is True for field in _CANARY_BOOLEAN_FIELDS):
            return True
        for field in _CANARY_LABEL_FIELDS:
            value = container.get(field)
            if isinstance(value, str) and _CANARY_TOKEN.search(value):
                return True
    return False


def _admission_mode_for(collection: str, falkor_prefix: str) -> str:
    reserved_collection = collection == CANARY_COLLECTION
    reserved_prefix = falkor_prefix == CANARY_PREFIX
    if reserved_collection != reserved_prefix:
        raise ValueError(
            "reserved canary collection and Falkor prefix must be selected together"
        )
    return (
        ADMISSION_ISOLATED_CANARY
        if reserved_collection
        else ADMISSION_PRODUCTION
    )


def classify_point(
    point: dict[str, Any],
    *,
    admission_mode: str = ADMISSION_PRODUCTION,
    canonical_payload: dict[str, Any] | None = None,
) -> tuple[dict[str, Any] | None, str]:
    """Return one admitted fact plus a stable reason without poisoning a page."""

    if admission_mode not in {ADMISSION_PRODUCTION, ADMISSION_ISOLATED_CANARY}:
        raise ValueError(f"unsupported admission mode {admission_mode!r}")
    if not isinstance(point, dict) or point.get("id") is None:
        return None, "invalid_point"
    payload = point.get("payload") or {}
    if not isinstance(payload, dict):
        return None, "invalid_payload"
    if payload.get("user_id") != OWNER_ID:
        return None, "owner_mismatch"
    if payload.get("run_id") == "corpus-seed":
        return None, "corpus_seed"
    if _is_explicitly_retired(payload) or RETENTION.marker_is_active(point, canonical_payload):
        return None, "retired"
    if is_explicitly_expired(payload):
        return None, "expired"

    try:
        scope = normalize_scope(payload.get("scope"))
    except (TypeError, ValueError):
        return None, "invalid_scope"
    is_canary = _is_explicit_canary(payload)
    if admission_mode == ADMISSION_PRODUCTION:
        if is_canary or scope in CANARY_SCOPES:
            return None, "canary_excluded"
    else:
        if not is_canary:
            return None, "canary_marker_required"
        if scope not in CANARY_SCOPES:
            return None, "canary_scope_excluded"

    text = _text_from_payload(payload)
    if not text:
        return None, "empty_text"
    point_id = str(point["id"])
    run_id = str(payload.get("run_id") or f"solo-{point_id[:8]}")
    date_value = payload.get("thread_date") or payload.get("added_mdt") or ""
    date_s = str(date_value)[:10]
    try:
        digest = payload_digest(payload)
    except (TypeError, ValueError):
        return None, "invalid_payload"
    return (
        {
            "point_id": point_id,
            "payload_digest": digest,
            "payload": payload,
            "text": text,
            "run_id": run_id,
            "scope": scope,
            "kind": str(payload.get("kind") or payload.get("agent_id") or "fact"),
            "date": date_s,
        },
        "admitted",
    )


def point_to_fact(
    point: dict[str, Any],
    *,
    admission_mode: str = ADMISSION_PRODUCTION,
) -> dict[str, Any] | None:
    """Compatibility view of :func:`classify_point` for one Qdrant point."""

    fact, _reason = classify_point(point, admission_mode=admission_mode)
    return fact


def _processed_digest(entry: Any) -> str | None:
    if isinstance(entry, str):
        return entry
    if isinstance(entry, dict):
        value = entry.get("payload_digest") or entry.get("digest")
        return str(value) if value else None
    return None


def _retry_row(entry: dict[str, Any]) -> list[dict[str, Any]]:
    rows = entry.get("rows")
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict) and row.get("point_id")]


def episode_id_for(scope: str, run_id: str, rows: list[dict[str, Any]]) -> str:
    """Derive one stable UUID from the scoped point/digest delta."""

    identity = {
        "scope": normalize_scope(scope),
        "run_id": str(run_id),
        "facts": [
            {"point_id": str(row["point_id"]), "payload_digest": row["payload_digest"]}
            for row in sorted(rows, key=lambda row: (str(row["point_id"]), row["payload_digest"]))
        ],
    }
    name = json.dumps(identity, sort_keys=True, separators=(",", ":"))
    return str(uuid.uuid5(EPISODE_NAMESPACE, name))


def recovery_episode_id_for(
    original_episode_id: str, rows: list[dict[str, Any]]
) -> str:
    """Derive one non-destructive revision UUID from exact original provenance."""

    if not isinstance(original_episode_id, str) or not original_episode_id:
        raise ValueError("original episode id must be a non-empty string")
    identity = {
        "schema": RECOVERY_SCHEMA,
        "original_episode_id": original_episode_id,
        "facts": [
            {
                "point_id": str(row["point_id"]),
                "payload_digest": str(row["payload_digest"]),
            }
            for row in sorted(
                rows,
                key=lambda row: (str(row["point_id"]), str(row["payload_digest"])),
            )
        ],
    }
    name = json.dumps(identity, sort_keys=True, separators=(",", ":"))
    return str(uuid.uuid5(EPISODE_NAMESPACE, name))


def projection_identity(episode_id: str, row: dict[str, Any]) -> str:
    return f"{episode_id}:{row['point_id']}:{row['payload_digest']}"


def episode_completion_token(group: dict[str, Any]) -> str:
    """Bind graph-native completion proof to the authorized source identity."""

    identity = {
        "schema": EPISODE_COMPLETION_SCHEMA,
        "episode_id": group["episode_id"],
        "scope": group["scope"],
        "run_id": group["run_id"],
        "group_id": group["group_id"],
        "facts": [
            {
                "point_id": str(row["point_id"]),
                "payload_digest": row["payload_digest"],
            }
            for row in sorted(
                group["rows"],
                key=lambda row: (str(row["point_id"]), row["payload_digest"]),
            )
        ],
    }
    recovery_of = group.get("recovery_of_episode_id")
    if recovery_of is not None:
        recovery_of = str(recovery_of)
        if group.get("recovery_schema") != RECOVERY_SCHEMA:
            raise ValueError("recovery group has an unsupported schema")
        if group.get("episode_id") != recovery_episode_id_for(
            recovery_of, group["rows"]
        ):
            raise ValueError("recovery group identity does not match its provenance")
        identity["recovery"] = {
            "schema": RECOVERY_SCHEMA,
            "original_episode_id": recovery_of,
        }
    encoded = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return f"{EPISODE_COMPLETION_SCHEMA}:{hashlib.sha256(encoded).hexdigest()}"


def _row_for_retry(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "point_id": row["point_id"],
        "payload_digest": row["payload_digest"],
        "payload": row.get("payload") or {},
        "text": row["text"],
        "run_id": row["run_id"],
        "scope": row["scope"],
        "kind": row["kind"],
        "date": row["date"],
    }


def _group_from_rows(
    rows: list[dict[str, Any]],
    registry: ScopeRegistry,
    *,
    falkor_prefix: str,
    expected_episode_id: str | None = None,
    group_id: str | None = None,
) -> dict[str, Any]:
    ordered = sorted(
        rows, key=lambda row: (str(row["point_id"]), row["payload_digest"])
    )
    scope = ordered[0]["scope"]
    run_id = ordered[0]["run_id"]
    if any(row["scope"] != scope or row["run_id"] != run_id for row in ordered):
        raise ValueError("retry episode rows must retain one scope and run_id")
    episode_id = episode_id_for(scope, run_id, ordered)
    if expected_episode_id is not None and episode_id != expected_episode_id:
        raise ValueError("retry episode identity does not match its source rows")
    if group_id is None:
        group_id = f"{falkor_prefix}{registry.ensure_scope(scope)}"
    if not is_valid_group_key(group_id):
        raise ValueError(f"invalid scoped group key {group_id!r}")
    return {
        "scope": scope,
        "run_id": run_id,
        "group_id": group_id,
        "episode_id": episode_id,
        "rows": ordered,
        "episode_body": "\n".join(row["text"] for row in ordered)[:6000],
        "source_description": ordered[0]["kind"],
        "reference_time": ref_time(max(row["date"] for row in ordered)),
    }


def _recovery_revision_group(
    original_group: dict[str, Any], original_episode_id: str
) -> dict[str, Any]:
    """Return the one deterministic revision without changing the original."""

    if original_group["episode_id"] != original_episode_id:
        raise ValueError("recovery provenance does not match original episode")
    revision = dict(original_group)
    revision["episode_id"] = recovery_episode_id_for(
        original_episode_id, original_group["rows"]
    )
    revision["recovery_schema"] = RECOVERY_SCHEMA
    revision["recovery_of_episode_id"] = original_episode_id
    return revision


def _date_rank(value: Any) -> int:
    """Rank a valid source date; malformed/missing dates stay cold."""

    if not isinstance(value, str):
        return 0
    try:
        return date.fromisoformat(value[:10]).toordinal()
    except ValueError:
        return 0


def _group_date_rank(group: dict[str, Any], *, newest: bool) -> int:
    ranks = [_date_rank(row.get("date")) for row in group["rows"]]
    return (max if newest else min)(ranks, default=0)


def _group_is_source_change(
    group: dict[str, Any], processed: dict[str, Any]
) -> bool:
    return any(
        str(row["point_id"]) in processed
        and _processed_digest(processed.get(str(row["point_id"])))
        != str(row["payload_digest"])
        for row in group["rows"]
    )


def _order_current_groups(
    groups: list[dict[str, Any]],
    processed: dict[str, Any],
    selection_turn: int,
) -> list[dict[str, Any]]:
    """Order current work as fresh head, cold continuity, then fair scopes."""

    if len(groups) < 2:
        return list(groups)
    queues: dict[str, list[dict[str, Any]]] = {}
    for group in groups:
        queues.setdefault(group["scope"], []).append(group)
    scopes = sorted(queues)
    rotation = selection_turn % len(scopes)
    rotated_scopes = scopes[rotation:] + scopes[:rotation]
    rotation_rank = {scope: index for index, scope in enumerate(rotated_scopes)}
    for scope, queue in queues.items():
        queue.sort(
            key=lambda group: (
                -int(_group_is_source_change(group, processed)),
                -_group_date_rank(group, newest=True),
                str(group["run_id"]),
                str(group["episode_id"]),
            )
        )

    ordered: list[dict[str, Any]] = []

    def remove(group: dict[str, Any]) -> None:
        queue = queues[group["scope"]]
        queue.remove(group)

    heads = [queues[scope][0] for scope in rotated_scopes if queues[scope]]
    fresh = min(
        heads,
        key=lambda group: (
            -int(_group_is_source_change(group, processed)),
            -_group_date_rank(group, newest=True),
            rotation_rank[group["scope"]],
            str(group["episode_id"]),
        ),
    )
    remove(fresh)
    ordered.append(fresh)

    remaining = [group for queue in queues.values() for group in queue]
    if remaining:
        historical = [
            group
            for group in remaining
            if not _group_is_source_change(group, processed)
        ] or remaining
        oldest = min(
            historical,
            key=lambda group: (
                _group_date_rank(group, newest=False),
                rotation_rank[group["scope"]],
                str(group["episode_id"]),
            ),
        )
        remove(oldest)
        ordered.append(oldest)

    fair_queues = {scope: deque(queue) for scope, queue in queues.items()}
    remaining_count = sum(len(queue) for queue in fair_queues.values())
    while remaining_count:
        for scope in rotated_scopes:
            if fair_queues[scope]:
                ordered.append(fair_queues[scope].popleft())
                remaining_count -= 1
    return ordered


def _select_groups(
    retry_groups: list[dict[str, Any]],
    current_groups: list[dict[str, Any]],
    fact_limit: int | None,
    episode_limit: int | None,
    selection_turn: int,
) -> list[dict[str, Any]]:
    """Bound work without letting retry or current-source groups monopolize it."""

    if fact_limit is None and episode_limit is None:
        return retry_groups + current_groups
    if fact_limit is not None and fact_limit < 0:
        raise ValueError("fact_limit must be non-negative")
    if episode_limit is not None and episode_limit < 0:
        raise ValueError("episode_limit must be non-negative")
    if fact_limit == 0 or episode_limit == 0:
        return []

    queues = {"retry": list(retry_groups), "current": list(current_groups)}
    preferred = "retry" if selection_turn % 2 == 0 else "current"
    selected: list[dict[str, Any]] = []
    used = 0
    while queues["retry"] or queues["current"]:
        if episode_limit is not None and len(selected) >= episode_limit:
            break
        alternate = "current" if preferred == "retry" else "retry"
        picked_kind = None
        for kind in (preferred, alternate):
            queue = queues[kind]
            if not queue:
                continue
            size = len(queue[0]["rows"])
            if fact_limit is None or used + size <= fact_limit or not selected:
                picked_kind = kind
                break
        if picked_kind is None:
            break
        group = queues[picked_kind].pop(0)
        selected.append(group)
        used += len(group["rows"])
        preferred = "current" if picked_kind == "retry" else "retry"
        if fact_limit is not None and used >= fact_limit:
            break
    return selected


def _pending_group_queues(
    facts: list[dict[str, Any]],
    state: dict[str, Any],
    registry: ScopeRegistry,
    *,
    falkor_prefix: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Build retry and current queues while preserving exact group identity."""

    processed = state["graph_processed"]
    current_unique: dict[tuple[str, str], dict[str, Any]] = {}
    retry_rows: dict[str, list[dict[str, Any]]] = {}
    retry_order: dict[str, tuple[int, str, str]] = {}
    recovery_of: dict[str, str] = {}
    for row in facts:
        point_id = str(row["point_id"])
        digest = str(row["payload_digest"])
        if _processed_digest(processed.get(point_id)) == digest:
            continue
        retry_episode_id = row.get("_feed_retry_episode_id")
        if retry_episode_id:
            episode_id = str(retry_episode_id)
            retry_rows.setdefault(episode_id, []).append(row)
            retry_order[episode_id] = (
                int(row.get("_feed_retry_attempts") or 0),
                str(row.get("_feed_retry_last_attempt_mdt") or ""),
                episode_id,
            )
            recovery_original = row.get("_feed_recovery_of_episode_id")
            if recovery_original is not None:
                original_episode_id = str(recovery_original)
                if original_episode_id != episode_id:
                    raise ValueError("recovery row does not match retry episode")
                recovery_of[episode_id] = original_episode_id
        else:
            current_unique[(point_id, digest)] = row

    rows = list(current_unique.values())
    rows.sort(
        key=lambda row: (
            row["scope"],
            row["run_id"],
            row["point_id"],
            row["payload_digest"],
        )
    )

    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault((row["scope"], row["run_id"]), []).append(row)

    group_ids: dict[str, str] = {}

    def make_group(
        group_rows: list[dict[str, Any]],
        *,
        expected_episode_id: str | None = None,
    ) -> dict[str, Any]:
        scope = group_rows[0]["scope"]
        if scope not in group_ids:
            group_ids[scope] = f"{falkor_prefix}{registry.ensure_scope(scope)}"
        return _group_from_rows(
            group_rows,
            registry,
            falkor_prefix=falkor_prefix,
            expected_episode_id=expected_episode_id,
            group_id=group_ids[scope],
        )

    current_groups: list[dict[str, Any]] = []
    for (scope, run_id), group_rows in sorted(grouped.items()):
        for start in range(0, len(group_rows), MAX_FACTS_PER_EPISODE):
            chunk = group_rows[start : start + MAX_FACTS_PER_EPISODE]
            current_groups.append(make_group(chunk))

    current_groups = _order_current_groups(
        current_groups,
        processed,
        int(state.get("selection_turn") or 0),
    )

    retry_groups: list[dict[str, Any]] = []
    for episode_id in sorted(retry_rows, key=lambda item: retry_order[item]):
        group = make_group(
            retry_rows[episode_id],
            expected_episode_id=episode_id,
        )
        if episode_id in recovery_of:
            group = _recovery_revision_group(group, recovery_of[episode_id])
        retry_groups.append(group)
    return retry_groups, current_groups


def build_pending_plan(
    facts: list[dict[str, Any]],
    state: dict[str, Any],
    registry: ScopeRegistry,
    *,
    falkor_prefix: str = "",
    fact_limit: int | None = None,
    episode_limit: int | None = None,
) -> dict[str, Any]:
    """Return selected groups and count-only backlog evidence."""

    retry_groups, current_groups = _pending_group_queues(
        facts,
        state,
        registry,
        falkor_prefix=falkor_prefix,
    )
    selected = _select_groups(
        retry_groups,
        current_groups,
        fact_limit,
        episode_limit,
        int(state.get("selection_turn") or 0),
    )
    retry_ids = {group["episode_id"] for group in retry_groups}
    selected_retry = [group for group in selected if group["episode_id"] in retry_ids]
    selected_current = [group for group in selected if group["episode_id"] not in retry_ids]
    return {
        "groups": selected,
        "observed_pending_retry_facts": sum(
            len(group["rows"]) for group in retry_groups
        ),
        "observed_pending_retry_episodes": len(retry_groups),
        "observed_pending_current_facts": sum(
            len(group["rows"]) for group in current_groups
        ),
        "observed_pending_current_episodes": len(current_groups),
        "selected_facts": sum(len(group["rows"]) for group in selected),
        "selected_episodes": len(selected),
        "selected_retry_facts": sum(
            len(group["rows"]) for group in selected_retry
        ),
        "selected_retry_episodes": len(selected_retry),
        "selected_current_facts": sum(
            len(group["rows"]) for group in selected_current
        ),
        "selected_current_episodes": len(selected_current),
        "deferred_retry_facts": sum(
            len(group["rows"]) for group in retry_groups
        )
        - sum(len(group["rows"]) for group in selected_retry),
        "deferred_retry_episodes": len(retry_groups) - len(selected_retry),
        "deferred_current_facts": sum(
            len(group["rows"]) for group in current_groups
        )
        - sum(len(group["rows"]) for group in selected_current),
        "deferred_current_episodes": len(current_groups) - len(selected_current),
    }


def build_pending_groups(
    facts: list[dict[str, Any]],
    state: dict[str, Any],
    registry: ScopeRegistry,
    *,
    falkor_prefix: str = "",
    fact_limit: int | None = None,
    episode_limit: int | None = None,
) -> list[dict[str, Any]]:
    """Compatibility list view of the deterministic pending plan."""

    return build_pending_plan(
        facts,
        state,
        registry,
        falkor_prefix=falkor_prefix,
        fact_limit=fact_limit,
        episode_limit=episode_limit,
    )["groups"]


async def _maybe_await(value: Any) -> Any:
    if hasattr(value, "__await__"):
        return await value
    return value


def _consume_task_result(task: asyncio.Task[Any]) -> None:
    with contextlib.suppress(BaseException):
        task.result()


async def _close_resource_bounded(resource: Any, timeout_s: float) -> None:
    """Bound cleanup after all write tasks have already stopped."""

    close = getattr(resource, "close", None) or getattr(resource, "aclose", None)
    if close is None:
        return
    result = close()
    if not hasattr(result, "__await__"):
        return
    task = asyncio.create_task(_maybe_await(result))
    done, _ = await asyncio.wait({task}, timeout=timeout_s)
    if task not in done:
        task.cancel()
        task.add_done_callback(_consume_task_result)
        raise TimeoutError("resource close exceeded its cleanup bound")
    await task


async def _add_graph_episode(
    graph_writer: Any, group: dict[str, Any], timeout_s: float
) -> Any:
    if timeout_s <= 0:
        raise ValueError("episode_timeout_s must be positive")
    method = getattr(graph_writer, "add_episode", None)
    if method is None:
        raise TypeError("graph writer must provide add_episode(group)")
    try:
        return await asyncio.wait_for(_maybe_await(method(group)), timeout=timeout_s)
    except asyncio.TimeoutError as exc:
        raise EpisodeTimeout from exc


def _result_count(result: Any, name: str) -> int:
    if isinstance(result, dict):
        value = result.get(name)
        return int(value) if isinstance(value, (int, float)) else 0
    value = getattr(result, name, None)
    try:
        return len(value or [])
    except TypeError:
        return 0


async def _read_page(
    source: Any, cursor_before: Any, page_limit: int
) -> tuple[list[dict[str, Any]], Any]:
    page = await _maybe_await(source.scroll(cursor_before, page_limit))
    if isinstance(page, tuple) and len(page) == 2:
        points, cursor_after = page
    elif isinstance(page, dict):
        result = page.get("result", page)
        points = result.get("points") or []
        cursor_after = result.get("next_page_offset")
    else:
        raise TypeError("source.scroll must return (points, next_page_offset) or an object")
    if not isinstance(points, list):
        raise ValueError("Qdrant scroll points must be a list")
    return points, cursor_after


def _cursor_identity(cursor: Any) -> str:
    try:
        return json.dumps(cursor, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        return repr(cursor)


async def _read_source_window(
    source: Any,
    cursor_before: Any,
    page_limit: int,
    scan_point_limit: int | None,
    scan_page_limit: int,
) -> tuple[list[dict[str, Any]], Any, dict[str, Any]]:
    """Read a bounded multi-page window before scheduling expensive graph work."""

    if page_limit <= 0:
        raise ValueError("page_limit must be positive")
    if scan_point_limit is not None and scan_point_limit <= 0:
        raise ValueError("scan_point_limit must be positive")
    if scan_page_limit <= 0:
        raise ValueError("scan_page_limit must be positive")

    effective_point_limit = scan_point_limit or page_limit
    points: list[dict[str, Any]] = []
    cursor = cursor_before
    cursor_after = cursor_before
    pages = 0
    seen = {_cursor_identity(cursor_before)} if cursor_before is not None else set()
    while pages < scan_page_limit and len(points) < effective_point_limit:
        request_limit = min(page_limit, effective_point_limit - len(points))
        page_points, cursor_after = await _read_page(source, cursor, request_limit)
        points.extend(page_points)
        pages += 1
        if cursor_after is None:
            break
        identity = _cursor_identity(cursor_after)
        if identity in seen:
            raise ValueError("source cursor did not advance")
        seen.add(identity)
        cursor = cursor_after

    full_collection_from_start = cursor_before is None and cursor_after is None
    return (
        points,
        cursor_after,
        {
            "points": len(points),
            "pages": pages,
            "point_limit": effective_point_limit,
            "page_limit": scan_page_limit,
            "ended_at_collection_end": cursor_after is None,
            "full_collection_from_start": full_collection_from_start,
            "non_atomic": True,
        },
    )


def _candidate_facts(
    state: dict[str, Any], current_facts: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    retry_facts: list[dict[str, Any]] = []
    current_by_point = {str(row["point_id"]): row for row in current_facts}
    active_retry_pairs: set[tuple[str, str]] = set()
    blocked_pairs: set[tuple[str, str]] = set()
    for episode_id, retry_entry in sorted(state["retry"].items()):
        if not isinstance(retry_entry, dict) or retry_entry.get("kind") == "projection":
            continue
        rows = _retry_row(retry_entry)
        if not rows:
            continue
        if any(
            _processed_digest(state["graph_processed"].get(str(row["point_id"])))
            == str(row["payload_digest"])
            for row in rows
        ):
            state["retry"].pop(episode_id, None)
            continue
        if any(
            str(row["point_id"]) in current_by_point
            and str(current_by_point[str(row["point_id"])]["payload_digest"])
            != str(row["payload_digest"])
            for row in rows
        ):
            retry_entry["retryable"] = False
            retry_entry["status"] = "superseded_by_source"
            continue
        pairs = {
            (str(row["point_id"]), str(row["payload_digest"])) for row in rows
        }
        recovery_ready = (
            retry_entry.get("retryable") is False
            and retry_entry.get("status") == "needs_native_reconciliation"
        )
        if retry_entry.get("retryable") is False and not recovery_ready:
            blocked_pairs.update(pairs)
            continue
        active_retry_pairs.update(pairs)
        for index, row in enumerate(rows):
            candidate = dict(row)
            candidate["_feed_retry_episode_id"] = str(episode_id)
            candidate["_feed_retry_row_index"] = index
            candidate["_feed_retry_attempts"] = int(
                retry_entry.get(
                    "recovery_attempts" if recovery_ready else "attempts"
                )
                or 0
            )
            candidate["_feed_retry_last_attempt_mdt"] = str(
                retry_entry.get(
                    "recovery_last_attempt_mdt"
                    if recovery_ready
                    else "last_attempt_mdt"
                )
                or ""
            )
            if recovery_ready:
                candidate["_feed_recovery_of_episode_id"] = str(episode_id)
            retry_facts.append(candidate)

    current = [
        row
        for row in current_facts
        if (str(row["point_id"]), str(row["payload_digest"]))
        not in active_retry_pairs | blocked_pairs
    ]
    return retry_facts + current


def _result_already_present(result: Any) -> bool:
    if isinstance(result, dict):
        return bool(result.get("already_present"))
    return bool(getattr(result, "already_present", False))


def _new_feed_counters(
    points: list[dict[str, Any]],
    current_facts: list[dict[str, Any]],
    groups: list[dict[str, Any]],
    worker_count: int,
) -> dict[str, Any]:
    return {
        "scanned": len(points),
        "organic_points": len(current_facts),
        "new_points": 0,
        "queued_episodes": len(groups),
        "slice_facts": sum(len(group["rows"]) for group in groups),
        "new_episodes": 0,
        "duplicate_episodes": 0,
        "recovery_revisions": 0,
        "recovery_retry_count": 0,
        "retry_count": 0,
        "projection_events": 0,
        "graph_nodes": 0,
        "graph_edges": 0,
        "worker_count": worker_count,
    }


def _apply_worker_results(
    state: dict[str, Any], worker_results: list[dict[str, Any]], counters: dict[str, Any]
) -> None:
    """Apply worker outcomes from the coordinator's single state-writer seam."""

    for worker_result in sorted(worker_results, key=lambda item: item["worker"]):
        for outcome in worker_result["outcomes"]:
            group = outcome["group"]
            episode_id = group["episode_id"]
            recovery_of = group.get("recovery_of_episode_id")
            retry_key = str(recovery_of or episode_id)
            if not outcome["ok"]:
                previous_retry = state["retry"].get(retry_key) or {}
                if recovery_of is not None:
                    recovery_attempts = int(
                        previous_retry.get("recovery_attempts") or 0
                    ) + 1
                    recovery_unproven = (
                        outcome["error_type"] == EpisodeCompletionUnproven.__name__
                    )
                    updated_retry = dict(previous_retry)
                    updated_retry.update(
                        {
                            "kind": "graph",
                            "episode_id": retry_key,
                            "rows": [
                                _row_for_retry(row) for row in group["rows"]
                            ],
                            "status": (
                                "recovery_completion_unproven"
                                if recovery_unproven
                                else "needs_native_reconciliation"
                            ),
                            "retryable": False,
                            "recovery_schema": RECOVERY_SCHEMA,
                            "recovery_episode_id": episode_id,
                            "recovery_attempts": recovery_attempts,
                            "recovery_last_error": outcome["error_type"],
                            "recovery_last_attempt_mdt": now_mdt_text(),
                        }
                    )
                    state["retry"][retry_key] = updated_retry
                    counters["retry_count"] += 1
                    counters["recovery_retry_count"] += 1
                    continue
                attempts = int(previous_retry.get("attempts") or 0) + 1
                needs_reconciliation = (
                    outcome["error_type"] == EpisodeCompletionUnproven.__name__
                )
                retryable = not needs_reconciliation or (
                    attempts < MAX_RECONCILIATION_ATTEMPTS
                )
                state["retry"][retry_key] = {
                    "kind": "graph",
                    "episode_id": episode_id,
                    "rows": [_row_for_retry(row) for row in group["rows"]],
                    "attempts": attempts,
                    "last_error": outcome["error_type"],
                    "last_attempt_mdt": now_mdt_text(),
                    "status": (
                        "needs_native_reconciliation"
                        if needs_reconciliation
                        else "retryable"
                    ),
                    "retryable": retryable,
                }
                counters["retry_count"] += 1
                continue

            result = outcome.get("result")
            # This is the post-Graphiti-success seam. Only the coordinator
            # marks point digests processed and adds deterministic projection
            # work. Workers never receive the mutable state object.
            completed_at = now_mdt_text()
            recovery_fields = (
                {
                    "recovery_schema": RECOVERY_SCHEMA,
                    "recovery_of_episode_id": str(recovery_of),
                }
                if recovery_of is not None
                else {}
            )
            for row in group["rows"]:
                state["graph_processed"][row["point_id"]] = {
                    "point_id": row["point_id"],
                    "payload_digest": row["payload_digest"],
                    "scope": row["scope"],
                    "run_id": row["run_id"],
                    "group_id": group["group_id"],
                    "episode_id": episode_id,
                    "processed_at_mdt": completed_at,
                    **recovery_fields,
                }
                identity = projection_identity(episode_id, row)
                if identity not in state["projection_pending"]:
                    state["projection_pending"][identity] = {
                        "identity": identity,
                        "episode_id": episode_id,
                        "point_id": row["point_id"],
                        "payload_digest": row["payload_digest"],
                        "scope": row["scope"],
                        "run_id": row["run_id"],
                        "group_id": group["group_id"],
                        "status": "pending",
                        "created_at_mdt": completed_at,
                        **recovery_fields,
                    }
                    counters["projection_events"] += 1
            state["retry"].pop(retry_key, None)
            if recovery_of is not None:
                counters["recovery_revisions"] += 1
            if _result_already_present(result):
                counters["duplicate_episodes"] += 1
            else:
                counters["new_episodes"] += 1
            counters["graph_nodes"] += _result_count(result, "nodes")
            counters["graph_edges"] += _result_count(result, "edges")


def _worker_progress_line(
    worker_index: int,
    group_count: int,
    attempts_done: int,
    episodes_done: int,
    facts_done: int,
    failed: int,
    progress_changed_epoch: int,
    started: float,
) -> str:
    elapsed = max(time.monotonic() - started, 0.001)
    rate = facts_done / elapsed * 3600.0
    return (
        "WORKER-PROGRESS "
        f"worker={worker_index} shard={worker_index} groups={group_count} "
        f"attempts_done={attempts_done} episodes_done={episodes_done} "
        f"facts_done={facts_done} failed={failed} "
        f"progress_changed_epoch={progress_changed_epoch} "
        f"rate_facts_per_hour={rate:.1f}"
    )


async def _worker_progress_loop(
    worker_index: int,
    group_count: int,
    stats: dict[str, int],
    started: float,
    stop: asyncio.Event,
    interval_s: float,
) -> None:
    while True:
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_s)
            return
        except asyncio.TimeoutError:
            log(
                _worker_progress_line(
                    worker_index,
                    group_count,
                    stats["attempts_done"],
                    stats["episodes_done"],
                    stats["facts_done"],
                    stats["failed"],
                    stats["progress_changed_epoch"],
                    started,
                )
            )


async def _make_worker_writer(
    graph_writer: Any, worker_index: int, episode_timeout_s: float
) -> Any:
    if graph_writer is None:
        return GraphitiWriter(
            worker_index=worker_index,
            episode_timeout_s=episode_timeout_s,
        )
    if hasattr(graph_writer, "add_episode"):
        return graph_writer
    if callable(graph_writer):
        return await _maybe_await(graph_writer(worker_index))
    raise TypeError("graph writer must provide add_episode(group) or be a factory")


async def _run_worker(
    worker_index: int,
    groups: list[dict[str, Any]],
    graph_writer: Any,
    *,
    emit_progress: bool,
    progress_interval_s: float,
    episode_timeout_s: float,
    pass_deadline: float,
    cleanup_margin_s: float,
    outcome_queue: asyncio.Queue[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    started = time.monotonic()
    stats = {
        "attempts_done": 0,
        "episodes_done": 0,
        "facts_done": 0,
        "failed": 0,
        "max_attempt_timeout_s": 0.0,
        "deadline_stopped": False,
        "progress_changed_epoch": int(time.time()),
    }
    outcomes: list[dict[str, Any]] = []
    writer = None
    owns_writer = not (
        graph_writer is not None and hasattr(graph_writer, "add_episode")
    )
    stop = asyncio.Event()
    progress_task: asyncio.Task[Any] | None = None

    async def publish(outcome: dict[str, Any]) -> None:
        if outcome_queue is None:
            outcomes.append(outcome)
            return
        acknowledgement = asyncio.get_running_loop().create_future()
        await outcome_queue.put(
            {
                "kind": "outcome",
                "worker": worker_index,
                "outcome": outcome,
                "acknowledgement": acknowledgement,
            }
        )
        await acknowledgement

    def progress() -> None:
        if emit_progress:
            log(
                _worker_progress_line(
                    worker_index,
                    len(groups),
                    stats["attempts_done"],
                    stats["episodes_done"],
                    stats["facts_done"],
                    stats["failed"],
                    stats["progress_changed_epoch"],
                    started,
                )
            )

    def remaining_attempt_budget() -> float:
        return min(
            episode_timeout_s,
            pass_deadline - time.monotonic() - cleanup_margin_s,
        )

    if emit_progress:
        progress()
        progress_task = asyncio.create_task(
            _worker_progress_loop(
                worker_index,
                len(groups),
                stats,
                started,
                stop,
                progress_interval_s,
            )
        )
    try:
        if groups and remaining_attempt_budget() > 0:
            try:
                writer = await _make_worker_writer(
                    graph_writer, worker_index, episode_timeout_s
                )
            except Exception as exc:  # noqa: BLE001 - attempted setup failures retry
                for group in groups:
                    if remaining_attempt_budget() <= 0:
                        stats["deadline_stopped"] = True
                        break
                    stats["attempts_done"] += 1
                    stats["failed"] += 1
                    stats["progress_changed_epoch"] = int(time.time())
                    await publish(
                        {
                            "group": group,
                            "ok": False,
                            "error_type": type(exc).__name__,
                        }
                    )
                    progress()
            else:
                for group in groups:
                    attempt_timeout_s = remaining_attempt_budget()
                    if attempt_timeout_s <= 0:
                        stats["deadline_stopped"] = True
                        break
                    stats["max_attempt_timeout_s"] = max(
                        stats["max_attempt_timeout_s"], attempt_timeout_s
                    )
                    try:
                        result = await _add_graph_episode(
                            writer, group, attempt_timeout_s
                        )
                    except Exception as exc:  # noqa: BLE001 - retry at next pass
                        stats["failed"] += 1
                        outcome = {
                            "group": group,
                            "ok": False,
                            "error_type": type(exc).__name__,
                        }
                    else:
                        stats["episodes_done"] += 1
                        stats["facts_done"] += len(group["rows"])
                        outcome = {"group": group, "ok": True, "result": result}
                    stats["attempts_done"] += 1
                    stats["progress_changed_epoch"] = int(time.time())
                    await publish(outcome)
                    progress()
        elif groups:
            stats["deadline_stopped"] = True
    finally:
        stop.set()
        if progress_task is not None:
            progress_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await progress_task
        progress()
        if writer is not None and owns_writer:
            close = getattr(writer, "close", None)
            if close is not None:
                with contextlib.suppress(Exception):
                    await _maybe_await(close())

    elapsed = max(time.monotonic() - started, 0.001)
    return {
        "worker": worker_index,
        "shard": worker_index,
        "groups_total": len(groups),
        "graph_keys": len({group["group_id"] for group in groups}),
        "attempts_done": stats["attempts_done"],
        "episodes_done": stats["episodes_done"],
        "facts_done": stats["facts_done"],
        "failed": stats["failed"],
        "groups_unattempted": len(groups) - stats["attempts_done"],
        "facts_unattempted": sum(
            len(group["rows"]) for group in groups[stats["attempts_done"] :]
        ),
        "deadline_stopped": stats["deadline_stopped"],
        "max_attempt_timeout_s": round(stats["max_attempt_timeout_s"], 3),
        "rate_facts_per_hour": round(stats["facts_done"] / elapsed * 3600.0, 2),
        "outcomes": outcomes,
    }


async def _worker_entry(
    outcome_queue: asyncio.Queue[dict[str, Any]],
    worker_index: int,
    groups: list[dict[str, Any]],
    graph_writer: Any,
    *,
    emit_progress: bool,
    progress_interval_s: float,
    episode_timeout_s: float,
    pass_deadline: float,
    cleanup_margin_s: float,
) -> None:
    """Report worker completion or failure through the coordinator queue."""

    try:
        result = await _run_worker(
            worker_index,
            groups,
            graph_writer,
            emit_progress=emit_progress,
            progress_interval_s=progress_interval_s,
            episode_timeout_s=episode_timeout_s,
            pass_deadline=pass_deadline,
            cleanup_margin_s=cleanup_margin_s,
            outcome_queue=outcome_queue,
        )
    except asyncio.CancelledError:
        raise
    except BaseException as exc:
        await outcome_queue.put(
            {
                "kind": "worker_error",
                "worker": worker_index,
                "exception": exc,
            }
        )
    else:
        await outcome_queue.put(
            {"kind": "worker_done", "worker": worker_index, "result": result}
        )


async def _run_coordinated(
    source: Any,
    graph_writer: Any,
    *,
    state_path: str | Path,
    scope_map_path: str | Path,
    collection: str,
    falkor_prefix: str,
    page_limit: int,
    scan_point_limit: int | None,
    scan_page_limit: int,
    fact_limit: int | None,
    episode_limit: int | None,
    receipt_id: str | None,
    worker_count: int,
    emit_progress: bool,
    progress_interval_s: float,
    episode_timeout_s: float,
    pass_budget_s: float,
    cleanup_margin_s: float,
    on_start: Callable[[dict[str, Any]], Any] | None,
) -> dict[str, Any]:
    """Run one bounded source window with one coordinator and N graph workers."""

    if page_limit <= 0:
        raise ValueError("page_limit must be positive")
    if worker_count <= 0:
        raise ValueError("worker_count must be positive")
    if progress_interval_s <= 0 or progress_interval_s > 60:
        raise ValueError("progress_interval_s must be > 0 and <= 60")
    if episode_timeout_s <= 0:
        raise ValueError("episode_timeout_s must be positive")
    if pass_budget_s <= 0:
        raise ValueError("pass_budget_s must be positive")
    if cleanup_margin_s < 0 or cleanup_margin_s >= pass_budget_s:
        raise ValueError("cleanup_margin_s must be non-negative and below pass_budget_s")

    episode_timeout_s = min(float(episode_timeout_s), MAX_EPISODE_TIMEOUT_S)
    pass_started = time.monotonic()
    pass_deadline = pass_started + float(pass_budget_s)

    state_file = Path(state_path)
    state = load_state(state_file)
    prune_projected_pending(
        state, state_file.with_name("graph-recall-projector-ledger.json")
    )
    registry = ScopeRegistry(scope_map_path)
    receipt_id = receipt_id or f"{now_mdt():%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:8]}"
    cursor_before = state["scan_offset"]
    points, cursor_after, source_window = await _read_source_window(
        source,
        cursor_before,
        page_limit,
        scan_point_limit,
        scan_page_limit,
    )

    admission_mode = _admission_mode_for(collection, falkor_prefix)
    admission_counts = {reason: 0 for reason in ADMISSION_REASONS}
    current_facts: list[dict[str, Any]] = []
    source_by_id = {str(point.get("id")): point for point in points if isinstance(point, dict)}
    for point in points:
        payload = point.get("payload") if isinstance(point, dict) else None
        marker = payload.get("retention_marker") if isinstance(payload, dict) else None
        canonical = source_by_id.get(str(marker.get("canonical_id"))) if isinstance(marker, dict) else None
        fact, reason = classify_point(point, admission_mode=admission_mode, canonical_payload=canonical)
        admission_counts[reason] += 1
        if fact is not None:
            current_facts.append(fact)
    plan = build_pending_plan(
        _candidate_facts(state, current_facts),
        state,
        registry,
        falkor_prefix=falkor_prefix,
        fact_limit=fact_limit,
        episode_limit=episode_limit,
    )
    groups = plan["groups"]
    counters = _new_feed_counters(points, current_facts, groups, worker_count)
    counters["new_points"] = sum(
        _processed_digest(state["graph_processed"].get(row["point_id"]))
        != row["payload_digest"]
        for row in current_facts
    )
    shards = assign_groups_to_shards(groups, worker_count)
    start_info = {
        "scanned": counters["scanned"],
        "new_points": counters["new_points"],
        "slice_episodes": counters["queued_episodes"],
        "slice_facts": counters["slice_facts"],
        "worker_count": worker_count,
        "cursor_before": cursor_before,
        "cursor_after": cursor_after,
        "scan_epoch": state["scan_epoch"],
        "source_pages": source_window["pages"],
        "source_full_collection_from_start": source_window[
            "full_collection_from_start"
        ],
        "observed_pending_current_facts": plan[
            "observed_pending_current_facts"
        ],
        "deferred_current_facts": plan["deferred_current_facts"],
    }
    if on_start is not None:
        await _maybe_await(on_start(start_info))

    outcome_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    worker_results: list[dict[str, Any]] = []
    selection_turn_checkpointed = False
    worker_tasks = [
        asyncio.create_task(
            _worker_entry(
                outcome_queue,
                worker_index,
                shard_groups,
                graph_writer,
                emit_progress=emit_progress,
                progress_interval_s=progress_interval_s,
                episode_timeout_s=episode_timeout_s,
                pass_deadline=pass_deadline,
                cleanup_margin_s=cleanup_margin_s,
            ),
            name=f"graph-worker-{worker_index}",
        )
        for worker_index, shard_groups in enumerate(shards)
    ]
    try:
        completed_workers = 0
        while completed_workers < worker_count:
            event = await outcome_queue.get()
            if event["kind"] == "worker_error":
                raise event["exception"]
            if event["kind"] == "worker_done":
                worker_results.append(event["result"])
                completed_workers += 1
                continue

            acknowledgement = event["acknowledgement"]
            try:
                _apply_worker_results(
                    state,
                    [{"worker": event["worker"], "outcomes": [event["outcome"]]}],
                    counters,
                )
                if not selection_turn_checkpointed:
                    state["selection_turn"] += 1
                    selection_turn_checkpointed = True
                # Batch size one: an outcome is accepted only by this atomic
                # coordinator save. Fair selection advances with the first
                # outcome; the page cursor stays unchanged here.
                save_state_atomic(state_file, state)
            except BaseException as exc:
                if not acknowledgement.done():
                    acknowledgement.set_exception(exc)
                raise
            else:
                acknowledgement.set_result(None)

        await asyncio.gather(*worker_tasks)
        unattempted_episodes = sum(
            result["groups_unattempted"] for result in worker_results
        )
        unattempted_facts = sum(
            result["facts_unattempted"] for result in worker_results
        )
        deadline_stopped = any(
            result["deadline_stopped"] for result in worker_results
        )
        committed_cursor_after = (
            cursor_before if unattempted_episodes else cursor_after
        )
        state["scan_offset"] = committed_cursor_after
        if not selection_turn_checkpointed:
            state["selection_turn"] += 1
        if not unattempted_episodes and cursor_after is None:
            state["scan_epoch"] += 1
            state["last_complete_scan_mdt"] = now_mdt_text()
        state["receipt_counters"][receipt_id] = dict(counters)
        # Only this final save may advance the page cursor. Holding it when a
        # selected group was not attempted makes that group visible next pass.
        save_state_atomic(state_file, state)
    finally:
        for task in worker_tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*worker_tasks, return_exceptions=True)

    graph_retry_entries = [
        entry
        for entry in state["retry"].values()
        if isinstance(entry, dict) and entry.get("kind") != "projection"
    ]
    retry_status_counts: dict[str, int] = {}
    for entry in graph_retry_entries:
        status = str(entry.get("status") or "retryable")
        retry_status_counts[status] = retry_status_counts.get(status, 0) + 1
    unresolved_reconciliation = sum(
        entry.get("retryable") is False for entry in graph_retry_entries
    )
    pass_elapsed_s = round(max(time.monotonic() - pass_started, 0.0), 3)
    receipt = {
        "schema": "graph-feed/2",
        "receipt_id": receipt_id,
        "at_mdt": now_mdt_text(),
        "collection": collection,
        "falkor_prefix": falkor_prefix,
        "scan_epoch": state["scan_epoch"],
        "cursor_before": cursor_before,
        "cursor_after": committed_cursor_after,
        "fact_limit": fact_limit,
        "episode_limit": episode_limit,
        "episode_timeout_s": episode_timeout_s,
        "pass_budget_s": float(pass_budget_s),
        "cleanup_margin_s": float(cleanup_margin_s),
        "pass_elapsed_s": pass_elapsed_s,
        "deadline_stopped": deadline_stopped,
        "unattempted_episodes": unattempted_episodes,
        "unattempted_facts": unattempted_facts,
        "cursor_held_for_unattempted": bool(unattempted_episodes),
        "source_window": source_window,
        "admission_counts": admission_counts,
        "backlog": {
            key: value for key, value in plan.items() if key != "groups"
        }
        | {
            "denominator_kind": (
                "full_collection_non_atomic"
                if source_window["full_collection_from_start"]
                else "cursor_window_non_atomic"
            ),
            "unresolved_reconciliation_episodes": unresolved_reconciliation,
            "budget_deferred_episodes": unattempted_episodes,
            "budget_deferred_facts": unattempted_facts,
        },
        "checkpoint_batch_size": CHECKPOINT_BATCH_SIZE,
        "max_uncheckpointed_successes": worker_count,
        "pending_graph_retries": len(graph_retry_entries),
        "unresolved_reconciliation": unresolved_reconciliation,
        "retry_status_counts": retry_status_counts,
        "worker_stats": [
            {
                key: value
                for key, value in worker_result.items()
                if key != "outcomes"
            }
            for worker_result in sorted(worker_results, key=lambda item: item["worker"])
        ],
        **counters,
        "outcome": (
            "FAIL"
            if counters["retry_count"]
            else "PARTIAL"
            if graph_retry_entries or unattempted_episodes
            else "PASS"
        ),
    }
    return receipt


async def run_once(
    source: Any,
    graph_writer: Any,
    *,
    state_path: str | Path = STATE_F,
    scope_map_path: str | Path = SCOPE_MAP_F,
    collection: str = DEFAULT_COLLECTION,
    falkor_prefix: str = "",
    page_limit: int = DEFAULT_PAGE_LIMIT,
    scan_point_limit: int | None = None,
    scan_page_limit: int = 1,
    fact_limit: int | None = None,
    episode_limit: int | None = None,
    receipt_id: str | None = None,
    episode_timeout_s: float = DEFAULT_EPISODE_TIMEOUT_S,
    pass_budget_s: float = DEFAULT_PASS_BUDGET_S,
    cleanup_margin_s: float = DEFAULT_CLEANUP_MARGIN_S,
) -> dict[str, Any]:
    """Compatibility serial pass using the same coordinator state seam."""

    return await _run_coordinated(
        source,
        graph_writer,
        state_path=state_path,
        scope_map_path=scope_map_path,
        collection=collection,
        falkor_prefix=falkor_prefix,
        page_limit=page_limit,
        scan_point_limit=scan_point_limit,
        scan_page_limit=scan_page_limit,
        fact_limit=fact_limit,
        episode_limit=episode_limit,
        receipt_id=receipt_id,
        worker_count=1,
        emit_progress=False,
        progress_interval_s=DEFAULT_PROGRESS_INTERVAL_S,
        episode_timeout_s=episode_timeout_s,
        pass_budget_s=pass_budget_s,
        cleanup_margin_s=cleanup_margin_s,
        on_start=None,
    )


async def run_parallel_once(
    source: Any,
    graph_writer: Any = None,
    *,
    state_path: str | Path = STATE_F,
    scope_map_path: str | Path = SCOPE_MAP_F,
    collection: str = DEFAULT_COLLECTION,
    falkor_prefix: str = "",
    page_limit: int = DEFAULT_PAGE_LIMIT,
    scan_point_limit: int | None = None,
    scan_page_limit: int = 1,
    fact_limit: int | None = None,
    episode_limit: int | None = None,
    receipt_id: str | None = None,
    worker_count: int = DEFAULT_WORKERS,
    emit_progress: bool = True,
    progress_interval_s: float = DEFAULT_PROGRESS_INTERVAL_S,
    episode_timeout_s: float = DEFAULT_EPISODE_TIMEOUT_S,
    pass_budget_s: float = DEFAULT_PASS_BUDGET_S,
    cleanup_margin_s: float = DEFAULT_CLEANUP_MARGIN_S,
    on_start: Callable[[dict[str, Any]], Any] | None = None,
) -> dict[str, Any]:
    """Run one bounded scroll window through disjoint graph-key shards.

    ``graph_writer`` may be a writer factory accepting the worker index. The
    live CLI passes ``None`` so every non-empty shard receives its own
    ``GraphitiWriter`` and therefore its own model-door client.
    """

    return await _run_coordinated(
        source,
        graph_writer,
        state_path=state_path,
        scope_map_path=scope_map_path,
        collection=collection,
        falkor_prefix=falkor_prefix,
        page_limit=page_limit,
        scan_point_limit=scan_point_limit,
        scan_page_limit=scan_page_limit,
        fact_limit=fact_limit,
        episode_limit=episode_limit,
        receipt_id=receipt_id,
        worker_count=worker_count,
        emit_progress=emit_progress,
        progress_interval_s=progress_interval_s,
        episode_timeout_s=episode_timeout_s,
        pass_budget_s=pass_budget_s,
        cleanup_margin_s=cleanup_margin_s,
        on_start=on_start,
    )


async def drain_projection(state_path: str | Path, projector: Any) -> dict[str, int]:
    """Compatibility read-only helper; production draining owns a ledger.

    Older GF-02 tests use this seam to prove a projection failure does not
    re-add a Graphiti episode. It deliberately never saves feed state. The
    production projector uses its own ledger-backed ``drain`` method below.
    """

    state = load_state(state_path)
    attempted = delivered = failed = 0
    for identity, pending in sorted(state["projection_pending"].items()):
        attempted += 1
        try:
            await _maybe_await(projector.project(identity, pending))
        except Exception:  # noqa: BLE001 - caller observes retryable failure
            failed += 1
        else:
            delivered += 1
    return {"attempted": attempted, "delivered": delivered, "failed": failed}


class QdrantSource:
    """Small payload-only Qdrant scroll client used by one feed pass."""

    def __init__(
        self,
        collection: str = DEFAULT_COLLECTION,
        *,
        base_url: str = QDRANT,
        client: httpx.Client | None = None,
    ):
        import httpx

        self.collection = collection
        self.base_url = base_url.rstrip("/")
        self.client = client or httpx.Client(timeout=30)
        self._owns_client = client is None

    def scroll(self, offset: Any, limit: int) -> tuple[list[dict[str, Any]], Any]:
        body: dict[str, Any] = {
            "limit": limit,
            "with_payload": True,
            "with_vector": False,
            "filter": {
                "must": [{"key": "user_id", "match": {"value": OWNER_ID}}],
                "must_not": [{"key": "run_id", "match": {"value": "corpus-seed"}}]
            },
        }
        if offset is not None:
            body["offset"] = offset
        response = self.client.post(
            f"{self.base_url}/collections/{self.collection}/points/scroll", json=body
        )
        response.raise_for_status()
        result = response.json()["result"]
        return result.get("points") or [], result.get("next_page_offset")

    def collection_exists(self) -> bool:
        response = self.client.get(f"{self.base_url}/collections/{self.collection}")
        return response.status_code == 200

    def create_collection(self) -> None:
        response = self.client.put(
            f"{self.base_url}/collections/{self.collection}",
            json={"vectors": {"size": int(CONFIG.values["BORG_EMBED_DIMS"]), "distance": "Cosine"}},
        )
        response.raise_for_status()

    def point_ids(self) -> set[str]:
        ids: set[str] = set()
        offset: Any = None
        while True:
            points, offset = self.scroll(offset, 256)
            ids.update(str(point["id"]) for point in points if point.get("id") is not None)
            if offset is None:
                return ids

    def upsert(self, points: list[dict[str, Any]]) -> None:
        response = self.client.put(
            f"{self.base_url}/collections/{self.collection}/points?wait=true",
            json={"points": points},
        )
        response.raise_for_status()

    def close(self) -> None:
        if self._owns_client:
            self.client.close()


class GraphitiWriter:
    """Graphiti adapter with deterministic episode UUIDs and duplicate guard."""

    _COMPLETION_PROPERTY = "graph_feed_completion_token"

    def __init__(
        self,
        *,
        graph_host: str = str(CONFIG.values["BORG_FALKORDB_HOST"]),
        graph_port: int = int(CONFIG.values["BORG_FALKORDB_PORT"]),
        llm_url: str | None = None,
        worker_index: int = 0,
        episode_timeout_s: float = DEFAULT_EPISODE_TIMEOUT_S,
        native_request_timeout_s: float | None = None,
        native_max_retries: int | None = None,
        client_close_timeout_s: float = DEFAULT_CLIENT_CLOSE_TIMEOUT_S,
    ):
        from openai import AsyncOpenAI
        from graphiti_core import Graphiti
        from graphiti_core.cross_encoder.openai_reranker_client import OpenAIRerankerClient
        from graphiti_core.driver.falkordb_driver import FalkorDriver
        from graphiti_core.embedder.openai import OpenAIEmbedder, OpenAIEmbedderConfig
        from graphiti_core.llm_client import LLMConfig
        from graphiti_core.llm_client.openai_generic_client import OpenAIGenericClient

        shim = normalize_llm_url(str(CONFIG.values["BORG_OLLAMA_URL"]) if CONFIG.portable else os.environ.get("MEM0_EMBED_OLLAMA_URL", "http://127.0.0.1:11439"))
        urls = llm_url_pool()
        v6_url = normalize_llm_url(llm_url) if llm_url else urls[worker_index % len(urls)]
        model = str(CONFIG.values["BORG_GRAPH_MODEL"]) if CONFIG.portable else os.environ.get(
            "GRAPH_LLM_MODEL", "mlx-community/Qwen3-4B-Instruct-2507-4bit"
        )
        configured_request_timeout = (
            env_float(
                "GRAPH_NATIVE_REQUEST_TIMEOUT_S",
                DEFAULT_NATIVE_REQUEST_TIMEOUT_S,
            )
            if native_request_timeout_s is None
            else float(native_request_timeout_s)
        )
        request_timeout = min(configured_request_timeout, float(episode_timeout_s))
        retries = (
            env_int("GRAPH_NATIVE_MAX_RETRIES", DEFAULT_NATIVE_MAX_RETRIES)
            if native_max_retries is None
            else int(native_max_retries)
        )
        if request_timeout <= 0:
            raise ValueError("native request timeout must be positive")
        if retries < 0:
            raise ValueError("native max retries must be non-negative")
        if client_close_timeout_s <= 0:
            raise ValueError("client close timeout must be positive")
        llm_transport = AsyncOpenAI(
            api_key="ollama",
            base_url=v6_url,
            timeout=request_timeout,
            max_retries=retries,
        )
        llm = OpenAIGenericClient(
            config=LLMConfig(
                api_key="ollama",
                model=model,
                small_model=model,
                base_url=v6_url,
                temperature=0.0,
            ),
            client=llm_transport,
        )
        embedding_transport = AsyncOpenAI(
            api_key="ollama",
            base_url=shim,
            timeout=request_timeout,
            max_retries=retries,
        )
        embedder = OpenAIEmbedder(
            config=OpenAIEmbedderConfig(
                embedding_model=str(CONFIG.values["BORG_EMBED_MODEL"]),
                embedding_dim=int(CONFIG.values["BORG_EMBED_DIMS"]),
                api_key="ollama",
                base_url=shim,
            ),
            client=embedding_transport,
        )
        reranker = OpenAIRerankerClient(
            config=LLMConfig(
                api_key="ollama", model=model, base_url=v6_url, temperature=0.0
            ),
            client=llm.client,
        )
        self._Graphiti = Graphiti
        self._FalkorDriver = FalkorDriver
        self._graph_host = graph_host
        self._graph_port = graph_port
        self._llm = llm
        self._embedder = embedder
        self._reranker = reranker
        self._owned_clients = [llm_transport, embedding_transport]
        self._client_close_timeout_s = float(client_close_timeout_s)
        self.native_request_timeout_s = request_timeout
        self.native_max_retries = retries
        self.llm_url = v6_url
        self.worker_index = int(worker_index)
        self._graphs: dict[str, Any] = {}

    def _graph(self, group_id: str) -> Any:
        graph = self._graphs.get(group_id)
        if graph is None:
            driver = self._FalkorDriver(
                host=self._graph_host,
                port=self._graph_port,
                database=group_id,
            )
            graph = self._Graphiti(
                graph_driver=driver,
                llm_client=self._llm,
                embedder=self._embedder,
                cross_encoder=self._reranker,
            )
            self._graphs[group_id] = graph
        return graph

    @staticmethod
    def _query_records(result: Any) -> list[dict[str, Any]]:
        records = result[0] if isinstance(result, tuple) else result
        if not isinstance(records, list):
            return []
        return [record for record in records if isinstance(record, dict)]

    async def _read_completion_proof(
        self, driver: Any, episode_uuid: str, group_id: str
    ) -> dict[str, Any]:
        result = await driver.execute_query(
            "MATCH (e:Episodic {uuid: $episode_uuid}) "
            "WHERE e.group_id = $group_id "
            "RETURN e.graph_feed_completion_token AS completion_token, "
            "e.graph_feed_recovery_schema AS recovery_schema, "
            "e.graph_feed_recovery_of_episode_uuid AS recovery_of_episode_id "
            "LIMIT 1",
            episode_uuid=episode_uuid,
            group_id=group_id,
        )
        records = self._query_records(result)
        return records[0] if records else {}

    @staticmethod
    def _completion_proof_matches(
        group: dict[str, Any], proof: dict[str, Any], completion_token: str
    ) -> bool:
        if proof.get("completion_token") != completion_token:
            return False
        recovery_of = group.get("recovery_of_episode_id")
        if recovery_of is None:
            return True
        return (
            proof.get("recovery_schema") == RECOVERY_SCHEMA
            and proof.get("recovery_of_episode_id") == str(recovery_of)
        )

    async def _mark_complete(
        self, driver: Any, group: dict[str, Any], completion_token: str
    ) -> None:
        recovery_of = group.get("recovery_of_episode_id")
        query = (
            "MATCH (e:Episodic {uuid: $episode_uuid}) "
            "WHERE e.group_id = $group_id "
            "SET e.graph_feed_completion_token = $completion_token "
        )
        params = {
            "episode_uuid": group["episode_id"],
            "group_id": group["group_id"],
            "completion_token": completion_token,
        }
        if recovery_of is not None:
            query += (
                ", e.graph_feed_recovery_schema = $recovery_schema "
                ", e.graph_feed_recovery_of_episode_uuid = $recovery_of_episode_id "
            )
            params.update(
                {
                    "recovery_schema": RECOVERY_SCHEMA,
                    "recovery_of_episode_id": str(recovery_of),
                }
            )
        query += (
            "RETURN e.graph_feed_completion_token AS completion_token, "
            "e.graph_feed_recovery_schema AS recovery_schema, "
            "e.graph_feed_recovery_of_episode_uuid AS recovery_of_episode_id"
        )
        result = await driver.execute_query(query, **params)
        records = self._query_records(result)
        if not records or not self._completion_proof_matches(
            group, records[0], completion_token
        ):
            raise EpisodeCompletionUnproven(
                "graph completion marker could not be verified"
            )

    async def add_episode(self, group: dict[str, Any]) -> dict[str, int | bool]:
        from graphiti_core.nodes import EpisodeType, EpisodicNode
        from graphiti_core.errors import NodeNotFoundError

        graph = self._graph(group["group_id"])
        driver = graph.driver
        episode_uuid = group["episode_id"]
        completion_token = episode_completion_token(group)
        try:
            existing = await EpisodicNode.get_by_uuid(driver, episode_uuid)
        except NodeNotFoundError:
            existing = None

        # Content equality is not completion proof: the required pre-extraction
        # stub already has the final content. Only a marker written after
        # Graphiti returns can reconcile a missed local checkpoint.
        if existing is not None:
            recorded = await self._read_completion_proof(
                driver, episode_uuid, group["group_id"]
            )
            if self._completion_proof_matches(group, recorded, completion_token):
                return {"nodes": 0, "edges": 0, "already_present": True}
            raise EpisodeCompletionUnproven(
                "deterministic episode exists without matching completion proof"
            )
        if existing is None:
            stub = EpisodicNode(
                uuid=episode_uuid,
                name=group["run_id"],
                group_id=group["group_id"],
                labels=[],
                source=EpisodeType.text,
                source_description=group["source_description"],
                # Graphiti Core 0.29.3 loads the EXISTING node when a uuid is
                # passed and extracts from ITS content; an empty stub yielded
                # zero entities for every episode (measured 2026-09-03). Seed
                # the real text so extraction sees it.
                content=group["episode_body"],
                valid_at=group["reference_time"],
            )
            await stub.save(driver)

        result = await graph.add_episode(
            name=group["run_id"],
            episode_body=group["episode_body"],
            source_description=group["source_description"],
            source=EpisodeType.text,
            reference_time=group["reference_time"],
            group_id=group["group_id"],
            uuid=episode_uuid,
        )
        await self._mark_complete(driver, group, completion_token)
        return {
            "nodes": len(getattr(result, "nodes", []) or []),
            "edges": len(getattr(result, "edges", []) or []),
            "already_present": False,
        }

    async def close(self) -> None:
        errors: list[BaseException] = []
        graphs = list(self._graphs.values())
        self._graphs.clear()
        for graph in graphs:
            try:
                await _close_resource_bounded(
                    graph, self._client_close_timeout_s
                )
            except BaseException as exc:
                errors.append(exc)
        closed: set[int] = set()
        for client in self._owned_clients:
            if id(client) in closed:
                continue
            closed.add(id(client))
            try:
                await _close_resource_bounded(
                    client, self._client_close_timeout_s
                )
            except BaseException as exc:
                errors.append(exc)
        self._owned_clients.clear()
        if errors:
            raise errors[0]


def acquire_lock(path: str | Path = LOCK_F) -> bool:
    """Acquire a PID lock without replacing a live owner."""

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        try:
            pid = int(target.read_text(encoding="utf-8").strip())
            os.kill(pid, 0)
            return False
        except (OSError, ValueError):
            target.unlink(missing_ok=True)
            fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(str(os.getpid()))
    return True


def release_lock(path: str | Path = LOCK_F) -> None:
    Path(path).unlink(missing_ok=True)


class _ScenarioSource:
    def __init__(self, pages: dict[Any, tuple[list[dict[str, Any]], Any]]):
        self.pages = pages
        self.offsets: list[Any] = []

    def scroll(self, offset: Any, _limit: int) -> tuple[list[dict[str, Any]], Any]:
        self.offsets.append(offset)
        return self.pages.get(offset, ([], None))


class _ScenarioGraph:
    def __init__(self, failures: int = 0):
        self.calls: list[dict[str, Any]] = []
        self.failures = failures

    async def add_episode(self, group: dict[str, Any]) -> dict[str, int]:
        self.calls.append(group)
        if self.failures:
            self.failures -= 1
            raise RuntimeError("forced extraction failure")
        return {"nodes": len(group["rows"]), "edges": 0}


async def behavioral_canary_proof() -> dict[str, str]:
    """Run all six GF-02 behavioral cases against the real state machine."""

    cases: dict[str, str] = {}

    with tempfile.TemporaryDirectory(prefix="graph-feed-canary-proof-") as temp:
        root = Path(temp)

        def make_point(point_id: str, text: str, run_id: str, scope: str) -> dict[str, Any]:
            return {
                "id": point_id,
                "payload": {
                    "data": text,
                    "run_id": run_id,
                    "scope": scope,
                    "user_id": OWNER_ID,
                    "thread_date": "2026-09-03",
                    "kind": "canary",
                },
            }

        def paths(name: str) -> tuple[Path, Path]:
            return root / f"{name}.json", root / f"{name}-scopes.json"

        async def run_scenario(source: Any, graph: Any, **kwargs: Any) -> dict[str, Any]:
            return await run_once(
                source,
                graph,
                collection=CANARY_COLLECTION,
                falkor_prefix=CANARY_PREFIX,
                **kwargs,
            )

        state, scope_map = paths("cursor")
        graph = _ScenarioGraph()
        source = _ScenarioSource({None: ([make_point("after", "after", "r", CANARY_SCOPES[0])], "o")})
        await run_scenario(source, graph, state_path=state, scope_map_path=scope_map)
        cases["point_after_cursor"] = "PASS" if load_state(state)["scan_offset"] == "o" else "FAIL"

        state, scope_map = paths("wrap")
        graph = _ScenarioGraph()
        await run_scenario(
            _ScenarioSource({None: ([make_point("middle", "middle", "r", CANARY_SCOPES[0])], "o")}),
            graph,
            state_path=state,
            scope_map_path=scope_map,
        )
        await run_scenario(
            _ScenarioSource({"o": ([make_point("last", "last", "r", CANARY_SCOPES[0])], None)}),
            graph,
            state_path=state,
            scope_map_path=scope_map,
        )
        await run_scenario(
            _ScenarioSource({None: ([make_point("before", "before", "r", CANARY_SCOPES[0])], "o")}),
            graph,
            state_path=state,
            scope_map_path=scope_map,
        )
        cases["point_before_offset_after_wrap"] = "PASS" if any(
            row["point_id"] == "before" for row in graph.calls[-1]["rows"]
        ) else "FAIL"

        state, scope_map = paths("delta")
        graph = _ScenarioGraph()
        await run_scenario(
            _ScenarioSource({None: ([make_point("old", "old", "old-run", CANARY_SCOPES[0])], None)}),
            graph,
            state_path=state,
            scope_map_path=scope_map,
        )
        await run_scenario(
            _ScenarioSource({None: ([
                make_point("old", "old", "old-run", CANARY_SCOPES[0]),
                make_point("delta", "delta", "old-run", CANARY_SCOPES[0]),
            ], None)}),
            graph,
            state_path=state,
            scope_map_path=scope_map,
        )
        cases["old_run_delta_episode"] = "PASS" if [
            row["point_id"] for row in graph.calls[-1]["rows"]
        ] == ["delta"] else "FAIL"

        state, scope_map = paths("idempotency")
        graph = _ScenarioGraph()
        source = _ScenarioSource({None: ([make_point("same", "same", "r", CANARY_SCOPES[0])], None)})
        first = await run_scenario(source, graph, state_path=state, scope_map_path=scope_map)
        second = await run_scenario(source, graph, state_path=state, scope_map_path=scope_map)
        cases["identical_replay"] = "PASS" if (
            len(graph.calls) == 1
            and first["new_episodes"] == 1
            and second["new_episodes"] == 0
            and second["projection_events"] == 0
        ) else "FAIL"

        state, scope_map = paths("retry")
        graph = _ScenarioGraph(failures=1)
        failed = await run_scenario(
            _ScenarioSource({None: ([make_point("retry", "retry", "r", CANARY_SCOPES[0])], None)}),
            graph,
            state_path=state,
            scope_map_path=scope_map,
        )
        cases["failed_extraction_retryable"] = "PASS" if (
            failed["retry_count"] == 1
            and not load_state(state)["graph_processed"]
            and load_state(state)["retry"]
        ) else "FAIL"

        state, scope_map = paths("scopes")
        graph = _ScenarioGraph()
        await run_scenario(
            _ScenarioSource({None: ([
                make_point("a", "a", "r", CANARY_SCOPES[0]),
                make_point("b", "b", "r", CANARY_SCOPES[1]),
            ], None)}),
            graph,
            state_path=state,
            scope_map_path=scope_map,
        )
        cases["scope_isolation"] = "PASS" if (
            len(graph.calls) == 2
            and len({call["group_id"] for call in graph.calls}) == 2
            and all(len(call["rows"]) == 1 for call in graph.calls)
        ) else "FAIL"

    return cases


def _canary_point(point_name: str, scope: str, run_id: str) -> dict[str, Any]:
    point_id = str(uuid.uuid5(uuid.UUID("e1dfbe2b-7f1f-4c45-8b04-bd8e617f5d2c"), point_name))
    return {
        "id": point_id,
        "vector": [0.0] * int(CONFIG.values["BORG_EMBED_DIMS"]),
        "payload": {
            "data": f"GF-02 isolated canary fact {point_name}",
            "run_id": run_id,
            "scope": scope,
            "user_id": OWNER_ID,
            "thread_date": "2026-09-03",
            "kind": "gf-02-canary",
        },
    }


async def _graph_counts(group_ids: list[str]) -> dict[str, dict[str, int]]:
    from graphiti_core.driver.falkordb_driver import FalkorDriver

    counts: dict[str, dict[str, int]] = {}
    for group_id in group_ids:
        driver = FalkorDriver(host=str(CONFIG.values["BORG_FALKORDB_HOST"]), port=int(CONFIG.values["BORG_FALKORDB_PORT"]), database=group_id)
        try:
            node_result = await driver.execute_query("MATCH (n) RETURN count(n) AS count")
            edge_result = await driver.execute_query("MATCH ()-[r]->() RETURN count(r) AS count")
            episode_result = await driver.execute_query(
                "MATCH (e:Episodic) RETURN count(e) AS count"
            )
            counts[group_id] = {
                "nodes": int(node_result[0][0]["count"]),
                "edges": int(edge_result[0][0]["count"]),
                "episodes": int(episode_result[0][0]["count"]),
            }
        finally:
            await driver.close()
    return counts


async def run_isolated_canary(*, page_limit: int = 100, json_output: bool = False) -> dict[str, Any]:
    collection = os.environ.get("GRAPH_FEED_CANARY_COLLECTION", CANARY_COLLECTION)
    prefix = os.environ.get("GRAPH_FEED_CANARY_PREFIX", CANARY_PREFIX)
    if collection != CANARY_COLLECTION or prefix != CANARY_PREFIX:
        raise ValueError("isolated canary names do not match the reserved GF-02 names")

    source = QdrantSource(collection=collection)
    state_path = CANARY_STATE_F if CONFIG.portable else Path(os.environ.get("GRAPH_FEED_CANARY_STATE_FILE", CANARY_STATE_F))
    scope_map_path = CANARY_SCOPE_MAP_F if CONFIG.portable else Path(
        os.environ.get("GRAPH_FEED_CANARY_SCOPE_MAP", CANARY_SCOPE_MAP_F)
    )
    points = [
        _canary_point("base-a", CANARY_SCOPES[0], "gf-02-canary-run-a"),
        _canary_point("base-b", CANARY_SCOPES[1], "gf-02-canary-run-b"),
    ]
    try:
        if not source.collection_exists():
            source.create_collection()
        existing_ids = source.point_ids()
        missing = [point for point in points if str(point["id"]) not in existing_ids]
        if missing:
            source.upsert(missing)

        registry = ScopeRegistry(scope_map_path)
        group_ids = [f"{prefix}{registry.ensure_scope(scope)}" for scope in CANARY_SCOPES]
        before = await _graph_counts(group_ids)
        previous_state = load_state(state_path)
        writer = GraphitiWriter()
        try:
            receipt = await run_once(
                source,
                writer,
                state_path=state_path,
                scope_map_path=scope_map_path,
                collection=collection,
                falkor_prefix=prefix,
                page_limit=page_limit,
            )
        finally:
            await writer.close()
        after = await _graph_counts(group_ids)
        state_after = load_state(state_path)
        receipt["graph_counts_before"] = before
        receipt["graph_counts_after"] = after
        receipt["projection_count_before"] = len(previous_state["projection_pending"])
        receipt["projection_count_after"] = len(state_after["projection_pending"])
        receipt["graph_keys"] = group_ids
        receipt["behavioral_cases"] = await behavioral_canary_proof()
        receipt["behavioral_outcome"] = (
            "PASS"
            if all(value == "PASS" for value in receipt["behavioral_cases"].values())
            else "FAIL"
        )
        receipt["outcome"] = (
            "PASS"
            if receipt["outcome"] == "PASS" and receipt["behavioral_outcome"] == "PASS"
            else "FAIL"
        )
        if json_output:
            print(json.dumps(receipt, sort_keys=True), flush=True)
        return receipt
    finally:
        source.close()


def safe_receipt_for_log(receipt: dict[str, Any]) -> dict[str, Any]:
    """Remove source-store cursor values before a receipt reaches stdout."""

    safe = dict(receipt)
    before = safe.pop("cursor_before", None)
    after = safe.pop("cursor_after", None)
    safe["cursor_before_present"] = before is not None
    safe["cursor_after_present"] = after is not None
    return safe


async def async_main(args: argparse.Namespace) -> int:
    if args.canary:
        receipt = await run_isolated_canary(page_limit=args.fetch, json_output=args.json)
        return 0 if receipt["outcome"] == "PASS" else 1
    if not env_flag("GRAPH_FEED_LIVE", default=False):
        line = log(
            "RUN-START scanned=0 new_points=0 slice_episodes=0 slice_facts=0 "
            "outcome=NOT_RUN graph_feed_live=0 default-off",
            echo=not args.json,
        )
        log("RUN-END new_episodes=0 retry_count=0 outcome=NOT_RUN", echo=not args.json)
        if args.json:
            print(
                json.dumps(
                    {
                        "schema": "graph-feed/2",
                        "at_mdt": now_mdt_text(),
                        "outcome": "NOT_RUN",
                        "reason": "GRAPH_FEED_LIVE=0",
                        "log": line,
                    },
                    sort_keys=True,
                )
            )
        return 0

    state_path = STATE_F if CONFIG.portable else Path(os.environ.get("GRAPH_FEED_STATE_FILE", STATE_F))
    scope_map_path = SCOPE_MAP_F if CONFIG.portable else Path(os.environ.get("GRAPH_FEED_SCOPE_MAP", SCOPE_MAP_F))
    source = QdrantSource(collection=DEFAULT_COLLECTION)
    if CONFIG.portable:
        ScopeRegistry(scope_map_path).ensure_scope(str(CONFIG.values["BORG_MEMORY_SCOPE"]))

    def run_start(info: dict[str, Any]) -> None:
        log(
            "RUN-START "
            f"scanned={info['scanned']} new_points={info['new_points']} "
            f"slice_episodes={info['slice_episodes']} slice_facts={info['slice_facts']} "
            f"source_pages={info['source_pages']} "
            f"source_full={int(info['source_full_collection_from_start'])} "
            f"observed_pending_current_facts={info['observed_pending_current_facts']} "
            f"deferred_current_facts={info['deferred_current_facts']} "
            f"workers={info['worker_count']} "
            f"cursor_before_present={int(info['cursor_before'] is not None)} "
            f"cursor_after_present={int(info['cursor_after'] is not None)} "
            f"scan_epoch={info['scan_epoch']}",
            echo=not args.json,
        )

    try:
        receipt = await run_parallel_once(
            source,
            state_path=state_path,
            scope_map_path=scope_map_path,
            collection=DEFAULT_COLLECTION,
            page_limit=args.fetch,
            scan_point_limit=args.scan_points,
            scan_page_limit=args.scan_pages,
            fact_limit=args.limit,
            episode_limit=args.episodes,
            worker_count=args.workers,
            emit_progress=True,
            progress_interval_s=min(
                env_float("GRAPH_FEED_PROGRESS_INTERVAL_S", DEFAULT_PROGRESS_INTERVAL_S),
                DEFAULT_PROGRESS_INTERVAL_S,
            ),
            episode_timeout_s=args.episode_timeout,
            pass_budget_s=args.pass_budget,
            cleanup_margin_s=args.cleanup_margin,
            on_start=run_start,
        )
        log(
            "RUN-END "
            f"new_episodes={receipt['new_episodes']} retry_count={receipt['retry_count']} "
            f"projection_events={receipt['projection_events']} "
            f"unattempted_episodes={receipt['unattempted_episodes']} "
            f"pass_elapsed_s={receipt['pass_elapsed_s']:.3f} "
            f"pass_budget_s={receipt['pass_budget_s']:g} "
            f"outcome={receipt['outcome']}",
            echo=not args.json,
        )
        if args.json:
            print(json.dumps(safe_receipt_for_log(receipt), sort_keys=True), flush=True)
        return 0 if receipt["outcome"] in {"PASS", "PARTIAL"} else 1
    finally:
        source.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="one incremental Graphiti feed pass")
    parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_FACT_LIMIT,
        help="target maximum facts in this pass (whole episodes remain indivisible)",
    )
    parser.add_argument(
        "--fetch", type=int, default=DEFAULT_PAGE_LIMIT, help="Qdrant page size"
    )
    parser.add_argument(
        "--scan-points",
        type=int,
        default=DEFAULT_SCAN_POINT_LIMIT,
        help="maximum source points inspected before graph selection",
    )
    parser.add_argument(
        "--scan-pages",
        type=int,
        default=DEFAULT_SCAN_PAGE_LIMIT,
        help="maximum Qdrant pages inspected before graph selection",
    )
    parser.add_argument(
        "--episodes",
        type=int,
        default=DEFAULT_EPISODE_LIMIT,
        help="maximum graph episode attempts in this pass",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=DEFAULT_WORKERS,
        help="number of disjoint graph-key extraction workers",
    )
    parser.add_argument(
        "--episode-timeout",
        type=float,
        default=env_float("GRAPH_EPISODE_TIMEOUT_S", DEFAULT_EPISODE_TIMEOUT_S),
        help="maximum seconds for one graph add before retryable timeout",
    )
    parser.add_argument(
        "--pass-budget",
        type=float,
        default=env_float("GRAPH_PASS_BUDGET_S", DEFAULT_PASS_BUDGET_S),
        help="shared wall seconds for scanning and graph attempts in this pass",
    )
    parser.add_argument(
        "--cleanup-margin",
        type=float,
        default=env_float("GRAPH_PASS_CLEANUP_MARGIN_S", DEFAULT_CLEANUP_MARGIN_S),
        help="pass seconds reserved before starting another graph attempt",
    )
    parser.add_argument("--canary", action="store_true", help="run only the reserved isolated canary")
    parser.add_argument("--json", action="store_true", help="emit the receipt as JSON")
    args = parser.parse_args()

    if not acquire_lock():
        if args.json:
            print(json.dumps({"outcome": "NOT_RUN", "reason": "already-running"}))
        else:
            print("already running", flush=True)
        return 0
    atexit.register(release_lock)
    try:
        return asyncio.run(async_main(args))
    except Exception as exc:  # noqa: BLE001 - command reports a hard failure
        error = {
            "schema": "graph-feed/2",
            "at_mdt": now_mdt_text(),
            "outcome": "FAIL",
            "error_type": type(exc).__name__,
        }
        if args.json:
            print(json.dumps(error, sort_keys=True), flush=True)
        else:
            log(f"RUN-END outcome=FAIL error_type={error['error_type']}")
        return 1
    finally:
        release_lock()


if __name__ == "__main__":
    raise SystemExit(main())
