-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0219_scale_versions.sql
--
-- K3 — a SCALE version beside H12's method version on every instrument ledger.
--
-- The two answer different questions and change on different days.
-- `method_version` (0211) says whether the same CODE produced two numbers;
-- `scale_version` says whether the two numbers MEAN the same thing. A method
-- can be re-cut without moving the scale — and a scale can move without any
-- code changing at all, which is exactly what migration 0188 did to
-- `intensity_score` in August 2026 and why a reader comparing an intensity of
-- 59 today with 59 in July was comparing nothing.
--
-- A scale is named for the QUANTITY, not the module (`intensity/2026-08`,
-- `acute_probability/2026-07`), because a scale outlives the handler that
-- publishes onto it and two instruments may publish onto one scale.
--
-- The instruments that publish off `analyst_outputs.data` / `situations.data`
-- carry the stamp inside the JSONB and need no column (0211's reasoning,
-- unchanged). These three publish off their own tables:
--
--   * `acute_forecasts`   — stamps `forecast_acute.SCALE_VERSION` at mint:
--     `p` / `p_base` live in the clamped OPEN interval
--     (P_EPSILON, 1-P_EPSILON). The clamp is what makes them a scale; the 19
--     pre-clamp degenerate rows 0075 voided are not readings on it.
--   * `band_calibration_claims` — stamps
--     `band_calibration_tracker.SCALE_VERSION`: the scorecard BAND LADDER a
--     claim is a transition on. A persistence rate may only be pooled across
--     claims that share it.
--   * `desk_baselines` — stamps BOTH (this sidecar carried neither):
--     `desk_baseline.METHOD_VERSION` and `desk_baseline.SCALE_VERSION`, the
--     latter being the 24h-bucket count the band and the sigma-distance are
--     measured in.
--
-- All four columns are NULLABLE and UN-BACKFILLED: NULL on a pre-existing row
-- is the honest stamp for "written before the instrument carried a version".
-- A guessed retro label would read as a declared scale that never existed —
-- and for `acute_forecasts` in particular a backfill would assert that the
-- voided pre-clamp batch was on the current probability scale, which is the
-- precise falsehood 0075 was written to prevent. docs/ANALYSIS.md §10.9
-- records the era boundaries instead, and the reader surface renders an
-- unstamped row as "unstamped (pre-2026-09)", never as a version.
--
-- WHAT (idempotent — ADD COLUMN IF NOT EXISTS only; additive, no data move).

ALTER TABLE public.acute_forecasts
    ADD COLUMN IF NOT EXISTS scale_version TEXT;

ALTER TABLE public.band_calibration_claims
    ADD COLUMN IF NOT EXISTS scale_version TEXT;

ALTER TABLE public.desk_baselines
    ADD COLUMN IF NOT EXISTS method_version TEXT;

ALTER TABLE public.desk_baselines
    ADD COLUMN IF NOT EXISTS scale_version TEXT;
