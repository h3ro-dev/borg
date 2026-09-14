"""Small, dependency-free helpers for safe bounded memory selection."""

from __future__ import annotations

import copy
from datetime import date, datetime, timezone
import math
import re
from typing import Any, Iterable

__all__ = [
    "explicit_expiry_state",
    "filter_expired_rows",
    "is_explicitly_expired",
    "merge_exact_rows",
    "normalized_text",
    "row_identity",
    "select_rows",
]

_WHITESPACE = re.compile(r"\s+", re.UNICODE)
_WORDS = re.compile(r"\w+", re.UNICODE)
_NUMBERS = re.compile(r"\d+(?:[./:_-]\d+)*", re.UNICODE)
_URL_OR_PATH = re.compile(
    r"(?:https?://\S+|www\.\S+|(?:^|[\s(])(?:[/~][^\s),]+|[A-Za-z]:[\\/][^\s),]+))",
    re.UNICODE,
)
_NEGATION_PHRASES = re.compile(
    r"\b(?:aren't|can't|couldn't|didn't|doesn't|don't|hadn't|hasn't|haven't|"
    r"isn't|mustn't|shouldn't|wasn't|weren't|won't|wouldn't)\b",
    re.IGNORECASE,
)
_NEGATION = set(
    "cannot deny denied denies disallow disallowed exclude excluded forbid forbidden "
    "never neither nor no none not prohibited reject rejected refuse refused without"
    .split()
)
_NUMBER_WORDS = set(
    "one two three four five six seven eight nine ten eleven twelve first second third "
    "fourth fifth sixth seventh eighth ninth tenth".split()
)
_STATUS_OR_DECISION = set(
    "accept accepted active allow allowed approve approved block blocked can current "
    "decayed decision delete deleted deny denied disabled enable enabled exclude excluded "
    "expired fail false grant granted historical inactive invalid keep live may must "
    "optional pass pending permit permitted preserve prohibit prohibited publish read "
    "reject rejected remove removed required retain retired rollback stale status suppress "
    "superseded tombstoned true valid writable".split()
)
_OVERLAP_THRESHOLD = 0.72
_OVERLAP_PENALTY = 0.45
_MIN_NOVELTY_SCORE = 0.25
_EXPLICIT_DATE = re.compile(r"\d{4}-\d{2}-\d{2}", re.ASCII)
EXPIRY_FIELDS = ("expiration_date", "expires_at", "valid_until")
DURABLE_RETENTION = "durable"


def normalized_text(value: Any) -> str:
    """Normalize whitespace only; preserve Unicode, case, and punctuation."""

    return _WHITESPACE.sub(" ", "" if value is None else str(value).strip())


def _text(row: dict[str, Any]) -> str:
    value = row.get("memory")
    if value in (None, ""):
        value = row.get("text", "")
    return "" if value is None else str(value)


def _mapping_containers(value: dict[str, Any]) -> Iterable[dict[str, Any]]:
    """Walk row metadata containers without selecting a preferred container."""

    pending = [value]
    seen: set[int] = set()
    while pending:
        container = pending.pop(0)
        if not isinstance(container, dict) or id(container) in seen:
            continue
        seen.add(id(container))
        yield container
        for key in ("metadata", "payload"):
            nested = container.get(key)
            if isinstance(nested, dict):
                pending.append(nested)


def _expiry_values(row: dict[str, Any]) -> list[Any]:
    """Collect every explicitly supplied expiry value in every row container."""

    return [
        container[field]
        for container in _mapping_containers(row)
        for field in EXPIRY_FIELDS
        if field in container
    ]


def _has_durable_retention(row: dict[str, Any]) -> bool:
    return any(
        isinstance(container.get("retention_class"), str)
        and container["retention_class"].strip().casefold() == DURABLE_RETENTION
        for container in _mapping_containers(row)
    )


def explicit_expiry_state(row: dict[str, Any], *, today: date | None = None) -> str:
    """Classify an explicit ISO date as active, expired, or unknown.

    Missing, malformed, and non-string values remain ``unknown``. Storage time,
    age, and similarity are deliberately not used as validity evidence.
    """

    values = _expiry_values(row)
    if not values or _has_durable_retention(row):
        return "unknown"
    parsed_values = []
    for raw in values:
        if not isinstance(raw, str) or _EXPLICIT_DATE.fullmatch(raw) is None:
            return "unknown"
        try:
            parsed_values.append(date.fromisoformat(raw))
        except ValueError:
            return "unknown"
    if len(set(parsed_values)) != 1:
        return "unknown"
    current = today or datetime.now(timezone.utc).date()
    return "expired" if parsed_values[0] < current else "active"


def is_explicitly_expired(row: dict[str, Any], *, today: date | None = None) -> bool:
    """Return true only when the row carries a valid explicit past expiry."""

    return explicit_expiry_state(row, today=today) == "expired"


def filter_expired_rows(
    rows: Iterable[dict[str, Any]], *, today: date | None = None
) -> list[dict[str, Any]]:
    """Drop only explicitly expired rows; preserve unknown expiry rows."""

    return [row for row in rows if isinstance(row, dict) and not is_explicitly_expired(row, today=today)]


def _scope_atom(value: Any) -> tuple[str, str]:
    return type(value).__name__, repr(value)


def _scope_identity(row: dict[str, Any]) -> tuple[Any, ...]:
    metadata = row.get("metadata")
    metadata_has_scope = isinstance(metadata, dict) and "scope" in metadata
    row_has_scope = "scope" in row
    if metadata_has_scope and row_has_scope:
        metadata_scope = metadata["scope"]
        row_scope = row["scope"]
        if metadata_scope != row_scope:
            return ("conflict", _scope_atom(metadata_scope), _scope_atom(row_scope))
        return ("scope", _scope_atom(metadata_scope))
    if metadata_has_scope:
        return ("scope", _scope_atom(metadata["scope"]))
    if row_has_scope:
        return ("scope", _scope_atom(row["scope"]))
    return ("missing-scope",)


def row_identity(row: dict[str, Any]) -> tuple[Any, ...]:
    """Identity for exact duplicates, including effective scope and text."""

    return (_scope_identity(row), normalized_text(_text(row)))


def _source_label(row: dict[str, Any]) -> str:
    metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
    source = metadata.get("source", row.get("source"))
    return "graphiti" if row.get("is_graph") is True or source == "graphiti" else "mem0"


def _pointer(row: dict[str, Any]) -> dict[str, Any]:
    metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
    row_id = row.get("id")
    pointer: dict[str, Any] = {
        "source": _source_label(row),
        "id": "" if row_id is None else str(row_id),
    }
    source_ref = metadata.get("source", row.get("source"))
    if source_ref not in (None, "", "graphiti", "mem0"):
        pointer["source_ref"] = str(source_ref)
    for field in ("scope", "source_graph_key", "graph_edge_uuid", "edge_uuid"):
        if field in metadata:
            pointer[field] = copy.deepcopy(metadata[field])
        elif field in row:
            pointer[field] = copy.deepcopy(row[field])
    if "episode_uuids" in metadata:
        pointer["episode_uuids"] = copy.deepcopy(metadata["episode_uuids"])
    elif "episode_uuids" in row:
        pointer["episode_uuids"] = copy.deepcopy(row["episode_uuids"])
    return pointer


def _provenance(row: dict[str, Any]) -> list[dict[str, Any]]:
    metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
    existing = metadata.get("provenance")
    pointers = [
        copy.deepcopy(item)
        for item in existing
        if isinstance(item, dict)
    ] if isinstance(existing, list) else []
    own = _pointer(row)
    if own not in pointers:
        pointers.append(own)
    return pointers


def _score(row: dict[str, Any]) -> float:
    value = row.get("score")
    if isinstance(value, bool):
        return 0.0
    try:
        score = float(value)
    except (TypeError, ValueError):
        return 0.0
    return score if math.isfinite(score) else 0.0


def _with_provenance(row: dict[str, Any], pointers: list[dict[str, Any]]) -> dict[str, Any]:
    item = copy.deepcopy(row)
    metadata = item.get("metadata")
    metadata = copy.deepcopy(metadata) if isinstance(metadata, dict) else {}
    metadata["provenance"] = copy.deepcopy(pointers)
    item["metadata"] = metadata
    item["provenance"] = copy.deepcopy(pointers)
    return item


def merge_exact_rows(
    rows: Iterable[dict[str, Any]], *, add_single_provenance: bool = True
) -> list[dict[str, Any]]:
    """Coalesce exact rows without merging loser metadata into the winner."""

    merged: dict[tuple[Any, ...], dict[str, Any]] = {}
    order: list[tuple[Any, ...]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        key = row_identity(row)
        existing = merged.get(key)
        if existing is None:
            item = copy.deepcopy(row)
            if add_single_provenance:
                item = _with_provenance(item, _provenance(item))
            merged[key] = item
            order.append(key)
            continue
        winner = row if _score(row) > _score(existing) else existing
        pointers = _provenance(existing)
        for pointer in _provenance(row):
            if pointer not in pointers:
                pointers.append(pointer)
        merged[key] = _with_provenance(winner, pointers)
    return [merged[key] for key in order]


def _tokens(text: str) -> set[str]:
    return {token.casefold() for token in _WORDS.findall(text)}


def _protected_markers(
    text: str,
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    tokens = _tokens(text)
    numbers = tuple(sorted(_NUMBERS.findall(text) + list(tokens & _NUMBER_WORDS)))
    urls_paths = tuple(sorted(match.strip(" .,;:)") for match in _URL_OR_PATH.findall(text)))
    negation = tuple(sorted((tokens & _NEGATION) | set(_NEGATION_PHRASES.findall(text))))
    status = tuple(sorted(tokens & _STATUS_OR_DECISION))
    return numbers, urls_paths, negation, status


def _overlap(left: dict[str, Any], right: dict[str, Any]) -> float:
    if _scope_identity(left) != _scope_identity(right):
        return 0.0
    left_text = normalized_text(_text(left))
    right_text = normalized_text(_text(right))
    if not left_text or not right_text:
        return 0.0
    if _protected_markers(left_text) != _protected_markers(right_text):
        return 0.0
    left_tokens = _tokens(left_text)
    right_tokens = _tokens(right_text)
    if min(len(left_tokens), len(right_tokens)) < 5:
        return 0.0
    ratio = len(left_tokens & right_tokens) / min(len(left_tokens), len(right_tokens))
    return ratio if ratio >= _OVERLAP_THRESHOLD else 0.0


def select_rows(
    rows: Iterable[dict[str, Any]],
    limit: int,
    *,
    add_single_provenance: bool = True,
) -> list[dict[str, Any]]:
    """Return a deterministic relevance-first, diversity-aware bounded list."""

    try:
        requested = max(1, int(limit))
    except (TypeError, ValueError):
        requested = 1
    source_rows = [row for row in rows if isinstance(row, dict)]
    expired_by_identity: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    selectable = []
    for row in source_rows:
        if is_explicitly_expired(row):
            expired_by_identity.setdefault(row_identity(row), []).append(row)
        else:
            selectable.append(row)
    candidates = merge_exact_rows(selectable, add_single_provenance=add_single_provenance)
    if add_single_provenance and expired_by_identity and candidates:
        # An expired exact source is not selectable, but its provenance pointer
        # still belongs on an active exact winner.
        for index, candidate in enumerate(candidates):
            pointers = _provenance(candidate)
            for expired in expired_by_identity.get(row_identity(candidate), []):
                for pointer in _provenance(expired):
                    if pointer not in pointers:
                        pointers.append(pointer)
            candidates[index] = _with_provenance(candidate, pointers)
    if len(candidates) <= requested:
        return candidates

    strongest = max((_score(row) for row in candidates), default=0.0)
    floor = max(_MIN_NOVELTY_SCORE, strongest * 0.5)
    remaining = list(enumerate(candidates))
    selected: list[tuple[int, dict[str, Any]]] = []
    overlaps: dict[int, float] = {}
    while remaining and len(selected) < requested:
        if not selected:
            position, winner = max(remaining, key=lambda pair: (_score(pair[1]), -pair[0]))
        else:
            # Only the newest selection can increase each remaining overlap.
            for position, candidate in remaining:
                overlaps[position] = max(
                    overlaps.get(position, 0.0), _overlap(candidate, selected[-1][1])
                )

            def rank(pair: tuple[int, dict[str, Any]]) -> tuple[int, float, float, int]:
                position, candidate = pair
                score = _score(candidate)
                overlap = overlaps[position]
                adjusted = score - _OVERLAP_PENALTY * overlap if score >= floor else score
                return (1 if score >= floor else 0, adjusted, score, -position)

            position, winner = max(remaining, key=rank)
        selected.append((position, winner))
        remaining = [
            (candidate_position, row)
            for candidate_position, row in remaining
            if candidate_position != position
        ]
    return [row for _, row in selected]
