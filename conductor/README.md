# BORG conductor

This package is the source-only BORG control plane for an independently owned
installation. It preserves the supplied production Codex `app-server` bridge,
including JSON-RPC initialization, role-aware thread start and resume, exact
turn start/steer/interrupt controls, event pagination, native thread status,
approval denial, and private metadata-only logs.

On `POST /thread/resume`, the conductor resumes the exact requested thread and
then performs `thread/read` with complete turns before updating `/status`.
Bookkeeping uses the resume response's effective `cwd`, model, and reasoning
effort rather than the persisted thread's historical cwd. It restores the
latest turn state and native parent/fork/session identifiers, and preserves
the BORG role plus an optional caller-supplied `predecessor` evidence object
across later resumes. Native thread identifiers must agree across the request,
resume response, and readback or the request fails closed.

It adds a portable install contract and a fail-closed router. Machines and
account lanes come only from `conductors/config.json`; the source has
no owner roster, authenticated account, fleet size assumption, private policy,
or external queue. A one-machine, one-lane configuration is valid.

## Runtime contract

- Node exactly `24.21.0`
- Codex CLI exactly `0.146.0`
- Canonical absolute `BORG_HOME`
- Source at `$BORG_HOME/app/conductor`
- Root installer config at `$BORG_HOME/config.json`, preserved byte-for-byte
- Conductor config at `$BORG_HOME/conductors/config.json`, mode `0600`
- Primary Codex profile at `$BORG_HOME/conductors/primary/profile`
- Primary logs at `$BORG_HOME/conductors/primary/logs`
- Node at the root-provided executable inside `$BORG_HOME/runtime/`
- Codex at `$BORG_HOME/runtime/npm/node_modules/.bin/codex`
- HTTP bound only to `127.0.0.1:$PORT`

Fresh installs contain no provider authentication. The owner authenticates the
dedicated profile with native `codex login`, then pins the observed account
identity digest before that lane can receive routed work.

See [INTEGRATION.md](INTEGRATION.md) for the exact bootstrap, start, status,
authentication, ranking, and routing commands. `config.example.json` documents
the complete non-secret configuration schema.

## Routing contract

For each configured lane, a routing attempt obtains and validates:

1. fresh physical machine capacity and fresh active claims;
2. native conductor status, configured endpoint, and dedicated `CODEX_HOME`;
3. the current native account identity and its configured digest pin; and
4. current native allowance windows for the requested model bucket.

Unknown, stale, unreachable, hot, memory-saturated, unclaimed, unauthenticated,
or wrong-account lanes are ineligible. Eligible lanes are ordered by highest
remaining usable native allowance; earliest reset is only the tie break.
Actual provider exhaustion and provider spend controls have separate evidence
codes. No discretionary reservation or allowance floor is created.

Dispatch holds a private lock, persists an intent before admission scans,
rechecks the configured fleet before selection, and refuses duplicate
`{workId,cwd}` intents across process restarts. Workspaces are compared by
filesystem-canonical identity, so aliases and parent/child paths overlap. Active
workspace and work-ID claims, including this router's own receipts under any
claims collector, are checked before native lifecycle calls. An ambiguous thread or turn
response remains `DO_NOT_RETRY` evidence.

Use `route-status --cwd ABS --work-id ID` to inspect saved phase history and
native IDs without contacting providers or launching work. Admission/provider
stages and read-only receipt scans have bounded deadlines; unknown outcomes
never permit automatic replay. See [launch reliability and recovery](docs/LAUNCH-RELIABILITY.md)
for states, scan bounds, timeout controls and remaining filesystem/release limits.

Capacity and claims support local OS observation or an explicitly configured
absolute JSON-producing command. Remote machines therefore require an
owner-installed native collector; absent collectors fail closed. No Desktop
Commander dependency is present.

## Provider boundaries

The supplied Grok conductor and launch bus are retained under `providers/`.
Grok exposes the native lifecycle features present in that source. Claude
headless launch remains available where configured, but native thread status
and mid-turn steer are declared missing rather than emulated. Codex login is
always provider-native and isolated by `CODEX_HOME`.

## Tests

From the installed source directory:

```sh
BORG_TEST_CODEX_BIN="$BORG_HOME/runtime/npm/node_modules/.bin/codex" \
  "$NODE_BIN" --test tests/*.test.mjs providers/*.test.mjs
```

Unit providers are used only for deterministic protocol and refusal cases.
`tests/native-app-server.test.mjs` starts the real configured Codex
`app-server` with a separate empty profile and proves initialization through
the loopback `/status` endpoint; it never logs in or starts a thread.

## Source and release boundary

This directory is a review-ready source artifact. The root installer owns
copying it to `$BORG_HOME/app/conductor`, installing pinned runtimes, service
startup, end-to-end installation verification, and release. See
`THIRD_PARTY_NOTICES.md` for distributable references. Private operational reports
and account inventories are excluded from the distribution.
