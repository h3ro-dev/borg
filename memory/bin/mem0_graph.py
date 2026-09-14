"""Scope-safe Graphiti adapter used by the mem0 MCP door.

The adapter is deliberately lazy: importing this module never connects to
FalkorDB, imports Graphiti, or calls a model.  The MCP server can therefore be
tested with a small in-memory adapter and can fail open to ordinary mem0
recall when the optional graph dependency is unavailable.

GF-02 owns the durable scope registry in the graphiti tree.  This module
mirrors its key contract: ``memscope_`` followed by the first 24 hexadecimal
characters of SHA-256(scope).  It reads the registry but never writes it.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import importlib.machinery
import json
import logging
import os
import threading
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

CONFIG = importlib.machinery.SourceFileLoader(
    "borg_config_mem0_graph", str(Path(__file__).resolve().parent / "borg_config.py")
).load_module().CONFIG

LOGGER = logging.getLogger(__name__)
GRAPH_SCOPE_PREFIX = "memscope_"
GRAPH_SCOPE_DIGEST_LENGTH = 24
LEGACY_GRAPH_KEY = str(CONFIG.values["BORG_FALKORDB_GRAPH"])
DEFAULT_SCOPE_GRAPH_FILE = CONFIG.graph_data_root / "scope-graphs.json"
GRAPH_SEARCH_TIMEOUT_SECONDS = 5.0
GRAPH_EMBEDDING_REQUEST_TIMEOUT_SECONDS = 4.0
GRAPH_PROVENANCE_TIMEOUT_SECONDS = 1.0
GRAPH_PROVENANCE_MAX_EPISODES = 25
GRAPH_PROVENANCE_MAX_SOURCE_POINTS = 100
GRAPH_PROVENANCE_SCHEMA = "graph-source-provenance-v1"
GRAPH_EVIDENCE_STATE = "derived_unverified"
GRAPH_MARKER_SOURCES = frozenset({"seed", "dream-v2-canary"})
RETIRED_SOURCE_STATUSES = frozenset({"tombstoned", "decayed"})
DEFAULT_GRAPH_FEED_STATE_FILE = CONFIG.graph_data_root / "backfill-state.json"
DEFAULT_QDRANT_URL = str(CONFIG.values["BORG_QDRANT_URL"])
DEFAULT_QDRANT_COLLECTION = str(CONFIG.values["BORG_QDRANT_COLLECTION"])
DEFAULT_EMBED_OLLAMA_URL = str(CONFIG.values["BORG_OLLAMA_URL"]) if CONFIG.portable else os.environ.get(
    "MEM0_EMBED_OLLAMA_URL", str(CONFIG.values["BORG_OLLAMA_URL"])
).rstrip("/")


class GraphScopeError(RuntimeError):
    """The scope registry cannot safely authorize a graph query."""


class GraphUnavailable(RuntimeError):
    """The optional Graphiti/Falkor dependency or service is unavailable."""


def _log_graph_phase_failure(phase: str, started: float) -> None:
    """Emit bounded timing metadata without query, scope, or evidence content."""

    LOGGER.warning(
        "graph_phase_failure phase=%s elapsed_ms=%d",
        phase,
        max(0, int((time.monotonic() - started) * 1000)),
    )


def scope_graph_key(scope: str) -> str:
    """Return the Graphiti-safe deterministic key for one exact scope string."""

    if not isinstance(scope, str) or not scope:
        raise ValueError("scope must be a non-empty string")
    digest = hashlib.sha256(scope.encode("utf-8")).hexdigest()
    return GRAPH_SCOPE_PREFIX + digest[:GRAPH_SCOPE_DIGEST_LENGTH]


# Names used by adjacent lanes and tests.  Keep both aliases pointed at the
# same implementation so the contract cannot drift by spelling.
graph_key_for_scope = scope_graph_key
scope_to_graph_key = scope_graph_key


def _mapping_value(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        for name in ("graph_key", "group_id", "key", "graph"):
            candidate = value.get(name)
            if isinstance(candidate, str):
                return candidate
    return None


def _scope_mapping(document: Any) -> dict[str, str]:
    """Extract the small set of registry shapes used by the graph lane.

    The canonical shape is ``{"scopes": {scope: graph_key}}``.  The alternate
    names are accepted for a safe read during the GF-02 handoff; all extracted
    values still go through deterministic and collision checks below.
    """

    if not isinstance(document, Mapping):
        raise GraphScopeError("scope graph mapping must be a JSON object")

    for name in ("scopes", "scope_to_key", "scope_to_graph", "scope_graphs"):
        candidate = document.get(name)
        if isinstance(candidate, Mapping):
            return {
                str(scope): key
                for scope, value in candidate.items()
                if (key := _mapping_value(value)) is not None
            }

    graphs = document.get("graphs")
    if isinstance(graphs, Mapping):
        reverse: dict[str, str] = {}
        for graph_key, value in graphs.items():
            if isinstance(value, Mapping):
                scope = value.get("scope")
                if isinstance(scope, str) and isinstance(graph_key, str):
                    reverse[scope] = graph_key
        if reverse:
            return reverse

    # A direct scope -> key object is useful for the first isolated canary and
    # is unambiguous once non-scope metadata keys are ignored.
    direct = {
        str(scope): key
        for scope, value in document.items()
        if isinstance(scope, str) and (key := _mapping_value(value)) is not None
    }
    if direct:
        return direct
    raise GraphScopeError("scope graph mapping has no scope entries")


class ScopeGraphRegistry:
    """Validated read-only mapping between mem0 scopes and Falkor graph keys."""

    def __init__(self, scope_to_key: Mapping[str, str], source: str = "in-memory"):
        self.source = source
        self.scope_to_key: dict[str, str] = {}
        self.key_to_scope: dict[str, str] = {}
        for scope, graph_key in scope_to_key.items():
            if not isinstance(scope, str) or not scope:
                raise GraphScopeError("scope graph mapping contains an invalid scope")
            if not isinstance(graph_key, str) or not graph_key:
                raise GraphScopeError(f"missing graph key for scope {scope!r}")
            expected = scope_graph_key(scope)
            if graph_key != expected:
                raise GraphScopeError(
                    f"graph key mismatch for scope {scope!r}: expected deterministic key"
                )
            previous = self.key_to_scope.get(graph_key)
            if previous is not None and previous != scope:
                raise GraphScopeError(f"graph key collision for {graph_key!r}")
            self.scope_to_key[scope] = graph_key
            self.key_to_scope[graph_key] = scope

    @classmethod
    def from_document(cls, document: Any, source: str = "in-memory") -> "ScopeGraphRegistry":
        return cls(_scope_mapping(document), source=source)

    @classmethod
    def from_mapping(
        cls, scope_to_key: Mapping[str, str], source: str = "in-memory"
    ) -> "ScopeGraphRegistry":
        return cls(scope_to_key, source=source)

    @classmethod
    def load(cls, path: Path = DEFAULT_SCOPE_GRAPH_FILE) -> "ScopeGraphRegistry":
        if not path.exists():
            raise GraphScopeError(f"scope graph mapping missing: {path}")
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise GraphScopeError("scope graph mapping cannot be read") from exc
        return cls.from_document(document, source=str(path))

    def graph_key(self, scope: str) -> str:
        try:
            return self.scope_to_key[scope]
        except KeyError as exc:
            raise GraphScopeError(f"scope has no registered graph key: {scope!r}") from exc

    def scope(self, graph_key: str) -> str | None:
        return self.key_to_scope.get(graph_key)

    def registered_graph_keys(self) -> list[str]:
        return sorted(self.key_to_scope)

    def keys_for(self, scopes: Iterable[str], full_access: bool = False) -> list[str]:
        if full_access:
            return self.registered_graph_keys()
        concrete = sorted({scope for scope in scopes if isinstance(scope, str) and scope})
        if not concrete:
            raise GraphScopeError("principal has no concrete graph scopes")
        return sorted({self.graph_key(scope) for scope in concrete})


def _value(row: Any, *names: str) -> Any:
    if isinstance(row, Mapping):
        for name in names:
            if name in row:
                return row[name]
        metadata = row.get("metadata")
        if isinstance(metadata, Mapping):
            for name in names:
                if name in metadata:
                    return metadata[name]
        return None
    for name in names:
        try:
            value = getattr(row, name)
        except AttributeError:
            continue
        if value is not None:
            return value
    return None


def row_graph_key(row: Any) -> str | None:
    value = _value(row, "graph_key", "group_id", "source_graph_key", "graph")
    return value if isinstance(value, str) and value else None


def row_scope(row: Any) -> str | None:
    value = _value(row, "scope")
    return value if isinstance(value, str) and value else None


def _string_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _episode_ids(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, Iterable) and not isinstance(value, (bytes, Mapping)):
        return [str(item) for item in value if item is not None and str(item)]
    return [str(value)]


def _parse_time(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _safe_identifier(value: Any, *, maximum: int = 256) -> str | None:
    candidate = _string_value(value).strip()
    if not candidate or len(candidate) > maximum:
        return None
    if any(ord(character) < 32 for character in candidate):
        return None
    return candidate


def _safe_digest(value: Any) -> str | None:
    candidate = _string_value(value).strip().lower()
    if len(candidate) != 64 or any(character not in "0123456789abcdef" for character in candidate):
        return None
    return candidate


def _payload_digest(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _is_marker_episode(*values: Any) -> bool:
    for value in values:
        candidate = _string_value(value).strip().lower()
        if candidate in GRAPH_MARKER_SOURCES:
            return True
        if any(
            candidate.startswith(marker + separator)
            for marker in GRAPH_MARKER_SOURCES
            for separator in (":", "-", "/", "_")
        ):
            return True
    return False


def _time_bounds(values: Iterable[Any]) -> tuple[str | None, str | None]:
    parsed: list[tuple[datetime, str]] = []
    for value in values:
        text = _string_value(value).strip()
        moment = _parse_time(text)
        if moment is not None:
            parsed.append((moment, text))
    if not parsed:
        return None, None
    parsed.sort(key=lambda item: item[0])
    return parsed[0][1], parsed[-1][1]


def _latest_time(values: Iterable[Any]) -> str | None:
    return _time_bounds(values)[1]


def _fallback_source_provenance(
    *, graph_key: str, scope: str | None, episodes: Iterable[Any], valid_at: Any
) -> dict[str, Any]:
    episode_uuids = [
        identifier
        for value in episodes
        if (identifier := _safe_identifier(value)) is not None
    ]
    event_time = _string_value(valid_at).strip() or None
    return {
        "schema": GRAPH_PROVENANCE_SCHEMA,
        "evidence_state": GRAPH_EVIDENCE_STATE,
        "binding_type": "episode_group",
        "binding_state": "unbound",
        "source_group_status": "unbound",
        "graph_key": graph_key,
        "scope": scope,
        "episode_uuids": episode_uuids[:GRAPH_PROVENANCE_MAX_EPISODES],
        "episode_uuids_truncated": len(episode_uuids) > GRAPH_PROVENANCE_MAX_EPISODES,
        "source_groups": [],
        "event_time_min": event_time,
        "event_time_max": event_time,
        "ingestion_watermark": None,
        "ingestion_watermark_state": "unknown",
    }


def _safe_source_provenance(
    value: Any,
    *,
    graph_key: str,
    scope: str | None,
    episodes: list[str],
    valid_at: str,
) -> dict[str, Any]:
    """Accept only bounded adapter metadata tied to the authorized graph row."""

    fallback = _fallback_source_provenance(
        graph_key=graph_key, scope=scope, episodes=episodes, valid_at=valid_at
    )
    if not isinstance(value, Mapping):
        return fallback
    if value.get("schema") != GRAPH_PROVENANCE_SCHEMA:
        return fallback
    if value.get("evidence_state") != GRAPH_EVIDENCE_STATE:
        return fallback
    if value.get("graph_key") != graph_key or value.get("scope") != scope:
        return fallback

    allowed_episode_ids = set(episodes)
    groups: list[dict[str, Any]] = []
    source_point_count = 0
    raw_groups = value.get("source_groups")
    if isinstance(raw_groups, Iterable) and not isinstance(raw_groups, (str, bytes, Mapping)):
        for raw_group in raw_groups:
            if len(groups) >= GRAPH_PROVENANCE_MAX_EPISODES or not isinstance(raw_group, Mapping):
                break
            episode_uuid = _safe_identifier(raw_group.get("episode_uuid"))
            if episode_uuid is None or episode_uuid not in allowed_episode_ids:
                continue
            group: dict[str, Any] = {"episode_uuid": episode_uuid}
            for name, maximum in (("run_id", 256), ("source_category", 80)):
                candidate = _safe_identifier(raw_group.get(name), maximum=maximum)
                if candidate is not None:
                    group[name] = candidate
            for name in ("event_time", "graph_created_at", "ingestion_watermark"):
                candidate = _string_value(raw_group.get(name)).strip()
                if candidate and _parse_time(candidate) is not None:
                    group[name] = candidate
            group["marker"] = raw_group.get("marker") is True
            binding_state = raw_group.get("binding_state")
            if binding_state in {"bound", "partial", "unbound"}:
                group["binding_state"] = binding_state
            group_status = raw_group.get("source_group_status")
            if group_status in {"all_active", "mixed", "all_retired", "unbound"}:
                group["source_group_status"] = group_status

            source_points: list[dict[str, Any]] = []
            raw_points = raw_group.get("source_points")
            if isinstance(raw_points, Iterable) and not isinstance(
                raw_points, (str, bytes, Mapping)
            ):
                for raw_point in raw_points:
                    if source_point_count >= GRAPH_PROVENANCE_MAX_SOURCE_POINTS:
                        break
                    if not isinstance(raw_point, Mapping):
                        continue
                    point_id = _safe_identifier(raw_point.get("point_id"))
                    digest = _safe_digest(raw_point.get("payload_digest"))
                    status = raw_point.get("status")
                    digest_state = raw_point.get("digest_state")
                    if point_id is None or digest is None:
                        continue
                    if status not in {"active", "retired", "stale", "missing"}:
                        continue
                    if digest_state not in {"matched", "changed", "missing"}:
                        continue
                    point = {
                        "point_id": point_id,
                        "payload_digest": digest,
                        "status": status,
                        "digest_state": digest_state,
                    }
                    observed = _safe_digest(raw_point.get("observed_payload_digest"))
                    if observed is not None and observed != digest:
                        point["observed_payload_digest"] = observed
                    source_points.append(point)
                    source_point_count += 1
            group["source_points"] = source_points
            groups.append(group)

    safe = dict(fallback)
    safe["source_groups"] = groups
    for name, allowed in (
        ("binding_state", {"bound", "partial", "unbound"}),
        ("source_group_status", {"all_active", "mixed", "all_retired", "unbound"}),
        ("ingestion_watermark_state", {"observed", "partial", "unknown"}),
    ):
        candidate = value.get(name)
        if candidate in allowed:
            safe[name] = candidate
    for name in (
        "event_time_min",
        "event_time_max",
        "graph_created_at_min",
        "graph_created_at_max",
        "ingestion_watermark",
        "last_complete_scan",
    ):
        candidate = _string_value(value.get(name)).strip()
        if candidate and _parse_time(candidate) is not None:
            safe[name] = candidate
    candidate_ids = [
        identifier
        for item in _episode_ids(value.get("episode_uuids"))
        if (identifier := _safe_identifier(item)) is not None and identifier in allowed_episode_ids
    ]
    safe["episode_uuids"] = candidate_ids[:GRAPH_PROVENANCE_MAX_EPISODES]
    safe["episode_uuids_truncated"] = bool(value.get("episode_uuids_truncated")) or (
        len(candidate_ids) > GRAPH_PROVENANCE_MAX_EPISODES
    )
    return safe


def is_current_fact(valid_at: str, invalid_at: str, now: datetime | None = None) -> bool:
    """Return whether a fact is safe for automatic current-context recall."""

    if invalid_at:
        return False
    parsed = _parse_time(valid_at)
    if parsed is None:
        # A missing validity start is how older active edges are represented;
        # an explicit but unparseable value is treated as unknown and closed.
        return not valid_at
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return parsed <= current.astimezone(timezone.utc)


def graph_edge_dict(edge: Any, default_graph_key: str | None = None) -> dict[str, Any]:
    """Project a Graphiti edge/object into a serializable adapter row."""

    metadata = _value(edge, "metadata")
    graph_key = row_graph_key(edge) or default_graph_key
    return {
        "id": _value(edge, "id", "uuid", "edge_uuid"),
        "fact": _value(edge, "fact", "memory", "text"),
        "score": _value(edge, "score", "distance"),
        "graph_key": graph_key,
        "scope": _value(edge, "scope")
        or (metadata.get("scope") if isinstance(metadata, Mapping) else None),
        "episodes": _value(edge, "episodes", "episode_uuids", "source_episode_uuids"),
        "valid_at": _string_value(_value(edge, "valid_at")),
        "invalid_at": _string_value(_value(edge, "invalid_at")),
        "entity": _value(edge, "entity"),
        "relation": _value(edge, "relation"),
        "other": _value(edge, "other"),
        "source_provenance": _value(edge, "source_provenance"),
    }


def _dedupe_graph_rows(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop only repeated native edge identities within the same graph.

    Falkor can return both orientations of one edge for an undirected match.
    The graph key remains part of the identity so equal edge UUIDs in distinct
    authorized graphs remain separate historical evidence.
    """

    output: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for row in rows:
        graph_key = row_graph_key(row)
        edge_id = _safe_identifier(_value(row, "id", "uuid", "edge_uuid"))
        if graph_key is not None and edge_id is not None:
            identity = (graph_key, edge_id)
            if identity in seen:
                continue
            seen.add(identity)
        output.append(row)
    return output


def normalize_graph_row(
    row: Any,
    *,
    graph_key: str,
    scope: str | None,
    current_only: bool = False,
) -> dict[str, Any] | None:
    """Create the existing mem0 row shape with graph provenance attached."""

    projected = graph_edge_dict(row, default_graph_key=graph_key)
    fact = _string_value(projected.get("fact")).strip()
    if not fact or not graph_key:
        return None
    valid_at = projected["valid_at"]
    invalid_at = projected["invalid_at"]
    if current_only and not is_current_fact(valid_at, invalid_at):
        return None

    identifier = _string_value(projected.get("id")).strip()
    if not identifier:
        identifier = "graphiti:" + hashlib.sha256(
            f"{graph_key}\0{scope or ''}\0{fact}\0{valid_at}\0{invalid_at}".encode("utf-8")
        ).hexdigest()[:24]
    episodes = _episode_ids(projected.get("episodes"))
    source_provenance = _safe_source_provenance(
        projected.get("source_provenance"),
        graph_key=graph_key,
        scope=scope,
        episodes=episodes,
        valid_at=valid_at,
    )
    provenance = {
        "graph_key": graph_key,
        "edge_id": identifier,
        "episode_uuids": episodes,
        **source_provenance,
    }
    metadata = {
        "source": "graphiti",
        "graph_key": graph_key,
        "scope": scope,
        "valid_at": valid_at,
        "invalid_at": invalid_at,
        "episode_uuids": episodes,
        "provenance": provenance,
    }
    for name in (
        "evidence_state",
        "binding_type",
        "binding_state",
        "source_group_status",
        "event_time_min",
        "event_time_max",
        "graph_created_at_min",
        "graph_created_at_max",
        "ingestion_watermark",
        "ingestion_watermark_state",
        "last_complete_scan",
    ):
        if name in source_provenance:
            metadata[name] = source_provenance[name]
    score = projected.get("score")
    if not isinstance(score, (int, float)) or isinstance(score, bool):
        score = None
    output = {
        "memory": fact,
        "score": score,
        "id": identifier,
        "agent_id": "graphiti",
        "scope": scope,
        "metadata": metadata,
    }
    for name in ("entity", "relation", "other"):
        if projected.get(name) is not None:
            output[name] = projected[name]
    return output


class GraphitiAdapter:
    """Lazy production adapter; methods are async so Graphiti stays native.

    Embeddings use the dedicated ``MEM0_EMBED_OLLAMA_URL`` route and its
    OpenAI-compatible ``/v1`` API. Extraction and reranking remain on the
    separate LLM route.
    """

    def __init__(
        self,
        registry: ScopeGraphRegistry | None = None,
        *,
        graph_host: str = str(CONFIG.values["BORG_FALKORDB_HOST"]),
        graph_port: int = int(CONFIG.values["BORG_FALKORDB_PORT"]),
        embed_ollama_url: str = DEFAULT_EMBED_OLLAMA_URL,
        llm_url: str = str(CONFIG.values["BORG_GRAPH_LLM_URL"]),
        llm_model: str = str(CONFIG.values["BORG_GRAPH_MODEL"]),
        feed_state_path: Path = DEFAULT_GRAPH_FEED_STATE_FILE,
        qdrant_url: str = DEFAULT_QDRANT_URL,
        qdrant_collection: str = DEFAULT_QDRANT_COLLECTION,
        source_point_fetcher: Any = None,
    ):
        self._registry_is_explicit = registry is not None
        if registry is not None:
            self.registry = registry
        elif DEFAULT_SCOPE_GRAPH_FILE.exists():
            self.registry = ScopeGraphRegistry.load()
        else:
            # Restricted principals still fail closed in keys_for(); an empty
            # registry never guesses a legacy graph.
            self.registry = ScopeGraphRegistry({})
        self.graph_host = graph_host
        self.graph_port = graph_port
        self.embed_ollama_url = embed_ollama_url.rstrip("/")
        self.llm_url = llm_url.rstrip("/")
        if not self.llm_url.endswith("/v1"):
            self.llm_url += "/v1"
        self.llm_model = llm_model
        self.feed_state_path = Path(feed_state_path)
        self.qdrant_url = qdrant_url.rstrip("/")
        self.qdrant_collection = qdrant_collection
        self._source_point_fetcher = source_point_fetcher
        self._graphiti = None
        self._graphiti_init_lock = threading.Lock()
        self._falkor: dict[str, Any] = {}
        self._checkpoint_cache_version: tuple[int, int, int, int] | None = None
        self._checkpoint_cache: dict[str, Any] | None = None
        self._checkpoint_cache_lock = threading.Lock()

    @property
    def registry(self) -> ScopeGraphRegistry:
        # A long-lived door must observe later feed registration without restart.
        if CONFIG.portable and not self._registry_is_explicit:
            return ScopeGraphRegistry.load()
        return self._registry

    @registry.setter
    def registry(self, value: ScopeGraphRegistry) -> None:
        self._registry = value

    def graph_keys(self, scopes: Iterable[str], full_access: bool = False) -> list[str]:
        return self.registry.keys_for(scopes, full_access=full_access)

    def _graphiti_client(self) -> Any:
        if self._graphiti is not None:
            return self._graphiti
        with self._graphiti_init_lock:
            if self._graphiti is not None:
                return self._graphiti
            try:
                from graphiti_core import Graphiti
                from graphiti_core.cross_encoder.openai_reranker_client import (
                    OpenAIRerankerClient,
                )
                from graphiti_core.driver.falkordb_driver import FalkorDriver
                from graphiti_core.embedder.openai import OpenAIEmbedder, OpenAIEmbedderConfig
                from graphiti_core.llm_client import LLMConfig
                from graphiti_core.llm_client.openai_generic_client import OpenAIGenericClient
                from openai import AsyncOpenAI
            except ImportError as exc:
                raise GraphUnavailable("Graphiti dependency unavailable") from exc

            class ReadOnlyFalkorDriver(FalkorDriver):
                async def build_indices_and_constraints(self, delete_existing: bool = False):
                    return None

                def clone(self, database: str):
                    if database == self._database:
                        return self
                    cloned = copy.copy(self)
                    cloned._database = (
                        "default_db" if database == self.default_group_id else database
                    )
                    return cloned

            # Keep the retained local LLM arrangement; embeddings are isolated below.
            llm = OpenAIGenericClient(
                config=LLMConfig(
                    api_key="ollama",
                    model=self.llm_model,
                    small_model=self.llm_model,
                    base_url=self.llm_url,
                    temperature=0.0,
                )
            )
            embedding_url = f"{self.embed_ollama_url}/v1"
            embedding_client = AsyncOpenAI(
                api_key="ollama",
                base_url=embedding_url,
                timeout=GRAPH_EMBEDDING_REQUEST_TIMEOUT_SECONDS,
                max_retries=0,
            )
            embedder = OpenAIEmbedder(
                config=OpenAIEmbedderConfig(
                    embedding_model=str(CONFIG.values["BORG_EMBED_MODEL"]),
                    embedding_dim=int(CONFIG.values["BORG_EMBED_DIMS"]),
                    api_key="ollama",
                    base_url=embedding_url,
                ),
                client=embedding_client,
            )
            reranker = OpenAIRerankerClient(
                config=LLMConfig(
                    api_key="ollama",
                    model=self.llm_model,
                    base_url=self.llm_url,
                    temperature=0.0,
                ),
                client=llm.client,
            )
            self._graphiti = Graphiti(
                graph_driver=ReadOnlyFalkorDriver(host=self.graph_host, port=self.graph_port),
                llm_client=llm,
                embedder=embedder,
                cross_encoder=reranker,
            )
            return self._graphiti

    def _falkor_graph(self, graph_key: str) -> Any:
        graph = self._falkor.get(graph_key)
        if graph is not None:
            return graph
        try:
            from falkordb import FalkorDB
        except ImportError as exc:
            raise GraphUnavailable("FalkorDB dependency unavailable") from exc
        graph = FalkorDB(
            host=self.graph_host,
            port=self.graph_port,
            socket_timeout=GRAPH_SEARCH_TIMEOUT_SECONDS,
            socket_connect_timeout=GRAPH_SEARCH_TIMEOUT_SECONDS,
        ).select_graph(graph_key)
        self._falkor[graph_key] = graph
        return graph

    @staticmethod
    def _read_query(graph: Any, query: str, params: dict | None = None) -> Any:
        try:
            return graph.ro_query(query, params or {})
        except Exception as exc:
            if type(exc).__name__ == "ResponseError" and str(exc) == "Invalid graph operation on empty key":
                from types import SimpleNamespace
                return SimpleNamespace(result_set=[], empty_graph=True)
            raise

    async def close(self) -> None:
        if self._graphiti is not None:
            await self._graphiti.close()
            await self._graphiti.llm_client.client.close()
            await self._graphiti.embedder.client.close()
        for graph in self._falkor.values():
            graph.client.close()
        self._falkor.clear()

    async def recent_episodes(self, graph_keys: list[str], limit: int = 10) -> list[dict[str, Any]]:
        rows = []
        for key in graph_keys:
            graph = await asyncio.to_thread(self._falkor_graph, key)
            result = await asyncio.to_thread(self._read_query, graph,
                "MATCH (e:Episodic) RETURN e.name, e.valid_at, e.source_description "
                "ORDER BY e.valid_at DESC LIMIT $k", {"k": max(1, min(int(limit), 25))})
            rows.extend({"graph_key": key, "name": a, "at": _string_value(b), "source": c}
                        for a, b, c in result.result_set)
        return sorted(rows, key=lambda row: row["at"], reverse=True)[:limit]

    def _checkpoint_version(self) -> tuple[int, int, int, int]:
        stat = self.feed_state_path.stat()
        return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)

    def _checkpoint_snapshot(self) -> dict[str, Any]:
        """Return a metadata-only reverse feed map cached by exact file version."""

        version_before = self._checkpoint_version()
        with self._checkpoint_cache_lock:
            if (
                self._checkpoint_cache is not None
                and self._checkpoint_cache_version == version_before
            ):
                return self._checkpoint_cache
            document = json.loads(self.feed_state_path.read_text(encoding="utf-8"))
            if self._checkpoint_version() != version_before:
                raise OSError("graph feed checkpoint changed while being read")
            processed = document.get("graph_processed") if isinstance(document, Mapping) else None
            if not isinstance(processed, Mapping):
                raise ValueError("graph feed checkpoint has no graph_processed mapping")

            by_episode_run: defaultdict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
            watermarks: defaultdict[tuple[str, str], list[str]] = defaultdict(list)
            for raw_point_id, raw_entry in processed.items():
                if not isinstance(raw_entry, Mapping):
                    continue
                point_id = _safe_identifier(raw_point_id)
                episode_id = _safe_identifier(raw_entry.get("episode_id"))
                run_id = _safe_identifier(raw_entry.get("run_id"))
                graph_key = _safe_identifier(raw_entry.get("group_id"))
                scope = _safe_identifier(raw_entry.get("scope"))
                digest = _safe_digest(
                    raw_entry.get("payload_digest") or raw_entry.get("digest")
                )
                processed_at = _string_value(raw_entry.get("processed_at_mdt")).strip()
                if not all((point_id, episode_id, run_id, graph_key, scope, digest)):
                    continue
                entry = {
                    "point_id": point_id,
                    "episode_id": episode_id,
                    "run_id": run_id,
                    "group_id": graph_key,
                    "scope": scope,
                    "payload_digest": digest,
                    "processed_at_mdt": processed_at,
                }
                by_episode_run[(episode_id, run_id)].append(entry)
                if processed_at and not _is_marker_episode(run_id):
                    watermarks[(scope, graph_key)].append(processed_at)

            for entries in by_episode_run.values():
                entries.sort(key=lambda entry: entry["point_id"])
            scoped_watermarks = {
                key: _latest_time(values) for key, values in watermarks.items()
            }
            snapshot: dict[str, Any] = {
                "by_episode_run": {
                    key: tuple(entries) for key, entries in by_episode_run.items()
                },
                "watermarks": scoped_watermarks,
            }
            last_complete = _string_value(document.get("last_complete_scan_mdt")).strip()
            if last_complete and _parse_time(last_complete) is not None:
                snapshot["last_complete_scan"] = last_complete
            self._checkpoint_cache_version = version_before
            self._checkpoint_cache = snapshot
            return snapshot

    def _fetch_source_points(
        self, point_ids: list[str], *, timeout_seconds: float
    ) -> dict[str, Mapping[str, Any]]:
        if not point_ids:
            return {}
        if self._source_point_fetcher is not None:
            raw = self._source_point_fetcher(list(point_ids), timeout_seconds)
        else:
            collection = urllib.parse.quote(self.qdrant_collection, safe="")
            request = urllib.request.Request(
                f"{self.qdrant_url}/collections/{collection}/points",
                data=json.dumps(
                    {
                        "ids": list(point_ids),
                        "with_payload": True,
                        "with_vector": False,
                    },
                    separators=(",", ":"),
                ).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=max(0.01, timeout_seconds)) as response:
                document = json.load(response)
            raw = document.get("result") if isinstance(document, Mapping) else None

        if isinstance(raw, Mapping):
            raw = [
                {"id": point_id, "payload": payload}
                for point_id, payload in raw.items()
            ]
        if not isinstance(raw, Iterable) or isinstance(raw, (str, bytes, Mapping)):
            raise ValueError("source point read returned an invalid result")
        requested = set(point_ids)
        points: dict[str, Mapping[str, Any]] = {}
        for item in raw:
            if isinstance(item, Mapping):
                point_id = _safe_identifier(item.get("id"))
                payload = item.get("payload")
            else:
                point_id = _safe_identifier(getattr(item, "id", None))
                payload = getattr(item, "payload", None)
            if point_id in requested and isinstance(payload, Mapping):
                points[point_id] = payload
        return points

    def _episode_nodes(
        self, graph_key: str, episode_uuids: list[str]
    ) -> dict[str, dict[str, Any]]:
        if not episode_uuids:
            return {}
        graph = self._falkor_graph(graph_key)
        result = self._read_query(graph,
            "MATCH (e:Episodic) WHERE e.uuid IN $episode_uuids "
            "RETURN e.uuid, e.name, e.source_description, e.valid_at, e.created_at, e.group_id",
            {"episode_uuids": list(episode_uuids)},
        )
        requested = set(episode_uuids)
        nodes: dict[str, dict[str, Any]] = {}
        for raw_values in getattr(result, "result_set", []) or []:
            values = list(raw_values)
            values.extend([None] * max(0, 6 - len(values)))
            episode_uuid = _safe_identifier(values[0])
            run_id = _safe_identifier(values[1])
            node_group = _safe_identifier(values[5])
            if episode_uuid not in requested or run_id is None:
                continue
            if node_group is not None and node_group != graph_key:
                continue
            nodes[episode_uuid] = {
                "episode_uuid": episode_uuid,
                "run_id": run_id,
                "source_category": _safe_identifier(values[2], maximum=80),
                "event_time": _string_value(values[3]).strip() or None,
                "graph_created_at": _string_value(values[4]).strip() or None,
            }
        return nodes

    def _fallback_rows(
        self, rows: list[dict[str, Any]], allowed_graph_keys: Iterable[str]
    ) -> list[dict[str, Any]]:
        allowed = set(allowed_graph_keys)
        output: list[dict[str, Any]] = []
        for raw in rows:
            row = dict(raw)
            graph_key = row_graph_key(row) or ""
            scope = self.registry.scope(graph_key) if graph_key in allowed else None
            row["source_provenance"] = _fallback_source_provenance(
                graph_key=graph_key,
                scope=scope,
                episodes=_episode_ids(row.get("episodes")),
                valid_at=row.get("valid_at"),
            )
            output.append(row)
        return output

    def _enrich_rows_sync(
        self,
        rows: list[dict[str, Any]],
        allowed_graph_keys: list[str],
        deadline: float,
    ) -> list[dict[str, Any]]:
        allowed = set(allowed_graph_keys)
        snapshot = self._checkpoint_snapshot()
        requested_by_row: list[list[tuple[str, str]]] = []
        requested_order: list[tuple[str, str]] = []
        requested_seen: set[tuple[str, str]] = set()
        eligible_rows: list[bool] = []

        for row in rows:
            graph_key = row_graph_key(row) or ""
            scope = self.registry.scope(graph_key) if graph_key in allowed else None
            claimed_scope = row_scope(row)
            eligible = (
                graph_key in allowed
                and graph_key != LEGACY_GRAPH_KEY
                and scope is not None
                and (claimed_scope is None or claimed_scope == scope)
            )
            eligible_rows.append(eligible)
            row_requests: list[tuple[str, str]] = []
            if eligible:
                for episode_uuid in _episode_ids(row.get("episodes")):
                    safe_uuid = _safe_identifier(episode_uuid)
                    if safe_uuid is None:
                        continue
                    key = (graph_key, safe_uuid)
                    row_requests.append(key)
                    if key not in requested_seen:
                        requested_seen.add(key)
                        requested_order.append(key)
            requested_by_row.append(row_requests)

        selected = requested_order[:GRAPH_PROVENANCE_MAX_EPISODES]
        selected_set = set(selected)
        selected_by_graph: defaultdict[str, list[str]] = defaultdict(list)
        for graph_key, episode_uuid in selected:
            selected_by_graph[graph_key].append(episode_uuid)

        native_nodes: dict[tuple[str, str], dict[str, Any]] = {}
        for graph_key, episode_uuids in selected_by_graph.items():
            if time.monotonic() >= deadline:
                raise TimeoutError("graph provenance deadline")
            try:
                found = self._episode_nodes(graph_key, episode_uuids)
            except Exception:
                continue
            for episode_uuid, node in found.items():
                native_nodes[(graph_key, episode_uuid)] = node

        group_work: dict[tuple[str, str], dict[str, Any]] = {}
        point_ids: list[str] = []
        point_seen: set[str] = set()
        for key in selected:
            graph_key, episode_uuid = key
            node = native_nodes.get(key)
            scope = self.registry.scope(graph_key)
            if node is None or scope is None:
                continue
            marker = _is_marker_episode(node.get("run_id"), node.get("source_category"))
            entries = []
            if not marker:
                entries = [
                    entry
                    for entry in snapshot["by_episode_run"].get(
                        (episode_uuid, node["run_id"]), ()
                    )
                    if entry["scope"] == scope and entry["group_id"] == graph_key
                ]
            chosen: list[dict[str, str]] = []
            capped = False
            for entry in entries:
                point_id = entry["point_id"]
                if point_id in point_seen:
                    chosen.append(entry)
                    continue
                if len(point_ids) >= GRAPH_PROVENANCE_MAX_SOURCE_POINTS:
                    capped = True
                    continue
                point_seen.add(point_id)
                point_ids.append(point_id)
                chosen.append(entry)
            if len(chosen) < len(entries):
                capped = True
            group_work[key] = {
                "node": node,
                "scope": scope,
                "entries": chosen,
                "entry_count": len(entries),
                "marker": marker,
                "capped": capped,
            }

        if time.monotonic() >= deadline:
            raise TimeoutError("graph provenance deadline")
        remaining = max(0.01, deadline - time.monotonic())
        source_points = self._fetch_source_points(point_ids, timeout_seconds=remaining)

        source_groups: dict[tuple[str, str], dict[str, Any]] = {}
        for key, work in group_work.items():
            node = work["node"]
            group: dict[str, Any] = {
                "episode_uuid": node["episode_uuid"],
                "run_id": node["run_id"],
                "marker": work["marker"],
                "source_points": [],
            }
            for name in ("source_category", "event_time", "graph_created_at"):
                if node.get(name):
                    group[name] = node[name]
            entries = work["entries"]
            if work["marker"] or not entries:
                group["binding_state"] = "unbound"
                group["source_group_status"] = "unbound"
                source_groups[key] = group
                continue

            statuses: list[str] = []
            incomplete = bool(work["capped"])
            for entry in entries:
                point_id = entry["point_id"]
                checkpoint_digest = entry["payload_digest"]
                payload = source_points.get(point_id)
                observed_digest = _payload_digest(payload) if payload is not None else None
                digest_state = (
                    "missing"
                    if observed_digest is None
                    else "matched"
                    if observed_digest == checkpoint_digest
                    else "changed"
                )
                if payload is None:
                    status = "missing"
                    incomplete = True
                else:
                    payload_scope = _string_value(payload.get("scope") or str(CONFIG.values["BORG_MEMORY_SCOPE"]))
                    source_marker = _is_marker_episode(
                        payload.get("run_id"),
                        payload.get("kind"),
                        payload.get("agent_id"),
                    ) or payload.get("is_canary") is True or payload.get("canary") is True
                    native_status = _string_value(payload.get("status")).strip().lower()
                    if payload_scope != work["scope"] or source_marker or (CONFIG.portable and payload.get("user_id") != str(CONFIG.values["BORG_OWNER_ID"])):
                        status = "stale"
                        incomplete = True
                    elif native_status in RETIRED_SOURCE_STATUSES:
                        status = "retired"
                    elif digest_state != "matched":
                        status = "stale"
                        incomplete = True
                    else:
                        status = "active"
                point = {
                    "point_id": point_id,
                    "payload_digest": checkpoint_digest,
                    "status": status,
                    "digest_state": digest_state,
                }
                if observed_digest is not None and observed_digest != checkpoint_digest:
                    point["observed_payload_digest"] = observed_digest
                group["source_points"].append(point)
                statuses.append(status)

            if work["capped"]:
                group_status = "mixed"
            elif statuses and all(status == "active" for status in statuses):
                group_status = "all_active"
            elif statuses and all(status == "retired" for status in statuses):
                group_status = "all_retired"
            else:
                group_status = "mixed"
            group["source_group_status"] = group_status
            group["binding_state"] = "partial" if incomplete else "bound"
            watermark = _latest_time(entry["processed_at_mdt"] for entry in entries)
            if watermark is not None:
                group["ingestion_watermark"] = watermark
            source_groups[key] = group

        output: list[dict[str, Any]] = []
        for index, raw in enumerate(rows):
            row = dict(raw)
            graph_key = row_graph_key(row) or ""
            scope = self.registry.scope(graph_key) if graph_key in allowed else None
            requests = requested_by_row[index]
            groups = [source_groups[key] for key in requests if key in source_groups]
            states = [group["binding_state"] for group in groups]
            missing_request = any(key not in selected_set or key not in source_groups for key in requests)
            if not eligible_rows[index] or not groups or all(state == "unbound" for state in states):
                binding_state = "unbound"
            elif missing_request or any(state != "bound" for state in states):
                binding_state = "partial"
            else:
                binding_state = "bound"
            group_statuses = [group["source_group_status"] for group in groups]
            if not group_statuses or all(status == "unbound" for status in group_statuses):
                group_status = "unbound"
            elif (
                not missing_request
                and group_statuses
                and all(status == "all_active" for status in group_statuses)
            ):
                group_status = "all_active"
            elif (
                not missing_request
                and group_statuses
                and all(status == "all_retired" for status in group_statuses)
            ):
                group_status = "all_retired"
            else:
                group_status = "mixed"

            event_min, event_max = _time_bounds(
                [row.get("valid_at"), *(group.get("event_time") for group in groups)]
            )
            created_min, created_max = _time_bounds(
                group.get("graph_created_at") for group in groups
            )
            watermark = _latest_time(group.get("ingestion_watermark") for group in groups)
            provenance = _fallback_source_provenance(
                graph_key=graph_key,
                scope=scope,
                episodes=_episode_ids(row.get("episodes")),
                valid_at=row.get("valid_at"),
            )
            provenance.update(
                {
                    "binding_state": binding_state,
                    "source_group_status": group_status,
                    "source_groups": groups,
                    "event_time_min": event_min,
                    "event_time_max": event_max,
                    "ingestion_watermark": watermark,
                    "ingestion_watermark_state": (
                        "unknown"
                        if watermark is None
                        else "observed"
                        if binding_state == "bound"
                        else "partial"
                    ),
                    "episode_uuids_truncated": missing_request
                    or len(requests) > GRAPH_PROVENANCE_MAX_EPISODES,
                }
            )
            if created_min is not None:
                provenance["graph_created_at_min"] = created_min
                provenance["graph_created_at_max"] = created_max
            if snapshot.get("last_complete_scan"):
                provenance["last_complete_scan"] = snapshot["last_complete_scan"]
            row["source_provenance"] = provenance
            output.append(row)
        return output

    async def _enrich_rows(
        self,
        rows: list[dict[str, Any]],
        allowed_graph_keys: list[str],
        *,
        budget_seconds: float,
    ) -> list[dict[str, Any]]:
        if not rows or budget_seconds <= 0:
            return self._fallback_rows(rows, allowed_graph_keys)
        phase_started = time.monotonic()
        bounded_budget = min(GRAPH_PROVENANCE_TIMEOUT_SECONDS, budget_seconds)
        deadline = time.monotonic() + bounded_budget
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(
                    self._enrich_rows_sync,
                    rows,
                    list(allowed_graph_keys),
                    deadline,
                ),
                timeout=bounded_budget,
            )
        except Exception:
            _log_graph_phase_failure("enrichment", phase_started)
            return self._fallback_rows(rows, allowed_graph_keys)

    async def search(self, query: str, graph_keys: list[str], limit: int = 10) -> list[dict[str, Any]]:
        started = time.monotonic()
        bounded_limit = max(1, int(limit))
        phase = {"name": "init", "started": started}

        def begin_phase(name: str) -> None:
            phase["name"] = name
            phase["started"] = time.monotonic()

        async def run_search():
            if not graph_keys or not query.strip():
                return []
            selected_keys = list(graph_keys)
            if CONFIG.portable:
                # A registered scope may precede any graph write. Establish
                # emptiness with read-only native queries before invoking a model.
                selected_keys = []
                for key in graph_keys:
                    graph = await asyncio.to_thread(self._falkor_graph, key)
                    result = await asyncio.to_thread(
                        self._read_query, graph, "MATCH (n:Entity) RETURN count(n)"
                    )
                    if result.result_set and result.result_set[0][0]:
                        selected_keys.append(key)
                if not selected_keys:
                    return []
            client = await asyncio.to_thread(self._graphiti_client)
            if not hasattr(client, "clients") or not hasattr(client, "embedder"):
                # Preserve the adapter's existing small test/canary seam.
                begin_phase("native-search")
                return await client.search(
                    query, group_ids=selected_keys, num_results=bounded_limit
                )

            # Graphiti's Falkor multi-group decorator runs the whole search once
            # per graph, including one identical embedding request per graph.
            # Embed once, then run its native bounded edge search against each
            # exact authorized graph with the same vector.
            from graphiti_core.helpers import semaphore_gather
            from graphiti_core.search.search import search as graphiti_search
            from graphiti_core.search.search_config_recipes import EDGE_HYBRID_SEARCH_RRF
            from graphiti_core.search.search_filters import SearchFilters

            begin_phase("embedding")
            query_vector = await client.embedder.create(
                input_data=[query.replace("\n", " ")]
            )
            begin_phase("native-search")
            search_config = copy.deepcopy(EDGE_HYBRID_SEARCH_RRF)
            search_config.limit = bounded_limit

            async def search_graph(graph_key: str):
                driver = client.clients.driver.clone(database=graph_key)
                results = await graphiti_search(
                    client.clients,
                    query,
                    [graph_key],
                    search_config,
                    SearchFilters(),
                    query_vector=query_vector,
                    driver=driver,
                )
                return results.edges

            grouped = await semaphore_gather(
                *(search_graph(graph_key) for graph_key in selected_keys),
                max_coroutines=getattr(client, "max_coroutines", None),
            )
            return [edge for edges in grouped for edge in edges]

        try:
            results = await asyncio.wait_for(run_search(), timeout=GRAPH_SEARCH_TIMEOUT_SECONDS)
        except asyncio.CancelledError:
            # The synchronous MCP bridge has the same deadline as this reader.
            # It can cancel us just before wait_for raises its own timeout; keep
            # the last safe phase observable without logging request content.
            _log_graph_phase_failure(phase["name"], phase["started"])
            raise
        except asyncio.TimeoutError as exc:
            _log_graph_phase_failure(phase["name"], phase["started"])
            raise GraphUnavailable("Graphiti search timed out") from exc
        except Exception:
            _log_graph_phase_failure(phase["name"], phase["started"])
            raise
        rows = _dedupe_graph_rows(graph_edge_dict(edge) for edge in results)[:bounded_limit]
        remaining = GRAPH_SEARCH_TIMEOUT_SECONDS - (time.monotonic() - started) - 0.02
        begin_phase("enrichment")
        try:
            return await self._enrich_rows(
                rows, graph_keys, budget_seconds=min(GRAPH_PROVENANCE_TIMEOUT_SECONDS, remaining)
            )
        except asyncio.CancelledError:
            _log_graph_phase_failure(phase["name"], phase["started"])
            raise
        except Exception:
            _log_graph_phase_failure(phase["name"], phase["started"])
            raise

    async def entity_timeline(
        self, entity: str, graph_keys: list[str], limit: int = 20
    ) -> list[dict[str, Any]]:
        started = time.monotonic()
        bounded_limit = max(1, int(limit))
        query = (
            "MATCH (n:Entity)-[r]-(m) "
            "WHERE toLower(n.name) CONTAINS toLower($e) "
            "AND r.fact IS NOT NULL "
            "RETURN DISTINCT n.name, type(r), r.fact, r.uuid, r.valid_at, r.invalid_at, "
            "r.episodes, m.name ORDER BY r.valid_at DESC LIMIT $k"
        )
        rows: list[dict[str, Any]] = []
        for graph_key in graph_keys:
            graph = await asyncio.to_thread(self._falkor_graph, graph_key)
            result = await asyncio.to_thread(
                self._read_query, graph, query, {"e": entity, "k": bounded_limit}
            )
            for values in getattr(result, "result_set", []) or []:
                values = list(values)
                values.extend([None] * max(0, 8 - len(values)))
                rows.append(
                    {
                        "entity": values[0],
                        "relation": values[1],
                        "fact": values[2],
                        "id": values[3],
                        "valid_at": _string_value(values[4]),
                        "invalid_at": _string_value(values[5]),
                        "episodes": values[6],
                        "other": values[7],
                        "graph_key": graph_key,
                    }
                )
        rows = _dedupe_graph_rows(rows)[:bounded_limit]
        remaining = GRAPH_SEARCH_TIMEOUT_SECONDS - (time.monotonic() - started) - 0.02
        return await self._enrich_rows(
            rows, graph_keys, budget_seconds=min(GRAPH_PROVENANCE_TIMEOUT_SECONDS, remaining)
        )

    async def stats(self, graph_keys: list[str]) -> dict[str, Any]:
        started = time.monotonic()
        graphs: dict[str, dict[str, Any]] = {}
        if not graph_keys:
            # Verify service availability without manufacturing a graph key.
            from falkordb import FalkorDB
            def ping():
                db = FalkorDB(host=self.graph_host, port=self.graph_port,
                              socket_timeout=GRAPH_SEARCH_TIMEOUT_SECONDS,
                              socket_connect_timeout=GRAPH_SEARCH_TIMEOUT_SECONDS)
                try:
                    db.connection.ping()
                finally:
                    db.connection.close()
            await asyncio.to_thread(ping)
        for graph_key in graph_keys:
            graph = await asyncio.to_thread(self._falkor_graph, graph_key)
            node_result = await asyncio.to_thread(self._read_query, graph, "MATCH (n) RETURN count(n)")
            if getattr(node_result, "empty_graph", False):
                nodes, edges, episodes = 0, 0, [0, None]
            else:
                nodes = node_result.result_set[0][0]
                edges = (await asyncio.to_thread(self._read_query, graph,
                         "MATCH ()-[r]->() RETURN count(r)")).result_set[0][0]
                episodes = (await asyncio.to_thread(self._read_query, graph,
                    "MATCH (e:Episodic) RETURN count(e), "
                    "max(CASE WHEN e.source_description IN $markers OR e.name IN $markers "
                    "THEN NULL ELSE e.valid_at END)", {"markers": sorted(GRAPH_MARKER_SOURCES)})).result_set[0]
            graphs[graph_key] = {
                "nodes": nodes, "edges": edges, "episodes": episodes[0],
                "newest_episode": _string_value(episodes[1]),
            }

        remaining = GRAPH_SEARCH_TIMEOUT_SECONDS - (time.monotonic() - started) - 0.02
        if remaining <= 0:
            snapshot = None
        else:
            try:
                snapshot = await asyncio.wait_for(
                    asyncio.to_thread(self._checkpoint_snapshot),
                    timeout=min(GRAPH_PROVENANCE_TIMEOUT_SECONDS, remaining),
                )
            except Exception:
                snapshot = None
        scoped_watermarks: list[str] = []
        for graph_key, values in graphs.items():
            scope = self.registry.scope(graph_key)
            watermark = (
                snapshot["watermarks"].get((scope, graph_key))
                if snapshot is not None and scope is not None
                else None
            )
            values["ingestion_watermark"] = watermark
            values["ingestion_watermark_state"] = (
                "observed" if watermark is not None else "unknown"
            )
            if watermark is not None:
                scoped_watermarks.append(watermark)
        output: dict[str, Any] = {
            "graphs": graphs,
            "status": "READY",
            "empty": not any(v["nodes"] or v["edges"] for v in graphs.values()),
            "ingestion_watermark": _latest_time(scoped_watermarks),
            "ingestion_watermark_state": (
                "observed" if scoped_watermarks else "unknown"
            ),
        }
        if snapshot is not None and snapshot.get("last_complete_scan"):
            output["last_complete_scan"] = snapshot["last_complete_scan"]
        return output
