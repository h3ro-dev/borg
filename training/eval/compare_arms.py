#!/usr/bin/env python3
"""Structural comparison of the two canary groups + the promotion evidence pack.

ox-pilot/compare_quality.py compares a pilot group against backfill-v1. This
canary has TWO fresh isolated groups, so the same idea is re-pointed: per
episode, the Entity names each arm's group holds via MENTIONS, plus the graph
totals, duplicate-name rate and per-call-type LLM outcomes.

READ-ONLY on FalkorDB. Writes only training/eval/CANARY-2026-08-28.{md,json}.
"""
import json
import subprocess
from collections import Counter
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent
PORT = "6383"
A = "canary-27b-20260828"
B = "canary-student-20260828"


def rq(graph, cypher):
    out = subprocess.run(["redis-cli", "-p", PORT, "GRAPH.RO_QUERY", graph, cypher],
                         capture_output=True, text=True).stdout.splitlines()
    return [l.strip() for l in out[1:-2] if l.strip()]


def ep_nodes(graph, rid):
    safe = rid.replace("'", "\\'")
    return rq(graph, f"MATCH (e:Episodic)-[:MENTIONS]->(n:Entity) "
                     f"WHERE e.name = '{safe}' RETURN n.name")


def load(stem):
    return json.loads((BASE / f"{stem}.json").read_text())


# Every place graphiti_core 0.29.3 accepts the JSON but THROWS PART OF IT AWAY.
# These are schema-clean responses that are still semantically wrong, so they
# never show up in a format-validity score — only in the runtime log.
REJECTIONS = {
    "edge_dedup_idx_out_of_range": "LLM returned invalid duplicate_facts idx values",
    "edge_target_entity_hallucinated": "Target entity not found in nodes for edge relation",
    "edge_endpoint_missing": "Could not find source or target node for extracted edge",
    "node_dedup_duplicate_id": "Duplicate LLM dedupe id",
    "node_dedup_missing_resolutions": "LLM did not return resolutions for IDs",
    "bad_valid_at_date": "Error parsing valid_at date",
    "bad_invalid_at_date": "Error parsing invalid_at date",
    "timestamp_extraction_failed": "Failed to extract timestamps for edge",
}


def rejections(nohup_name):
    p = BASE / nohup_name
    text = p.read_text(errors="ignore") if p.exists() else ""
    out = {k: text.count(v) for k, v in REJECTIONS.items()}
    out["total"] = sum(out.values())
    return out


def dup_stats(graph):
    names = [n.strip() for n in rq(graph, "MATCH (n:Entity) RETURN n.name") if n.strip()]
    low = Counter(n.lower() for n in names)
    dupes = {k: v for k, v in low.items() if v > 1}
    return {"total_nodes": len(names), "distinct_names": len(low),
            "dup_name_groups": len(dupes),
            "extra_nodes_from_dupes": sum(v - 1 for v in dupes.values()),
            "dup_rate_pct": round(100 * sum(v - 1 for v in dupes.values()) / max(1, len(names)), 1),
            "top_dupes": dict(sorted(dupes.items(), key=lambda kv: -kv[1])[:15])}


def main():
    ra = load(f"canary-arm-a-{A}")
    rb = load(f"canary-arm-b-{B}")
    da, db = dup_stats(A), dup_stats(B)
    ja, jb = rejections("arm-a-nohup.out"), rejections("arm-b-nohup.out")

    per_ep = []
    for x, y in zip(ra["results"], rb["results"]):
        assert x["run_id"] == y["run_id"], "episode sets diverged"
        na = {n.lower() for n in ep_nodes(A, x["run_id"])}
        nb = {n.lower() for n in ep_nodes(B, y["run_id"])}
        inter = len(na & nb)
        union = len(na | nb)
        per_ep.append({"run_id": x["run_id"], "chars": x["chars"], "kind": x["kind"],
                       "a_status": x["status"], "b_status": y["status"],
                       "a_sec": x["sec"], "b_sec": y["sec"],
                       "a_nodes": len(na), "b_nodes": len(nb),
                       "a_edges": x.get("edges", 0), "b_edges": y.get("edges", 0),
                       "overlap": inter,
                       "jaccard": round(inter / union, 3) if union else None,
                       "a_only": sorted(na - nb)[:12], "b_only": sorted(nb - na)[:12]})

    call_types = sorted(set(ra["per_call_type"]) | set(rb["per_call_type"]))
    pack = {"at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "arm_a": ra, "arm_b": rb, "dup_a": da, "dup_b": db, "per_episode": per_ep,
            "call_types": call_types, "graphiti_rejections_a": ja,
            "graphiti_rejections_b": jb}
    probe = BASE / "probe-resolve-edge.json"
    if probe.exists():
        pack["disproof_probe_resolve_edge"] = json.loads(probe.read_text())
    (BASE / "CANARY-2026-08-28.json").write_text(json.dumps(pack, indent=1))

    def row(label, va, vb):
        print(f"{label:34} {str(va):>22} {str(vb):>22}")

    print(f"\n{'metric':34} {'A: qwen3.8:27b (shim)':>22} {'B: 4B student':>22}")
    print("-" * 80)
    row("episodes ok / total", f"{ra['ok']}/{ra['episodes']}", f"{rb['ok']}/{rb['episodes']}")
    row("failed (llm / graph)", f"{ra['fail_llm']} / {ra['fail_graph']}",
        f"{rb['fail_llm']} / {rb['fail_graph']}")
    row("wall total (s)", ra["wall_s"], rb["wall_s"])
    row("s/episode mean", ra["sec_per_episode_wall"], rb["sec_per_episode_wall"])
    row("s/episode median", ra["median_sec_per_episode"], rb["median_sec_per_episode"])
    row("LLM calls", ra["llm_calls_total"], rb["llm_calls_total"])
    row("LLM calls schema-clean %", ra["llm_clean_pct"], rb["llm_clean_pct"])
    row("entities in group", da["total_nodes"], db["total_nodes"])
    row("entity edges in group", ra["graph"]["entity_edges"], rb["graph"]["entity_edges"])
    row("duplicate name groups", da["dup_name_groups"], db["dup_name_groups"])
    row("dup rate %", da["dup_rate_pct"], db["dup_rate_pct"])
    row("episodes yielding 0 nodes",
        len([p for p in per_ep if p["a_nodes"] == 0]),
        len([p for p in per_ep if p["b_nodes"] == 0]))
    row("graphiti-rejected fragments", ja["total"], jb["total"])

    print(f"\n{'graphiti rejection (schema-clean but wrong)':46} {'A':>10} {'B':>10}")
    print("-" * 70)
    for k in REJECTIONS:
        if ja[k] or jb[k]:
            print(f"{k:46} {ja[k]:>10} {jb[k]:>10}")

    print(f"\n{'call type':46} {'A n/clean%/empty':>20} {'B n/clean%/empty':>20}")
    print("-" * 90)
    for ct in call_types:
        a = ra["per_call_type"].get(ct)
        b = rb["per_call_type"].get(ct)
        fa = f"{a['calls']}/{a['clean_pct']}%/{a['empty_payload_calls']}" if a else "-"
        fb = f"{b['calls']}/{b['clean_pct']}%/{b['empty_payload_calls']}" if b else "-"
        print(f"{ct:46} {fa:>20} {fb:>20}")

    print(f"\n{'episode':44} {'chars':>6} {'A n/e':>8} {'B n/e':>8} {'A s':>7} {'B s':>7} {'jac':>5}")
    print("-" * 92)
    for p in per_ep:
        print(f"{p['run_id'][:42]:44} {p['chars']:>6} "
              f"{str(p['a_nodes'])+'/'+str(p['a_edges']):>8} "
              f"{str(p['b_nodes'])+'/'+str(p['b_edges']):>8} "
              f"{p['a_sec']:>7} {p['b_sec']:>7} {str(p['jaccard']):>5}")
    print("\nwrote CANARY-2026-08-28.json")


if __name__ == "__main__":
    main()
