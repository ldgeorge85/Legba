-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0093_band_calibration_claims.sql
--
-- P2-3 (scorecard calibration harness) — band transitions become RESOLVABLE
-- claims, auto-resolved and scored over time by the `band_calibration_tracker`
-- deterministic analyst.
--
-- WHY a dedicated table (the acute_forecasts precedent, migration 0047):
--   The harness's whole value is RIGOR. When a desk×dimension scorecard band
--   CHANGES (the same ladder→ladder transitions P1-3's alert_trigger_scan
--   detects), the implied directional statement — "escalation band for desk X
--   moved low→elevated at T0" — is logged here as a pinned, resolvable record
--   with a HARD resolution spec: at T0+14d and T0+28d, does the then-current
--   band confirm (still at/beyond the new band), or did it revert? Resolution
--   reads ONLY later scorecard rows (deterministic, no LLM), so an outside
--   observer given (desk, dimension, T0, to_band) can recompute every outcome
--   independently. Storing these in analyst_outputs would pour bookkeeping
--   rows into the findings feed and entangle them with the prediction
--   resolvers; a separate table keeps the claim / spec / outcome pinned and
--   segregable — band-persistence rates are NEVER pooled into any Brier.
--
-- HONESTY FIELDS (mirroring acute_forecasts):
--   * outcome_14/outcome_28 stay NULL until the horizon has PASSED and the
--     deterministic resolver graded the claim — never pre-filled.
--   * resolved_by_14/resolved_by_28 name the grader
--     ('band_calibration_deterministic' | 'operator:<id>'); a 'voided:' prefix
--     withdraws a horizon from grading (the resolver skips it and never
--     overwrites it back — the P7-F6 acute_forecasts void contract).
--   * An already-resolved horizon is NEVER overwritten (operator labels win).
--   * Outcome vocabulary is CLOSED: 'held' | 'worsened' | 'improved' |
--     'reverted' | 'insufficient' | 'unresolvable'. 'insufficient' (the band
--     at horizon read insufficient-evidence) and 'unresolvable' (no later
--     scorecard row / unreadable band at the horizon) are first-class honest
--     states EXCLUDED from persistence/reversal denominators, never dropped.
--   * NO probability column exists ON PURPOSE: bands are ordinal risk
--     categories, not probabilities, so no Brier / Brier-skill claim can be
--     minted from this table.
--
-- One row per transition; identity = (desk, dimension, scorecard_row_id) — the
-- NEW scorecard row that carried the changed band — so a watermark reset or an
-- overlapping re-scan can never duplicate a claim (ON CONFLICT DO NOTHING).
--
-- band_calibration_scan_state is the tracker's OWN tiny watermark store (one
-- 'scorecard_scan' row) — deliberately NOT alert_trigger_watermarks, whose
-- state belongs to alert_trigger_scan (P2-3 reuses the comparison SHAPE only).
--
-- SAFETY (idempotent, additive, forward-only): CREATE TABLE/INDEX IF NOT
-- EXISTS only; no existing table is touched; re-apply and cold-start are both
-- no-ops. The runner wraps this file in its own transaction and records it in
-- `legba_data_migrations` (no inline BEGIN/COMMIT — same as 0091/0092).

CREATE TABLE IF NOT EXISTS public.band_calibration_claims (
    id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    desk                  TEXT        NOT NULL,  -- target descriptor id (e.g. country_g20_us)
    dimension             TEXT        NOT NULL,  -- scorecard dimension (unit analyst_id)
    from_band             TEXT        NOT NULL,  -- ladder band before the transition
    to_band               TEXT        NOT NULL,  -- ladder band the claim asserts persists
    direction             TEXT        NOT NULL,  -- 'deterioration' | 'improvement' (ladder→ladder only)
    transition_at         TIMESTAMPTZ NOT NULL,  -- T0 = produced_at of the scorecard row carrying to_band
    scorecard_row_id      UUID        NOT NULL,  -- the NEW scorecard analyst_outputs row (lineage)
    prev_scorecard_row_id UUID,                  -- the row that carried from_band
    resolution_spec       TEXT        NOT NULL DEFAULT 'hard_band_at_horizon_v1',
    horizon_14_at         TIMESTAMPTZ NOT NULL,  -- T0 + 14d
    horizon_28_at         TIMESTAMPTZ NOT NULL,  -- T0 + 28d
    logged_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- Resolution @ 14d (stamped deterministically once the horizon has passed):
    resolved_band_14      TEXT,                  -- the then-current band read at T0+14d
    outcome_14            TEXT,                  -- held|worsened|improved|reverted|insufficient|unresolvable
    resolved_by_14        TEXT,                  -- 'band_calibration_deterministic' | 'operator:<id>' | 'voided:*'
    resolved_at_14        TIMESTAMPTZ,
    -- Resolution @ 28d:
    resolved_band_28      TEXT,
    outcome_28            TEXT,
    resolved_by_28        TEXT,
    resolved_at_28        TIMESTAMPTZ
);

-- Claim identity: at most ONE claim per changed band per new scorecard row.
-- The tracker INSERTs ON CONFLICT DO NOTHING against this, so re-scans (or a
-- lost watermark) are idempotent — the "no dup per transition" contract.
CREATE UNIQUE INDEX IF NOT EXISTS band_calibration_claims_transition_uq
    ON public.band_calibration_claims (desk, dimension, scorecard_row_id);

-- The resolver scans for due-but-unresolved horizons; partial indexes keep
-- both sweeps cheap as the table grows (the acute_forecasts_open_window_idx
-- precedent).
CREATE INDEX IF NOT EXISTS band_calibration_claims_open14_idx
    ON public.band_calibration_claims (horizon_14_at)
    WHERE outcome_14 IS NULL;

CREATE INDEX IF NOT EXISTS band_calibration_claims_open28_idx
    ON public.band_calibration_claims (horizon_28_at)
    WHERE outcome_28 IS NULL;

-- The tracker's own durable scan watermark (one 'scorecard_scan' row whose
-- state fingerprints the last pair-compared scorecard produced_at). A missing
-- row = first-ever scan (bounded historical backfill; the unique index above
-- is the dedup floor either way).
CREATE TABLE IF NOT EXISTS public.band_calibration_scan_state (
    state_key  TEXT PRIMARY KEY,
    state      JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
