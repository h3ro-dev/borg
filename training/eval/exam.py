#!/usr/bin/env python3
"""Grade a student checkpoint against held-out teacher pairs.

Metrics per pair and aggregate: JSON validity, entity-name Jaccard vs the
teacher, relation-count ratio, dated-relation fraction, and serving speed
(tokens/sec, wall-clock around generate). Greedy decoding.
"""
import argparse
import json
import re
import time
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
    ap.add_argument("--model", required=True)
    ap.add_argument("--adapter", default=None)
    ap.add_argument("--data", required=True)
    ap.add_argument("--limit", type=int, default=100)
    ap.add_argument("--offset", type=int, default=0)
    ap.add_argument("--max-tokens", type=int, default=1600)
    ap.add_argument("--out", required=True)
    ap.add_argument("--save-raw", default=None, help="directory to write per-pair raw outputs")
    args = ap.parse_args()
    raw_dir = Path(args.save_raw) if args.save_raw else None
    if raw_dir:
        raw_dir.mkdir(parents=True, exist_ok=True)

    from mlx_lm import load, generate
    model, tokenizer = load(args.model, adapter_path=args.adapter)

    rows = []
    with open(args.data) as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    rows = rows[args.offset : args.offset + args.limit]

    n = len(rows)
    valid = 0
    jaccards = []
    rel_ratios = []
    dated_fracs = []
    gen_tokens = 0
    gen_seconds = 0.0
    failures = []

    for i, row in enumerate(rows):
        user = row["messages"][0]["content"]
        target = json.loads(row["messages"][1]["content"])
        prompt = tokenizer.apply_chat_template(
            [{"role": "user", "content": user}], add_generation_prompt=True, tokenize=False
        )
        t0 = time.monotonic()
        out = generate(model, tokenizer, prompt=prompt, max_tokens=args.max_tokens, verbose=False)
        dt = time.monotonic() - t0
        toks = len(tokenizer.encode(out))
        gen_tokens += toks
        gen_seconds += dt
        if raw_dir:
            (raw_dir / f"{i + args.offset:04d}.txt").write_text(out)

        pred = parse_json_lenient(out)
        strict_ok = False
        try:
            json.loads(out.strip())
            strict_ok = True
        except (json.JSONDecodeError, ValueError):
            pass
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
        if i % 10 == 0:
            print(f"[{i+1}/{n}] valid={valid} mean_jaccard={sum(jaccards)/len(jaccards):.3f} tok/s={gen_tokens/max(gen_seconds,1e-9):.0f}", flush=True)
        _ = strict_ok

    summary = {
        "pairs": n,
        "json_valid": valid,
        "json_valid_pct": round(100.0 * valid / n, 2) if n else None,
        "entity_jaccard_mean": round(sum(jaccards) / len(jaccards), 4) if jaccards else None,
        "entity_jaccard_p10": round(sorted(jaccards)[max(0, len(jaccards) // 10 - 1)], 4) if jaccards else None,
        "relation_count_ratio_mean": round(sum(rel_ratios) / len(rel_ratios), 3) if rel_ratios else None,
        "dated_fraction_mean": round(sum(dated_fracs) / len(dated_fracs), 3) if dated_fracs else None,
        "serving_tokens_per_sec": round(gen_tokens / gen_seconds, 1) if gen_seconds else None,
        "seconds_per_item_mean": round(gen_seconds / n, 2) if n else None,
        "parse_failures_idx": failures[:20],
        "model": args.model,
        "adapter": args.adapter,
    }
    Path(args.out).write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
