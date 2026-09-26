-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0211_method_versions.sql
--
-- H12 — a method/scale version on every instrument's ledger rows. The
-- instruments that publish numbers off their own tables (rather than
-- `analyst_outputs.data` or `situations.data`, which carry the stamp inside
-- the JSONB and need no column) gain a `method_version` column:
--
--   * `acute_forecasts`   — the sealed-forecast ledger. Its rows already carry
--     `method` ('recent_rate_poisson_shrunk') but never a REVISION of it; the
--     column stamps `forecast_acute.METHOD_VERSION` at mint.
--   * `band_calibration_claims` — one row per logged band transition; stamps
--     `band_calibration_tracker.METHOD_VERSION` (the horizons / outcome
--     vocabulary / resolution spec the claim was logged under).
--   * `grader_calibrations` — the correctness gate's ledger; stamps
--     `_correctness_calibration.METHOD_VERSION` (the draw + coverage rule the
--     gate ran under) at regate/seed time.
--
-- All three are NULLABLE and un-backfilled: NULL on a pre-existing row is the
-- honest stamp for "written before the instrument carried a version" — a
-- guessed retro label would read as a revision that was never declared. The
-- ANALYSIS.md method-versions table records the era boundary instead.
--
-- WHAT (idempotent — ADD COLUMN IF NOT EXISTS only; additive, no data move).

ALTER TABLE public.acute_forecasts
    ADD COLUMN IF NOT EXISTS method_version TEXT;

ALTER TABLE public.band_calibration_claims
    ADD COLUMN IF NOT EXISTS method_version TEXT;

ALTER TABLE public.grader_calibrations
    ADD COLUMN IF NOT EXISTS method_version TEXT;
