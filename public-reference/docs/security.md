# Security and publication boundaries

## Profiles

Define distinct read-context, write-memory, operate-workspace, and privileged-desktop capabilities. Default new installations to read-context. Scope grants to a tenant, project, source allowlist, and operation class. An owner-wide private installation can deliberately expose broad local control; that configuration is not an acceptable shared-customer default.

Require authenticated identity at every external gateway and independent local service authentication. Validate signature, issuer, audience, expiration and applicable not-before claims; revoke and rotate through supported credential tooling. Never trust an email or tenant header merely because a reverse proxy forwarded it. Keep OAuth client identity separate from the human's permitted BORG identity.

## Isolation

Use dedicated customer processes/containers/VMs and workspace roots for execution. Namespace vector and graph data independently and test cross-tenant queries, guessed IDs, stale caches, and source fetch. Bind caches and job claims to the same authorization context. A metadata scope label alone does not isolate a shell process with access to all tenants' files.

Desktop automation needs explicit local permissions, exact application/window/page identities, current snapshots, and ownership of shared focus. Unavailable GUI permissions must not stop read-only memory service startup. Never disable operating-system or provider protections to pass a demo.

## Data lifecycle

No customer training by default. Retain original source evidence only under a documented purpose, retention limit, and access rule. Separate operational audit metadata from payload-bearing transcripts. Log tool name, outcome, duration, request correlation, and policy decision; redact credentials before data reaches framework logging handlers.

A deletion request needs receipts for source records, vector/graph projections, caches and backups. A model trained on data is a separate artifact; revoking access to a record is not unlearning. Training rights and deletion consequences must be agreed before capture is admitted to a corpus.

## Release controls

This package uses explicit allowlists, rejects symlinks and forbidden artifact types, inspects text without echoing matching secrets, and emits per-file hashes. Tests use reserved synthetic identifiers. Review the exact archive, not merely the checkout. Additional gates for a full product include secret-scanner history coverage, sensitive-term review, license/SBOM provenance, dependency vulnerability checks, and clean-machine install verification.

The architecture archive excludes Git history and all existing research weights. A full clone or GitHub-generated source archive of the surrounding repository has a different boundary. The builder does not certify that surrounding history, binary tensors, or previously published material.

## Recovery

Keep a prior working configuration and versioned migration plan. Reconcile uncertain writes and dispatched jobs before retrying. Require operation-level idempotency rather than claiming exactly-once behavior from a network transport. Preserve input versions during evaluation, and do not lower gates merely because a demo succeeds.
