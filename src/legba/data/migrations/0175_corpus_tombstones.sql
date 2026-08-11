-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0175_corpus_tombstones.sql
--
-- THE OPENSEARCH CORPUS HAS NEVER HAD A DELETE PATH (W2-C, 2026-08-03).
--
-- `legba_signals_corpus` is written by exactly two callers (the `corpus_indexer`
-- sweep and `scripts/backfill_corpus.py`) and its `_id` IS the `signals.id` uuid,
-- so a doc maps back to its row losslessly. What never existed is the other
-- direction: `OpenSearchStore` exposed `ensure_index` / `bulk_index` / `search` /
-- `get` and NO delete of any kind, and no code anywhere in `src/`, `scripts/` or
-- `tests/` ever issued one. Every signals purge in the platform's history
-- therefore left its documents behind, permanently.
--
-- MEASURED READ-ONLY AGAINST THE LIVE INDEX, 2026-08-03 (exhaustive scroll of
-- every `_id`, set-differenced against `signals` — not a sample):
--
--     corpus docs                                       182,648
--     signals rows                                      111,537
--     ORPHAN docs (no signals row at all)                75,871   (41.5%)
--     live docs                                         106,777
--     indexed signals carrying NO doc                     4,743
--
-- The 41.5% CORRECTS the 54% in the 2026-08-02 engine review §3.3, which was a
-- 200-doc sample (108/200) rather than a census; the true rate is a third lower.
-- The orphan total is 75,871 to the row, which is EXACTLY the size of the 07-28
-- `collapse_intrasource_dupes --apply` run. That is not a coincidence: the corpus
-- index was built by the signal-content-depth program in July, AFTER the 06-26
-- health remediation, so the intra-source collapse is the whole population, and
-- that script's own header already conceded the debt --- it dumps `--ids-out`
-- "for an optional later sidecar cleanup" that was never built. This is it.
--
-- WHY A TOMBSTONE TABLE AND NOT A DIRECT DELETE AT EACH SITE. Three call sites
-- delete signals (`_retention_sweep._purge_signals`, `collapse_intrasource_dupes`,
-- `seed_predictor_signals`). Reaching OpenSearch from inside each one would put a
-- fallible network call inside a Postgres transaction that is currently atomic and
-- local: a timeout would either abort a good purge or, worse, commit the purge and
-- lose the delete — reintroducing the orphan it was added to prevent. So the
-- deletion sites record INTENT transactionally (an INSERT in the same transaction
-- as the DELETE, which either both land or neither does), and one sweep
-- (`corpus_retention`) drains that queue against OpenSearch with a bounded budget
-- and a retry that costs nothing. The queue is also the audit trail the platform
-- did not have: "which docs did we drop, when, and why" is a SELECT.
--
-- THE DRAIN RE-VERIFIES BEFORE IT DELETES. A tombstone is a claim that a row is
-- gone, and the drain never trusts it: it re-checks `NOT EXISTS (SELECT 1 FROM
-- signals WHERE id = doc_id)` at drain time and skips any doc whose row is alive,
-- counting it as `skipped_row_alive`. A mistaken or stale tombstone therefore
-- cannot destroy a live document — the only failure mode left is an orphan that
-- outlives its tombstone, which is the harmless direction.
--
-- REVERSIBILITY. Up until the drain runs, a tombstone is a plain row and the
-- whole queue is cancellable:
--
--     DELETE FROM corpus_tombstones WHERE purged_at IS NULL AND reason = '...';
--
-- After the drain the OpenSearch delete is real (a search index has no undo), but
-- it is also the one operation that is genuinely recoverable by other means: the
-- doc was a projection of a Postgres row that no longer exists, so there is
-- nothing to restore and nothing to lose. The `purged_at` stamp keeps every
-- dropped id queryable forever.

CREATE TABLE IF NOT EXISTS public.corpus_tombstones (
    -- The deleted `signals.id`. It IS the OpenSearch `_id` (see
    -- `opensearch.signal_to_doc`), so no mapping table is needed. PRIMARY KEY
    -- makes the recording idempotent: a re-run of any purge or of the orphan
    -- backfill re-INSERTs the same ids and ON CONFLICT DO NOTHING absorbs it.
    doc_id      uuid        PRIMARY KEY,

    -- Which index the doc lives in. One index exists today
    -- (`legba_signals_corpus`) but the column costs nothing and stops this table
    -- from being the thing that has to change when a second one appears.
    index_name  text        NOT NULL,

    -- Who tombstoned it, for the audit trail: 'signals_retention',
    -- 'intrasource_collapse', 'orphan_backfill'. Free text by design — a new
    -- deletion site should not need a migration to record itself.
    reason      text        NOT NULL,

    created_at  timestamptz NOT NULL DEFAULT now(),

    -- NULL = still queued. Set when the drain has confirmed the delete.
    purged_at   timestamptz,

    -- Bounded-retry bookkeeping. A doc OpenSearch keeps refusing shows up here
    -- rather than silently cycling forever; the sweep surfaces the max.
    attempts    integer     NOT NULL DEFAULT 0,
    last_error  text
);

-- The drain's only hot query: the pending queue, oldest first. Partial, so the
-- index holds the BACKLOG and not the (permanent, growing) purged history.
CREATE INDEX IF NOT EXISTS idx_corpus_tombstones_pending
    ON public.corpus_tombstones (created_at)
    WHERE purged_at IS NULL;

-- The S-1 gauge's `resolved_sql` window scan.
CREATE INDEX IF NOT EXISTS idx_corpus_tombstones_purged_at
    ON public.corpus_tombstones (purged_at)
    WHERE purged_at IS NOT NULL;

COMMENT ON TABLE public.corpus_tombstones IS
    'Queue of OpenSearch corpus documents whose substrate row was deleted. '
    'Written transactionally by every signals-deletion site; drained by the '
    'corpus_retention sweep, which re-verifies the row is gone before deleting.';
