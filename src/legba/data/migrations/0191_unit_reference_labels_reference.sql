-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0191_unit_reference_labels_reference.sql
--
-- A-1 (ATTENTION_MEASUREMENT_DESIGN_2026-09-05 §1.3) — the out-of-plane daily
-- desk reference, stored on the table migration 0057 built for exactly this and
-- that has held ONE row (for a retired analyst, with zero canonical_source_ids)
-- for its whole life.
--
-- REUSE-BEFORE-CREATE. 0057's own comment reads: "one row = one (unit, target)
-- gold answer, GROUNDED to the provenance it was drawn from". That IS the
-- reference row: a dated, URL-bearing answer to one bounded unit's own question
-- for one country, whose `canonical_source_ids` the collection diff fills in
-- with the signal ids it matched. No new table.
--
-- WHAT THIS ADDS (additive columns only; no data rewrite, no data repair):
--
--   window_start   timestamptz  -- the 24h the reference covers (its UTC day)
--   window_end     timestamptz
--   items          jsonb NOT NULL '[]'
--                  -- [{ordinal, headline, sentence, entities[], entity_folds[],
--                  --   urls[], published_at, materiality}]
--   status         text  NOT NULL 'ok'
--                  -- ok | nothing_material | unverified_liveness | degraded
--   provenance     jsonb NOT NULL '{}'
--                  -- {query, provider, engine_mix, n_results, control_probe,
--                  --  model, pipeline_version, items_url_fenced}
--
-- WHY THE DEFAULTS MATTER. Every column is nullable or defaulted, so the ONE
-- pre-existing operator row is untouched and still reads exactly as it did:
-- `window_start IS NULL` is what marks a row as NOT machine-authored at the
-- schema layer, and the partial unique index below is scoped to precisely that
-- predicate so an operator can still write as many rows per (unit, target) as
-- the labelling loop wants.
--
-- THE FIRE-ONCE INDEX. `uq_unit_reference_labels_machine_day` makes "one
-- reference per (target x bounded unit x UTC day)" — the design's D-a grain —
-- a SCHEMA fact rather than a handler convention. A re-run inside the same day
-- collides and is skipped rather than doubling the population a mean is taken
-- over. It is PARTIAL on `window_start IS NOT NULL`, so it constrains only the
-- machine-authored rows and never the operator's.
--
-- F-2 IS NOT IN THIS FILE, AND MUST NOT BE. Writing rows here would revive
-- `unit_correctness_scorer`'s dead `correctness_vs_reference` axis as a
-- MACHINE-authored number under the OPERATOR label key. The gate for that is a
-- WHERE clause on `labeled_by` in the scorer's own read (and in
-- `correctness_axis.UNIT_LABELS_SQL`), landed in the same commit as this
-- migration. A schema constraint could not express it: the two label
-- populations legitimately share the table, they must simply never be pooled.
--
-- SAFETY (idempotent, additive, forward-only): `ADD COLUMN IF NOT EXISTS` /
-- `CREATE INDEX IF NOT EXISTS` are no-ops on re-apply and on a fresh
-- cold-start substrate. The runner wraps this file in its own transaction and
-- records it in `legba_data_migrations` (no inline BEGIN/COMMIT here).

ALTER TABLE public.unit_reference_labels
    ADD COLUMN IF NOT EXISTS window_start timestamptz,
    ADD COLUMN IF NOT EXISTS window_end   timestamptz,
    ADD COLUMN IF NOT EXISTS items        jsonb NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN IF NOT EXISTS status       text  NOT NULL DEFAULT 'ok',
    ADD COLUMN IF NOT EXISTS provenance   jsonb NOT NULL DEFAULT '{}'::jsonb;

COMMENT ON COLUMN public.unit_reference_labels.window_start IS
    'A-1: start of the 24h the reference covers. NULL on an operator-authored '
    'label — that NULL is what scopes the machine-row unique index below.';
COMMENT ON COLUMN public.unit_reference_labels.items IS
    'A-1: the <=5 reference items, each {ordinal, headline, sentence, '
    'entities[], entity_folds[], urls[], published_at, materiality}. Every url '
    'came from the search result set the handler passed the model (the URL '
    'fence); an item whose urls did not is dropped and counted, never repaired.';
COMMENT ON COLUMN public.unit_reference_labels.status IS
    'A-1 liveness ladder: ok | nothing_material | unverified_liveness | '
    'degraded. Both attention diffs DECLINE (None, never 0.0) for any row that '
    'is not ok or nothing_material — a search outage must never read as a '
    'quiet world.';
COMMENT ON COLUMN public.unit_reference_labels.provenance IS
    'A-1: {query, provider, provider_route, engine_mix, n_results, '
    'control_probe, model, pipeline_version, items_url_fenced}. The rung '
    'identity is carried so a metric shift can be attributed to a provider '
    'change rather than to the world (design F-6 ii).';

-- ONE reference per (unit, target, UTC day) — the design's D-a grain, enforced.
-- PARTIAL so it binds only machine-authored rows (window_start IS NOT NULL) and
-- leaves the operator labelling loop free to write as many rows as it likes.
CREATE UNIQUE INDEX IF NOT EXISTS uq_unit_reference_labels_machine_day
    ON public.unit_reference_labels (unit_analyst_id, target_id, window_start)
    WHERE window_start IS NOT NULL;

-- The diffs read the day's rows back by window and by labeler prefix (the F-2
-- population split). Neither the 0057 index nor the unique index above serves
-- that scan, because it does not lead with unit_analyst_id.
CREATE INDEX IF NOT EXISTS idx_unit_reference_labels_window
    ON public.unit_reference_labels (window_start DESC, labeled_by)
    WHERE window_start IS NOT NULL;
