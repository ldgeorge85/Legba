-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0096_correctness_labels.sql
--
-- P2-5 (correctness gold-set labeling loop) — per-FINDING operator correctness
-- verdicts + the pinned weekly worksheet sample.
--
-- NUMBERING NOTE: 0094/0095 are reserved by concurrent work; 0096 is this
-- branch's number. The runner discovers by sorted glob, so a gap is harmless.
--
-- WHY A NEW TABLE (not `unit_reference_labels`, migration 0057): the existing
-- gold set stores one (unit, target) REFERENCE ANSWER grounded to
-- canonical_source_ids — the deterministic source-overlap scorer's substrate
-- (unit_correctness_scorer, P2-T5). The labeling LOOP needs a different shape:
-- one SEMANTIC verdict per individual FINDING (was this read right?), with a
-- closed vocabulary, upsert identity, and a snapshot of what was judged.
-- Folding a verdict enum + finding_id + snapshot into 0057's answer rows would
-- muddle two distinct measurements (source-recall vs human semantic verdict);
-- the scoreboard reports BOTH, segregated, each with its own n.
--
-- HONESTY FIELDS:
--   * label vocabulary is CLOSED (CHECK constraint):
--     'correct' | 'partially_correct' | 'incorrect' | 'unresolvable'.
--     'unresolvable' is a first-class honest state (the operator looked and
--     could not judge) — EXCLUDED from the operator-correctness numerator and
--     denominator, never dropped and never scored as wrongness.
--   * finding_snapshot pins title + claims prose + citations AT LABEL TIME, so
--     a later supersession can never orphan the judgment (the operator's
--     verdict stays attached to exactly what was judged).
--   * created_at is the FIRST label time and is never updated; labeled_at
--     moves on re-label. Weekly-sample exclusion keys off created_at so a
--     finding labeled in a past week never re-enters a later week's sample.
--
-- `goldset_week_samples` pins each ISO week's worksheet membership on first
-- read (the band_calibration_claims "pinned claim" precedent): the sampler is
-- deterministic (rendezvous-hash seeded by the ISO week), but candidate churn
-- during the week (new findings, supersession) could still shift a recompute —
-- pinning makes "same week → same sample" a hard guarantee. ON CONFLICT DO
-- NOTHING keeps concurrent first reads idempotent (the deterministic sampler
-- computes the identical set on both sides of the race).
--
-- SAFETY (idempotent, additive, forward-only): CREATE TABLE/INDEX IF NOT
-- EXISTS only; no existing table is touched; re-apply and cold-start are both
-- no-ops. The runner wraps this file in its own transaction and records it in
-- `legba_data_migrations` (no inline BEGIN/COMMIT — same as 0092/0093).

CREATE TABLE IF NOT EXISTS public.correctness_labels (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    finding_id       uuid NOT NULL UNIQUE,       -- the judged analyst_outputs row; UNIQUE = the upsert identity (one verdict per finding, latest wins)
    unit_analyst_id  text NOT NULL,              -- the bounded unit that produced the finding (denormalized for per-unit aggregation)
    target_id        text,                       -- the finding's target at label time (NULL for a meta finding)
    label            text NOT NULL CHECK (label IN ('correct', 'partially_correct', 'incorrect', 'unresolvable')),
    rationale        text,                       -- optional operator note (why this verdict)
    labeled_by       text,                       -- the labeler / principal
    labeled_at       timestamptz NOT NULL DEFAULT now(),  -- moves on re-label
    created_at       timestamptz NOT NULL DEFAULT now(),  -- FIRST label time; never updated (weekly-sample exclusion keys off this)
    finding_snapshot jsonb NOT NULL DEFAULT '{}'::jsonb   -- title + claims + citations at label time (supersession can't orphan the judgment)
);

-- Backs the scoreboard's per-unit operator-correctness aggregate.
CREATE INDEX IF NOT EXISTS idx_correctness_labels_unit
    ON public.correctness_labels (unit_analyst_id);

-- The pinned weekly worksheet sample: one row per (ISO week, sampled finding).
CREATE TABLE IF NOT EXISTS public.goldset_week_samples (
    week            text NOT NULL,               -- ISO week key, e.g. '2026-W30'
    finding_id      uuid NOT NULL,
    rank            int  NOT NULL,               -- display order within the week
    unit_analyst_id text NOT NULL,               -- stratum bookkeeping (which unit slot this filled)
    sampled_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (week, finding_id)
);
