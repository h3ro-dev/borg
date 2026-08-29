# The Borg Memory System: One Brain for Every Agent

*How a single-operator estate wired Claude Code, Codex, and Grok into one shared,
self-consolidating memory — and then taught tiny local models to run it.*

## The problem

Every agent session used to start from zero. Facts learned at 2 AM by a Codex lane were
unknown to the Claude session at 9 AM. Multiply by three CLI harnesses, sixteen account
profiles, and five machines, and the estate was paying its smartest models to rediscover
its own history daily.

## The shape of the answer

Two layers, one rule.

**Layer 1 — mem0, the recall layer.** A local vector store (Qdrant, one collection) holding
tens of thousands of extracted facts — 67,405 at census. Facts arrive from everywhere the
estate thinks: session transcripts of all three harnesses, the operator's mailbox, git
history, remote machines' corpora (mined in place by SSH miners; raw transcripts never
leave their box — only filtered facts cross), and live session hooks. Everything passes a
local LLM extractor and a secrets/PII drop-filter before storage. Consolidation runs
nightly ("dream"): duplicates ≥0.90 cosine merge, 0.75–0.90 clusters get contradiction
review, newest wins, every action logged and reversible.

**Layer 2 — Graphiti, the judgment layer.** A temporal knowledge graph (FalkorDB) built
from mem0's facts: entities, relations, valid-time, supersession — the layer that knows a
port *moved*, not just that two ports were mentioned. Local models do the extraction
through a grammar shim that makes JSON schema violations impossible rather than unlikely.

**The rule: memory is never the authority.** Files, ledgers, and databases stay the source
of truth. Both layers are rebuildable read-models over them. This single sentence, written
into the CLI's docstring, is what makes an aggressive automation posture safe.

## The integrations (what "one brain" means concretely)

| Harness | Read path | Write path |
|---|---|---|
| Claude Code | MCP server (scoped v2) + a fast prompt-time recall hook that injects relevant facts | SessionEnd capture hook: transcript tail → extraction → drop-filters → store |
| Codex | Same MCP server registered per account profile (8 of 16 homes at census) | Lifecycle hook exists; deployment across conductor lanes was 0-of-10 at the morning census — and 7 seats were live with per-seat tokens by that same afternoon. Gaps this system records tend to get closed the day they're written down |
| Grok | Same MCP server + a SessionStart inject file (Grok reads a file at start; the hook writes it) plus Grok's native memory config | The *same* SessionEnd capture binary Claude uses — one code path, two harnesses |

Cross-harness notes also travel a deliberately dumb channel: a one-way file mirror and a
two-lane file mailbox between Grok and Claude/Codex. No live injection, no agent puppeting
another — briefs, not commands. Boring is a feature at the trust boundary.

Scoped access arrived with the v2 MCP server: every request is attributed to a principal
and filtered to that principal's allowed scopes (personal scopes default-closed, client
scopes granted narrowly). One store, many privilege levels — the difference between a
hive mind and a leak.

## Teaching small models the job

The extraction workload is the expensive, always-on part — a 27B teacher was measured
running the equivalent of 130–196% of a calendar day under contention. So the estate
distills students:

1. **Harvest** — every extraction the teachers perform is written down as a training pair.
   The decisive version of this is the grammar shim's tee: the proxy in front of the local
   model copies every live request/response to a corpus file, labeled by call shape, at
   zero marginal cost (~168 pairs/hour observed).
2. **Build** — dedupe (one teacher's harvest turned out to be 83.7% duplicates of the
   other's — measured, and a costly lesson), validity and length filters, per-shape
   splits.
3. **Train** — `mlx_lm` LoRA on Apple Silicon, rank 8, checkpoints every 200–300 steps,
   GPU-crash auto-resume (Metal recovery events on a shared GPU are routine, not
   exceptional; the supervisor retries only on the matched crash signature).
4. **Exam** — held-out pairs, JSON validity, entity-agreement vs teacher (the bar: the
   *measured agreement between two teachers*, 0.53 — demanding more would demand the
   student out-agree its own teachers), speed.
5. **Canary** — the real gate. The student and the incumbent run the identical real
   backlog through the real pipeline into isolated graph groups, instrumented per call
   shape — because the pipeline silently treats well-formed-but-wrong-keys JSON as "found
   nothing," format validity alone proves nothing.
6. **Promote or bench.** The best exam scorer in the program (400/400 valid, 0.644
   agreement) was benched by its canary: 1-of-25 episodes, 0/534 on a call shape its
   training never contained. Its successor trains on all six shapes — recorded free by the
   shim. The full economics of all of this are in the cost-audit paper.

## Keeping it alive (the part nobody writes down)

Adapters and memory don't stay healthy by themselves; the estate runs standing machinery:

- **Nightly dream** (03:30): re-seed, incremental ingestion, consolidation, contradiction
  review, morning report. Kill switches are files (`HOOK_OFF`, `DREAM_OFF`) — visible,
  auditable, reversible.
- **Launchd-owned chains** for training and lanes, each stage logging `STAGE <name> <rc>`
  lines that watchdogs key on; supervisors auto-resume from checkpoints on the known GPU
  crash signature and *stop* on anything unrecognized.
- **Safety invariants under unit test** — the consolidation pass has deterministic
  known-good/known-bad canary states; prompt budgets are enforced per model; six test
  files cover hooks, dedup, scope filtering, and the dream cycle.
- **Fallback is doctrine.** The 27B teacher stays warm behind every student; a failed
  canary costs a day, never an outage.
- **Everything self-reports, and self-reports get audited.** Two independent counters
  (a recorder's lifetime stats vs. its append-only file; a harvester's kept-count vs.
  `wc -l`) were both caught under-counting by this program's own audits. The append-only
  file is the authority; in-memory counters lie after restarts.

## What ships in this repo, and what never will

Machinery ships: the CLI, MCP servers, hooks, dream cycle, shim, backfill, exam, canary,
training chains — scrubbed of the estate's paths, hosts, and identifiers. Data never
ships: no vector store, no graph, no training pairs, no session content. The adapters ship
with honest cards and a memorization-probe gate. The estate's own client registry and
anything it names stays home.

## What we'd tell you to build first

The shim. One ~200-line proxy bought three things at once: schema violations became
impossible (grammar), thinking-token corruption disappeared (stripped), and every
production call became labeled training data (tee). It is the highest-leverage component
in the entire system — and the reason the next student costs eleven dollars.
