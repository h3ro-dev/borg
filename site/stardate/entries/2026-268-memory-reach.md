---
stardate: 2026.268
title: Memory rarely reaches the agents that need it
date: 2026-09-25
time: "01:00"
status: finished
summary: On lane hosts, recall delivered something on only 5 to 10% of prompts, and almost nothing for Grok. The facts were in memory, but queries built from the raw prompt missed them.
---

## What we did

Our agents share one memory. Recall is the step that hands an agent the useful memories before it answers. Before judging whether memory helps, we asked a plainer question: does the right memory reach the agent at all?

At about 01:00 on 2026-09-25 we measured three things:

- **Lane hosts**, the studios that run our agents' delegated jobs. From the recall hook's log since 09-18 (a hook is a small program that runs at set points in an agent's loop), we counted how many prompts got any memory.
- **The main studio**, where Claude gets recall on almost every prompt, reranked by JEV, our small judge model.
- **Retrieval.** For six fleet-knowledge questions, did recall carry the answering fact, and could a focused search find it?

## What we learned

**On lane hosts, recall rarely delivered anything.** Across all runtimes it delivered something on about 5 to 10% of prompts.

| Lane host | Claude prompts with recall | Codex prompts with recall |
|---|---|---|
| one studio | 63 of 1,205 | 63 of 641 |
| another | 32 of 401 | not listed |
| a third | 9 of 90 | 20 of 85 |
| a fourth | 0 of 23 | not listed |
| a fifth | 0 of 15 | not listed |

Grok got almost nothing: its recall step before tool calls logged 730 skips on one lane host. The cause is a word-matching and project gate. It keeps a memory only if the prompt shares its words, and drops memories filed under a different project, which is just the name of the agent's working folder.

**On the main studio, recall arrives but is rarely used.** It injects 3 to 5 memories on almost every prompt. Our outcome grader found only about 0.8% clearly used.

**The facts are there; the queries miss them.** Recall on the main studio carried the answering fact for 1 of the 6 knowledge questions, or 2 of 6 with file content added to the query. A focused memory search found the same facts at the top. The fact that one studio takes no new agent work, for example, scored 0.83 to 0.86.

What surprised us is why. Prompts often name the key thing only inside a file, and recall at prompt time never sees it. So when memory looks unhelpful, the problem is mostly delivery and query building, not missing knowledge. Our rule now: before judging memory's value in any test, check that recall delivered the relevant fact.

## Open questions

- Would a search tool that agents call themselves fix delivery on lane hosts? Our Grok A/B test points that way.
- Would running recall again after the agent reads its files close the gap?
- Six questions is a small test. How often does recall carry the answer on a larger set?

## Lab book log

- `2026-09-18` Start of the lane-host recall log used for this count.
- `01:00` Counted delivery per lane host and runtime: about 5 to 10% of prompts got anything.
- `grok-check` Grok's recall before tool calls logged 730 skips on one lane host.
- `main-recall` The main studio's recall injects 3 to 5 memories on almost every prompt; about 0.8% clearly used.
- `retrieval-test` Recall carried the answer for 1 of 6 questions, or 2 with file content added.
- `search-test` A focused search found the same facts at the top, scoring 0.83 to 0.86.
