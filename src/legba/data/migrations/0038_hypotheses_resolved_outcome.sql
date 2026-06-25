-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0038_hypotheses_resolved_outcome.sql
--
-- EXOGENOUS calibration column for the ACH competing_hypotheses kind
-- (analytical-rigor stream — close the CIRCULAR Brier).
--
-- WHY:
--   calibration_tracking scored ACH hypotheses by reading `status`
--   (confirmed=1 / refuted=0) as the OUTCOME and deriving the claimed
--   confidence from |evidence_balance|. But `status` IS SET from
--   `evidence_balance` (competing_hypotheses._status_for auto-transitions a
--   hypothesis past ±K). So the Brier measured the system against its OWN
--   evidence count — a CIRCULAR score that is structurally incapable of
--   detecting miscalibration (a confidently-wrong analyst scores perfectly).
--   The deep-review honesty disclosure named this; this column backs the fix.
--
-- WHAT:
--   Three EXOGENOUS resolution columns on `public.hypotheses`:
--     * `resolved_outcome smallint`  — 1 = the thesis came true, 0 = it did
--       not. NULL = unresolved (the default — no exogenous signal yet).
--     * `resolved_at  timestamptz`   — when the exogenous resolution landed.
--     * `resolved_by  text`          — provenance of the resolution: who/what
--       resolved it (`'subsequent_facts'` for the automated resolver that
--       reads facts produced AFTER the hypothesis, or an operator label like
--       `'operator:<id>'`). NEVER the hypothesis's own evidence_balance.
--   calibration_tracking reads `resolved_outcome` (NOT `status`) as the Brier
--   outcome, so the score becomes EXOGENOUS: the claim is graded against what
--   the world subsequently showed, not against the evidence that produced it.
--
-- SAFETY (idempotent, additive, NULL-default — no data repair):
--   All three columns are nullable with no default value, so every existing
--   row is left `resolved_outcome IS NULL` (unresolved) and is simply absent
--   from the EXOGENOUS calibration sample until a resolver or an operator
--   stamps it. ADD COLUMN IF NOT EXISTS is a no-op on re-apply. No row is
--   mutated; no existing column or index is touched. The CHECK constraint is
--   added guarded so re-applying is a no-op.

ALTER TABLE public.hypotheses
    ADD COLUMN IF NOT EXISTS resolved_outcome smallint,
    ADD COLUMN IF NOT EXISTS resolved_at      timestamp with time zone,
    ADD COLUMN IF NOT EXISTS resolved_by      text;

-- resolved_outcome is a 0/1 label or NULL (unresolved). Guard the domain so a
-- bad write fails loud rather than silently corrupting the Brier sample.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'hypotheses_resolved_outcome_chk'
    ) THEN
        ALTER TABLE public.hypotheses
            ADD CONSTRAINT hypotheses_resolved_outcome_chk
            CHECK (resolved_outcome IS NULL OR resolved_outcome IN (0, 1));
    END IF;
END
$$;

-- Partial index over the RESOLVED rows only — the exogenous calibration pull
-- filters `resolved_outcome IS NOT NULL`, and resolved rows are the small
-- minority, so a partial index keeps the calibration scan cheap.
CREATE INDEX IF NOT EXISTS idx_hypotheses_resolved_outcome
    ON public.hypotheses (resolved_at)
    WHERE resolved_outcome IS NOT NULL;
