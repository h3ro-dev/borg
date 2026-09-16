# Install a machine blueprint

A blueprint is a portable plan for one or more independently owned BORG homes. It contains choices and workload inputs, never credentials, endpoints, commands, or private paths. Sizing in the website is an engineering estimate, not measured capacity or permission to dispatch agents. Machine capacity, provider allowance, account pins and work claims still govern dispatch.

Inspect the entire file from a release checkout before installation:

```sh
python3 borg.py blueprint inspect blueprint.json
python3 borg.py blueprint inspect blueprint.json --machine node-1
./install.sh --owner my-owner --blueprint blueprint.json --machine node-1
```

The shell installer needs Python 3 on PATH to validate blueprint input before bootstrap creates a home or downloads files. It still installs and uses the pinned managed Python runtime. `--home /absolute/private/home`, `--port-base PORT` and `--no-start` retain their existing meanings. Different concurrent homes need distinct port ranges.

`inspect` works without a BORG home, login or network access. It validates **every** machine even when displaying only one. Its output names selected services, catalog setup steps and remaining limitations. The installed command is also available as `borg blueprint inspect FILE`.

## Profiles and components

| Choice | Installation behavior |
| --- | --- |
| No blueprint | Existing full installation, Codex, Inbox, Beads and four native memory hooks |
| `full` | Qdrant, graph, Ollama, graph LLM shim, memory, brain, connector and watchdog |
| `tools` | Connector and watchdog; skips memory/graph startup, model pulls and memory hooks |
| `codex` | Configures the isolated primary profile, BORG MCP client and conductor; owner signs in with `borg auth codex` |
| `inbox` | Bootstraps and starts the native installation-owned Inbox |
| `beads` | Initializes the private embedded work store and installs the `bin/bd` wrapper |
| `fleet` | Adds owner enrollment instructions; never discovers or enrolls machines automatically |
| `grok`, `claude`, `cursor`, `launch-bus`, `router` | Bundled source and safe conductor configuration preparation; provider binary/profile/login and operational qualification remain owner steps |
| External integrations | Actionable catalog setup checklist; no deployment, credential import or authentication |
| Adapters and training | Preparation intent only, restricted to full Apple Silicon nodes; no automatic download, training or promotion |

The complete source and locked runtime package remain installed for either profile. Selection controls configured services and owner setup, not package trimming. Beads without Inbox prepares the native coordination configuration that its custody wrapper needs, without starting an Inbox service. Tools-only Codex gets the isolated BORG MCP connection with zero memory lifecycle hooks. Its watchdog probes authenticated connector liveness; absent memory is not treated as a working memory service.

Grok, Claude and Cursor binaries are not supplied by checking their boxes. Their provider entries remain disabled until the owner configures and qualifies their own runtime. Onboarding documents dedicated profiles and native sign-in, plus the bundled launch-bus/provider limitations. The installer does not enable automatic provider authentication. Prepared configuration does not prove a running service, provider account or usable quota.

Run `borg onboard` for selected setup instructions and `borg doctor` for observed readiness. `local_services_ready` covers selected native installation services. `ready` remains false when a selected external/research/operational capability has no automatic acceptance probe; `selected_setup` names those unverified choices. Follow the documented owner checks. Neither a checkbox nor a source directory proves those capabilities operational.

## Validation and preservation

The format is `borg-blueprint/v1` and must match the release's `platform/catalog.json` version. See [the deterministic example](../installer/fixtures/blueprint-v1.json).

- Only documented fields are accepted. Duplicate JSON keys, duplicate choices, duplicate machine IDs, unknown IDs and non-finite numbers fail.
- Choose 1–100 machines. IDs match `[a-z][a-z0-9-]{0,31}`. Labels contain 1–60 UTF-16 code units, with no control or formatting characters.
- Goals are `coding`, `research`, `automation`, `learning` or `custom`; profiles are `full` or `tools`.
- Workload bounds: agents 0–128, browsers/builds 0–32, project GB 0–100000 (integers); stored memory 0–100 million; context 8192/16384/32768; training boolean.
- Mandatory catalog dependencies must be selected explicitly. Profile and platform compatibility are checked. Training workload requires the training component on full `macos-arm64`.
- Windows can be inspected as an unsupported plan; installation refuses it before state creation. Installation also requires the selected OS/architecture to match the actual host. Linux and Intel macOS native acceptance remain pending.

The selected blueprint is stored as mode `0600` in `BORG_HOME/blueprint.json`, with its machine ID recorded in the private installation configuration. Rerun with the identical blueprint and machine; `borg onboard` supplies that command. For an installation made with `--no-start`, rerun the installer without that flag to initialize the selected stores and models.

An existing default/full home cannot be converted to a blueprint or tools home. A changed blueprint, owner or machine is rejected, preserving current files and services. Use a new home for different choices. Existing source-manifest, private-file, profile, account-pin and native service identity checks still apply; a changed release is not silently installed over another release.

## Native acceptance

On 2026-09-16, browser-exported full and tools blueprints were installed into separate fresh Apple Silicon macOS homes. Both passed native selected-service readiness, file write/read and process execution. Identical reruns preserved installation identity and source; changed blueprints were refused. Blueprint files had mode `0600`.

The full node verified memory authentication, graph storage, model digests, four trusted capture hooks, an actual brain cycle, Inbox and Beads. The tools node verified its connector, watchdog, selected conductor, Inbox and Beads with no memory services listening and no capture hooks. Both correctly reported that provider sign-in was still required. These checks do not establish Linux/Intel acceptance, hosted web-client OAuth, external integrations or real provider throughput.
