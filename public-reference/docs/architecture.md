# One system, explicit boundaries

## Purpose

Help an authorized agent answer three different questions: what was previously learned, what source supports it, and what work is currently happening. These require different evidence. Memory is a rebuildable retrieval layer, not a replacement for repositories, task ledgers, or current user instructions.

```text
Claude / Codex / Grok / ChatGPT / other MCP clients
                    |
           authenticated context gateway
                    |
   +----------------+-------------------+
   |                |                   |
context service   source resolver    work-status service
   |                |                   |
mem0 + vector DB  approved files     task / run / event ledger
   |
versioned projection / outbox
   |
Graphiti + temporal graph backend

separately authorized operation gateway -> isolated workspace runner
                                      -> optional desktop broker
```

These are logical boundaries, not a requirement to deploy many microservices. A single-customer process can implement several interfaces, but memory reads must not implicitly grant command execution.

## Capture and recall

A client adapter observes documented lifecycle events, identifies a source and version, and requests scoped context. It does not scrape credentials, import a whole home directory, or assume all clients have the same hook API. Capture enqueues a bounded pointer or sanitized candidate rather than blocking session teardown on model inference. A unavailable recall service should not break an otherwise valid agent session; an unavailable authorization check must deny access.

The context service resolves the authenticated principal and project namespace server-side. Filter before vector or graph retrieval, not only after ranking. Missing or ambiguous scope is private by default. Cache keys include principal, tenant, scopes, policy version, project, and source version. A memory ID is not a capability to read its original document.

Each result carries a source reference, observation time, ingestion time, validity interval where applicable, confidence/evidence state, and retrieval reason. Re-fetch an approved source before consequential work. Treat memory prose as untrusted data, including remembered instructions.

## One logical write, explicit projection lag

Use a durable ingestion record with an idempotency key and an outbox for downstream projections. Do not claim an atomic transaction across a vector store and graph simply because two calls returned. Model extraction, scope assignment, vector writes, graph episode processing, and deletion can fail independently.

Record source event/version and independent vector and graph watermarks. A recent graph scan does not establish recent source ingestion. Retry only deduplicated operations; retain failed records in a bounded recovery queue. Rebuild derived indexes from approved sources and retained event metadata. For deletion, track graph, vector, caches, backups, and any training datasets separately; changing memory rows cannot remove knowledge from released weights.

## Inbox coordination is not memory or execution

Reuse the existing Agent Inbox for authenticated agent-to-agent messages, current assignment owners and versions, delegated grants, delivery leases, acknowledgments, and discoveries. BORG project orientation may join read-only Inbox metadata by exact work ID and scope, but it must not automatically poll or acknowledge work while gathering context. Beads remains the canonical work/acceptance record; conductors retain execution attempts and provider thread/turn receipts. See [Inbox integration](inbox-integration.md).

## Work is not memory

Maintain distinct IDs for work item, owner claim, workspace, run attempt, provider thread, and provider turn. Preserve event sequence and terminal reason. A process being alive does not prove a task is owned or progressing. A stale ledger row saying RUNNING is historical evidence, not current status.

Before retrying a disconnected dispatch, reconcile its attempt and provider thread. Require expected-turn checks for steering. Persist an operation receipt before launch. Network interruption must not silently create a duplicate job. Disclose which backends implement native steering and which implement interruption followed by resume.

## Model roles

Embedding, fact extraction, entity/relation extraction, reranking, sensitivity classification, scope classification, and consolidation are different tasks. Give each its own model/adapter manifest, evaluation corpus, and promotion gate. Quality on one JSON shape does not qualify a checkpoint for all Graphiti calls. A valid support-reference field does not prove that the referenced source entails the fact.

## Deployment forms

Start with one customer-owned installation and customer-owned credentials. A hosted control plane can later manage versioned installations without receiving their memory payloads by default. A shared multi-tenant memory service requires independently tested tenant isolation, source authorization, encryption/key separation, deletion, and incident procedures; it is not achieved by replacing an owner email with a customer email.

The architecture is a target contract. Existing historical modules require reconciliation before they can be claimed to implement all of it.
