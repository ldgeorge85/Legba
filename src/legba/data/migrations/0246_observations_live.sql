-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0246_observations_live.sql
--
-- N-L: let `observations` (migration 0220) accept LIVE series rows written by
-- a source's ObservationSink, without weakening what its history-only CHECK
-- protected.
--
-- WHAT `observations_origin_class_history_only` PROTECTED. It refused every
-- origin_class but the three history classes, so the ONLY writer of the table
-- was the collection loader and every row in it was an operator-approved,
-- licence-stamped HOLDING of the past. Readers rely on that by construction:
-- `runtime/_observations_read.py` reads only rows whose `collection_id` is a
-- `collection_descriptors` head in state `loaded`, and the desk history
-- grounding presents what it reads as history, never as "now" (the collection
-- firewall: history must not look like a live surge, SEAMS #57/#62).
--
-- THE REPLACEMENT keeps both halves and states the pairing explicitly:
--   * a HISTORY row (archive / backfill_*) must NOT carry a source_id — the
--     loader's rows are exactly what they were;
--   * a LIVE row must carry its source_id, a `collection_id` of
--     'live:<source_id>' (a value the CollectionId pattern can never match, so
--     no history read can ever select a live row — the firewall is structural,
--     no reader SQL changed), the sha256 of the fetched response it was parsed
--     from (`fetch_sha256`, resolvable in `fetch_versions`), a methodology
--     version, an explicit period granularity and how `record_time` was set.
-- Every other origin_class stays refused.
--
-- APPEND-ONLY for live rows, by trigger: a revision is a NEW row (the unique
-- identity already includes record_time), an as-of read picks the latest
-- record_time <= as_of. UPDATE/DELETE of a live row raises. History rows keep
-- the loader's semantics (its tests tear down their own rows).
--
-- SAFETY: additive columns (NULLable), one CHECK replaced by a strictly
-- equivalent-or-stronger one for every existing row (all 355 live-DB rows on
-- 2026-11-13 are 'archive' with no source_id), one trigger, one partial
-- index. The runner wraps this file in its own transaction.

ALTER TABLE public.observations
    ADD COLUMN IF NOT EXISTS source_id           text,
    ADD COLUMN IF NOT EXISTS methodology_version text,
    ADD COLUMN IF NOT EXISTS fetch_sha256        text,
    ADD COLUMN IF NOT EXISTS period              text,
    ADD COLUMN IF NOT EXISTS record_time_basis   text;

ALTER TABLE public.observations
    DROP CONSTRAINT IF EXISTS observations_origin_class_history_only;
ALTER TABLE public.observations
    DROP CONSTRAINT IF EXISTS observations_origin_class_pairing;
ALTER TABLE public.observations
    ADD CONSTRAINT observations_origin_class_pairing CHECK (
        (origin_class IN ('archive', 'backfill_native', 'backfill_reconstructed')
         AND source_id IS NULL)
        OR
        (origin_class = 'live'
         AND source_id IS NOT NULL
         AND collection_id = 'live:' || source_id
         AND fetch_sha256 ~ '^[0-9a-f]{64}$'
         AND methodology_version IS NOT NULL
         AND period IN ('day', 'week', 'month', 'quarter', 'year')
         AND record_time_basis IN ('provider', 'first_fetch'))
    );

CREATE OR REPLACE FUNCTION public.observations_live_append_only()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.origin_class = 'live' THEN
        RAISE EXCEPTION 'observations: live rows are append-only (a revision is a new row) — % refused for %',
            TG_OP, OLD.id USING ERRCODE = 'restrict_violation';
    END IF;
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS observations_live_append_only ON public.observations;
CREATE TRIGGER observations_live_append_only
    BEFORE UPDATE OR DELETE ON public.observations
    FOR EACH ROW EXECUTE FUNCTION public.observations_live_append_only();

-- The ObservationSink's change test and the series_change detector's window:
-- latest vintage per (source, series, subject, period).
CREATE INDEX IF NOT EXISTS observations_live_vintage_idx
    ON public.observations USING btree
    (source_id, series_id, subject, valid_from, valid_to, record_time DESC)
    WHERE origin_class = 'live';

COMMENT ON COLUMN public.observations.fetch_sha256 IS
    'N-L: sha256 of the fetched response this live value was parsed from '
    '(fetch_versions.sha256; bytes in the evidence store). NULL for history.';
COMMENT ON COLUMN public.observations.record_time_basis IS
    'N-L: provider = record_time is the provider''s revision stamp; '
    'first_fetch = the provider gives none, so record_time is the fetch time of '
    'the poll that first saw this value.';
