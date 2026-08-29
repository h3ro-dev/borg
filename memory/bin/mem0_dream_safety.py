"""Shared safety primitives for the nightly Dream v2 pipeline.

This module uses the standard library by default and an explicitly configured
local Qwen tokenizer only when available.  The dream therefore still fails
safely when optional Python clients are unavailable.  It owns the two
invariants that must not drift between ``mem0-dream`` and ``mem0ctl
consolidate``:

* serialized model prompts are admitted only inside a per-model input budget;
* destructive Dream canaries have deterministic ids and expected lifecycle
  states.
"""

from __future__ import annotations

import json
import math
import os
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Callable, Iterable, Sequence

# Input envelopes are deliberately lower than vendor context windows: output,
# tokenizer drift, and the immutable judge instruction all need headroom.
# Add a model here before it is allowed to make a destructive Dream call.
MODEL_MAX_INPUT_TOKENS: dict[str, int] = {
    "grok-4.6": 48_000,
    "qwen3.8:27b-mlx": 48_000,
    "qwen3.8:27b": 48_000,
    "opencode/x-preview-f-free": 48_000,
    "openrouter/stealth/ox-alpha": 48_000,
}
QWEN_TOKENIZER_MODEL = "qwen3.8:27b-mlx"

# Legacy character guards remain secondary circuit breakers.  They are never
# allowed to truncate data: the token admission controller counts first and
# recursively splits an oversized row set instead.
SECONDARY_MAX_SERIALIZED_CHARS = 393_216
ESTIMATED_CHARS_PER_TOKEN = 4
ESTIMATED_TOKEN_SAFETY_MARGIN = 1.20

CANARY_NAMESPACE = uuid.UUID("6ceff688-79d8-5ac2-99b3-9bd27c203b48")
CANARY_VERSION = "dream-v2-wave1"
CANARY_USER = "james"


@dataclass(frozen=True)
class TokenMeasurement:
    tokens: int
    source: str

    @property
    def estimated(self) -> bool:
        return self.source.startswith("ESTIMATED")


@dataclass(frozen=True)
class PromptAdmission:
    rows: tuple[dict[str, Any], ...]
    prompt: str
    measurement: TokenMeasurement
    envelope: int


def _post_json(url: str, body: dict[str, Any], timeout_s: float = 8.0) -> Any:
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout_s) as response:
        return json.loads(response.read().decode("utf-8"))


def _ollama_token_count(model: str, serialized_prompt: str, ollama_url: str) -> int | None:
    """Ask Ollama's tokenizer endpoint without ever sending a generation call.

    Not every installed Ollama build exposes ``/api/tokenize``.  Its absence is
    expected and is handled by the explicit, loudly-marked conservative
    estimate below.
    """
    try:
        result = _post_json(
            ollama_url.rstrip("/") + "/api/tokenize",
            {"model": model, "prompt": serialized_prompt},
        )
    except (OSError, urllib.error.URLError, urllib.error.HTTPError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(result, dict):
        return None
    for key in ("token_count", "count", "tokens"):
        value = result.get(key)
        if isinstance(value, int) and value >= 0:
            return value
        if isinstance(value, list):
            return len(value)
    return None


@lru_cache(maxsize=2)
def _local_qwen_tokenizer(path: str):
    """Load only an explicitly configured local tokenizer; never download one."""
    from transformers import AutoTokenizer

    return AutoTokenizer.from_pretrained(path, local_files_only=True, use_fast=True)


def _local_qwen_token_count(serialized_prompt: str) -> int | None:
    """Use a local Qwen tokenizer when the venv has one configured on disk."""
    paths = [part for part in os.environ.get("MEM0_QWEN_TOKENIZER_PATH", "").split(os.pathsep) if part]
    for path in paths:
        try:
            return len(_local_qwen_tokenizer(path).encode(serialized_prompt, add_special_tokens=False))
        except Exception:
            continue
    return None


def measure_serialized_prompt(serialized_prompt: str, *, ollama_url: str | None = None) -> TokenMeasurement:
    """Count a fully serialized prompt, with a conservative marked fallback."""
    endpoint = ollama_url or os.environ.get("MEM0_OLLAMA_URL", "http://127.0.0.1:11434")
    if not os.environ.get("MEM0_TOKEN_GATE_FORCE_ESTIMATE"):
        exact = _ollama_token_count(QWEN_TOKENIZER_MODEL, serialized_prompt, endpoint)
        if exact is not None:
            return TokenMeasurement(exact, "OLLAMA_API_TOKENIZE")
        local = _local_qwen_token_count(serialized_prompt)
        if local is not None:
            return TokenMeasurement(local, "LOCAL_QWEN_TOKENIZER")
    estimated = math.ceil(
        (len(serialized_prompt) / ESTIMATED_CHARS_PER_TOKEN) * ESTIMATED_TOKEN_SAFETY_MARGIN
    )
    return TokenMeasurement(estimated, "ESTIMATED_CHARS_DIV4_X1.20")


def _row_label(rows: Sequence[dict[str, Any]]) -> str:
    if len(rows) != 1:
        return f"rows={len(rows)}"
    return f"row={str(rows[0].get('id') or rows[0].get('canary_id') or 'unknown')[:120]}"


def admit_serialized_prompt(
    *,
    phase: str,
    model: str | None = None,
    models: Sequence[str] | None = None,
    rows: Iterable[dict[str, Any]],
    render_prompt: Callable[[Sequence[dict[str, Any]]], str],
    log: Callable[[str], None],
    secondary_max_chars: int = SECONDARY_MAX_SERIALIZED_CHARS,
    envelope: int | None = None,
) -> list[PromptAdmission]:
    """The sole admission gate for destructive Dream model calls.

    ``render_prompt`` must return the whole request string, including static
    instructions.  Every candidate is measured after that serialization.  An
    oversize multi-row request is recursively bisected; an oversize single row
    is quarantined and never handed to a model.  No branch truncates prompt
    content.
    """
    row_tuple = tuple(rows)
    if not row_tuple:
        return []
    call_models = tuple(models or (() if model is None else (model,)))
    if not call_models:
        raise ValueError(f"TOKEN-ADMISSION-ERROR phase={phase} has no model")
    unknown = [name for name in call_models if name not in MODEL_MAX_INPUT_TOKENS]
    if unknown:
        raise ValueError(f"TOKEN-ADMISSION-ERROR phase={phase} models={','.join(unknown)} have no max_input envelope")
    max_tokens = envelope if envelope is not None else min(MODEL_MAX_INPUT_TOKENS[name] for name in call_models)
    model_label = ",".join(call_models)
    prompt = render_prompt(row_tuple)
    measured = measure_serialized_prompt(prompt)
    marker = " ESTIMATED" if measured.estimated else ""
    log(
        f"TOKEN-ADMISSION phase={phase} models={model_label} tokens={measured.tokens} "
        f"envelope={max_tokens} chars={len(prompt)} tokenizer={measured.source}{marker} {_row_label(row_tuple)}"
    )
    if measured.tokens <= max_tokens and len(prompt) <= secondary_max_chars:
        return [PromptAdmission(row_tuple, prompt, measured, max_tokens)]
    if len(row_tuple) == 1:
        reason = "tokens" if measured.tokens > max_tokens else "secondary_chars"
        log(
            f"TOKEN-QUARANTINE phase={phase} models={model_label} reason={reason} "
            f"tokens={measured.tokens} envelope={max_tokens} chars={len(prompt)} "
            f"tokenizer={measured.source} {_row_label(row_tuple)}"
        )
        return []
    midpoint = len(row_tuple) // 2
    log(
        f"TOKEN-SPLIT phase={phase} models={model_label} tokens={measured.tokens} envelope={max_tokens} "
        f"rows={len(row_tuple)} split={midpoint}+{len(row_tuple) - midpoint}"
    )
    return admit_serialized_prompt(
        phase=phase,
        model=model,
        models=call_models,
        rows=row_tuple[:midpoint],
        render_prompt=render_prompt,
        log=log,
        secondary_max_chars=secondary_max_chars,
        envelope=max_tokens,
    ) + admit_serialized_prompt(
        phase=phase,
        model=model,
        models=call_models,
        rows=row_tuple[midpoint:],
        render_prompt=render_prompt,
        log=log,
        secondary_max_chars=secondary_max_chars,
        envelope=max_tokens,
    )


def canary_id(scope: str, case: str, side: str) -> str:
    return str(uuid.uuid5(CANARY_NAMESPACE, f"{CANARY_VERSION}:{scope}:{case}:{side}"))


def _row(scope: str, case: str, side: str, text: str, *, status: str | None = None,
         superseded_by: str | None = None, peer_scope: str | None = None) -> dict[str, Any]:
    pid = canary_id(scope, case, side)
    payload: dict[str, Any] = {
        "data": text,
        "user_id": CANARY_USER,
        "agent_id": "dream-v2-canary",
        "scope": scope,
        "source": CANARY_VERSION,
        "is_canary": True,
        "canary_case": case,
        "canary_side": side,
        "canary_id": pid,
        "canary_expected_status": status or "live",
    }
    if status:
        payload["status"] = status
    if superseded_by:
        payload["superseded_by"] = superseded_by
    if peer_scope:
        payload["canary_peer_scope"] = peer_scope
    return {"id": pid, "payload": payload}


def canary_rows(scopes: Sequence[str]) -> list[dict[str, Any]]:
    """Return five deterministic pair cases (ten stored rows) for each scope.

    The named cases are pairs, so exercising all five necessarily requires ten
    Qdrant points per scope.  Cross-scope rows form a ring: each scope owns one
    outgoing and one incoming sentinel, preserving that ten-point denominator.
    """
    ordered = sorted({str(scope) for scope in scopes if str(scope).strip()})
    rows: list[dict[str, Any]] = []
    if not ordered:
        return rows
    for index, scope in enumerate(ordered):
        successor = ordered[(index + 1) % len(ordered)]
        supersession_new = canary_id(scope, "supersession", "new")
        rows.extend([
            _row(scope, "exact-duplicate", "a", f"{CANARY_VERSION} exact duplicate sentinel for {scope}."),
            _row(scope, "exact-duplicate", "b", f"{CANARY_VERSION} exact duplicate sentinel for {scope}."),
            _row(scope, "near-distinct", "a", f"{CANARY_VERSION} near but distinct alpha fact for {scope}."),
            _row(scope, "near-distinct", "b", f"{CANARY_VERSION} near but distinct beta fact for {scope}."),
            _row(scope, "supersession", "old", f"{CANARY_VERSION} old superseded fact for {scope}.",
                 status="tombstoned", superseded_by=supersession_new),
            _row(scope, "supersession", "new", f"{CANARY_VERSION} current superseding fact for {scope}."),
            _row(scope, "contradiction-keep", "a", f"{CANARY_VERSION} contradiction keep alpha evidence for {scope}."),
            _row(scope, "contradiction-keep", "b", f"{CANARY_VERSION} contradiction keep beta evidence for {scope}."),
            _row(scope, "cross-scope", "out", f"{CANARY_VERSION} cross-scope sentinel from {scope} to {successor}.",
                 peer_scope=successor),
            _row(scope, "cross-scope", "in", f"{CANARY_VERSION} cross-scope sentinel into {scope}.",
                 peer_scope=ordered[(index - 1) % len(ordered)]),
        ])
    return rows


def check_canary_payloads(points: Iterable[dict[str, Any]]) -> list[str]:
    """Return violations; an empty list means every expected canary survived."""
    violations: list[str] = []
    for point in points:
        pid = str(point.get("id") or "unknown")
        payload = point.get("payload") or {}
        if not isinstance(payload, dict):
            violations.append(f"{pid}:missing-payload")
            continue
        expected = str(payload.get("canary_expected_status") or "live")
        actual = str(payload.get("status") or "live")
        if not payload.get("is_canary"):
            violations.append(f"{pid}:is_canary-missing")
        if actual != expected:
            violations.append(f"{pid}:status={actual},expected={expected}")
        if payload.get("canary_case") == "supersession" and payload.get("canary_side") == "old":
            if not payload.get("superseded_by"):
                violations.append(f"{pid}:supersession-link-missing")
    return violations
