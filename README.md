# Comm-Log Send Reconciliation

Take-home submission: reproducing Finance's `target_base = 22` for merchant 501, October 2026,
Diwali campaigns, from raw `comm_log` data — plus independent cross-validation and edge-case
tests for the logic beyond just this one dataset.

## Contents

- [`RECONCILIATION.md`](./RECONCILIATION.md) — the reconciliation bridge (naive count → final
  number), the investigation writeup in the order things were discovered, and the
  cross-validation / edge-case sections.
- [`query.sql`](./query.sql) — the final SQL query, parameterized (merchant/period/name filter
  live in one `params` CTE) and cycle-guarded against corrupted `parent_id` chains.
- [`validate.py`](./validate.py) — an independent, structurally different implementation
  (iterative graph traversal in pandas, no SQL) that recomputes `target_base` and diffs it
  against the SQL result. Two implementations agreeing is much stronger evidence than either
  passing alone.
- [`tests/test_reconciliation.py`](./tests/test_reconciliation.py) — regression tests against
  the real dataset, plus adversarial tests against synthetic data for cases the sample data
  doesn't happen to cover (deep chains, `parent_id` cycles, an ineligible chain root with an
  eligible retry).
- [`data/`](./data) — the raw dataset provided for the exercise (`comm_log.db`, CSVs, and the
  data dictionary).
- [`generate_dataset.py`](./generate_dataset.py) — the script that generated the synthetic
  dataset (provided as part of the exercise, included here for reference/reproducibility).

## Running it

```bash
pip install -r requirements.txt

make query      # sqlite3 data/comm_log.db < query.sql  ->  22
make validate   # cross-check SQL against the independent pandas implementation
make test       # pytest tests/ -v  (9 tests)
make all        # all three
```

## Result summary

| Step | Description | Result |
|---|---|---|
| 0 | Naive `COUNT(*)` | 30 |
| 1 | Exclude non-finalized campaign (`approval_awaiting`) | 26 |
| 2 | Dedupe distinct customers within retry chain 1 | 23 |
| 3 | Dedupe distinct customers within retry chain 2 | 22 |
| final | Confirm standalone campaign is exempt from dedup | **22** |

Full reasoning for each step, the cross-validation output, and the edge-case discussion are in
[`RECONCILIATION.md`](./RECONCILIATION.md).
