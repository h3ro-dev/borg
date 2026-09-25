"""Conservative, read-only selection at both public memory boundaries.

Similarity is not confidence. Semantic searches remain candidate discovery; only
explicit quoted phrases and opaque identifiers require literal evidence. This is
not entity resolution, a corpus-wide absence proof, or an authority promotion.
"""
from __future__ import annotations

import math
import re
from typing import Any

from fastmcp.exceptions import ToolError

SCHEMA = "borg-recall-selection/v1"
# Whole-record rules only: discussion or diagnosis of these strings is retained.
_VACUOUS = frozenset({
    "the tool used is tool.", "the tool used is tool",
    "the tool is tool.", "the tool is tool",
    "the tool is called 'tool'.", 'the tool is called "tool".',
})
_QUOTED = re.compile(r'"([^"\n]+)"')
_TOKEN = re.compile(r"[\w][\w.:-]*", re.UNICODE)
_HEX = re.compile(r"[0-9a-f]{16,}\Z", re.IGNORECASE)
_HEX_PART = re.compile(r"(?:^|[_.:-])[0-9a-f]{8,}(?:$|[_.:-])", re.IGNORECASE)
_UUID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\Z", re.IGNORECASE)


def _normalized(text: str) -> str:
    return " ".join(text.split()).casefold()


def _opaque(token: str) -> bool:
    return bool(_UUID.fullmatch(token) or _HEX.fullmatch(token)
                or (len(token) >= 16 and any(c.isdigit() for c in token) and _HEX_PART.search(token)))


def exact_terms(query: str) -> list[str]:
    """Do not guess identities from ordinary capitalized names or short words."""
    terms = [_normalized(value) for value in _QUOTED.findall(query) if value.strip()]
    for match in _TOKEN.finditer(query):
        token = match.group().rstrip(".:")
        if _opaque(token):
            terms.append(_normalized(token))
    return list(dict.fromkeys(terms))


def _contains(text: str, term: str) -> bool:
    # Literal term with token boundaries: an ID prefix is not that ID, and a
    # surname prefix is not a full surname. No fuzzy identity assertion.
    if _opaque(term):
        # Token equality rejects ID suffixes while allowing sentence punctuation.
        return any(match.group().rstrip(".:") == term for match in _TOKEN.finditer(text))
    return re.search(r"(?<!\w)" + re.escape(term) + r"(?!\w)", text) is not None


def select_memories(query: str, rows: Any, limit: int) -> tuple[list[dict], dict]:
    if not isinstance(rows, list):
        raise ToolError("BORG upstream response has an unexpected shape")
    requested = max(1, min(int(limit), 12))
    terms = exact_terms(query)
    omitted = {"malformed": 0, "vacuous": 0, "invalid_score": 0, "exact_mismatch": 0}
    eligible = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("memory"), str) or not row["memory"].strip():
            omitted["malformed"] += 1
            continue
        text = _normalized(row["memory"])
        if text in _VACUOUS:
            omitted["vacuous"] += 1
            continue
        score = row.get("score")
        # Missing scores are legacy-compatible; no uncalibrated numeric cutoff.
        try:
            valid_score = score is None or (not isinstance(score, bool)
                and isinstance(score, (int, float)) and math.isfinite(score))
        except OverflowError:
            valid_score = False
        if not valid_score:
            omitted["invalid_score"] += 1
            continue
        native_id = _normalized(row["id"]) if isinstance(row.get("id"), str) else None
        if terms and not all(_contains(text, term) or (_opaque(term) and term == native_id)
                             for term in terms):
            omitted["exact_mismatch"] += 1
            continue
        eligible.append(row)
    selected = eligible[:requested]
    details = {
        "schema": SCHEMA,
        "status": "candidates" if selected else "sparse",
        "match_policy": "literal_terms" if terms else "semantic_candidates",
        "exact_term_count": len(terms),
        "candidate_count": len(rows),
        "eligible_count": len(eligible),
        "returned_count": len(selected),
        "omitted": omitted,
        "limit_omitted": max(0, len(eligible) - len(selected)),
        "coverage": "upstream_candidate_pool_only",
        "notice": "Similarity is not confidence. Sparse results do not prove absence from the corpus; no authority is inferred.",
    }
    return selected, {"result_floor": "sparse" if len(selected) < requested else "populated",
                      "retrieval": details}


def unavailable_retrieval() -> dict:
    """A failed upstream is never disguised as a successful empty search."""
    return {"result_floor": "unavailable", "retrieval": {
        "schema": SCHEMA, "status": "unavailable", "candidate_count": None,
        "returned_count": 0, "coverage": "unavailable",
    }}
