#!/usr/bin/env python3
"""Grade the capture student against held-out luna-taught pairs.

Target shape: {"candidates":[{"fact","kind","support"},...]} — gate survivors.
Metrics: JSON validity, fact-level Jaccard vs target (normalized strings),
candidate-count ratio, kind distribution match, support-ref presence, speed.
"""
import argparse
import json
import re
import time
from pathlib import Path


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


def norm_fact(s):
    return re.sub(r"\s+", " ", str(s).strip().lower().rstrip("."))


def facts_of(obj):
    if not isinstance(obj, dict):
        return set(), []
    cands = obj.get("candidates")
    if not isinstance(cands, list):
        return set(), []
    ok = [c for c in cands if isinstance(c, dict) and c.get("fact")]
    return {norm_fact(c["fact"]) for c in ok}, ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--adapter", required=True)
    ap.add_argument("--data", required=True)
    ap.add_argument("--limit", type=int, default=300)
    ap.add_argument("--offset", type=int, default=0)
    ap.add_argument("--max-tokens", type=int, default=1400)
    ap.add_argument("--out", required=True)
    ap.add_argument("--save-raw", default=None)
    args = ap.parse_args()
    raw_dir = Path(args.save_raw) if args.save_raw else None
    if raw_dir:
        raw_dir.mkdir(parents=True, exist_ok=True)

    from mlx_lm import load, generate
    model, tokenizer = load(args.model, adapter_path=args.adapter)

    rows = [json.loads(l) for l in open(args.data) if l.strip()]
    rows = rows[args.offset:args.offset + args.limit]
    n = len(rows)
    valid = 0
    jaccards, count_ratios, support_fracs = [], [], []
    gen_tokens = 0
    gen_seconds = 0.0
    failures = []

    for i, row in enumerate(rows):
        prompt = tokenizer.apply_chat_template(
            [{"role": "user", "content": row["messages"][0]["content"]}],
            add_generation_prompt=True, tokenize=False)
        t0 = time.monotonic()
        out = generate(model, tokenizer, prompt=prompt, max_tokens=args.max_tokens, verbose=False)
        dt = time.monotonic() - t0
        gen_tokens += len(tokenizer.encode(out))
        gen_seconds += dt
        if raw_dir:
            (raw_dir / f"{i + args.offset:04d}.txt").write_text(out)

        target = json.loads(row["messages"][1]["content"])
        t_facts, t_cands = facts_of(target)
        pred = parse_json_lenient(out)
        p_facts, p_cands = facts_of(pred) if pred else (set(), [])
        if pred is None or not isinstance(pred.get("candidates"), list):
            failures.append(i + args.offset)
            continue
        valid += 1
        union = t_facts | p_facts
        jaccards.append(len(t_facts & p_facts) / len(union) if union else 1.0)
        count_ratios.append(len(p_cands) / len(t_cands) if t_cands else 1.0)
        if p_cands:
            support_fracs.append(sum(1 for c in p_cands if c.get("support")) / len(p_cands))
        if i % 20 == 0:
            print(f"[{i+1}/{n}] valid={valid} jacc={sum(jaccards)/max(len(jaccards),1):.3f} tok/s={gen_tokens/max(gen_seconds,1e-9):.0f}", flush=True)

    summary = {
        "pairs": n,
        "json_valid": valid,
        "json_valid_pct": round(100.0 * valid / n, 2) if n else None,
        "fact_jaccard_mean": round(sum(jaccards) / len(jaccards), 4) if jaccards else None,
        "fact_jaccard_p10": round(sorted(jaccards)[max(0, len(jaccards) // 10 - 1)], 4) if jaccards else None,
        "candidate_count_ratio_mean": round(sum(count_ratios) / len(count_ratios), 3) if count_ratios else None,
        "support_ref_fraction_mean": round(sum(support_fracs) / len(support_fracs), 3) if support_fracs else None,
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
