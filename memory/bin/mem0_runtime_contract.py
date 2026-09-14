#!/usr/bin/env python3
"""Versioned admission contract for new BORG Mem0 writes."""
from __future__ import annotations

from copy import deepcopy
import math
import numbers
import os
import re
from pathlib import Path
from typing import Mapping
import importlib.machinery

CONFIG = importlib.machinery.SourceFileLoader(
    "borg_config_runtime_contract", str(Path(__file__).resolve().parent / "borg_config.py")
).load_module().CONFIG

MEMORY_SCHEMA_VERSION = int(os.environ.get("MEM0_MEMORY_SCHEMA_VERSION", "2"))
EMBED_MODEL = str(CONFIG.values["BORG_EMBED_MODEL"])
EMBED_MODEL_ID = os.environ.get("MEM0_EMBED_MODEL_ID", str(CONFIG.values["BORG_EMBED_MODEL_ID"]))
EMBED_DIMS = int(os.environ.get("MEM0_EMBED_DIMS", "768"))
EXTRACTOR_MODEL_ID = os.environ.get("MEM0_EXTRACTOR_MODEL_ID", str(CONFIG.values["BORG_EXTRACTION_MODEL_ID"]))
EXTRACTOR_MODEL_IDS = {
    str(CONFIG.values["BORG_EXTRACTION_MODEL"]): EXTRACTOR_MODEL_ID,
    EXTRACTOR_MODEL_ID: EXTRACTOR_MODEL_ID,
}


def extractor_identity(model: str) -> str:
    try:
        return EXTRACTOR_MODEL_IDS[model]
    except (KeyError, TypeError):
        raise ValueError("extractor_model:unregistered") from None


_SCOPE_RE = re.compile(r"(?:ops|[a-z][a-z0-9_-]*:[A-Za-z0-9][A-Za-z0-9._-]*)\Z")


def _clean_text(value, field, maximum=4096):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{field}:invalid")
    value = value.strip()
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise ValueError(f"{field}:invalid")
    return value


def normalize_metadata(metadata: Mapping | None = None, *, default_scope: str | None = None,
                       default_source: str = "mem0-runtime", infer: bool | None = None, extractor_model_id: str | None = None) -> dict:
    if metadata is not None and not isinstance(metadata, Mapping):
        raise ValueError("metadata:invalid")
    out = deepcopy(dict(metadata or {}))
    scope = _clean_text(out.pop("scope", None) or default_scope or str(CONFIG.values["BORG_MEMORY_SCOPE"]), "scope", 256)
    if not _SCOPE_RE.fullmatch(scope):
        raise ValueError("scope:concrete-required")
    source = _clean_text(out.pop("source", None) or default_source, "source")
    if infer is not None and not isinstance(infer, bool):
        raise ValueError("infer:invalid")
    mode = "inferred" if infer is True else "verbatim" if infer is False else "native"
    expected = {
        "schema_version": MEMORY_SCHEMA_VERSION,
        "embedding_model": EMBED_MODEL,
        "embedding_model_id": EMBED_MODEL_ID,
        "embedding_dimensions": EMBED_DIMS,
        "write_mode": mode,
        "extractor_model_id": (extractor_model_id or EXTRACTOR_MODEL_ID) if infer is True else None,
    }
    for key, value in expected.items():
        if key in out and (type(out[key]) is not type(value) or out[key] != value):
            raise ValueError(f"{key}:mismatch")
        out[key] = value
    out["scope"], out["source"] = scope, source
    ok, problems = validate_new_write(out)
    if not ok:
        raise ValueError(";".join(problems))
    return out


def write_metadata(*, scope: str, source: str, extra: Mapping | None = None,
                   infer: bool | None = True) -> dict:
    scope = _clean_text(scope, "scope", 256)
    source = _clean_text(source, "source")
    extra = dict(extra or {})
    reserved = {
        "scope", "source", "schema_version", "embedding_model", "embedding_model_id",
        "embedding_dimensions", "write_mode", "extractor_model_id",
    }
    if reserved.intersection(extra):
        raise ValueError("metadata:reserved-field")
    return normalize_metadata({"scope": scope, "source": source, **extra}, infer=infer)


def validate_new_write(metadata: Mapping) -> tuple[bool, list[str]]:
    """Validate persisted metadata without silently supplying missing fields."""
    if not isinstance(metadata, Mapping):
        return False, ["metadata:invalid"]
    problems = []
    expected = {"schema_version": MEMORY_SCHEMA_VERSION, "embedding_model": EMBED_MODEL,
                "embedding_model_id": EMBED_MODEL_ID, "embedding_dimensions": EMBED_DIMS}
    for key, value in expected.items():
        if key not in metadata:
            problems.append(f"{key}:missing")
        elif type(metadata[key]) is not type(value) or metadata[key] != value:
            problems.append(f"{key}:mismatch")
    for key in ("scope", "source"):
        try:
            value = _clean_text(metadata.get(key), key, 256 if key == "scope" else 4096)
            if metadata[key] != value:
                problems.append(f"{key}:noncanonical")
            if key == "scope" and not _SCOPE_RE.fullmatch(value):
                problems.append("scope:concrete-required")
        except ValueError as exc:
            problems.append(str(exc))
    mode = metadata.get("write_mode")
    if mode not in ("native", "verbatim", "inferred"):
        problems.append("write_mode:invalid")
    if "extractor_model_id" not in metadata:
        problems.append("extractor_model_id:missing")
    elif mode == "inferred" and metadata["extractor_model_id"] not in EXTRACTOR_MODEL_IDS.values():
        problems.append("extractor_model_id:unregistered")
    elif mode != "inferred" and metadata["extractor_model_id"] is not None:
        problems.append("extractor_model_id:mismatch")
    return not problems, problems


def validate_vector(vector) -> None:
    if not isinstance(vector, (list, tuple)) or len(vector) != EMBED_DIMS:
        raise ValueError("vector:dimension-mismatch")
    for value in vector:
        if isinstance(value, bool) or not isinstance(value, numbers.Real):
            raise ValueError("vector:invalid-value")
        if not math.isfinite(value):
            raise ValueError("vector:invalid-value")
