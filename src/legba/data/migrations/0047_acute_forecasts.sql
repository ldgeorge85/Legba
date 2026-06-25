-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0047_acute_forecasts.sql
--
-- R1-T1.3 (#92) — a dedicated, pre-registered binary-forecast pilot table so the
-- project can EARN BACK the word "forecast" with an exogenously-resolved Brier /
-- Brier-skill score, kept fully ISOLATED from the live findings feed and the
-- existing CI-coverage prediction resolver.
--
-- WHY a dedicated table (not a `kind='prediction'` analyst_outputs row):
--   The pilot's whole value is RIGOR. Storing it in analyst_outputs would (a)
--   pour 19 low-information p=0.07 rows/week into the operator's findings feed,
--   and (b) entangle it with `_resolve_open_predictions` (the CI-coverage
--   resolver) and the pooled calibration pull. A separate table makes the pilot's
--   target / event-class / horizon / probability / resolution-rule pinned and
--   independent, and lets calibration report `brier_forecast_acute` as a DISTINCT
--   key that is NEVER pooled into the honest headline Brier.
--
-- THE TASK (one narrow, falsifiable call):
--   For each G20 country C, on a weekly cadence, emit p = P(>=1 ACUTE event of
--   class K occurs in C during the FORWARD 7-day window [window_start,
--   window_end)). K = `hazard_severe` = the curated hazard catalogs the ingest
--   layer already severity-filters (USGS significant quakes / NWS severe+extreme
--   alerts / NASA EONET events). Resolve o EXOGENOUSLY by counting class-K events
--   in that exact window by the UPSTREAM source's own event timestamp (quake
--   origin time / alert onset / event date) — never the forecaster's thesis text,
--   never fetched_at. Score (p - o)^2; report Brier + Brier skill score vs the
--   per-country climatological base rate p_base. The project earns "forecast"
--   only when BSS > 0 on the pilot.
--
-- WHAT (idempotent — CREATE ... IF NOT EXISTS only; additive, no data migration).

CREATE TABLE IF NOT EXISTS public.acute_forecasts (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    region           TEXT        NOT NULL,            -- G20 target descriptor id (e.g. country_g20_us)
    event_class      TEXT        NOT NULL,            -- frozen class-K id, e.g. 'hazard_severe'
    window_start     TIMESTAMPTZ NOT NULL,            -- forward 7d window, fully in the future at issue time
    window_end       TIMESTAMPTZ NOT NULL,
    p                DOUBLE PRECISION NOT NULL,       -- model P(>=1 event) — the claimed probability scored by Brier
    p_base           DOUBLE PRECISION NOT NULL,       -- climatology base rate (for the skill-score denominator)
    method           TEXT        NOT NULL,            -- 'recent_rate_poisson' | 'arima_poisson'
    lambda_model     DOUBLE PRECISION,                -- expected class-K count over the window (diagnostic)
    issued_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- Resolution (stamped EXOGENOUSLY once the window has closed + settled):
    resolved_outcome INTEGER,                         -- 0/1, NULL until resolved
    actual_value     INTEGER,                         -- realized class-K event count in the window
    resolved_by      TEXT,                            -- 'forecast_acute_exogenous' | 'operator:<id>'
    resolved_at      TIMESTAMPTZ
);

-- Idempotent issuance: at most ONE forecast per (country, class, week). The
-- weekly producer re-attempts each daily tick; the first insert for a given
-- window_start pins p (computed from data BEFORE the window) and every later
-- attempt is a no-op via this conflict target.
CREATE UNIQUE INDEX IF NOT EXISTS acute_forecasts_region_class_window_uq
    ON public.acute_forecasts (region, event_class, window_start);

-- The resolver scans for closed-but-unresolved windows; the calibration pull
-- scans for resolved rows. Partial index keeps both cheap as the table grows.
CREATE INDEX IF NOT EXISTS acute_forecasts_open_window_idx
    ON public.acute_forecasts (window_end)
    WHERE resolved_outcome IS NULL;

CREATE INDEX IF NOT EXISTS acute_forecasts_resolved_idx
    ON public.acute_forecasts (resolved_at)
    WHERE resolved_outcome IS NOT NULL;
