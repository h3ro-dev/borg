---
stardate: 2026.268
title: Grok memory hooks, an A/B test
date: 2026-09-25
status: finished
summary: In 96 headless Grok runs, a memory-search tool got the fewest knowledge questions wrong and answered about 3× cheaper and 4× faster. Injecting recall into the prompt made Grok worse.
---

## What we did

How should Grok reach our shared memory? We compared four setups, called arms. (A hook is a small program that runs at set points in an agent's loop and can add text to its prompt.)

- **A**: no memory hooks.
- **R**: recall injected into the prompt, as Claude gets it on the main studio.
- **M**: a read-only memory-search tool.
- **C**: recall plus advice hooks from JEV, our small judge model.

The 12 tasks were six fleet-knowledge questions whose answers live in memory, and six small coding tasks with tests. Each ran twice per arm: 96 headless runs (scripted, nobody at the keyboard), same settings throughout. Each run had a fresh private home folder, and grading happened elsewhere, so no agent saw the answer key.

## What we learned

All 48 coding runs passed in every arm; memory neither helped nor hurt. Knowledge questions told a different story (12 runs per arm, 95% ranges in parentheses):

| Arm | Wrong | Mean cost | Median time |
|---|---|---|---|
| M, search tool | 25% (9 to 53%) | $0.119 | 24 s |
| A, no hooks | 50% (25 to 75%) | $0.386 | 106 s |
| C, recall and advice | 50% (25 to 75%) | $0.390 | 276 s |
| R, recall injected | 75% (47 to 91%) | $0.373 | 113 s |

- **The search tool won on cost and speed**: about 3× cheaper and about 4× faster, clear against every arm (p ≤ 0.04). On accuracy, only its gap to injected recall is clear (p = 0.04); against no hooks it is suggestive, not proven (p = 0.40).
- **Injected recall made Grok worse.** 8 of its 12 knowledge runs hit the 20-turn limit without answering. The recall block sent Grok hunting through the host to check what it said: 216 tool calls on host paths, against 7 for the search tool.
- **Advice hooks gave no clear gain.** Turns took about 14 s instead of about 6 s, causing 4 of the 5 timeouts, perhaps partly because each test run used a fresh state folder.
- **Stale memories mislead.** Both of M's misses on which machines take no new work came from old machine-state notes treated as current. The search tool returns facts without their dates.

What surprised us most: recall meant to help sent Grok checking instead of answering.

**Caveats.** With 12 runs per arm, treat the accuracy order as a strong hint; cost and speed are solid. "No memory" is not knowledge-free: in 10 of 12 no-hook knowledge runs, Grok read the host's own notes.

Two lessons for anyone running agents: an agent without a sandbox can read anything its user account can, and a killed run can leave its tool processes running.

## Open questions

- Would the search tool, recommended but not yet built, help lane-host agents in production?
- Would returning each memory with its date and type stop the stale-note misses?
- Can JEV advice calls get fast enough for Grok lanes? Until then we would keep them off.

## Lab book log

- `setup` Harness built and validated for $3.96: 12 tasks, 4 arms, 2 repeats.
- `runs` 96 headless Grok runs on one lane host; recorded cost $16.30.
- `cleanup` Stopped ten leftover scans that ran up to 55 minutes after killed runs.
- `grading` Graded elsewhere; one task regraded by hand, same rule for every arm.
- `2026-09-25` Report written: $20.26 recorded, about $22 to 25 counting five timed-out runs.
