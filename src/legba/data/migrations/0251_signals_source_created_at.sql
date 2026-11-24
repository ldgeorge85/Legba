-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0251_signals_source_created_at.sql
--
-- `idx_signals_source_created_at` (source_id, created_at): the per-source
-- freshness index `GET /v3/system/source-firing` reads (lane R-1, the
-- console landing's read latency).
--
-- WHY
-- ---
-- The route answered "how many signals did each source land in 24 h / 7 days,
-- and when was its newest" with a GROUP BY over the WHOLE signals table — a
-- parallel sequential scan of ~436k rows (2.4 GB with TOAST) on every call,
-- 1.1-1.9 s of database time live on 2026-11-14, and the landing asks three
-- times. With this index the route reads each source head's newest row (one
-- index probe) and its 7-day range (an index range scan) instead: 20 ms on a
-- test database loaded to the live row count, against 630 ms for the old
-- statement there. Same rows, byte for byte (compared on live and on the test
-- database inside one snapshot).
--
-- SAFETY ON A LIVE ~436k-ROW TABLE (the 0247 pattern)
-- ---------------------------------------------------
-- The runner executes this file inside ONE transaction, so nothing here can be
-- CONCURRENTLY. The index is built here ONLY when the table is small (a fresh
-- substrate, a test database: at most 50,000 rows). On a larger table this
-- file skips it with a NOTICE, and the merger builds the SAME index out of
-- transaction with `scripts/create_read_latency_indexes.py --create` (CREATE
-- INDEX CONCURRENTLY IF NOT EXISTS with the identical definition; a test pins
-- the script's definition to this file's). Additive; re-apply is a no-op; no
-- inline BEGIN/COMMIT.

SET LOCAL lock_timeout = '10s';

DO $$
BEGIN
    IF (SELECT count(*) FROM (SELECT 1 FROM public.signals LIMIT 50001) s) <= 50000 THEN
        CREATE INDEX IF NOT EXISTS idx_signals_source_created_at
            ON public.signals USING btree (source_id, created_at);
    ELSE
        RAISE NOTICE '0251: signals has more than 50000 rows; idx_signals_source_created_at is NOT built here. Run scripts/create_read_latency_indexes.py --create (CONCURRENTLY).';
    END IF;
END
$$;
