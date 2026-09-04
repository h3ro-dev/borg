---
license: mit
base_model: mlx-community/Qwen3-1.7B-4bit
tags:
  - lora
  - mlx
  - adapter
  - knowledge-graph
  - information-extraction
  - borg
library_name: mlx
---

# Borg — Graphiti extraction (Qwen3 1.7B LoRA)

Part of **the Borg** — a local-first shared memory system for AI agents
(machinery, papers, and honest promotion gates: https://github.com/h3ro-dev/borg).

Turns a text episode into a strict Graphiti extraction object — `{"entities":[...],"relations":[...]}` — for a temporal knowledge graph. This is the speed tier: roughly 2.2x the throughput of the 4B sibling at a few points less format validity.

**Works only with the exact base model** `mlx-community/Qwen3-1.7B-4bit` (a LoRA adapter is a
delta on specific frozen weights). Load with:

```bash
pip install mlx-lm
python -m mlx_lm generate --model mlx-community/Qwen3-1.7B-4bit --adapter-path <this-repo> --prompt "..."
```

## Evaluation (held-out exam, this exact checkpoint)

| Metric | Value |
|---|---|
| Held-out exam | 400 scrubbed pairs |
| JSON-valid | 371/400 (**92.75%**) |
| Entity-name Jaccard vs teacher | **0.610** (bar: 0.53 = measured inter-teacher agreement) |
| Entity Jaccard p10 | 0.286 |
| Relation-count ratio | 0.955 |
| Dated-fraction | 0.845 |
| Serving (M3 Ultra, contended) | ~86 tok/s, ~8.6 s/item |
| Training | 3,300 iterations, ~13.7M trained tokens, 4.98M trainable params (0.289%) |
| Memorization probe | 36 generations, 56 registry terms screened — **0 registry-term hits** |
| Promotion canary | **Not canaried.** Exam-qualified only |

## Training & provenance

Trained with `mlx_lm lora` (rank 8, 16 layers, batch 4, seq 4096, lr 1e-5) on
**identifier-scrubbed** pairs distilled from a working single-operator estate:
client names, domains, contact names, phone numbers, and token-shaped strings
were replaced with synthetic stand-ins before training. Pre-release gate: an
adversarial memorization probe (elicitation prompts, temperature 1.0) screened
against the estate's own sensitive-term registry — this release required zero
registry-term hits. The training pairs themselves are private; the full
pipeline to build your own from your own traffic is open source in the repo.
