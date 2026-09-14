# Install an independent BORG

BORG installs a private local brain, native computer/browser connector, conductor,
Agent Inbox and Beads workspace. Each home has new identities, credentials and empty
data stores. It never joins another installation automatically.

## Requirements

The current native acceptance target is macOS on Apple Silicon. Allow at least 20 GB
of free disk for runtimes, caches and initial models, with additional space for your
data. The model uses an explicit 16,384-token context by default; increasing it raises
memory use. Linux and Intel Mac downloads are pinned but full native acceptance on
those platforms remains pending. Windows is unsupported.

You need an ordinary user session, HTTPS access to the upstream package registries,
and your own provider account to run cloud agents. macOS asks for its normal
Accessibility, Screen Recording or Automation permissions when UI operations need
them. Browser automation uses the included Chromium and its own profile.

## Install

```sh
git clone https://github.com/h3ro-dev/borg.git
cd borg
./install.sh --owner yourname
"$HOME/.borg/bin/borg" auth codex
"$HOME/.borg/bin/borg" doctor
```

Use a stable lowercase owner identifier. Setup verifies pinned downloads before
execution, installs managed Python and Node under the home, creates services, pulls
locked extraction/embedding models, and configures four native Codex recall/capture
hooks. No existing provider profile is copied. `auth codex` performs the provider's
normal login and pins the resulting account to this conductor.

Choose a different home or existing project directories explicitly:

```sh
./install.sh --home "$HOME/my-borg" --owner yourname --port-base 20760 \
  --projects "$HOME/Projects/example"
```

The home must be an absolute canonical path without symlinks, owned by you and mode
0700. The default project workspace is `BORG_HOME/projects`. Ports occupy the eleven
consecutive numbers starting at `--port-base`. Another installation needs a different
home and a non-overlapping port range.

`--no-start` installs source, dependencies and native configuration without launching
services. Run the same installer again without that flag to pull the models,
initialize the empty stores and complete setup.

## What setup creates

| Location under BORG_HOME | Contents |
|---|---|
| `config.json` | Owner, instance UUID, ports, project roots and model settings |
| `app/` | The exact installed source and its hash manifest |
| `runtime/`, `cache/` | Managed runtimes, browser and verified downloads |
| `models/` | Local model weights |
| `mem0/` | Semantic memory, scoped credentials, history and capture tools |
| `graphiti/` | Temporal graph, graph scope registry and recall projector |
| `borg-context/` | Native connector, operation receipts, jobs and browser state |
| `conductors/` | Independent provider profiles, account pin and conductor configuration |
| `coordination/`, `beads/` | Native Inbox identities/grants/messages and project work store |
| `logs/`, `services/` | Service logs and generated service definitions |

Services bind to loopback. macOS service labels and Linux user-service names include
this installation's UUID. The background brain incrementally feeds saved memories
into the real temporal graph and projects relationships for recall. The watchdog
checks the connector and, if configured, this installation's gateway and tunnel.

The source and all three adapter weights are included. Adapters remain inactive until
an owner validates them against a reproducible base model and the actual extraction
workflow. The default brain uses the locked Qwen model. Training-pair capture and
optional historical-data imports are disabled unless explicitly configured.

## Inspect and operate

```sh
"$HOME/.borg/bin/borg" doctor
"$HOME/.borg/bin/borg" tools browser_
"$HOME/.borg/bin/borg" call borg_status
"$HOME/.borg/bin/bd" list --json
"$HOME/.borg/bin/borg" stop
"$HOME/.borg/bin/borg" start
```

`tools` reads live MCP schemas; `call` accepts `--arguments` containing a JSON object.
Both use the installation's private local credential. Never put credentials in tool
arguments. Receipt state distinguishes confirmed completion from an unknown outcome;
inspect a receipt before repeating a write after a lost connection.

`doctor` distinguishes a responding service, authenticated memory/Inbox, verified
storage/model identity, trusted hooks and a pinned provider login. A local setup can
finish with `provider_sign_in_required`. Optional public OAuth still needs a real
client sign-in test. A healthy service process alone does not prove an extraction or
remote action succeeded.

## Reruns, recovery and removal

Rerunning the same source release preserves owner credentials, identities and data.
It refuses an unknown existing home, changed installed application source, foreign
ports or conflicting configuration. Inspect the exact component's log before
repairing it. A missing dependency can be restored from its verified cache.

This installer does not yet migrate an existing installation to a different source
release. Preserve the old home and use a separate home to evaluate the new release.
Do not overwrite a live data directory or copy another owner's credentials.

To stop BORG, run `borg stop`. Data remains available for backup or later restart.
After verifying a backup and stopping services, remove only your chosen BORG home
and the service definitions whose UUID matches its `config.json`. Global provider
profiles and unrelated services are outside the installation.

For web ChatGPT access, continue with [WEB.md](WEB.md). See
[DISTRIBUTION.md](DISTRIBUTION.md) and [third-party notices](../THIRD_PARTY_NOTICES.md)
for the included components, platform evidence and licensing boundaries.

## Prepare the optional MLX adapters

```sh
"$HOME/.borg/bin/borg" adapters list
"$HOME/.borg/bin/borg" adapters prepare all
```

Preparation creates a separate, locked MLX Python environment and downloads the
immutable base-model files and tokenizers recorded in the adapter manifest. It checks
all hashes and returns a runnable generation command for each adapter. Allow roughly
3.3 GB for the two base models, plus their runtime and caches. The two 4B adapters
share one verified base-model download within your installation.

Preparation does not activate adapters or train on your data. The historical training
commits were not recorded, so matching a reproducible release pin is followed by a
real compatibility and extraction canary before promotion. See the model cards and
training evaluation tools for the evidence required.
