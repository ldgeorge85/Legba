-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0197_unit_correctness_reference_age.sql
--
-- HOW OLD WAS THE REFERENCE THIS NUMBER RESTS ON
-- (LEDGER_RESET_2026-09-16 §5, G1-FIX defect D2).
--
-- 0196 shipped the grader with a reference lookup that required
-- `window_start <= as_of AND window_end >= as_of`. A reference is built for a
-- window that ENDS at its own T0, and every read it grades lands AFTER that
-- instant — so on the first live forced run, a reference covering
-- 2026-09-02T19:30Z → 2026-09-16T19:30Z matched NOTHING at 22:32Z the same day
-- and the sweep graded zero units. Strict containment made the nightly 01:20Z
-- run ungradable by construction.
--
-- The fix is a GRACE: a reference stays current until `window_end + grace`
-- (LEGBA_GRADER_REFERENCE_GRACE_DAYS, default 7) or until a newer reference for
-- that target supersedes it. Which raises the question this column answers.
--
-- WHY THE AGE IS A COLUMN AND NOT A DERIVATION. Once a number may rest on a
-- reference whose window has closed, "88% correct" and "88% correct against a
-- reference that closed six days ago" are different statements, and only the
-- second one is true. The age COULD be recomputed later from `as_of` and the
-- reference's `window_end` — but a reader would have to know to do it, and a
-- share read off this table alone would silently lose the caveat. The whole
-- design of `unit_correctness` is that a share travels with what qualifies it
-- (`correctness_share` beside `coverage_share`, both beside their n, every
-- single-family label counted). The age of the reference belongs in that set.
--
-- NULLABLE, and null is not zero: rows written before this migration measured
-- against a reference whose window contained the stamp, but the distance was
-- never recorded, and backfilling a 0 would assert a freshness nobody measured.
--
-- SAFETY (idempotent, additive, forward-only): ADD COLUMN IF NOT EXISTS plus a
-- guarded constraint add. No backfill, no rewrite, no default — an existing row
-- keeps NULL. The runner wraps this file in its own transaction and records it
-- in `legba_data_migrations` (no inline BEGIN/COMMIT here).

ALTER TABLE public.unit_correctness
    ADD COLUMN IF NOT EXISTS reference_age_days numeric;

COMMENT ON COLUMN public.unit_correctness.reference_age_days IS
    'as_of - reference.window_end, in DAYS, floored at zero and fractional '
    '(a sweep 4h20m past a window''s close is 0.18). Zero means the stamp fell '
    'inside the reference window; a positive value is how far past its close '
    'this number was measured, bounded by '
    'LEGBA_GRADER_REFERENCE_GRACE_DAYS (default 7) at write time. NULL means '
    'the distance was not recorded — every row written before migration 0197 — '
    'and is NOT zero.';

-- An age is a distance backwards from the stamp to a window that had already
-- closed. Negative is not a stale reference read early; it is a bug, and it is
-- unrepresentable rather than merely unexpected.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'ck_unit_correctness_reference_age'
           AND conrelid = 'public.unit_correctness'::regclass
    ) THEN
        ALTER TABLE public.unit_correctness
            ADD CONSTRAINT ck_unit_correctness_reference_age
            CHECK (reference_age_days IS NULL OR reference_age_days >= 0);
    END IF;
END
$$;

-- The staleness read: "which published numbers rest on an ageing reference",
-- newest first. Partial — a row with no recorded age cannot answer the
-- question and would only dilute the index.
CREATE INDEX IF NOT EXISTS idx_unit_correctness_reference_age
    ON public.unit_correctness (reference_age_days DESC, as_of DESC)
 WHERE reference_age_days IS NOT NULL;
