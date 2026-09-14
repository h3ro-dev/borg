#!/usr/bin/env python3
"""Pick configured canary episodes from an explicitly approved Qdrant source.

The target IDs, owner ID, endpoint, collection, and output are all required;
running this script with no new-owner configuration performs no data access.
"""
import argparse
import json
from pathlib import Path

def fetch(qdrant_url, collection, owner_id, cap=12000):
    import httpx

    rows, offset = [], None
    with httpx.Client(timeout=30) as c:
        while len(rows) < cap:
            body = {"limit": 500, "with_payload": True, "with_vector": False,
                    "filter": {"must_not": [{"key": "run_id", "match": {"value": "corpus-seed"}}]}}
            if offset:
                body["offset"] = offset
            r = c.post(
                f"{qdrant_url.rstrip('/')}/collections/{collection}/points/scroll",
                json=body,
            ).json()["result"]
            for p in r["points"]:
                pay = p.get("payload") or {}
                text = pay.get("data") or pay.get("memory") or ""
                if text and pay.get("user_id") == owner_id:
                    rows.append({"text": text,
                                 "run_id": pay.get("run_id") or "solo-" + str(p["id"])[:8],
                                 "kind": pay.get("kind") or pay.get("agent_id") or "fact",
                                 "date": (pay.get("thread_date") or pay.get("added_mdt") or "")[:10]})
            offset = r.get("next_page_offset")
            if not offset:
                break
    return rows


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--qdrant-url", required=True)
    parser.add_argument("--collection", required=True)
    parser.add_argument("--owner-id", required=True)
    parser.add_argument("--targets", type=Path, required=True,
                        help="private JSON object mapping run IDs to [seconds,nodes,edges]")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--cap", type=int, default=12000)
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    targets = json.loads(args.targets.read_text(encoding="utf-8"))
    if not isinstance(targets, dict):
        raise SystemExit("targets must be a JSON object")
    rows = fetch(args.qdrant_url, args.collection, args.owner_id, args.cap)
    groups = {}
    for f in rows:
        groups.setdefault(f["run_id"], []).append(f)
    out = []
    for rid, metrics in targets.items():
        if not isinstance(rid, str) or not isinstance(metrics, list) or len(metrics) != 3:
            raise SystemExit("each target must map a run ID to [seconds,nodes,edges]")
        qsec, qn, qe = metrics
        fs = groups.get(rid)
        if not fs:
            print(f"MISSING {rid}")
            continue
        # identical body construction to backfill.py
        body = "\n".join(f["text"] for f in fs[:12])[:6000]
        out.append({"run_id": rid, "body": body, "kind": fs[0]["kind"],
                    "date": max(f["date"] for f in fs), "facts": len(fs),
                    "chars": len(body),
                    "qwen": {"sec": qsec, "nodes": qn, "edges": qe}})
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(out, indent=1) + "\n", encoding="utf-8")
    for e in out:
        print(f"{e['run_id'][:44]:46} facts={e['facts']:2} chars={e['chars']:5} "
              f"date={e['date']} qwen={e['qwen']['sec']}s n={e['qwen']['nodes']} e={e['qwen']['edges']}")


if __name__ == "__main__":
    main()
