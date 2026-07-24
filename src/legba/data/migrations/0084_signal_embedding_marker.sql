-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0084_signal_embedding_marker.sql
--
-- Adds the partial scan index for the new `signal_embedder` deterministic
-- sub-handler (an async sweep that embeds signal bodies into the Qdrant
-- `legba_signals` collection — the VECTOR PLANE of the signal-content-depth
-- program, the semantic-retrieval substrate that lights up `vector_search`,
-- which no-ops today because the collection holds 0 points).
--
-- signals.embedding_ref (text) ALREADY EXISTS (0001_baseline.sql:706) — this
--   migration adds NOTHING to the table. The sweep uses it as the forward-progress
--   marker: a successfully-embedded row is stamped embedding_ref = the signal id
--   (the Qdrant point _id, so a re-embed overwrites in place), a no-body row is
--   stamped the sentinel 'no_body', and a poison-row embed failure is stamped
--   'embed_failed' — mirroring signals.summarized_at (0081) / signals.indexed_at
--   (0082) stamp-all-examined idempotency, so examined rows drain out of the
--   partial index and are never re-scanned.
--
-- idx_signals_unembedded (partial, on fetched_at):
--   The newest-first scan predicate of the sweep — WHERE embedding_ref IS NULL —
--   mirrors idx_signals_unsummarized (0081) / idx_signals_unindexed (0082). Keeps
--   the per-tick SELECT index-only over the shrinking un-embedded backlog (~109k
--   signals) as it drains.
--
--   NO modality filter (like 0082's corpus index, unlike 0081): the vector plane
--   embeds every signal that carries a usable body/facet payload, not just
--   text-modality rows.
--
-- IDEMPOTENT: CREATE INDEX IF NOT EXISTS, so a re-run against an already-migrated
--   DB (the live dev rig) is a clean no-op. Routed through the filename-gated
--   migration runner (ONE txn + ledger per file; NO inline BEGIN/COMMIT).

CREATE INDEX IF NOT EXISTS idx_signals_unembedded
    ON public.signals USING btree (fetched_at)
    WHERE embedding_ref IS NULL;
