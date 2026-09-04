---
license: mit
base_model: {{BASE_MODEL}}
tags:
  - lora
  - mlx
  - adapter
  - knowledge-graph
  - information-extraction
  - borg
library_name: mlx
---

# {{TITLE}}

Part of **the Borg** — a local-first shared memory system for AI agents
(machinery, papers, and honest promotion gates: https://github.com/h3ro-dev/borg).

{{JOB_LINE}}

**Works only with the exact base model** `{{BASE_MODEL}}` (a LoRA adapter is a
delta on specific frozen weights). Load with:

```bash
pip install mlx-lm
python -m mlx_lm generate --model {{BASE_MODEL}} --adapter-path <this-repo> --prompt "..."
```

## Evaluation (held-out exam, this exact checkpoint)

{{EVAL_BLOCK}}

## Training & provenance

Trained with `mlx_lm lora` (rank 8, 16 layers, batch 4, seq 4096, lr 1e-5) on
**identifier-scrubbed** pairs distilled from a working single-operator estate:
client names, domains, contact names, phone numbers, and token-shaped strings
were replaced with synthetic stand-ins before training. Pre-release gate: an
adversarial memorization probe (elicitation prompts, temperature 1.0) screened
against the estate's own sensitive-term registry — this release required zero
registry-term hits. The training pairs themselves are private; the full
pipeline to build your own from your own traffic is open source in the repo.
