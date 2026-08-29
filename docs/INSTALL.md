# Installing the Borg

The reference estate runs everything on one Mac (Apple Silicon, macOS) with Docker and
Ollama, plus optional worker machines over SSH tunnels. Nothing here requires a cloud
account. Adapt paths to taste; scripts read config from environment where it matters.

## 0. Prerequisites

- Python 3.12 (via `uv` recommended), Docker (or colima), Node 18+ (conductor only)
- [Ollama](https://ollama.com) with two models pulled:
  - a capable extractor: a ~27B-class **GGUF** model (llama.cpp engine — **required**;
    Ollama's MLX engine silently ignores `format` grammars, GGUF enforces them)
  - `nomic-embed-text` (768-dim embeddings)
- For training/serving students: `pip install mlx-lm` (Apple Silicon)

## 1. Stores

```bash
# mem0's vector store — server mode, NOT embedded (embedded Qdrant is
# single-process and deadlocks the moment CLI + MCP + hooks coexist)
docker run -d --name mem0-qdrant --restart unless-stopped \
  -p 127.0.0.1:6333:6333 -v "$HOME/borg-data/qdrant:/qdrant/storage" qdrant/qdrant

# Graphiti's temporal graph
docker run -d --name memory-graph-falkordb --restart unless-stopped \
  -p 127.0.0.1:6383:6379 -p 127.0.0.1:3003:3000 \
  -v "$HOME/borg-data/falkor:/data" falkordb/falkordb
```

## 2. The memory layer (mem0)

```bash
uv venv --python 3.12 ~/borg-venv && source ~/borg-venv/bin/activate
pip install mem0ai graphiti-core fastmcp httpx openai
```

- `memory/bin/mem0ctl` — the CLI (`add / search / list / stats / consolidate`). Two
  hard-won defaults inside: telemetry force-disabled, and `think=false` injected for
  thinking-capable Ollama models (thinking tokens otherwise corrupt extraction JSON and
  facts get silently dropped).
- Register the MCP server so agents share the store:
  - **Claude Code**: `claude mcp add --scope user mem0 -- ~/borg/memory/bin/mem0-mcp-server-v2`
  - **Codex** (`~/.codex/config.toml`): `[mcp_servers.mem0]` with `command` pointing at the
    same server. Repeat per profile home if you shard accounts.
  - **Grok** (`~/.grok/config.toml`): same `[mcp_servers.mem0]` stanza.
- Hooks close the loop (agents write memory without being asked):
  - Claude Code `settings.json`: `UserPromptSubmit → memory/bin/mem0-claude-hook`
    (fast recall injection), `SessionEnd → memory/bin/mem0-capture-hook` (fact capture
    with secret/PII drop-filters; kill switch: `touch $BORG_HOME/HOOK_OFF`).
  - Grok hooks file: `SessionStart → memory/bin/mem0-grok-inject` (writes the injection
    file Grok reads), `SessionEnd → mem0-capture-hook` (the same binary Claude uses).
- Nightly: schedule `memory/bin/mem0-dream` (03:30 works) — seed refresh, incremental
  ingestion, consolidation of ≥0.90-cosine duplicates, contradiction review, morning report.

The v2 MCP server is scope-aware: every request carries a principal, every read is filtered
to that principal's allowed scopes. Default-closed for personal scopes. Use it (not v1) for
anything multi-agent or multi-person.

## 3. The graph layer (Graphiti)

```bash
# The grammar shim: OpenAI-compatible proxy on :11500 in front of Ollama that
# (a) grammar-enforces json_schema via the native API, (b) strips thinking,
# (c) TEES every request/response pair to a training corpus, tagged by call shape.
python graph/bin/ollama-schema-shim   # or install as a KeepAlive launchd/systemd service
```

Point `graphiti-core` at the shim (`base_url=http://127.0.0.1:11500/v1`) and at FalkorDB
(port 6383). `graph/backfill.py` walks your mem0 facts into episodes and builds the graph;
`graph/canary.py` is the smoke test that your local model + shim actually produce a valid
temporal graph before you commit to a backfill.

**The shim's tee is the whole trick.** From the moment it is in place, every production
extraction call becomes a free, shape-labeled training pair. On the reference estate it
records ~168 pairs/hour without a single dedicated teacher call.

## 4. Training students (optional, when the corpus is ready)

```bash
python training/build_dataset.py          # pairs → train/valid/test with dedup + filters
bash  training/run_v3_chain.sh            # exam → dataset build → LoRA train, launchd-ownable,
                                          # with GPU-crash auto-resume (Metal recovery events
                                          # on shared GPUs are a matter of when, not if)
python training/eval/exam.py ...          # held-out exam: JSON validity, Jaccard vs teacher
python training/eval/canary_promotion.py  # THE gate: student vs incumbent, real pipeline,
                                          # real backlog, isolated graph groups, per-call-shape
```

Promotion rule from the reference estate: exam ≥99% JSON-valid AND Jaccard above the
measured inter-teacher agreement gets you a canary; only the canary promotes. Read
`docs/PAPER-1-finetuning-cost-audit.md` before over-collecting pairs — the data says you
need far less than you think, spread across call shapes rather than piled on one.

## 5. The conductor fleet (optional)

`conductor/conductor.mjs` (zero-dependency Node) wraps `codex app-server` as a local HTTP
API: `POST /thread/start`, `POST /turn/start`, `POST /turn/steer` (mid-flight!),
`POST /turn/interrupt`, `GET /events`. Run one instance per Codex account profile
(`CODEX_HOME=<profile-dir> node conductor.mjs --port 47xx`) and you have a steerable,
observable worker fleet on subscription seats. `docs/PAPER-3-conductors.md` covers the
protocol gotchas we hit so you don't have to.

## 6. Sanity checks

- `mem0ctl stats` returns row counts; a search with zero keyword overlap still finds the
  fact (semantic recall working).
- `curl :11500/health` → `{"ok": true}`.
- Ask for a strict JSON schema through the shim and try to make the model chat instead —
  grammar wins, or your model is on the wrong engine.
- Kill a training run mid-iteration; the chain resumes from the last checkpoint.
