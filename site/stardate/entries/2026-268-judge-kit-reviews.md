---
stardate: 2026.268
title: The judge kit and its two independent reviews
date: 2026-09-25
status: finished
summary: We added seven quality items around our agents. Two independent code reviews found real defects, and every high and medium finding is now fixed.
---

## What we did

The judge kit is our set of quality checks around the agents. We added seven items, each with a canary (a small automatic check that runs all the time) in our regression harness.

| Item | What it does | First result |
|---|---|---|
| Outcome grading | Hourly, asks whether each recalled memory was used | 0.8% of 4,451 clearly used |
| Claim checks | Hourly, asks whether a "done" claim has evidence | about 5% of claim turns flagged |
| Pre-send guard | Checks recipients and holds before a client message; only orchestration-tier models may send | tests 20 of 20 |
| Brief checks | Blocks a task brief with no acceptance check, artifact or stop condition | 45% of real briefs lacked acceptance checks |
| Hook triage | Measures the text our hooks add to prompts | about 18.7 KB per prompt |
| Drift alarms | Every 15 minutes, compares each lane with its last 24 hours | in a backtest, caught the 09-24 incidents within the hour |
| Routing scoreboard | Hourly, observed success per model, account and machine | one model, 90% on one studio and 65% on another |

Six items had passed their own tests, canaries and live samples, but no second reader had seen the code. Our question: would independent reviewers find what our own tests missed?

## What we learned

**The first review found real defects.** A reviewer on another studio read all the code, ran every suite and wrote 12 probe scripts.

- **Five high-severity and five medium defects:** three in the pre-send guard, two in the drift alarms, and partial grading in the hourly checks.
- The kind of thing it caught: a lane failing every call never raised an alarm, and turns still in progress were graded on a partial reply and never graded again.

All 5 high and 5 medium findings were fixed, and 15 of the 16 low. We also made subagents (helpers started by another agent) draft-only for client messages.

**The second review, of the fixes, found three more:** one high and two medium, in the pre-send guard and the brief check. All are fixed, along with 11 low findings.

What surprised us: code that had passed its own tests, canaries and live samples still hid high-severity gaps.

## Open questions

- Only 0.8% of injected memories are clearly used. Is recall picking poorly, or is the grader strict?
- A tested patch for the Inbox check-in saved 4.7 MB a day (38%) in a replay, and it is now live on the main studio. Will it hold up across the fleet?

## Lab book log

- `build` Seven items built, each with a canary.
- `review-one` First review of six items: 5 high, 5 medium, 16 low.
- `fixes` All high and medium fixed, 15 of 16 low; probes rerun clean.
- `review-two` Second review, of the fixes: one high, two medium, 11 low.
- `refix` Second-review findings fixed; three low items accepted as they are; 100 tests pass; four superseded grants revoked.
- `2026-09-25` Report written: all seven items live, regression 12 of 12.
