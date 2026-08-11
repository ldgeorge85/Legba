-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0122_band_calibration_judge_pipeline_version.sql
--
-- POPULATION SPLIT KEY (2026-08-02 engine review, P3 §5a). `verify.py` has
-- stamped every faithfulness critique with `judge_pipeline_version` since
-- 0c4c165 — and repo-wide the stamp had NO READER. Two writes, one test, some
-- prose. "The population SPLIT key currently splits nothing."
--
-- It matters here specifically. Band-calibration claims are logged at a
-- transition and resolved 14/28 days later, and bands derive from
-- faithfulness-gated findings — so a claim logged before a judge swap resolves
-- after it. That is exactly what happened: the grading model changed 07-30
-- 20:14Z and mean faithfulness moved +7pp, while `band_calibration_claims`
-- carried no column to tell the two populations apart (migration 0093 has
-- none). Every rate in the readout pooled across the swap.
--
-- NULLABLE ON PURPOSE, AND NEVER BACKFILLED. A claim logged before the stamp
-- existed has no honest value to carry; guessing one (say, stamping every old
-- row with the version that happened to be current) would manufacture the very
-- comparability this column exists to deny. NULL means "logged before the
-- split key existed", the aggregation excludes it from the current-stamp
-- population, and the finding says how many it excluded.
--
-- Idempotent: IF NOT EXISTS. Data-only for existing rows (they stay NULL).

ALTER TABLE band_calibration_claims
    ADD COLUMN IF NOT EXISTS judge_pipeline_version TEXT;

COMMENT ON COLUMN band_calibration_claims.judge_pipeline_version IS
    'verify.JUDGE_PIPELINE_VERSION current when the claim was LOGGED. NULL = '
    'logged before the split key existed (never backfilled — a guessed stamp '
    'would fabricate cross-swap comparability). Aggregation filters to the '
    'current stamp and reports what it excluded.';

-- The aggregation filters on (transition_at, judge_pipeline_version).
CREATE INDEX IF NOT EXISTS idx_band_calibration_claims_pipeline_version
    ON band_calibration_claims (judge_pipeline_version, transition_at);
