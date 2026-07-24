-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0085_signal_reenriched_marker.sql
--
-- Adds the forward-progress marker + partial scan index for the new
-- `reenrich_ner` deterministic sub-handler — a ONE-TIME backfill sweep that
-- re-runs the LIVE multilingual NER (translate-then-NER + telegram payload.text)
-- over the historical backlog of signals that were ingested BEFORE the
-- NERMultilingualHandler M11/M12 fix landed and therefore carry 0 entities.
--
-- The FORWARD fix is already live (src/legba/data/filters/ner.py) — every NEW
-- signal is enriched correctly at ingest. This sweep drains the ~9,143
-- already-persisted signals the forward fix cannot reach (it only runs at
-- ingest), mirroring the signal_summarizer (0081) / corpus_indexer (0082) /
-- signal_embedder (0084) async-sweep pattern.
--
-- signals.reenriched_at (nullable timestamptz):
--   Stamped now() on EVERY row the sweep examines (re-enriched, no-entities, or a
--   per-signal NER failure), mirroring signals.summarized_at (0081) /
--   signals.entities_resolved_at (baseline) stamp-all-examined idempotency so the
--   sweep is idempotent + forward-progressing — a row that gains no entities drains
--   out of the partial index cheaply and is never re-scanned, and a poison row that
--   fails the hosted NER (on an otherwise-healthy tick) is stamped (not retried
--   forever). Rows that GAIN entities additionally get entities_resolved_at reset
--   to NULL by the sweep so the existing `entity_resolution` sweep re-folds their
--   new entities into entity_profiles.
--
-- idx_signals_needs_reenrich (partial, on fetched_at):
--   The newest-first scan predicate of the sweep — WHERE reenriched_at IS NULL AND
--   <candidate predicate> — mirrors idx_signals_unsummarized (0081) /
--   idx_signals_unembedded (0084). Keeps the per-tick SELECT index-only over the
--   shrinking un-re-enriched backlog as it drains.
--
--   Candidate predicate = a signal with NO entities that the OLD ner path could
--   not enrich: it is either a TELEGRAM signal (message body in payload.text, which
--   the pre-M12 field set never NER'd) OR a non-Latin-script language (ar/fa/he/
--   ru/uk/zh/ja/ko/hi/th/ur — the pre-M11 English-only spaCy extracted ~0 spans).
--   The language list is exactly ner.py::_NON_LATIN_TRANSLATE_LANGS (the handler's
--   default `translate_languages`); the sweep passes that same set into its SELECT,
--   so the two stay in sync in the normal (default-config) case. An operator who
--   widens translate_languages via the descriptor widens the sweep's SELECT but not
--   this index — correctness still holds (the extra rows fall back to a seq scan);
--   the index is only a drain-time optimization.
--
--   The "no entities" test is guarded (jsonb_typeof … <> 'array') so a row whose
--   payload->'entities' is absent / a JSON null / any non-array value is treated as
--   "no entities" and never trips jsonb_array_length on a non-array (verified live
--   2026-07-09: 0 rows carry a non-array entities value; all are 'array' or absent).
--
-- MEASURED (live `legba`, 2026-07-09, migration head 0084): 111,671 signals total;
--   the candidate predicate matches exactly 9,143 rows — the historical backlog this
--   sweep drains (7,164 telegram no-entity + 3,573 non-Latin-lang no-entity, 1,594
--   overlap).
--
-- IDEMPOTENT: ADD COLUMN IF NOT EXISTS + CREATE INDEX IF NOT EXISTS, so a re-run
--   against an already-migrated DB (the live dev rig) is a clean no-op. Routed
--   through the filename-gated migration runner (ONE txn + ledger per file; NO
--   inline BEGIN/COMMIT).

ALTER TABLE public.signals
    ADD COLUMN IF NOT EXISTS reenriched_at timestamp with time zone;

CREATE INDEX IF NOT EXISTS idx_signals_needs_reenrich
    ON public.signals USING btree (fetched_at)
    WHERE reenriched_at IS NULL
      AND (
            payload IS NULL
            OR NOT (payload ? 'entities')
            OR jsonb_typeof(payload->'entities') <> 'array'
            OR jsonb_array_length(payload->'entities') = 0
          )
      AND (
            source_id ILIKE '%telegram%'
            OR lower(payload->>'language') IN (
                 'ar', 'fa', 'he', 'ru', 'uk', 'zh', 'ja', 'ko', 'hi', 'th', 'ur'
               )
          );
