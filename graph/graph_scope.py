"""Deterministic, collision-checked Graphiti group keys for memory scopes.

The source scope is deliberately kept separate from the FalkorDB graph name.
Graphiti accepts only ASCII letters, digits, ``-`` and ``_`` in a group ID,
while memory scopes contain characters such as ``:``.  The first 24 hex
characters of SHA-256 provide a stable, compact graph key and the registry
keeps the reverse mapping so a collision or a missing mapping fails closed.
"""

from __future__ import annotations

import hashlib
import importlib.machinery
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any


def _default_scope() -> str:
    """Use only the owner scope from an explicit BORG installation."""

    config_path = Path(__file__).resolve().parent / "bin" / "borg_config.py"
    config = importlib.machinery.SourceFileLoader(
        "borg_config_graph_scope", str(config_path)
    ).load_module().CONFIG
    return str(config.values["BORG_MEMORY_SCOPE"])


DEFAULT_SCOPE = _default_scope()
GROUP_PREFIX = "memscope_"
GROUP_DIGEST_HEX_LENGTH = 24
GROUP_KEY_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
MAPPING_SCHEMA = 1
DEFAULT_MAPPING_PATH = Path(__file__).resolve().parent / "data" / "scope-graphs.json"


class ScopeMappingError(ValueError):
    """Base class for invalid or unsafe scope registry data."""


class ScopeCollisionError(ScopeMappingError):
    """Raised when two scopes resolve to one graph key."""


def normalize_scope(scope: Any) -> str:
    """Return a usable source scope, defaulting only missing/blank values."""

    if scope is None:
        return DEFAULT_SCOPE
    if not isinstance(scope, str):
        raise ScopeMappingError(f"scope must be a string or missing, got {type(scope).__name__}")
    value = scope.strip()
    return value or DEFAULT_SCOPE


def group_key_for_scope(scope: str) -> str:
    """Compute the deterministic Graphiti-valid key for one source scope."""

    normalized = normalize_scope(scope)
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    key = GROUP_PREFIX + digest[:GROUP_DIGEST_HEX_LENGTH]
    if not is_valid_group_key(key):
        raise ScopeMappingError(f"derived invalid Graphiti group key: {key!r}")
    return key


def is_valid_group_key(group_key: str) -> bool:
    """Return whether a key is safe for Graphiti's Falkor group_id contract."""

    return isinstance(group_key, str) and bool(GROUP_KEY_PATTERN.fullmatch(group_key))


def _empty_mapping() -> dict[str, Any]:
    return {"schema": MAPPING_SCHEMA, "scopes": {}}


def _read_mapping(path: Path) -> dict[str, Any]:
    if not path.exists():
        return _empty_mapping()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ScopeMappingError(f"cannot read scope mapping {path}: {exc}") from exc
    if not isinstance(raw, dict) or raw.get("schema") != MAPPING_SCHEMA:
        raise ScopeMappingError(f"unsupported scope mapping schema in {path}")
    scopes = raw.get("scopes")
    if not isinstance(scopes, dict):
        raise ScopeMappingError(f"scope mapping has no object-valued scopes field: {path}")
    for source_scope, group_key in scopes.items():
        if not isinstance(source_scope, str) or not isinstance(group_key, str):
            raise ScopeMappingError("scope mappings must contain string keys and values")
        if not is_valid_group_key(group_key):
            raise ScopeMappingError(f"invalid group key in scope mapping: {group_key!r}")
    _assert_no_collisions(scopes)
    return {"schema": MAPPING_SCHEMA, "scopes": dict(scopes)}


def _assert_no_collisions(scopes: dict[str, str]) -> None:
    owners: dict[str, str] = {}
    for source_scope, group_key in scopes.items():
        previous = owners.get(group_key)
        if previous is not None and previous != source_scope:
            raise ScopeCollisionError(
                f"scope collision: {previous!r} and {source_scope!r} both map to {group_key!r}"
            )
        owners[group_key] = source_scope


def save_scope_mapping_atomic(path: str | Path, mapping: dict[str, Any]) -> None:
    """Persist a validated mapping with same-directory temp-file replacement."""

    target = Path(path)
    scopes = mapping.get("scopes") if isinstance(mapping, dict) else None
    if (
        not isinstance(mapping, dict)
        or mapping.get("schema") != MAPPING_SCHEMA
        or not isinstance(scopes, dict)
    ):
        raise ScopeMappingError("cannot write an invalid scope mapping")
    _assert_no_collisions(scopes)
    for group_key in scopes.values():
        if not is_valid_group_key(group_key):
            raise ScopeMappingError(f"invalid group key: {group_key!r}")

    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(mapping, handle, indent=2, sort_keys=True)
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


class ScopeRegistry:
    """Read and atomically extend the source-scope to graph-key registry."""

    def __init__(self, path: str | Path = DEFAULT_MAPPING_PATH):
        self.path = Path(path)

    def _mapping(self) -> dict[str, Any]:
        return _read_mapping(self.path)

    def ensure_scope(self, scope: Any) -> str:
        normalized = normalize_scope(scope)
        mapping = self._mapping()
        scopes = mapping["scopes"]
        expected = group_key_for_scope(normalized)
        existing = scopes.get(normalized)
        if existing is not None and existing != expected:
            raise ScopeMappingError(
                f"scope mapping changed for {normalized!r}: {existing!r} != {expected!r}"
            )
        for other_scope, other_key in scopes.items():
            if other_scope != normalized and other_key == expected:
                raise ScopeCollisionError(
                    f"scope collision: {other_scope!r} and {normalized!r} both map to {expected!r}"
                )
        if existing is None:
            scopes[normalized] = expected
            _assert_no_collisions(scopes)
            save_scope_mapping_atomic(self.path, mapping)
        return expected

    def get(self, scope: Any) -> str | None:
        normalized = normalize_scope(scope)
        return self._mapping()["scopes"].get(normalized)

    def registered(self) -> dict[str, str]:
        return dict(self._mapping()["scopes"])
