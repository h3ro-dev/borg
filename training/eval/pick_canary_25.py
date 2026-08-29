#!/usr/bin/env python3
"""Pick the 25 REAL backlog episodes used by BOTH canary arms. READ-ONLY.

Reuses ox-pilot/pick_episodes.fetch() (qdrant scroll, same body construction
backfill.py uses) and skips anything already in data/backfill-state.json or in
the old ox-pilot canary set. Writes ONLY into training/eval/.

Selection = the 25 DENSEST remaining episodes. Measured 2026-08-28, the
un-processed pool is 1,794 episodes with a median body of 210 chars; graphiti
resolves those stubs to nodes=0 without ever issuing a dedup or an edge call,
so an unfiltered slice would not exercise the internal call shapes this canary
exists to test. Ingest order is date-desc (backfill.py's own order) so both
arms accumulate identical group state before each dedup call.
"""
import os
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, os.path.expanduser("~/Library/Memory/graphiti/ox-pilot"))
from pick_episodes import fetch  # noqa: E402

BASE = Path(__file__).resolve().parent
GRAPHITI = BASE.parent.parent
STATE_F = GRAPHITI / "data/backfill-state.json"
OXCANARY = GRAPHITI / "ox-pilot/canary-episodes.json"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-n", type=int, default=25)
    ap.add_argument("--min-chars", type=int, default=900)
    ap.add_argument("--skip", type=int, default=0)
    ap.add_argument("--out", default="canary-episodes-25-20260828.json")
    args = ap.parse_args()

    rows = fetch()
    groups = {}
    for f in rows:
        groups.setdefault(f["run_id"], []).append(f)
    ordered = sorted(groups.items(), key=lambda kv: max(x["date"] for x in kv[1]), reverse=True)

    st = json.loads(STATE_F.read_text()) if STATE_F.exists() else {"done": {}}
    done = set(st.get("done") or {})
    old = {e["run_id"] for e in json.loads(OXCANARY.read_text())} if OXCANARY.exists() else set()

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

    (BASE / args.out).write_text(json.dumps(out, indent=1))
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
