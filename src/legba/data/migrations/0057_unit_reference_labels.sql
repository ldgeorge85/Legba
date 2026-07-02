-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0057_unit_reference_labels.sql
--
-- P2-T4 — the per-bounded-unit GOLD / reference-set substrate. Phase 2 measures
-- each small reasoning unit individually; per-unit CORRECTNESS ("is the unit's
-- read RIGHT?") needs a labeled reference answer to score against, separate from
-- faithfulness (P0-T2, "is the prose faithful to its cites?"). This table is that
-- labeled set: one row = one (unit, target) gold answer, GROUNDED to the
-- provenance it was drawn from (`canonical_source_ids`) so a label is anchored to
-- real substrate rows rather than free opinion.
--
-- NET-NEW (no prior gold/reference-set table exists). The labels API
-- (registry/labels_api.py) writes + reads this table; a downstream correctness
-- scorer (later P2 task) joins a unit's live output against the gold row.
--
-- WHAT:
--   * `unit_reference_labels` — id (uuid pk), unit_analyst_id (which bounded
--     unit), target_id (the assessed target; NULL for a meta / non-target unit),
--     reference_answer (the gold text), canonical_source_ids (uuid[] — the
--     substrate rows that ground the label), labeled_by (the labeler / principal),
--     created_at. A (unit_analyst_id, target_id) index backs the scorer's
--     per-unit, per-target lookup (and supports >=10 labels for one unit).
--
-- SAFETY (idempotent, additive, forward-only — no data rewrite, no data repair):
--   `CREATE TABLE IF NOT EXISTS` / `CREATE INDEX IF NOT EXISTS` are no-ops on
--   re-apply and on a fresh cold-start substrate. The runner wraps this file in
--   its own transaction and records it in `legba_data_migrations` (no inline
--   BEGIN/COMMIT here — same as 0054/0055/0056). CREATE-only/clean-slate policy
--   honored (no data migration).

CREATE TABLE IF NOT EXISTS public.unit_reference_labels (
    id                   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    unit_analyst_id      text NOT NULL,                 -- which bounded reasoning unit
    target_id            text,                          -- assessed target (NULL for a meta unit)
    reference_answer     text NOT NULL,                 -- the gold answer the unit's read is scored against
    canonical_source_ids uuid[] NOT NULL DEFAULT '{}',  -- substrate rows that GROUND the label (provenance, not opinion)
    labeled_by           text,                          -- the labeler / principal that recorded it
    created_at           timestamptz NOT NULL DEFAULT now()
);

-- Backs the correctness scorer's per-unit, per-target gold lookup (and the
-- ">=10 labels for one unit" read).
CREATE INDEX IF NOT EXISTS idx_unit_reference_labels_unit_target
    ON public.unit_reference_labels (unit_analyst_id, target_id);
