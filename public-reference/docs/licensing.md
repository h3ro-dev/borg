# Commercialization and third-party rights

Research checkpoint: 2026-09-12. This is an engineering issue map, not a legal opinion, license grant, freedom-to-operate clearance, or representation that every historic artifact is distributable. Review the actual version, distribution method, signed order form, and terms in effect when source data was generated.

## Selling software is not automatically infringement

The project's MIT license permits selling copies while retaining its required notice. That does not grant rights in every dependency, provider account, training corpus, model, trademark, or customer record. Charge for original integration, installation, maintenance, and properly licensed services rather than representing third-party technology as exclusively owned.

[MIT license](https://opensource.org/license/mit).

## Dependency decisions

| Component | Upstream license / contract observed | Release consequence |
| --- | --- | --- |
| mem0 OSS | Apache-2.0 | Retain license, applicable notices and change disclosures; hosted mem0 services have separate terms. |
| Graphiti | Apache-2.0 | The framework license is not the license of every supported database. |
| Qdrant server/client | Apache-2.0 | Preserve attribution; pin the actual versions in an installable distribution. |
| FalkorDB server | SSPL v1 | Assess its service-source obligation before hosted delivery. Its Python client being MIT does not change the server license. |
| FastMCP | Apache-2.0 in the inspected release | Do not describe the whole stack as MIT simply because BORG is MIT. |
| MCP Python SDK | MIT | Preserve the selected SDK's own notice. |
| Codex CLI source | Apache-2.0 | Source reuse and OpenAI hosted-service access are separate permissions. |
| Desktop Commander / Peekaboo | MIT in the inspected upstream source | Preserve notices and review any patches; do not bundle user config or runtime dependencies blindly. |
| Ollama / MLX-LM | MIT in inspected upstream source | Serving software licenses do not license the models they load. |
| Qwen3 1.7B / 4B bases used by research adapters | Apache-2.0 model-card declarations | Pin upstream and conversion revisions; identify adapter and base separately. |
| Llama 3.1 fallback | Llama 3.1 Community License | Custom attribution, redistribution, naming and commercial conditions apply; not a permissive OSS substitute by default. |
| nomic embedding model family | Upstream v1.5 card declares Apache-2.0 | Resolve a mutable local tag to the exact model/version before approving it. |

Primary licenses: [mem0](https://github.com/mem0ai/mem0/blob/main/LICENSE), [Graphiti](https://github.com/getzep/graphiti/blob/main/LICENSE), [Qdrant](https://github.com/qdrant/qdrant/blob/master/LICENSE), [FalkorDB](https://github.com/FalkorDB/FalkorDB/blob/master/LICENSE.txt), [FastMCP](https://github.com/jlowin/fastmcp/blob/main/LICENSE), [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk/blob/main/LICENSE), [Codex](https://github.com/openai/codex/blob/main/LICENSE), [Desktop Commander](https://github.com/wonderwhy-er/DesktopCommanderMCP/blob/main/LICENSE), [Peekaboo](https://github.com/steipete/Peekaboo/blob/main/LICENSE), [Ollama](https://github.com/ollama/ollama/blob/main/LICENSE), [MLX-LM](https://github.com/ml-explore/mlx-lm/blob/main/LICENSE), [Qwen3 4B conversion](https://huggingface.co/mlx-community/Qwen3-4B-Instruct-2507-4bit), [Qwen3 1.7B conversion](https://huggingface.co/mlx-community/Qwen3-1.7B-4bit), [Llama 3.1](https://github.com/meta-llama/llama-models/blob/main/models/llama3_1/LICENSE), [nomic](https://huggingface.co/nomic-ai/nomic-embed-text-v1.5).

SSPL is not OSI-approved open source. Its section 13 can require extensive service-source disclosure when the covered functionality is offered as a service. Obtain an architecture-specific determination, appropriate vendor rights, or evaluate a compatible alternative. Installing in a customer's environment changes the delivery analysis but does not automatically remove obligations. [OSI explanation](https://opensource.org/blog/the-sspl-is-not-an-open-source-license).

## Hosted model providers

OpenAI permits API-based customer applications. Its services agreement also restricts account resale, credential transfer, usage-limit circumvention, and certain competing-model development using outputs. Its stated categorization/classification/organization exception is limited to models not distributed or commercially available to third parties. A research adapter needs its own training-rights analysis; owning an output does not erase these conditions. [Agreement](https://openai.com/policies/services-agreement/).

Anthropic distinguishes ordinary use of native applications from third-party products. Its Claude Code documentation directs product/SDK integrations to API-key authentication rather than repurposed consumer-subscription OAuth. Its commercial terms restrict competing-model/service development absent approval. Connecting a memory MCP server to a person's native client is a different activity from selling access to a pooled agent account. [Claude Code compliance](https://code.claude.com/docs/en/legal-and-compliance), [commercial terms](https://www.anthropic.com/legal/commercial-terms).

xAI's enterprise terms permit API-backed bundled services but restrict using outputs to train, fine-tune, or improve other models unless an Order Form expressly allows it. Do not assume a general output-ownership clause clears a distillation corpus. [Enterprise terms](https://x.ai/legal/terms-of-service-enterprise).

These are separate contractual risks, not a finding that a particular historical adapter infringes copyright. Determine which contract covered the actual teacher calls and obtain any needed permission before distributing weights. A local teacher also requires its own model-license and input-rights review.

## Authorship, confidentiality, and naming

Confirm employee/contractor contribution rights and third-party code provenance. Keep licenses and a software bill of materials in releases. Purely machine-generated output and human-authored edits can have different copyright treatment; record meaningful human authorship and review. [U.S. Copyright Office](https://www.copyright.gov/newsnet/2025/1060.html).

Use BORG as a working project label, not a claim of trademark clearance. A proper search assesses related goods/services and confusingly similar marks, not just an exact GitHub name. Avoid fictional-franchise imagery, provider logos, or statements implying endorsement without authorization. [USPTO guidance](https://www.uspto.gov/trademarks/search/likelihood-confusion).

Have counsel review the selected database delivery, teacher-output rights, model redistribution, customer privacy agreements, contribution ownership, and brand before a commercial launch. Preserve already-issued licenses; a new commercial tier does not retroactively withdraw permissions previously granted for public code.
