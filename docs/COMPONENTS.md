# Configure your BORG components

Start with the [interactive machine planner](https://borg.utlyze.com/#configure), then use the [blueprint installation guide](BLUEPRINT.md). The machine planner and installer share the versioned [component catalog](../platform/catalog.json). A selection records what you want to set up; live readiness requires the checks for that component.

## What a new owner gets

A full node provides its own semantic memory, temporal graph, pinned small local models and authenticated native connector. Add the conductors and work coordination you need. A tools node starts the connector and selected work services without the local brain. Both roles retain the complete runtime/source package. They do not import anyone else's accounts, memories or fleet.

The original full installation is verified on Apple Silicon macOS. Linux and Intel macOS have pinned artifacts but complete native installation acceptance remains pending. Windows is unsupported by the installer. Provider sign-in, external-service configuration and macOS permissions remain your setup steps.

## Choose the right kind of integration

- **Included:** BORG ships the implementation. The blueprint selects applicable local services. Provider login and native verification may still be required.
- **Owner setup:** source or a connection path is included, with additional configuration stated below. The selection does not start an authenticated provider or install an external CLI.
- **Connect your service:** use your own deployed service/account and its official MCP, API or CLI. BORG does not deploy or proxy it.
- **Research:** adapter/training source is included and inactive. Preparation does not promote an adapter.

Each node has separate data and identity. Enrolling it with `borg fleet add` routes native tools; it does not replicate memory, share Inbox records, create conductor lanes or increase provider allowance. You can configure your clients to use a chosen brain, but this is separate from tool enrollment. The [conductor integration contract](../conductor/INTEGRATION.md) describes remote collectors and account lanes.

## Orchestration, coordination and native tools

<a id="codex"></a>

### Codex conductor

**Included.** Start, inspect, steer and interrupt native Codex work in an installation-owned profile.

[Implementation](../conductor/borg-conductor.mjs)

1. Run borg auth codex on this machine, then borg doctor.
2. Use the installed conductor with your own project path and a unique work ID.

Your provider account, allowance and live machine admission are required. A selected conductor is not a running agent.

<a id="router"></a>

### Machine and account router

**Included.** Choose among your configured Codex lanes using live capacity, account identity, work claims and allowance.

[Implementation](../conductor/router/router.mjs)

1. Configure your own machine/account lanes in conductors/config.json.
2. Verify current capacity and claims collectors before dispatch; follow conductor/INTEGRATION.md.

Remote tool enrollment does not add conductor lanes. Unknown capacity or account state fails admission.

<a id="grok"></a>

### Grok conductor

**Owner setup.** Run the bundled Grok bridge with interruption and same-session resume.

[Implementation](../conductor/providers/grok-conductor.mjs)

1. Install and sign in to a compatible Grok CLI in your own dedicated profile.
2. Configure its binary, profile, version, private state and loopback port in conductors/config.json; follow the provider setup guide.
3. Qualify the actual CLI and create its readiness receipt before starting the bundled bridge.

Grok CLI and credentials are not installed by BORG. The bridge is not part of Codex allowance routing.

<a id="claude"></a>

### Claude launch adapter

**Owner setup.** Launch bounded Claude Code headless work through the provider launch bus.

[Implementation](../conductor/providers/launch-bus.mjs)

1. Install Claude Code and complete its native sign-in with your own account.
2. Set the Claude binary and enable the provider in your private conductor configuration.
3. Use the documented launch packet with a bounded work ID, prompt and project path.

This adapter does not provide native thread status, mid-turn steering or provider allowance routing.

<a id="launch-bus"></a>

### Provider launch bus

**Included.** Send a common work packet to the selected Codex, Grok or Claude backend and retain a launch receipt.

[Implementation](../conductor/providers/launch-bus.mjs)

1. Configure the provider you will use, then validate its native readiness.
2. Submit a bounded packet through the launch-bus CLI; inspect the provider result as well as the launch receipt.

A launch receipt is not completed work. Provider capabilities differ.

<a id="inbox"></a>

### Agent Inbox

**Included.** Authenticated identities, grants, leased messages, assignments and shared discoveries.

[Implementation](../coordination/borg_coordination/portable.py)

1. Use the installation-owned Inbox; create scoped identities for your agents.
2. Verify a send, recipient read and acknowledgment using those identities.

Each installation starts with its own Hub and empty records; selecting Inbox does not federate multiple Hubs.

<a id="beads"></a>

### Beads work tracking

**Included.** Durable projects, owned tasks, dependencies and acceptance evidence.

[Implementation](../coordination/borg_coordination/cli.py)

1. Use the installed borg home/bin/bd wrapper to access this installation’s work store.
2. Create a project, claim work and record acceptance before closing it.

<a id="fleet-context"></a>

### Fleet context extension

**Owner setup.** Public source for a bounded view of machine, account and work observations from your own collectors.

[Implementation](../coordination/comms/hub/fleet_context.py)

1. Build or connect your own telemetry and account/work collectors using the documented source contract.
2. Embed the fleet context reader with explicit owner-controlled sources; the default source list is empty.
3. Verify fresh coverage and actual work ownership before using its observations.

Source extension only: the installer does not deploy collectors or operational dashboards. Missing and stale observations stay unknown.

<a id="maintenance"></a>

### Memory maintenance scripts

**Owner setup.** Source for scoped ingestion, consolidation, decay and cleanup under your own retention policy.

[Implementation](../memory/bin/mem0-dream)

1. Review the shipped script and its configuration/help before running against your data.
2. Set your own retention, backup, allowed sources and scheduling; verify a bounded operation first.

Scripts are included; historical schedules and private ingestion sources are not. The background graph feed is a separate service.

<a id="credential-handles"></a>

### Provider login handles

**Owner setup.** Keep nonsecret provider metadata and route an agent to the correct native login or consent screen.

[Implementation](../connector/service_handles.py)

1. Configure provider names, HTTPS login URLs and intended scopes in the private borg-context/credentials.json registry.
2. Discover credential_ tools and verify the returned native login handoff.

Handles contain metadata only. They do not hold credentials, proxy authenticated APIs or prove current provider authorization.

<a id="fleet"></a>

### Multi-machine connector

**Included.** Route files, commands, jobs and browser tools to explicitly enrolled machines.

[Implementation](../connector/fleet_tools.py)

1. Install each target independently and verify its borg_identity.
2. Establish trusted SSH, then enroll its exact owner, home and instance ID with borg fleet add.
3. Verify fleet_hosts and perform a reversible tool call on the selected machine.

Enrollment is explicit. Memory is not replicated between machines. Shared desktop resources remain serialized.

<a id="browser"></a>

### Native browser tools

**Included.** BORG-owned browser sessions with inspect, click, navigation and durable operation receipts.

[Implementation](../connector/native_browser.py)

1. Use borg tools browser_ to discover the installed schema.
2. Open an allowed page and verify a reversible interaction.

Browser runtime is included even if this planning option is unselected. Account sign-in and website permissions remain yours.

<a id="desktop"></a>

### macOS desktop tools

**Owner setup.** Inspect and operate native apps in an interactive macOS session.

[Implementation](../connector/native_ui.py)

1. Grant the OS permissions required by your chosen apps and tools.
2. Use ui_permissions and a reversible native app action to verify access.

Requires an interactive macOS session. One physical desktop cannot serve independent simultaneous focus actions.

<a id="adapters"></a>

### Extraction LoRA adapters

**Research — inactive.** Three shipped adapter weight sets and model cards for local extraction research.

[Implementation](../adapters/MANIFEST.json)

1. Inspect borg adapters list.
2. On Apple Silicon, use borg adapters prepare only when you want the separately downloaded, pinned MLX base models.
3. Run your own held-out evaluation and real-pipeline canary before any promotion.

Inactive by default. Preparation does not enable extraction or establish semantic quality.

<a id="training"></a>

### Training and evaluation

**Research — inactive.** Private dataset builders, training chains, held-out exams and promotion canaries.

[Implementation](../training)

1. Set a private dataset and checkpoint budget; use only data you are permitted to train on.
2. Read the training and adapter model cards before running an isolated experiment.
3. Keep the daily memory pipeline separate from resource-heavy training.

No unattended training or promotion is enabled. Hardware allowances are not measured training minimums.

## External service choices

These are connection/setup recipes. They are not bundled deployments, authenticated sessions, or a promise that every provider exposes an MCP endpoint. Use each official guide for its supported interface.

| Choice | Purpose | Official setup |
|---|---|---|
| GitHub | Repository context, issues, pull requests and code review. | [Guide](https://github.com/github/github-mcp-server) |
| Figma | Design context and implementation handoff through the official MCP server. | [Guide](https://developers.figma.com/docs/figma-mcp-server/) |
| Sentry | Application error and issue context. | [Guide](https://docs.sentry.io/product/sentry-mcp/) |
| Vercel | Project and deployment context for your Vercel account. | [Guide](https://vercel.com/docs/mcp) |
| Railway | Owner-controlled projects, services and deployments. | [Guide](https://docs.railway.com/ai/mcp-server) |
| Cloudflare / web ChatGPT | Expose your BORG MCP through your own domain, Access policy and tunnel. | [Guide](../docs/WEB.md) |
| Google Workspace | Owner-authorized files, calendars and workspace data via the chosen client connector or official APIs. | [Guide](https://developers.google.com/workspace) |
| Microsoft 365 | Owner-scoped Microsoft Graph access to mail, files and calendar. | [Guide](https://learn.microsoft.com/en-us/graph/overview) |
| Slack | Team context and authorized workflow actions. | [Guide](https://docs.slack.dev/) |
| Notion | Workspace pages and knowledge through Notion MCP. | [Guide](https://developers.notion.com/docs/mcp) |
| n8n | Visual workflows, scheduled tasks, webhooks and API glue. | [Guide](https://docs.n8n.io/advanced-ai/accessing-n8n-mcp-server/) |
| Twenty CRM | Owner-hosted customer and pipeline data. | [Guide](https://docs.twenty.com/) |
| Penpot | Owner-hosted collaborative design and design-system context. | [Guide](https://help.penpot.app/) |
| ERPNext / Frappe | Accounting and operational records via your scoped Frappe API. | [Guide](https://docs.frappe.io/framework/user/en/api/rest) |
| Grafana / Prometheus | Machine and service metrics to support capacity decisions. | [Guide](https://grafana.com/docs/) |
| Metabase | Business reporting against your selected data sources. | [Guide](https://www.metabase.com/docs/latest/) |
| PostgreSQL / PostGIS | Owner-managed relational and spatial data. | [Guide](https://www.postgresql.org/docs/) |
| Label Studio | Human-reviewed annotations for evaluation and training. | [Guide](https://labelstud.io/guide/) |
| MLflow / dbt / DuckDB | Experiment tracking and reproducible local data transformations. | [Guide](https://mlflow.org/docs/latest/) |
| Custom MCP or API | Bring a compatible connector or an owner-built tool. | [Guide](https://modelcontextprotocol.io/docs/develop/connect-local-servers) |
| Document, media and science tools | Use your own Blender, FFmpeg, document, CAD, GIS or analysis toolchain through native process jobs. | [Guide](https://www.blender.org/) |

## Extend without importing another owner's system

Use a provider-native MCP connection for an MCP server, an owner-authenticated CLI for a command-line service, or your own scoped API wrapper. Configure credentials locally in the appropriate vault or provider flow. Keep credentials, private endpoints and machine paths out of public blueprints.

The connector's `credential_list`, `credential_status` and `credential_handoff` tools expose nonsecret metadata and a native login handoff only. They do not load credentials, execute authenticated HTTP requests or prove the account is signed in. Similarly, the owner tool catalog makes tools discoverable; it does not mount arbitrary MCP servers.

Configure optional fleet-context sources in your own embedding and qualify their freshness and coverage. There is no included universal deployment of private fleet collectors, operational dashboards or historic schedules. Use an external workflow service such as n8n when its documented capabilities fit your work.

For each connected service, verify its current identity and intended scope, an authorized read, and a reversible action before relying on it. Retain the native receipt. BORG's source is MIT; third-party runtimes, services, model bases and tools keep their own [licenses and terms](../THIRD_PARTY_NOTICES.md). The catalog records individual license boundaries.
