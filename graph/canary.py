#!/usr/bin/env python3
"""Graphiti canary for a configured local model and isolated synthetic facts.

Feeds reserved synthetic facts, including temporal supersession, into the
configured FalkorDB, measures each episode, then queries the result.
"""
import asyncio
import importlib.machinery
import time
from datetime import datetime, timezone
from pathlib import Path

# Portable canaries use only the native reserved synthetic feed contract.
import os
if os.environ.get("BORG_HOME"):
    import argparse
    import subprocess
    config = importlib.machinery.SourceFileLoader(
        "borg_config_canary_dispatch", str(Path(__file__).resolve().parent / "bin/borg_config.py")
    ).load_module().CONFIG
    parser = argparse.ArgumentParser(description="reserved synthetic graph feed canary")
    parser.add_argument("--run", action="store_true", help="write the reserved synthetic canary collection/graphs")
    args = parser.parse_args()
    if not args.run:
        parser.exit(2, "optional canary not run: use --run on a coordinated disposable installation\n")
    raise SystemExit(subprocess.call([
        str(config.mem0_root / "venv/bin/python"), str(config.graph_root / "backfill.py"),
        "--canary", "--json",
    ]))

from graphiti_core import Graphiti
from graphiti_core.driver.falkordb_driver import FalkorDriver
from graphiti_core.llm_client import LLMConfig
from graphiti_core.llm_client.openai_generic_client import OpenAIGenericClient
from graphiti_core.embedder.openai import OpenAIEmbedder, OpenAIEmbedderConfig
from graphiti_core.cross_encoder.openai_reranker_client import OpenAIRerankerClient
from graphiti_core.nodes import EpisodeType

import os
CONFIG = importlib.machinery.SourceFileLoader(
    "borg_config_graph_canary", str(Path(__file__).resolve().parent / "bin" / "borg_config.py")
).load_module().CONFIG
OLLAMA = str(CONFIG.values["BORG_GRAPH_LLM_URL"])
QWEN = str(CONFIG.values["BORG_GRAPH_MODEL"])

EPISODES = [
    ("ownership", "Example Company owns two synthetic compute nodes, node-a and node-b."),
    ("fleet", "Node-a runs a local extraction model for Example Company synthetic memory."),
    ("dashboard-v1", "On 2026-01-14, the example dashboard ran on port 3005."),
    ("dashboard-v2", "On 2026-01-20, the example dashboard moved from port 3005 to port 3007."),
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
    embedder = OpenAIEmbedder(config=OpenAIEmbedderConfig(embedding_model=str(CONFIG.values["BORG_EMBED_MODEL"]), embedding_dim=int(CONFIG.values["BORG_EMBED_DIMS"]), api_key="ollama", base_url=OLLAMA))
    reranker = OpenAIRerankerClient(config=LLMConfig(api_key="ollama", model=QWEN, base_url=OLLAMA), client=shared)
    driver = FalkorDriver(host=str(CONFIG.values["BORG_FALKORDB_HOST"]), port=int(CONFIG.values["BORG_FALKORDB_PORT"]))
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
                group_id=str(CONFIG.values["BORG_FALKORDB_GRAPH"]),
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
        res = await g.search("what port does the example dashboard run on", num_results=5)
        print("\nsearch 'example dashboard port' — edges returned:")
        for e in res:
            valid = getattr(e, "valid_at", None)
            invalid = getattr(e, "invalid_at", None)
            print(f"  - {getattr(e,'fact','?')[:100]}  [valid={valid} invalid={invalid}]")
    except Exception as e:
        print("search failed:", type(e).__name__, str(e)[:120])

    await g.close()


if __name__ == "__main__":
    asyncio.run(main())
