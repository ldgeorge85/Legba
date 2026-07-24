-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0081_signal_summarized_marker.sql
--
-- Adds the forward-progress marker + partial scan index for the new
-- `signal_summarizer` deterministic sub-handler (an async sweep that distills
-- long signal bodies into signals.payload.distilled_body via the CORE
-- self-hosted LLM plane, so downstream synthesis reads OUR analysis-tuned brief
-- instead of the publisher's teaser).
--
-- signals.summarized_at (nullable timestamptz):
--   Stamped now() on EVERY row the sweep examines (summarized, skipped-short, or
--   LLM-failed), mirroring signals.entities_resolved_at (baseline / migration
--   0029) so the sweep is idempotent + forward-progressing — short/no-body rows
--   drain out of the partial index cheaply and are never re-scanned, and a
--   poison row that fails the LLM is stamped (not retried forever).
--
-- idx_signals_unsummarized (partial, on fetched_at):
--   The oldest-first scan predicate of the sweep — WHERE summarized_at IS NULL
--   AND modality = 'text' — mirrors idx_signals_entities_unresolved
--   (0001_baseline.sql:2384-2387). Keeps the per-tick SELECT index-only over the
--   shrinking un-summarized text backlog as it drains.
--
-- IDEMPOTENT: ADD COLUMN IF NOT EXISTS + CREATE INDEX IF NOT EXISTS, so a re-run
--   against an already-migrated DB (the live dev rig) is a clean no-op. Routed
--   through the filename-gated migration runner (ONE txn + ledger per file; NO
--   inline BEGIN/COMMIT).
--
-- MEASURED (live `legba`, 2026-07-07, migration head 0080): 37,998 signals with
--   modality='text'; ~4.5k carry a body substantial enough (> 500 chars) to
--   warrant an LLM summary — the backlog this sweep drains over ~1–2 days.

ALTER TABLE public.signals
    ADD COLUMN IF NOT EXISTS summarized_at timestamp with time zone;

CREATE INDEX IF NOT EXISTS idx_signals_unsummarized
    ON public.signals USING btree (fetched_at)
    WHERE summarized_at IS NULL AND modality = 'text';
