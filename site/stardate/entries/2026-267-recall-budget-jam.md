---
stardate: 2026.267
title: The recall budget jam and its fix
date: 2026-09-24
status: finished
summary: Recall fell back to its old behaviour on 1,539 of 1,685 live attempts because its budget ledger was full of phantom charges, while real spend was about $0.0014. We traced the cause and fixed the accounting.
---

## What we did

Recall is the step where JEV, our small judge model, picks which memories go into an agent's context before it answers. Every JEV lane (one job the judge does) has a daily budget. When the budget says no, the lane falls back to its old behaviour. Nothing breaks, but the agent answers without the judge's help.

Each lane keeps a ledger: before each call it books an estimated cost, called a reservation.

On 2026-09-24 we found recall falling back far too often. Why was a lane that spends almost nothing always out of money? We looked at 1,685 live recall attempts, the lane's ledger, and the timing of each step inside one recall.

## What we learned

**The budget was full of phantom charges.** 1,539 of the 1,685 attempts, about 91%, fell back because the budget said no. The ledger showed $0.4989 charged against a $0.50 limit. Real spend was about $0.0014.

The chain:

1. A recall step has 2.0 seconds in total.
2. About 0.4 s went on two Inbox lookups of the lane's grant (its signed permission), and about 0.4 s on fetching its key from the vault.
3. The lane kept 1.0 s in reserve for a retry, and the call itself got only about 0.3 s.
4. Large requests take 0.4 to 0.5 s, so they timed out with an unknown outcome.
5. An unknown outcome was booked at a worst case that grew with request size and question count, then doubled. With about 86 questions in a large recall, each booking was 16 to 750 times the real bill.
6. These bookings, and those for refused calls (HTTP 403), never aged out of what was meant to be a daily window. They piled up until the ledger was full.

What surprised us: the lane was not short of money at all. Its own bookkeeping blocked it. The same pessimistic accounting sits in the shared client used by other JEV lanes, including the incoming filter and nightly dreaming. Any lane with real traffic and occasional timeouts will jam the same way.

**The fix** is a corrected client for the lane. A booking is capped by the request's size in bytes, old bookings age out of a true rolling window, and a refused request (a 4xx error) costs nothing. The lesson we keep: check a lane's ledger before blaming the provider.

A later readout, from our system map on 2026-09-25, shows recall applying on 590 of 591 prompts over six hours. That is a later reading, not proof that this fix alone caused it.

## Open questions

- Other lanes shared the same accounting when we found this. Should they move to the corrected client before real traffic jams them?
- The lookups eat into a 2.0 s step. How much of that time can we win back?
- The later readout covers six hours. Does it hold over days and under heavy load?

## Lab book log

- `2026-09-24` Found 1,539 of 1,685 live recall attempts falling back on budget.
- `timing` Grant lookups took about 0.4 s, the key fetch about 0.4 s; the call got about 0.3 s.
- `root-cause` Unknown outcomes were booked at 16 to 750 times the real bill and never aged out.
- `fix` Corrected client: capped bookings, rolling-window aging, no charge for refused calls.
- `2026-09-25` Later readout from the system map: recall applies on 590 of 591 prompts over six hours.
