# Conductors: Turning ChatGPT Seats into a Steerable Worker Fleet

*How the estate wraps `codex app-server` in a tiny HTTP control plane, runs one conductor
per account, and wires the fleet into the Borg's shared memory.*

## Why a conductor exists

`codex exec` fires a worker you cannot talk to until it exits. Real programs need the
opposite: start a thread, watch its events, **steer it mid-flight** when it drifts,
interrupt it when it's wrong, resume it after a crash — and do all of that across many
ChatGPT-account seats without babysitting terminals.

`conductor/conductor.mjs` is that control plane: a zero-dependency Node process that owns
a `codex app-server` child over stdio JSON-RPC and exposes local HTTP:

```
POST /thread/start      {cwd, model?}          → threadId
POST /turn/start        {threadId, text}       → turnId
POST /turn/steer        {threadId, expectedTurnId, text}   ← the whole point
POST /turn/interrupt    {threadId}
POST /thread/resume     {threadId}
GET  /events?threadId=&afterSeq=               → ordered event stream
GET  /status · GET /threads                    → live census
```

Approvals are denied-and-logged by default — a conductor lane is a worker, not a
privilege-holder.

## Fleet topology

One conductor per Codex account profile (`CODEX_HOME=<profile> node conductor.mjs --port
47xx`). The reference estate runs eight lanes on adjacent ports — separate auth, separate
quota pools, one operating pattern. Sub-agent fan-out happens *inside* a lane (the manager
thread spawns its own workers via the harness's own multi-agent machinery), so the
conductor stays a thin, reliable pipe.

A ninth conductor speaks the same idea in a different accent: the estate's Grok CLI runs
behind its own conductor with its own protocol. Same lesson — every agent CLI eventually
wants a durable HTTP control plane in front of it.

## Memory integration (the Borg part)

Two wires connect a lane to the shared brain:

1. **MCP registration** — each profile's `config.toml` carries the `[mcp_servers.mem0]`
   stanza, so a worker can *actively* search and write the shared store mid-task. Eight of
   sixteen profile homes carried it at census.
2. **Lifecycle hooks** — a launch shim injects narrow-scope recall at session start and
   fact capture at session end. Two shims exist deliberately: a full-grant one for the
   operator's own seat and a narrow one (project/ops scopes only) for automation lanes —
   the scoped v2 MCP server enforces the difference. At census the hook was deployed to
   zero of the ten tracked conductor homes — the mechanism existed, the rollout hadn't
   happened. Published here as-is because honest gaps age better than quiet ones.

Workers running in sandboxes without network access use the third wire: write facts to a
file, and a receiving tool on the host re-filters and stores them. Same drop-filters,
defense in depth.

## The gotchas that cost us real time (so they don't cost you any)

- **Complex JSON through shell quoting silently no-ops.** A `curl -d` with nested quotes
  can start a thread and *not* fire the turn — no error anywhere. Build the JSON with a
  real serializer into a file and `curl --data-binary @file`. Prove the turn with its
  returned `turnId`, never with the HTTP 200.
- **`/threads` returns `{"data":[...]}`** — print raw before parsing anything on first
  contact.
- **Account switches kill every in-flight thread.** The long-lived app-server holds the
  old token; when the seat re-authenticates, threads die with a token-refresh error. The
  recovery ritual, proven: restart the conductor first, run a tiny canary turn to prove
  auth, then mass-relaunch lanes from their on-disk briefs. Design lanes so relaunch is
  safe (briefs on disk, fresh workspaces).
- **Only conductor-launched lanes are steerable.** A plain `codex exec` cannot be adopted
  mid-flight. If you might ever want to steer it, launch it under the conductor.
- **Guard steering with `expectedTurnId`.** Steering "whatever turn is live" is how a
  correction lands on the wrong work.
- **Pipes buffer, workers "hang."** Driving a CLI worker through a pipe with a `| tail`
  can buffer its output until the process dies with nothing. Event streams over HTTP are
  the observability path; stdout is not.
- **Version-pin the harness.** A CLI upgrade broke the multi-agent API once
  mid-program. The conductor makes the pin one process instead of many terminals.

## Testing a conductor honestly

The smoke test that counts, end-to-end: start a manager thread → it spawns two parallel
sub-workers → steer the manager mid-turn → verify the final artifacts reflect the original
task *and* the steer → keep the receipts. A conductor that hasn't proven mid-flight
steering with an artifact diff hasn't proven anything.

For fleet health: a census loop over `/status` on every port, plus a dispatch ledger that
resolves each attempt against the rollout transcript (`task_complete` in the transcript is
the truth; "the conductor said running" is a lead). The estate's ledger caught a dead lane
the same morning this paper's census did — the pattern works.

## Install

See `docs/INSTALL.md` §5. Short version: Node 18+, a Codex CLI login per profile,
one `conductor.mjs` per `CODEX_HOME`, ports of your choosing, and the mem0 stanza in each
profile's config if you want the fleet remembering as one.
