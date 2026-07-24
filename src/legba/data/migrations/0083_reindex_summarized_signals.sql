-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0083_reindex_summarized_signals.sql
--
-- One-time re-queue of already-summarized signals so their distilled_body flows
-- into the OpenSearch corpus.
--
-- BACKGROUND: `signal_summarizer` (writes payload.distilled_body) and
-- `corpus_indexer` (projects signals into the OpenSearch full-text corpus) are
-- SEPARATE cadence sweeps. The indexer scans `WHERE indexed_at IS NULL`, so a
-- signal that was INDEXED before it was SUMMARIZED kept a summary-less corpus doc
-- (its indexed_at was already stamped, so the new distilled_body never re-flowed).
-- The code fix (signal_summarizer._WRITE_SUMMARY_SQL now nulls indexed_at on every
-- summary write — the corpus DIRTY-MARKER contract) closes this GOING FORWARD, but
-- signals summarized BEFORE that fix shipped are already stamped-and-stale.
--
-- This migration nulls indexed_at for every signal that already carries a
-- distilled_body but is still marked indexed, re-enqueueing it for the indexer's
-- next sweep (which OVERWRITES the doc in place — _id = signal id — with the
-- distilled_body / best_body brief). raw_body stays independently searchable, so a
-- re-index only ENRICHES the doc; recall is never reduced.
--
-- BOUNDED + SAFE: touches only rows with a distilled_body AND indexed_at set (~the
-- handful summarized-then-indexed before the fix — <200 live). Filename-gated, so
-- it runs EXACTLY ONCE (no churn on re-migrate). On a fresh deploy no signal is
-- summarized yet → a clean no-op. Routed through the migration runner (ONE txn +
-- ledger per file; NO inline BEGIN/COMMIT).

UPDATE public.signals
   SET indexed_at = NULL
 WHERE payload ? 'distilled_body'
   AND indexed_at IS NOT NULL;
