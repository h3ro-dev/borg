#!/usr/bin/env python3
"""Graphiti canary — does local qwen 3.8 27B build a valid temporal knowledge
graph? Feeds real domain facts (incl. a temporal supersession) into Graphiti
on the dedicated FalkorDB, measures success/failure per episode, then queries.
"""
import asyncio
import time
from datetime import datetime, timezone

from graphiti_core import Graphiti
from graphiti_core.driver.falkordb_driver import FalkorDriver
from graphiti_core.llm_client import LLMConfig
from graphiti_core.llm_client.openai_generic_client import OpenAIGenericClient
from graphiti_core.embedder.openai import OpenAIEmbedder, OpenAIEmbedderConfig
from graphiti_core.cross_encoder.openai_reranker_client import OpenAIRerankerClient
from graphiti_core.nodes import EpisodeType

import os
OLLAMA = os.environ.get("GRAPH_LLM_URL", "http://127.0.0.1:11500/v1")  # schema-shim: grammar-enforced
QWEN = os.environ.get("GRAPH_LLM_MODEL", "llama3.1:8b")  # MLX engine ignores format grammars; use a llama.cpp-engine model

EPISODES = [
    ("ownership", "James owns four machines: Studio0, second-machine, worker-machine, and Cody's Mac mini. He does not own Emily's Mac Studio."),
    ("fleet", "worker-machine runs a qwen 3.8 27B model that mines chat threads into James's shared memory system. second-machine runs another qwen that handles the email mine."),
    ("dashboard-v1", "On 2026-08-14, the NROS dashboard ran on port 3005."),
    ("dashboard-v2", "On 2026-08-20, the NROS dashboard was moved from port 3005 to port 3007 to resolve a port conflict."),
]


def nothink_client():
    """AsyncOpenAI client that disables qwen3 thinking on every call — otherwise
    thinking tokens overflow Graphiti's long prompts and content returns empty."""
    from openai import AsyncOpenAI
    c = AsyncOpenAI(api_key="ollama", base_url=OLLAMA)
    orig = c.chat.completions.create
    async def create(*a, **k):
        eb = k.get("extra_body") or {}
        ctk = eb.get("chat_template_kwargs") or {}
        ctk["enable_thinking"] = False
        eb["chat_template_kwargs"] = ctk
        k["extra_body"] = eb
        return await orig(*a, **k)
    c.chat.completions.create = create
    return c


async def main():
    shared = nothink_client()
    llm = OpenAIGenericClient(config=LLMConfig(api_key="ollama", model=QWEN, small_model=QWEN, base_url=OLLAMA), client=shared)
    embedder = OpenAIEmbedder(config=OpenAIEmbedderConfig(embedding_model="nomic-embed-text:latest", embedding_dim=768, api_key="ollama", base_url=OLLAMA))
    reranker = OpenAIRerankerClient(config=LLMConfig(api_key="ollama", model=QWEN, base_url=OLLAMA), client=shared)
    driver = FalkorDriver(host="127.0.0.1", port=6383)
    g = Graphiti(graph_driver=driver, llm_client=llm, embedder=embedder, cross_encoder=reranker)

    await g.build_indices_and_constraints()
    print("indices built")

    ok = fail = 0
    for name, body in EPISODES:
        t0 = time.time()
        try:
            r = await g.add_episode(
                name=name, episode_body=body,
                source_description="canary", source=EpisodeType.text,
                reference_time=datetime.now(timezone.utc),
                group_id="canary",
            )
            nodes = len(getattr(r, "nodes", []) or [])
            edges = len(getattr(r, "edges", []) or [])
            print(f"OK  {name:14s} {time.time()-t0:5.0f}s  nodes={nodes} edges={edges}")
            ok += 1
        except Exception as e:
            print(f"FAIL {name:13s} {time.time()-t0:5.0f}s  {type(e).__name__}: {str(e)[:120]}")
            fail += 1

    print(f"\n=== extraction: {ok} ok / {fail} fail of {len(EPISODES)} ===")

    # Query: what is the CURRENT dashboard port? (temporal test)
    try:
        res = await g.search("what port does the NROS dashboard run on", num_results=5)
        print("\nsearch 'NROS dashboard port' — edges returned:")
        for e in res:
            valid = getattr(e, "valid_at", None)
            invalid = getattr(e, "invalid_at", None)
            print(f"  - {getattr(e,'fact','?')[:100]}  [valid={valid} invalid={invalid}]")
    except Exception as e:
        print("search failed:", type(e).__name__, str(e)[:120])

    await g.close()


if __name__ == "__main__":
    asyncio.run(main())
