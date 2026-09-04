---
license: mit
base_model: mlx-community/Qwen3-4B-Instruct-2507-4bit
tags:
  - lora
  - mlx
  - adapter
  - knowledge-graph
  - information-extraction
  - borg
library_name: mlx
---

# Borg — Graphiti extraction (Qwen3 4B LoRA)

Part of **the Borg** — a local-first shared memory system for AI agents
(machinery, papers, and honest promotion gates: https://github.com/h3ro-dev/borg).

Turns a text episode into a strict Graphiti extraction object — `{"entities":[...],"relations":[...]}` — for a temporal knowledge graph. The accuracy tier: the best entity agreement of the three, at roughly half the 1.7B's speed.

**Works only with the exact base model** `mlx-community/Qwen3-4B-Instruct-2507-4bit` (a LoRA adapter is a
delta on specific frozen weights). Load with:

```bash
pip install mlx-lm
python -m mlx_lm generate --model mlx-community/Qwen3-4B-Instruct-2507-4bit --adapter-path <this-repo> --prompt "..."
```

## Evaluation (held-out exam, this exact checkpoint)

| Metric | Value |
|---|---|
| Held-out exam | 400 scrubbed pairs |
| JSON-valid | 374/400 (**93.5%**) |
| Entity-name Jaccard vs teacher | **0.654** (bar: 0.53 = measured inter-teacher agreement) |
| Entity Jaccard p10 | 0.278 |
| Relation-count ratio | 1.02 |
| Dated-fraction | 0.86 |
| Serving (M3 Ultra, contended) | ~39 tok/s, ~19.6 s/item |
| Training | 2,800 iterations, ~11.6M trained tokens in the final resumed segment, 7.34M trainable params (0.182%); finished on attempt 4 after three Metal GPU-recovery crashes |
| Memorization probe | 36 generations, 56 registry terms screened — **0 registry-term hits** |
| Promotion canary | **Not canaried.** Its predecessor scored 400/400 on the same exam shape and then handled 1 of 25 real pipeline episodes — it had been trained on 1 of the pipeline's 6 internal call shapes. This retrain inherits that same single-shape scope. Run your own canary |

## Training & provenance

Trained with `mlx_lm lora` (rank 8, 16 layers, batch 4, seq 4096, lr 1e-5) on
**identifier-scrubbed** pairs distilled from a working single-operator estate:
client names, domains, contact names, phone numbers, and token-shaped strings
were replaced with synthetic stand-ins before training. Pre-release gate: an
adversarial memorization probe (elicitation prompts, temperature 1.0) screened
against the estate's own sensitive-term registry — this release required zero
registry-term hits. The training pairs themselves are private; the full
pipeline to build your own from your own traffic is open source in the repo.
