#!/usr/bin/env python3
"""Pick a dense configured episode set for two isolated canary arms.

The source, owner, prior selections, feed state, and destination are explicit.
Selection favors dense inputs so internal graph call shapes are exercised;
ingest order remains date-desc so both arms see identical accumulated state.
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pick_episodes import fetch  # noqa: E402

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-n", type=int, default=25)
    ap.add_argument("--min-chars", type=int, default=900)
    ap.add_argument("--skip", type=int, default=0)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--qdrant-url", required=True)
    ap.add_argument("--collection", required=True)
    ap.add_argument("--owner-id", required=True)
    ap.add_argument("--state-file", type=Path, required=True,
                    help="explicit private graph feed state")
    ap.add_argument("--prior-canary-file", type=Path, default=None,
                    help="optional explicit prior selection to exclude")
    args = ap.parse_args()

    rows = fetch(args.qdrant_url, args.collection, args.owner_id)
    groups = {}
    for f in rows:
        groups.setdefault(f["run_id"], []).append(f)
    ordered = sorted(groups.items(), key=lambda kv: max(x["date"] for x in kv[1]), reverse=True)

    st = json.loads(args.state_file.read_text()) if args.state_file.exists() else {"done": {}}
    done = set(st.get("done") or {})
    old = (
        {e["run_id"] for e in json.loads(args.prior_canary_file.read_text())}
        if args.prior_canary_file is not None and args.prior_canary_file.exists()
        else set()
    )

    pool = []
    for rid, fs in ordered:
        if rid in done or rid in old:
            continue
        body = "\n".join(f["text"] for f in fs[:12])[:6000]
        pool.append({"run_id": rid, "body": body, "kind": fs[0]["kind"],
                     "date": max(f["date"] for f in fs), "facts": len(fs),
                     "chars": len(body)})
    seen_sizes = [e["chars"] for e in pool]
    dense = [e for e in pool if e["chars"] >= args.min_chars]
    dense.sort(key=lambda e: e["chars"], reverse=True)
    out = dense[args.skip:args.skip + args.n]
    # ingest order = backfill.py's own order (newest thread date first)
    out.sort(key=lambda e: e["date"], reverse=True)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(out, indent=1) + "\n", encoding="utf-8")
    chars = sorted(e["chars"] for e in out)
    print(f"{len(out)} episodes -> {args.out}")
    print(f"chars min={chars[0]} median={chars[len(chars)//2]} max={chars[-1]} "
          f"mean={sum(chars)//len(chars)}")
    print(f"eligible pool (not done, not old-canary): {len(seen_sizes)}  "
          f">= {args.min_chars} chars: {len([c for c in seen_sizes if c >= args.min_chars])}")
    for e in out:
        print(f"  {e['run_id'][:46]:48} facts={e['facts']:3} chars={e['chars']:5} {e['date']} {e['kind'][:18]}")


if __name__ == "__main__":
    main()
