-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0235_external_grades_search_health.sql
--
-- `external_grades.search_health` — the per-engine health of the search that
-- decided the grade.
--
-- WHY
-- ---
-- A grade row recorded only the provider's boolean `search_degraded`. Every
-- SearXNG search in the 10-05 → 10-08 runtime log reported at least one
-- unresponsive engine, so the boolean was true on every NOT_FOUND since 10-02
-- and said nothing about HOW degraded the search was — which engines answered, which failed and why,
-- how many results came back, how long it took. The typed-absence route needs
-- that to decide between `searched_found_nothing` and `search_failed`
-- (`legba.data.registry.search_health.read_search_health`), its proof needs it
-- to name the engines, and the search-health read needs it to publish a
-- per-engine error share.
--
-- The column holds the `web_search` tool's health block verbatim
-- (`search_health.build_health_block`, version `search-health/1`). `{}` means
-- "not recorded" — a row written before this migration, or by a path that had
-- no search — and every reader treats it as exactly that, never as healthy.
--
-- SAFETY
-- ------
-- Additive. `ADD COLUMN ... DEFAULT '{}'::jsonb NOT NULL` with a constant
-- default is a catalog-only change on Postgres >= 11: no table rewrite, and
-- the append-only trigger pair from 0190 (BEFORE UPDATE / BEFORE DELETE) does
-- not fire. No backfill — the past rows keep `{}` and are read by the
-- provider's own flag. Re-apply is a no-op (IF NOT EXISTS); routed through the
-- runner (no inline BEGIN/COMMIT).

ALTER TABLE public.external_grades
    ADD COLUMN IF NOT EXISTS search_health jsonb NOT NULL DEFAULT '{}'::jsonb;

COMMENT ON COLUMN public.external_grades.search_health IS
    'The web_search health block of the search that decided this grade '
    '(engines answered/errored, result count, elapsed; mig 0235). {} = not '
    'recorded, never healthy.';
