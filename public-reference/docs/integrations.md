# Agent and connector integration contracts

This document distinguishes working architectural patterns from a shipped universal installer. The examples describe integration boundaries; no provider account, runtime binary, credentials, or production config is bundled.

## Claude

For a person using native Claude Code, register an authenticated BORG MCP endpoint through Claude's supported MCP configuration. Use documented lifecycle hooks for bounded recall and queued capture, and preserve hook trust controls. Version and test event payloads, especially teardown and resumed-session behavior. Native Claude Desktop and Claude Code need separate acceptance; a common brand does not imply identical hooks.

For a product that embeds agent execution, use the Claude API / Agent SDK with an authorized commercial configuration. Do not turn individual Claude subscription OAuth tokens into an authentication service for customers. See the provider's [legal and compliance documentation](https://code.claude.com/docs/en/legal-and-compliance) and [commercial terms](https://www.anthropic.com/legal/commercial-terms).

## Codex

Codex's documented app-server supplies thread and turn lifecycle, approvals, and event streams. A conductor can translate these into the work contract below while preserving sandbox and approval decisions. Keep provider thread and turn IDs, handle unknown outcomes, and check expected-turn identity before steering.

Native Codex supports its documented sign-in methods. For unattended commercial product execution, use a reviewed API or enterprise authentication design; do not sell access to the operator's personal accounts or pool accounts to evade limits. See [app-server](https://developers.openai.com/codex/app-server/) and [authentication](https://developers.openai.com/codex/auth/).

Published BORG conductor code is a historical adapter, not evidence that every current CLI version or provider feature is supported. The historical public snapshot also omits some lifecycle modules referenced by its tests and memory server. Reconcile these before advertising a complete installation.

## Grok

Keep Grok as a peer backend, not an impersonated Codex account. An adapter can use supported Grok Build MCP/lifecycle facilities or the xAI API. Where a runtime cannot accept mid-turn input, expose steering as `interrupt_resume`, not `native_steer`, and document partial side effects.

Use current [Grok Build documentation](https://docs.x.ai/build) and the [xAI enterprise terms](https://x.ai/legal/terms-of-service-enterprise). Permission to call an API is distinct from permission to train another model on its outputs. This reference does not include a complete Grok conductor implementation.

## ChatGPT

Expose a standard remote MCP endpoint with authenticated discovery and a separately enforced server-side policy. One possible transport is a private outbound tunnel plus an OAuth gateway; OpenAI Secure MCP Tunnel is another supported option when its account requirements are met. These are alternatives, not cumulative prerequisites.

Client app registration, OAuth consent, server authentication, tool discovery, and actual tool invocation are separate acceptance steps. An HTTP-ready tunnel does not prove ChatGPT has registered the application. Accurate MCP read/write/destructive annotations improve the client contract but do not replace server authorization. See [developer mode](https://developers.openai.com/api/docs/guides/developer-mode) and [Cloudflare Managed OAuth](https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/managed-oauth/).

## Common context contract

Recommended operations: status, search, projects, project_context, and source_fetch. Separate memory_write from read access. Source fetch resolves an opaque approved reference and re-authorizes it; it must not accept an arbitrary filesystem path or URL as authority. Return freshness and source coverage, including UNKNOWN, rather than silently reporting no activity when a dependency failed.

## Common conductor contract

A work request binds a tenant, project, canonical work item, claimed workspace, idempotency key, permitted tools, cost budget, and expiration. A returned receipt binds the run attempt to its actual provider thread/turn. Expose backend capabilities such as native steering, interrupt/resume, durable replay, sandbox, and supported approvals. Never claim capabilities inferred from another backend.

Lifecycle states: requested -> admitted -> running -> succeeded, failed, cancelled, or unknown. Connection loss enters unknown until reconciled; it is not success, cancellation, or permission to resubmit. Work completion requires artifact/test evidence in addition to a provider terminal event.

## Installation acceptance

Use synthetic projects to test all registered clients: recall, source navigation, scope denial, restart/reconnect, long-lived command recovery, and missing-dependency behavior. Test write and desktop operations separately in disposable owned resources. Preserve provider and operating-system enforcement; changing connectors must not be a way to retry a denied action.
