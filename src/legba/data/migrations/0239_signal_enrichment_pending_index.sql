-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0239_signal_enrichment_pending_index.sql
--
-- The partial scan index for the `reenrich_pending` deterministic sub-handler
-- (W11-F118) — the standing sweep over signals the source actor wrote
-- UNENRICHED.
--
-- WHY
-- ---
-- W11-F103 (runtime/source_enrich_hold.py) writes an entry without its
-- enrichment chain when the NLP plane is degraded for the rest of a poll, or
-- when the same entry was skipped on three consecutive polls, and tags it
-- `payload.enrichment_pending` (`nlp_degraded_this_poll` /
-- `enrich_hold_exhausted`). Nothing else re-enriches such a row, so the sweep
-- selects `WHERE payload ? 'enrichment_pending' ORDER BY fetched_at ASC LIMIT n`
-- every tick. No existing index covers `?` on signals.payload (the baseline's
-- GIN indexes are on derived_from / entity_classes / geo / tags only), and a
-- jsonb_ops GIN over the whole payload would be a large index for a predicate
-- that matches only the rows of an outage window. A PARTIAL btree on
-- fetched_at with exactly the sweep's predicate is tiny (empty in steady
-- state), serves the oldest-first ORDER BY + LIMIT directly, and shrinks as
-- the sweep removes the marker — the idx_signals_unsummarized (0081) /
-- idx_signals_needs_reenrich (0085) pattern.
--
-- The sweep's attempt bound (`payload.enrichment_pending_attempts`) filters
-- within this index; the `still_pending` backlog count rides it too.
--
-- SAFETY
-- ------
-- Additive: one index. Re-apply is a no-op (IF NOT EXISTS); routed through the
-- filename-gated migration runner (ONE txn + ledger per file; no inline
-- BEGIN/COMMIT).

CREATE INDEX IF NOT EXISTS idx_signals_enrichment_pending
    ON public.signals USING btree (fetched_at)
    WHERE payload ? 'enrichment_pending';
