#!/usr/bin/env python3
"""Promotion canary: crowned 4B student vs qwen3.8:27b baseline, head to head
through the REAL graphiti_core 0.29.3 pipeline, into ISOLATED graph groups.

  canary_promotion.py --arm a      # baseline: 27B via schema-shim  -> canary-27b-20260828
  canary_promotion.py --arm b      # student : mlx_lm 4B + LoRA     -> canary-student-20260828

Why the per-call instrumentation exists
---------------------------------------
graphiti_core does NOT validate an LLM response against its pydantic
response_model. `_generate_response` only does `json.loads(...)`; the CALLER
then does `response.get('extracted_entities', [])`. So a syntactically valid
JSON object with the WRONG KEYS is not an error anywhere in the stack — it
silently yields zero entities / zero edges. Format validity is therefore not
the promotion question; SCHEMA validity per internal call type is.

This client records, for every internal call:
  prompt_name (graphiti's own call-type label), response_model, latency,
  finish_reason, http error, empty body, json parse result, and a pydantic
  validation of the parsed object against the declared response_model.
Any call that is not schema-clean has its first 500 raw chars written to
<out>.failures.jsonl.

Writes ONLY to training/eval/ and to the arm's own group_id. Never touches
data/backfill*.json, the shim, the supervisor, or a production group.
"""
import argparse
import asyncio
import contextvars
import json
import os
import subprocess
import time
import typing
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

os.environ.setdefault("GRAPHITI_TELEMETRY_ENABLED", "false")

import openai  # noqa: E402
from openai import AsyncOpenAI  # noqa: E402
from pydantic import BaseModel  # noqa: E402

from graphiti_core import Graphiti  # noqa: E402
from graphiti_core.driver.falkordb_driver import FalkorDriver  # noqa: E402
from graphiti_core.embedder.openai import OpenAIEmbedder, OpenAIEmbedderConfig  # noqa: E402
from graphiti_core.cross_encoder.openai_reranker_client import OpenAIRerankerClient  # noqa: E402
from graphiti_core.llm_client import LLMConfig  # noqa: E402
from graphiti_core.llm_client.config import DEFAULT_MAX_TOKENS, ModelSize  # noqa: E402
from graphiti_core.llm_client.errors import EmptyResponseError  # noqa: E402
from graphiti_core.llm_client.openai_generic_client import OpenAIGenericClient  # noqa: E402
from graphiti_core.nodes import EpisodeType  # noqa: E402
from graphiti_core.prompts.models import Message  # noqa: E402

BASE = Path(__file__).resolve().parent
EMBED_URL = "http://127.0.0.1:11434/v1"          # nomic, identical for both arms
FALKOR_HOST, FALKOR_PORT = "127.0.0.1", 6383
HTTP_TIMEOUT = 600.0

ARMS = {
    "a": {"name": "baseline-27b", "url": "http://127.0.0.1:11500/v1",
          "model": "qwen3.8:27b", "group": "canary-27b-20260828",
          "mode": "json_schema",   # shim turns this into an ollama decoder grammar
          "nothink": True},        # inert through the shim (shim sets think:false itself)
    "b": {"name": "student-4b-crowned", "url": "http://127.0.0.1:11440/v1",
          "model": "mlx-community/Qwen3-4B-Instruct-2507-4bit",
          "group": "canary-student-20260828",
          "mode": "json_object",   # mlx_lm has NO constrained decoding; schema goes in-prompt
          "nothink": False},       # 2507 instruct line is non-thinking
}

CTX = contextvars.ContextVar("callctx", default=None)
CALLS: list[dict] = []
FAIL_F: Path | None = None


def log(msg, logf=None):
    line = f"{datetime.now().strftime('%H:%M:%S')} {msg}"
    print(line, flush=True)
    if logf:
        with open(logf, "a") as fh:
            fh.write(f"{datetime.now().isoformat(timespec='seconds')} {msg}\n")


def record_failure(rec, raw):
    if FAIL_F is None:
        return
    with open(FAIL_F, "a") as fh:
        fh.write(json.dumps({**{k: v for k, v in rec.items() if k != "raw"},
                             "raw_head_500": (raw or "")[:500]}, ensure_ascii=False) + "\n")


class InstrumentedClient(OpenAIGenericClient):
    """OpenAIGenericClient with per-call recording. Behaviour is byte-identical
    to upstream 0.29.3 `_generate_response` — the only additions are the record
    and the (non-raising) pydantic check."""

    async def generate_response(self, messages, response_model=None, max_tokens=None,
                                model_size=ModelSize.medium, group_id=None,
                                prompt_name=None, *, attribute_extraction=False):
        cur = dict(CTX.get() or {})
        cur["prompt_name"] = prompt_name or "?"
        tok = CTX.set(cur)
        try:
            return await super().generate_response(
                messages, response_model, max_tokens, model_size,
                group_id, prompt_name, attribute_extraction=attribute_extraction)
        finally:
            CTX.reset(tok)

    async def _generate_response(self, messages: list[Message],
                                 response_model: type[BaseModel] | None = None,
                                 max_tokens: int = DEFAULT_MAX_TOKENS,
                                 model_size: ModelSize = ModelSize.medium
                                 ) -> dict[str, typing.Any]:
        ctx = CTX.get() or {}
        rec = {"episode": ctx.get("episode"), "prompt_name": ctx.get("prompt_name", "?"),
               "response_model": getattr(response_model, "__name__", None),
               "prompt_chars": sum(len(m.content) for m in messages)}
        openai_messages = []
        for m in messages:
            m.content = self._clean_input(m.content)
            if m.role in ("user", "system"):
                openai_messages.append({"role": m.role, "content": m.content})

        t0 = time.time()
        raw = ""
        try:
            response = await self.client.chat.completions.create(
                model=self.model or "gpt-4.1-mini",
                messages=openai_messages,
                temperature=self.temperature,
                max_tokens=max_tokens,
                response_format=self._build_response_format(response_model),
            )
            rec["sec"] = round(time.time() - t0, 2)
            ch = response.choices[0]
            rec["finish_reason"] = getattr(ch, "finish_reason", None)
            usage = getattr(response, "usage", None)
            if usage:
                rec["prompt_tokens"] = getattr(usage, "prompt_tokens", None)
                rec["completion_tokens"] = getattr(usage, "completion_tokens", None)
            raw = ch.message.content or ""
            rec["out_chars"] = len(raw)
            if not raw:
                rec["outcome"] = "empty"
                CALLS.append(rec)
                record_failure(rec, raw)
                raise EmptyResponseError("LLM returned an empty response")
            try:
                parsed = json.loads(self._strip_code_fences(raw))
            except json.JSONDecodeError as e:
                rec["outcome"] = "json_invalid"
                rec["error"] = str(e)[:200]
                CALLS.append(rec)
                record_failure(rec, raw)
                raise
            # --- the check graphiti_core does NOT do ---
            rec["raw_head"] = raw[:500]
            rec["top_keys"] = sorted(parsed.keys()) if isinstance(parsed, dict) else ["<not-object>"]
            if isinstance(parsed, dict):
                rec["payload_counts"] = {k: len(v) for k, v in parsed.items()
                                         if isinstance(v, list)}
            if response_model is not None:
                try:
                    response_model.model_validate(parsed)
                    rec["outcome"] = "ok"
                except Exception as e:
                    rec["outcome"] = "schema_invalid"
                    rec["error"] = str(e).replace("\n", " ")[:300]
                    rec["expected_keys"] = sorted(response_model.model_json_schema()
                                                  .get("properties", {}).keys())
                    record_failure(rec, raw)
            else:
                rec["outcome"] = "ok"
            CALLS.append(rec)
            return parsed
        except openai.RateLimitError:
            rec.setdefault("sec", round(time.time() - t0, 2))
            rec["outcome"] = "rate_limit"
            CALLS.append(rec)
            raise
        except (EmptyResponseError, json.JSONDecodeError):
            raise
        except Exception as e:
            rec.setdefault("sec", round(time.time() - t0, 2))
            rec["outcome"] = "http_error"
            rec["error"] = f"{type(e).__name__}: {str(e)[:300]}"
            CALLS.append(rec)
            record_failure(rec, raw)
            raise


def nothink(client: AsyncOpenAI) -> AsyncOpenAI:
    orig = client.chat.completions.create

    async def create(*a, **k):
        eb = k.get("extra_body") or {}
        ctk = eb.get("chat_template_kwargs") or {}
        ctk["enable_thinking"] = False
        eb["chat_template_kwargs"] = ctk
        k["extra_body"] = eb
        return await orig(*a, **k)

    client.chat.completions.create = create
    return client


def build(arm: dict, max_coroutines: int):
    raw_client = AsyncOpenAI(api_key="local", base_url=arm["url"], timeout=HTTP_TIMEOUT,
                             max_retries=0)
    if arm["nothink"]:
        raw_client = nothink(raw_client)
    llm = InstrumentedClient(
        config=LLMConfig(api_key="local", model=arm["model"], small_model=arm["model"],
                         base_url=arm["url"]),
        client=raw_client, max_tokens=16384, structured_output_mode=arm["mode"])
    embedder = OpenAIEmbedder(config=OpenAIEmbedderConfig(
        embedding_model="nomic-embed-text:latest", embedding_dim=768,
        api_key="ollama", base_url=EMBED_URL))
    reranker = OpenAIRerankerClient(
        config=LLMConfig(api_key="local", model=arm["model"], base_url=arm["url"]),
        client=raw_client)
    g = Graphiti(graph_driver=FalkorDriver(host=FALKOR_HOST, port=FALKOR_PORT),
                 llm_client=llm, embedder=embedder, cross_encoder=reranker,
                 max_coroutines=max_coroutines)
    return g, llm, raw_client


def ref_time(s):
    try:
        return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except Exception:
        return datetime.now(timezone.utc)


def rq(graph, cypher):
    out = subprocess.run(["redis-cli", "-p", str(FALKOR_PORT), "GRAPH.RO_QUERY", graph, cypher],
                         capture_output=True, text=True).stdout.splitlines()
    return [l.strip() for l in out[1:-2] if l.strip()]


def scalar(graph, cypher, default=0):
    try:
        r = rq(graph, cypher)
        return int(r[0]) if r else default
    except Exception:
        return default


def graph_stats(group):
    names = rq(group, "MATCH (n:Entity) RETURN n.name")
    low = Counter(n.strip().lower() for n in names if n.strip())
    dupes = {k: v for k, v in low.items() if v > 1}
    return {
        "episodes": scalar(group, "MATCH (e:Episodic) RETURN count(e)"),
        "entities": scalar(group, "MATCH (n:Entity) RETURN count(n)"),
        "entity_edges": scalar(group, "MATCH ()-[x:RELATES_TO]->() RETURN count(x)"),
        "mentions": scalar(group, "MATCH ()-[x:MENTIONS]->() RETURN count(x)"),
        "distinct_names": len(low),
        "duplicate_name_groups": len(dupes),
        "duplicate_extra_nodes": sum(v - 1 for v in dupes.values()),
        "duplicate_names": dict(sorted(dupes.items(), key=lambda kv: -kv[1])[:25]),
        "sample_names": sorted(low)[:60],
    }


async def ingest(g, ep, group, tag, logf):
    tok = CTX.set({"episode": ep["run_id"]})
    n0 = len(CALLS)
    t0 = time.time()
    try:
        r = await g.add_episode(
            name=ep["run_id"], episode_body=ep["body"], source_description=ep["kind"],
            source=EpisodeType.text, reference_time=ref_time(ep["date"]), group_id=group)
        dt = time.time() - t0
        nodes = [getattr(n, "name", "?") for n in (getattr(r, "nodes", []) or [])]
        edges = [getattr(e, "fact", "?") for e in (getattr(r, "edges", []) or [])]
        log(f"{tag}ok {dt:6.1f}s nodes={len(nodes):2} edges={len(edges):2} "
            f"calls={len(CALLS) - n0:2} {ep['run_id'][:44]}", logf)
        return {"run_id": ep["run_id"], "chars": ep["chars"], "kind": ep["kind"],
                "status": "ok", "sec": round(dt, 1), "nodes": len(nodes),
                "edges": len(edges), "node_names": nodes, "edge_facts": edges,
                "llm_calls": len(CALLS) - n0}
    except Exception as e:
        dt = time.time() - t0
        name = type(e).__name__
        blob = f"{name} {e}".lower()
        cause = "graph" if any(s in blob for s in ("redis", "falkor", "timed out", "socket",
                                                   "connection")) else "llm"
        log(f"{tag}FAIL {dt:6.1f}s [{cause}] {name}: {str(e)[:120]} {ep['run_id'][:44]}", logf)
        return {"run_id": ep["run_id"], "chars": ep["chars"], "kind": ep["kind"],
                "status": "fail", "fail_cause": cause, "sec": round(dt, 1),
                "error": f"{name}: {str(e)[:400]}", "llm_calls": len(CALLS) - n0}
    finally:
        CTX.reset(tok)


def call_summary():
    by = defaultdict(lambda: Counter())
    secs = defaultdict(list)
    for c in CALLS:
        by[c["prompt_name"]][c.get("outcome", "?")] += 1
        if c.get("sec") is not None:
            secs[c["prompt_name"]].append(c["sec"])
    empties = defaultdict(int)
    items = defaultdict(int)
    for c in CALLS:
        pc = c.get("payload_counts")
        if pc is not None:
            n = sum(pc.values())
            items[c["prompt_name"]] += n
            if n == 0:
                empties[c["prompt_name"]] += 1
    out = {}
    for pn, ctr in sorted(by.items()):
        tot = sum(ctr.values())
        s = sorted(secs[pn]) or [0]
        out[pn] = {"calls": tot, **{k: v for k, v in sorted(ctr.items())},
                   "clean_pct": round(100 * ctr.get("ok", 0) / tot, 1),
                   "empty_payload_calls": empties[pn], "items_returned": items[pn],
                   "median_sec": s[len(s) // 2], "max_sec": s[-1]}
    return out


async def main():
    global FAIL_F
    ap = argparse.ArgumentParser()
    ap.add_argument("--arm", choices=["a", "b"], required=True)
    ap.add_argument("--episodes", default="canary-episodes-25-20260828.json")
    ap.add_argument("--limit", type=int, default=0, help="smoke: first N episodes only")
    ap.add_argument("--group", default=None, help="override group_id (smoke runs)")
    ap.add_argument("--max-coroutines", type=int, default=20,
                    help="20 = graphiti's SEMAPHORE_LIMIT default, what backfill.py runs")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    arm = dict(ARMS[args.arm])
    if args.group:
        arm["group"] = args.group
    stem = args.out or f"canary-arm-{args.arm}-{arm['group']}"
    out_f = BASE / f"{stem}.json"
    FAIL_F = BASE / f"{stem}.failures.jsonl"
    logf = BASE / f"{stem}.log"

    eps = json.loads((BASE / args.episodes).read_text())
    if args.limit:
        eps = eps[:args.limit]

    g, llm, raw = build(arm, args.max_coroutines)
    log(f"START arm={args.arm} ({arm['name']}) model={arm['model']} url={arm['url']} "
        f"mode={arm['mode']} group={arm['group']} episodes={len(eps)}", logf)
    await g.build_indices_and_constraints()

    t0 = time.time()
    results = []
    for i, ep in enumerate(eps, 1):
        results.append(await ingest(g, ep, arm["group"], f"[{i}/{len(eps)}] ", logf))
    wall = time.time() - t0

    ok = [r for r in results if r["status"] == "ok"]
    stats = graph_stats(arm["group"])
    per_call = call_summary()
    outcomes = Counter(c.get("outcome", "?") for c in CALLS)
    summary = {
        "arm": args.arm, "engine": arm["name"], "model": arm["model"], "url": arm["url"],
        "structured_output_mode": arm["mode"], "group_id": arm["group"],
        "graphiti_core": "0.29.3", "embedder": "nomic-embed-text:latest @ " + EMBED_URL,
        "at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "episodes": len(eps), "ok": len(ok),
        "fail": len([r for r in results if r["status"] != "ok"]),
        "fail_graph": len([r for r in results if r.get("fail_cause") == "graph"]),
        "fail_llm": len([r for r in results if r.get("fail_cause") == "llm"]),
        "wall_s": round(wall, 1),
        "sec_per_episode_wall": round(wall / max(1, len(eps)), 1),
        "median_sec_per_episode": sorted(r["sec"] for r in results)[len(results) // 2],
        "returned_nodes": sum(r.get("nodes", 0) for r in ok),
        "returned_edges": sum(r.get("edges", 0) for r in ok),
        "llm_calls_total": len(CALLS),
        "llm_call_outcomes": dict(sorted(outcomes.items())),
        "llm_clean_pct": round(100 * outcomes.get("ok", 0) / max(1, len(CALLS)), 1),
        "per_call_type": per_call,
        "graph": stats,
        "results": results,
    }
    out_f.write_text(json.dumps(summary, indent=1))
    (BASE / f"{stem}.calls.json").write_text(json.dumps(CALLS, indent=1))
    log(f"END ok={summary['ok']} fail={summary['fail']} wall={summary['wall_s']}s "
        f"{summary['sec_per_episode_wall']}s/ep | calls={len(CALLS)} "
        f"clean={summary['llm_clean_pct']}% | graph entities={stats['entities']} "
        f"edges={stats['entity_edges']} dup_groups={stats['duplicate_name_groups']}", logf)
    for pn, d in per_call.items():
        log(f"  {pn:52} n={d['calls']:4} clean={d['clean_pct']:5}% med={d['median_sec']}s", logf)
    await g.close()
    await raw.close()


if __name__ == "__main__":
    asyncio.run(main())
