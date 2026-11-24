-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0237_indicator_flips.sql
--
-- `indicator_flips` — every indicator status flip the `indicator_tracker`
-- observes, ONCE, graded `raw` or `debounced` (X3 debounce lane, W11-F55).
--
-- WHY
-- ---
-- The tracker kept no record of what it had already reported: every heartbeat
-- it diffed the two newest runs of every unit stream and, whenever any stream
-- moved, re-published the whole flip set. Over seven days 265 receipts
-- carried 11,517 flip mentions for 684 distinct flips (about 17 each), against
-- docs/ANALYSIS.md's "an idempotent re-run never repeats a feed row". The
-- distinct flips were nearly symmetric (346 up, 338 down): with no hysteresis
-- an indicator toggling every run read as a stream of changes.
--
-- The tracker now walks each stream's runs once (a watermark per stream in
-- `alert_trigger_watermarks`, class `indicator_tracker`) and writes:
--   * `raw`       — every status change between two consecutive runs of one
--                   indicator (the old pairwise diff): the audit trail, never
--                   fed;
--   * `debounced` — a change of the indicator's CONFIRMED status, once the new
--                   status has held (`hysteresis_runs` consecutive runs, or a
--                   span of `hysteresis_hours`; docs/TUNABLES.md). Only these
--                   reach the feed, and `debounced_flips(target, since)` reads
--                   only these.
--
-- `evidence_finding_id` is the run the change was seen in: for a raw flip the
-- run whose status differs from the run before (`prev_finding_id`); for a
-- debounced flip the ONSET run, the first run in the new status, while
-- `confirmed_finding_id` is the run that made it hold. The unique index makes
-- one row per (grade, unit, desk, indicator, transition, evidence run): a
-- re-run over the same evidence inserts nothing.
--
-- `backfill` marks flips found on a stream's FIRST sweep, read from history
-- rather than observed as they happened; they are kept for the reader and
-- never fed. `raw_flips_between` on a debounced row counts the raw toggles it
-- took to settle (1 = a clean change).
--
-- No foreign keys: the finding ids point into `analyst_outputs`, whose rows are
-- append-only, and a missing referent must not block the audit row.
--
-- SAFETY
-- ------
-- Additive: a new table and its indexes. Re-apply is a no-op (IF NOT EXISTS);
-- routed through the runner (no inline BEGIN/COMMIT).

CREATE TABLE IF NOT EXISTS public.indicator_flips (
    id                   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    grade                text NOT NULL,
    target_id            text,
    source_analyst_id    text NOT NULL,
    indicator_id         text NOT NULL,
    statement            text NOT NULL DEFAULT '',
    from_status          text NOT NULL,
    to_status            text NOT NULL,
    activation           boolean NOT NULL,
    match                text NOT NULL DEFAULT 'id',
    citations            jsonb NOT NULL DEFAULT '[]'::jsonb,
    evidence_finding_id  uuid NOT NULL,
    evidence_at          timestamptz NOT NULL,
    prev_finding_id      uuid,
    confirmed_finding_id uuid,
    confirmed_at         timestamptz,
    confirmed_by         text,
    held_runs            integer,
    held_hours           real,
    raw_flips_between    integer,
    backfill             boolean NOT NULL DEFAULT false,
    method_version       text NOT NULL,
    recorded_at          timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT indicator_flips_grade_vocab CHECK (grade IN ('raw', 'debounced')),
    CONSTRAINT indicator_flips_status_vocab CHECK (
        from_status IN ('triggered', 'not_observed', 'expired')
        AND to_status IN ('triggered', 'not_observed', 'expired')
    ),
    CONSTRAINT indicator_flips_is_a_change CHECK (from_status <> to_status),
    CONSTRAINT indicator_flips_match_vocab CHECK (match IN ('id', 'statement')),
    CONSTRAINT indicator_flips_confirmed_by_vocab CHECK (
        confirmed_by IS NULL OR confirmed_by IN ('runs', 'hours')
    ),
    -- A debounced row always names the run that confirmed it; a raw row never.
    CONSTRAINT indicator_flips_confirmation_shape CHECK (
        (grade = 'debounced') = (confirmed_finding_id IS NOT NULL
                                 AND confirmed_at IS NOT NULL
                                 AND confirmed_by IS NOT NULL)
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS indicator_flips_once
    ON public.indicator_flips (grade, source_analyst_id, (coalesce(target_id, '')),
                               indicator_id, from_status, to_status,
                               evidence_finding_id);

-- The reader: debounced flips of one desk (or all) confirmed inside a window.
CREATE INDEX IF NOT EXISTS indicator_flips_debounced_confirmed
    ON public.indicator_flips (target_id, confirmed_at)
    WHERE grade = 'debounced';

CREATE INDEX IF NOT EXISTS indicator_flips_debounced_confirmed_all
    ON public.indicator_flips (confirmed_at)
    WHERE grade = 'debounced';

COMMENT ON TABLE public.indicator_flips IS
    'Every indicator_tracker flip, once: raw (consecutive-run change, audit '
    'only) or debounced (a held change of the confirmed status, the only grade '
    'fed). Unique per grade/unit/desk/indicator/transition/evidence run (mig 0237).';
