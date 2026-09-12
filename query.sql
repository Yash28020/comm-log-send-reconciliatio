-- Comm-Log Reconciliation: target_base
--
-- Computes: "for a given underlying communication (a campaign plus every
-- retry chained off it), how many distinct customers were reached?" --
-- per the data dictionary's definition of Finance's target_base metric.
--
-- Run:
--   sqlite3 data/comm_log.db < query.sql
--
-- For merchant 501 / Oct 2026 / Diwali campaigns, this returns 22.
--
-- Reusability note: the WHERE-clause literals below (merchant id, date
-- range, name filter) are the only merchant/period-specific parts of this
-- query. Everything else -- the eligibility gate, chain rollup, and
-- standalone exception -- is general-purpose and doesn't need touching to
-- run this for a different merchant or month.

WITH RECURSIVE

params AS (                                    -- single place to change scope
  SELECT
    501           AS merchant_id,
    '2026-10-01'  AS period_start,              -- inclusive
    '2026-11-01'  AS period_end,                -- exclusive
    '%Diwali%'    AS name_filter,
    '2'           AS communication_type          -- '2' = Campaign
),

eligible AS (                                  -- Step 1 (bridge): only "finalized" campaigns
                                                -- count toward reporting -- creation_status
                                                -- must have cleared approval, and
                                                -- processing_status must be 'processed'.
                                                -- Drops campaigns still mid-approval even if
                                                -- their send pipeline already produced logs
                                                -- (e.g. 9004 in the sample data).
  SELECT c.id, c.parent_id
  FROM campaign c, params p
  WHERE c.merchant_id = p.merchant_id
    AND c.creation_status IN ('approved', 'aborted', 'resumed', 'stopped')
    AND c.processing_status = 'processed'
),

roots(id, root_id, depth, path) AS (           -- Walk parent_id chains to each eligible
                                                -- campaign's ultimate root ancestor. All
                                                -- campaigns chained together (root + every
                                                -- retry off it) represent one underlying
                                                -- communication. `path` + `depth` guard
                                                -- against a corrupted parent_id cycle causing
                                                -- infinite recursion -- if id -> ... -> id
                                                -- ever recurs, the id already appears in
                                                -- `path` and the branch is cut (see WHERE
                                                -- below, and the assertion in tests/).
  SELECT id, id AS root_id, 0 AS depth, '/' || id || '/' AS path
  FROM eligible
  WHERE parent_id IS NULL

  UNION ALL

  SELECT e.id, r.root_id, r.depth + 1, r.path || e.id || '/'
  FROM eligible e
  JOIN roots r ON e.parent_id = r.id
  WHERE r.path NOT LIKE '%/' || e.id || '/%'    -- cycle guard: don't revisit an id
    AND r.depth < 50                             -- sane hard cap regardless
),

chain_size AS (                                -- A root whose chain contains only itself (no
                                                -- retries point at it, and it points at
                                                -- nothing) is a standalone campaign, not a
                                                -- retry chain -- this flips the counting rule
                                                -- below.
  SELECT root_id, COUNT(*) AS n_campaigns
  FROM roots
  GROUP BY root_id
),

logs AS (                                      -- Qualifying log rows: right merchant, right
                                                -- communication_type, sent within the scoped
                                                -- period, whose CHAIN ROOT's campaign name
                                                -- matches the name filter (a retry named
                                                -- generically, e.g. "Retry A", still belongs
                                                -- to its Diwali-named root communication).
  SELECT cl.customer_id, r.root_id, cs.n_campaigns
  FROM communication_log cl
  JOIN roots r        ON cl.communication_id = r.id
  JOIN chain_size cs  ON cs.root_id = r.root_id
  JOIN campaign c     ON c.id = r.root_id
  JOIN params p
  WHERE cl.merchant_id = p.merchant_id
    AND cl.communication_type = p.communication_type
    AND c.name LIKE p.name_filter
    AND cl.sent_time >= p.period_start
    AND cl.sent_time <  p.period_end
),

per_root AS (
  SELECT
    root_id,
    n_campaigns,
    CASE
      WHEN n_campaigns = 1 THEN COUNT(*)                  -- standalone: every send is its
                                                            -- own event, even repeats to the
                                                            -- same customer
      ELSE COUNT(DISTINCT customer_id)                     -- chain: a customer who took
                                                            -- multiple attempts counts once
    END AS qualifying_sends
  FROM logs
  GROUP BY root_id, n_campaigns
)

SELECT SUM(qualifying_sends) AS target_base
FROM per_root;

-- Per-chain breakdown (swap the final SELECT above for this one when debugging):
--
-- SELECT root_id, n_campaigns, qualifying_sends FROM per_root ORDER BY root_id;
--
-- Expected for the sample data:
--   9001 | 3 | 10   (chain 9001 -> 9002 -> 9003)
--   9101 | 1 | 7    (standalone)
--   9201 | 2 | 5    (chain 9201 -> 9202)
