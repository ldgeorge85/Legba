-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0212_sealed_forecast_ledger.sql
--
-- H13 — the sealed forecast ledger (Program 7, pulled forward 2026-09-23).
--
-- (1) `acute_forecasts.resolution_test TEXT NOT NULL` — the resolution test
--     frozen as TEXT at mint by forecast_acute.py (its `RESOLUTION_TEST`
--     constant): the event class, the exogenous column/join the resolver
--     counts on, the threshold and the window. Existing rows are backfilled
--     with the resolver's CURRENT rule prefixed `retro: ` — an honest
--     retroactive stamp, not a claim the text existed at mint. The DO block
--     below RAISEs NOTICE with the backfilled row count at apply time.
--
-- (2) `receipt_anchors` — the external timestamp ledger the `receipt_anchor`
--     handler writes: one row per (day, calendar) holding the day's Merkle
--     root over the latest `analyst_traces.receipt_hash` per analyst, the
--     calendar endpoint the 32-byte digest was POSTed to, the returned proof
--     bytes, and status ('submitted' | 'pending' — an unreachable calendar is
--     retried next tick, never an error that stops the run).
--
-- WHAT (idempotent: ADD COLUMN IF NOT EXISTS / CREATE TABLE IF NOT EXISTS;
-- the backfill UPDATE is a no-op on re-apply because retro-stamped rows are
-- non-NULL).

ALTER TABLE public.acute_forecasts
    ADD COLUMN IF NOT EXISTS resolution_test TEXT;

DO $$
DECLARE
    n integer;
BEGIN
    UPDATE public.acute_forecasts
    SET resolution_test = 'retro: ' ||
        'class=hazard_severe; '
        || 'o=1 iff >=1 signal with source_id IN (source.usgs.earthquakes_m45, source.nasa.eonet_events) '
        || 'geo-overlapping the region''s geo codes and timed by the UPSTREAM event '
        || 'stamp (usgs origin ms / eonet event date / fetched_at fallback) inside '
        || '[window_start, window_end); '
        || 'window=7d weekly; resolver grace=1d'
    WHERE resolution_test IS NULL;
    GET DIAGNOSTICS n = ROW_COUNT;
    RAISE NOTICE '0212: backfilled % acute_forecasts rows with retro: resolution_test', n;
END $$;

ALTER TABLE public.acute_forecasts
    ALTER COLUMN resolution_test SET NOT NULL;

CREATE TABLE IF NOT EXISTS public.receipt_anchors (
    id           uuid      DEFAULT gen_random_uuid() NOT NULL PRIMARY KEY,
    day          date      NOT NULL,
    root_hash    text      NOT NULL,
    leaf_count   integer   NOT NULL,
    calendar     text      NOT NULL,
    proof        bytea,
    status       text      NOT NULL,
    submitted_at timestamp with time zone,
    created_at   timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT uq_receipt_anchors_day_calendar UNIQUE (day, calendar)
);

COMMENT ON TABLE public.receipt_anchors IS
    'H13 — one OpenTimestamps anchor per (day, calendar) over the receipt-chain '
    'heads (Merkle root of sha256(analyst_id || receipt_hash) leaves, sorted by '
    'analyst_id). proof bytea = the calendar''s raw response; status pending = '
    'unreachable, retried next tick.';
