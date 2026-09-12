# Comm-Log Send Reconciliation — merchant 501, October 2026, Diwali campaigns

**Target:** reproduce Finance's reported `target_base = 22` from raw data and explain any gap
against a naive query.

**Final answer: 22.** SQL is in [`query.sql`](./query.sql), runnable directly against
`data/comm_log.db`.

## Investigation, in the order I actually found things

**Started naive.** `SELECT COUNT(*)` over `communication_log`, joined to `campaign`, filtered to
merchant 501, campaigns named like `%Diwali%`, sent in October 2026. That's "every send attempt
logged," with no business rules applied yet. Result: **30**.

**First thing that looked wrong.** The data dictionary says a campaign only counts toward
reporting once its creation workflow has cleared approval *and* processing is done — and calls
out explicitly that the send pipeline can run ahead of approval bookkeeping. Checking the
`campaign` table, campaign `9004` ("Retry C (pending)") is `creation_status = 'approval_awaiting'`
even though `processing_status = 'processed'` and it already has 4 log rows (customers C11–C14,
all `delivery_status = 900`, i.e. delivered-looking). Those customers don't appear under any
other campaign, so excluding `9004` removes all 4 rows outright, not just re-attributes them.
→ **26**.

**Second thing that looked wrong.** In the remaining data, some customers appear more than once
against *different but related* campaign ids. Specifically:
- `C2`: sent under `9001` (failed, 1100), then under `9002` (delivered, 900)
- `C3`: sent under `9001` (failed), `9002` (failed), then `9003` (delivered)
- `D1`: sent under `9201` (failed), then `9202` (delivered)

`9002`/`9003` have `parent_id` pointing back at `9001`, and `9202`'s `parent_id` points at `9201`
— these are retry chains per the data dictionary ("the same underlying communication,
re-attempted"), not independent communications. A customer who eventually got delivered after
2–3 attempts within one chain should count once, not once per attempt. Collapsing to distinct
customers *within each chain* removes 3 rows from the `9001→9002→9003` chain and 1 row from the
`9201→9202` chain. → **22**.

**Check I ran to avoid over-correcting.** Campaign `9101` has `parent_id = NULL` and nothing else
in the `campaign` table points at it — it's not part of any retry chain, it's a standalone
campaign. It has customer `C20` appearing twice, 10 days apart, both delivered. The data
dictionary explicitly distinguishes this case: a standalone campaign's repeat sends to the same
customer are independent events, not a retry, and should **not** be deduped. I verified my query
doesn't collapse this — `9101` contributes all 7 of its rows (2 for C20 + 5 singles), not 6. This
step didn't move the final number, but it's the check that confirms the retry-chain logic isn't
being applied too broadly.

## Reconciliation bridge

| Step | Description | Result | Reason |
|---|---|---|---|
| 0 | Naive `COUNT(*)` of all `communication_log` rows for merchant 501, Oct 2026, Diwali campaigns | 30 | Starting point — treats every send attempt as a qualifying send |
| 1 | Exclude campaign `9004` (`creation_status = 'approval_awaiting'`) | 26 | Per the data dictionary, a campaign only counts once its creation workflow is finalized; `9004`'s send pipeline ran ahead of approval, so its 4 rows (C11–C14) don't count at all |
| 2 | Within retry chain `9001→9002→9003`, dedupe to distinct customers | 23 | A retry chain is one underlying communication; C2 and C3 each had multiple attempts and should count once, not once per attempt (removes 3 rows) |
| 3 | Within retry chain `9201→9202`, dedupe to distinct customers | 22 | Same logic as step 2 applied to the second chain — D1's two attempts collapse to one (removes 1 row) |
| final | Confirm standalone campaign `9101` is **not** deduped — C20's two independent sends both count | **22** | Standalone campaigns are explicitly exempted from the retry-dedup rule; this check changed nothing but confirms the logic wasn't over-applied |

## Per-chain result (from the final query)

| root campaign | campaigns in chain | qualifying sends |
|---|---|---|
| 9001 (`9001→9002→9003`) | 3 | 10 |
| 9101 (standalone) | 1 | 7 |
| 9201 (`9201→9202`) | 2 | 5 |
| **Total** | | **22** |

## Validating the answer two different ways

A bridge that lands on the right number can still be wrong for the wrong reasons — so I
implemented the logic twice, independently: once as the recursive SQL in
[`query.sql`](./query.sql), and once as iterative graph traversal in pandas in
[`validate.py`](./validate.py). They share no code. Running `python3 validate.py` executes
both and diffs the results:

```
$ python3 validate.py
[validate] excluding 1 non-finalized campaign(s): [9004] (creation_status=['approval_awaiting'])

Per-chain breakdown (pandas, independent implementation):
 root_id                      root_name  n_campaigns  qualifying_sends
    9001  Diwali Cart Recovery - Wave 1            3                10
    9101 Diwali Flash Sale - Standalone            1                 7
    9201                  Diwali Wave 2            2                 5

[validate] pandas implementation -> target_base = 22
[validate] SQL implementation      -> target_base = 22

 Both independent implementations agree: target_base = 22
```

## Edge cases the sample data doesn't exercise (but production data eventually will)

The retry-chain model (self-referencing `parent_id`) implies a few failure modes that
happen not to show up in this dataset. I wrote `tests/test_reconciliation.py` against small
synthetic datasets to pin these down rather than leaving them as blind spots:

- **Chains deeper than 2–3 levels.** A→B→C→D with the same customer retried at every level
  should still count once. Verified with a 4-level synthetic chain.
- **A corrupted `parent_id` cycle** (A's parent is B, B's parent is A). Both the SQL recursive
  CTE and the Python graph walk need to detect this and stop, not hang. I added a path-based
  cycle guard to the CTE (`WHERE r.path NOT LIKE '%/' || e.id || '/%'`) and an explicit
  hop-count guard in `find_root()` in Python, and tested both against a real cyclic dataset.
- **A chain whose root is ineligible but a retry off it is eligible** (e.g. root still
  `approval_awaiting`, but its retry is `approved`). The README doesn't resolve what should
  happen here — my implementation currently drops the retry too, since it has no path to an
  eligible root to roll up into. I didn't invent an answer to this; I wrote a test that pins
  down and documents the current behavior as an explicit assumption, so it's a visible design
  decision rather than a silent gap if it ever comes up.

Run `pytest tests/ -v` (or `make test`) to see all of this exercised — 9 tests, including the
two regression checks against the real dataset and the three adversarial cases above.

## What surprised me

The `approval_awaiting` campaign (`9004`) having fully-formed, delivered-looking log rows was
the biggest surprise — it's a reminder that "a row exists in the log table" and "eligible for
reporting" are genuinely different conditions, and a naive join would silently include sends
that were never actually signed off. The retry-vs-standalone distinction turned out to be
cleanly recoverable from campaign connectivity alone (`parent_id` in/out-degree) — no separate
flag was needed. The one thing that didn't change my final number but could easily trip someone
up: `C20` in the standalone campaign is a legitimate exact duplicate (same campaign, same
customer, twice) — it's tempting to reflexively dedupe any repeated `customer_id`, but doing so
here would have under-counted by 1 and been wrong per the spec.
