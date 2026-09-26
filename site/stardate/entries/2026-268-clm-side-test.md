---
stardate: 2026.268
title: The CLM side test, and why we stay on JEV
date: 2026-09-25
status: finished
summary: We tested whether CLM, an 8B model run on one of our own studios, could pick recall context instead of JEV. It could not; it found the needed rows far less often and was fast enough only with a precomputed cache.
---

## What we did

Recall picks which memory rows go into an agent's context before it answers, and today JEV, our small judge model, does that job. Could CLM, an 8B model run locally on an Apple Silicon studio, do it instead, at least as well and fast enough?

We used a frozen holdout of 600 episodes. An episode is one question with a pool of candidate rows; a holdout is a test set kept apart from all tuning. In 300 episodes the needed evidence was present. CLM's input format and a score cutoff were chosen on a separate practice set, and the rule for switching was written before any scoring. CLM ran zero-shot, as it comes, with no training on our data. Its authors say this kind of use needs fine-tuned heads, so a zero-shot result is not the model's ceiling.

## What we learned

**Quality: no.** Routing picks 8 rows from the pool. How often did those 8 hold every row the answer needs, over the 300 evidence-present episodes?

| Router | Full cover | Versus the JEV arm |
|---|---|---|
| JEV arm's routing (cheap word matching) | 230 (76.7%) | baseline |
| Installed current workflow | 192 (64.0%) | −12.7 points |
| CLM, row text as is | 81 (27.0%) | −49.7 points |
| CLM, row prefixed with its file name | 146 (48.7%) | −28.0 points |

Both CLM gaps have uncertainty ranges entirely below zero, the pre-registered signal to stay on JEV.

End to end, CLM's own arm declined every question: on the practice set, no score cutoff beat declining everything. Its 30 most confident answers were 26 critical errors and 4 fully supported.

What surprised us: one CLM variant beat a baseline by 6.3 points of fully supported answers, yet a control using 8 random rows matched it in all three seeds. The gain came from the framework's word-matching repair pass, which fired in 440 of 600 episodes behind CLM against 197 behind the word-matching router.

**Speed: only with a cache.** On a shared GPU that was 92 to 99% busy, encoding the query and every row took 3.0 to 4.0 s at the median, against a recall budget of about 1.6 s. With each row's embedding (numbers that stand for its meaning) cached ahead of time, it took 123 to 125 ms. Cold start was 16 s to ready, and peak memory was 20.7 GB.

**Not a port defect.** Our local build matched the maintainers' reference outputs, with the largest gap under 0.001 in cosine terms.

## Open questions

- The framework has no recording of JEV's own reranking. A shadow run comparing JEV's top picks with a candidate's on live pools would fill that gap.
- The questions name topic terms from the answering row, which favours word matching. Would real, more semantic queries narrow a 28-point gap?
- A fine-tuned head of about 19 million parameters might help, in a separate project that never trains on this holdout.

## Lab book log

- `2026-09-25` The source report records no run dates; this entry was filed on this day.
- `pre-registration` Switch rule written before any calibration or holdout scoring.
- `parity` Local build matched the maintainers' outputs within 0.001 cosine.
- `dev` Format and cutoff chosen; no cutoff beat declining everything.
- `holdout` 600 episodes scored; CLM routing 49.7 and 28.0 points behind.
- `latency` 60 real pools timed on a shared GPU; 3.0 to 4.0 s median uncached.
- `control` Random rows matched the best CLM variant.
- `decision` Stay on JEV.
