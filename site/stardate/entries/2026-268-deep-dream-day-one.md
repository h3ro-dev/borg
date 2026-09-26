---
stardate: 2026.268
title: Deep dream, day one
date: 2026-09-25
time: "20:03"
status: finished
summary: Our first full clean-up pass over the main studio's memory retired 3,618 of 117,506 live memories, 3.1%, all reversibly. It finished at 20:03 after the near-duplicate wave resumed on a raised budget, and recall never returned a retired copy.
---

## What we did

Deep dream is the on-demand, full-pass version of our nightly memory clean-up. It looks for duplicates and useless notes, such as tool echoes ("Wall time 1.2 seconds") and old snapshots of a machine's state. Our question: how much can one pass retire, with every change undoable?

Nothing is deleted. A note is retired with a retirement marker, a flag that hides it from recall without deleting it. One command restores a whole wave.

Three independent code reviews came first, and every required fix was applied. A rehearsal on a copy of the memory applied and undid every stage, byte for byte. The live plan, made at 06:22, covered 117,506 live memories in four waves. In every wave except exact duplicates, JEV, our small judge model, must clear each retirement.

## What we learned

The pass retired 3,618 memories, 3.1% of the plan. By 09:36 it had retired 3,133; the near-duplicate wave paused at the day's JEV budget, resumed at 16:09 and finished at 20:03.

| Wave | Candidates | Retired | Why the rest stayed |
|---|---|---|---|
| Machine-state snapshots | 13 | 9 | 3 kept by JEV; 1 not yet 7 days old |
| Tool echoes | 498 | 371 | 90 kept by JEV; 3 held back for privacy; 34 pointed at by another marker |
| Exact duplicates | 535 extra copies | 11 | 4 protected decisions; the rest refused by the planner's compatibility rules |
| Near-duplicates | 9,642 pairs | 3,227 | 9,297 pairs judged; 228 contradictions logged, not merged; 109 held by the chain rule |

For near-duplicates, JEV must say "same fact" or "supersedes" with at least 90% confidence; then the shorter or older copy is retired. The chain rule keeps any note that another marker points to.

**The checks held.** Recall, sampled 40 times after each wave, never returned a retired copy, and found the kept fact about 90% of the time. At the finish we sampled it 300 times: it returned no retired copy, every kept copy was still live, and the control search found 298 of 300. Of the 19,284 near-duplicate candidates, 3,227 changed only in their marker and labels, and none changed in any other way. A live undo test on the snapshot wave restored 9, and all 13 notes matched the before-snapshot byte for byte. Spot checks of merges down to a similarity of 0.92 found only true rewordings.

**It was cheap.** JEV calls from 07:05 to the finish cost $0.281, for 9,584 calls: $0.176 for 6,173 calls by the morning, and $0.098 for 3,411 calls in the evening. Deep dream first stopped at $0.23 of a rolling 24-hour budget of $0.25 that it shares with the nightly. We raised that budget to $1.00, with an undo, and resumed the remaining pairs at 16:09 instead of waiting for the next morning.

What surprised us: exact duplicates, which sound easy, retired only 11 of 535 extra copies. And most near-duplicate pairs stayed: of 9,297 judged, JEV cleared 3,336 for retirement. On many of the rest it was at least 90% sure the notes were redundant, but split its answer between "same fact" and "newer replaces older". Neither answer reached 90%, so both copies stayed.

## Open questions

- Which copy should we keep when JEV splits its answer? One proposal: the newer copy when it holds everything the older says, otherwise the longer.
- 385 exact-duplicate groups hold the same text saved as different kinds of note. Nothing merges them yet.
- The lane re-checks its permission with about 6 Inbox hub calls per pair. When the hub was slow, 17 of those checks failed safely and both copies stayed. Can it check less often, safely?
- Late in the evening the hub was busy enough that recall's own permission check started missing its deadline too. Should lanes share one short-lived proof of permission instead of each asking the hub?

## Lab book log

- `review` Three independent code reviews, then a rehearsal on a copy; every stage undone byte for byte.
- `06:22` Live plan made over 117,506 live memories.
- `07:05` Cost window opens.
- `09:36` 3,133 retired, 2.7% of the plan; near-duplicates paused at the day's JEV budget after 6,173 calls ($0.176).
- `16:09` Rolling budget raised to $1.00, with an undo; the remaining near-duplicate pairs resumed.
- `18:47` A slow hub stopped the wave safely; it resumed 20 seconds later.
- `20:03` Finished: every remaining pair judged; 3,618 retired in total, 3.1% of the plan; 9,584 JEV calls ($0.281).
- `20:05` Final checks: 300 recall samples returned no retired copy; no memory changed except its marker and labels.
