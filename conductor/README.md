# codex-conductor

Drive [Codex](https://github.com/openai/codex) agent threads programmatically —
start them, watch them, and **steer them while they run** — from any outer
orchestrator: a script, a human at a terminal, or another AI agent.

Codex's `app-server` is the same engine the Codex Desktop app runs on. It speaks
newline-delimited JSON-RPC over stdio and exposes the full thread lifecycle:
`thread/start`, `turn/start`, **`turn/steer`**, `turn/interrupt`,
`thread/resume`, `thread/fork`, plus a live notification stream of everything
every thread (and every sub-agent it spawns) is doing.

`conductor.mjs` wraps one `app-server` child process and exposes that power as
a small local HTTP API, so orchestration becomes `curl`:

```bash
node conductor.mjs &                # http://127.0.0.1:4747

# start a manager thread in a workspace
curl -s -X POST localhost:4747/thread/start -d '{"cwd":"/path/to/work"}'

# fire a long-running turn (manager can spawn its own sub-agents)
curl -s -X POST localhost:4747/turn/start -d '{"threadId":"…","text":"…"}'

# change requirements WHILE it works
curl -s -X POST localhost:4747/turn/steer \
  -d '{"threadId":"…","expectedTurnId":"…","text":"STEERING UPDATE: …"}'

# watch everything it and its sub-agents do
curl -s 'localhost:4747/events?threadId=…&afterSeq=0'
```

## Why

Agent-orchestrating-agents topologies need a control plane: the outer
orchestrator must be able to dispatch inner orchestrators, observe them
mid-flight, and correct course without killing the run. Codex's open app-server
protocol provides exactly that — this repo is the thin adapter that makes it
scriptable.

Proven end-to-end (see `docs/receipts/2026-06-12-smoke-1/`): a conductor-launched
manager thread spawned two parallel sub-agents, was steered mid-turn with a new
requirement, and produced artifacts reflecting both the original task and the
steering update.

## API

| Endpoint | Body | Purpose |
|---|---|---|
| `GET /status` | — | health, known threads, last event seq |
| `GET /threads?limit=` | — | `thread/list` passthrough |
| `GET /events?threadId=&afterSeq=` | — | buffered notification stream |
| `POST /thread/start` | `{cwd, model?, instructions?, sandbox?, config?}` | new thread (defaults: `workspace-write`, `approvalPolicy: never`) |
| `POST /thread/resume` | `{threadId, cwd?, sandbox?, approvalPolicy?}` | reattach an existing thread; defaults to `read-only` + `never` |
| `POST /turn/start` | `{threadId, text, effort?}` | fire a turn; returns `turnId` once started |
| `POST /turn/steer` | `{threadId, expectedTurnId, text}` | inject guidance into a **running** turn |
| `POST /turn/interrupt` | `{threadId, turnId}` | stop a running turn |
| `POST /rpc` | `{method, params, timeoutMs?}` | raw JSON-RPC escape hatch |

## Multi-account sharding

Each conductor instance owns one `CODEX_HOME` (one account, one rate-limit
pool). Run several:

```bash
CONDUCTOR_PORT=4747 node conductor.mjs &                       # default account
CONDUCTOR_PORT=4748 CODEX_HOME=~/.codex-acct2 node conductor.mjs &
```

For new work across several account lanes, use `conductor-usage-router.mjs`
instead of choosing a port by hand. It refreshes native provider headroom,
checks the exact configured account and profile, excludes lanes that lack the
requested capability, serializes concurrent starts, reranks the full fleet, and
journals the attempt before starting a thread:

```bash
node conductor-usage-router.mjs rank
node conductor-usage-router.mjs dispatch \
  --cwd /path/to/work \
  --prompt-file /path/to/task.txt \
  --work-id ISSUE-123
```

See `docs/conductor-usage-routing.md` for setup, model-bucket mapping, failure
behavior, and the direct-port bypass boundary.

## Requirements

- Codex CLI ≥ 0.133 on PATH (tested on 0.137.0), signed in (`codex login`)
- Node 20+
- For sub-agent spawning: `features.multi_agent_v2` enabled in Codex config.
  Note: Codex CLI 0.138–0.140-alpha currently reject this flag server-side
  ([openai/codex#26753](https://github.com/openai/codex/issues/26753)); 0.137
  works.

## Safety posture

- HTTP binds to `127.0.0.1` only.
- Threads default to `workspace-write` sandbox, scoped to their `cwd`.
- If the server requests an approval, the conductor **denies it and logs** —
  it never silently grants.
- Full audit trail: every notification is appended to `logs/` per thread.

See `docs/protocol-notes.md` for how the protocol was mapped and the dead ends
(daemon control socket, remote-control mode) so you don't repeat them.

## Passive agent workspace registry

`agent-registry.mjs` inventories explicitly configured filesystem roots and
produces a read-only workspace/process snapshot plus a dry-run retirement
report. It is deliberately separate from the live conductor server: running it
does not start, steer, interrupt, archive, or message any Codex or Claude task.

```bash
node agent-registry.mjs \
  --config agent-registry.config.example.json \
  --output-dir artifacts/agent-registry

node --test
```

The registry fails closed. An unregistered clean checkout is `UNKNOWN`, not a
deletion candidate. A dirty inactive checkout is `PARKED`. Only an explicitly
archived, clean, inactive, non-canonical workspace with verified containment,
a recovery receipt, and the configured decision owner's retirement approval
can be reported as `RECLAIMABLE`; the tool still performs no deletion.

See `docs/agent-registry.md` for the lifecycle contract and configuration,
`docs/agent-admission.md` for prospective worktree and heavy-job admission
control, `docs/agent-search-hygiene.md` for bounded discovery on a clone-heavy
machine, and `docs/agent-first-cleanup-plan.md` for the staged system cleanup
path. Existing lanes are inventoried in place and are never moved merely to
match the prospective path standard.
