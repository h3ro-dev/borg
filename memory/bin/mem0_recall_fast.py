"""Fast direct recall for the configured BORG memory store.

Purpose
-------
The Codex ``UserPromptSubmit`` hook blocks every prompt while it retrieves
memory candidates. Loading the full ``mem0`` Python stack in that subprocess
costs seconds (mem0 imports, ONNX/fastembed runtime, hybrid BM25 + entity-boost
retrieval). This module reproduces the *recall transport* only, over plain
HTTP, so the blocking path stays in the low hundreds of milliseconds.

Transport
---------
1. ``POST {EMBED_OLLAMA_URL}/api/embed`` with ``nomic-embed-text`` -> 768-dim vector.
2. ``POST {QDRANT_URL}/collections/{COLLECTION}/points/search`` with that
   vector, ``with_payload=true``, a ``score_threshold``, and a payload filter.

Dependencies
------------
Standard library only. ``httpx`` is available in the venv but was measured to
cost ~120 ms more import time than ``urllib.request`` for the same three
loopback POSTs, and connection pooling buys nothing on 127.0.0.1, so this
module deliberately uses ``urllib.request``. It never imports ``mem0``,
``onnxruntime``, ``fastembed``, ``qdrant_client``, or ``ollama``.

Parity with ``mem0.Memory.search``
----------------------------------
* Same collection, same embedding model, same Cosine space.
* Same filter shape: ``user_id`` plus optional ``agent_id`` equality.
* Same threshold semantics: mem0 gates on the raw semantic (cosine) score
  *before* hybrid fusion (``mem0/utils/scoring.py::score_and_rank``), and
  ``score_threshold`` here gates the same raw cosine value.
* Same expiry and empty-payload skips as ``mem0.memory.main``.
* Same row keys the hook consumes (``memory``/``score``/``id``/``agent_id``/
  ``metadata``), so downstream ranking, dedupe and formatting are untouched.

Known, intentional difference: the returned ``score`` is the raw cosine
similarity, not mem0's fused ``(semantic + bm25 + entity_boost) / max_possible``
value. Candidate ordering can therefore differ where BM25 or entity boosts
would have reordered rows.

Failure policy
--------------
Fail open and fail fast. Any error - unreachable Ollama, unreachable Qdrant,
bad JSON, timeout - returns an empty list. This module never raises to its
caller and never logs. Callers own all logging, and must not log memory text.

A caller that needs to tell "nothing matched" apart from "the service is down"
may pass ``errors=[]``; each function appends a short static reason
(``"embed"``, ``"search"``, ``"projection"``, ``"no-agents"``) to that
list. Reasons are fixed
strings and never carry query text, memory text, or host detail.

Environment
-----------
``MEM0_EMBED_OLLAMA_URL`` defaults to the configured BORG Ollama URL.
``MEM0_QDRANT_URL``       defaults to the configured BORG Qdrant URL.
``MEM0_QDRANT_COLLECTION`` comes from the required BORG configuration.
``MEM0_EMBED_MODEL``      defaults to the configured BORG embedding model.
"""

from __future__ import annotations

import json
import hashlib
import importlib.machinery
import os
from pathlib import Path
import re
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from threading import Lock
from typing import Any, Iterable, Sequence

CONFIG = importlib.machinery.SourceFileLoader(
    "borg_config_recall_fast", str(Path(__file__).resolve().parent / "borg_config.py")
).load_module().CONFIG

__all__ = [
    "embed",
    "search",
    "search_projection",
    "search_agents",
    "recall",
    "merge_rows",
    "graph_in_search_enabled",
    "DEFAULT_MIN_SCORE",
    "DEFAULT_TIMEOUT_S",
]

EMBED_OLLAMA_URL = os.environ.get(
    "MEM0_EMBED_OLLAMA_URL", str(CONFIG.values["BORG_OLLAMA_URL"])
).rstrip("/")
# Retain the internal name used by existing tests and local callers while
# separating it from MEM0_OLLAMA_URL, which remains extraction-only.
OLLAMA_URL = EMBED_OLLAMA_URL
QDRANT_URL = os.environ.get("MEM0_QDRANT_URL", str(CONFIG.values["BORG_QDRANT_URL"])).rstrip("/")
COLLECTION = os.environ.get("MEM0_QDRANT_COLLECTION", str(CONFIG.values["BORG_QDRANT_COLLECTION"]))
GRAPH_COLLECTION = os.environ.get("MEM0_GRAPH_RECALL_COLLECTION", COLLECTION + "_graph_recall")
GRAPH_QDRANT_URL = os.environ.get("MEM0_GRAPH_QDRANT_URL", QDRANT_URL).rstrip("/")
EMBED_MODEL = os.environ.get("MEM0_EMBED_MODEL", str(CONFIG.values["BORG_EMBED_MODEL"]))

DEFAULT_USER_ID = str(CONFIG.values["BORG_OWNER_ID"])
DEFAULT_MIN_SCORE = 0.55
# FINDING A1 sentinel: an empty allowed_scopes grant must match nothing.
_IMPOSSIBLE_SCOPE = "__scope_boundary__no_scopes_granted__"
DEFAULT_TIMEOUT_S = 2.5
MIN_LEG_TIMEOUT_S = 0.25
CANDIDATE_POOL_MULTIPLIER = 4
MAX_CANDIDATE_POOL = 100
GRAPH_IN_SEARCH_ENV = "MEM0_GRAPH_IN_SEARCH"
GRAPH_LIMIT = 2
GRAPH_MIN_SCORE = float(os.environ.get("MEM0_GRAPH_MIN_SCORE", str(DEFAULT_MIN_SCORE)))
EMBED_TARGET_MS = 160
SEARCH_TARGET_MS = 90
EMBED_TARGET_S = EMBED_TARGET_MS / 1000
SEARCH_TARGET_S = SEARCH_TARGET_MS / 1000
_GRAPH_KEY_RE = re.compile(r"^memscope_[0-9a-f]{24}$")
_SEARCH_POOL: ThreadPoolExecutor | None = None
_SEARCH_POOL_LOCK = Lock()
SELECTOR = importlib.machinery.SourceFileLoader(
    "mem0_selection", str(Path(__file__).resolve().parent / "memory_selection.py")
).load_module()
RETENTION = importlib.machinery.SourceFileLoader(
    "mem0_retention_recall", str(Path(__file__).resolve().parent / "retention.py")
).load_module()

GRAPH_SOURCE_BINDING_SCHEMA = "mem0-graph-source-bindings-v1"
MAX_GRAPH_SOURCE_EPISODES = 25
MAX_SOURCE_IDENTITY_POINTS = MAX_CANDIDATE_POOL
_PAYLOAD_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_SOURCE_BINDING_KEYS = frozenset(
    {"graph_key", "scope", "episode_uuid", "point_id", "payload_digest"}
)

TOMBSTONE_STATUS = "tombstoned"
DECAY_STATUS = "decayed"
RETIRED_STATUSES = (TOMBSTONE_STATUS, DECAY_STATUS, "retired")
RETIREMENT_STATUS_FIELDS = (
    "status",
    "lifecycle_status",
    "memory_status",
    "retention_status",
)
RETIREMENT_FLAG_FIELDS = ("retired", "is_retired", "tombstoned")

# Mirrors mem0.memory.main._search_vector_store step 9 so `metadata` matches
# what mem0 would have handed the hook.
_PROMOTED_PAYLOAD_KEYS = (
    "user_id",
    "agent_id",
    "run_id",
    "actor_id",
    "role",
    "attributed_to",
    "expiration_date",
    "expires_at",
    "valid_until",
)
_CORE_AND_PROMOTED_KEYS = {
    "data",
    "hash",
    "created_at",
    "updated_at",
    "id",
    "text_lemmatized",
    *_PROMOTED_PAYLOAD_KEYS,
}


class _Budget:
    """Monotonic deadline shared by the embed leg and every search leg."""

    def __init__(self, total_s: float) -> None:
        self.deadline = time.monotonic() + max(float(total_s), MIN_LEG_TIMEOUT_S)

    def remaining(self) -> float:
        return self.deadline - time.monotonic()

    def leg(self, cap_s: float | None = None) -> float | None:
        left = self.remaining()
        if left <= 0:
            return None
        if cap_s is not None:
            return min(left, max(float(cap_s), 0.01))
        return max(left, MIN_LEG_TIMEOUT_S)


def _env_flag(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def graph_in_search_enabled() -> bool:
    """Return the transparent graph-recall flag without changing the default."""

    return _env_flag(GRAPH_IN_SEARCH_ENV, default=False)


def _post_json(url: str, body: dict[str, Any], timeout_s: float) -> Any:
    payload = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout_s) as response:
        return json.loads(response.read().decode("utf-8"))


def _note(errors: list[str] | None, reason: str) -> None:
    if errors is not None:
        errors.append(reason)


def _search_pool() -> ThreadPoolExecutor:
    """Reuse the two search workers across prompts in one hook process."""

    global _SEARCH_POOL
    if _SEARCH_POOL is None:
        with _SEARCH_POOL_LOCK:
            if _SEARCH_POOL is None:
                _SEARCH_POOL = ThreadPoolExecutor(
                    max_workers=2,
                    thread_name_prefix="mem0-recall",
                )
    return _SEARCH_POOL


def embed(
    text: str,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    *,
    model: str | None = None,
    errors: list[str] | None = None,
) -> list[float] | None:
    """Return the query embedding, or None on any failure."""
    clean = (text or "").strip()
    if not clean:
        return None
    try:
        result = _post_json(
            f"{OLLAMA_URL}/api/embed",
            {"model": model or EMBED_MODEL, "input": clean},
            timeout_s,
        )
        vectors = result.get("embeddings") if isinstance(result, dict) else None
        if isinstance(vectors, list) and vectors and isinstance(vectors[0], list) and vectors[0]:
            return [float(value) for value in vectors[0]]
    except Exception:
        _note(errors, "embed")
        return None
    _note(errors, "embed")
    return None


def _build_filter(user_id: str | None, agent_id: str | None, scopes: Sequence[str] | None = None) -> dict[str, Any]:
    must: list[dict[str, Any]] = []
    if user_id:
        must.append({"key": "user_id", "match": {"value": user_id}})
    if agent_id:
        must.append({"key": "agent_id", "match": {"value": agent_id}})
    if scopes:
        must.append({"key": "scope", "match": {"any": list(scopes)}})
    query_filter: dict[str, Any] = {
        "must_not": [
            {"key": "status", "match": {"value": status}}
            for status in RETIRED_STATUSES
        ] + [{"key": "is_canary", "match": {"value": True}}]
    }
    if must:
        query_filter["must"] = must
    return query_filter


def _expected_graph_key(scope: str) -> str:
    return "memscope_" + hashlib.sha256(scope.encode("utf-8")).hexdigest()[:24]


def _present(value: Any) -> bool:
    return value not in (None, "", False)


def _is_expired(payload: dict[str, Any]) -> bool:
    """Same rule as mem0.memory.main._payload_is_expired."""
    return SELECTOR.is_explicitly_expired({"metadata": payload})


def _source_payload_is_live(payload: dict[str, Any]) -> bool:
    for key in RETIREMENT_STATUS_FIELDS:
        if key not in payload:
            continue
        value = payload[key]
        if not isinstance(value, str) or value.strip().casefold() in RETIRED_STATUSES:
            return False
    for key in RETIREMENT_FLAG_FIELDS:
        if key in payload and (
            not isinstance(payload[key], bool) or payload[key] is True
        ):
            return False
    if payload.get("retired_at") not in (None, "", False):
        return False
    for key in ("is_canary", "canary"):
        if key in payload and (
            not isinstance(payload[key], bool) or payload[key] is True
        ):
            return False
    return not _is_expired(payload)


def _requested_limit(limit: int) -> int:
    try:
        return max(1, int(limit))
    except (TypeError, ValueError):
        return 1


def _candidate_pool_limit(limit: int) -> int:
    """Over-fetch once, before local expiry/dedupe/diversity selection."""

    return min(MAX_CANDIDATE_POOL, CANDIDATE_POOL_MULTIPLIER * _requested_limit(limit))


def _to_row(point: dict[str, Any]) -> dict[str, Any] | None:
    payload = point.get("payload")
    if not isinstance(payload, dict):
        return None
    text = payload.get("data")
    if not isinstance(text, str) or not text.strip():
        return None
    if not _source_payload_is_live(payload):
        return None

    metadata = {
        key: value for key, value in payload.items() if key not in _CORE_AND_PROMOTED_KEYS
    }
    score = point.get("score")
    row: dict[str, Any] = {
        "id": str(point.get("id") or ""),
        "text": text,
        # `memory` is the key mem0 returns and the key the hook reads.
        "memory": text,
        "score": float(score) if isinstance(score, (int, float)) else 0.0,
        "agent": str(payload.get("agent_id") or ""),
        "agent_id": str(payload.get("agent_id") or ""),
        "kind": str(payload.get("kind") or metadata.get("kind") or ""),
        "created": str(payload.get("created_at") or ""),
        "created_at": payload.get("created_at"),
        "updated_at": payload.get("updated_at"),
        "hash": payload.get("hash"),
        "source": str(metadata.get("source") or ""),
        "run_id": payload.get("run_id"),
        "user_id": payload.get("user_id"),
        "metadata": metadata,
    }
    return row


def _source_scope_allowed(row: dict[str, Any], allowed_scopes: Sequence[str] | None) -> bool:
    """Recheck the provider response at the source boundary, fail closed."""

    if allowed_scopes is None:
        return True
    metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
    scope = metadata.get("scope", row.get("scope"))
    return scope in set(allowed_scopes)


def _payload_digest(payload: dict[str, Any]) -> str:
    """Match the feed's digest of the complete canonical Qdrant payload."""

    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _retention_canonical_id(point: dict[str, Any]) -> Any | None:
    payload = point.get("payload") if isinstance(point, dict) else None
    marker = payload.get(RETENTION.MARKER_KEY) if isinstance(payload, dict) else None
    if not isinstance(marker, dict):
        return None
    value = marker.get("canonical_id")
    if isinstance(value, bool) or value in (None, ""):
        return None
    return value if isinstance(value, (str, int)) else None


def _retention_marker_state(
    point: dict[str, Any], canonical: dict[str, Any] | None
) -> bool | None:
    """Return the supplied retention proof, or unknown if it cannot decide."""

    try:
        return bool(RETENTION.marker_is_active(point, canonical))
    except Exception:
        return None


def _graph_source_bindings(
    point: dict[str, Any], allowed_scopes: Sequence[str]
) -> list[dict[str, str]] | None:
    """Return one structurally complete, scope-bound projection identity."""

    payload = point.get("payload") if isinstance(point, dict) else None
    if not isinstance(payload, dict):
        return None
    if payload.get("source_binding_schema") != GRAPH_SOURCE_BINDING_SCHEMA:
        return None
    scope = payload.get("scope")
    graph_key = payload.get("source_graph_key")
    if (
        not isinstance(scope, str)
        or scope not in set(allowed_scopes)
        or not isinstance(graph_key, str)
    ):
        return None
    raw_episodes = payload.get("episode_uuids")
    if not isinstance(raw_episodes, list):
        return None
    episodes = [str(value).strip() for value in raw_episodes]
    if (
        not episodes
        or any(not value for value in episodes)
        or len(episodes) != len(set(episodes))
        or len(episodes) > MAX_GRAPH_SOURCE_EPISODES
    ):
        return None
    raw_bindings = payload.get("source_bindings")
    if (
        not isinstance(raw_bindings, list)
        or not raw_bindings
        or len(raw_bindings) > MAX_SOURCE_IDENTITY_POINTS
    ):
        return None

    bindings: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    seen_points: set[str] = set()
    covered: set[str] = set()
    for raw in raw_bindings:
        if not isinstance(raw, dict) or set(raw) != _SOURCE_BINDING_KEYS:
            return None
        episode_uuid = raw.get("episode_uuid")
        point_id = raw.get("point_id")
        digest = raw.get("payload_digest")
        if (
            raw.get("graph_key") != graph_key
            or raw.get("scope") != scope
            or not isinstance(episode_uuid, str)
            or episode_uuid not in set(episodes)
            or not isinstance(point_id, str)
            or not point_id.strip()
            or not isinstance(digest, str)
            or _PAYLOAD_DIGEST_RE.fullmatch(digest) is None
        ):
            return None
        identity = (episode_uuid, point_id, digest)
        if identity in seen or point_id in seen_points:
            return None
        seen.add(identity)
        seen_points.add(point_id)
        covered.add(episode_uuid)
        bindings.append(
            {
                "graph_key": graph_key,
                "scope": scope,
                "episode_uuid": episode_uuid,
                "point_id": point_id,
                "payload_digest": digest,
            }
        )
    if covered != set(episodes):
        return None
    return bindings


def _raw_source_allowed(
    point: dict[str, Any],
    *,
    user_id: str | None,
    scopes: Sequence[str] | None,
) -> bool:
    payload = point.get("payload") if isinstance(point, dict) else None
    if not isinstance(payload, dict):
        return False
    if user_id is not None and payload.get("user_id") != user_id:
        return False
    if scopes is not None and payload.get("scope") not in set(scopes):
        return False
    return True


def _retrieve_source_points(
    point_ids: Iterable[Any],
    *,
    user_id: str | None,
    scopes: Sequence[str] | None,
    budget: _Budget,
) -> tuple[dict[str, dict[str, Any]], bool]:
    """Read exact identities once; optional validation failures are nonfatal."""

    requested: dict[str, Any] = {}
    for raw_id in point_ids:
        if isinstance(raw_id, bool) or raw_id in (None, ""):
            continue
        if not isinstance(raw_id, (str, int)):
            continue
        requested.setdefault(str(raw_id), raw_id)
    if not requested:
        return {}, True
    if len(requested) > MAX_SOURCE_IDENTITY_POINTS or (user_id is None and scopes is None):
        return {}, False
    try:
        leg = budget.leg(SEARCH_TARGET_S)
        if leg is None:
            return {}, False
        query_filter = _build_filter(user_id, None, scopes)
        query_filter.setdefault("must", []).insert(
            0, {"has_id": list(requested.values())}
        )
        result = _post_json(
            f"{QDRANT_URL.rstrip('/')}/collections/{COLLECTION}/points/scroll",
            {
                "filter": query_filter,
                "limit": len(requested),
                "with_payload": True,
                "with_vector": False,
            },
            leg,
        )
        page = result.get("result") if isinstance(result, dict) else None
        points = page.get("points") if isinstance(page, dict) else page
        if not isinstance(points, list):
            raise ValueError("invalid source validation response")
        found: dict[str, dict[str, Any]] = {}
        for point in points:
            if not isinstance(point, dict):
                continue
            point_id = str(point.get("id"))
            if (
                point_id in requested
                and _raw_source_allowed(point, user_id=user_id, scopes=scopes)
            ):
                found[point_id] = point
        return found, True
    except Exception:
        return {}, False


def _graph_sources_current(
    bindings: Sequence[dict[str, str]],
    source_points: dict[str, dict[str, Any]],
    *,
    user_id: str | None,
) -> bool:
    for binding in bindings:
        point = source_points.get(binding["point_id"])
        if not isinstance(point, dict):
            return False
        payload = point.get("payload")
        if not isinstance(payload, dict):
            return False
        try:
            observed_digest = _payload_digest(payload)
        except (TypeError, ValueError):
            return False
        if (
            observed_digest != binding["payload_digest"]
            or payload.get("scope") != binding["scope"]
            or (user_id is not None and payload.get("user_id") != user_id)
            or not _source_payload_is_live(payload)
        ):
            return False
        if RETENTION.MARKER_KEY in payload:
            canonical_id = _retention_canonical_id(point)
            canonical = (
                source_points.get(str(canonical_id))
                if canonical_id is not None
                else None
            )
            if canonical_id is not None and canonical is None:
                return False
            if _retention_marker_state(point, canonical) is not False:
                return False
    return True


def _to_graph_row(point: dict[str, Any], allowed_scopes: Sequence[str]) -> dict[str, Any] | None:
    """Convert one derived projection point, failing closed on its boundary."""

    payload = point.get("payload") if isinstance(point, dict) else None
    if not isinstance(payload, dict):
        return None
    if payload.get("source") != "graphiti" or payload.get("is_graph") is not True:
        return None
    scope = payload.get("scope")
    source_graph_key = payload.get("source_graph_key")
    if not isinstance(scope, str) or not scope.strip():
        return None
    scope = scope.strip()
    if scope not in set(allowed_scopes):
        return None
    if not isinstance(source_graph_key, str) or not _GRAPH_KEY_RE.fullmatch(source_graph_key):
        return None
    if source_graph_key != _expected_graph_key(scope):
        return None
    if _graph_source_bindings(point, allowed_scopes) is None:
        return None
    # Graph invalidation and expiry are represented separately from the
    # Qdrant point's ordinary mem0 status. They must never reach a prompt.
    if _present(payload.get("invalid_at")) or _present(payload.get("expired_at")):
        return None
    row = _to_row(point)
    if row is None:
        return None
    metadata = dict(row.get("metadata") or {})
    metadata["source"] = "graphiti"
    metadata["scope"] = scope
    metadata["source_graph_key"] = source_graph_key
    for field in ("graph_edge_uuid", "edge_uuid", "episode_uuids", "valid_at", "invalid_at"):
        if field in payload:
            metadata[field] = payload[field]
    row["metadata"] = metadata
    row["source"] = "graphiti"
    row["is_graph"] = True
    row["graph_edge_uuid"] = str(
        payload.get("graph_edge_uuid") or payload.get("edge_uuid") or point.get("id") or ""
    )
    row["source_graph_key"] = source_graph_key
    row["scope"] = scope
    row["valid_at"] = payload.get("valid_at")
    row["invalid_at"] = payload.get("invalid_at")
    return row


def merge_rows(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge exact source/projection rows while retaining all source pointers."""

    return SELECTOR.merge_exact_rows(rows)


def _search_points(
    vector: Sequence[float],
    *,
    collection: str,
    limit: int,
    min_score: float,
    user_id: str | None,
    agent_id: str | None,
    scopes: Sequence[str] | None,
    budget: _Budget,
    errors: list[str] | None,
    error_reason: str,
    timeout_cap_s: float | None = None,
    base_url: str | None = None,
) -> list[dict[str, Any]]:
    try:
        leg = budget.leg(timeout_cap_s)
        if leg is None:
            _note(errors, error_reason)
            return []
        body = {
            "vector": list(vector),
            "limit": _candidate_pool_limit(limit),
            "with_payload": True,
            "with_vector": False,
            "score_threshold": float(min_score),
            "filter": _build_filter(user_id, agent_id, scopes),
        }
        result = _post_json(
            f"{(base_url or QDRANT_URL).rstrip('/')}/collections/{collection}/points/search",
            body,
            leg,
        )
        points = result.get("result") if isinstance(result, dict) else None
        if not isinstance(points, list):
            _note(errors, error_reason)
            return []
        return [point for point in points if isinstance(point, dict)]
    except Exception:
        _note(errors, error_reason)
        return []


def search_projection(
    query: str,
    *,
    allowed_scopes: Sequence[str] | None,
    limit: int,
    timeout_s: float = 0.5,
    errors: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Return only validated graph-projection rows for concrete scopes.

    This is deliberately a single graph leg. It shares the embedding and
    monotonic budget helpers with ordinary recall, but never queries the
    ordinary Mem0 collection.
    """
    try:
        candidates = (
            [allowed_scopes]
            if isinstance(allowed_scopes, str)
            else (allowed_scopes or [])
        )
        scopes = sorted(
            {
                scope.strip()
                for scope in candidates
                if isinstance(scope, str)
                and scope.strip()
                and scope.strip() != "*"
                and not scope.strip().endswith(":*")
            }
        )
        # Do not even embed when authorization did not yield a concrete scope.
        if not scopes:
            return []

        budget = _Budget(timeout_s)
        embed_timeout = budget.leg()
        if embed_timeout is None:
            _note(errors, "projection")
            return []
        vector = embed(query, embed_timeout, errors=errors)
        if vector is None:
            return []

        points = _search_points(
            vector,
            collection=GRAPH_COLLECTION,
            limit=limit,
            min_score=GRAPH_MIN_SCORE,
            user_id=None,
            agent_id=None,
            scopes=scopes,
            budget=budget,
            errors=errors,
            error_reason="projection",
            timeout_cap_s=SEARCH_TARGET_S,
            base_url=GRAPH_QDRANT_URL,
        )
        candidates: list[tuple[dict[str, Any], list[dict[str, str]]]] = []
        for point in points:
            row = _to_graph_row(point, scopes)
            bindings = _graph_source_bindings(point, scopes)
            if row is not None and bindings is not None:
                candidates.append((row, bindings))
        point_ids = [
            binding["point_id"]
            for _row, bindings in candidates
            for binding in bindings
        ]
        sources, available = _retrieve_source_points(
            point_ids,
            user_id=None,
            scopes=scopes,
            budget=budget,
        )
        rows = [
            row
            for row, bindings in candidates
            if available and _graph_sources_current(bindings, sources, user_id=None)
        ]
        return SELECTOR.select_rows(rows, limit)
    except Exception:
        _note(errors, "projection")
        return []


def search(
    query: str,
    limit: int = 5,
    min_score: float = DEFAULT_MIN_SCORE,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    *,
    user_id: str | None = DEFAULT_USER_ID,
    agent_id: str | None = None,
    scopes: Sequence[str] | None = None,
    allowed_scopes: Sequence[str] | None = None,
    vector: Sequence[float] | None = None,
    budget: _Budget | None = None,
    errors: list[str] | None = None,
    include_graph: bool | None = None,
    graph_collection: str | None = None,
    graph_qdrant_url: str | None = None,
    graph_limit: int = GRAPH_LIMIT,
    graph_min_score: float = GRAPH_MIN_SCORE,
) -> list[dict[str, Any]]:
    """Semantic recall against the configured collection.

    Returns a list of row dicts carrying at least ``text``, ``score``,
    ``agent``, ``kind``, ``created`` and ``id``. Returns ``[]`` on any error.

    ``allowed_scopes`` is the FINDING A1 default-closed boundary: pass it and
    the Qdrant filter constrains rows to those exact scope values; pass it
    EMPTY and the filter matches nothing at all. Rows with no ``scope``
    payload can never match a scope MatchAny, so they are excluded
    structurally. ``scopes`` (legacy) stays advisory-only for old callers.
    """
    try:
        explicit_scope_grant = allowed_scopes is not None
        if allowed_scopes is not None:
            scopes = list(allowed_scopes) or [_IMPOSSIBLE_SCOPE]
        budget = budget or _Budget(timeout_s)
        graph_requested = graph_in_search_enabled() if include_graph is None else bool(include_graph)
        graph_scopes: list[str] | None = None
        if graph_requested:
            # A graph query without a concrete grant would turn a malformed
            # projection into a cross-scope read. It is intentionally omitted.
            graph_scopes = [] if explicit_scope_grant and not allowed_scopes else list(scopes or [])
            if not graph_scopes:
                graph_requested = False
        if vector is None:
            # Embedding is shared by both legs; keep the ordinary Mem0
            # caller budget even when the optional graph leg is enabled.
            leg = budget.leg()
            if leg is None:
                _note(errors, "search")
                return []
            vector = embed(query, leg, errors=errors)
            if vector is None:
                return []
        leg = budget.leg()
        if leg is None:
            _note(errors, "search")
            return []

        source_points: list[dict[str, Any]] = []
        graph_points: list[dict[str, Any]] = []
        if graph_requested:
            pool = _search_pool()
            source_future = pool.submit(
                _search_points,
                vector,
                collection=COLLECTION,
                limit=limit,
                min_score=min_score,
                user_id=user_id,
                agent_id=agent_id,
                scopes=scopes,
                budget=budget,
                errors=errors,
                error_reason="search",
                timeout_cap_s=None,
            )
            graph_future = pool.submit(
                _search_points,
                vector,
                collection=graph_collection or GRAPH_COLLECTION,
                limit=min(max(1, int(graph_limit)), GRAPH_LIMIT),
                min_score=graph_min_score,
                user_id=None,
                agent_id=None,
                scopes=graph_scopes,
                budget=budget,
                errors=errors,
                error_reason="graph-search",
                timeout_cap_s=SEARCH_TARGET_S,
                base_url=graph_qdrant_url or GRAPH_QDRANT_URL,
            )
            source_points = source_future.result()
            graph_points = graph_future.result()
        else:
            source_points = _search_points(
                vector,
                collection=COLLECTION,
                limit=limit,
                min_score=min_score,
                user_id=user_id,
                agent_id=agent_id,
                scopes=scopes,
                budget=budget,
                errors=errors,
                error_reason="search",
            )

        source_candidates: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for point in source_points:
            row = _to_row(point)
            if row is not None and _source_scope_allowed(row, allowed_scopes):
                source_candidates.append((point, row))
        graph_candidates: list[
            tuple[dict[str, Any], list[dict[str, str]]]
        ] = []
        if graph_requested and graph_scopes is not None:
            for point in graph_points:
                row = _to_graph_row(point, graph_scopes)
                bindings = _graph_source_bindings(point, graph_scopes)
                if row is not None and bindings is not None:
                    graph_candidates.append((row, bindings))

        source_identity_ids: list[Any] = [
            canonical_id
            for point, _row in source_candidates
            if (canonical_id := _retention_canonical_id(point)) is not None
        ]
        source_identity_ids.extend(
            binding["point_id"]
            for _row, bindings in graph_candidates
            for binding in bindings
        )
        validated_sources, validation_available = _retrieve_source_points(
            source_identity_ids,
            user_id=user_id,
            scopes=scopes,
            budget=budget,
        )

        rows: list[dict[str, Any]] = []
        for point, row in source_candidates:
            canonical_id = _retention_canonical_id(point)
            canonical = (
                validated_sources.get(str(canonical_id))
                if canonical_id is not None and validation_available
                else None
            )
            if _retention_marker_state(point, canonical) is not True:
                rows.append(row)
        if validation_available:
            rows.extend(
                row
                for row, bindings in graph_candidates
                if _graph_sources_current(bindings, validated_sources, user_id=user_id)
            )
        return SELECTOR.select_rows(rows, limit)
    except Exception:
        _note(errors, "search")
        return []


def recall(query: str, *, scopes: Sequence[str] | None = None, limit: int = 5,
           timeout: float = DEFAULT_TIMEOUT_S,
           include_graph: bool | None = None) -> list[dict[str, Any]]:
    """Compatibility wrapper used by the Claude hook's shared fast path."""
    return search(
        query,
        scopes=scopes,
        allowed_scopes=scopes,
        limit=limit,
        timeout_s=timeout,
        include_graph=include_graph,
    )


def search_agents(
    query: str,
    agents: Iterable[str],
    limit: int = 5,
    min_score: float = DEFAULT_MIN_SCORE,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    *,
    user_id: str | None = DEFAULT_USER_ID,
    errors: list[str] | None = None,
) -> list[dict[str, Any]]:
    """One embed, one search per agent, concatenated in ``agents`` order.

    Equivalent to calling :func:`search` once per agent, minus the repeated
    embedding round trip. The embedding is deterministic for a fixed query, so
    the result set is the same.
    """
    try:
        agent_list = [agent for agent in agents if agent]
        if not agent_list:
            _note(errors, "no-agents")
            return []
        budget = _Budget(timeout_s)
        vector = embed(query, budget.leg() or MIN_LEG_TIMEOUT_S, errors=errors)
        if vector is None:
            return []
        rows: list[dict[str, Any]] = []
        for agent in agent_list:
            if budget.remaining() <= 0:
                _note(errors, "search")
                break
            rows.extend(
                search(
                    query,
                    limit=limit,
                    min_score=min_score,
                    user_id=user_id,
                    agent_id=agent,
                    vector=vector,
                    budget=budget,
                    errors=errors,
                )
            )
        return rows
    except Exception:
        _note(errors, "search")
        return []


def _main(argv: list[str]) -> int:
    """Tiny debug CLI: mem0_recall_fast.py "<query>" [agent ...]"""
    if len(argv) < 2:
        print('usage: mem0_recall_fast.py "<query>" [agent ...]')
        return 2
    query = argv[1]
    agents = argv[2:] or [None]  # type: ignore[list-item]
    started = time.monotonic()
    rows: list[dict[str, Any]] = []
    if agents == [None]:
        rows = search(query, limit=5, min_score=0.45)
    else:
        rows = search_agents(query, agents, limit=5, min_score=0.45)
    elapsed_ms = int((time.monotonic() - started) * 1000)
    print(f"rows={len(rows)} elapsed_ms={elapsed_ms}")
    for row in rows:
        print(f"  {row['score']:.3f} [{row['agent']}] {row['text'][:100]}")
    return 0


if __name__ == "__main__":
    import sys

    raise SystemExit(_main(sys.argv))
