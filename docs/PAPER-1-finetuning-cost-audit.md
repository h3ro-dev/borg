# What Fine-Tuning Actually Cost: A Full Audit

*The Borg estate, audited 2026-08-29. Five LoRA training runs, three adapters, two memory
systems, one merciless promotion gate — and every token counted.*

---

## The one-paragraph answer

Training the models cost almost nothing. **Every token that touched a model during this
program — teacher data generation, five training runs, all exams and canaries — prices out
at about $75 at public API list rates** (≈$52 with the 30% enterprise discount we assume,
under $1 in actual marginal cash because everything ran on owned hardware, local models,
and free preview windows). The real cost was the intelligence *around* the training:
allocated at the owner's stated subscription rates, the AI-agent engineering that designed,
ran, debugged, and gated this program comes to **≈$2,754** for the fine-tuning work alone
(≈$4,956 for the whole memory-system program it lives inside). The cost of fine-tuning is
not the tokens. It is the engineering. And the engineering is exactly the part the
open-sourced harness makes reusable.

---

## 1. Stated assumptions (read these first)

1. **Claude subscription: $2,000 per week = 100% capacity.** Owner-stated planning rate.
   Every week is treated as fully consumed (the fleet ran saturated through this window;
   usage-limit stalls are on record).
2. **Codex subscription: $14,000 per month = $3,500 per week = 100% capacity** across the
   account fleet. Owner-stated planning rate.
3. **Attribution window: 2026-08-19 → 2026-08-29** (10.4 days = 1.486 weeks): memory-layer
   build began 08-19, pair harvesting 08-20, student training 08-27→29 (v3 was still
   training when this audit ran).
4. **Attribution method: session-transcript byte share.** Every Claude session (142 files,
   757MB) and every Codex rollout (11,603 files, 9,083MB, all account homes) modified in
   the window was scanned in full and tagged by fine-tuning keyword frequency (`mlx_lm`,
   `lora`, `adapters.safetensors`, `training pairs`, `canary-arm`, `exam-4b`, …) with a
   ≥40-hit threshold so shared session preambles cannot false-tag. Bytes proxy tokens;
   JSONL overhead inflates numerator and denominator alike.
5. **Token counts are measured, not guessed.** Training tokens come from `mlx_lm`'s own
   `Trained Tokens` counters summed across every resume segment. Dataset and teacher
   tokens come from the real Qwen3-4B tokenizer run offline over 400-line random samples
   (seed 42) per corpus, extrapolated by mean × line count — and used as a single
   cross-teacher proxy (true per-provider tokenizers differ ±10–20%).
6. **Prices are public list, retrieved 2026-08-29** from official pricing pages
   (Anthropic, OpenAI, xAI, Alibaba, DeepSeek; sources in the appendix). GPT-5.6 Sol is in
   a promo ($4/$20; durable list $5/$30). Sonnet 5's $2/$10 is now standard.

## 2. What was actually built (the thing being costed)

Five `mlx_lm` LoRA runs on Apple Silicon across 2026-08-27→29:

| Run | Base | Iters done / planned | Trained tokens (measured) | Wall clock | Outcome |
|---|---|---|---|---|---|
| v1 (1.7B) | Qwen3-1.7B-4bit | 1,040 / 6,900 | 4,313,068 | 2h09m | GPU-recovery crash |
| v1b (resume) | 〃 | 2,400 / 2,400 | 9,906,458 | 2h16m | Exam 96.5% / 0.607 |
| v2 (4B) | Qwen3-4B-2507-4bit | 3,060 / 6,900 | 12,662,666 | 6h08m | Stopped unexplained; **step-2,800 checkpoint crowned** |
| capture (4B) | 〃 | 3,200 / 3,200 (3 launches) | 28,831,157 gross / 22,279,941 delivered | ~16h span | Exam 99.67% JSON; content metric unusable |
| v3 (4B) | 〃 | 5,000 planned (live at audit) | ~23.1M projected (4.70M @ 20%) | ~13.5h projected | The canary-fix retrain |

**Gross training compute: ≈78.8M trained tokens, ≈39 GPU-hours.**

The crowned student scored 400/400 format-valid, 0.644 entity-agreement on the held-out
exam — then **failed the promotion canary 1-of-25** against the 27B incumbent because it
had been trained on one of the pipeline's six call shapes and scored 0/534 on a shape it
had never seen. That verdict, not any benchmark, is why v3 exists — trained on all six
shapes recorded from live traffic.

## 3. Rate #1 — Subscription-allocated cost (the real bill)

| | Claude | Codex | Total |
|---|---|---|---|
| Window spend at stated rates (1.486 wk) | $2,971 | $5,200 | $8,171 |
| Fine-tuning share of session bytes | 17.1% | 43.2% | — |
| **Fine-tuning-only allocation** | **$508** | **$2,246** | **$2,754** |
| Whole-Borg-program share | 50.4% | 66.5% | — |
| **Whole-program allocation** | **$1,497** | **$3,458** | **$4,956** |

Not included: the Grok subscription (one bake-off candidate + research lanes — immaterial),
Ox Alpha (free evaluation window, $0), electricity (counted in Rate #4).

## 4. Rate #2 — Straight-line public API equivalent

What the same tokens would have cost bought at list, per official 2026-08-29 pricing.

**Teacher-side (data generation), measured tokens × list rates:**

| Teacher | Pairs | Tokens (in / out) | Rate basis | List cost |
|---|---|---|---|---|
| qwen3.8:27b (local fleet) | 12,658 | 3.44M / 9.68M | Together serverless 27B-class, $0.90/M flat | $11.81 |
| Ox Alpha (hosted free preview) | 12,612 | 3.39M / 9.17M | same proxy (actual: $0) | $11.31 |
| gpt-5.6-luna (capture teacher) | 7,026 | 10.81M / 1.76M | $0.20 / $1.20 | $4.28 |
| luna + terra pilots (never used) | 660 | 0.32M / 1.56M | luna + terra list | $3.04 |
| 6-model teacher bake-off | 254 calls | 0.37M / 0.08M | per-model list | $0.90 |
| **Recorder (shim tee, 6 shapes)** | 3,326 | — | **tee on live traffic** | **$0.00** |
| Teacher subtotal | | ≈26.9M | | **≈$31.3** |

**Training compute at hosted-LoRA rates** (Fireworks $0.50/M, Together $0.48/M training
tokens for ≤16B LoRA): 78.8M × $0.50 = **≈$39.4**. (OpenAI's fine-tuning platform is
winding down — new users cannot start — so open-model hosts are the benchmark.)

**Evaluation inference** (≈1,900 graded exam generations, 842 canary calls, probes):
**≈$4** — generous.

> **Rate #2 total: ≈$75 at public list.**

Cross-checks: pricing the local 27B at Alibaba's managed qwen3.5-plus rates instead of the
Together flat proxy moves its line from $11.81 to $24.61 — the total stays under $90 on
any defensible proxy choice. Luna at its official $0.20/$1.20 is the quiet star: the
entire 7,026-pair capture corpus cost $4.28 of teacher tokens at list.

## 5. Rate #3 — Enterprise-discounted

| Basis | Total |
|---|---|
| Owner's stated assumption: flat 30% off | **≈$52** |
| Evidence-based committed-use band, 15–50% off list | $37–$64 |
| No-negotiation path anyone has: Batch APIs at −50% on token spend | ≈$57 |

Where the 15–50% band comes from (primary sources, retrieved 2026-08-29): Google 1-year
provisioned throughput = 26% off monthly PT; AWS Bedrock reserved-tier guidance = 30–50%
with commitment (published provisioned examples run 44–52%); OpenAI reserved capacity ≈15%
(1-year vs 3-month); batch/flex tiers at 50% are public at OpenAI, Anthropic, Bedrock, and
Vertex. Azure's famous 64–70% PTU reservation savings are off *capacity-hours*, not
per-token list, and are excluded. First-party private-deal percentages are not published;
the owner's 30% sits comfortably inside the defensible band.

## 6. Rate #4 — Actual marginal cash

Owned hardware, local models, free preview windows, flat-rate subscriptions already paid:
**≈$0.71 of electricity** (≈39 GPU-hours × ~150W × $0.12/kWh) and nothing else.

## 7. The 36:1 finding

$2,754 of agent-engineering per $75 of model tokens. Fine-tuning's cost lives in the loops
*around* training: designing harvests, babysitting crashed runs, building exams, arguing
with canaries. That is precisely the part that is now a reusable, open-sourced harness —
which converts the next program's $2,754 into something much closer to its $75.

v3 is the existence proof: its marginal cost was ≈$11.5 of training-compute equivalent and
**$0 of data** (recorder pairs) — because the harness already existed.

## 8. The excess: what we over-bought, measured

The owner's hypothesis going in: *"we got ~15,000 pairs; 5,000 were enough; really 3,000."*
The evidence agrees — and sharpens where the real deficit was.

**Volume was over-provisioned ≈4×:**
- 25,270 pairs harvested from two teachers → 14,457 unique (42.8% discarded before
  training touched them). The second teacher (Ox) duplicated 83.7% of the first's
  episodes: +100% token spend for +14.8% unique pairs — 6.2× worse economics per kept pair.
- Validation loss flattened by iteration 600–900 on *both* model sizes — ≈3,600
  pair-presentations, under 27% of one epoch. The 1.7B passed the quality bar (0.58 vs
  0.53) at step 900: **13% of its planned compute**. The crowned 4B is the step-2,800
  checkpoint of a 6,900-step plan: **59% of its planned compute was never needed**.
- Marginal quality per extra compute, measured: v2's val loss 0.393 at step 600 → 0.356
  at step 3,000 (−9% for 5× the compute); exam agreement 0.58 at 13% of training → 0.644
  at 41%. Classic asymptote.

**Coverage was under-provisioned 6×:** the pipeline makes six call shapes; the corpus
covered one. The exam (drawn from the same one shape) could not see it; the canary could:
0/534 on `resolve_edge`, verdict DO-NOT-PROMOTE. The fix needed just 2,201 new pairs — of
the *right shapes*, recorded free from live traffic — plus a 2× reweight, not another
13,657-pair harvest.

**Right-sized plan, in hindsight:** ~500–900 pairs per call shape × 6 shapes (≈3,000–5,400
total — the owner's "3–5k" almost exactly), one epoch with exam-at-checkpoint early
stopping (~≤1,500 iterations), single teacher, recorder from day zero. At list rates that
is ≈$25–30 instead of ≈$75 — but the dominant saving is upstream: roughly half the
harvest-and-babysit engineering inside the $2,754 attaches to the excess (the duplicate
second-teacher harvest, the over-long runs, the crash-resume cycles around them).

## 9. The densing-law frame: why the harness is the asset

Capability density of open models doubles roughly every 3.3–3.5 months (Xiao et al.,
arXiv:2412.04315; published in *Nature Machine Intelligence* 7:1823–1833, 2025). Every
adapter is therefore a **depreciating asset with a ~one-quarter half-life**: the base
models it beats today are matched by something smaller and cheaper within months.

Our own program ran ahead of that curve: the crowned adapter went from exam triumph to
benched *in 26 hours* — not by densing but by its own promotion gate. Either way the
conclusion is identical, and it answers the question a reader posed while this audit ran
(if density keeps doubling, doesn't the durable value sit in the harness?): **yes — the
integral value is the harness.** The harvester, the grammar shim that turns production
traffic into labeled pairs, the exam, the canary, the crash-tolerant training chain — that
machinery re-targets any new base model for ≈$11 of compute-equivalent and a half-day of
wall clock, forever. Train adapters like you buy milk, not like you buy a house. That is
also the strongest argument for open-sourcing the adapters themselves: their moat value
decays on a fast clock, while their teaching value (honest cards, reproducible gates) does
not.

## 10. What we would do differently (and what you can skip entirely)

1. **Constrained decoding before fine-tuning.** The grammar shim already forces valid
   schema output from stock local models — the exact failure class that killed the canary.
   Fine-tune for judgment and speed, never for JSON shape.
2. **Off-the-shelf floor first.** Zero-shot joint entity/relation extractors exist on
   Hugging Face (GLiNER-Relex family; NuExtract-class structured extractors) — no
   pre-trained adapter for *our* schemas exists (LoRA adapters are base-and-task specific,
   which is also why ours only fit their exact bases), but a zero-shot floor should be
   measured before any distillation is funded.
3. **One teacher until the pilot proves a second adds unique coverage.** Measure dedupe
   rate on a 500-pair pilot before buying a full second harvest.
4. **Recorder from day zero.** 168 pairs/hour, all six shapes, $0. The corpus is a flow,
   not a stock.
5. **Early-stop on the exam, not the plan.** Checkpoint every 200–300 steps, exam each
   checkpoint, stop at the elbow. Compute plans written before the loss curve are fiction.
6. **Semantic metrics for sentence-facts.** Exact-string Jaccard scored correct paraphrases
   as 0.008 and nearly condemned a working adapter.
7. **Batch APIs for any paid teacher** (−50%, no negotiation), and cache-priced prompts
   for repeated harvest preambles.
8. **Keep the incumbent warm.** The 27B fallback is why a failed canary cost a day, not an
   outage.

## Appendix A — price sources (all retrieved 2026-08-29)

Anthropic platform pricing page (Opus 5 $5/$25, Sonnet 5 $2/$10, Haiku 4.5 $1/$5, batch
−50%, cache read 0.1×) · OpenAI developer pricing (GPT-5.6 Sol $4/$20 promo to ≥2026-11-21,
durable $5/$30; Terra $2/$12; Luna $0.20/$1.20; GPT-5.3-Codex $1.75/$14; fine-tuning
platform winding down) · xAI docs (Grok 4.6 $2/$6, 2× ≥200k) · Alibaba Model Studio Intl
(qwen3.8-max $2/$6; qwen3.5-plus $0.40/$2.40) · DeepSeek (V4-Flash off-peak $0.22/$0.66) ·
Together/Fireworks fine-tuning ($0.48–0.50/M LoRA ≤16B) · enterprise-discount primaries as
cited in §5. Full URL list ships in the audit evidence pack.

## Appendix B — evidence trail

Every number above traces to a file: `mlx_lm` run logs (Trained Tokens counters, loss
series), `BUILD-REPORT.json` dataset waterfalls, exam scorecards
(`exam-v1-step900` → `exam-4b-full-merged`), the canary pack (`CANARY-2026-08-28`), the
teacher bake-off directory, the recorder's append-only pair file, and the session-byte
census scripts. The audit ran read-only while v3 trained live on the same GPU.
