"""The declared backlog drains — the production gauge's one non-auto class.

Extracted verbatim from ``production_gauge.py`` when the 2026-08-09 gauge
calibration pushed that module over the size gate; pure declarations, no
behavior. ``production_gauge`` re-exports both names, so every importer and
test keeps its spelling.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BacklogDrain:
    """One declared "work is due and somebody owns draining it" loop.

    ``overdue_sql`` and ``resolved_sql`` are fully-formed statements, never
    interpolated fragments. ``overdue_sql`` takes no parameters and returns
    ``overdue`` (int) + ``oldest_due_at`` (timestamptz|NULL); ``resolved_sql``
    takes ``$1`` (the window start) and returns ``resolved`` (int). Both are
    executed against the live schema by a test, so a column rename breaks the
    suite instead of silently zeroing the gauge.
    """

    backlog_id: str
    label: str
    owner_analyst_id: str
    unit: str
    overdue_sql: str
    resolved_sql: str


#: The declared set. Adding one is six lines and a test run; there is
#: deliberately no auto-discovery, because "overdue" is a per-table semantic
#: no descriptor states and guessing it would be worse than declaring it.
BACKLOG_DRAINS: tuple[BacklogDrain, ...] = (
    BacklogDrain(
        backlog_id="acute_forecast_resolution",
        label="Acute-forecast resolution",
        owner_analyst_id="forecast_scoreboard",
        unit="forecast",
        # A one-day grace past window close mirrors the resolver's own.
        # `resolved_at`, not `resolved_outcome`: a voided forecast (resolved_by
        # 'voided:*') carries a resolution timestamp but NO outcome, and it is
        # drained work — the resolver dealt with it. Keying on the outcome
        # column counted every void as permanently overdue (false CRITICAL,
        # 2026-08-09).
        overdue_sql="""
            SELECT count(*)::int AS overdue,
                   min(window_end) AS oldest_due_at
              FROM acute_forecasts
             WHERE resolved_at IS NULL
               AND window_end < now() - interval '1 day'
        """,
        resolved_sql="""
            SELECT count(*)::int AS resolved
              FROM acute_forecasts
             WHERE resolved_at IS NOT NULL
               AND resolved_at > $1
        """,
    ),
    BacklogDrain(
        backlog_id="corpus_tombstone_drain",
        label="OpenSearch corpus tombstone drain",
        owner_analyst_id="corpus_retention",
        unit="doc",
        # Orphaned corpus docs waiting to be deleted (migration 0175). The
        # `NOT EXISTS` mirrors the drain's own safety gate exactly: a tombstone
        # whose signals row is still alive is NOT work, it is a bad row, and
        # counting it would manufacture a permanent deficit nobody can clear.
        # `attempts < 5` matches corpus_retention._MAX_ATTEMPTS so a poison doc
        # the sweep has given up on stops being counted as drainable — the gauge
        # measures what the drain will actually attempt, not what it has parked.
        #
        # WHY THIS ONE IS DECLARED. The whole reason the corpus grew to 41.5%
        # orphans is that nothing measured it: a queue with no gauge is exactly
        # how a month of purges went unnoticed. A 1-hour grace covers four
        # cadence ticks, so a single missed tick is not a deficit.
        overdue_sql="""
            SELECT count(*)::int AS overdue,
                   min(t.created_at) AS oldest_due_at
              FROM corpus_tombstones t
             WHERE t.purged_at IS NULL
               AND t.attempts < 5
               AND t.created_at < now() - interval '1 hour'
               AND NOT EXISTS (
                     SELECT 1 FROM signals s WHERE s.id = t.doc_id
                   )
        """,
        resolved_sql="""
            SELECT count(*)::int AS resolved
              FROM corpus_tombstones
             WHERE purged_at IS NOT NULL
               AND purged_at > $1
        """,
    ),
    BacklogDrain(
        backlog_id="entity_edges_dual_write_parity",
        label="entity_edges dual-write parity",
        owner_analyst_id="relationship_reifier",
        unit="edge",
        # W3-A — THE DRIFT IS A SILENT-FAILURE CLASS, which is the only reason
        # this is declared. `entity_edges` is written by ONE choke point
        # (`write_entity_edge_for_nexus`, called from the single `INSERT INTO
        # nexuses` in the codebase) inside the nexus transaction. That design
        # makes divergence impossible *while it runs* — and therefore makes a
        # regression completely invisible: the nexus rows keep landing, every
        # reader on this train keeps returning answers, and the answers quietly
        # narrow to whatever the last backfill left. Nothing else in the system
        # would notice, because there is no error to log. A gauge is the only
        # thing that can see it.
        #
        # THE INVARIANT, stated exactly. Every OPEN nexus whose two endpoint
        # names resolve to two DISTINCT entities must have a matching open
        # `entity_edges` row on (src_id, dst_id, edge_type). Rows that fail the
        # resolve are NOT counted: an unresolvable endpoint is the documented
        # park outcome (0143), not a dual-write failure, and counting it would
        # manufacture a permanent ~2.75% deficit nobody can clear — the same
        # trap the corpus drain's `NOT EXISTS` guard avoids above.
        #
        # A ONE-HOUR GRACE, though the write is same-transaction and needs none
        # in principle. It costs nothing and it means a gauge scan that races a
        # long-running ingest batch cannot report a deficit that resolves
        # itself a second later.
        #
        # The 30-day floor bounds the scan (~2.5s live at 12.7k open nexuses).
        # It also matches what the gauge can act on: a divergence older than a
        # month is a backfill job, not a drain.
        overdue_sql="""
            SELECT count(*)::int AS overdue, min(n.created_at) AS oldest_due_at
              FROM nexuses n
             CROSS JOIN LATERAL (
                    SELECT public.resolve_entity_name(n.subject) AS s_id,
                           public.resolve_entity_name(n.object)  AS d_id
                  ) r
             WHERE n.valid_until IS NULL AND n.superseded_by IS NULL
               AND n.created_at < now() - interval '1 hour'
               AND n.created_at > now() - interval '30 days'
               AND r.s_id IS NOT NULL AND r.d_id IS NOT NULL
               AND r.s_id <> r.d_id
               AND NOT EXISTS (
                     SELECT 1 FROM entity_edges e
                      WHERE e.valid_until IS NULL AND e.superseded_by IS NULL
                        AND e.src_id = r.s_id AND e.dst_id = r.d_id
                        AND lower(e.edge_type) = lower(n.rel_type)
                   )
        """,
        # "The dual-write is writing." `created_at` rather than `updated_at`:
        # an upsert onto an existing edge bumps `updated_at` and is a genuine
        # re-observation, but only a NEW row proves the write path produced
        # something it could not have produced by touching old rows.
        resolved_sql="""
            SELECT count(*)::int AS resolved
              FROM entity_edges
             WHERE created_at > $1
        """,
    ),
    BacklogDrain(
        backlog_id="situation_trajectory_ledger",
        label="Situation trajectory ledger",
        owner_analyst_id="situation_tracker",
        unit="situation",
        # CONTINUITY P2 — WHY THIS ONE IS DECLARED. The trajectory writer's
        # failure mode is silence: `situation_tracker` degrades-not-breaks at
        # every layer (a failed LLM batch defers, a failed ledger append logs and
        # returns), which is right for a run and useless for an operator. A
        # tracker that quietly stopped adjudicating would look exactly like a
        # tracker with nothing to say, and every downstream surface — the
        # composition register, the escalation alert class, the /v3 route — would
        # keep working and keep reporting "no trajectory", forever. The cadence
        # and production classes cannot catch it either: the tracker still runs
        # and still writes a TRACE_ONLY receipt on an idle tick. Only the QUEUE
        # can be measured, so it is declared here.
        #
        # THE INVARIANT, STATED EXACTLY: an OPEN situation holding a VERIFIED
        # member newer than its newest ledger row — i.e. evidence the tracker was
        # allowed to act on arrived, and the ledger has not spoken to it.
        #
        # It compares against the newest VERIFIED member, NOT `last_event_at`,
        # and that is the whole correctness of this drain. `last_event_at` is
        # max(produced_at) over ALL members with no verification gate, while the
        # tracker may only cite members clearing min(confidence, faithfulness) >=
        # the floor. Comparing the two would leave any situation whose newest
        # member is ungraded or demoted permanently overdue — no matter how
        # correctly the tracker had adjudicated everything it is permitted to
        # see. That is exactly the unclearable deficit this drain must not
        # manufacture, so the gate is mirrored here.
        #
        # Also deliberately NOT counted:
        #   * situations with NO ledger row at all. Every one is pre-seed backlog
        #     on day one, and the tracker's seed pass adopts them silently by
        #     design — counting them would open a deficit nobody can drain. Once
        #     a situation has ONE row it is in scope forever after.
        #   * the 1-hour grace, which covers a full tracker interval (the
        #     descriptor fires at :41 hourly), so a scan racing an in-flight
        #     cycle cannot report a self-resolving deficit.
        #   * a 30-day floor bounding the scan, mirroring the parity drain.
        overdue_sql="""
            SELECT count(*)::int AS overdue, min(v.newest_verified) AS oldest_due_at
              FROM situations s
             CROSS JOIN LATERAL (
                    SELECT max(e.occurred_at) AS newest_delta
                      FROM situation_events e
                     WHERE e.situation_id = s.id
                  ) l
             CROSS JOIN LATERAL (
                    SELECT max(f.produced_at) AS newest_verified
                      FROM analyst_outputs f
                      JOIN LATERAL (
                          SELECT (cr.data->>'overall_score')::real AS faith
                            FROM analyst_outputs cr
                           WHERE cr.kind = 'critique'
                             AND cr.data->>'analyzed_output_id' = f.id::text
                             AND cr.data->>'overall_score' IS NOT NULL
                             AND cr.title LIKE 'Faithfulness verify%'
                           ORDER BY cr.produced_at DESC, cr.id DESC
                           LIMIT 1
                      ) c ON TRUE
                     WHERE f.id = ANY(s.derived_from)
                       AND f.kind = 'finding'
                       AND LEAST(f.confidence, c.faith) >= 0.50
                  ) v
             WHERE s.superseded_by IS NULL
               AND (s.valid_until IS NULL OR s.valid_until > now())
               AND s.status <> 'closed'
               AND l.newest_delta IS NOT NULL
               AND v.newest_verified IS NOT NULL
               AND v.newest_verified < now() - interval '1 hour'
               AND v.newest_verified > now() - interval '30 days'
               AND v.newest_verified > l.newest_delta
        """,
        # "The ledger is being written." Ledger rows are append-only and their
        # `created_at` is stamped once, so a count over the window is exactly
        # the drain's throughput with no re-touch to confuse it.
        resolved_sql="""
            SELECT count(*)::int AS resolved
              FROM situation_events
             WHERE created_at > $1
        """,
    ),
)
