-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0058_critique_analyzed_output_id_index.sql
--
-- S8-T5(a) — a partial expression index backing the verify-floor / critic-score
-- LATERAL JOIN that every "fold the latest critique onto a finding" read runs.
--
-- WHY:
--   Multiple hot reads correlate a finding to its newest ``kind='critique'`` row
--   via ``cr.data->>'analyzed_output_id' = f.id::text`` inside an
--   ``INNER/LEFT JOIN LATERAL`` — the world/country composition slice
--   (meta_findings_synthesizer), the substrate-reads effective-confidence API
--   (substrate_reads_api), the GEPA eval join (dapr_workflow/gepa), the
--   scorecard banding read, and ``get_assessments`` (journal + consult). With no
--   index on the JSONB ``analyzed_output_id`` expression, each of those laterals
--   SEQ-SCANS the whole critique population per outer finding row (the 835ms
--   world read observed 2026-07-01; the scan grows with critique volume, ~264
--   new critiques/day). This partial expression index lets the planner probe the
--   matching critique rows directly.
--
--   PARTIAL (``WHERE kind = 'critique'``): only critique rows carry
--   ``analyzed_output_id``, so the predicate keeps the index small (it indexes
--   only the ~critique subset, not every analyst_outputs row) and matches the
--   join's own ``cr.kind = 'critique'`` filter so the planner can use it.
--
-- SAFETY (idempotent, additive, forward-only — no data rewrite, no data repair):
--   ``CREATE INDEX IF NOT EXISTS`` is a no-op on re-apply and on a fresh
--   cold-start substrate. The runner wraps this file in its own transaction and
--   records it in ``legba_data_migrations`` (no inline BEGIN/COMMIT here — same
--   as 0054..0057). CREATE-only / clean-slate policy honored (no data
--   migration). NOT built CONCURRENTLY: the runner runs each migration inside a
--   transaction (CONCURRENTLY cannot run in one) and this is a clean-slate /
--   dev-only substrate where a brief build-time lock is acceptable.

CREATE INDEX IF NOT EXISTS idx_analyst_outputs_critique_analyzed_output_id
    ON public.analyst_outputs ((data->>'analyzed_output_id'))
    WHERE kind = 'critique';
