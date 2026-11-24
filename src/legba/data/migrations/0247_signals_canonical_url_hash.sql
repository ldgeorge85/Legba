-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0247_signals_canonical_url_hash.sql
--
-- The stored tier-1 dedupe key: `signals.canonical_url_hash` = SHA-256 of
-- `legba.data._url_canon.canonical_url(canonical_url)`, and the two partial
-- indexes the ingest-dedupe lookup reads.
--
-- WHY
-- ---
-- `IngestDedupe._lookup_by_url_hash` (data/filters/ingest_dedupe.py) compared
-- the probe's canonical URL against every stored row by reading ALL non-empty
-- `canonical_url` rows of the tenant and canonicalising each in Python, once
-- per written signal. Measured on live 2026-11-13: 380,879 rows, 1.1 s of
-- database execution plus ~1.9 s of row transfer and up to ~11 s of Python on
-- a miss — about 7 s per written signal, the median 9-15 s gap between
-- written signals fleet-wide, and the reason a 30 s poll budget wrote four
-- UKMTO incidents. (Audit row A-31, "unindexed URL scan", was the same
-- defect.) The stored URL is the display URL as landed (158k of 381k already
-- canonical), but `canonical_url()` is idempotent on all 381k stored values,
-- so a hash of the canonical form, computed once at write time, turns the
-- lookup into one indexed equality with identical matches.
--
--   * `idx_signals_canonical_url_hash` (canonical_url_hash, owner_tenant,
--     fetched_at, id) WHERE canonical_url_hash IS NOT NULL — the equality
--     probe, tenant-scoped, already in the lookup's earliest-first order.
--   * `idx_signals_canonical_url_unhashed` (fetched_at, id) WHERE the hash is
--     NULL but canonical_url is not empty — the bounded fallback set the
--     lookup still canonicalises in Python while the backfill is incomplete.
--     It empties as `scripts/backfill_signal_url_hash.py` proceeds (the
--     0239 / 0081 shrinking-partial-index pattern).
--
-- SAFETY ON A LIVE ~430k-ROW TABLE
-- --------------------------------
-- The runner executes this file inside ONE transaction, so nothing here can
-- be CONCURRENTLY (0218's note). Hence:
--   * the column is added NULLABLE with NO default — a catalog-only change;
--     the ACCESS EXCLUSIVE lock it needs is held for milliseconds, and
--     `lock_timeout` makes the migration FAIL (retry it) instead of queueing
--     every signal writer behind a long-running reader;
--   * the indexes are built here ONLY when the table is small (a fresh
--     substrate, a test database: at most 50,000 rows). On a larger table
--     this file skips them with a NOTICE, and the merger builds the SAME two
--     indexes out of transaction with
--     `scripts/backfill_signal_url_hash.py --create-indexes` (CREATE INDEX
--     CONCURRENTLY IF NOT EXISTS with the identical definitions; a test pins
--     the script's definitions to the ones in this file and checks the
--     built indexes against pg_indexes);
--   * the backfill is NOT in this file: it runs in bounded batches from the
--     same script, dry-run first.
-- Additive, re-apply is a no-op (IF NOT EXISTS throughout); no inline
-- BEGIN/COMMIT.

SET LOCAL lock_timeout = '10s';

ALTER TABLE public.signals
    ADD COLUMN IF NOT EXISTS canonical_url_hash TEXT;

DO $$
BEGIN
    IF (SELECT count(*) FROM (SELECT 1 FROM public.signals LIMIT 50001) s) <= 50000 THEN
        CREATE INDEX IF NOT EXISTS idx_signals_canonical_url_hash
            ON public.signals USING btree (canonical_url_hash, owner_tenant, fetched_at, id)
            WHERE canonical_url_hash IS NOT NULL;
        CREATE INDEX IF NOT EXISTS idx_signals_canonical_url_unhashed
            ON public.signals USING btree (fetched_at, id)
            WHERE canonical_url_hash IS NULL
              AND canonical_url IS NOT NULL
              AND canonical_url <> '';
    ELSE
        RAISE NOTICE '0247: signals has more than 50000 rows; the two canonical_url_hash indexes are NOT built here. Run scripts/backfill_signal_url_hash.py --create-indexes (CONCURRENTLY), then the backfill.';
    END IF;
END
$$;
