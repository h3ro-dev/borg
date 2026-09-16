# BORG Collective

**Your machines. Your agents. One collective.** An agent-first operating platform
you install and control.

[Explore BORG Collective](https://borg.utlyze.com/) ·
[Size and configure your machines](https://borg.utlyze.com/#configure) ·
[Set up your collective](docs/SETUP.md) ·
[Hardware guide](docs/HARDWARE.md) · [MIT license](LICENSE)

BORG gives your AI agents a shared local brain and native tools. Each installation has
its own memory, temporal graph, computer and browser tools, conductor, Agent Inbox,
Beads project, provider profile and credentials. It does not connect to the author's fleet.

The installer provisions the real component implementations and pinned runtimes in a
private BORG home. Codex gets native recall and capture hooks automatically. Claude and
Grok integrations are included for owner configuration. ChatGPT on the web can connect
through your own Cloudflare Access application and tunnel.

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
| `graph/` | The Graphiti layer: episode backfill, canary smoke test, and the **grammar shim** — an OpenAI-compatible proxy that grammar-locks local model JSON, with optional private training-pair capture |
| `training/` | Dataset builders, GPU-crash-tolerant training chains, and the eval harness: held-out exam, promotion canary, disproof probes |
| `conductor/` | codex-conductor: an HTTP control plane over `codex app-server` — start, steer (mid-flight), interrupt, and stream Codex threads; one instance per account profile |
| `connector/` | Authenticated MCP gateway with native files, processes, jobs, browser, UI, SSH and credential handles; no Desktop Commander dependency |
| `coordination/` | Native Agent Inbox, identities, grants, leased messages, work assignments and Beads bootstrap |
| `installer/` | Independent configuration, dependency locks, service lifecycle, native client setup and readiness diagnostics |
| `site/` | Static BORG Collective website with original artwork and local fonts |
| `docs/` | Three papers: the fine-tuning cost audit, the memory-system build, and the conductor fleet |

## The rules the system lives by

1. **Memory is never the authority.** Files, ledgers, and databases stay the source of
   truth. The Borg is a rebuildable recall layer over them.
2. **Nothing is promoted on a benchmark.** An exam score qualifies a student for a canary;
   only running the real pipeline head-to-head against the incumbent, on real backlog,
   promotes it. Our best exam scorer (400/400 format-valid) failed its canary 1-of-25 and
   stayed benched. The papers show the full numbers.
3. **Training capture is an owner decision.** Fresh installations disable the grammar
   shim's training-pair capture. Enable a private training workflow only for data you may use.
4. **An installation owns its data.** Capture filters and scoped MCP grants protect
   memory boundaries. The distribution includes source and reviewed adapters, without
   account sessions, secrets or private corpora.

## Quickstart

Use the [machine planner](https://borg.utlyze.com/#configure) to choose roles,
orchestrators, integrations and workload for each machine. Download its blueprint,
then follow the [blueprint guide](docs/BLUEPRINT.md). The [component catalog](docs/COMPONENTS.md)
distinguishes included services, provider setup, external integrations and inactive
research adapters. [Sizing assumptions](docs/SIZING.md) are public and versioned.

Start with an empty directory on an Apple Silicon Mac:

```sh
git clone https://github.com/h3ro-dev/borg.git
cd borg
./install.sh --owner yourname
"$HOME/.borg/bin/borg" auth codex
"$HOME/.borg/bin/borg" doctor
"$HOME/.borg/bin/borg" onboard
```

Setup installs local Qdrant, FalkorDB, Ollama, models, the native connector, conductor,
Inbox and lifecycle hooks. It uses your own provider login. Desktop control requires
the normal operating-system permissions. The included LoRA adapters remain benched
until validated against their exact base models; default extraction uses pinned Qwen.

See [installation and recovery](docs/INSTALL.md) and [web ChatGPT setup](docs/WEB.md).
Linux and Intel artifacts are pinned, but their complete native installation is not yet
verified. Windows is not supported by this installer.

## Connect your own machines

Install BORG on each machine, start its connector, then explicitly enroll its SSH
alias and installation identity with `borg fleet add`. `fleet_hosts` discovers the
configured targets, `fleet_tools` reads a selected target's actual tool schemas,
and `fleet_call` routes one native operation to that target. Each target keeps its
own credentials, process sessions, browser sessions and operation receipts.

Independent requests run concurrently; work touching the same resource coordinates
locally. Capacity is configurable per host, and there is no single-client connection
limit. One physical desktop still has one keyboard, mouse and focus. The
[setup guide](docs/SETUP.md) covers enrollment, conductor/provider setup, adapters
and recovery after an uncertain result.

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

Owner-authored BORG code and the included adapters are MIT. Base models and runtime
dependencies retain their own licenses; see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
Base weights are downloaded separately from their pinned upstream repositories.
