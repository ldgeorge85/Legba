-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0218_events_signal_count_correction.sql — the one-off correction of
-- ``events.signal_count`` (and its ``distinct_source_count`` companion, kept
-- on the same rule), plus the index the tower's open-event read wants.
--
-- PART 1 — the correction
-- -----------------------
-- ``events.signal_count`` is the event's ``signal_event_links`` membership.
-- Measured live 2026-09-25 02:00Z: 1,099 of 1,246 rows disagreed with that
-- membership, and 1,110 of them carried EXACTLY ``n_links *
-- greatest(n_actor_links, 1)`` — the worst row 170,526. The cause was one
-- query, ``_event_lifecycle._OPEN_EVENT_STATS_SQL``: it LEFT JOINs BOTH
-- ``signal_event_links`` AND ``event_entity_links`` onto ``events`` in one
-- FROM, so each row is a (member x actor) PAIR, and its ``count(sel.signal_id)``
-- counted pairs. That number was then written straight onto the row by the
-- lifecycle's state update on every transition. The sibling
-- ``count(DISTINCT NULLIF(sel.source_id,''))`` collapsed the same fan-out and
-- so stayed exact on all 1,246 rows, which is why the drift read as a
-- ``signal_count``-only fault; the 147 correct rows are the events with no
-- actor link at all, where the pair count degenerates to the link count.
--
-- Fixed at the source in the same release (that query counts DISTINCT signal
-- ids now) and made a post-condition of every event write
-- (``events._writes.reconcile_event_rollups``, run inside ``_insert_event``'s
-- transaction after the link upserts). This migration corrects the rows the
-- old code already wrote — once, set-based, one pass over each table.
--
-- Idempotent twice over: the ``IS DISTINCT FROM`` guard makes a second run a
-- zero-row UPDATE, and the value it computes is a pure function of the link
-- table, so re-running can only ever re-assert the same number. Events with
-- NO links at all are included (the LEFT JOIN coalesces them to 0) rather
-- than skipped — a row claiming members it does not have is the same defect.
--
-- Not a live-writer change: the writer's own reconciliation covers every
-- future write, so nothing here needs to run again.
--
-- PART 2 — idx_events_analyst_updated
-- -----------------------------------
-- ``_open_event_read.OPEN_EVENTS_SQL`` opens every event-clustering tick with
-- ``WHERE analyst_id = $1 ORDER BY updated_at DESC, id ASC LIMIT $2`` and had
-- no index for it. Live EXPLAIN (ANALYZE) 2026-09-25, 1,246 events:
-- ``tower_backfill`` (1,060 rows) = Seq Scan + Sort, 12.1 ms / 342 buffers;
-- ``event_clustering`` (186 rows) = a Bitmap Index Scan that walks the WHOLE
-- of ``uq_events_signature_analyst`` (analyst_id is its trailing column) +
-- Sort, 3.3 ms / 126 buffers. Both are cheap at 1,246 rows and both scale
-- with TOTAL events rather than with this producer's own — this is the one
-- piece of that read that does.
--
-- Plain ``CREATE INDEX``, not ``CONCURRENTLY``: the runner
-- (``legba.data.migrate._apply_file``) executes each file's whole body inside
-- ``async with conn.transaction()``, and ``CREATE INDEX CONCURRENTLY`` cannot
-- run in a transaction block — it would abort the migration. At 1,246 rows
-- the plain build is instant and its ACCESS EXCLUSIVE lock is held for that
-- instant; at a table size where the lock mattered, the right move is a
-- separate out-of-transaction DDL step, not a flag here.
-- ---------------------------------------------------------------------------

DO $$
DECLARE
    n_fixed bigint;
BEGIN

UPDATE events e
   SET signal_count          = c.n,
       distinct_source_count = c.d
  FROM (
    SELECT ev.id AS event_id,
           COALESCE(l.n, 0)::int AS n,
           COALESCE(l.d, 0)::int AS d
      FROM events ev
      LEFT JOIN (
        SELECT event_id,
               count(*)::int AS n,
               count(DISTINCT NULLIF(source_id, ''))::int AS d
          FROM signal_event_links
         GROUP BY event_id
      ) l ON l.event_id = ev.id
  ) c
 WHERE c.event_id = e.id
   AND (e.signal_count          IS DISTINCT FROM c.n
        OR e.distinct_source_count IS DISTINCT FROM c.d);
GET DIAGNOSTICS n_fixed = ROW_COUNT;
RAISE NOTICE '0218 PART 1: corrected signal_count/distinct_source_count on % events', n_fixed;

END $$;

CREATE INDEX IF NOT EXISTS idx_events_analyst_updated
    ON events (analyst_id, updated_at DESC, id);
