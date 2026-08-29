#!/usr/bin/env python3
"""Backfill slice: newest organic mem0 facts -> Graphiti temporal graph.

- Pulls organic facts (run_id != corpus-seed) from qdrant via REST.
- Groups facts by run_id (same thread/conversation = one episode), newest first.
- reference_time = the fact's thread_date (real-world validity anchor).
- LLM: qwen3.8:27b GGUF through the schema-shim (grammar + thinking ON).
- Harvests every (episode -> nodes/edges) result to data/training-pairs.jsonl
  as future fine-tune data.
- Resumable (data/backfill-state.json), locked, logged.

Usage: backfill.py [--limit 500]
"""
import asyncio
import argparse
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

from graphiti_core import Graphiti
from graphiti_core.driver.falkordb_driver import FalkorDriver
from graphiti_core.llm_client import LLMConfig
from graphiti_core.llm_client.openai_generic_client import OpenAIGenericClient
from graphiti_core.embedder.openai import OpenAIEmbedder, OpenAIEmbedderConfig
from graphiti_core.cross_encoder.openai_reranker_client import OpenAIRerankerClient
from graphiti_core.nodes import EpisodeType

BASE = Path(__file__).resolve().parent
SHIM = os.environ.get("GRAPH_LLM_URL", "http://127.0.0.1:11500/v1")
MODEL = os.environ.get("GRAPH_LLM_MODEL", "qwen3.8:27b")
QDRANT = "http://127.0.0.1:6333"
STATE_F = BASE / "data/backfill-state.json"
PAIRS_F = BASE / "data/training-pairs.jsonl"
LOG_F = BASE / "data/backfill.log"
LOCK_F = BASE / "data/backfill.lock"
GROUP_ID = "backfill-v1"


def log(msg):
    line = f"{datetime.now().strftime('%Y-%m-%d %H:%M')} {msg}"
    with open(LOG_F, "a") as f:
        f.write(line + "\n")
    print(line, flush=True)


def fetch_organic_facts(cap=4000):
    rows, offset = [], None
    with httpx.Client(timeout=30) as c:
        while len(rows) < cap:
            body = {
                "limit": 500,
                "with_payload": True,
                "with_vector": False,
                "filter": {"must_not": [{"key": "run_id", "match": {"value": "corpus-seed"}}]},
            }
            if offset:
                body["offset"] = offset
            r = c.post(f"{QDRANT}/collections/studio0/points/scroll", json=body).json()["result"]
            for p in r["points"]:
                pay = p.get("payload") or {}
                text = pay.get("data") or pay.get("memory") or ""
                if text and pay.get("user_id") == "james":
                    rows.append({
                        "text": text,
                        "run_id": pay.get("run_id") or "solo-" + str(p["id"])[:8],
                        "kind": pay.get("kind") or pay.get("agent_id") or "fact",
                        "date": (pay.get("thread_date") or pay.get("added_mdt") or "")[:10],
                    })
            offset = r.get("next_page_offset")
            if not offset:
                break
    return rows


def ref_time(date_s):
    try:
        return datetime.strptime(date_s, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except Exception:
        return datetime.now(timezone.utc)


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=500, help="max facts in this slice")
    ap.add_argument("--fetch", type=int, default=4000, help="how many organic facts to pull from qdrant before grouping")
    args = ap.parse_args()

    if LOCK_F.exists():
        try:
            os.kill(int(LOCK_F.read_text()), 0)
            print("already running")
            return
        except (ProcessLookupError, ValueError):
            pass
    LOCK_F.write_text(str(os.getpid()))
    import atexit
    atexit.register(lambda: LOCK_F.unlink(missing_ok=True))

    facts = fetch_organic_facts(cap=args.fetch)
    groups = {}
    for f in facts:
        groups.setdefault(f["run_id"], []).append(f)
    ordered = sorted(groups.items(), key=lambda kv: max(x["date"] for x in kv[1]), reverse=True)

    st = json.loads(STATE_F.read_text()) if STATE_F.exists() else {"done": {}}
    todo, count = [], 0
    for rid, fs in ordered:
        if rid in st["done"] or count >= args.limit:
            continue
        todo.append((rid, fs))
        count += len(fs)
    log(f"RUN-START organic_facts={len(facts)} groups={len(groups)} slice_episodes={len(todo)} slice_facts={count}")

    llm = OpenAIGenericClient(config=LLMConfig(api_key="ollama", model=MODEL, small_model=MODEL, base_url=SHIM))
    embedder = OpenAIEmbedder(config=OpenAIEmbedderConfig(embedding_model="nomic-embed-text:latest", embedding_dim=768, api_key="ollama", base_url=SHIM))
    reranker = OpenAIRerankerClient(config=LLMConfig(api_key="ollama", model=MODEL, base_url=SHIM), client=llm)
    g = Graphiti(graph_driver=FalkorDriver(host="127.0.0.1", port=6383), llm_client=llm, embedder=embedder, cross_encoder=reranker)
    await g.build_indices_and_constraints()

    ok = fail = 0
    for i, (rid, fs) in enumerate(todo, 1):
        body = "\n".join(f["text"] for f in fs[:12])
        date = max(f["date"] for f in fs)
        t0 = time.time()
        try:
            r = await g.add_episode(
                name=rid, episode_body=body[:6000],
                source_description=fs[0]["kind"], source=EpisodeType.text,
                reference_time=ref_time(date), group_id=GROUP_ID,
            )
            nodes = [getattr(n, "name", "?") for n in (getattr(r, "nodes", []) or [])]
            edges = [getattr(e, "fact", "?") for e in (getattr(r, "edges", []) or [])]
            with open(PAIRS_F, "a") as pf:
                pf.write(json.dumps({"input": body, "date": date, "kind": fs[0]["kind"], "nodes": nodes, "edges": edges, "model": MODEL}) + "\n")
            st["done"][rid] = {"nodes": len(nodes), "edges": len(edges)}
            ok += 1
            log(f"[{i}/{len(todo)}] ok {time.time()-t0:.0f}s nodes={len(nodes)} edges={len(edges)} {rid[:40]}")
        except Exception as e:
            st["done"][rid] = {"status": f"error:{type(e).__name__}"}
            fail += 1
            log(f"[{i}/{len(todo)}] FAIL {time.time()-t0:.0f}s {type(e).__name__}: {str(e)[:100]} {rid[:40]}")
        STATE_F.write_text(json.dumps(st))
    log(f"RUN-END ok={ok} fail={fail}")
    await g.close()


if __name__ == "__main__":
    asyncio.run(main())
