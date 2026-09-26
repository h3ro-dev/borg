---
stardate: 2026.268
title: Deep dream, day one
date: 2026-09-25
time: "09:36"
status: running
summary: Our first full clean-up pass over the main studio's memory retired 3,133 of 117,506 live memories by 09:36, all reversibly. The near-duplicate wave paused at the day's JEV budget, and we resumed it the same afternoon.
---

## What we did

Deep dream is the on-demand, full-pass version of our nightly memory clean-up. It looks for duplicates and useless notes, such as tool echoes ("Wall time 1.2 seconds") and old snapshots of a machine's state. Our question: how much can one pass retire, with every change undoable?

Nothing is deleted. A note is retired with a retirement marker, a flag that hides it from recall without deleting it. One command restores a whole wave.

Three independent code reviews came first, and every required fix was applied. A rehearsal on a copy of the memory applied and undid every stage, byte for byte. The live plan, made at 06:22, covered 117,506 live memories in four waves. In every wave except exact duplicates, JEV, our small judge model, must clear each retirement.

## What we learned

By 09:36, 3,133 memories were retired, 2.7% of the plan.

| Wave | Candidates | Retired | Why the rest stayed |
|---|---|---|---|
| Machine-state snapshots | 13 | 9 | 3 kept by JEV; 1 not yet 7 days old |
| Tool echoes | 498 | 371 | 90 kept by JEV; 3 held back for privacy; 34 pointed at by another marker |
| Exact duplicates | 535 extra copies | 11 | 4 protected decisions; the rest refused by the planner's compatibility rules |
| Near-duplicates | 9,642 pairs | 2,742 | 6,010 pairs judged so far; 142 contradictions logged, not merged; 103 held by the chain rule |

For near-duplicates, JEV must say "same fact" or "supersedes" with at least 90% confidence; then the shorter or older copy is retired. The chain rule keeps any note that another marker points to.

**The checks held.** Recall, sampled 40 times after each wave, never returned a retired copy, and found the kept fact about 90% of the time. Every changed memory changed only in its marker and labels. A live undo test on the snapshot wave restored 9, and all 13 notes matched the before-snapshot byte for byte. Spot checks of merges down to a similarity of 0.92 found only true rewordings.

**It was cheap.** JEV calls since 07:05 cost $0.176, for 6,173 calls. Deep dream was set to stop at $0.23 of a rolling 24-hour budget of $0.25 that it shares with the nightly. The near-duplicate wave stopped at the day's JEV budget. The remaining 3,632 pairs were first set for the next morning; instead we raised the rolling budget to $1.00, with an undo, and resumed them at 16:09.

What surprised us: exact duplicates, which sound easy, retired only 11 of 535 extra copies. And on most near-duplicate pairs JEV was at least 90% sure the notes were redundant, but split its answer between "same fact" and "newer replaces older". Neither answer reached 90%, so both copies stayed.

## Open questions

- Which copy should we keep when JEV splits its answer? One proposal: the newer copy when it holds everything the older says, otherwise the longer.
- 385 exact-duplicate groups hold the same text saved as different kinds of note. Nothing merges them yet.
- The lane re-checks its permission with about 6 Inbox hub calls per pair, which caused 9 safe stops when the hub was slow. Can it check less often, safely?

## Lab book log

- `review` Three independent code reviews, then a rehearsal on a copy; every stage undone byte for byte.
- `06:22` Live plan made over 117,506 live memories.
- `07:05` Cost window opens; $0.176 over 6,173 JEV calls from here.
- `09:36` 3,133 retired, 2.7% of the plan; near-duplicates paused at the day's JEV budget.
- `16:09` Rolling budget raised to $1.00, with an undo; the remaining near-duplicate pairs resumed.
