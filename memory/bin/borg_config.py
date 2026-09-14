"""Portable configuration for the extracted BORG runtime.

Configuration must come from an absolute ``BORG_HOME`` and its
``config.json``.  There is deliberately no previous-owner fallback: a public
installation cannot infer another person's paths, identity, services, or
data sources.

The module has no third-party imports so every native hook and SourceFileLoader
entry point can use it before optional runtime dependencies are imported.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


REQUIRED = (
    "BORG_OWNER_ID",
    "BORG_MEMORY_SCOPE",
    "BORG_QDRANT_URL",
    "BORG_QDRANT_COLLECTION",
    "BORG_HISTORY_DB",
    "BORG_OLLAMA_URL",
    "BORG_EXTRACTION_MODEL",
    "BORG_EMBED_MODEL",
    "BORG_EMBED_DIMS",
    "BORG_FALKORDB_HOST",
    "BORG_FALKORDB_PORT",
    "BORG_FALKORDB_GRAPH",
    "BORG_GRAPH_LLM_URL",
    "BORG_GRAPH_MODEL",
)
MODEL_ID_KEYS = ("BORG_EXTRACTION_MODEL_ID", "BORG_EMBED_MODEL_ID")


class BorgConfigError(ValueError):
    """Raised when an explicit portable BORG configuration is invalid."""


def _nonempty(name: str, value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BorgConfigError(f"{name} must be a non-empty string")
    return value.strip()


def _url(name: str, value: object) -> str:
    value = _nonempty(name, value).rstrip("/")
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise BorgConfigError(f"{name} must be an absolute http(s) URL")
    return value


def _port(name: str, value: object) -> int:
    try:
        port = int(str(value))
    except (TypeError, ValueError) as exc:
        raise BorgConfigError(f"{name} must be an integer port") from exc
    if not 1 <= port <= 65535:
        raise BorgConfigError(f"{name} must be between 1 and 65535")
    return port


def _dims(value: object) -> int:
    try:
        dims = int(str(value))
    except (TypeError, ValueError) as exc:
        raise BorgConfigError("BORG_EMBED_DIMS must be a positive integer") from exc
    if dims <= 0:
        raise BorgConfigError("BORG_EMBED_DIMS must be a positive integer")
    return dims


def _home_from_env() -> Path:
    raw = os.environ.get("BORG_HOME")
    if raw is None or not raw.strip():
        raise BorgConfigError("BORG_HOME is required for the BORG memory runtime")
    home = Path(raw).expanduser()
    if not home.is_absolute():
        raise BorgConfigError("BORG_HOME must be an absolute path")
    return home.resolve()


def _load_document(home: Path) -> dict[str, object]:
    path = home / "config.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise BorgConfigError(f"BORG_HOME is set but config is missing: {path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise BorgConfigError(f"cannot read BORG config {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise BorgConfigError(f"BORG config {path} must contain a JSON object")
    return raw


def _values() -> tuple[Path, dict[str, object]]:
    home = _home_from_env()
    document = _load_document(home)
    values: dict[str, object] = {}
    missing = []
    for key in REQUIRED:
        # Environment is the installer's explicit service interface and may
        # override a generated document.  In portable mode an empty override
        # is still an error, never a signal to use a legacy fallback.
        value = os.environ.get(key, document.get(key))
        if value is None or (isinstance(value, str) and not value.strip()):
            missing.append(key)
        else:
            values[key] = value
    if missing:
        raise BorgConfigError("missing essential BORG settings: " + ", ".join(missing))
    for key in MODEL_ID_KEYS:
        value = os.environ.get(key, document.get(key))
        if value is None or not str(value).strip():
            missing.append(key)
        else:
            values[key] = value
    if missing:
        raise BorgConfigError("missing essential BORG settings: " + ", ".join(missing))
    for key in MODEL_ID_KEYS:
        values[key] = _nonempty(key, values[key])
        if not re.fullmatch(r"ollama:sha256:[0-9a-fA-F]{64}", values[key]):
            raise BorgConfigError(f"{key} must be an ollama:sha256:<digest> identity")
    values["BORG_OWNER_ID"] = _nonempty("BORG_OWNER_ID", values["BORG_OWNER_ID"])
    values["BORG_MEMORY_SCOPE"] = _nonempty("BORG_MEMORY_SCOPE", values["BORG_MEMORY_SCOPE"])
    values["BORG_QDRANT_URL"] = _url("BORG_QDRANT_URL", values["BORG_QDRANT_URL"])
    values["BORG_OLLAMA_URL"] = _url("BORG_OLLAMA_URL", values["BORG_OLLAMA_URL"])
    values["BORG_GRAPH_LLM_URL"] = _url("BORG_GRAPH_LLM_URL", values["BORG_GRAPH_LLM_URL"])
    values["BORG_QDRANT_COLLECTION"] = _nonempty("BORG_QDRANT_COLLECTION", values["BORG_QDRANT_COLLECTION"])
    values["BORG_HISTORY_DB"] = _nonempty("BORG_HISTORY_DB", values["BORG_HISTORY_DB"])
    values["BORG_EXTRACTION_MODEL"] = _nonempty("BORG_EXTRACTION_MODEL", values["BORG_EXTRACTION_MODEL"])
    values["BORG_EMBED_MODEL"] = _nonempty("BORG_EMBED_MODEL", values["BORG_EMBED_MODEL"])
    values["BORG_EMBED_DIMS"] = _dims(values["BORG_EMBED_DIMS"])
    values["BORG_FALKORDB_HOST"] = _nonempty("BORG_FALKORDB_HOST", values["BORG_FALKORDB_HOST"])
    values["BORG_FALKORDB_PORT"] = _port("BORG_FALKORDB_PORT", values["BORG_FALKORDB_PORT"])
    values["BORG_FALKORDB_GRAPH"] = _nonempty("BORG_FALKORDB_GRAPH", values["BORG_FALKORDB_GRAPH"])
    values["BORG_GRAPH_MODEL"] = _nonempty("BORG_GRAPH_MODEL", values["BORG_GRAPH_MODEL"])
    history = Path(str(values["BORG_HISTORY_DB"])).expanduser()
    if not history.is_absolute():
        history = home / history
    history = history.resolve()
    try:
        history.relative_to(home)
    except ValueError as exc:
        raise BorgConfigError("BORG_HISTORY_DB must remain below BORG_HOME") from exc
    values["BORG_HISTORY_DB"] = str(history)
    return home, values


@dataclass(frozen=True)
class BorgConfig:
    home: Path
    values: dict[str, object]

    @property
    def portable(self) -> bool:
        return True

    @property
    def mem0_root(self) -> Path:
        return self.home / "mem0"

    @property
    def graph_root(self) -> Path:
        return self.home / "graphiti"

    @property
    def data_root(self) -> Path:
        return self.mem0_root / "data"

    @property
    def graph_data_root(self) -> Path:
        return self.graph_root / "data"

    @property
    def owner_id(self) -> str:
        return str(self.values["BORG_OWNER_ID"])

    @property
    def memory_scope(self) -> str:
        return str(self.values["BORG_MEMORY_SCOPE"])

    @property
    def qdrant_url(self) -> str:
        return str(self.values["BORG_QDRANT_URL"])

    @property
    def qdrant_collection(self) -> str:
        return str(self.values["BORG_QDRANT_COLLECTION"])

    @property
    def ollama_url(self) -> str:
        return str(self.values["BORG_OLLAMA_URL"])

    def env(self) -> dict[str, str]:
        return {key: str(value) for key, value in self.values.items()}

    def path(self, key: str) -> Path:
        return Path(str(self.values[key])).expanduser()


HOME, VALUES = _values()
CONFIG = BorgConfig(HOME, VALUES)

# Native BORG scripts predate the public ``BORG_*`` interface and still read
# a few MEM0_/GRAPH_* names.  Keep those aliases in-process so the real
# implementation remains compatible while the installer exports the explicit
# BORG names to child services.  ``setdefault`` preserves focused test
# overrides such as a deliberately dead endpoint.
_ALIASES = {
    "MEM0_QDRANT_URL": "BORG_QDRANT_URL",
    "MEM0_QDRANT_COLLECTION": "BORG_QDRANT_COLLECTION",
    "MEM0_COLLECTION": "BORG_QDRANT_COLLECTION",
    "MEM0_OLLAMA_URL": "BORG_OLLAMA_URL",
    "MEM0_EMBED_OLLAMA_URL": "BORG_OLLAMA_URL",
    "MEM0_EMBED_MODEL": "BORG_EMBED_MODEL",
    "MEM0_EMBED_MODEL_ID": "BORG_EMBED_MODEL_ID",
    "MEM0_EXTRACTOR_MODEL_ID": "BORG_EXTRACTION_MODEL_ID",
    "GRAPH_LLM_URL": "BORG_GRAPH_LLM_URL",
    "GRAPH_LLM_MODEL": "BORG_GRAPH_MODEL",
    "GRAPH_RECALL_HOST": "BORG_FALKORDB_HOST",
    "GRAPH_RECALL_PORT": "BORG_FALKORDB_PORT",
}
for _alias, _source in _ALIASES.items():
    os.environ.setdefault(_alias, str(VALUES[_source]))


def require() -> BorgConfig:
    """Return the validated configuration (or raise a clear config error)."""
    return CONFIG


def ensure_runtime_dirs() -> None:
    """Create only BORG-owned mutable directories in portable mode."""
    CONFIG.data_root.mkdir(parents=True, exist_ok=True)
    CONFIG.graph_data_root.mkdir(parents=True, exist_ok=True)
