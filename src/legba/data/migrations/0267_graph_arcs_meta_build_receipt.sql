-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0267_graph_arcs_meta_build_receipt.sql
--
-- P-7 (2026-11-14) — the graph projection's rebuild made cheap. The
-- projector (`deterministic_handlers/graph_projector.py`, version
-- 2026-11/p7) now builds `graph_arcs` UNLOGGED, indexes after the load,
-- leaves the citation-lineage copies and the `contends_with` arcs out by
-- default, and SKIPS a tick whose inputs have not changed. The one-row build
-- receipt grows the columns that make those behaviours visible:
--
--   checked_at        the last tick that proved the projection current — a
--                     build OR an unchanged-input skip. Readers measure
--                     freshness from the newer of projected_at / checked_at.
--   input_watermark   the change detector the skip compares against (write
--                     counters + relfilenode per source table, the resolver
--                     functions' source, the projector version, its options).
--   last_outcome      'built' | 'skipped_unchanged'.
--   excluded_sources  src_table values switched off by option; the S-1 gauge
--                     does not call them a missing leg.
--   persistence       'unlogged' | 'logged' — the serving table's journaling.
--
-- Additive, nullable, no rewrite: ADD COLUMN IF NOT EXISTS on a one-row
-- table. `graph_arcs` itself is NOT altered here — the next build creates the
-- unlogged table and swaps it in (an ALTER ... SET UNLOGGED would rewrite
-- 3.2 GB under an exclusive lock for nothing).
--
-- The runner wraps this file in its own transaction and records it in
-- `legba_data_migrations` (no inline BEGIN/COMMIT).

ALTER TABLE public.graph_arcs_meta
    ADD COLUMN IF NOT EXISTS checked_at       timestamptz,
    ADD COLUMN IF NOT EXISTS input_watermark  jsonb,
    ADD COLUMN IF NOT EXISTS last_outcome     text,
    ADD COLUMN IF NOT EXISTS excluded_sources jsonb NOT NULL DEFAULT '[]',
    ADD COLUMN IF NOT EXISTS persistence      text;

COMMENT ON COLUMN public.graph_arcs_meta.checked_at IS
    'P-7: last tick that proved the projection current (build or '
    'unchanged-input skip); freshness = greatest(projected_at, checked_at).';
COMMENT ON COLUMN public.graph_arcs_meta.input_watermark IS
    'P-7: the skip rule''s change detector over every table the enabled '
    'sources read.';
COMMENT ON COLUMN public.graph_arcs_meta.excluded_sources IS
    'P-7: src_table values off by projector option (never a missing leg).';
