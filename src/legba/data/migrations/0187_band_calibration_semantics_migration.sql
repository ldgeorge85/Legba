-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0187_band_calibration_semantics_migration.sql
--
-- H3-GUARD — the semantics-mismatch guard's calibration-side flag. The H3
-- banding train (damper retired + basis alignment) legitimately moves ~30
-- bands fleet-wide on its first post-deploy sweep, and every one of those
-- moves straddles a BANDING_SEMANTICS / DAMPING_SEMANTICS change (H3 adds
-- `damping_semantics`, so every pre-H3 scorecard lacks it while every
-- post-H3 one carries `"off"`). Logging those as ordinary
-- deterioration/improvement claims would silently pollute the persistence /
-- reversal rates with pairs that were never comparable on the ladder in the
-- first place — the two cards disagree about what the band even MEANS, so
-- "did the band hold" is not a question this table can honestly answer for
-- them.
--
-- `semantics_migration` marks exactly those claims. They are still LOGGED
-- (never silently dropped — an operator can see the transition happened and
-- why it was excluded) and still carry a `direction` of
-- `'semantics-migration'` (never `'deterioration'`/`'improvement'` — no CHECK
-- constrains `direction`, see 0093), but the aggregation query
-- (`band_calibration_tracker._AGG_SQL` and friends) filters
-- `WHERE NOT semantics_migration`, so they can NEVER enter
-- `summarize_claims`'s `overall` / `by_direction` / `by_dimension` blocks —
-- the exclusion is a query predicate, not a cosmetic label nobody reads.
--
-- NOT NULL DEFAULT FALSE, so every existing row (all logged before this guard
-- existed, none of them semantics-migration claims) reads FALSE with no
-- backfill needed — an honest default, not a guess.
--
-- Idempotent: IF NOT EXISTS. Additive only; no existing row is touched.

ALTER TABLE band_calibration_claims
    ADD COLUMN IF NOT EXISTS semantics_migration BOOLEAN NOT NULL DEFAULT FALSE;

COMMENT ON COLUMN band_calibration_claims.semantics_migration IS
    'H3-GUARD: TRUE when this transitions prior and current scorecard were '
    'computed under different banding_semantics/damping_semantics stamps '
    '(scorecard_banding.semantics_changed). direction reads '
    'semantics-migration, never deterioration/improvement, and the '
    'aggregation query excludes these rows outright — never pooled into any '
    'persistence/reversal rate.';

-- The exclusion-count readout (population.excluded_semantics_migration) and
-- the current-population pull both filter on this column over the lookback
-- window — a partial index over the TRUE rows keeps that filter cheap as the
-- table grows (the judge_pipeline_version partial-index precedent, 0122).
CREATE INDEX IF NOT EXISTS idx_band_calibration_claims_semantics_migration
    ON band_calibration_claims (transition_at)
    WHERE semantics_migration;
