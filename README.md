# The Borg

One brain, many hands. The Borg is a local-first shared memory system for AI agents: every
agent session on the machine — Claude Code, Codex, Grok — reads from and writes to the same
memory, so anything one agent learns, every agent knows. The collective grows every session.

It is not a framework you adopt. It is the working machinery of a real single-operator
estate, extracted, scrubbed, and published: the recall layer, the temporal knowledge graph,
the fine-tuned local extraction models, the promotion gates that keep them honest, and the
conductor that turns ChatGPT-account Codex seats into a steerable worker fleet.

```
        ┌────────────┐  ┌────────────┐  ┌────────────┐
        │ Claude Code│  │   Codex    │  │    Grok    │
        └─────┬──────┘  └─────┬──────┘  └─────┬──────┘
              │  MCP + hooks  │  MCP + hooks  │
              ▼               ▼               ▼
        ┌─────────────────────────────────────────────┐
        │              mem0 recall layer              │   ← facts (vector store)
        │        local LLM extraction + filters       │
        └──────────────────────┬──────────────────────┘
                               ▼
        ┌─────────────────────────────────────────────┐
        │        Graphiti temporal knowledge graph    │   ← entities, relations, time
        │   grammar-locked local models via the shim  │
        └──────────────────────┬──────────────────────┘
                               ▼
        ┌─────────────────────────────────────────────┐
        │   LoRA students (this repo's adapters/)     │   ← tiny local models learning
        │   exam → canary → promote, or stay benched  │     the teachers' jobs
        └─────────────────────────────────────────────┘
```

## What is in this repo

| Directory | What it holds |
|---|---|
| `adapters/` | Trained LoRA adapters (weights included) for local extraction students, with honest model cards — including the predecessor that failed its promotion canary and why |
| `memory/` | The mem0 layer: CLI, MCP servers (v1 + scoped v2), session hooks for Claude Code / Codex / Grok, nightly consolidation ("dream"), ingestion tools, tests |
| `graph/` | The Graphiti layer: episode backfill, canary smoke test, and the **grammar shim** — an OpenAI-compatible proxy that grammar-locks local model JSON *and* tees every request/response into a free training corpus |
| `training/` | Dataset builders, GPU-crash-tolerant training chains, and the eval harness: held-out exam, promotion canary, disproof probes |
| `conductor/` | codex-conductor: an HTTP control plane over `codex app-server` — start, steer (mid-flight), interrupt, and stream Codex threads; one instance per account profile |
| `docs/` | Three papers: the fine-tuning cost audit, the memory-system build, and the conductor fleet |

## The rules the system lives by

1. **Memory is never the authority.** Files, ledgers, and databases stay the source of
   truth. The Borg is a rebuildable recall layer over them.
2. **Nothing is promoted on a benchmark.** An exam score qualifies a student for a canary;
   only running the real pipeline head-to-head against the incumbent, on real backlog,
   promotes it. Our best exam scorer (400/400 format-valid) failed its canary 1-of-25 and
   stayed benched. The papers show the full numbers.
3. **The corpus builds itself.** The grammar shim tees live production traffic into
   training pairs at zero marginal cost (~168 pairs/hour on this estate), tagged by call
   shape. Data is a flow, not a stock you go harvest.
4. **Secrets and client data never enter shared memory.** Extraction runs behind drop
   filters; scoped access (v2 MCP) gates who reads what; this public repo ships machinery
   and the adapter weights only — no data, no private corpora. The published adapters were
   trained on identifier-scrubbed pairs and gated on a memorization probe.

## Quickstart

See [docs/INSTALL.md](docs/INSTALL.md) for the full path. The short version:

1. Run Qdrant and FalkorDB containers, and Ollama with a capable local model
   (we use qwen-class 27B GGUF; llama.cpp engine required for grammar enforcement).
2. Create the venv, `pip install mem0ai graphiti-core`, point `memory/bin/mem0ctl` at your
   store, seed it, and register `memory/bin/mem0-mcp-server-v2` with your agent CLIs.
3. Put `graph/bin/ollama-schema-shim` in front of Ollama (port 11500) so extraction JSON is
   grammar-enforced — and so every call starts feeding your own training corpus.
4. When the corpus is ready, `training/` takes you from pairs to a LoRA student to an exam
   to a canary. Promotion is your call, made on canary evidence.

These tools were extracted from a working estate, not built as a product. Paths and
defaults are configurable but opinionated; expect to adapt them to your machine.

## The adapters

Small local students trained to take over extraction jobs from big models. Each adapter
works **only** with the exact base model it was trained on (that is how LoRA works — the
adapter is a delta on specific frozen weights):

All three are trained on **identifier-scrubbed** pairs and shipped with their weights,
after a memorization probe returned **zero sensitive-registry hits** on each:

| Adapter | Base model (required, exact) | Job | Status |
|---|---|---|---|
| `graphiti-extraction-qwen3-1.7b` | `mlx-community/Qwen3-1.7B-4bit` | Graphiti entity/relation extraction | Clean retrain; exam-passed speed tier (92.75% JSON-valid, Jaccard 0.610, ~86 tok/s) |
| `graphiti-extraction-qwen3-4b` | `mlx-community/Qwen3-4B-Instruct-2507-4bit` | Graphiti entity/relation extraction | Clean retrain of the exam winner that **failed its promotion canary** (93.5% JSON-valid, Jaccard 0.654) — successor to the case study, **not itself canaried** |
| `capture-extraction-qwen3-4b` | `mlx-community/Qwen3-4B-Instruct-2507-4bit` | Session-fact capture (mem0) | Clean retrain; format-solid (100% JSON-valid, support-ref 1.0), content agreement still unmeasured — exact-match metric proved unsuitable |

Full cards with every number: [adapters/README.md](adapters/README.md).

## Why "the Borg"

Because the point is assimilation — every session, every agent, every machine feeding one
collective memory that compounds. Resistance was futile; the estate's agents stopped
re-learning the same facts every morning.

## License

MIT. The Qwen base models the adapters attach to are separately licensed (Apache-2.0) and
are not distributed here — pull them from their own repos.
