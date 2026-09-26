---
stardate: 2026.268
title: Marker chains, when the kept copy gets retired
date: 2026-09-25
time: "14:58"
status: running
summary: We found 527 duplicate markers that had quietly switched off because the copy they pointed to was later retired. The fix is installed, the repair has run, and the first protected nightly run is next.
---

## What we did

When our memory holds two copies of the same fact, the nightly dream keeps one and retires the other with a retirement marker, a flag that hides a note from recall without deleting it. Each marker points to its kept copy, the one that stays visible.

On 2026-09-25 we asked a narrow question: do those markers keep working over time? A marker hides its duplicate only while its kept copy is live and unmarked. So we ran a census, a read-only count of every marker and the state of its kept copy, on the main studio.

## What we learned

**Markers were quietly switching off.** At 13:44 the census found 7,222 marked notes. 527 exact-duplicate markers were inactive, because their kept copy had itself been retired later. Nothing was lost, but each of those duplicates was visible to recall again.

- 503 were exact-on-exact chains, built up since 09-06 at about 26 a night: a later exact-duplicate pass retired an earlier marker's kept copy.
- 24 came from 4 kept copies that the nightly's near-duplicate step retired.
- None came from deep dream. Its chain rule, which keeps any note another marker points to, held.

**The cause.** The nightly's exact-duplicate step picked the winner of each group of identical copies by source quality, then by id. A new identical copy with a smaller id won, and the old kept copy was retired. The retention planner, which proposes what to retire, also skips any note that carries a marker, even an inactive one. So no later run ever fixed a chain.

What surprised us: nothing reported it. A marker written by any tool can be undone, silently, by a later run that retires its kept copy.

**The fix.** Three changes:

- the planner takes a protected list, so a kept copy is never proposed for retirement and always wins its group;
- the nightly scans every marker's kept copy before it plans, which took 0.8 s live, and guards its near-duplicate proposals too;
- restore can target chosen markers only.

A repair restores only the chained markers in the nightly's journal, and the next protected nightly marks them again.

**Where it stands.** An independent review returned accept, with 0 blocking findings. The fix is installed with a one-command undo. The repair restored the 527 markers with 0 other changes to the stored notes, and the census then showed 0 inactive markers.

## Open questions

- The first protected nightly run, set for 09-26 03:30, will re-mark up to 500 of the restored duplicates. Will it do so cleanly?
- Any new tool that writes retention markers must protect the kept copies of all existing markers. How do we make sure every future writer does?
- Dream health should be measured by the count of inactive markers, not just markers applied. What count should raise an alarm?

## Lab book log

- `2026-09-06` Start of the count: 503 exact-on-exact chains built up from here, about 26 a night.
- `13:44` Census on the main studio: 7,222 marked notes, 527 inactive duplicate markers.
- `cause` Traced to the nightly's exact step letting a newer identical copy win its group.
- `fix` Planner protects every kept copy; the nightly scans them before each plan.
- `review` Independent review: accept, with 0 blocking findings.
- `install` Fix installed with a one-command undo.
- `repair` 527 markers restored with 0 other changes; the census then showed 0 inactive.
- `14:58` State recorded; the first protected nightly is set for 09-26 03:30.
