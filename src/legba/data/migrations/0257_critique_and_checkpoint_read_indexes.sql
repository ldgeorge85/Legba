-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0257_critique_and_checkpoint_read_indexes.sql
--
-- Three read indexes (the console's remaining slow reads). Each was
-- measured before/after with EXPLAIN (ANALYZE, BUFFERS) on a copy of the live
-- analyst_outputs / audit_checkpoints rows (200,566 outputs of
-- which 87,811 critiques with their real documents; 293,435 checkpoints); the
-- "before" plans are the live ones.
--
-- 1. idx_analyst_outputs_critique_created_at (created_at, id) WHERE critique
--    The critique-window family: every reader that asks "critiques written
--    since T" (`kind = 'critique' AND title LIKE 'Faithfulness verify%' AND
--    created_at > $1` — judge-stats cube / health streak / uncited share, the
--    production gauge's judge availability, the rejudge floor) and the
--    scorecard's newest-first walk (`ORDER BY created_at DESC, id DESC`).
--    The only index on kind is (kind, produced_at), so each of these was a
--    parallel seq scan of the whole table (~31k heap pages) filtering out the
--    ~113k non-critique rows. After: an index range scan of the window; the
--    scorecard walk becomes an index-only backward scan.
--      scorecard newest 500:      seq scan + sort 97.6 ms  -> 24.3 ms
--      gauge judge window (1 d):  scan 100 ms -> 1.2 ms of a 80.6 ms read
--                                 (the rest is detoasting the 634 critiques)
--      uncited share (14 d):      0.49 s -> 0.44 s (with the P-5 reshaping
--                                 of _UNCITED_SQL that keeps its sort small)
--    3.5 MB.
--
-- 2. idx_analyst_outputs_critique_analyzed_analyst
--    ((data->>'analyzed_analyst_id'), created_at, id) WHERE critique
--    `GET /v3/eval/scorecard?analyst_id=X` matches the analyzed analyst inside
--    the critique document: without this index every critique is detoasted to
--    test the key (live: 349k buffers, 1.17 s for country_composition; 0.6 s
--    for an analyst with no critiques at all). After: a bitmap index scan of
--    that analyst's critiques only.
--      country_composition (16,449 critiques):  650 ms -> 70 ms
--      an analyst with none:                    598 ms -> 0.1 ms
--    5.7 MB.
--
-- 3. idx_audit_checkpoints_chain_head (chain_head_hash)
--    The lineage walk's covering-checkpoint read joins its (analyst_id,
--    receipt_hash) keys on audit_checkpoints.chain_head_hash; the only index
--    is (analyst_id, checkpointed_at), so the read hash-joined a seq scan of
--    every checkpoint (live: 0.08 s at 50 keys, 0.23-0.28 s at 400-5000,
--    1.0 s at 16,000). After: an index probe per key.
--      50 keys: 137 ms -> 1.8 ms; 400: 74 ms -> 10 ms; 2000: 330 ms -> 44 ms
--      (16,000 keys: the planner keeps the hash join, unchanged).
--    25 MB.
--
-- SAFETY ON LIVE-SIZED TABLES (the 0247 / 0251 pattern)
-- -----------------------------------------------------
-- The runner executes this file inside ONE transaction, so nothing here can be
-- CONCURRENTLY. Each index is built here ONLY when its table is small (a fresh
-- substrate, a test database: at most 50,000 rows). On a larger table this
-- file skips it with a NOTICE, and the merger builds the SAME index out of
-- transaction with `scripts/create_read_latency_indexes.py --create` (CREATE
-- INDEX CONCURRENTLY IF NOT EXISTS with the identical definition; a test pins
-- the script's definitions to this file's). Additive; re-apply is a no-op; no
-- inline BEGIN/COMMIT.

SET LOCAL lock_timeout = '10s';

DO $$
BEGIN
    IF (SELECT count(*) FROM (SELECT 1 FROM public.analyst_outputs LIMIT 50001) s) <= 50000 THEN
        CREATE INDEX IF NOT EXISTS idx_analyst_outputs_critique_created_at ON public.analyst_outputs USING btree (created_at, id) WHERE (kind = 'critique');
        CREATE INDEX IF NOT EXISTS idx_analyst_outputs_critique_analyzed_analyst ON public.analyst_outputs USING btree (((data ->> 'analyzed_analyst_id')), created_at, id) WHERE (kind = 'critique');
    ELSE
        RAISE NOTICE '0257: analyst_outputs has more than 50000 rows; the critique read indexes are NOT built here. Run scripts/create_read_latency_indexes.py --create (CONCURRENTLY).';
    END IF;
    IF (SELECT count(*) FROM (SELECT 1 FROM public.audit_checkpoints LIMIT 50001) s) <= 50000 THEN
        CREATE INDEX IF NOT EXISTS idx_audit_checkpoints_chain_head ON public.audit_checkpoints USING btree (chain_head_hash);
    ELSE
        RAISE NOTICE '0257: audit_checkpoints has more than 50000 rows; idx_audit_checkpoints_chain_head is NOT built here. Run scripts/create_read_latency_indexes.py --create (CONCURRENTLY).';
    END IF;
END
$$;
