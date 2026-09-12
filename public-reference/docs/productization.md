# Product direction and delivery plan

## Recommended first offering

Start with a paid, single-customer installation and maintenance service around a portable open core. Sell continuity of project context across authorized agents, dependable source navigation, and observable task handoffs. Do not sell a bundle of the operator's subscriptions or an unrestricted remote desktop as a multi-tenant service.

Customer-owned storage and provider accounts make the installation boundary easier to explain, but do not automatically solve software licensing, provider terms, security, or privacy obligations. The initial offering should use audited existing models without automatic training on customer data.

## Public and commercial boundaries

Publish portable interfaces, the memory/event contract, provider adapter interfaces, source adapters, synthetic examples, reproducible evaluation tools, and the secure reference deployment once accepted. Retain applicable notices. Keep all customer data, credentials, account inventories, private governance records, and deployment receipts outside the public release.

A possible commercial layer is managed installation, support, policy administration, organization identity/SSO integration, signed upgrades, compliance evidence, fleet dashboards, audited recovery and customer-specific integration. These are proposed boundaries, not a claim that the modules already exist or that dependencies permit every hosted deployment.

Previously MIT-licensed source remains available under its issued license. Separate new components deliberately rather than claiming exclusive ownership of an upstream library or rescinding earlier grants. Keep weights and datasets in separate release processes.

## One product, modular internals

Use a single install/configuration command and one health dashboard. Internally separate context, source authorization, model execution, work state, and privileged execution. Define one versioned configuration format with external secret references. Ship no personal email defaults, hardcoded local usernames, operator home paths, or assumed account routing.

Build provider interfaces for `recall`, `capture`, `source_fetch`, and run lifecycle. Support an explicit capability matrix so a backend without native mid-turn steering cannot pretend to offer it. A graph backend remains replaceable only after parity tests for temporal queries, scope denial, migration and deletion.

## Roadmap with acceptance gates

| Stage | Deliverable | Gate before moving on |
| --- | --- | --- |
| A: architectural release | This reference, explicit export list, tests and license map | Exact archive reviewed; no live payloads or weights; limitations visible |
| B: reproducible core | Reconciled source, all imported modules, dependency lock/SBOM, settings migration | Clean-machine setup; unit/integration suites; backup and restore; no dependency on original workstation |
| C: trusted memory | Durable ingestion/outbox, scope-at-write, independently visible projection lag, source resolver | Tenant/scope adversarial tests; duplicate and crash recovery; deletion receipts; semantic retrieval benchmark |
| D: client parity | Supported versions of Claude, Codex, Grok and ChatGPT integrations | Native lifecycle and reconnect acceptance for each client; no unsupported credential reuse |
| E: safe operations | Workspace isolation, claims, durable run receipts, expected-turn control | Disconnect/restart tests; no duplicate dispatch; approval handling; least-privilege operation profiles |
| F: customer pilot | Customer-owned deployment, documentation, recovery and support process | Measured use-case value, support burden, right-sized compute, provider and license review |
| G: hosted offering | Identity, tenant isolation, billing, compliance and service operations | Database license cleared; cross-tenant tests; incident/retention procedures; commercial terms signed |
| H: optional learned models | Rights-cleared corpora and separately packaged adapters | Independent rights, privacy, semantic quality, exact-checkpoint canary gates |

Do not make model retraining a dependency of the first useful customer install. Model improvements and hosted service development can proceed independently after their own risk gates.

## Evaluate value before setting a price

Pilot metrics: time to first useful project answer, correct source resolution, unsupported-fact rate, freshness accuracy, avoided duplicate work, recovery success, installation effort, and support hours. Report p50/p95 latency and cold-start behavior rather than one best-case query. Test tasks with memory disabled as a baseline.

A useful planning equation is contribution per customer = installation/support revenue minus attributable labor, infrastructure, provider usage, licensing, and incident reserve. Set explicit usage budgets and customer billing responsibility. Price ranges without demand and cost evidence are hypotheses, not forecasts.

## What this release does not establish

It is not a clean-machine runtime, audited enterprise sandbox, complete adapter distribution, legal clearance, trademark search, or evidence of paying customers. It is a constrained, inspectable architectural starting point for those deliverables. Production and customer readiness require the later gates above.
