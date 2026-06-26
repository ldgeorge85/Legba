-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0050_receipt_chain_fork_tombstone.sql
--
-- D11 — forward-only AUDIT tombstone on the historical receipt-chain forks.
--
-- WHY:
--   Each analyst keeps a tamper-evident SHA-256 receipt chain in
--   `analyst_traces` (`receipt_hash` chains `prev_receipt_hash`; see
--   data/provenance/receipts.py + _core.compute_receipt_hash). A healthy chain
--   is linear with exactly ONE tip. Process recreates that lost the in-memory
--   head, plus two concurrent same-analyst runs racing the prev pointer, FORKED
--   several chains into a multi-tip tree (review: cross_source_dedup ~27 tips,
--   country_critic ~6). The runtime now derives the head DETERMINISTICALLY
--   (receipts.py::_head_locked picks the stable tip) so it stops MAKING the fork
--   worse — but the existing forks remain.
--
--   We do NOT re-link the chain. `prev_receipt_hash` is one of the SIGNED fields
--   of every successor's `receipt_hash` (compute_receipt_hash hashes
--   prev_receipt_hash), so rewriting any `prev_receipt_hash` would invalidate the
--   signatures of every row downstream of it and destroy the tamper-evidence we
--   are trying to preserve. This migration is therefore AUDIT-ONLY: it MARKS the
--   historical fork rows so an operator / verifier can see exactly where each
--   chain branched, and leaves every hash byte-for-byte intact.
--
-- WHAT (idempotent, audit-only — writes a tombstone into `error_payload`, an
-- UNSIGNED column, under a namespaced key; never touches receipt_hash /
-- prev_receipt_hash / status / output_payload / any signed field):
--   A row is a FORK ARTIFACT when, within its analyst's chain, EITHER
--     (a) FORK PARENT: its `receipt_hash` is the `prev_receipt_hash` of MORE
--         THAN ONE sibling row (the branch point — >1 child share one prev), OR
--     (b) NON-CANONICAL TIP: it is a tip (its `receipt_hash` is referenced by no
--         row's `prev_receipt_hash`) AND it is NOT the canonical tip the runtime
--         would pick (newest by run_started_at, ties by run_id text — mirrors
--         receipts.py::_head_locked ORDER BY). The single canonical tip per
--         analyst is the live head and is NOT tombstoned.
--   The marker namespaces under error_payload->'d11_fork_tombstone' and records
--   the reason + when it was stamped. We MERGE (|| ) onto any existing
--   error_payload so a genuine error_payload on a fork row is preserved.
--
-- SAFETY (idempotent, transactional, audit-only, signature-preserving):
--   * Only `error_payload` is written; every signed field is untouched, so all
--     receipt signatures still verify after this runs.
--   * Re-run is a no-op: the WHERE clause excludes any row that already carries
--     `error_payload ? 'd11_fork_tombstone'`.
--   * `status` is left as-is (a tombstoned fork row that succeeded still reads
--     'success'); this is a marker, not a failure.

WITH child_counts AS (
    -- For each (analyst, prev_receipt_hash), how many rows chain off it. >1 = a
    -- fork branch point on the parent whose receipt_hash == that prev.
    SELECT
        analyst_id,
        prev_receipt_hash,
        count(*) AS n_children
    FROM analyst_traces
    WHERE prev_receipt_hash IS NOT NULL
    GROUP BY analyst_id, prev_receipt_hash
),
fork_parents AS (
    -- (a) A row whose receipt_hash is some prev shared by >1 child.
    SELECT t.run_id
    FROM analyst_traces t
    JOIN child_counts c
      ON c.analyst_id = t.analyst_id
     AND c.prev_receipt_hash = t.receipt_hash
    WHERE c.n_children > 1
),
tips AS (
    -- Tip = a receipt_hash referenced by no row's prev_receipt_hash (same
    -- DISTINCT-guarded NOT IN the runtime uses).
    SELECT t.run_id, t.analyst_id, t.run_started_at
    FROM analyst_traces t
    WHERE t.receipt_hash NOT IN (
        SELECT DISTINCT p.prev_receipt_hash
        FROM analyst_traces p
        WHERE p.prev_receipt_hash IS NOT NULL
    )
),
canonical_tip AS (
    -- The ONE tip per analyst the runtime keeps as live head (receipts.py
    -- ORDER BY run_started_at DESC, run_id::text DESC LIMIT 1). NOT tombstoned.
    SELECT DISTINCT ON (analyst_id) run_id
    FROM tips
    ORDER BY analyst_id, run_started_at DESC, run_id::text DESC
),
non_canonical_tips AS (
    -- (b) Every tip that is NOT its analyst's canonical tip = an orphaned branch
    -- leaf left by the fork.
    SELECT run_id FROM tips
    EXCEPT
    SELECT run_id FROM canonical_tip
),
fork_rows AS (
    SELECT run_id FROM fork_parents
    UNION
    SELECT run_id FROM non_canonical_tips
)
UPDATE analyst_traces t
   SET error_payload = COALESCE(t.error_payload, '{}'::jsonb)
       || jsonb_build_object(
            'd11_fork_tombstone',
            jsonb_build_object(
                'reason', CASE
                    WHEN fp.run_id IS NOT NULL AND nct.run_id IS NOT NULL
                        THEN 'fork_parent+non_canonical_tip'
                    WHEN fp.run_id IS NOT NULL
                        THEN 'fork_parent'
                    ELSE 'non_canonical_tip'
                END,
                'note', 'receipt chain fork artifact; audit-only, prev_receipt_hash NOT rewritten (would break signatures)',
                'migration', '0050_receipt_chain_fork_tombstone',
                'tombstoned_at', to_jsonb(NOW())
            )
       )
  FROM fork_rows fr
  LEFT JOIN fork_parents fp ON fp.run_id = fr.run_id
  LEFT JOIN non_canonical_tips nct ON nct.run_id = fr.run_id
 WHERE t.run_id = fr.run_id
   AND NOT (COALESCE(t.error_payload, '{}'::jsonb) ? 'd11_fork_tombstone');
