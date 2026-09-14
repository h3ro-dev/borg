#!/usr/bin/env python3
"""Build the student-v3 corpus from graphiti's OWN internal calls.

WHY THIS EXISTS
---------------
Prior isolated evaluation showed that training one extraction call shape did
not generalize to Graphiti's other internal calls. In particular, a model can
emit valid JSON with the wrong schema keys, silently producing no graph data.

The rule that broke: **a student is only a drop-in replacement for the callsite
it was trained on.** So this builder does not invent a prompt. It reads the
(request, response) pairs the live shim now tees to data/shim-pairs.jsonl,
which are Graphiti's real callsites and their explicitly supplied answers.

WHAT IT PRODUCES
----------------
mlx-lm messages rows, one file per call shape plus a combined file, with the
system + user turns exactly as graphiti sent them and the assistant turn the
teacher's answer re-serialized canonically. Train order from the canary:

  1. dedupe_edges.resolve_edge      -> EdgeDuplicate      (the blocker)
  2. dedupe_nodes.nodes             -> NodeResolutions
  3. extract_edges.edge             -> ExtractedEdges
  4. extract_edges.extract_timestamps / extract_nodes.extract_summaries_batch
     (never fairly tested against the student — assume untested, not working)

MIXING
------
This corpus may be mixed with an explicitly supplied extraction corpus at a
ratio the new owner chooses. Nothing is mixed by default. `--emit-mixed
--mix-with PRIVATE_CORPUS --mix-ratio N:M` writes the mixed file and reports what
went in; the report always prints the per-shape counts you need to choose N:M.

Note the format difference the mix has to live with: the v1 corpus rows are
user+assistant (a single hand-written prompt, no system turn); these rows are
system+user+assistant, because graphiti's system turn is part of the callsite
and dropping it would train the student on a prompt it will never see.

THE GATE THAT MATTERS
---------------------
graphiti does not validate a response against the schema it asked for — it
only json.loads it, then reads the key it wanted. A well-formed JSON object
with the wrong keys is not an error anywhere in the stack; it silently becomes
"nothing found". That is exactly how the v2 student failed while scoring
"100% valid JSON" on its exam. So valid JSON is not the bar here: every kept
row must carry the required top-level keys of the schema the call declared.

Usage (all data sources are explicit; nothing is written until --out):
    build_v3_dataset.py --pairs PRIVATE_PAIRS --report-only
    build_v3_dataset.py --pairs PRIVATE_PAIRS --out PRIVATE_OUTPUT
"""
import argparse
import hashlib
import json
import random
import statistics
from collections import Counter, defaultdict
from pathlib import Path

# Required top-level keys per declared schema, read off graphiti_core 0.29.3's
# pydantic models. A response missing any of these is shape-wrong, however
# valid its JSON — that is the exact failure the canary caught.
REQUIRED_KEYS = {
    "EdgeDuplicate": ("duplicate_facts", "contradicted_facts"),
    "NodeResolutions": ("entity_resolutions",),
    "NodeDuplicate": ("id", "name", "duplicate_candidate_id"),
    "ExtractedEdges": ("edges",),
    "ExtractedEntities": ("extracted_entities",),
    "SummarizedEntities": ("summaries",),
    "EntitySummary": ("summary",),
    "Summary": ("summary",),
    "SummaryDescription": ("description",),
    "BatchEdgeTimestamps": ("timestamps",),
    "CombinedExtraction": ("extracted_entities", "edges"),
    # EdgeTimestamps has no required field: {} is a legitimate answer
    # ("no dates found"), so its gate is only "is a JSON object".
    "EdgeTimestamps": (),
}

# Canary priority. Shapes outside this list are still built; they just are not
# what v3 is being trained to fix.
PRIORITY = [
    "dedupe_edges.resolve_edge",
    "dedupe_nodes.nodes",
    "extract_edges.edge",
    "extract_edges.extract_timestamps",
    "extract_nodes.extract_summaries_batch",
    "extract_nodes.extract_text",
]

DROP_REASONS = ("not_ok", "truncated", "empty_response", "json_invalid",
                "not_object", "shape_wrong", "too_long", "duplicate",
                "no_user_turn")


def strip_code_fences(s):
    """Same tolerance graphiti's own client applies before json.loads."""
    s = s.strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[-1] if "\n" in s else s[3:]
        if s.rstrip().endswith("```"):
            s = s.rstrip()[:-3]
    return s.strip()


def load_pairs(path):
    with open(path, encoding="utf-8") as fh:
        for n, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                yield {"_unparsable_line": n}


def build(args):
    kept = defaultdict(list)
    drops = defaultdict(Counter)
    chars = defaultdict(list)
    seen = set()
    read = bad_lines = 0

    for rec in load_pairs(args.pairs):
        if "_unparsable_line" in rec:
            bad_lines += 1
            continue
        read += 1
        ct = rec.get("call_type", "unknown")
        d = drops[ct]

        if not rec.get("ok"):
            d["not_ok"] += 1
            continue
        if rec.get("truncated"):
            # The 64KB cap mangled this prompt or answer; it is not a clean
            # training target even though the live call succeeded.
            d["truncated"] += 1
            continue
        raw = rec.get("response") or ""
        if not raw.strip():
            d["empty_response"] += 1
            continue
        try:
            parsed = json.loads(strip_code_fences(raw))
        except json.JSONDecodeError:
            d["json_invalid"] += 1
            continue
        if not isinstance(parsed, dict):
            d["not_object"] += 1
            continue
        if not args.no_shape_gate:
            need = REQUIRED_KEYS.get(rec.get("schema"))
            if need is not None and not all(k in parsed for k in need):
                d["shape_wrong"] += 1
                continue

        msgs = []
        for m in rec.get("messages") or []:
            role, content = m.get("role"), m.get("content")
            if role in ("system", "user") and isinstance(content, str):
                msgs.append({"role": role, "content": content})
        if not any(m["role"] == "user" for m in msgs):
            d["no_user_turn"] += 1
            continue

        target = json.dumps(parsed, ensure_ascii=False)
        size = sum(len(m["content"]) for m in msgs) + len(target)
        if args.max_chars and size > args.max_chars:
            d["too_long"] += 1
            continue

        key = hashlib.sha256(
            (ct + "\x00" + "\x00".join(m["content"] for m in msgs)
             ).encode("utf-8")).hexdigest()
        if key in seen:
            d["duplicate"] += 1
            continue
        seen.add(key)

        kept[ct].append({"messages": msgs +
                         [{"role": "assistant", "content": target}]})
        chars[ct].append(size)

    return kept, drops, chars, read, bad_lines


def split_rows(kept, args):
    """Stratified split: every shape is represented in valid and test, so v3
    is graded per shape instead of on one blended number."""
    rng = random.Random(args.seed)
    parts = {"train": [], "valid": [], "test": []}
    per_shape = {}
    for ct, rows in kept.items():
        rows = list(rows)
        rng.shuffle(rows)
        n = len(rows)
        n_test = min(args.holdout, max(0, n // 10))
        n_valid = min(args.holdout, max(0, (n - n_test) // 10))
        parts["test"] += rows[:n_test]
        parts["valid"] += rows[n_test:n_test + n_valid]
        parts["train"] += rows[n_test + n_valid:]
        per_shape[ct] = {"total": n, "train": n - n_test - n_valid,
                         "valid": n_valid, "test": n_test}
    for p in parts.values():
        rng.shuffle(p)
    return parts, per_shape


def read_v1(path, limit=None):
    rows = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(line)
            if limit and len(rows) >= limit:
                break
    return rows


def write_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for r in rows:
            fh.write((r if isinstance(r, str)
                      else json.dumps(r, ensure_ascii=False)) + "\n")
    return path


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--pairs", type=Path, required=True,
                    help="explicit approved request/response pair source")
    ap.add_argument("--out", type=Path, default=None,
                    help="explicit private output directory; nothing is written without it")
    ap.add_argument("--report-only", action="store_true")
    ap.add_argument("--max-chars", type=int, default=0,
                    help="prompt+target ceiling; 0 = no cap. The report prints "
                         "per-shape size percentiles so you can pick one that "
                         "does not silently delete a whole shape.")
    ap.add_argument("--holdout", type=int, default=100,
                    help="max valid/test rows per shape")
    ap.add_argument("--no-shape-gate", action="store_true",
                    help="keep rows whose JSON lacks the schema's required "
                         "keys — off by default, and turning it on rebuilds "
                         "the exact blind spot the canary found")
    ap.add_argument("--emit-mixed", action="store_true",
                    help="also write mixed-train.jsonl")
    ap.add_argument("--mix-with", type=Path, default=None,
                    help="explicit existing corpus; required with --emit-mixed")
    ap.add_argument("--mix-ratio", default="1:1",
                    help="new:existing, by rows, e.g. 1:2")
    ap.add_argument("--seed", type=int, default=13)
    args = ap.parse_args()

    if args.emit_mixed and args.mix_with is None:
        ap.error("--mix-with is required with --emit-mixed")

    if not args.pairs.exists():
        raise SystemExit(f"no pairs file at {args.pairs}")

    kept, drops, chars, read, bad_lines = build(args)
    parts, per_shape = split_rows(kept, args)

    shapes = {}
    for ct in sorted(set(list(kept) + list(drops)),
                     key=lambda c: (PRIORITY.index(c) if c in PRIORITY else 99,
                                    c)):
        sizes = sorted(chars.get(ct, []))
        shapes[ct] = {
            "kept": len(kept.get(ct, [])),
            "dropped": {k: v for k, v in sorted(drops[ct].items()) if v},
            "split": per_shape.get(ct, {}),
            "canary_priority": (PRIORITY.index(ct) + 1) if ct in PRIORITY
            else None,
            "chars": {"p50": int(statistics.median(sizes)),
                      "p95": sizes[int(len(sizes) * 0.95) - 1],
                      "max": sizes[-1]} if sizes else {},
        }

    report = {
        "pairs_file": str(args.pairs),
        "records_read": read,
        "unparsable_lines": bad_lines,
        "rows_kept": sum(len(v) for v in kept.values()),
        "shape_gate": not args.no_shape_gate,
        "max_chars": args.max_chars or None,
        "split_totals": {k: len(v) for k, v in parts.items()},
        "per_shape": shapes,
    }

    if args.out and not args.report_only:
        out = args.out
        for ct, rows in kept.items():
            write_jsonl(out / "by_shape" / (ct.replace(".", "_") + ".jsonl"),
                        rows)
        for name, rows in parts.items():
            write_jsonl(out / f"{name}.jsonl", rows)
        report["written"] = str(out)

        if args.emit_mixed:
            n_new, n_old = (int(x) for x in args.mix_ratio.split(":"))
            new_rows = parts["train"]
            want_old = int(len(new_rows) * n_old / max(1, n_new))
            old_rows = read_v1(args.mix_with)
            rng = random.Random(args.seed)
            rng.shuffle(old_rows)
            old_rows = old_rows[:want_old]
            mixed = [json.dumps(r, ensure_ascii=False) for r in new_rows] + \
                old_rows
            rng.shuffle(mixed)
            write_jsonl(out / "mixed-train.jsonl", mixed)
            report["mixed"] = {"ratio": args.mix_ratio,
                               "new_rows": len(new_rows),
                               "existing_rows": len(old_rows),
                               "existing_source": str(args.mix_with),
                               "total": len(mixed)}
        (out / "BUILD-REPORT.json").write_text(
            json.dumps(report, indent=2) + "\n")

    print(json.dumps(report, indent=2))
    print()
    print(f"{'shape':44} {'kept':>6} {'train':>6} {'val':>5} {'test':>5}  "
          f"dropped")
    for ct, s in shapes.items():
        sp = s["split"]
        star = "*" if s["canary_priority"] else " "
        print(f"{star}{ct:43} {s['kept']:6} {sp.get('train', 0):6} "
              f"{sp.get('valid', 0):5} {sp.get('test', 0):5}  "
              f"{s['dropped'] or ''}")
    print("\n* = a shape the canary named for v3, in that order. "
          "Nothing else matters until dedupe_edges.resolve_edge is fixed.")
    if not args.out:
        print("\n(report only — pass --out to write files)")


if __name__ == "__main__":
    main()
