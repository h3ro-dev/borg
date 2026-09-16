# BORG system and hardware guide

For a new full-system setup, we recommend **one Apple Silicon Mac with at least
32 GB unified memory and 100 GB free SSD space**. Consider **64 GB** when BORG
shares the machine with several active development/browser workloads or optional
model experiments. These are engineering recommendations, not measured minimums
or promises about agent count. One machine is enough to begin.

Reviewed **September 16, 2026**, against public source **d0a55a4**. Apple Silicon
macOS is the native verified installation path. Linux and Intel macOS artifacts
are pinned, but complete native acceptance remains pending. Windows is unsupported
by this installer. See the [installation requirements][install] and
[one-machine conductor contract][one-machine].

## What runs on your machine

BORG gives the agents you connect a persistent local recall system, tools and work
coordination. Each installation starts with its own identities and empty data;
it does not join another owner's machines or import their memories.

| Part | What it does | Local implementation |
|---|---|---|
| Semantic memory | Stores useful facts, preferences and decisions for retrieval across sessions. | mem0 with a Qdrant vector store and SQLite history. |
| Temporal graph | Connects entities and relationships with validity times, so a changed relationship can be distinguished from an older one. | Graphiti over persistent FalkorDB. |
| Capture and recall | Retrieves relevant context at lifecycle boundaries and extracts supported memory candidates from completed work. | Four native Codex hooks; other clients require their own integration. |
| Conductor | Controls native agent threads and routes work using machine, identity, claim and allowance checks. | Codex app-server bridge and router; provider-specific limits remain explicit. |
| Agent Inbox and Beads | Keeps agent messages/assignments and durable project work records. | Local authenticated Inbox Hub plus an independent Beads workspace. |
| Native connector | Exposes files, processes, browser and OS tools; routes selected calls to explicitly enrolled machines. | Authenticated MCP, native tools and SSH transport. |

Implementation sources: [memory store configuration][memory-store], [graph validity
windows][graph-time], [Codex hook registration][hooks], [conductor admission][routing],
[coordination boundary][coordination] and [targeted native calls][fleet].

Semantic memory answers questions such as “What preferences have I saved?” The
temporal graph adds relationships and change: for example, who owned a project and
when that changed. The background brain feeds saved facts into Graphiti, then
projects active graph facts into a rebuildable Qdrant recall collection. Prompt
hooks query that projection; they do not run the full graph search on every
prompt. Graph enrichment is asynchronous, not a promise of immediate consistency.
[Brain cycle][brain] · [Recall projection][projection].

Recall remains evidence to check against current files and records. It is not an
authoritative replacement for them. Capturing memory also **does not fine-tune a
model**: optional training-pair collection and LoRA training are separate workflows.
The grammar shim's training capture is off unless explicitly enabled.
[Memory principle][memory-principle] · [Training opt-in][capture-opt-in].

## Exact default model and service configuration

| Setting | Shipped default |
|---|---|
| Fact extraction and graph model | `qwen3:4b` through the installation's Ollama service. |
| Extraction quantization | The pinned Ollama artifact is Q4_K_M, approximately four-bit weights; it is separate from the optional MLX adapter bases. |
| Embedding model | `nomic-embed-text:latest`, with **768-dimensional** vectors. This creates retrieval vectors, not chat responses. |
| Extraction context | **16,384 tokens** via `OLLAMA_CONTEXT_LENGTH`. This is not a claim about the embedding model's input limit or a cloud provider's context. |
| Local inference scheduling | `OLLAMA_NUM_PARALLEL=1`, `OLLAMA_MAX_LOADED_MODELS=2`, `OLLAMA_KEEP_ALIVE=5m`. These are configuration limits, not measured concurrency guarantees. |
| Local model cloud mode | `OLLAMA_NO_CLOUD=1`. Separately configured cloud coding agents still use their provider services. |

The installer selects `qwen3:4b` as its [initial model][model-default], configures
[model names/dimensions][config-models] and passes the [Ollama service settings][ollama-env].
It verifies downloaded model manifest digests against [models.lock.json][model-lock],
including the tag named `latest`; a different digest is refused by the
[model verification path][model-check]. The publisher identifies the same Qwen
digest prefix, `359d7dd4bcda`, as [Q4_K_M][qwen-publisher]. Nomic's publisher describes
its [embedding-only role][nomic-publisher].

The managed service definitions cover Qdrant, the FalkorDB graph service, Ollama,
the graph JSON-schema shim, memory API, background brain, native connector, Codex
conductor, Inbox and watchdog. Optional web access adds the OAuth gateway and
Cloudflare tunnel. Beads is initialized as the owner's work store. These service
names are not a process-count benchmark. [Service definitions][services].

Pinned supporting components include Python **3.12.12**, Node **24.21.0**, Codex
CLI **0.146.0**, Qdrant **1.19.1**, Ollama **0.34.0**, Beads **1.2.2** and Chromium
**151.0.7922.34**. The Python layer pins mem0ai **2.0.18**, graphiti-core **0.29.3**
and falkordblite **0.10.0**. FalkorDB is the graph engine; Qdrant is the vector
store—this installation does not require adding Neo4j.
[Managed Python][python] · [Runtime pins][runtime-lock] · [Codex pin][codex-pin] ·
[Browser pin][browser-lock] · [Python package pins][python-pins] · [Graph service][falkor-service].

## Choose useful headroom

All figures below are **planning recommendations for Apple Silicon**, not results
from RAM-limit, throughput or concurrent-agent benchmarks. Free space means space
available for BORG and growth, not the SSD's advertised total capacity.

| Tier | Unified memory | Free SSD space to plan for | Assumptions and limits |
|---|---:|---:|---|
| Starter evaluation on a machine you already own | **16 GB** | **50 GB** | One modest owner workflow, default small local models, limited other apps; no training. Evaluate memory pressure and backlog first. This is not a certified minimum or a recommendation to buy a 16 GB machine for the full system. |
| Recommended full system | **32 GB or more** | **100 GB** | Default memory/graph services, 16K extraction context, coding client and ordinary browser/project work. Leave adapters inactive initially. |
| More concurrent work | **64 GB or more** | **200 GB or more** | More active browser/build workloads, larger stores, extra local models or occasional adapter evaluation. No fixed number of agents is promised. |
| Optional LoRA training | **64 GB or more as a planning starting point** | **200 GB or more, plus dataset/checkpoint budget** | Profile your exact model, batch, sequence length and trainable layers. Prefer a separate training window or machine so training does not starve daily memory services. This is not a proven training minimum. |

Why recommend 32 GB when the default models are much smaller? Their locked on-disk
payloads total about **2.77 GB**, but model files are only one part of live memory.
Runtime allocations, attention caches for the 16K context, embeddings, databases,
indexes, browser processes, tools and macOS all need room. Keeping the default
extraction and embedding models loaded also competes with foreground work.
**32 GB is a conservative allowance for that combined workload**, not a measured
sum of component peaks.

The case for 64 GB is additional operating headroom, especially for independent
browser/build jobs or simultaneous local-model experiments. More RAM does not
remove CPU/GPU contention, provider limits or shared-desktop conflicts. Ollama's
[concurrency guidance][ollama-faq] explains that parallel requests increase context
memory; its [context guide][ollama-context] confirms that longer context consumes
more memory. BORG's explicit settings govern its defaults even when upstream
Ollama defaults change.

Cloud provider inference and local extraction are different workloads. Running a
Codex, Claude or Grok client does not mean loading that provider's frontier model
on your Mac. Local tools, builds and browsers still consume local resources;
provider sign-in, Internet access, allowance and any charges remain separate.
For unattended work, keep the host awake, powered and reachable. Desktop control
needs the relevant OS permissions and an interactive session.

## Disk: pinned artifacts versus a usable installation

The existing installer guide asks for **at least 20 GB free** for runtimes, caches
and initial models, plus data. Keep that baseline distinct from the **50/100/200 GB
headroom recommendations** above. Neither the recommendations nor the figures
below are measurements of total installed disk usage. [Installer baseline][install].

Sizes below are decimal bytes/GB. They come from the reviewed release locks and
adapter manifest; compressed downloads, model payloads and expanded installations
must not be treated as interchangeable.

| Fixed artifact group | Recorded bytes | Meaning |
|---|---:|---|
| Default `qwen3:4b` | 2,497,293,444 | Locked Ollama model size. Extraction and graph share this model; count it once. |
| Default `nomic-embed-text:latest` | 274,302,030 | Locked embedding model size. |
| Default model total | **2,771,595,474** | About **2.77 GB**, before runtimes, caches or data. |
| Three included LoRA deltas | **78,715,544** | About **78.72 MB** of adapter weight files; separate base models are still needed for use. |
| Two unique optional MLX base sets | **3,262,982,941** | About **3.26 GB** including pinned config/tokenizer/index files. The two 4B adapters share one base download. |
| Apple Silicon Chromium archive | 187,406,357 | Download archive only; expanded browser and browser profiles require more space. |
| Five other Apple Silicon runtime archives | 268,295,286 | Sum of recorded uv, Qdrant, Ollama, Beads and cloudflared archive sizes; deliberately excludes Node, Python and package caches. |

Sources: [model lock][model-lock], [adapter manifest][adapter-manifest],
[browser lock][browser-lock] and [runtime lock][runtime-lock]. Default model
[downloads are deduplicated][model-check]. Optional [MLX preparation][adapter-prepare]
reuses the same model/revision directory for the two 4B adapters.

The source checkout, installed application copy, downloaded archives, expanded
runtimes, Python/npm caches, models, indexes, logs, operation receipts, browser
profiles and backups all add storage. Training adds datasets and checkpoints.
Some locks provide hashes without a byte count, so these entries cannot establish
an exact whole-install total. Keep growth and recovery space; use your actual
home's storage and memory-pressure observations before adding work.

## Three trained adapters, all inactive

These are included **research LoRA deltas**, not three additional default services
and not complete standalone models. The default brain uses pinned Ollama Qwen.
All three adapter entries are inactive and have not passed a promotion canary.
[Release policy and manifest][adapter-manifest].

| Adapter | Role | Required MLX base | Readiness limit |
|---|---|---|---|
| `graphiti-extraction-qwen3-1.7b` | Extract entities and relationships from a text episode. | `mlx-community/Qwen3-1.7B-4bit` | Evaluated on a held-out extraction shape; benched, not canaried. |
| `graphiti-extraction-qwen3-4b` | The larger entity/relation extraction student. | `mlx-community/Qwen3-4B-Instruct-2507-4bit` | Benched and uncanaried; single-shape training does not establish whole-pipeline coverage. |
| `capture-extraction-qwen3-4b` | Produce session-fact candidates with supporting references. | `mlx-community/Qwen3-4B-Instruct-2507-4bit` | Benched; semantic content quality remains unknown. Valid JSON and support-reference fields are insufficient proof of factual fidelity. |

The capture card reports 300/300 JSON-valid outputs but about 1.756 times the
teacher's candidate count. Its exact-string agreement metric cannot establish
semantic quality; a suitable semantic evaluation is still needed. Do not turn
format validity into an accuracy claim. [Capture model card][capture-card].

Use the exact base repository, compatible architecture/quantization and reviewed
release revision. The 1.7B release pin is
`3b1b1768f8f8cf8351c712464f906e86c2b8269e`; the shared 4B release pin is
`50d427756c6b1b2fe0c0a10f67fbda1fc8e82c1b`. The original training-time commits were
**not recorded**. These release pins make fetching reproducible; they do not
reconstruct missing training provenance or replace compatibility testing.
[1.7B pin and provenance][base-17] · [4B pin and provenance][base-4].

`borg adapters prepare all` is an optional Apple Silicon/MLX workflow. It verifies
artifacts and returns canary generation commands; it does not activate adapters.
Test the actual extraction workflow on data you may use before considering
promotion. [Preparation contract][adapter-prepare].

Training needs a separate budget. Frozen four-bit base weights reduce one part
of memory use; activations, gradients, optimizer state and evaluation still add
cost. Batch size, sequence length and the number of trained layers change the
requirement. The [MLX-LM LoRA guide][mlx-lora] recommends adjusting those parameters
and gradient checkpointing when memory is constrained. There is no validated RAM
minimum here for reproducing the shipped adapters, and no promised training rate.

## Start with one machine; enroll more when useful

Install BORG on each machine you choose to use, then explicitly enroll its SSH
alias and installation identity with `borg fleet add`. `fleet_hosts` checks
identity and discovers available tools, `fleet_tools` returns the selected
machine's schemas, and `fleet_call` invokes one of its native operations.
Each target keeps its own credentials, sessions and receipts.
[Enrollment flow][enrollment] · [Connector implementation][fleet].

Independent requests can overlap, subject to capacity and resource locks. One
physical desktop still has one keyboard, mouse and focus. Reachability does not
prove provider login or workload admission. Remote tool access also does not
turn separate installations into an automatically replicated memory database.
There is no required fleet size or special cluster purchase.
[Resource boundaries][fleet-limits] · [One-machine contract][one-machine].

## Source and licensing

[BORG's public repository](https://github.com/h3ro-dev/borg) includes its source,
three adapter deltas, model cards and evaluation tooling. Owner-authored BORG
source and those deltas carry the repository's **MIT** grant. Upstream base models
and dependencies retain their own terms. In particular, the **FalkorDB engine is
SSPL v1**; the entire installed stack is not MIT. The MLX Qwen base repositories
identify their models as Apache-2.0. [Third-party notices][notices] ·
[1.7B publisher card][mlx17-card] · [4B publisher card][mlx4-card] ·
[FalkorDB license][falkor-license].

The distribution does not include private training corpora, learned historical
memories or provider accounts. Source availability does not remove provider
subscriptions, API charges or the need to evaluate adapters on your own workflow.
Continue with [SETUP.md](SETUP.md) and [INSTALL.md](INSTALL.md).

[install]: https://github.com/h3ro-dev/borg/blob/d0a55a4/docs/INSTALL.md#L7-L18
[one-machine]: https://github.com/h3ro-dev/borg/blob/d0a55a4/conductor/README.md#L18-L21
[memory-store]: https://github.com/h3ro-dev/borg/blob/d0a55a4/memory/bin/mem0ctl#L602-L637
[graph-time]: https://github.com/h3ro-dev/borg/blob/d0a55a4/graph/bin/graphiti-mcp-server#L218-L238
[hooks]: https://github.com/h3ro-dev/borg/blob/d0a55a4/installer/clients.py#L104-L140
[routing]: https://github.com/h3ro-dev/borg/blob/d0a55a4/conductor/README.md#L45-L76
[coordination]: https://github.com/h3ro-dev/borg/blob/d0a55a4/coordination/README.md#L1-L21
[fleet]: https://github.com/h3ro-dev/borg/blob/d0a55a4/connector/fleet_tools.py#L171-L260
[brain]: https://github.com/h3ro-dev/borg/blob/d0a55a4/installer/brain_service.py#L79-L138
[projection]: https://github.com/h3ro-dev/borg/blob/d0a55a4/graph/bin/graph-recall-projector#L1-L7
[memory-principle]: https://github.com/h3ro-dev/borg/blob/d0a55a4/README.md#L55-L67
[capture-opt-in]: https://github.com/h3ro-dev/borg/blob/d0a55a4/graph/bin/ollama-schema-shim#L20-L34
[model-default]: https://github.com/h3ro-dev/borg/blob/d0a55a4/installer/config.py#L169-L195
[config-models]: https://github.com/h3ro-dev/borg/blob/d0a55a4/installer/config.py#L190-L199
[ollama-env]: https://github.com/h3ro-dev/borg/blob/d0a55a4/installer/services.py#L22-L35
[model-lock]: https://github.com/h3ro-dev/borg/blob/d0a55a4/installer/models.lock.json#L1-L15
[model-check]: https://github.com/h3ro-dev/borg/blob/d0a55a4/installer/dependencies.py#L83-L104
[qwen-publisher]: https://ollama.com/library/qwen3:4b
[nomic-publisher]: https://ollama.com/library/nomic-embed-text
[services]: https://github.com/h3ro-dev/borg/blob/d0a55a4/installer/services.py#L38-L89
[python]: https://github.com/h3ro-dev/borg/blob/d0a55a4/installer/dependencies.py#L23-L49
[runtime-lock]: https://github.com/h3ro-dev/borg/blob/d0a55a4/installer/runtime-lock.json
[codex-pin]: https://github.com/h3ro-dev/borg/blob/d0a55a4/installer/npm/package.json#L1-L8
[browser-lock]: https://github.com/h3ro-dev/borg/blob/d0a55a4/installer/browser-lock.json#L1-L13
[python-pins]: https://github.com/h3ro-dev/borg/blob/d0a55a4/installer/requirements.in#L1-L15
[falkor-service]: https://github.com/h3ro-dev/borg/blob/d0a55a4/installer/falkor_service.py#L8-L24
[ollama-faq]: https://docs.ollama.com/faq#how-does-ollama-handle-concurrent-requests
[ollama-context]: https://docs.ollama.com/context-length
[adapter-manifest]: https://github.com/h3ro-dev/borg/blob/d0a55a4/adapters/MANIFEST.json
[adapter-prepare]: https://github.com/h3ro-dev/borg/blob/d0a55a4/installer/adapters.py#L72-L123
[capture-card]: https://github.com/h3ro-dev/borg/blob/d0a55a4/adapters/capture-extraction-qwen3-4b/README.md#L29-L47
[base-17]: https://github.com/h3ro-dev/borg/blob/d0a55a4/adapters/MANIFEST.json#L13-L59
[base-4]: https://github.com/h3ro-dev/borg/blob/d0a55a4/adapters/MANIFEST.json#L110-L160
[mlx-lora]: https://github.com/ml-explore/mlx-lm/blob/main/mlx_lm/LORA.md#memory-issues
[enrollment]: https://github.com/h3ro-dev/borg/blob/d0a55a4/docs/SETUP.md#L250-L274
[fleet-limits]: https://github.com/h3ro-dev/borg/blob/d0a55a4/docs/SETUP.md#L276-L299
[notices]: https://github.com/h3ro-dev/borg/blob/d0a55a4/THIRD_PARTY_NOTICES.md#L13-L71
[mlx17-card]: https://huggingface.co/mlx-community/Qwen3-1.7B-4bit
[mlx4-card]: https://huggingface.co/mlx-community/Qwen3-4B-Instruct-2507-4bit
[falkor-license]: https://github.com/FalkorDB/FalkorDB/blob/master/LICENSE.txt
