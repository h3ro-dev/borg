"""Additive, staged runtime configuration plans for the ecosystem inbox.

This module never activates a runtime by itself.  ``Installer.plan`` describes
the requested merge, ``Installer.stage`` writes a candidate and rollback
metadata into a caller-owned staging directory, and only an explicit caller
invocation of ``apply`` changes the supplied target.  JSON changes are
recursive and additive; TOML changes are append-only.  Existing scalar values
and keyed list entries are never silently replaced.

Every config uses a patch-only stage: it records only safe additions, their
inverse, and a baseline hash.  Runtime credentials remain owned by the
transport client and are never read, copied, printed, or moved by this
installer.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence


class InstallError(ValueError):
    """Invalid install plan, unsupported config, or unsafe patch."""


class MergeConflict(InstallError):
    """An additive change would replace an existing value."""


_SECRET_KEY_PARTS = (
    "password",
    "passwd",
    "token",
    "secret",
    "credential",
    "bearer",
    "cookie",
    "api_key",
    "apikey",
    "access_key",
    "private_key",
    "client_secret",
    "authorization",
)
_KEYED_LIST_FIELDS = ("id", "name", "key", "command", "type")


def _utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _json_copy(value: Any) -> Any:
    try:
        return json.loads(json.dumps(value))
    except (TypeError, ValueError) as exc:
        raise InstallError("install changes must be JSON-compatible") from exc


def _hash_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _secret_key(key: Any) -> bool:
    if not isinstance(key, str):
        return False
    lowered = key.lower().replace("-", "_")
    # File/path references do not copy the referenced credential and are safe
    # to include in a staged plan.
    if lowered.endswith(("_file", "_path", "_ref")):
        return False
    return any(part in lowered for part in _SECRET_KEY_PARTS)


def _contains_secret_like(value: Any, key: str | None = None) -> bool:
    if key is not None and _secret_key(key):
        # A path/reference key is filtered above; other matching keys are
        # refused even if their value is redacted-looking, because the staged
        # additions must never introduce credential material.
        return True
    if isinstance(value, Mapping):
        return any(_contains_secret_like(item, str(item_key)) for item_key, item in value.items())
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return any(_contains_secret_like(item) for item in value)
    return False


def _mapping_path(path: Sequence[str]) -> str:
    return ".".join(path) or "<root>"


def _safe_stage_id(value: Any) -> str:
    if not isinstance(value, str) or not value or value in {".", ".."}:
        raise InstallError("stage ID must be a non-empty path-safe string")
    if "\x00" in value or Path(value).name != value or "/" in value or "\\" in value:
        raise InstallError("stage ID must not contain path separators")
    return value


def _list_identity(value: Mapping[str, Any]) -> tuple[str, Any] | None:
    for field in _KEYED_LIST_FIELDS:
        if field in value and isinstance(value[field], (str, int, float, bool)):
            return field, value[field]
    return None


def merge_additive(existing: Any, additions: Any, path: Sequence[str] = ()) -> Any:
    """Merge JSON-like values without replacing unrelated existing fields.

    Missing mapping keys are added.  Lists append new items, but a keyed item
    with the same identity must be byte-for-byte equivalent or the operation
    raises ``MergeConflict``.  Existing scalars can only be repeated with the
    same value; a different value is never overwritten.
    """

    if isinstance(existing, Mapping) and isinstance(additions, Mapping):
        merged = {key: _json_copy(value) for key, value in existing.items()}
        for key, value in additions.items():
            child_path = (*path, str(key))
            if key not in existing:
                merged[key] = _json_copy(value)
            else:
                merged[key] = merge_additive(existing[key], value, child_path)
        return merged

    if isinstance(existing, list) and isinstance(additions, list):
        merged = [_json_copy(item) for item in existing]
        identities: dict[tuple[str, Any], Any] = {}
        for item in existing:
            if isinstance(item, Mapping):
                identity = _list_identity(item)
                if identity is not None:
                    identities[identity] = item
        for item in additions:
            if item in merged:
                continue
            if isinstance(item, Mapping):
                identity = _list_identity(item)
                if identity is not None and identity in identities:
                    if identities[identity] != item:
                        raise MergeConflict(
                            f"additive list conflict at {_mapping_path(path)} for {identity[0]}={identity[1]!r}"
                        )
                    continue
                merged_item = _json_copy(item)
                merged.append(merged_item)
                if identity is not None:
                    identities[identity] = merged_item
                continue
            merged.append(_json_copy(item))
        return merged

    if existing == additions:
        return _json_copy(existing)
    raise MergeConflict(f"cannot replace existing value at {_mapping_path(path)}")


def _json_patch_inverse(existing: Any, additions: Any, path: Sequence[str] = ()) -> list[dict[str, Any]]:
    """Describe only the additions that must be removed on rollback.

    ``existing`` may contain values that must not be serialized into staging
    artifacts.  The returned operations therefore contain only mapping keys,
    list positions, and values supplied by ``additions``.  The forward merge is
    validated first so this helper never emits a partial inverse for a
    conflicting change.
    """

    merge_additive(existing, additions, path)
    if isinstance(existing, Mapping) and isinstance(additions, Mapping):
        inverse: list[dict[str, Any]] = []
        # Staging serializes object keys in sorted order. Inverse operations
        # must retain that order when rederived after the JSON round trip.
        for key in sorted(additions):
            value = additions[key]
            child_path = (*path, str(key))
            if key not in existing:
                inverse.append({"kind": "remove_key", "path": list(child_path)})
                continue
            if isinstance(existing[key], Mapping) and isinstance(value, Mapping):
                inverse.extend(_json_patch_inverse(existing[key], value, child_path))
            elif isinstance(existing[key], list) and isinstance(value, list):
                inverse.extend(_json_patch_inverse(existing[key], value, child_path))
        return inverse
    if isinstance(existing, list) and isinstance(additions, list):
        inverse = []
        working = [_json_copy(item) for item in existing]
        for item in additions:
            if item in working:
                continue
            inverse.append({"kind": "remove_list_item", "path": list(path), "value": _json_copy(item)})
            working.append(_json_copy(item))
        return inverse
    return []


def _json_mapping_at(root: Any, path: Sequence[str]) -> Mapping[str, Any]:
    current = root
    for key in path:
        if not isinstance(key, str) or not isinstance(current, Mapping) or key not in current:
            raise InstallError("staged JSON inverse no longer matches the applied config")
        current = current[key]
    if not isinstance(current, Mapping):
        raise InstallError("staged JSON inverse points to a non-object")
    return current


def _json_value_at(root: Any, path: Sequence[str]) -> Any:
    current = root
    for key in path:
        if not isinstance(key, str) or not isinstance(current, Mapping) or key not in current:
            raise InstallError("staged JSON inverse no longer matches the applied config")
        current = current[key]
    return current


def _apply_json_patch_inverse(root: Any, inverse: Sequence[Mapping[str, Any]]) -> Any:
    """Remove exactly the fields/list entries recorded by a patch stage."""

    restored = _json_copy(root)
    for operation in reversed(list(inverse)):
        if not isinstance(operation, Mapping):
            raise InstallError("staged JSON inverse contains an invalid operation")
        kind = operation.get("kind")
        path = operation.get("path")
        if not isinstance(path, list) or any(not isinstance(item, str) for item in path):
            raise InstallError("staged JSON inverse contains an invalid path")
        if kind == "remove_key":
            if not path:
                raise InstallError("staged JSON inverse cannot remove the root")
            parent = _json_mapping_at(restored, path[:-1])
            key = path[-1]
            if key not in parent:
                raise InstallError("staged JSON inverse no longer matches the applied config")
            del parent[key]
        elif kind == "remove_list_item":
            container = _json_value_at(restored, path)
            if not isinstance(container, list) or "value" not in operation:
                raise InstallError("staged JSON inverse points to an invalid list")
            value = operation["value"]
            for index, item in enumerate(container):
                if item == value:
                    del container[index]
                    break
            else:
                raise InstallError("staged JSON inverse list item is missing")
        else:
            raise InstallError("staged JSON inverse contains an unknown operation")
    return restored


def _tree_contains(container: Any, expected: Any) -> bool:
    """Return whether a parsed TOML result still contains all old values."""

    if isinstance(expected, Mapping):
        return isinstance(container, Mapping) and all(
            key in container and _tree_contains(container[key], value) for key, value in expected.items()
        )
    if isinstance(expected, list):
        return isinstance(container, list) and len(container) >= len(expected) and all(
            _tree_contains(container[index], value) for index, value in enumerate(expected)
        )
    return container == expected


def _toml_without_comment(line: str) -> str:
    quoted = False
    escaped = False
    for index, char in enumerate(line):
        if char == "\\" and quoted and not escaped:
            escaped = True
            continue
        if char == '"' and not escaped:
            quoted = not quoted
        if char == "#" and not quoted:
            return line[:index]
        escaped = False
    return line


def _toml_array_tables(text: str) -> set[str]:
    """Return array-of-table names for the Python 3.9 TOML fallback."""

    arrays: set[str] = set()
    for raw_line in text.splitlines():
        line = _toml_without_comment(raw_line).strip()
        if line[:2] == "[[" and line[-2:] == "]]":
            section = line[2:-2].strip()
            if section:
                arrays.add(section)
    return arrays


def _toml_declared_paths(text: str, *, strict: bool) -> tuple[set[str], set[str]]:
    """Collect enough TOML structure for a conservative 3.9 fallback.

    Python 3.11's ``tomllib`` provides full validation. On Python 3.9 we do
    not need to reimplement TOML values: rejecting a repeated table/key keeps
    the merge additive while allowing arbitrary existing values to remain
    untouched.
    """

    tables: set[str] = set()
    array_tables: set[str] = set()
    keys: set[str] = set()
    section = ""
    multiline = False
    for raw_line in text.splitlines():
        line = _toml_without_comment(raw_line).strip()
        if not line:
            continue
        if multiline:
            if line.endswith("]") or line.endswith('"') or line.endswith("'"):
                multiline = False
            continue
        if line[:2] == "[[" and line[-2:] == "]]":
            section = line[2:-2].strip()
            if not section:
                if strict:
                    raise InstallError("TOML table name is empty")
                continue
            if section in tables and section not in array_tables and strict:
                raise MergeConflict(f"TOML table is repeated: {section}")
            tables.add(section)
            array_tables.add(section)
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip()
            if not section:
                if strict:
                    raise InstallError("TOML table name is empty")
                continue
            if section in tables and strict:
                raise MergeConflict(f"TOML table is repeated: {section}")
            if section in array_tables and strict:
                raise MergeConflict(f"TOML table changes an array-of-tables: {section}")
            tables.add(section)
            continue
        if "=" not in line:
            if strict:
                # Allow the continuation lines of a multiline value, but do
                # not accept a new bare statement as an additive change.
                if line.startswith(('"', "'", "[", "{")):
                    multiline = True
                    continue
                raise InstallError("TOML addition contains an invalid statement")
            continue
        key = line.split("=", 1)[0].strip()
        if not key:
            if strict:
                raise InstallError("TOML key is empty")
            continue
        full_key = f"{section}.{key}" if section else key
        if full_key in keys and strict and section not in array_tables:
            raise MergeConflict(f"TOML key is repeated: {full_key}")
        keys.add(full_key)
    return tables, keys


def _merge_toml_without_tomllib(
    existing_text: str,
    snippets: Sequence[str],
) -> str:
    old_tables, old_keys = _toml_declared_paths(existing_text, strict=False)
    old_array_tables = _toml_array_tables(existing_text)
    new_tables: set[str] = set()
    new_array_tables: set[str] = set()
    new_keys: set[str] = set()
    for snippet in snippets:
        tables, keys = _toml_declared_paths(snippet, strict=True)
        snippet_array_tables = _toml_array_tables(snippet)
        table_conflicts = old_tables.intersection(tables) - old_array_tables.intersection(snippet_array_tables)
        shared_array_tables = old_array_tables.intersection(snippet_array_tables)
        key_conflicts = old_keys.intersection(keys)
        key_conflicts = {
            key
            for key in key_conflicts
            if not any(key.startswith(f"{section}.") for section in shared_array_tables)
        }
        if table_conflicts or key_conflicts:
            raise MergeConflict("TOML additions redefine an existing table or field")
        table_conflicts = new_tables.intersection(tables) - new_array_tables.intersection(snippet_array_tables)
        shared_array_tables = new_array_tables.intersection(snippet_array_tables)
        key_conflicts = new_keys.intersection(keys)
        key_conflicts = {
            key
            for key in key_conflicts
            if not any(key.startswith(f"{section}.") for section in shared_array_tables)
        }
        if table_conflicts or key_conflicts:
            raise MergeConflict("TOML additions redefine a field")
        new_tables.update(tables)
        new_array_tables.update(snippet_array_tables)
        new_keys.update(keys)
        if any(_secret_key(key.rsplit(".", 1)[-1]) for key in keys):
            raise InstallError("secret-like fields are not valid TOML additions")
    suffix = "\n".join(item.rstrip("\n") for item in snippets)
    base = existing_text.rstrip("\n")
    return f"{base}\n\n{suffix}\n" if base else f"{suffix}\n"


def _toml_snippets(additions: Any) -> list[str]:
    if isinstance(additions, Mapping):
        snippets = additions.get("toml_append", additions.get("append"))
    else:
        snippets = additions
    if isinstance(snippets, str):
        snippets = [snippets]
    if not isinstance(snippets, Sequence) or isinstance(snippets, (bytes, bytearray)):
        raise InstallError("TOML changes must be a string or list under toml_append")
    if any(not isinstance(item, str) or not item.strip() for item in snippets):
        raise InstallError("TOML additions must contain non-empty strings")
    normalized = [item for item in snippets]
    for snippet in normalized:
        _tables, keys = _toml_declared_paths(snippet, strict=True)
        if any(_secret_key(key.rsplit(".", 1)[-1]) for key in keys):
            raise InstallError("secret-like fields are not valid TOML additions")
    return normalized


def _toml_candidate(
    existing_text: str,
    snippets: Sequence[str],
) -> str:
    """Validate an append-only TOML candidate without deciding how to store it."""

    suffix = "\n".join(item.rstrip("\n") for item in snippets)
    if suffix.strip() and all(item.strip() in existing_text for item in snippets):
        return existing_text
    base = existing_text.rstrip("\n")
    candidate = f"{base}\n\n{suffix}\n" if base else f"{suffix}\n"
    try:
        import tomllib

        original = tomllib.loads(existing_text or "")
        merged = tomllib.loads(candidate)
    except ImportError:
        return _merge_toml_without_tomllib(
            existing_text,
            snippets,
        )
    except (ValueError, TypeError) as exc:
        raise MergeConflict("TOML additions are invalid or redefine an existing field") from exc
    if not _tree_contains(merged, original):
        raise MergeConflict("TOML additions would replace an existing field")
    return candidate


def merge_toml_additive(existing_text: str, additions: Any) -> str:
    """Append TOML snippets and prove that all prior parsed values survive."""

    snippets = _toml_snippets(additions)
    return _toml_candidate(existing_text, snippets)


def _json_candidate_bytes(existing: Any, additions: Any) -> bytes:
    if not isinstance(existing, Mapping) or not isinstance(additions, Mapping):
        raise InstallError("JSON config and additions must contain objects")
    merged = merge_additive(existing, additions)
    return (json.dumps(merged, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _toml_managed_block(existing_text: str, snippets: Sequence[str]) -> tuple[str, int]:
    """Return the exact append block and original trailing-newline count."""

    trailing_newlines = len(existing_text) - len(existing_text.rstrip("\n"))
    if not snippets or all(item.strip() in existing_text for item in snippets):
        return "", trailing_newlines
    suffix = "\n".join(item.rstrip("\n") for item in snippets)
    base = existing_text.rstrip("\n")
    candidate = f"{base}\n\n{suffix}\n" if base else f"{suffix}\n"
    return candidate[len(base) :], trailing_newlines


def _infer_format(config_path: str | os.PathLike[str], explicit: str | None) -> str:
    if explicit:
        value = explicit.lower()
        if value not in {"json", "toml"}:
            raise InstallError("config format must be json or toml")
        return value
    suffix = Path(config_path).suffix.lower()
    if suffix == ".toml":
        return "toml"
    if suffix == ".json":
        return "json"
    raise InstallError("cannot infer config format; pass format='json' or format='toml'")


def _atomic_write_bytes(path: Path, data: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile("wb", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            os.chmod(handle.name, mode)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


@dataclass(frozen=True)
class InstallPlan:
    plan_id: str
    runtime: str
    config_path: str
    format: str
    changes: Any
    created_at: str
    activation: str = "root-only"

    def as_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "runtime": self.runtime,
            "config_path": self.config_path,
            "format": self.format,
            "changes": _json_copy(self.changes),
            "created_at": self.created_at,
            "activation": self.activation,
        }


class Installer:
    """Create and optionally apply additive staged config changes."""

    def __init__(self, staging_dir: str | os.PathLike[str], manifest_path: str | os.PathLike[str] | None = None) -> None:
        self.staging_dir = Path(staging_dir).expanduser()
        self.staging_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.manifest_path = Path(manifest_path) if manifest_path else Path(__file__).with_name("runtime-manifest.json")
        try:
            with self.manifest_path.open("r", encoding="utf-8") as handle:
                self.manifest = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            raise InstallError(f"cannot read runtime manifest {self.manifest_path}") from exc
        if not isinstance(self.manifest, Mapping):
            raise InstallError("runtime manifest must contain an object")

    def _runtime_entry(self, runtime: str) -> Mapping[str, Any]:
        runtimes = self.manifest.get("runtimes")
        if not isinstance(runtimes, Mapping):
            raise InstallError("runtime manifest has no runtimes map")
        requested = runtime.lower().replace("_", "-")
        for name, entry in runtimes.items():
            if not isinstance(entry, Mapping):
                continue
            aliases = entry.get("aliases", [])
            if name == requested or (isinstance(aliases, Sequence) and requested in aliases):
                return entry
        raise InstallError(f"runtime {runtime!r} is not in the manifest")

    def plan(
        self,
        runtime: str,
        config_path: str | os.PathLike[str],
        *,
        changes: Any | None = None,
        format: str | None = None,
        variables: Mapping[str, str] | None = None,
    ) -> InstallPlan:
        entry = self._runtime_entry(runtime)
        config = entry.get("installer", {})
        if not isinstance(config, Mapping):
            config = {}
        chosen_format = _infer_format(config_path, format or config.get("format"))
        if changes is None:
            changes = config.get("changes", {})
        changes = _json_copy(changes)
        if variables:
            changes = _substitute(changes, variables)
        # Validate the additions before they can be returned by the CLI.  In
        # particular, a caller must not smuggle a secret-bearing addition into
        # a plan whose JSON representation would then be printed or staged.
        if chosen_format == "json":
            if not isinstance(changes, Mapping):
                raise InstallError("JSON additions must contain an object")
            if _contains_secret_like(changes):
                raise InstallError("secret-like fields are not valid installer additions")
        else:
            _toml_snippets(changes)
        return InstallPlan(
            plan_id=str(uuid.uuid4()),
            runtime=runtime,
            config_path=str(Path(config_path).expanduser()),
            format=chosen_format,
            changes=changes,
            created_at=_utc_now(),
            activation=str(config.get("activation", "root-only")),
        )

    @staticmethod
    def merge(existing: Any, changes: Any, format: str) -> Any:
        if format == "json":
            if not isinstance(existing, Mapping) or not isinstance(changes, Mapping):
                raise InstallError("JSON config and additions must contain objects")
            if _contains_secret_like(changes):
                raise InstallError("secret-like fields are not valid installer additions")
            return merge_additive(existing, changes)
        if format == "toml":
            if not isinstance(existing, str):
                raise InstallError("TOML config must be text")
            return merge_toml_additive(existing, changes)
        raise InstallError(f"unsupported config format: {format}")

    def stage(self, plan: InstallPlan | Mapping[str, Any]) -> dict[str, Any]:
        raw_plan: Any = plan.as_dict() if isinstance(plan, InstallPlan) else plan
        if not isinstance(raw_plan, Mapping):
            raise InstallError("install plan must contain an object")
        for field in ("plan_id", "runtime", "config_path", "format", "changes"):
            if field not in raw_plan:
                raise InstallError(f"install plan missing {field}")
        # Copy only the plan fields that are part of the installer contract.
        # A caller-supplied mapping may contain unrelated data; carrying it
        # into stage.json would defeat the patch-only privacy boundary.
        plan_value: dict[str, Any] = {
            "plan_id": _json_copy(raw_plan["plan_id"]),
            "runtime": _json_copy(raw_plan["runtime"]),
            "config_path": _json_copy(raw_plan["config_path"]),
            "format": _json_copy(raw_plan["format"]),
            "changes": _json_copy(raw_plan["changes"]),
            "created_at": _json_copy(raw_plan.get("created_at", _utc_now())),
            "activation": _json_copy(raw_plan.get("activation", "root-only")),
        }
        for field in ("plan_id", "runtime", "config_path", "format", "created_at", "activation"):
            if not isinstance(plan_value[field], str):
                raise InstallError(f"install plan field {field} must be a string")
        plan_id = _safe_stage_id(plan_value["plan_id"])
        config_path = Path(str(plan_value["config_path"])).expanduser()
        format_value = str(plan_value["format"])
        if format_value not in {"json", "toml"}:
            raise InstallError("config format must be json or toml")
        if format_value == "json":
            if not isinstance(plan_value["changes"], Mapping):
                raise InstallError("JSON additions must contain an object")
            if _contains_secret_like(plan_value["changes"]):
                raise InstallError("secret-like fields are not valid installer additions")
        else:
            snippets = _toml_snippets(plan_value["changes"])
        if config_path.is_symlink():
            raise InstallError("refusing to stage through a symlinked config path")
        if config_path.exists() and not config_path.is_file():
            raise InstallError("config path is not a regular file")

        existed = config_path.exists()
        source = config_path.read_bytes() if existed else b""
        source_mode = (config_path.stat().st_mode & 0o7777) if existed else 0o600
        staged_bytes: bytes
        staged_artifact: bytes
        source_structure_sha256: str | None = None
        if format_value == "json":
            try:
                existing_value = json.loads(source.decode("utf-8")) if source.strip() else {}
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise InstallError("existing JSON config is invalid") from exc
            if not isinstance(existing_value, Mapping):
                raise InstallError("existing JSON config must contain an object")
            source_structure_sha256 = _hash_bytes(_json_candidate_bytes(existing_value, {}))
            inverse = _json_patch_inverse(existing_value, plan_value["changes"])
            candidate = _json_candidate_bytes(existing_value, plan_value["changes"])
            # Reapplying an already-present addition must not rewrite the
            # user's formatting or line endings.
            staged_bytes = candidate if inverse else source
            patch: dict[str, Any] = {
                "format": "json",
                "changes": _json_copy(plan_value["changes"]),
                "inverse": inverse,
            }
            staged_artifact = (
                json.dumps(
                    {"version": 1, "mode": "patch", "format": "json", "patch": patch},
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
                + "\n"
            ).encode("utf-8")
        else:
            try:
                source_text = source.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise InstallError("existing TOML config is not UTF-8") from exc
            candidate = _toml_candidate(
                source_text,
                snippets,
            )
            staged_bytes = candidate.encode("utf-8")
            managed_block, trailing_newlines = _toml_managed_block(source_text, snippets)
            patch = {
                "format": "toml",
                "snippets": _json_copy(snippets),
                "managed_block": managed_block,
                "source_trailing_newlines": trailing_newlines,
            }
            staged_artifact = (
                json.dumps(
                    {"version": 1, "mode": "patch", "format": "toml", "patch": patch},
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
                + "\n"
            ).encode("utf-8")

        stage_root = self.staging_dir / "staged" / plan_id
        stage_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        metadata = {
            "version": 1,
            "mode": "patch",
            "plan": plan_value,
            "target": str(config_path),
            "source_exists": existed,
            "source_mode": source_mode,
            "source_sha256": _hash_bytes(source) if existed else None,
            "source_structure_sha256": source_structure_sha256,
            "staged_sha256": _hash_bytes(staged_bytes),
            "patch": patch,
            "state": "staged",
            "created_at": _utc_now(),
        }
        staged_file = stage_root / "config.staged"
        _atomic_write_bytes(staged_file, staged_artifact, 0o600)
        _atomic_write_bytes(stage_root / "stage.json", json.dumps(metadata, indent=2, sort_keys=True).encode("utf-8"))
        return {
            "stage_id": plan_id,
            "runtime": plan_value["runtime"],
            "target": str(config_path),
            "staged": str(staged_file),
            "mode": "patch",
            "source_exists": existed,
            "source_sha256": metadata["source_sha256"],
            "staged_sha256": metadata["staged_sha256"],
            "activation": plan_value.get("activation", "root-only"),
            "state": "staged",
        }

    def _stage_metadata(self, stage: str | os.PathLike[str] | Mapping[str, Any]) -> dict[str, Any]:
        if isinstance(stage, Mapping):
            stage_id = stage.get("stage_id") or stage.get("plan_id")
            stage_id = _safe_stage_id(stage_id)
            path = self.staging_dir / "staged" / stage_id / "stage.json"
        else:
            path = Path(stage)
            if path.is_dir():
                path = path / "stage.json"
        if not path.exists():
            raise InstallError(f"stage metadata does not exist: {path}")
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise InstallError("stage metadata must contain an object")
        return value

    def _stage_root_for_metadata(self, metadata: Mapping[str, Any]) -> Path:
        plan = metadata.get("plan")
        if not isinstance(plan, Mapping):
            raise InstallError("stage metadata has no install plan")
        plan_id = _safe_stage_id(plan.get("plan_id"))
        return self.staging_dir / "staged" / plan_id

    def _write_stage_metadata(self, metadata: Mapping[str, Any]) -> None:
        path = self._stage_root_for_metadata(metadata) / "stage.json"
        _atomic_write_bytes(path, json.dumps(dict(metadata), indent=2, sort_keys=True).encode("utf-8"), 0o600)

    @staticmethod
    def _patch_artifact(staged: Path, metadata: Mapping[str, Any]) -> Mapping[str, Any]:
        patch = metadata.get("patch")
        if not isinstance(patch, Mapping):
            raise InstallError("patch stage metadata is missing its patch")
        try:
            artifact = json.loads(staged.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise InstallError("patch stage artifact is invalid") from exc
        if not isinstance(artifact, Mapping) or artifact.get("mode") != "patch" or artifact.get("patch") != patch:
            raise InstallError("patch stage artifact does not match its metadata")
        return patch

    @staticmethod
    def _read_json_object(data: bytes) -> Mapping[str, Any]:
        try:
            value = json.loads(data.decode("utf-8")) if data.strip() else {}
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise InstallError("existing JSON config is invalid") from exc
        if not isinstance(value, Mapping):
            raise InstallError("existing JSON config must contain an object")
        return value

    def _apply_patch(self, metadata: dict[str, Any], staged: Path, target: Path) -> dict[str, Any]:
        source_exists = bool(metadata.get("source_exists"))
        if source_exists and (not target.exists() or not target.is_file()):
            raise InstallError("patch target is missing or not a regular file")
        if not source_exists and target.exists() and not target.is_file():
            raise InstallError("patch target is not a regular file")
        current = target.read_bytes() if target.exists() else b""
        current_hash = _hash_bytes(current) if target.exists() else None
        if current_hash != metadata.get("source_sha256"):
            raise InstallError("target changed after staging; create a new plan")
        patch = self._patch_artifact(staged, metadata)
        format_value = patch.get("format")
        if format_value == "json":
            changes = patch.get("changes")
            inverse = patch.get("inverse")
            if not isinstance(changes, Mapping) or not isinstance(inverse, list):
                raise InstallError("JSON patch metadata is invalid")
            if _contains_secret_like(changes):
                raise InstallError("secret-like fields are not valid installer additions")
            existing = self._read_json_object(current)
            baseline_structure_hash = metadata.get("source_structure_sha256")
            if isinstance(baseline_structure_hash, str) and _hash_bytes(_json_candidate_bytes(existing, {})) != baseline_structure_hash:
                raise InstallError("JSON target structure changed after staging")
            expected_inverse = _json_patch_inverse(existing, changes)
            if expected_inverse != inverse:
                raise InstallError("JSON patch no longer matches its baseline")
            candidate = _json_candidate_bytes(existing, changes) if inverse else current
        elif format_value == "toml":
            snippets = _toml_snippets(patch.get("snippets"))
            try:
                source_text = current.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise InstallError("existing TOML config is not UTF-8") from exc
            candidate_text = _toml_candidate(
                source_text,
                snippets,
            )
            managed_block, trailing_newlines = _toml_managed_block(source_text, snippets)
            if patch.get("managed_block") != managed_block or patch.get("source_trailing_newlines") != trailing_newlines:
                raise InstallError("TOML patch no longer matches its baseline")
            candidate = candidate_text.encode("utf-8")
        else:
            raise InstallError("patch stage has an unsupported format")
        candidate_hash = _hash_bytes(candidate)
        if candidate_hash != metadata.get("staged_sha256"):
            raise InstallError("patch result does not match its staged baseline")
        _atomic_write_bytes(target, candidate, int(metadata.get("source_mode", 0o600)))
        metadata["state"] = "applied"
        metadata["applied_sha256"] = candidate_hash
        metadata["applied_at"] = _utc_now()
        self._write_stage_metadata(metadata)
        return {"state": "applied", "target": str(target), "sha256": candidate_hash, "mode": "patch"}

    def _rollback_patch(self, metadata: dict[str, Any], target: Path) -> dict[str, Any]:
        if not target.exists() or not target.is_file():
            raise InstallError("patch target is missing or not a regular file")
        source_exists = bool(metadata.get("source_exists"))
        current = target.read_bytes()
        current_hash = _hash_bytes(current)
        applied_hash = metadata.get("applied_sha256")
        if not isinstance(applied_hash, str) or current_hash != applied_hash:
            raise InstallError("target changed after activation; refusing to overwrite newer config")
        patch = metadata.get("patch")
        if not isinstance(patch, Mapping):
            raise InstallError("patch stage metadata is missing its patch")
        format_value = patch.get("format")
        if format_value == "json":
            inverse = patch.get("inverse")
            if not isinstance(inverse, list):
                raise InstallError("JSON patch metadata is invalid")
            existing = self._read_json_object(current)
            restored = _apply_json_patch_inverse(existing, inverse)
            restored_bytes = current if not inverse else (
                (json.dumps(restored, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
            )
            if source_exists:
                baseline_structure_hash = metadata.get("source_structure_sha256")
                if not isinstance(baseline_structure_hash, str) or _hash_bytes(_json_candidate_bytes(restored, {})) != baseline_structure_hash:
                    raise InstallError("JSON patch inverse did not restore its baseline structure")
        elif format_value == "toml":
            managed_block = patch.get("managed_block")
            trailing_newlines = patch.get("source_trailing_newlines")
            if not isinstance(managed_block, str) or not isinstance(trailing_newlines, int) or trailing_newlines < 0:
                raise InstallError("TOML patch metadata is invalid")
            try:
                current_text = current.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise InstallError("existing TOML config is not UTF-8") from exc
            if managed_block:
                if not current_text.endswith(managed_block):
                    raise InstallError("TOML patch insertion is no longer present")
                restored_text = current_text[: -len(managed_block)] + ("\n" * trailing_newlines)
            else:
                restored_text = current_text
            # Parse the result without retaining it in stage metadata. This
            # catches tampered insertion metadata before any write.
            _toml_candidate(restored_text, [])
            restored_bytes = restored_text.encode("utf-8")
            if source_exists and _hash_bytes(restored_bytes) != metadata.get("source_sha256"):
                raise InstallError("TOML patch inverse did not restore its baseline")
        else:
            raise InstallError("patch stage has an unsupported format")
        if not source_exists:
            if format_value == "json" and restored != {}:
                raise InstallError("JSON patch inverse did not restore the empty baseline")
            if format_value == "toml" and restored_bytes != (b"\n" * int(patch["source_trailing_newlines"])):
                raise InstallError("TOML patch inverse did not restore the empty baseline")
            target.unlink()
            restored_hash = None
        else:
            _atomic_write_bytes(target, restored_bytes, int(metadata.get("source_mode", 0o600)))
            restored_hash = _hash_bytes(restored_bytes)
        metadata["state"] = "rolled_back"
        metadata["rolled_back_sha256"] = restored_hash
        metadata["rolled_back_at"] = _utc_now()
        self._write_stage_metadata(metadata)
        return {"state": "rolled_back", "target": str(target), "sha256": restored_hash, "mode": "patch"}

    def apply(self, stage: str | os.PathLike[str] | Mapping[str, Any]) -> dict[str, Any]:
        """Explicitly activate a staged file; never called by ``plan``/``stage``."""

        metadata = self._stage_metadata(stage)
        staged = self._stage_root_for_metadata(metadata) / "config.staged"
        target = Path(str(metadata["target"])).expanduser()
        if not staged.exists():
            raise InstallError("staged config is missing")
        if target.is_symlink():
            raise InstallError("refusing to activate through a symlinked config path")
        if metadata.get("mode") != "patch":
            raise InstallError("stage is not a patch-only artifact")
        return self._apply_patch(metadata, staged, target)

    def rollback(self, stage: str | os.PathLike[str] | Mapping[str, Any]) -> dict[str, Any]:
        """Restore the exact pre-stage file after an explicit activation."""

        metadata = self._stage_metadata(stage)
        target = Path(str(metadata["target"])).expanduser()
        if target.is_symlink():
            raise InstallError("refusing to roll back through a symlinked config path")
        if metadata.get("mode") != "patch":
            raise InstallError("stage is not a patch-only artifact")
        return self._rollback_patch(metadata, target)


def _substitute(value: Any, variables: Mapping[str, str]) -> Any:
    if isinstance(value, str):
        output = value
        for key, replacement in variables.items():
            output = output.replace("{" + key + "}", replacement)
        return output
    if isinstance(value, Mapping):
        return {key: _substitute(item, variables) for key, item in value.items()}
    if isinstance(value, list):
        return [_substitute(item, variables) for item in value]
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Plan and stage additive inbox runtime config changes")
    parser.add_argument("--runtime", required=True)
    parser.add_argument("--config", required=True, dest="config_path")
    parser.add_argument("--staging-dir", required=True)
    parser.add_argument("--format", choices=("json", "toml"))
    parser.add_argument("--manifest")
    parser.add_argument("--variables-file", help="JSON object used for template substitution")
    parser.add_argument("--changes-file", help="JSON changes object/list; otherwise manifest template")
    parser.add_argument("--apply-stage", help="explicitly apply an existing stage after review")
    parser.add_argument("--rollback-stage", help="explicitly roll back an existing stage")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        installer = Installer(args.staging_dir, args.manifest)
        variables = None
        if args.variables_file:
            variables_value = json.loads(Path(args.variables_file).read_text(encoding="utf-8"))
            if not isinstance(variables_value, Mapping):
                raise InstallError("variables file must contain an object")
            variables = {str(key): str(value) for key, value in variables_value.items()}
        changes = None
        if args.changes_file:
            changes = json.loads(Path(args.changes_file).read_text(encoding="utf-8"))
        if args.apply_stage:
            result = installer.apply(args.apply_stage)
        elif args.rollback_stage:
            result = installer.rollback(args.rollback_stage)
        else:
            plan = installer.plan(
                args.runtime,
                args.config_path,
                changes=changes,
                format=args.format,
                variables=variables,
            )
            result = {"plan": plan.as_dict(), "stage": installer.stage(plan)}
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except (InstallError, OSError, json.JSONDecodeError):
        print(json.dumps({"error": "install operation failed"}))
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "InstallError",
    "InstallPlan",
    "Installer",
    "MergeConflict",
    "main",
    "merge_additive",
    "merge_toml_additive",
]
