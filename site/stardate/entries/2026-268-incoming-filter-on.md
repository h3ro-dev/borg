---
stardate: 2026.268
title: The incoming filter, from shadow to on
date: 2026-09-25
time: "14:07"
status: finished
summary: After 24 hours in shadow mode we read all 116 would-be drops by hand and found no durable facts. We then switched the filter on, and its first real drop was written to an owner-only recovery copy before being dropped.
---

## What we did

Agents write facts to our shared memory as they work. Most new rows, 80 to 98%, arrive through one capture path, and some of it is noise: echoes of tool output and scraps of telemetry. We wanted JEV, our small judge model, to stop that noise before it is stored, without losing a real fact.

So the filter started in shadow mode: it judges every fact but drops nothing. After an independent review we set the rules for leaving shadow:

- at least 24 hours of shadow after a credential fix, with credential errors near zero;
- at least 100 judged candidates;
- every would-be drop read by hand, with zero durable facts among them;
- drops counted under the live filter's caps;
- latency measured over at least 20 captures.

Once on, the filter may drop at most half of any one capture, and at most 8 facts an hour across the whole service. It may only drop two kinds of noise: telemetry fragments and echoes of commands that ran.

## What we learned

**The shadow day.** In 24 hours the filter saw 418 captures and 2,498 candidate facts, and made 1,894 JEV calls. Credential errors were 0.4%.

**The would-be drops.** With both caps applied, it would have dropped 83 facts; the caps spared 33 more. We read all 116 by hand. Every one was an echo of test, CI (automatic build-and-test runs), build or script output, plus one fragment of system time. None was a durable fact.

**Latency.** 95% of captures finished within 45.0 s, against 41.3 s before: 3.7 s more.

What surprised us was how uniform the list was: nothing but output echoes and one scrap of system time.

**Going on.** At 13:00 on 2026-09-25 we switched the filter on, and the service restarted in about 4 seconds. At 14:07 came the first real drop, a script error echo. It was written first to an owner-only recovery copy. We verified that the fact was absent from what was stored, and that the receipt links to the copy by hash.

**Safety nets.** If a recovery copy cannot be written and read back, nothing is dropped. Copies are kept for 30 days and purged only by hand, after review. A dropped fact can be restored by storing it again, word for word, in the same scope. The fast rollback is a switch in the service settings and a restart; the full one restores the exact previous files.

## Open questions

- Facts that match the capture hook's own drop patterns are kept unjudged, by design, so some echoes still get stored until the deep dream catches them. Should the filter judge those too?
- The caps spared 33 would-be drops in shadow. Are they tighter than they need to be?
- Will zero durable facts among the drops hold as the work changes?

## Lab book log

- `09-24 12:55` Credential fix in place; the 24-hour shadow clock counts from here.
- `09-24 12:56` Capture path starts in shadow mode: JEV judges, nothing is dropped.
- `09-24 14:03` Revised build installed after a third review; recovery copies forced to disk.
- `shadow` 24 hours: 418 captures, 2,498 candidate facts, 1,894 JEV calls.
- `review` All 116 would-be drops read by hand; zero durable facts.
- `13:00` Switched to on; the service restarted in about 4 seconds.
- `14:07` First real drop, a script error echo, saved first to a recovery copy and verified.
