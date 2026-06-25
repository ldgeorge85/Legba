-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0043_ingestion_conf1_backfill.sql
--
-- F2 — down-rank the historical conf-1.0 ingestion poison (backlog only).
--
-- WHY:
--   REBEL stamps a synthetic confidence=1.0 on every extracted triple
--   (historical: the relation backend was REBEL when this backfill ran; it is
--   GLiREL now, which emits real per-relation scores). The
--   ingestion confidence resolver (fact_extractor.py::_resolve_ingestion_
--   confidence) ALREADY collapses that to the 0.75 ingestion fallback on the
--   default 'relation' backend — verified live: ZERO new conf-1.0 ingestion
--   rows since 2026-06-18 00:15 despite active ingestion (294 facts on 06-19).
--   So the WRITE gate is closed; what remains is a pre-fix backlog of conf-1.0
--   open ingestion rows (344 live at authoring) that still sit at the TOP of
--   every "ORDER BY confidence DESC" consumer (ACH evidence weighting,
--   effective_confidence=min(conf,critic), the grounding tiebreak, the consult
--   read surface) above the 0.75 floor — i.e. machine-extracted NER triples
--   outranking real curated/seed facts. This down-ranks them to the same 0.75
--   fallback a fresh ingest would get, so they stop masquerading as
--   high-confidence.
--
-- WHAT:
--   Down-rank (NOT close) the open ingestion rows still at the synthetic 1.0 to
--   the 0.75 ingestion fallback. Down-rank, not delete/close, so the row
--   survives as evidence and the change is reversible; the Phase-5a grounding
--   provenance gate already EXCLUDES ingestion from the ground-truth preamble,
--   so this only affects confidence-ordered consumers.
--
-- SAFETY (idempotent, reversible, no schema change):
--   Touches ONLY open (valid_until IS NULL AND superseded_by IS NULL)
--   source_type='ingestion' rows still at exactly 1.0; a re-apply matches 0 rows
--   (they are now 0.75). No row deleted/closed, no column added. This is a
--   data-repair migration (allowed: dev-test data only, no prod — and the value
--   is the same the live write path now assigns).

UPDATE public.facts
   SET confidence = 0.75
 WHERE source_type = 'ingestion'
   AND confidence = 1.0
   AND valid_until IS NULL
   AND superseded_by IS NULL;
