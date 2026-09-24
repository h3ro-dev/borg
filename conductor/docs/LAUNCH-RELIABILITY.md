# Worker launch reliability and recovery

The router records an intent before admission scans and provider calls. Inspect it
without another dispatch or provider probe:

```sh
borg-conductor route-status --config /absolute/BORG_HOME/conductors/config.json \
  --cwd /absolute/workspace --work-id stable-work-id
```

Use the workspace (any spelling of it resolves to the same filesystem-canonical
path) and stable work ID supplied to `route`. This is
read-only reconciliation: it does not create, resume, cancel or complete a worker.
The returned receipt contains a bounded phase history, attempt ID, selected lane,
native thread/turn IDs when proved, and error classification. Prompt text is not
persisted, only its SHA-256. `completionVerified` is always false: a start receipt
is not evidence that a task finished, passed review or was deployed.

## States and decisions

| State | Meaning | Recovery |
| --- | --- | --- |
| `ATTEMPTING` | Intent exists; inspect phase (`PREPARING`, `ADMISSION`, `RECHECK`, `THREAD_START_PENDING`). | Do not start another copy. |
| `PRE_START_FAILED` | The recorded attempt failed before a native lifecycle request. | Diagnose the recorded phase. `noStartProven` applies only to this attempt, not all possible workers. |
| `THREAD_STARTED` | Native thread ID exists; turn start is pending. | Reconcile that exact thread. |
| `UNKNOWN_DO_NOT_RETRY` | Thread request may have executed; response was lost, late or invalid. | Verify native state before recovery; no automatic replay. |
| `STARTED_TURN_UNKNOWN` | Thread exists; turn outcome is unknown. | Reconcile exact thread/turn history before any continuation. |
| `DISPATCHED` | Native thread and turn IDs were returned. | Monitor actual worker outcome; do not call the task complete. |
| `NOT_FOUND` (status lookup) | No matching local intent was found. | **Not** proof that no worker started elsewhere. `noStartProven` remains false. |

Identical intents remain blocked on replay. No new retry, fallback, or no-start
claim is inferred from a timeout or a missing receipt. Existing admission selects
another eligible candidate before native mutation when a candidate is unusable;
uncertain lifecycle calls are not retried on a different account or machine.

## Admission and ownership

Missing, Boolean, empty, string and non-finite numeric telemetry are unknown, not
safe zeroes. The same rule applies to native usage measurements. Completed
transport dispatches and uncertain lifecycle states remain active claims until
explicitly reconciled. A different work ID cannot evade an existing workspace
claim; a different workspace cannot evade an active work ID.

Under the dispatch lock the router always rescans its own receipt ledger,
whichever claims collector each machine uses, and checks it together with every
machine's observed claims; only the current attempt is exempt. A command
collector therefore cannot hide this router's submitted or uncertain launches,
and a failed ledger scan refuses dispatch instead of trusting an empty external
list.

Workspace identity is filesystem-canonical. Native realpath resolves symlinks
and, on case-insensitive volumes, letter case; device/inode ancestry also matches
spellings realpath keeps distinct, such as macOS firmlinks and bind mounts.
Receipts, intents, `route-status` and the native thread use the canonical path.
Two workspaces conflict when one is the same directory as, or an ancestor of,
the other. The comparison is segment-aware, so sibling worktrees such as `repo`
and `repo-other` remain independently admissible.

Claimed paths from every machine are resolved on the router host's filesystem,
where `route` validates the workspace; paths reported for different machines are
never assumed disjoint. A claimed workspace that no longer exists still blocks
its ancestors. A claim without `cwd` reserves only its work ID; a malformed or
unresolvable claimed `cwd` refuses dispatch (`WORKSPACE_CLAIM_UNRESOLVED`).
Intents recorded under a lexical path by earlier versions still block replay and
remain visible to `route-status`. Symlinks inside a workspace that point
elsewhere are not traced.

A receipt scan is all-or-error. Malformed JSON, unsafe file permissions, symlinks,
unknown receipt states, active receipts without a work ID or absolute workspace,
oversize files or exceeded bounds never become an empty
or partial successful claims list. The fixed read-only reader runs in a separate
Node process, outside the router's filesystem worker pool. It accepts no shell
commands and never starts agents or writes state.

Reader bounds: 5 seconds by default, 10,000 directory entries, 65,536 bytes per
receipt, 4 MiB total receipt bytes, and 8 MiB captured output. It scans only the
requested directory, not a recursive tree. A timeout reports
`RECEIPT_READ_TIMEOUT` with its stage and target. Signalling the reader is not
reported as proof that the OS process exited. No incomplete scan permits launch.

## Deadlines and limits

`route --stage-timeout-ms N` bounds each admission/provider operation (1–60,000
milliseconds); the default is `routing.timeoutMs`. Independent machine probes
run concurrently. Late thread responses cannot advance to turn creation after
the caller records an unknown outcome.

These are **stage** deadlines, not an end-to-end service-level guarantee. Workspace
validation and identity resolution (including claimed paths), dispatch-lock I/O
and durable state writes can still be delayed by an unhealthy filesystem. The
final ledger rescan is bounded by the reader limits above, not by
`--stage-timeout-ms`. State writes are not raced against a timeout, since a late
write could corrupt the evidence used by a subsequent attempt. Existing lock
contention controls remain. The router never removes an ownership check or
claims it cancelled an external lifecycle request just because its wait ended.

## Verification

```sh
node --test conductor/tests/*.test.mjs conductor/providers/*.test.mjs
```

The regression suite covers missing telemetry/usage, active dispatched claims,
workspace/work-ID conflicts, local-ledger enforcement under a command collector,
workspace alias/case/firmlink/parent/child overlap with a sibling-worktree
control, unsafe/corrupt/oversize receipts, native reader
timeout, pre-start diagnostic persistence, lifecycle timeout with late response,
duplicate suppression, status lookup and the real command-line status path.
Optional native Codex initialization uses `BORG_TEST_CODEX_BIN` with an isolated
unauthenticated profile; it is not a paid inference or production dispatch test.

This portable-router change does not silently replace an estate's separately
installed fleet router, publish new MCP schemas, or install a goal-driven team
controller. An integrator must adopt the exact reviewed source, preserve its
existing machine/account/ownership policy, verify the deployed artifact, and
refresh client tool catalogs separately where needed.
