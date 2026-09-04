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

# Borg — session-fact capture (Qwen3 4B LoRA)

Part of **the Borg** — a local-first shared memory system for AI agents
(machinery, papers, and honest promotion gates: https://github.com/h3ro-dev/borg).

Reads an agent session transcript and emits durable memory candidates — `{"candidates":[{"fact","kind","support"}...]}` — each carrying a reference back to the text that supports it. Format behavior is production-grade; content fidelity is not yet measured (see below).

**Works only with the exact base model** `mlx-community/Qwen3-4B-Instruct-2507-4bit` (a LoRA adapter is a
delta on specific frozen weights). Load with:

```bash
pip install mlx-lm
python -m mlx_lm generate --model mlx-community/Qwen3-4B-Instruct-2507-4bit --adapter-path <this-repo> --prompt "..."
```

## Evaluation (held-out exam, this exact checkpoint)

| Metric | Value |
|---|---|
| Held-out exam | 300 scrubbed pairs |
| JSON-valid | 300/300 (**100%**) |
| Support-reference fraction | 1.0 (every candidate cites its supporting text) |
| Candidate-count ratio | 1.756 (over-generates ~1.8x the teacher's fact count) |
| Exact-string fact Jaccard | 0.006 — **treat as unmeasured, not as failure** |
| Serving (M3 Ultra, contended) | ~8.1 tok/s, ~29.6 s/item |
| Training | 3,200 iterations, ~22.3M trained tokens, 7.34M trainable params (0.182%) |
| Memorization probe | 36 generations, 56 registry terms screened — **0 registry-term hits** |
| Promotion canary | **Not canaried.** Research artifact |

**On that 0.006.** Side-by-side inspection of this adapter's predecessor showed the student
emitting near-paraphrases of the teacher's facts ("X is authoritative for Y" vs "X is the
authoritative source for Y"). An exact-string set metric scores a paraphrase as zero.
Correct measurement needs semantic scoring; until that exam exists, content quality here is
**UNKNOWN**. Do not rely on content fidelity. The format numbers above are real.

## Training & provenance

Trained with `mlx_lm lora` (rank 8, 16 layers, batch 4, seq 4096, lr 1e-5) on
**identifier-scrubbed** pairs distilled from a working single-operator estate:
client names, domains, contact names, phone numbers, and token-shaped strings
were replaced with synthetic stand-ins before training. Pre-release gate: an
adversarial memorization probe (elicitation prompts, temperature 1.0) screened
against the estate's own sensitive-term registry — this release required zero
registry-term hits. The training pairs themselves are private; the full
pipeline to build your own from your own traffic is open source in the repo.
