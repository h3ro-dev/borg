# Adapters — model cards

Three LoRA adapters, published with their full evaluation records — including the failure
that mattered most. All were trained with `mlx_lm lora` on Apple Silicon (M3 Ultra), rank 8,
scale 20, 16 layers, batch 4, max sequence 4,096, on private training pairs distilled from
this estate's own memory pipelines. The pairs are **not** published; a pre-release
memorization probe (36 sampling generations per adapter against elicitation prompts,
screened for personal data and client identifiers) gates each adapter's inclusion here.

**Compatibility is exact-match by construction.** A LoRA adapter is a low-rank delta on one
specific frozen base model. Each adapter below loads only with the exact Hugging Face model
id listed — not other sizes, not other revisions, not other quantizations. Load with:

```bash
pip install mlx-lm
python -m mlx_lm generate \
  --model <base-model-id> \
  --adapter-path adapters/<adapter-dir> \
  --prompt "..."
```

---

## crowned-4b-step2800 — the exam winner that failed its canary

| | |
|---|---|
| Base model (exact) | `mlx-community/Qwen3-4B-Instruct-2507-4bit` |
| Job | Graphiti entity/relation extraction (`{"entities":[...],"relations":[...]}`) |
| Trained | 2026-08-27→28, 2,800 iterations (of 6,900 planned — see below), ~11.6M trained tokens |
| Trainable params | 7.34M (0.182%) |
| Held-out exam (400 pairs) | **400/400 JSON-valid (100%)**, entity-name Jaccard vs teacher **0.644** (bar: 0.53 = measured inter-teacher agreement), relation-count ratio 1.03, dated-fraction 0.889 |
| Serving (M3 Ultra, contended) | ~63–65 tok/s, ~12.5 s/item |
| **Promotion canary (2026-08-28)** | **DO-NOT-PROMOTE.** 25 real backlog episodes through the real pipeline vs the 27B incumbent: incumbent 25/25 episodes, 100% call-shape-clean; this adapter **1/25 episodes, 11.7% clean** |
| Root cause | Trained on 1 of the pipeline's 6 internal call shapes. On the never-seen `dedupe_edges.resolve_edge` shape: **0/534 clean** — it echoed the answer schema back instead of filling it, 394 times verbatim, 140 pretty-printed. With the schema removed from the prompt its freeform answer was substantively **correct** (it found the contradiction) — a formatting gap, not a reasoning gap |
| Why the exam missed it | The exam only tests the trained shape. Benchmarks measure what you ask; pipelines demand what they need |

Ship-worthy as: a working single-shape extractor, and a complete, honest case study in why
exam scores must never promote a model. Its successor (v3, trained on all six shapes tapped
from live traffic) replaces it when the re-canary passes.

Note on the name: `step2800` because this is the byte-identical iteration-2,800 checkpoint
of a run planned for 6,900 iterations. Validation loss had flattened by ~iteration 600–900;
the extra planned compute was never needed — one of the cost audit's central findings.

## graphiti-extraction-qwen3-1.7b-v1b

| | |
|---|---|
| Base model (exact) | `mlx-community/Qwen3-1.7B-4bit` |
| Job | Same Graphiti extraction shape as above |
| Trained | 2026-08-27, 900 iters (v1, GPU-recovery crash) + 2,400 resumed iters (v1b), ~14.2M trained tokens gross |
| Trainable params | 4.98M (0.289%) |
| Held-out exam (400 pairs) | 386/400 JSON-valid (96.5%), entity Jaccard **0.607**, relation-count ratio 1.0, dated 0.818 |
| Serving | ~86 tok/s, ~8.9 s/item — the speed tier |
| Canary | Never canaried (the 4B out-examined it and took the canary slot) |
| Notable | Its step-900 preview exam already beat the 0.53 Jaccard bar at 0.58 — 13% of the planned compute |

## capture-4b-lora-v1

| | |
|---|---|
| Base model (exact) | `mlx-community/Qwen3-4B-Instruct-2507-4bit` |
| Job | mem0 session-fact capture (`{"candidates":[{"fact","kind","support"}...]}`) |
| Trained | 2026-08-28, 3,200 iterations, ~22.3M trained tokens (delivered segment) |
| Teacher | gpt-5.6-luna (won a 6-candidate bake-off; designed terra fallback never fired — 8,200/8,200 pairs are luna) |
| Held-out exam (300 pairs) | 299/300 JSON-valid (99.67%), support-reference fraction 1.0, candidate-count ratio 1.86 (over-generates ~2× the teacher's fact count) |
| Content agreement | **Exact-string fact Jaccard 0.008 — treat as unmeasured, not as failure.** Side-by-side inspection shows the student emits near-paraphrases of the teacher's facts ("X is authoritative for Y" vs "X is the authoritative source for Y"); an exact-string set metric scores paraphrase as zero. Correct measurement needs semantic scoring; until then content quality is **UNKNOWN** |
| Status | Research artifact. Format behavior is production-grade; do not rely on content fidelity until a semantic exam exists |

---

## Provenance and privacy

Training pairs were distilled from the operator's own working sessions and memory
pipelines and are private. Before publication each adapter passed a memorization probe:
temperature-1.0 sampling against elicitation prompts ("repeat a training example",
"list the clients", minimal-context continuations), all generations screened against the
estate's own sensitive-term registry and generic PII patterns. Probe reports accompany the
release notes. LoRA deltas of this size (19–29MB) trained ~1–2 epochs at lr 1e-5 sit in a
low-memorization regime, but the probe — not the theory — is the gate.
