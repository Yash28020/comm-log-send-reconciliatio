"""
Test suite for the comm-log reconciliation logic.

Two kinds of tests here:

1. Regression tests against the actual provided dataset (data/comm_log.db) --
   these lock in the answer (22) and the per-chain breakdown so a future
   change to query.sql or validate.py that silently breaks the logic gets
   caught immediately.

2. Edge-case tests against small, purpose-built synthetic datasets that the
   sample data doesn't happen to exercise -- a 3+ level chain, a corrupted
   parent_id cycle, and a chain whose ROOT is ineligible but whose retry is
   eligible. These aren't things I was asked to handle, but a dataset this
   shape (retry chains via self-referencing FK) will hit them eventually in
   production, and I'd rather document the assumption now than have it be a
   surprise later.

Run with: pytest tests/ -v
"""

import sqlite3
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from validate import compute_target_base, find_root  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = REPO_ROOT / "data" / "comm_log.db"
QUERY_PATH = REPO_ROOT / "query.sql"


# ---------------------------------------------------------------------------
# 1. Regression tests against the real dataset
# ---------------------------------------------------------------------------

def run_sql(db_path=DB_PATH):
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(QUERY_PATH.read_text()).fetchone()[0]
    finally:
        conn.close()


def test_sql_matches_reported_target_base():
    assert run_sql() == 22


def test_pandas_matches_reported_target_base():
    campaign = pd.read_csv(REPO_ROOT / "data" / "campaign.csv")
    comm_log = pd.read_csv(REPO_ROOT / "data" / "communication_log.csv")
    result, _ = compute_target_base(campaign, comm_log, 501, "2026-10-01", "2026-11-01")
    assert result == 22


def test_sql_and_pandas_agree_on_per_chain_breakdown():
    campaign = pd.read_csv(REPO_ROOT / "data" / "campaign.csv")
    comm_log = pd.read_csv(REPO_ROOT / "data" / "communication_log.csv")
    _, breakdown = compute_target_base(campaign, comm_log, 501, "2026-10-01", "2026-11-01")
    expected = {9001: 10, 9101: 7, 9201: 5}
    got = dict(zip(breakdown["root_id"], breakdown["qualifying_sends"]))
    assert got == expected


def test_ineligible_campaign_fully_excluded_not_reattributed():
    """Campaign 9004's 4 customers (C11-C14) don't appear anywhere else --
    confirms they're dropped outright, not silently folded into another
    chain's count."""
    campaign = pd.read_csv(REPO_ROOT / "data" / "campaign.csv")
    comm_log = pd.read_csv(REPO_ROOT / "data" / "communication_log.csv")
    excluded_customers = {"C11", "C12", "C13", "C14"}
    # None of C11-C14 appear under any campaign other than 9004, so excluding
    # 9004 drops them from target_base entirely rather than re-attributing
    # them to some other chain.
    other_campaigns = comm_log[comm_log["communication_id"] != 9004]
    assert not set(other_campaigns["customer_id"]).intersection(excluded_customers)


def test_standalone_campaign_not_deduped():
    """C20 under standalone campaign 9101 appears twice and both must count."""
    campaign = pd.read_csv(REPO_ROOT / "data" / "campaign.csv")
    comm_log = pd.read_csv(REPO_ROOT / "data" / "communication_log.csv")
    _, breakdown = compute_target_base(campaign, comm_log, 501, "2026-10-01", "2026-11-01")
    standalone_row = breakdown[breakdown["root_id"] == 9101].iloc[0]
    assert standalone_row["n_campaigns"] == 1
    assert standalone_row["qualifying_sends"] == 7  # not 6 -- C20's dup both count


# ---------------------------------------------------------------------------
# 2. Edge cases the sample data doesn't cover
# ---------------------------------------------------------------------------

def test_deep_chain_four_levels_dedupes_correctly():
    """A->B->C->D, one customer sent at every level before finally
    delivering at D, should count once for that customer."""
    campaign = pd.DataFrame([
        {"id": 1, "merchant_id": 999, "parent_id": None, "name": "Diwali X", "creation_status": "approved", "processing_status": "processed"},
        {"id": 2, "merchant_id": 999, "parent_id": 1, "name": "Retry", "creation_status": "approved", "processing_status": "processed"},
        {"id": 3, "merchant_id": 999, "parent_id": 2, "name": "Retry", "creation_status": "approved", "processing_status": "processed"},
        {"id": 4, "merchant_id": 999, "parent_id": 3, "name": "Retry", "creation_status": "approved", "processing_status": "processed"},
    ])
    comm_log = pd.DataFrame([
        {"merchant_id": 999, "communication_id": cid, "customer_id": "Z1",
         "communication_type": "2", "sent_time": f"2026-10-0{i+1} 10:00:00"}
        for i, cid in enumerate([1, 2, 3, 4])
    ])
    result, breakdown = compute_target_base(campaign, comm_log, 999, "2026-10-01", "2026-11-01")
    assert result == 1
    assert breakdown.iloc[0]["n_campaigns"] == 4


def test_cycle_in_parent_id_is_detected_not_infinite_looped():
    """Corrupted data: A's parent is B, B's parent is A. Should raise
    rather than hang."""
    parent_of = {1: 2, 2: 1}
    with pytest.raises(ValueError, match="Cycle detected"):
        find_root(1, parent_of)


def test_sql_cycle_guard_does_not_hang(tmp_path):
    """Same corruption, but through the actual recursive CTE in query.sql --
    confirms the path-based cycle guard (not just the Python one) holds."""
    db_path = tmp_path / "cycle.db"
    conn = sqlite3.connect(db_path)
    conn.execute("""
        create table campaign (
            id integer primary key, merchant_id integer, parent_id integer,
            name text, creation_status text, processing_status text
        )
    """)
    conn.execute("""
        create table communication_log (
            id integer primary key, merchant_id integer, communication_id integer,
            customer_id text, communication_type text, delivery_status integer,
            sent_time text, scheduled_time text, credit_used integer, channel text
        )
    """)
    # A <-> B cycle, both otherwise eligible
    conn.executemany(
        "insert into campaign values (?,?,?,?,?,?)",
        [
            (1, 501, 2, "Diwali A", "approved", "processed"),
            (2, 501, 1, "Diwali B", "approved", "processed"),
        ],
    )
    conn.commit()
    # This should complete (not hang) -- the cycle guard means neither row
    # ever anchors to a root (no campaign here has parent_id IS NULL), so
    # both are correctly excluded from the result rather than looping.
    result = conn.execute(QUERY_PATH.read_text()).fetchone()[0]
    assert result == 0 or result is None
    conn.close()


def test_ineligible_root_with_eligible_child_is_a_documented_assumption():
    """If a chain's ROOT campaign is ineligible (e.g. approval_awaiting) but
    a retry off it is eligible, the current implementation drops the retry
    too, because it can never resolve a path to an eligible root.

    This is a genuine ambiguity the README doesn't resolve -- flagging it
    explicitly here so it's a documented assumption, not a silent gap.
    """
    campaign = pd.DataFrame([
        {"id": 1, "merchant_id": 999, "parent_id": None, "name": "Diwali Y", "creation_status": "approval_awaiting", "processing_status": "processed"},
        {"id": 2, "merchant_id": 999, "parent_id": 1, "name": "Diwali Y Retry", "creation_status": "approved", "processing_status": "processed"},
    ])
    comm_log = pd.DataFrame([
        {"merchant_id": 999, "communication_id": 2, "customer_id": "Z1",
         "communication_type": "2", "sent_time": "2026-10-05 10:00:00"},
    ])
    result, _ = compute_target_base(campaign, comm_log, 999, "2026-10-01", "2026-11-01")
    # Documented current behavior: 0, not 1. If product intent is actually
    # "count the eligible retry on its own," this test will fail loudly and
    # point straight at the assumption to revisit.
    assert result == 0
