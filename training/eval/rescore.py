#!/usr/bin/env python3
"""Rescore saved raw outputs against the test set (no model needed).

Same parsing and metrics as exam.py, minus generation. Used to salvage a
partial exam's raws and to merge segment summaries.
"""
import argparse
import json
import re
from pathlib import Path


def entity_names(obj):
    try:
        return {str(e.get("name", "")).strip().lower() for e in obj.get("entities", []) if e.get("name")}
    except AttributeError:
        return set()


def parse_json_lenient(text):
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.S)
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start:i + 1])
                except json.JSONDecodeError:
                    return None
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--raw-dir", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    rows = [json.loads(l) for l in open(args.data) if l.strip()]
    valid = 0
    n = 0
    jaccards, rel_ratios, dated_fracs, failures = [], [], [], []
    for f in sorted(Path(args.raw_dir).glob("*.txt")):
        i = int(f.stem)
        target = json.loads(rows[i]["messages"][1]["content"])
        out = f.read_text()
        n += 1
        pred = parse_json_lenient(out)
        if pred is None:
            failures.append(i)
            continue
        valid += 1
        te, pe = entity_names(target), entity_names(pred)
        union = te | pe
        jaccards.append(len(te & pe) / len(union) if union else 1.0)
        t_rels = target.get("relations") or []
        p_rels = pred.get("relations") if isinstance(pred.get("relations"), list) else []
        rel_ratios.append(len(p_rels) / len(t_rels) if t_rels else 1.0)
        if p_rels:
            dated_fracs.append(sum(1 for r in p_rels if isinstance(r, dict) and r.get("date")) / len(p_rels))

    summary = {
        "pairs": n,
        "json_valid": valid,
        "json_valid_pct": round(100.0 * valid / n, 2) if n else None,
        "entity_jaccard_mean": round(sum(jaccards) / len(jaccards), 4) if jaccards else None,
        "relation_count_ratio_mean": round(sum(rel_ratios) / len(rel_ratios), 3) if rel_ratios else None,
        "dated_fraction_mean": round(sum(dated_fracs) / len(dated_fracs), 3) if dated_fracs else None,
        "parse_failures_idx": failures[:20],
        "raw_dir": args.raw_dir,
    }
    Path(args.out).write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
