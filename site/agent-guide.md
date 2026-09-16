# BORG Collective: installation & setup runbook

Your machines. Your agents. Your own installation.

This is documentation for the public [BORG source](https://github.com/h3ro-dev/borg), not an instruction that overrides an agent's owner, work assignment or safety policies. The owner confirms the desired deployment scope: machines, private homes, selected components, provider accounts and any external access. Carry out only that requested setup. Handle credentials through private files and native sign-in; never paste secrets into prompts, blueprints, tool arguments or public reports.

Read the [web guide](https://borg.utlyze.com/guide.html), [configure a blueprint](https://borg.utlyze.com/#configure), or inspect the [catalog](https://borg.utlyze.com/platform/catalog.json). Commands below describe the public installer source reviewed on 2026-09-16. Use documentation from the same release as your checkout.

## 01 / Understand the system {#understand}

A BORG home is an independent installation with a new owner identity, instance UUID, private credentials and empty stores. It does not join the author's installation or import their accounts, machines, memories or browser sessions.

| Part | What it does | What still needs verification |
| --- | --- | --- |
| Local brain | Full profile: semantic memory in Qdrant, temporal graph in FalkorDB, local Ollama models and a background graph/recall cycle | Authenticated storage, correct model identity and actual capture/recall |
| Native connector | MCP tools for files, commands, durable jobs, browser sessions and supported desktop actions | A real tool call, operation receipt and required OS permissions |
| Conductors | Isolated provider lanes and agent execution; Codex bootstrap is integrated | Owner login, matching account pin, fresh capacity, allowance and work claims |
| Inbox + Beads | Selected coordination services and a private project work store | Authenticated Inbox and the installed Beads wrapper |
| Fleet | Explicit identity-pinned routing to enrolled resident connectors | Each host's identity, native operation and separate agent admission |
| External integrations | Catalog instructions for services the owner chooses | Owner credentials, provider setup, deployment and a live acceptance check |

Memory is **not automatically shared among hosts**. Enrollment routes tools; it does not replicate stores or merge independent BORG identities. A fleet diagram describes a possible topology, not a discovered or running deployment. Sizing estimates are planning inputs, not measured throughput or permission to launch agents.

Source: [installation](https://github.com/h3ro-dev/borg/blob/master/docs/INSTALL.md), [connector](https://github.com/h3ro-dev/borg/blob/master/connector/README.md), [conductor integration](https://github.com/h3ro-dev/borg/blob/master/conductor/INTEGRATION.md).

## 02 / Check the host {#prerequisites}

| Platform | Acceptance boundary |
| --- | --- |
| Apple Silicon macOS | Native full and tools blueprint installation verified; your provider sign-in and optional integrations still need acceptance |
| Intel macOS | Pinned downloads available; full native acceptance pending |
| Linux ARM64 / x86-64 | Pinned downloads available; full native acceptance pending |
| Windows | Unsupported for installation; a blueprint can describe it but the installer refuses it |

Use an ordinary owner session and a POSIX shell. Have Git, HTTPS package access and at least **20 GB free** for initial runtimes, caches and models, plus room for project data. The bootstrap uses `curl`, `tar` and a SHA-256 utility. A blueprint requires **Python 3 on PATH before bootstrap**; use Python 3.11+ for the source-side Python commands here. The installer supplies its own pinned managed Python and Node; a system Python is not the installed runtime.

```sh
git --version
python3 --version
```

The full default installation can bootstrap managed Python without a preinstalled Python; blueprint validation cannot.

Choose a stable lowercase owner ID matching `[a-z][a-z0-9_-]{0,47}`. Examples use `yourname`: replace it before installing. Choose a new absolute private home without symlinks. If you create that directory yourself, it must be owned by your user and mode `0700`. Do not use `sudo` for the owner installation.

The default home is `$HOME/.borg`. Services bind to loopback and use **eleven ports, 18760–18770 by default**. A second installation needs a separate home and non-overlapping eleven-port range. `--port-base 20760` selects 20760–20770. Do not stop another service to free a port without checking its owner.

The full model's default context is 16,384 tokens; increasing context raises memory use. UI work may need macOS Accessibility, Screen Recording or Automation permission for the app performing it. Headless Chromium success does not prove desktop permissions or a signed-in browser.

Source: [requirements](https://github.com/h3ro-dev/borg/blob/master/docs/INSTALL.md), [bootstrap](https://github.com/h3ro-dev/borg/blob/master/install.sh), [configuration contract](https://github.com/h3ro-dev/borg/blob/master/installer/config.py).

## 03 / Choose one installation path {#install}

First obtain and inspect the public checkout. Retain the exact checkout/release used for future same-release recovery.

```sh
git clone https://github.com/h3ro-dev/borg.git
cd borg
./install.sh --help
```

### Path A — First-time full installation {#full-install}

Use this for a full local brain, Codex, Inbox, Beads and four native Codex memory lifecycle hooks. This downloads dependencies and models and starts services. Use a new home.

```sh
./install.sh --home "$HOME/.borg" --owner yourname
export BORG_HOME="$HOME/.borg"
"$BORG_HOME/bin/borg" onboard
"$BORG_HOME/bin/borg" doctor
```

A `provider_sign_in_required` result can be expected before section 05. Read the reported fields; a nonzero `doctor` exit does not by itself mean installation failed.

### Path B — Install your exported blueprint {#blueprint-install}

Build and download `borg-blueprint.json` from the [configurator](https://borg.utlyze.com/#configure). Put the file in the checkout, inspect the whole plan, then select the actual machine ID. The example uses `node-1`; replace it with the ID in your file. Run these commands on the matching target platform.

```sh
python3 borg.py blueprint inspect borg-blueprint.json
python3 borg.py blueprint inspect borg-blueprint.json --machine node-1
./install.sh --home "$HOME/.borg" --owner yourname \
  --blueprint borg-blueprint.json --machine node-1
export BORG_HOME="$HOME/.borg"
"$BORG_HOME/bin/borg" onboard
"$BORG_HOME/bin/borg" doctor
```

Inspection needs no BORG home, login or network. It validates every machine, even when displaying one. The format is `borg-blueprint/v1`; the catalog version must match this release. Required dependencies must be selected. Unknown or duplicate choices, invalid input and mismatched host platforms are refused. An exported multi-machine plan **does not install its other machines**: repeat owner-reviewed setup on each target.

| Choice | Installation behavior |
| --- | --- |
| `full` profile | Starts Qdrant, graph, Ollama, graph LLM shim, memory, brain, connector and watchdog |
| `tools` profile | Starts connector and watchdog; skips memory/graph startup, model pulls and memory hooks |
| Codex | Adds isolated client profile, MCP configuration and conductor; native login is still required |
| Inbox / Beads | Starts selected Inbox; initializes selected Beads store and its wrapper |
| Fleet | Supplies enrollment instructions; enrolls no machines automatically |
| Claude / Grok / launch bus / router | Prepares bundled source/configuration; additional provider binaries, login and operational checks remain owner work |
| Integrations | Supplies catalog setup steps; does not deploy or authenticate external services |
| Adapters / training | Records preparation intent only; full Apple Silicon nodes only; no automatic downloads, training or activation |

Both profiles install the complete source and locked runtime package. Selection controls services and setup, not a smaller dependency download. Tools-only Codex has MCP access with zero memory hooks. Beads without Inbox prepares the coordination configuration its custody wrapper needs without starting an Inbox service.

### Alternative home or existing project roots {#custom-home}

This is an alternative installation command, not a step to run after Path A or B. `--projects` may be repeated for owner-selected existing directories. For a blueprint install, also retain its `--blueprint` and `--machine` arguments.

```sh
./install.sh --home "$HOME/my-borg" --owner yourname --port-base 20760 \
  --projects "$HOME/Projects/example"
export BORG_HOME="$HOME/my-borg"
```

The remaining examples assume `BORG_HOME` points to the home you actually installed. Export it again in each new terminal session. Avoid using a default-home example against the wrong installation.

Source: [installation](https://github.com/h3ro-dev/borg/blob/master/docs/INSTALL.md), [blueprints](https://github.com/h3ro-dev/borg/blob/master/docs/BLUEPRINT.md), [CLI parser](https://github.com/h3ro-dev/borg/blob/master/installer/cli.py).

## 04 / Read the setup plan {#onboard}

```sh
"$BORG_HOME/bin/borg" onboard
"$BORG_HOME/bin/borg" doctor
"$BORG_HOME/bin/borg" call borg_status
```

`onboard` is a read-only metadata plan. It does not run its suggested commands, perform login or contact providers. Its `commands` contain argument arrays, environment overrides, working directories and prerequisites. Replace placeholders with your own facts. `ready: null` means unverified; a configured receipt or account pin does not prove current readiness.

`doctor` checks the selected local services, authentication, storage, models and applicable client configuration. `local_services_ready` describes the selected native services. Overall `ready` can remain false for missing provider sign-in or selected external/research capabilities without an automatic acceptance probe. Inspect `selected_setup` and complete the relevant owner checks; do not change the plan merely to make the indicator green.

If you used `--no-start`, rerun the **same release's installer without `--no-start`**, preserving the same home, owner, port range, project roots and blueprint/machine selection. This initializes selected stores and pulls models where required. `borg start` alone does not do that work. Onboarding supplies the home/blueprint rerun arguments; also preserve your original custom install options.

Source: [onboarding implementation](https://github.com/h3ro-dev/borg/blob/master/installer/onboarding.py), [health checks](https://github.com/h3ro-dev/borg/blob/master/installer/health.py).

## 05 / Connect your own provider {#providers}

### Codex — Integrated installer path {#codex}

Use this when Codex is selected. The conductor must be running for login's account-pin readback. Sign in with your own account; do not copy another profile's credentials.

```sh
"$BORG_HOME/bin/borg" auth codex
"$BORG_HOME/bin/borg" doctor
"$BORG_HOME/bin/borg" tools
```

This uses `conductors/primary/profile` and pins the observed provider account to the lane. It does not configure your global Codex profile. Full installs configure four recall/capture hooks; tools profiles do not.

Open the client with the exact command emitted by `borg onboard`. The equivalent below requires you to set `NODE_BIN` to **`runtime.nodeBin` from your own `conductors/config.json`**. The example path is a placeholder, not an assumed archive layout.

```sh
export NODE_BIN=/absolute/path/to/your/borg/runtime/node/bin/node
CODEX_HOME="$BORG_HOME/conductors/primary/profile" \
  PATH="$(dirname "$NODE_BIN"):/usr/bin:/bin" \
  "$BORG_HOME/runtime/npm/node_modules/.bin/codex"
```

Inspect BORG's MCP connection in that client and perform the disposable canary in section 08. These optional readbacks inspect the conductor and native account pin:

```sh
BORG_HOME="$BORG_HOME" "$NODE_BIN" \
  "$BORG_HOME/app/conductor/borg-conductor.mjs" status \
  --config "$BORG_HOME/conductors/config.json"
BORG_HOME="$BORG_HOME" "$NODE_BIN" \
  "$BORG_HOME/app/conductor/borg-conductor.mjs" auth status \
  --config "$BORG_HOME/conductors/config.json" --lane primary
```

Routing real agents additionally requires fresh machine capacity, active work claims, matching accounts and usable provider allowance. A process, lane or listening port alone proves none of these.

<details>
<summary>Optional: Claude Code, Grok and Cursor clients</summary>

Install a compatible CLI through the provider's supported distribution. BORG does not install these CLIs or provide `borg auth claude` / `borg auth grok` / `borg auth cursor`. The following commands match the native help audited with the source; check your installed version's help before using them.

Create private dedicated profiles, preserving any existing profile. Run from your BORG home so unrelated project configuration is not loaded.

```sh
cd "$BORG_HOME"
umask 077
mkdir -p "$BORG_HOME/providers/claude/profile" "$BORG_HOME/providers/grok/profile" \
  "$BORG_HOME/providers/cursor/profile" "$BORG_HOME/providers/cursor/bin"
```

Claude Code:

```sh
CLAUDE_CONFIG_DIR="$BORG_HOME/providers/claude/profile" claude auth login
CLAUDE_CONFIG_DIR="$BORG_HOME/providers/claude/profile" \
  claude mcp add --scope user --transport stdio borg -- \
  "$BORG_HOME/bin/borg" mcp-stdio --home "$BORG_HOME"
CLAUDE_CONFIG_DIR="$BORG_HOME/providers/claude/profile" claude auth status
CLAUDE_CONFIG_DIR="$BORG_HOME/providers/claude/profile" claude
```

Compatible Grok Build CLI:

```sh
GROK_HOME="$BORG_HOME/providers/grok/profile" grok login --oauth
GROK_HOME="$BORG_HOME/providers/grok/profile" \
  grok mcp add --scope user --transport stdio borg -- \
  "$BORG_HOME/bin/borg" mcp-stdio --home "$BORG_HOME"
GROK_HOME="$BORG_HOME/providers/grok/profile" grok mcp doctor
GROK_HOME="$BORG_HOME/providers/grok/profile" grok
```

Cursor agent CLI (install as `cursor-agent`, not as `agent` if another provider already uses that name):

```sh
cursor-agent login
cursor-agent models
```

Record a Grok model ID from that catalog into `providers.cursor.model`. Set `providers.cursor.binary` to the absolute `cursor-agent` path and enable the provider. The launch bus inherits `CURSOR_API_KEY`; it does not store the key.

Test BORG in the chosen client. MCP configuration does not register Codex's four lifecycle hooks in another provider. Diagnostics do not prove a live provider login, available model or allowance. The stdio bridge consumes the home's credential privately.

**Optional provider execution is a separate integration.** Claude's launch bus needs an explicit `providers.claude.binary`, enablement in `conductors/config.json` and `CLAUDE_CONFIG_DIR` in its launch environment. The included headless path has no native thread-status, mid-turn steering or provider allowance routing. A launch packet starts real work and may incur charges; it is not a setup probe.

Grok's conductor needs explicit `grokBin`, `grokHome`, `expectedVersion`, `readinessMarkerPath`, `statePath` and a distinct loopback port. There is no installer service, login orchestration or supported readiness-marker provisioning flow. A matching version/marker is not live account evidence. Do not fabricate a marker; leave this provider disabled until an owner-controlled provisioning and login-evidence workflow is verified.

Cursor's launch bus needs an explicit `providers.cursor.binary`, enablement, and a Grok `model` from the Cursor catalog. The included headless path is `cursor-agent -p --force`. Cloud Agents cannot reach this installation's loopback Inbox or hooks.

</details>

Source: [provider setup and execution boundaries](https://github.com/h3ro-dev/borg/blob/master/docs/SETUP.md), [conductor integration](https://github.com/h3ro-dev/borg/blob/master/conductor/INTEGRATION.md).

## 06 / Enroll another machine {#fleet}

This optional track connects only owner-selected machines. Prepare a resident connector on each target, a trusted SSH alias with a known host key, and its exact installation identity. Enrollment does not import credentials or sign in to providers.

On a **new target**, clone and enter the source checkout as in section 03, then use an empty default home:

```sh
./install.sh --owner yourname --no-start
"$HOME/.borg/bin/borg" start connector
"$HOME/.borg/bin/borg" call borg_identity
```

This prepares the complete runtime/browser package but starts only the connector. It does not pull model weights, initialize a full running brain or start a conductor. It is not a minimal dependency package. An existing full installation can use its existing connector instead. For custom homes use their actual paths and separate ports.

Record the target's exact `home`, `owner` and `instance_id`. On the **controlling BORG**, substitute those facts and your trusted SSH alias for every example below. `/absolute/target/.borg` and `TARGET_UUID` are placeholders; they are not defaults.

```sh
"$BORG_HOME/bin/borg" fleet add build-a \
  --ssh-alias build-a \
  --remote-home /absolute/target/.borg \
  --owner yourname \
  --instance-id TARGET_UUID
"$BORG_HOME/bin/borg" call fleet_hosts
"$BORG_HOME/bin/borg" call fleet_tools \
  --arguments '{"host":"build-a","prefix":"computer_"}'
```

Enrollment verifies identity first. Host IDs are stable routing names; use a new ID for a changed installation. Configuration is read per request; follow any connector restart instruction reported by the CLI. Discover the actual target schema, then call it with the explicit host. Replace the example path with that target's project directory:

```sh
"$BORG_HOME/bin/borg" call fleet_call \
  --arguments '{"host":"build-a","tool":"computer_list_directory","arguments":{"path":"/absolute/target/.borg/projects","depth":1}}'
```

All paths and process, browser or job IDs belong to the selected target. The resident connector retains state across an SSH bridge closing. Independent requests can run concurrently; shared files and the physical desktop still need coordination. Enrollment and tool discovery do not prove OS permissions, provider authentication, usable capacity or a completed job.

For remote **agents**, configure machine/account lanes, dedicated profiles, endpoints, pins and fresh capacity/claims collectors under `conductors/config.json` using the [multi-machine integration contract](https://github.com/h3ro-dev/borg/blob/master/conductor/INTEGRATION.md#multi-machine-or-multi-account-extension). A remote file operation is not a conductor dispatch. Unknown capacity or accounts fail admission.

To disable new calls to an enrolled host:

```sh
"$BORG_HOME/bin/borg" fleet disable build-a
```

Disabling routing or closing SSH does not establish cancellation of work already running. After a lost response inspect target state and target receipts before retrying:

```sh
"$BORG_HOME/bin/borg" call fleet_call \
  --arguments '{"host":"build-a","tool":"borg_operations_recent","arguments":{"limit":10}}'
```

Controlling-host and target receipts are distinct. No exactly-once guarantee is made. Source: [fleet setup](https://github.com/h3ro-dev/borg/blob/master/docs/SETUP.md#7-enroll-your-own-machines), [fleet implementation](https://github.com/h3ro-dev/borg/blob/master/connector/fleet_tools.py).

## 07 / Optional ChatGPT web gateway {#web}

A web app connects to an already running BORG. It does not install the software, enroll machines or make a sleeping computer available. Local setup works without this track.

1. Complete local setup and inspect `doctor`.
2. In your own Cloudflare account, create a locally managed tunnel and DNS hostname. Store its private credential JSON beneath `BORG_HOME/cloudflare` with mode `0600`. The bundled cloudflared executable is under `BORG_HOME/runtime/cloudflared`.
3. Create an Access MCP server application with Managed OAuth for that hostname. Restrict its policy to your exact sign-in email. Record the issuer and audience; these are not bearer credentials.
4. Replace all example identifiers and the credential path below. BORG configures local files; it does not create Cloudflare resources or DNS.

```sh
"$BORG_HOME/bin/borg" web \
  --public-url https://borg.example.com \
  --issuer YOUR_ACCESS_ISSUER \
  --audience YOUR_APPLICATION_AUDIENCE \
  --owner-email you@example.com \
  --tunnel-credentials "$BORG_HOME/cloudflare/credentials.json"
"$BORG_HOME/bin/borg" stop watchdog
"$BORG_HOME/bin/borg" start gateway tunnel watchdog
"$BORG_HOME/bin/borg" doctor
```

Use your ChatGPT account/workspace's supported custom MCP app controls, your actual hostname in the example `https://borg.example.com/mcp` URL and OAuth. Complete your own Access sign-in, inspect the current actions and enable those in scope. In a fresh chat, call `borg_status`, then perform a reversible file write/read in a disposable project. Local gateway health is not successful client OAuth.

The bare hostname deliberately returns 404; the endpoint is `/mcp`. Unauthenticated requests must encounter authentication rather than expose tools. Account/workspace controls still apply. Refresh and review client actions when server schemas change; old chats may retain cached schemas. Follow the current client documentation linked from the source guide.

Source: [web setup, Cloudflare and current client documentation](https://github.com/h3ro-dev/borg/blob/master/docs/WEB.md).

## 08 / Prove the workflow {#verify}

Run the selected local checks first:

```sh
"$BORG_HOME/bin/borg" doctor
"$BORG_HOME/bin/borg" tools computer_
"$BORG_HOME/bin/borg" tools browser_
"$BORG_HOME/bin/borg" call borg_status
"$BORG_HOME/bin/borg" call borg_operations_recent --arguments '{"limit":10}'
```

Then verify the actual client and chosen target using its live schemas:

1. **Files:** in a new disposable project under this home's configured project roots, create a uniquely named text file containing a non-secret canary string. Read it back and compare exact content. Keep the operation receipt and clean up only your canary artifacts.
2. **Commands:** run a harmless command and inspect its exit status and captured output. This direct local example should return `borg-canary` followed by a newline; follow the returned process ID with the discovered output tool if it is still running.

```sh
"$BORG_HOME/bin/borg" call computer_start_process \
  --arguments '{"command":"echo borg-canary"}'
```

3. **Memory, full profile only:** use a non-sensitive disposable fact to exercise the configured capture/recall path. Verify the expected fact is recalled and inspect the graph/brain health. Confirm current hook trust; a configuration receipt alone is insufficient. Remove only the test fact through the supported tool if appropriate. A tools profile has no memory service to prove.
4. **Coordination, when selected:** confirm authenticated Inbox in `doctor` and read the selected Beads store through its wrapper:

```sh
"$BORG_HOME/bin/bd" list --json
```

5. **Optional boundaries:** verify a real remote operation and its target receipt; web OAuth and an action from a fresh chat; required desktop permissions; and each chosen external integration's documented acceptance. A headless browser probe proves only that browser path.

Record the release, home/instance identity, selected services, outcomes, receipt references and remaining gaps privately. Report each capability as configured, verified or still requiring setup. Do not claim universal readiness from `doctor`, enrollment, a checkbox or a directory of source code.

Source: [acceptance boundaries](https://github.com/h3ro-dev/borg/blob/master/docs/SETUP.md#8-verify-os-gates-and-the-real-workflow), [native tool implementation](https://github.com/h3ro-dev/borg/blob/master/connector/computer_tools.py).

## 09 / Troubleshoot from evidence {#troubleshoot}

| Symptom | Next action |
| --- | --- |
| `provider_sign_in_required` | For selected Codex, ensure the conductor is running, complete `borg auth codex`, then check the native account/pin and `doctor` |
| `selected_setup_required` | Inspect `selected_setup` and the catalog/onboarding steps; manual integrations and research selections do not have automatic acceptance |
| `setup_incomplete` or a failed local call | Inspect the named component in `doctor` and its log under `BORG_HOME/logs`; check authenticated service health, not only a process/port |
| Source differs, owner/home conflict, unsafe directory, occupied port | Preserve the existing installation; inspect the refusal. Use the same source for recovery or a new home and port range for changed choices. Do not overwrite owner edits or foreign services |
| `busy`, `borg_busy`, queue full | Inspect `borg_status` concurrency and active work; a resource or host capacity limit was reached. Coordinate the shared resource and reduce simultaneous work. More clients do not create more host capacity |
| `timeout`, disconnect, `outcome_unknown` | Inspect the native file/process/job and `borg_operations_recent` on the actual target before repeating a mutation. A response loss is not failure or cancellation |
| `authentication_required` | Complete the relevant owner-native login; do not copy or expose credentials |
| `permission_denied` | Verify the specific OS/provider permission required for the selected operation |
| `policy_refused` or changed target identity | Inspect ownership/admission or the target identity. Use a narrower supported operation; do not bypass the refusal or silently repin a different machine |
| `capability_unavailable`, missing tool, rejected inputs | Compare `borg tools` or target `fleet_tools` with the client's refreshed actions and actual schema |
| Local calls work, `/mcp` returns 404 | Check exact path, hostname, DNS route and tunnel ingress; bare `/` is intentionally 404 |
| OAuth fails or expires | Check Access issuer, audience, exact email policy and provider refresh configuration; reauthenticate in the client as needed |
| Blueprint rejected | Check catalog version, complete schema, unique machine IDs, required dependencies and host platform; inspect the whole file before installation |

The native connector defaults to 64 in-flight operations, 256 queued operations and a 30-second queue wait. These are configurable operation limits, **not an agent count or throughput promise**. Size concurrency against measured host capacity; preserve resource coordination. A timed-out process-input call may have delivered a prefix, so inspect the process before resending input.

Source: [connector limits](https://github.com/h3ro-dev/borg/blob/master/connector/README.md), [error classification](https://github.com/h3ro-dev/borg/blob/master/connector/computer_tools.py), [operation receipts](https://github.com/h3ro-dev/borg/blob/master/connector/operation_receipts.py).

## 10 / Stop, maintain and recover {#operate}

Use the launcher from the intended home. These commands operate that installation's selected services:

```sh
"$BORG_HOME/bin/borg" stop
"$BORG_HOME/bin/borg" start
"$BORG_HOME/bin/borg" doctor
```

Stop/start retains data. After restart, verify authenticated services and the workflow you actually use. Coordinate active jobs before service maintenance and reconcile interrupted operations afterward.

| Private location under BORG_HOME | Purpose |
| --- | --- |
| `config.json`, `blueprint.json` when selected | Owner, installation identity, ports, projects and preserved machine plan |
| `app/` | Exact source and hash manifest |
| `runtime/`, `cache/`, `models/` | Managed runtimes, verified downloads and model weights |
| `mem0/`, `graphiti/` | Memory, temporal graph, history and scoped credentials |
| `borg-context/` | Connector configuration, operation receipts, jobs and browser state |
| `conductors/`, `providers/` when configured | Dedicated provider profiles, account pin and conductor configuration |
| `coordination/`, `beads/` | Installation-owned messaging, identities and project work |
| `logs/`, `services/` | Component logs and installation-specific service definitions |

**Same-release reruns:** retain the exact source, home, owner and install options. Blueprint reruns must use the identical plan and machine; the installer saves `blueprint.json` privately with mode `0600`. A changed blueprint/owner/machine is rejected. A default/full home cannot be converted into a blueprint/tools home. Use a new home for different choices.

**New releases:** there is no in-place release migration. Preserve the old home and evaluate a new release in a separate home with non-overlapping ports. Do not blindly `git pull` and run a changed installer over existing state. A missing dependency may be restored from verified cache; inspect the component's log and source refusal before repair.

**Backups and removal:** coordinate work, stop services, retain a private backup of state and configuration, and verify recovery before removal. Remove only the chosen home and service definitions whose UUID matches its `config.json`. Unrelated services and global provider profiles are outside that installation. No blanket deletion command is needed.

<details>
<summary>Optional research: inspect inactive LoRA adapters</summary>

```sh
"$BORG_HOME/bin/borg" adapters list
"$BORG_HOME/bin/borg" adapters prepare all
```

Run preparation only when requested, on a full Apple Silicon installation, with disk/network capacity for roughly 3.3 GB of base models plus runtime/cache. It verifies pinned files, creates a separate MLX runtime and emits generation commands for canaries. **It does not activate adapters, train on your data or prove extraction quality.** Historical training revisions remain unknown. Require reproducible compatibility and real extraction canaries before any separate promotion decision. Training-pair capture and historical imports are disabled by default.

</details>

Source: [recovery and adapters](https://github.com/h3ro-dev/borg/blob/master/docs/INSTALL.md), [blueprint preservation](https://github.com/h3ro-dev/borg/blob/master/docs/BLUEPRINT.md).

## 11 / Give your agent a bounded setup brief {#agent-prompt}

Attach your exported blueprint if you chose one. Fill in the scope before handing this prompt to your agent. This brief requests setup and verification; it does not grant unrelated account, fleet or publishing authority.

```text
Help me set up my own BORG using https://borg.utlyze.com/guide.html
and the self-contained runbook at https://borg.utlyze.com/agent-guide.md.
Use the public source at https://github.com/h3ro-dev/borg and inspect
its installer/docs at the same release before running commands.

My requested scope:
- Target machine and OS: [my machine]
- Private BORG home and lowercase owner ID: [my choices]
- Installation: [full default OR attached borg-blueprint.json + machine ID]
- Providers: [my selected providers and own accounts]
- Remote machines and web access: [none OR explicitly listed targets]

Confirm these deployment choices are complete, inspect prerequisites,
and carry out only this requested setup under my existing instructions.
Keep secrets private and use native login. Preserve existing homes,
profiles and unrelated services. Follow the documented release/rerun limits.
A selected integration still needs its own setup and acceptance.
Verify doctor, a real client tool call and a disposable file/command canary.
Verify remote operations or web OAuth only if included in my scope.
Report what is configured, what is verified, and any remaining setup gaps.
```

Continue: [configure your plan](https://borg.utlyze.com/#configure) · [fleet overview](https://borg.utlyze.com/#fleet) · [installation overview](https://borg.utlyze.com/#setup) · [agent orientation](https://borg.utlyze.com/llms.txt).
