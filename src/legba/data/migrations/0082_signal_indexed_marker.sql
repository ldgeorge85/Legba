-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0082_signal_indexed_marker.sql
--
-- Adds the forward-progress marker + partial scan index for the new
-- `corpus_indexer` deterministic sub-handler (an async sweep that indexes signal
-- bodies into the OpenSearch full-text corpus — the INDEX PLANE of the
-- signal-content-depth program, a lexical mining substrate over the shared
-- signal pool).
--
-- signals.indexed_at (nullable timestamptz):
--   Stamped now() on EVERY row the sweep examines (once its batch is indexed),
--   mirroring signals.summarized_at (migration 0081) / signals.entities_resolved_at
--   (baseline / migration 0029) so the sweep is idempotent + forward-progressing —
--   examined rows drain out of the partial index and are never re-scanned, while
--   the OpenSearch `_id` (= the signal id) makes any re-index an in-place overwrite.
--
-- idx_signals_unindexed (partial, on fetched_at):
--   The newest-first scan predicate of the sweep — WHERE indexed_at IS NULL —
--   mirrors idx_signals_unsummarized (0081). Keeps the per-tick SELECT index-only
--   over the shrinking un-indexed backlog as it drains.
--
--   NO modality filter (unlike 0081's `AND modality = 'text'`): the corpus indexes
--   ALL modalities — the full-text mining substrate should cover every signal that
--   carries body/facet metadata, not just text-modality rows.
--
-- IDEMPOTENT: ADD COLUMN IF NOT EXISTS + CREATE INDEX IF NOT EXISTS, so a re-run
--   against an already-migrated DB (the live dev rig) is a clean no-op. Routed
--   through the filename-gated migration runner (ONE txn + ledger per file; NO
--   inline BEGIN/COMMIT).

ALTER TABLE public.signals
    ADD COLUMN IF NOT EXISTS indexed_at timestamp with time zone;

CREATE INDEX IF NOT EXISTS idx_signals_unindexed
    ON public.signals USING btree (fetched_at)
    WHERE indexed_at IS NULL;
