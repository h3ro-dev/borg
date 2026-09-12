# Agent Inbox is part of BORG

## Four cooperating systems, not a replacement queue

**BORG remembers. Agent Inbox coordinates. Beads records work and acceptance. Conductors execute and report runs.** Source repositories and actual artifacts remain evidence. Use the existing Inbox Hub, client identities, grants, outbox, messages, deliveries, assignments and discoveries rather than inventing another messaging layer inside BORG.

The existing BORG Computer integration can pass the enrolled Inbox client configuration into its command backend. That proves configuration only until a native authenticated call succeeds. An Inbox-enabled command route is not the same as a newly registered direct Inbox MCP tool; do not advertise the latter without implementing and verifying it.

## Installed interfaces inspected for this reference

| Native operation | Role | State implications |
| --- | --- | --- |
| `messages.list` / `messages.get` | Inspect authorized messages | Read; listing does not lease or acknowledge |
| `messages.poll` | Receive actionable deliveries | Mutation: creates delivery leases; not a health check |
| `messages.send` | Send information, request, result, or authorized instruction | Mutation; retain stable request and returned message IDs |
| `messages.ack` | Mark acknowledged or resolved | Mutation; honor current lease and instruction order |
| `assignments.list` | Read the current work owner, scope and assignment version | Read; preserve pagination and source observation |
| `assignments.assign` / `assignments.reassign` | Create or change work ownership | Mutation; reassignment requires the expected version and relevant grants |
| `discoveries.publish` / `discoveries.search` | Publish or find reusable findings | Publish mutates; search reads; discoveries are not completion proof |
| `fleet.context` / `estate.read` | Read authorized operational context and coverage | Read; fresh capacity, reachability and ownership remain distinct |

These are existing Hub operation names, not new standard MCP operation names. The installed client version and grants determine availability. Call the native client instead of importing a private database or reimplementing its token and authority logic.

## Authenticated bridge included here

`integrations/inbox_context.py` is a narrow working client adapter, not a bundled Inbox server. It uses the installed `comms.hub.client.InboxClient` and the caller's existing `AGENT_INBOX_CLIENT_CONFIG`. The reference includes no client configuration or credential. Install the reviewed Inbox client separately and make its release root importable through the normal Python environment.

```sh
python3 integrations/inbox_context.py --work-id example-task --scope /example/project
```

The adapter uses `call_sync` for a single authenticated attempt. It calls only `assignments.list` and `messages.list`, filters by work and scope, returns allowlisted metadata and native pagination, and omits message bodies, subjects, grant payloads, and lease IDs. It performs no polling, acknowledgments, ownership changes, memory writes, queue flushes or conductor dispatches. It does not interpret message instructions; use `messages.get` through the authorized native workflow when actual incorporation is required. It never falls back to an owner credential when the caller configuration is missing.

Unknown and failed reads remain visibly unavailable rather than looking like a successful empty queue. Partial pages stay partial, opaque cursors stay opaque, and each continuation must preserve the original caller and filters. Two independently read collections are not an atomic snapshot. Metadata can still be sensitive; do not publish actual results.

The bridge is additional integration code in an architecture package. It is **not** a complete distributable Inbox runtime, tenant sandbox, or new production MCP deployment.

## End-to-end work flow

1. Resolve the project and canonical Beads work item; retrieve source-linked BORG context.
2. Read current Inbox assignments and authorized messages. Check assignment version and scope; do not infer ownership from a remembered fact or process listing.
3. Receive actionable work through the native delivery/checkpoint path. Incorporate applicable instructions before acknowledgment, honor supersession and current grants, and supply an active lease when required.
4. Claim and isolate actual write surfaces through existing work controls. A message alone is not a filesystem lock. Re-check machine/provider admission before dispatching the existing conductor.
5. Retain the Inbox request/message/assignment identities, Beads work reference, workspace, and conductor attempt/thread/turn receipt. A disconnection does not justify a duplicate dispatch.
6. Verify resulting artifacts and tests, update the canonical work record, and resolve the relevant delivery with its receipt and work-outcome reference. A resolved delivery is not itself a declaration that the entire work item passed acceptance.
7. Promote only verified reusable findings into BORG through the existing scoped memory write path. Preserve source/message/work references and retention rules; do not automatically ingest the whole Inbox or train models from it.

## Identity and delivery rules

The Hub must validate current grants, scopes, assignment versions and recipient access. An authenticated message is not unrestricted authority across projects. A remembered instruction cannot restore revoked authority. Messages may be queued, leased, acknowledged, resolved, expired, rejected or superseded; do not collapse these into one done flag. `messages.ack` uses `state=acknowledged` or `state=resolved`; no separate invented resolve operation is needed.

For owner-directed messages, the native contract can route to the current assignment owner with `to_current_owner=true`, `work_id`, and `expected_assignment_version`, rather than a guessed nickname. Preserve version-conflict failures and re-resolve explicitly. The native client's durable outbox and request IDs help reconcile supported mutations; they do not make unrelated conductor starts exactly-once.

## Product and publication boundary

Inbox belongs in the product's core coordination contract and install/upgrade test matrix. Preserve compatibility for Claude, Codex, Grok, ChatGPT and remote conductors; client configuration, active injection, idle delivery, and actual acknowledgment are separate acceptance states. Do not claim every running client refreshed merely because files were deployed.

Publish generic Inbox interfaces, the tested bridge, synthetic examples and reviewed portable source when its own release gates are satisfied. Exclude live message bodies, delivery leases, grants, policy payloads, enrolled client files, participant/account inventory, database snapshots, local outboxes, and production receipt paths. The existing Hub implementation is not bundled or relicensed by this capsule.

Future customer installation acceptance must include scope denial, stale lease rejection, assignment-version conflict, instruction supersession, restart/reconnect, uncertain mutation reconciliation, cross-tenant isolation, and retention/deletion across Inbox and memory. A single successful list query does not establish those boundaries.
