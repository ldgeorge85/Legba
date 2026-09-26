-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0216_access_class.sql
--
-- Wave-E (2026-09-24) — the ACCESS-CLASS mechanism, signals half. Every
-- source now carries a WHO-may-read verdict (`SourceScope.access_class`,
-- `legba.data.provenance.access.ACCESS_CLASSES`), fail-closed default
-- "restricted"; this migration gives every `signals` row the same column,
-- stamped at ingest by `runtime/source_actor.py:write_canonical_signal`
-- from the writing source's descriptor head, and backfills existing rows
-- from `source_descriptors` for the same reason migration 0209 backfilled
-- `origin_class` from what was already on the row rather than a guess.
--
-- MECHANISM, NOT ENFORCEMENT (SEAMS #59). Nothing reads this column to
-- refuse a request yet — the read routes accept an opt-in `access_class_in`
-- filter (data/registry/substrate_reads_api.py) that is applied only when a
-- caller passes it; absent, every existing read is byte-identical. The
-- readers' inheritance rule ("a fact/finding/event is as restricted as the
-- most restricted signal it derives from") is rendered as
-- `access_ceiling_sql()` in `data/provenance/access.py` for the enforcement
-- side to wire in later — this migration does not touch `facts` or
-- `analyst_outputs` at all; the ceiling is computed on read, not stored.
--
-- WHY "restricted", NOT "public", IS THE COLUMN DEFAULT: the Python schema
-- default (`SourceScope.access_class`) is fail-closed for exactly the same
-- reason the taxonomy note in `tests/data_pkg/test_access_class_taxonomy.py`
-- states for the ~103 committed descriptors it could not confidently
-- classify — an unclassified source reads as UNREVIEWED, not as cleared for
-- open reading. The classified value ("public" for the large majority of
-- open no-auth feeds, "licensed_noncommercial" for the four curated datasets
-- carrying real reuse terms, "restricted" for the never-bound generic
-- adapters) only reaches a row through an explicit descriptor re-register —
-- this migration alone does not widen anything past the fail-closed default.
--
-- MEASURED AT REVIEW (2026-09-24 baseline, the live tower, via the main
-- checkout's `docker compose exec postgres psql`):
--   signals = 292,958 · source_descriptors (is_head) = 139
-- The backfill UPDATE below joins all 292,958 rows against the head
-- descriptor table; QUOTED AS A QUERY TO TIME per the wave-E review rules —
-- none of the live heads carry `scope.access_class` yet (the field is new;
-- no descriptor has been re-registered with it), so every row lands at the
-- column default 'restricted' today. The join still runs a full backfill
-- pass to be correct the day a re-register lands a real value, rather than
-- leaving this migration silently unable to see one.
--
-- The runner wraps this file in its own transaction and records it in
-- `legba_data_migrations` (no inline BEGIN/COMMIT — same as 0204/0206/0207/
-- 0209).

ALTER TABLE public.signals
    ADD COLUMN IF NOT EXISTS access_class text NOT NULL DEFAULT 'restricted';

COMMENT ON COLUMN public.signals.access_class IS
    'Wave-E: closed five-class WHO-may-read vocabulary (public·'
    'licensed_commercial·licensed_noncommercial·restricted·internal), '
    'stamped at ingest from the writing source''s scope.access_class '
    '(fail-closed default ''restricted''). Mechanism only — see SEAMS #59 '
    'and data/provenance/access.py. Orthogonal to origin_class (WHERE a row '
    'came from) and payload.license_class (what may be KEPT of it).';

-- ── the backfill (292,958 rows measured 2026-09-24; every head descriptor's
--    scope.access_class is unset today, so this is a correctness pass for
--    the day one is set, not a live reclassification — see header) ─────────

UPDATE public.signals s
   SET access_class = COALESCE(sd.body->'scope'->>'access_class', 'restricted')
  FROM public.source_descriptors sd
 WHERE sd.descriptor_id = s.source_id
   AND sd.is_head
   AND sd.body->'scope'->>'access_class' IS NOT NULL;

-- ── the constraint, AFTER the backfill so it validates the final state ──────

ALTER TABLE public.signals
    ADD CONSTRAINT signals_access_class_vocab
        CHECK (access_class IN ('public', 'licensed_commercial',
                                 'licensed_noncommercial', 'restricted',
                                 'internal'));
