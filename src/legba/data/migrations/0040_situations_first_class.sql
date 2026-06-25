-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0040_situations_first_class.sql
--
-- Phase 5a.1 — make SITUATIONS first-class objects (planning/PHASE5_SITUATIONS_PLAN.md).
--
-- WHY:
--   Situations today are bottom-up aggregates: situation_clustering UPSERTs one
--   row per (situation_signature, analyst_id) via its OWN ad-hoc writer, with the
--   signature buried in `data` JSONB and NO temporal-validity columns. Two
--   consequences this migration removes:
--     1. The signature is invisible to SQL joins/filters (only `data->>...`),
--        and the standard write path (`write_situation` / `_insert_situation`)
--        has NO upsert key, so it would DUPLICATE a row every run — which is
--        exactly why clustering keeps its own writer. A real
--        `situation_signature` column + a UNIQUE (signature, analyst_id) key
--        gives the standard provenance write path an upsert target (0040 → S-2)
--        AND makes "which situation owns this finding" a plain join.
--     2. A situation is a mutable SNAPSHOT, not a temporal FRAME — it has no
--        valid_from/valid_until/superseded_by like facts/nexuses, so it cannot
--        express "active over [t0, t1)". The Phase-5 goal (situations as the
--        persistent frame that durably fixes temporal-collapse + subsumes the
--        events role) needs the temporal columns.
--
-- WHAT (all on public.situations):
--   * situation_signature text          — promoted from data->>'situation_signature'
--     (backfilled below) so it is a first-class, indexable, joinable column.
--   * valid_from  timestamptz           — when the situation began (S-2 sets it
--     to the earliest member finding; backfilled here to created_at for the
--     existing rows as a conservative proxy).
--   * valid_until timestamptz           — NULL while open/active; stamped when the
--     situation closes (S-2). Mirrors facts/nexuses.
--   * superseded_by uuid                — points at a successor situation when one
--     subsumes/merges this one (S-2/5b). NULL today.
--   * UNIQUE partial index (situation_signature, analyst_id) WHERE signature NOT
--     NULL — the upsert key for the standard write path.
--   * a partial index over ACTIVE, currently-valid situations for the
--     situations-as-grounding read (S-3).
--
-- SAFETY (idempotent, additive, behavior-preserving):
--   ADD COLUMN IF NOT EXISTS + CREATE INDEX IF NOT EXISTS are no-ops on re-apply.
--   The two backfill UPDATEs are guarded to touch ONLY rows that still need it
--   (the column NULL / signature derivable), so re-running mutates nothing. No
--   existing column or index is dropped or altered. The pre-apply uniqueness of
--   (data->>'situation_signature', analyst_id) was verified live (20 rows, 20
--   distinct) so the unique index builds clean; if a future dup exists it fails
--   LOUD rather than silently coercing (CREATE-only policy).

ALTER TABLE public.situations
    ADD COLUMN IF NOT EXISTS situation_signature text,
    ADD COLUMN IF NOT EXISTS valid_from          timestamp with time zone,
    ADD COLUMN IF NOT EXISTS valid_until         timestamp with time zone,
    ADD COLUMN IF NOT EXISTS superseded_by       uuid;

-- Backfill the promoted signature column from the JSONB it has lived in. Only
-- touches rows where the column is still NULL but the JSONB carries it, so a
-- re-apply is a no-op.
UPDATE public.situations
   SET situation_signature = data->>'situation_signature'
 WHERE situation_signature IS NULL
   AND data->>'situation_signature' IS NOT NULL;

-- Backfill valid_from for existing rows to created_at (a conservative proxy for
-- "when the frame began"; S-2 sets it to the earliest member finding going
-- forward). Only touches rows where it is still NULL.
UPDATE public.situations
   SET valid_from = created_at
 WHERE valid_from IS NULL;

-- The upsert key for the standard write path: one open situation per
-- (signature, analyst_id). Partial so a (legacy) signature-less row never
-- collides. ON CONFLICT in _insert_situation targets this exact predicate.
CREATE UNIQUE INDEX IF NOT EXISTS uq_situations_signature_analyst
    ON public.situations (situation_signature, analyst_id)
    WHERE situation_signature IS NOT NULL;

-- Grounding read (S-3): the situations-as-grounding resolver pulls ACTIVE,
-- currently-valid situations ordered by intensity. Partial keeps the scan to the
-- live frames only.
CREATE INDEX IF NOT EXISTS idx_situations_active_grounding
    ON public.situations (intensity_score DESC)
    WHERE status = 'active' AND superseded_by IS NULL AND valid_until IS NULL;
