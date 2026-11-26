-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 20261125T0844_trace_bulk_bundles.sql
--
-- Compressed bulk for old analyst traces (legba.data.trace_bulk).
--
--   analyst_trace_bundles   one row per bundle: the prompt_rendered,
--                           input_payload and output_payload of up to N runs
--                           of one analyst, xz-compressed together. `body` is
--                           STORAGE EXTERNAL: it is already compressed, so
--                           TOAST stores it out of line without trying pglz.
--   analyst_traces          two nullable pointer columns:
--                             bulk_bundle_id  the bundle holding the run's bulk
--                             bulk_columns    which bulk columns were non-NULL
--                                             when moved
--                           and a foreign key to the bundle, so a bundle that
--                           a trace points at cannot be deleted.
--   idx_analyst_traces_bulk_pending
--                           (run_started_at, analyst_id) WHERE bulk_bundle_id
--                           IS NULL: the compaction job's work list (which
--                           analysts have old rows pending, and each one's
--                           oldest pending rows). It holds only rows not yet
--                           compacted, so it stays about as large as the age
--                           cut-off's worth of runs, and the job's reads never
--                           walk the rows it already compacted.
--
-- LOCKS ON A LIVE-SIZED analyst_traces
-- ------------------------------------
-- * ADD COLUMN without a default is a catalog change: no table rewrite and no
--   scan. It takes ACCESS EXCLUSIVE for the instant of the catalog update.
-- * The foreign key is added NOT VALID: no scan of existing rows (they are all
--   NULL, so nothing could fail it); new and updated rows are checked. It
--   takes SHARE ROW EXCLUSIVE on both tables for the instant of the catalog
--   update.
-- * lock_timeout 10s: if a long transaction holds the table, this file fails
--   and can be re-run rather than queueing every trace writer behind it.
-- * The runner holds each migration in one transaction, so CREATE INDEX
--   CONCURRENTLY cannot run here. The pending index is built here only when
--   the table is small (a fresh substrate, a test database: at most 50,000
--   rows); on a larger table this file skips it with a NOTICE and
--   `scripts/create_read_latency_indexes.py --create` builds the same index
--   CONCURRENTLY.
--
-- Re-applying is a no-op. No inline BEGIN/COMMIT.

SET LOCAL lock_timeout = '10s';

CREATE TABLE IF NOT EXISTS public.analyst_trace_bundles (
    bundle_id     uuid PRIMARY KEY,
    analyst_id    text NOT NULL,
    codec         text NOT NULL,
    member_count  integer NOT NULL CHECK (member_count > 0),
    raw_bytes     bigint NOT NULL,
    stored_bytes  bigint NOT NULL,
    created_at    timestamptz NOT NULL DEFAULT now(),
    body          bytea NOT NULL
);

ALTER TABLE public.analyst_trace_bundles ALTER COLUMN body SET STORAGE EXTERNAL;

COMMENT ON TABLE public.analyst_trace_bundles IS
    'Compressed prompt_rendered / input_payload / output_payload of up to N '
    'old runs of one analyst (legba.data.trace_bulk). Read only through '
    'trace_bulk.hydrate_traces.';

ALTER TABLE public.analyst_traces ADD COLUMN IF NOT EXISTS bulk_bundle_id uuid;
ALTER TABLE public.analyst_traces ADD COLUMN IF NOT EXISTS bulk_columns text[];

COMMENT ON COLUMN public.analyst_traces.bulk_bundle_id IS
    'When set, prompt_rendered, input_payload and output_payload are NULL here '
    'and held in this analyst_trace_bundles row; read through '
    'trace_bulk.hydrate_traces.';
COMMENT ON COLUMN public.analyst_traces.bulk_columns IS
    'The bulk columns that were non-NULL when moved to the bundle.';

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'analyst_traces_bulk_bundle_id_fkey'
           AND conrelid = 'public.analyst_traces'::regclass
    ) THEN
        ALTER TABLE public.analyst_traces
            ADD CONSTRAINT analyst_traces_bulk_bundle_id_fkey
            FOREIGN KEY (bulk_bundle_id)
            REFERENCES public.analyst_trace_bundles (bundle_id)
            NOT VALID;
    END IF;
END
$$;

DO $$
BEGIN
    IF (SELECT count(*) FROM (SELECT 1 FROM public.analyst_traces LIMIT 50001) s) <= 50000 THEN
        CREATE INDEX IF NOT EXISTS idx_analyst_traces_bulk_pending ON public.analyst_traces USING btree (run_started_at, analyst_id) WHERE (bulk_bundle_id IS NULL);
    ELSE
        RAISE NOTICE 'trace_bulk_bundles: analyst_traces has more than 50000 rows; idx_analyst_traces_bulk_pending is NOT built here. Run scripts/create_read_latency_indexes.py --create (CONCURRENTLY).';
    END IF;
END
$$;
