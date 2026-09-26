-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0209_origin_class.sql
--
-- DATA MODEL V3 / P7 — the ORIGIN-CLASS columns and the reader firewall
-- (plan §2 P7; NEXT_ARC_CAPTURE_2026-09-23 §B5).
--
-- Every substrate row now says WHERE it came from in one closed vocabulary,
-- and the live gate refuses the classes that are history. The open-row gate
-- is `superseded_by IS NULL AND valid_until IS NULL` (migration 0032): a
-- backfilled 2016 fact with an open `valid_until` reads as *current today*
-- and would silently feed freshness, source health, calibration, salience
-- and the alert plane. The firewall therefore has to be the origin class,
-- not the validity window — and it has to exist BEFORE a single historical
-- row does. Program 7's collections machinery (the loader, the manifests,
-- the observations table) comes later; this file is only the columns, the
-- measured backfill, and the constraint that makes the meantime safe.
--
-- THE VOCABULARY (closed — a CHECK, not an enum type):
--   live · web_retrieval · seed · backfill_native · backfill_reconstructed · archive
-- `LIVE_CLASSES = {live, web_retrieval, seed}` is what every reader sees
-- today. The three history classes are refused AT THE TABLE until Program
-- 7's reader sweep lands.
--
-- THE TWO CONSTRAINTS PER TABLE, deliberately both:
--   <table>_origin_class_vocab            — the closed six-class vocabulary;
--                                           permanent.
--   <table>_origin_class_readers_not_swept — THE SEAM (SEAMS #57). It is
--                                           dropped by the migration that
--                                           lands the reader sweep, and its
--                                           name is what a premature backfill
--                                           sees in its error.
--
-- `collection_id` is text with NO foreign key — the collections table does
-- not exist yet; the pointer is stamped now so Program 7 rows land
-- classified rather than needing a second backfill.
--
-- `facts.superseded_at` closes the asymmetry `analyst_outputs` already had
-- (decision_predicate reads `produced_at`/`superseded_at` there): the write
-- path stamps it at every supersession close, and this file backfills it for
-- the 8,654 already-superseded rows as `successor.created_at` — AN
-- APPROXIMATION, stated plainly: the true close instant is not recoverable,
-- and the successor's creation time is the nearest durable stamp.
--
-- `btree_gist` is a trusted extension; Program 7's `entity_aliases` temporal
-- keys need it and the line belongs with the columns that make those keys
-- meaningful.
--
-- MEASURED AT REVIEW (2026-09-23 baseline, the live tower — the UPDATEs
-- below touch ~1,200 rows; the column defaults are metadata-only in
-- Postgres 18):
--   facts = 154,960   of which seed_batch_id IS NOT NULL: 1,014 ·
--                     source_type='seed': 1,012 · superseded_by IS NOT NULL: 8,654
--   signals = 287,927 of which retrieval_origin LIKE 'web_search:%': 127
--   events = 985      (all written by the live clustering lane — class 'live')
--
-- The facts → web_retrieval sweep keys on `evidence_set->>'signal_id'`, the
-- citation surface the ingestion fact writer actually populates (a dict of
-- {signal_id, text_excerpt}). `derived_from` is NOT swept: it mixes signal
-- and fact lineage and cannot be read as "cites only web signals" in one
-- statement — those rows stay 'live' by default rather than guessed.
--
-- The runner wraps this file in its own transaction and records it in
-- `legba_data_migrations` (no inline BEGIN/COMMIT — same as 0204/0206/0207).

CREATE EXTENSION IF NOT EXISTS btree_gist;

-- ── the columns ────────────────────────────────────────────────────────────

ALTER TABLE public.signals
    ADD COLUMN IF NOT EXISTS origin_class  text NOT NULL DEFAULT 'live',
    ADD COLUMN IF NOT EXISTS collection_id text;

ALTER TABLE public.facts
    ADD COLUMN IF NOT EXISTS origin_class  text NOT NULL DEFAULT 'live',
    ADD COLUMN IF NOT EXISTS collection_id text,
    ADD COLUMN IF NOT EXISTS superseded_at timestamptz;

ALTER TABLE public.events
    ADD COLUMN IF NOT EXISTS origin_class  text NOT NULL DEFAULT 'live',
    ADD COLUMN IF NOT EXISTS collection_id text;

COMMENT ON COLUMN public.signals.origin_class IS
    'V3/P7: closed six-class provenance vocabulary (live·web_retrieval·seed·'
    'backfill_native·backfill_reconstructed·archive). readers_not_swept '
    'refuses the three history classes until the reader sweep lands.';
COMMENT ON COLUMN public.facts.origin_class IS
    'V3/P7: closed six-class provenance vocabulary; the live gate is '
    'live_gate_sql() in data/provenance/origin.py.';
COMMENT ON COLUMN public.facts.superseded_at IS
    'V3/P7: decision-time close stamp, stamped by the write path at every '
    'supersession; backfilled for pre-0209 rows as successor.created_at '
    '(an approximation — the true close instant is not recoverable).';
COMMENT ON COLUMN public.events.origin_class IS
    'V3/P7: closed six-class provenance vocabulary; the event writer stamps '
    '''live'' — events are minted by the live clustering lane only.';
COMMENT ON COLUMN public.facts.collection_id IS
    'V3/P7: the collection this row belongs to. NO foreign key — the '
    'collections table lands with Program 7; stamped now so those rows '
    'arrive classified.';
COMMENT ON COLUMN public.signals.collection_id IS
    'V3/P7: the collection this row belongs to. NO foreign key — the '
    'collections table lands with Program 7.';
COMMENT ON COLUMN public.events.collection_id IS
    'V3/P7: the collection this row belongs to. NO foreign key — the '
    'collections table lands with Program 7.';

-- ── the measured backfill (~1,200 rows against the 2026-09-23 baseline) ─────

-- Seed-batch facts are the 'seed' class (1,014 by seed_batch_id; the
-- source_type='seed' disjunct covers pre-0034 seed writes with no batch id).
UPDATE public.facts
   SET origin_class = 'seed'
 WHERE seed_batch_id IS NOT NULL
    OR source_type = 'seed';

-- Web-retrieved signals are the 'web_retrieval' class (127 measured).
UPDATE public.signals
   SET origin_class = 'web_retrieval'
 WHERE retrieval_origin LIKE 'web_search:%'
    OR retrieval_origin = 'web_evidence';

-- A fact whose evidence_set cites ONLY a web-retrieved signal follows its
-- evidence (the single signal_id key is the whole citation surface the
-- ingestion writer emits).
UPDATE public.facts f
   SET origin_class = 'web_retrieval'
 WHERE f.origin_class = 'live'
   AND f.evidence_set ? 'signal_id'
   AND EXISTS (
         SELECT 1 FROM public.signals s
          WHERE s.id = (f.evidence_set->>'signal_id')::uuid
            AND s.origin_class = 'web_retrieval'
       );

-- The 8,654 superseded facts get their decision-time close stamp as the
-- successor's created_at — an approximation, named as one in the header.
UPDATE public.facts f
   SET superseded_at = s.created_at
  FROM public.facts s
 WHERE f.superseded_by = s.id
   AND f.superseded_at IS NULL;

-- ── the constraints, AFTER the backfill so they validate the final state ────

ALTER TABLE public.signals
    ADD CONSTRAINT signals_origin_class_vocab
        CHECK (origin_class IN ('live','web_retrieval','seed',
                                'backfill_native','backfill_reconstructed','archive')),
    ADD CONSTRAINT signals_origin_class_readers_not_swept
        CHECK (origin_class IN ('live','web_retrieval','seed'));

ALTER TABLE public.facts
    ADD CONSTRAINT facts_origin_class_vocab
        CHECK (origin_class IN ('live','web_retrieval','seed',
                                'backfill_native','backfill_reconstructed','archive')),
    ADD CONSTRAINT facts_origin_class_readers_not_swept
        CHECK (origin_class IN ('live','web_retrieval','seed'));

ALTER TABLE public.events
    ADD CONSTRAINT events_origin_class_vocab
        CHECK (origin_class IN ('live','web_retrieval','seed',
                                'backfill_native','backfill_reconstructed','archive')),
    ADD CONSTRAINT events_origin_class_readers_not_swept
        CHECK (origin_class IN ('live','web_retrieval','seed'));
