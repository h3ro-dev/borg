# Public distribution boundary

This source release is intended to let a new owner install an independent
BORG: their own memory and graph stores, conductors, adapters, Agent Inbox,
Beads project, and native computer connector, using only their credentials and
their data. It does not clone the original owner's working state.

The public payload contains source, configuration templates, exact installer
locks, and three adapter weights. It contains no provider login, API key,
cookie, owner or worker registry, host roster, account inventory, Inbox or
Beads database, memory rows, graph snapshot, learned historical memories,
private training pairs, client corpus, or private work report.

## Included inventory

The release guard generates the authoritative list of paths, byte counts and SHA-256
hashes for the exact release. The implementation includes:

| Component | Included implementation |
|---|---|
| Installer | Managed runtimes, exact dependency/model locks, private configuration, service lifecycle, client setup, health checks and release guard |
| Memory | Native Mem0 CLI and scoped MCP servers, recall/capture hooks, ingestion, retention and consolidation |
| Graph | Scoped Graphiti feed/backfill, recall projection, schema shim, supervision and canary tools |
| Conductors | Codex app-server bridge, account-aware routing/admission, Grok and launch-bus adapters, bootstrap and protocol tests |
| Coordination | Native Inbox Hub, identities, grants, leased messages, assignments, discoveries and Beads bootstrap |
| Connector | Native files/processes/jobs/browser/UI/SSH tools, credential handles, authenticated MCP, OAuth gateway and watchdog |
| Adapters | Three LoRA weight files, configurations, model cards and a checksummed base-model manifest |
| Training | Optional dataset builders, training chains, evaluation and promotion checks; no private training pairs |

The four accepted native Codex hooks are installation-owned registrations,
not copied profiles. Every new installation creates its own hook paths and
trusted configuration.

## Verified reference platform

Release acceptance on 2026-09-14 verified two fresh, independent installations
on macOS with Apple Silicon. Both ran the same unmodified source release with:

- managed Node.js `24.21.0`;
- CPython `3.12.12`;
- OpenAI Codex CLI `0.146.0`;
- exact hashed Python dependencies from `installer/requirements.lock`;
- native Qdrant and FalkorDB storage;
- pinned Ollama model downloads;
- semantic search through the connector before the embedding model was loaded;
- the native coordination and conductor protocol suites; and
- a 60-tool native MCP proxy with no Desktop Commander dependency.

Native workflow checks saved and recalled a synthetic fact, captured a second fact
through the installed Codex hooks, extracted temporal graph relationships, and
projected them into semantic recall. File write/read, a durable process job, and a
Chromium page load and button click completed through the connector. Separate Inbox
identities exchanged and acknowledged a message; each Beads store retained its own
project and issue even with inherited environment variables pointing to the other
installation. Cross-installation connector and Inbox credentials were rejected.

All ten local services in each installation were stopped and started. Native
readback then verified saved memories, graph relationships, projected recall,
completed jobs, messages and Beads work. Replaying the same capture event did not
create another memory. Rerunning the complete installer in both homes returned
success and preserved identities, credentials, source hashes and stored point
counts. The three optional adapters also passed native model-load
and generation checks; that evidence does not promote them into extraction duty.

This is not a claim of Windows, Linux, or Intel-macOS acceptance. Some runtime
locks contain publisher artifacts for other systems, but those pins are not
native acceptance evidence. Provider sign-in, public OAuth acceptance in ChatGPT,
and operating-system UI permissions remain each new owner's setup steps.

The subsystem-local `memory/requirements.txt`, `graph/requirements.txt`, and
`training/requirements.txt` describe their extracted or optional contexts.
The installer locks are authoritative for the installed product and should be
the only dependency source used by the public bootstrap.

## New-owner setup and optional services

The default installation creates new local state. It must not discover or
adopt an existing owner's home, policies, machine list, accounts, credentials,
or data directories.

- Codex, Claude, Grok, and other providers are optional. Each provider is
  disabled until the new owner performs its native sign-in or supplies its own
  supported credential and explicitly enables it.
- The MLX adapters require Apple Silicon, MLX/MLX-LM, and the exact immutable
  Hugging Face revisions in `adapters/MANIFEST.json`. Base model files are
  downloaded separately and verified; large base weights are not bundled.
- The local memory and graph services use the installer-pinned Qdrant,
  FalkorDB/FalkorDBLite, and Ollama components. Their separate licenses are in
  `THIRD_PARTY_NOTICES.md`.
- Remote MCP access is optional. A new owner supplies a domain, Cloudflare
  account, Access policy, OAuth identity, tunnel configuration, and DNS route.
  No original domain, audience, issuer, email, or tunnel token is reusable.
- Native file and headless browser tools run under the installing user. macOS UI
  operations require the relevant Accessibility, Screen Recording or Automation
  permissions from that owner.
  BORG does not install or proxy Desktop Commander.
- Inbox and Beads state are generated per installation. A new Hub creates new
  principals, grants, secret files, request receipts, and work stores; none are
  copied from the source repository.

New-owner memory and training data remain the new owner's property and stay in
their configured local stores. Enabling ingestion or training is a separate,
explicit choice. The shipped adapter weights encode no included historical
memory database and do not supply a prior owner's recall history.

## Publication guard

Export the committed public source to a temporary directory. Git metadata is
deliberately excluded from a distribution, so do not scan the working checkout:

```sh
borg_source="$(mktemp -d)"
git archive HEAD | tar -x -C "$borg_source"
python3 -B "$borg_source/installer/release_guard.py" --root "$borg_source" \
  --inventory "$borg_source/RELEASE-INVENTORY.json"
```

Exit `0` is required for publication. Exit `1` reports findings or an exact
inventory mismatch; exit `2` reports invalid guard configuration. Findings
contain relative paths, line numbers, rule names, and hashes only—the matched
source text and secret-like values are never printed.

When preparing a new release, export its exact proposed Git tree to a fresh
directory. After reviewing that source and confirming all three manifest-declared
weights are present, generate and verify its deterministic inventory:

```sh
python3 -B "$borg_source/installer/release_guard.py" --root "$borg_source" \
  --write-inventory "$borg_source/RELEASE-INVENTORY.json"
python3 -B "$borg_source/installer/release_guard.py" --root "$borg_source" \
  --inventory "$borg_source/RELEASE-INVENTORY.json"
```

The inventory covers every regular file by normalized relative path, byte
count, and SHA-256, refuses symlinks and unexpected binaries, verifies the
three adapter weights against `adapters/MANIFEST.json`, and rejects missing,
changed, or additional files. If an inventory is stored inside the source
tree, the guard records and validates its explicit `excluded_self` path.

Public author attribution or a necessary non-secret fixture that triggers a
heuristic can be approved only with an exact rule, path, line number, full-line
SHA-256, and reason in a `borg-release-guard-allowlist/v1` JSON document. Stale
entries fail. Credentials, private state/artifacts, private user paths,
implicit owner data/policy sources, private network addresses, and weight
integrity failures cannot be allowlisted. Reserved example domains and clearly
marked synthetic/test values are accepted without weakening secret patterns.

The guard is a release control, not a guarantee that arbitrary source text is
safe. Review the exact candidate diff and third-party notices before the
integration owner creates the public artifact.
