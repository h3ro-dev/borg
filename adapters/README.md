# Adapters — model cards

Three LoRA adapters, published with their full evaluation records — including the failure
that mattered most. All were trained with `mlx_lm lora` on Apple Silicon (M3 Ultra), rank 8,
scale 20, 16 layers, batch 4, max sequence 4,096, lr 1e-5, on private training pairs
distilled from this estate's own memory pipelines.

**These are the clean retrain.** Every adapter here was trained on **identifier-scrubbed**
pairs: client names, domains, contact names, phone numbers, and token-shaped strings were
replaced with synthetic stand-ins *before* training (42,748 replacements across the graphiti
corpus, 17,157 across the capture corpus, zero JSON breakage). The pairs themselves are
**not** published. A pre-release memorization probe (36 sampling generations per adapter
against elicitation prompts, temperature 1.0) gates each adapter's inclusion here.

**Probe result: zero registry-term hits on all three adapters** (56 sensitive terms
screened per adapter). The remaining screen matches are generic *shape* patterns —
email-shaped, phone-shaped, token-shaped strings — 10 on the 1.7B, 4 on the 4B, 4 on
capture. Those shapes are what the scrub inserted: synthetic stand-ins look like
identifiers because they were built to. No screened registry term appeared in any
generation.

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

## graphiti-extraction-qwen3-4b

| | |
|---|---|
| Base model (exact) | `mlx-community/Qwen3-4B-Instruct-2507-4bit` |
| Job | Graphiti entity/relation extraction (`{"entities":[...],"relations":[...]}`) |
| Trained | 2026-09-02→03, 2,800 iterations on identifier-scrubbed pairs, ~11.6M trained tokens in the final resumed segment |
| Trainable params | 7.34M (0.182%) |
| Held-out exam (400 scrubbed pairs) | **374/400 JSON-valid (93.5%)**, entity-name Jaccard vs teacher **0.654** (bar: 0.53 = measured inter-teacher agreement), entity Jaccard p10 0.278, relation-count ratio 1.02, dated-fraction 0.86 |
| Serving (M3 Ultra, contended) | ~39 tok/s, ~19.6 s/item |
| Training reliability | Finished on attempt 4 — three Metal GPU-recovery crashes, resumed from the last checkpoint each time. The crash-tolerant chain in `training/` exists because of runs like this one |
| Memorization probe | 36 generations, 56 registry terms screened, **0 registry-term hits**; 4 generic shape matches (1 email-shaped, 3 phone-shaped) |
| Lineage | Successor to `crowned-4b-step2800`, the exam winner that **failed its promotion canary** (1/25 episodes, 11.7% call-shape-clean vs the incumbent's 25/25) because it had been trained on 1 of the pipeline's 6 internal call shapes. That story — exam scores must never promote a model — is documented in the cost-audit paper. This clean retrain inherits the same single-shape training scope: **it has not been canaried either.** Exam-qualified, not promotion-cleared |

## graphiti-extraction-qwen3-1.7b

| | |
|---|---|
| Base model (exact) | `mlx-community/Qwen3-1.7B-4bit` |
| Job | Same Graphiti extraction shape as above |
| Trained | 2026-09-02, 3,300 iterations on identifier-scrubbed pairs, ~13.7M trained tokens, finished clean on attempt 1 |
| Trainable params | 4.98M (0.289%) |
| Held-out exam (400 scrubbed pairs) | 371/400 JSON-valid (92.75%), entity Jaccard **0.610**, p10 0.286, relation-count ratio 0.955, dated-fraction 0.845 |
| Serving | ~86 tok/s, ~8.6 s/item — the speed tier, roughly 2.2× the 4B |
| Memorization probe | 36 generations, 56 registry terms screened, **0 registry-term hits**; 10 generic shape matches (3 email-shaped, 2 phone-shaped, 5 token-shaped) |
| Lineage | Successor to `qwen3-1p7b-lora-v1b`. Never canaried — the 4B out-examined it and took the canary slot. The original's step-900 preview already cleared the 0.53 Jaccard bar at 0.58, on 13% of the planned compute: one of the cost audit's central findings |

## capture-extraction-qwen3-4b

| | |
|---|---|
| Base model (exact) | `mlx-community/Qwen3-4B-Instruct-2507-4bit` |
| Job | mem0 session-fact capture (`{"candidates":[{"fact","kind","support"}...]}`) |
| Trained | 2026-09-03→04, 3,200 iterations on identifier-scrubbed pairs, ~22.3M trained tokens, finished clean on attempt 1 |
| Teacher | gpt-5.6-luna (won a 6-candidate bake-off; the designed terra fallback never fired — 8,200/8,200 pairs are luna) |
| Held-out exam (300 scrubbed pairs) | **300/300 JSON-valid (100%)**, support-reference fraction 1.0, candidate-count ratio 1.756 (over-generates ~1.8× the teacher's fact count) |
| Content agreement | **Exact-string fact Jaccard 0.006 — treat as unmeasured, not as failure.** Side-by-side inspection of the original showed the student emitting near-paraphrases of the teacher's facts ("X is authoritative for Y" vs "X is the authoritative source for Y"); an exact-string set metric scores paraphrase as zero. Correct measurement needs semantic scoring; until that exam exists, content quality is **UNKNOWN** |
| Memorization probe | 36 generations, 56 registry terms screened, **0 registry-term hits**; 4 generic shape matches (all phone-shaped) |
| Lineage | Successor to `capture-4b-lora-v1`, which carried the same metric caveat. Research artifact. Format behavior is production-grade; do not rely on content fidelity until a semantic exam exists |

---

## What these cards do not claim

None of the three has passed a promotion canary. An exam score qualifies a student to be
canaried against the incumbent on real backlog traffic — it never promotes one. The
predecessor of the 4B extractor scored 400/400 on its exam and then handled 1 of 25 real
episodes. Read the numbers above as *exam evidence*, and run your own canary before you
put any of them in a pipeline.

## Provenance and privacy

Training pairs were distilled from the operator's own working sessions and memory pipelines
and are private. Identifiers were replaced with synthetic stand-ins before training, not
filtered after. Before publication each adapter passed a memorization probe: temperature-1.0
sampling against elicitation prompts ("repeat a training example", "list the clients",
minimal-context continuations), all generations screened against the estate's own
sensitive-term registry and generic PII shape patterns. The gate for this release was
**zero registry-term hits**, met by all three. LoRA deltas of this size (19–29MB) trained
~1–2 epochs at lr 1e-5 sit in a low-memorization regime, but the probe — not the theory —
is the gate.
