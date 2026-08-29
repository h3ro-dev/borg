#!/usr/bin/env python3
"""Disproof probe: is the student's dedupe_edges.resolve_edge failure caused by
the canary's json_object wiring (schema appended to the prompt), or by the
model itself?

Same real graphiti prompt, three wirings, N samples each:
  json_object  - graphiti appends the EdgeDuplicate schema to the last message
                 (what Arm B ran; the schema is present to be echoed)
  json_schema  - graphiti appends NOTHING; the schema only rides in
                 response_format, which mlx_lm ignores outright
  27b_baseline - the same json_schema request through the grammar shim

READ-ONLY. Talks to the two model endpoints, writes nothing but its own report.
"""
import asyncio
import json
import sys
from collections import Counter

from openai import AsyncOpenAI
from graphiti_core.prompts.dedupe_edges import resolve_edge
from graphiti_core.prompts.models import Message
from graphiti_core.utils.maintenance.edge_operations import EdgeDuplicate

STUDENT = ("http://127.0.0.1:11440/v1", "mlx-community/Qwen3-4B-Instruct-2507-4bit")
SHIM = ("http://127.0.0.1:11500/v1", "qwen3.8:27b")

CTX = {
    "existing_edges": [
        {"idx": 0, "fact": "James owns the machine Studio0"},
        {"idx": 1, "fact": "The NROS dashboard ran on port 3005 on 2026-08-14"},
        {"idx": 2, "fact": "second-machine runs a qwen model that handles the email mine"},
    ],
    "edge_invalidation_candidates": [
        {"idx": 3, "fact": "The NROS dashboard ran on port 3005"},
    ],
    "new_edge": "The NROS dashboard was moved from port 3005 to port 3007 on 2026-08-20",
}


def build_messages(mode: str) -> list[dict]:
    msgs: list[Message] = resolve_edge(CTX)
    if mode == "json_object":
        # exactly what OpenAIGenericClient.generate_response does in that mode
        msgs[-1].content += ("\n\nRespond with a JSON object in the following format:\n\n"
                             + json.dumps(EdgeDuplicate.model_json_schema()))
    msgs[0].content += (
        "\n\nAny extracted information should be returned in the same language as it was "
        "written in. Only output non-English text when the user has written full sentences "
        "or phrases in that non-English language. Otherwise, output English.")
    return [{"role": m.role, "content": m.content} for m in msgs]


def response_format(mode: str):
    if mode == "json_object":
        return {"type": "json_object"}
    return {"type": "json_schema",
            "json_schema": {"name": "EdgeDuplicate",
                            "schema": EdgeDuplicate.model_json_schema()}}


def classify(raw: str):
    try:
        parsed = json.loads(raw.strip().removeprefix("```json").removeprefix("```")
                            .removesuffix("```").strip())
    except json.JSONDecodeError:
        return "json_invalid", None
    if not isinstance(parsed, dict):
        return "not_object", None
    keys = sorted(parsed.keys())
    if {"properties", "title"} & set(keys):
        return "SCHEMA_ECHO", keys
    try:
        EdgeDuplicate.model_validate(parsed)
        return "ok", keys
    except Exception:
        return "schema_invalid", keys


async def run(label, url, model, mode, n):
    c = AsyncOpenAI(api_key="local", base_url=url, timeout=600, max_retries=0)
    msgs = build_messages(mode)
    outcomes, sample = Counter(), {}
    for _ in range(n):
        try:
            r = await c.chat.completions.create(
                model=model, messages=msgs, temperature=0, max_tokens=16384,
                response_format=response_format(mode))
            raw = r.choices[0].message.content or ""
        except Exception as e:
            outcomes[f"http_error:{type(e).__name__}"] += 1
            continue
        oc, keys = classify(raw)
        outcomes[oc] += 1
        sample.setdefault(oc, raw[:220])
    await c.close()
    total = sum(outcomes.values())
    clean = round(100 * outcomes.get("ok", 0) / max(1, total), 1)
    print(f"\n=== {label}  (mode={mode}, n={total}) -> clean {clean}%")
    for k, v in outcomes.most_common():
        print(f"    {v:3}x {k}")
    for k, v in sample.items():
        print(f"    [{k}] {v!r}"[:260])
    return {"label": label, "mode": mode, "n": total, "clean_pct": clean,
            "outcomes": dict(outcomes), "samples": sample}


async def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    out = []
    out.append(await run("student-4b json_object (Arm B wiring)", *STUDENT, "json_object", n))
    out.append(await run("student-4b json_schema (no schema in prompt)", *STUDENT, "json_schema", n))
    out.append(await run("27b via grammar shim", *SHIM, "json_schema", n))
    print("\n" + json.dumps({r["label"]: {"clean_pct": r["clean_pct"],
                                          "outcomes": r["outcomes"]} for r in out}, indent=1))
    from pathlib import Path
    Path(__file__).resolve().parent.joinpath("probe-resolve-edge.json").write_text(
        json.dumps(out, indent=1))


if __name__ == "__main__":
    asyncio.run(main())
