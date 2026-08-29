# Codex app-server protocol — field notes

Mapped 2026-06-12 against Codex CLI 0.137.0. Everything here was verified by
probing a live server, not read from docs.

## Transport

- `codex app-server` speaks **newline-delimited JSON-RPC 2.0 over stdio**.
  This is the same engine the Codex Desktop app hosts its threads on
  (`codex app-server --listen stdio://`).
- First request must be `initialize`:

```json
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{
  "clientInfo":{"name":"codex-conductor","version":"0.1.0"},
  "capabilities":{"experimentalApi":true}}}
```

- `experimentalApi: true` opts into the v2 thread/turn methods.
- Server → client **requests** (id + method) are approval prompts
  (`ExecCommandApproval`, `ApplyPatchApproval`, …); respond with
  `{"result":{"decision":"denied"}}` (or grant). Unanswered = hung turn.

## Method surface (v2 highlights)

Generate the full schema yourself — this is the authoritative source:

```bash
codex app-server generate-json-schema --out <dir>
codex app-server generate-ts --out <dir>     # TypeScript bindings
```

Key methods: `thread/start`, `thread/resume`, `thread/fork`, `thread/list`,
`thread/read`, `thread/archive`, `thread/rollback`, `thread/inject_items`,
`thread/goal/set`, `turn/start`, `turn/steer`, `turn/interrupt`.

Shapes that matter:

- `turn/start` params: `{threadId, input:[{type:"text", text}]}`. The RPC
  response may not resolve until the turn ends — treat `turn/started` /
  `turn/completed` **notifications** as the real lifecycle signal.
- `turn/steer` params: `{threadId, expectedTurnId, input:[…]}` — the
  `expectedTurnId` guard makes steering race-safe. Returns `{turnId}`.
  Verified working against a live in-flight turn.
- `thread/start` accepts `cwd`, `model`, `sandbox`
  (`read-only | workspace-write | danger-full-access`), `approvalPolicy`
  (`untrusted | on-failure | on-request | never`), `developerInstructions`,
  `personality`, and a `config` override object.

## Notification stream

Per turn you'll see `item/started` / `item/completed` (+ deltas) with item
types including `reasoning`, `agentMessage`, `commandExecution`, `fileChange`,
and — when the thread spawns sub-agents — **`collabAgentToolCall`** with
`tool: "spawnAgent"`, `senderThreadId`, and `receiverThreadIds` linking parent
to child threads. Sub-agent threads write their own rollout files under
`$CODEX_HOME/sessions/YYYY/MM/DD/` like any other thread.

## Dead ends (so you don't repeat them)

- **`codex app-server daemon start` + `codex app-server proxy`**: the managed
  daemon's control socket (`$CODEX_HOME/app-server-control/…sock`) refused our
  raw JSON-RPC client (broken pipe on first write). The proxy path appears to
  expect a specific client handshake. Owning the child directly over stdio is
  simpler and fully functional.
- **`codex remote-control`**: this is the relay for controlling Codex from
  other devices (it phones home to a relay service), not a local control
  socket. Not what you want for local orchestration.
- **Desktop app threads**: the app hosts its own `app-server` process; its
  stdio belongs to the app, so you cannot steer turns the *app* is running.
  You can, however, `thread/resume` (or `codex exec resume <id> "<prompt>"`)
  any persisted thread when the app isn't actively driving it, and you can
  always read every thread's rollout file live.

## Version gotcha

`features.multi_agent_v2` (the `spawnAgent` collaboration tool) works on CLI
0.137.0 but is rejected server-side on 0.138–0.140-alpha builds with
*"Function 'functions.spawn_agent' declares encrypted parameters but is not
configured for encrypted tool use by this model"*
([openai/codex#26753](https://github.com/openai/codex/issues/26753)). Pin your
conductor's `CODEX_BIN` to a working CLI version; the Desktop app's bundled
binary may differ from your PATH binary.
