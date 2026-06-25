-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0041_situations_valid_from_repair.sql
--
-- Phase 5a follow-up (adversarial-review M1 + the dead-index fix). Repairs the
-- inverted valid_from backfill from 0040 and re-cuts the grounding read index.
--
-- WHY:
--   1. 0040 backfilled situations.valid_from = created_at as a proxy for "when
--      the frame began". created_at is the situation ROW's materialization time,
--      which — after the country_assessor churn history — POSTDATES the member
--      findings. Verified live: all 20 rows had valid_from LATER than the true
--      earliest member produced_at, and all 6 CLOSED frames were INVERTED
--      (valid_from > valid_until). The correct start is min(member produced_at),
--      recoverable by joining situations.data->'member_finding_ids' to
--      analyst_outputs.produced_at.
--   2. 0040's idx_situations_active_grounding is partial on status='active', but
--      the grounding read (resolve_situations) selects status <> 'closed' (active
--      OR dormant). A partial index can only serve a query whose WHERE implies
--      the index predicate, and status<>'closed' does NOT imply status='active'
--      (live: 0 active rows, 14 dormant) — so the index can never cover the read.
--      Re-cut it to match the read.
--
-- SAFETY (idempotent, additive, self-correcting):
--   The UPDATE uses LEAST(valid_from, true_start) so it only ever moves valid_from
--   EARLIER — a correct value is never harmed and a re-apply is a no-op (the true
--   start is stable). Situations whose members are unresolvable (empty/missing
--   member list) are simply skipped (no min). The index DROP/CREATE is guarded
--   (IF EXISTS / IF NOT EXISTS). No column is added/dropped; no row is deleted.

UPDATE public.situations s
   SET valid_from = LEAST(s.valid_from, sub.min_produced)
  FROM (
    SELECT s2.id, min(ao.produced_at) AS min_produced
      FROM public.situations s2,
           jsonb_array_elements_text(
               COALESCE(s2.data->'member_finding_ids', '[]'::jsonb)) AS m
      JOIN public.analyst_outputs ao ON ao.id::text = m
     GROUP BY s2.id
  ) sub
 WHERE s.id = sub.id
   AND sub.min_produced IS NOT NULL
   AND s.valid_from > sub.min_produced;

-- The 0040 index never matched the read (status='active' only); replace it with
-- one whose immutable predicate matches resolve_situations (the now()-based
-- valid_until clause stays a residual filter, not an index predicate).
DROP INDEX IF EXISTS idx_situations_active_grounding;
CREATE INDEX IF NOT EXISTS idx_situations_open_grounding
    ON public.situations (intensity_score DESC)
    WHERE status <> 'closed' AND superseded_by IS NULL;
