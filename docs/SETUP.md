# Set up your own BORG

Start with one machine and your own provider account. An installation creates new
local identities and empty stores. It does not connect to the author's accounts,
machines, data or browser profiles.

## 1. Install and inspect the plan

The verified full-install target is Apple Silicon macOS. Allow at least 20 GB for
initial runtimes, models and caches. Linux and Intel macOS have pinned downloads,
but full native acceptance is pending. Windows is unsupported. Use an ordinary
owner session, an absolute home without symlinks, and outbound package access.

```sh
git clone https://github.com/h3ro-dev/borg.git
cd borg
./install.sh --home "$HOME/.borg" --owner yourname
export BORG_HOME="$HOME/.borg"
"$BORG_HOME/bin/borg" doctor
```

Replace `yourname` with your stable lowercase owner ID. A second installation needs
a separate home and a non-overlapping eleven-port range (`--port-base`). Existing
projects can be supplied with repeated `--projects` arguments. See [INSTALL.md](INSTALL.md).

The onboarding API is available without starting a service. From the source checkout:

```sh
python3 -B - "$BORG_HOME" <<'PY'
import json
import sys
from installer.config import load
from installer.onboarding import plan
print(json.dumps(plan(load(sys.argv[1])), indent=2))
PY
```

Use Python 3.11 or newer, or the installed `"$BORG_HOME/mem0/venv/bin/python"`.
For an initialized but incomplete home, run the same command from the checkout.
`plan(doc)` requires the validated installation document, so before any initialization
use `./install.sh --help` and the installation command above. The CLI integration
exposes this plan as `borg onboard`; releases without that command can use the API.

The ordered steps report observations, missing requirements and commands with explicit
argument arrays, environment overrides and prerequisites. Commands are suggestions;
no installer, login, network request or profile edit runs while generating the plan.
`ready: null` means unverified; `false` identifies a setup gap. A matching receipt or
account pin is labeled **configured**, never live-ready. Do not execute placeholder
arguments such as `<ACCESS_AUDIENCE>` until you have supplied your own values.

## 2. Finish local startup

```sh
"$BORG_HOME/bin/borg" doctor
"$BORG_HOME/bin/borg" call borg_status
```

`doctor` checks authenticated local services, storage, model identity, trusted hooks
and provider state. It can report `provider_sign_in_required` after local setup
succeeds. If you installed with `--no-start`, rerun the **same release's** installer
without that flag: it pulls models and initializes stores. `borg start` alone does
not perform those missing installation steps.

The installer does not migrate a home to a new source release. Preserve an existing
home and evaluate a new release in a separate home. Reconcile owner changes instead
of overwriting them to make a setup check pass.

## 3. Connect Codex and its conductor

```sh
"$BORG_HOME/bin/borg" auth codex
"$BORG_HOME/bin/borg" doctor
"$BORG_HOME/bin/borg" tools
```

`auth codex` runs native login in `conductors/primary/profile`, then pins the observed
account identity to that lane. The conductor must be running for the account pin
readback. Complete sign-in with your own account. Native authentication behavior is
covered by [OpenAI's authentication guide](https://developers.openai.com/codex/auth/).

Installation configures BORG's stdio MCP connection and four recall/capture hooks in
this dedicated Codex profile. It does not configure your global Codex profile. To
open the configured client, use the native runtime path recorded in your own
`conductors/config.json` under `runtime.nodeBin`; the onboarding plan emits the exact
command when it can safely observe that executable. Equivalent shell form:

```sh
# Set NODE_BIN to your installation's runtime.nodeBin, not a system Node.
export NODE_BIN=/absolute/path/to/your/borg/runtime/node/bin/node
CODEX_HOME="$BORG_HOME/conductors/primary/profile" \
  PATH="$(dirname "$NODE_BIN"):/usr/bin:/bin" \
  "$BORG_HOME/runtime/npm/node_modules/.bin/codex"
```

Do not assume the example Node path is your archive layout. The installer pins Node
and Codex in its runtime lock. In the client, inspect the BORG MCP connection and
perform a small reversible operation in a disposable project. Receipt presence does
not replace a real client tool call or current native hook-trust verification.

For explicit conductor and account readback:

```sh
BORG_HOME="$BORG_HOME" "$NODE_BIN" \
  "$BORG_HOME/app/conductor/borg-conductor.mjs" status \
  --config "$BORG_HOME/conductors/config.json"
BORG_HOME="$BORG_HOME" "$NODE_BIN" \
  "$BORG_HOME/app/conductor/borg-conductor.mjs" auth status \
  --config "$BORG_HOME/conductors/config.json" --lane primary
```

These checks are separate from routing admission. Routing requires current capacity,
active work claims, account matching and usable provider allowance. See the exact
[conductor integration contract](../conductor/INTEGRATION.md). A configured lane,
process or listening port does not establish admission.

## 4. Optional Claude and Grok clients

BORG's installer currently bundles and provisions Codex. Install any additional CLI
through that provider's supported distribution and use your own account. The native
commands below were audited against installed CLI help; use your version's help if
its interface differs. BORG does not implement `borg auth claude` or `borg auth grok`.

Create dedicated profiles beneath your own private home before using these examples.
Preserve an existing profile; do not point either variable at a shared profile. Run
these steps from your BORG home so unrelated project configuration is not loaded.

```sh
cd "$BORG_HOME"
umask 077
mkdir -p "$BORG_HOME/providers/claude/profile" "$BORG_HOME/providers/grok/profile"
```

For Claude Code:

```sh
CLAUDE_CONFIG_DIR="$BORG_HOME/providers/claude/profile" claude auth login
CLAUDE_CONFIG_DIR="$BORG_HOME/providers/claude/profile" \
  claude mcp add --scope user --transport stdio borg -- \
  "$BORG_HOME/bin/borg" mcp-stdio --home "$BORG_HOME"
CLAUDE_CONFIG_DIR="$BORG_HOME/providers/claude/profile" claude auth status
CLAUDE_CONFIG_DIR="$BORG_HOME/providers/claude/profile" claude
```

The [Claude CLI reference](https://code.claude.com/docs/en/cli-usage) documents login
and status. `CLAUDE_CONFIG_DIR` selects a separate configuration directory; see the
[environment reference](https://code.claude.com/docs/en/env-vars). Provider-native
credential storage remains the provider's responsibility. In the client, inspect
the MCP connection and test a reversible BORG action. Adding MCP does not register
BORG's four Codex-specific lifecycle hooks in Claude.

For a compatible Grok Build CLI:

```sh
GROK_HOME="$BORG_HOME/providers/grok/profile" grok login --oauth
GROK_HOME="$BORG_HOME/providers/grok/profile" \
  grok mcp add --scope user --transport stdio borg -- \
  "$BORG_HOME/bin/borg" mcp-stdio --home "$BORG_HOME"
GROK_HOME="$BORG_HOME/providers/grok/profile" grok mcp doctor
GROK_HOME="$BORG_HOME/providers/grok/profile" grok
```

The audited native help exposes `grok login --oauth`, `grok mcp add` and
`grok mcp doctor`. BORG does not pin or install that CLI, so verify your own version.
MCP diagnostics do not establish provider login, model availability or allowance.
Never copy credentials from another installation or paste bearer values into MCP
arguments: the local stdio bridge consumes this home's credential privately.

### Additional provider conductors

The following boundaries describe this repository's implementation, regardless of
what a newer provider CLI itself may support:

| Provider | Included source | Setup and evidence boundary |
|---|---|---|
| Codex | Native app-server conductor and router | Installer bootstrap, native login/pin and admission checks are integrated. |
| Claude | `conductor/providers/launch-bus.mjs` headless `claude -p` launch | Requires explicit `providers.claude.binary` and enablement in `conductors/config.json`. Supply `CLAUDE_CONFIG_DIR` to the launch process; the bus inherits it. No native thread-status, mid-turn steer or provider allowance routing is implemented. |
| Grok | `conductor/providers/grok-conductor.mjs` and launch bus | Requires explicit CLI/profile, version, readiness evidence and loopback service configuration. No installer service, login orchestration or readiness-marker provisioning flow is supplied. |

For an owner-reviewed Claude launch-bus integration, the real entry point is:

```sh
CLAUDE_CONFIG_DIR="$BORG_HOME/providers/claude/profile" BORG_HOME="$BORG_HOME" \
  "$NODE_BIN" "$BORG_HOME/app/conductor/providers/launch-bus.mjs" \
  /absolute/path/to/your/launch-packet.json
```

The packet supplies `workId`, `runtime: "claude"`, an absolute `cwd` and `prompt`.
This launches real work and may incur provider charges; it is not a setup probe.
Review the source's packet and provider contract before use. This optional bus does
not gain Codex routing admission or lifecycle features by being enabled.

Grok's source consumes `providers.grok` from `conductors/config.json`. Explicitly
configure `grokBin`, `grokHome`, `expectedVersion`, `readinessMarkerPath`, `statePath`
and a distinct loopback port; never rely on its historical home-directory defaults.
Its startup entry point is `BORG_HOME=... "$NODE_BIN"
"$BORG_HOME/app/conductor/providers/grok-conductor.mjs"`. The supplied readiness
check compares a version and a marker; it does not independently verify a live
account or allowance. **Do not invent a marker to make this check pass.** A reliable
owner provisioning and login-evidence workflow is still required, so leave the
provider disabled until that work has been completed and verified.

## 5. Inspect optional inactive adapters

```sh
"$BORG_HOME/bin/borg" adapters list
# Optional Apple Silicon preparation; downloads roughly 3.3 GB plus runtime/cache.
"$BORG_HOME/bin/borg" adapters prepare all
```

The adapters ship inactive. Preparation verifies pinned base files and included
weights, creates a separate MLX runtime, and returns generation commands for canaries.
It does not activate an adapter. Historical training revisions remain unknown;
compatibility and the actual extraction workflow need evaluation before promotion.
See [INSTALL.md](INSTALL.md#prepare-the-optional-mlx-adapters).

## 6. Optional web connector

Follow [WEB.md](WEB.md) using your own Cloudflare account, hostname, tunnel and Access
Managed OAuth application. `borg web` configures owner-supplied identifiers and a
private credential file under `BORG_HOME/cloudflare`; it does not create DNS or
Cloudflare account resources. Then restart the watchdog and start gateway/tunnel
services as shown there.

Local gateway health is separate from successful OAuth in the web client. Verify
sign-in, the current tool list, `borg_status`, and a reversible action in a new chat.
A web connector attaches to your running installation; it does not install BORG or
add machines. Client/workspace restrictions remain in effect.

## 7. Enroll your own machines

BORG can route native tools to multiple explicitly enrolled machines. Each target
runs a resident connector so process, search, job and browser state survives an
SSH bridge closing. Credentials stay in private files on that target. SSH uses
your existing aliases and known host keys; it never accepts an unknown key for you.

On each target, clone this repository and install it in its own empty home:

```sh
./install.sh --owner yourname --no-start
"$HOME/.borg/bin/borg" start connector
"$HOME/.borg/bin/borg" call borg_identity
```

This installs the pinned runtime and prepares local components, but starts only
the connector. It does not download model weights, start memory/model/conductor
services, or sign in to a provider. The full runtime and browser are still installed;
this is not a minimal dependency package. A full running BORG can use its existing
connector instead. Use distinct homes and ports when running multiple installations.

From the target's identity output, record its exact `home`, `owner` and
`instance_id`. On the controlling BORG, replace the example values below with
those facts and your already trusted SSH alias:

```sh
"$BORG_HOME/bin/borg" fleet add build-a \
  --ssh-alias build-a \
  --remote-home /home/yourname/.borg \
  --owner yourname \
  --instance-id TARGET_UUID
"$BORG_HOME/bin/borg" call fleet_hosts
"$BORG_HOME/bin/borg" call fleet_tools --arguments '{"host":"build-a","prefix":"computer_"}'
```

`fleet add` verifies the supplied identity before enrollment. Its private registry
starts empty and contains no SSH password or key. Host IDs are stable routing
names; a changed installation must receive a new ID. `fleet disable build-a`
prevents new calls. Configuration is read on each request; a newly enabled fleet
feature on an older compatible connector may require the restart reported by CLI.

Call the actual discovered tool using an explicit target ID:

```sh
"$BORG_HOME/bin/borg" call fleet_call --arguments '{"host":"build-a","tool":"computer_list_directory","arguments":{"path":"/home/yourname/.borg/projects","depth":1}}'
```

All paths and process/browser/job IDs refer to the selected target. The connector
checks installation identity and the resident process generation at dispatch.
Independent requests may run concurrently; shared files and physical desktops
retain their resource locks. Connection count is not limited to one. Actual work
remains bounded by configured capacity and each machine's resources.

After a timeout or lost response, a change may still be running. Never blindly
repeat it: inspect native state and call target `borg_operations_recent` through
`fleet_call`. The controlling and target receipts are distinct. Cancellation of an
SSH connection does not prove remote cancellation. No exactly-once guarantee is made.

`fleet_hosts` proves current identity and tool discovery. It does not prove OS
permissions, provider login, workload admission or completed work. Optional `--role`
labels describe your topology, not verified capability. Existing `remote_*` SSH
job tools remain available separately.

For agents, configure owner-controlled machine/account lanes, dedicated profiles,
endpoints, account pins and fresh capacity/claims collectors in
`conductors/config.json`, following
[INTEGRATION.md](../conductor/INTEGRATION.md#multi-machine-or-multi-account-extension).
Use `borg_tool_search` on a selected target to discover its native conductor,
coordination and work-store interfaces. A remote file operation is not a conductor
dispatch. An unknown collector or account fails agent admission. No estate roster,
provider profile or credential is imported from another owner.

## 8. Verify OS gates and the real workflow

macOS may request Accessibility, Screen Recording or Automation access for the
specific app performing UI work. Grant only what your chosen workflow needs. A
headless Chromium test does not prove desktop permissions or a signed-in browser.
BORG uses its own browser profile; it does not import another person's session.

Finish by checking `borg doctor`, a real BORG tool call in your selected client and,
if used, web OAuth and a remote job receipt. The onboarding plan intentionally leaves
those live acceptance gates visible after configuration is present.
