# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``claim_watch``'s SQL — every statement the matcher issues, in one place.

Extracted from :mod:`.claim_watch` under the module-size gate
(``tests/test_module_size_gate.py``): the handler had grown past its ceiling
carrying the CW precision train, and the "SQL" section banner was already the
author's own seam. Constants only — no logic, no imports, nothing to call.
The names stay private-by-convention (leading underscore) and are re-exported
from :mod:`.claim_watch`, so this split is invisible to every reader of the
handler and every caller of it.

The statements keep their original commentary verbatim: several of them
document a measurement or a correctness argument (why the global-df
denominator is the ATTRIBUTED signals and not every signal; why the skip-ahead
count is probe-bounded; why the forward walk treats a consumer with no
analyst_outputs row as live), and that reasoning belongs next to the SQL it
justifies rather than in a commit message nobody will find.
"""
from __future__ import annotations

_NEWEST_SIGNAL_SQL = """
    SELECT fetched_at, id
      FROM signals
     ORDER BY fetched_at DESC, id DESC
     LIMIT 1
"""

_NEW_SIGNALS_SQL = """
    SELECT id, fetched_at, geo, payload, canonical_url
      FROM signals
     WHERE (fetched_at, id) > ($1::timestamptz, $2::uuid)
     ORDER BY fetched_at ASC, id ASC
     LIMIT $3
"""

# EXACT count of the signals a skip-ahead abandons, over the half-open range
# (old cursor, horizon]. Bounded by a probe LIMIT so measuring an abandonment
# can never itself become an unbounded scan; saturation is reported, not
# rounded away.
_SKIPPED_SIGNAL_COUNT_SQL = """
    SELECT count(*)::bigint AS skipped
      FROM (
        SELECT 1
          FROM signals
         WHERE (fetched_at, id) > ($1::timestamptz, $2::uuid)
           AND (fetched_at, id) <= ($3::timestamptz, $4::uuid)
         LIMIT $5
      ) probe
"""

_OPEN_QUESTIONS_SQL = """
    SELECT id, thesis, status, target_id, produced_at, derived_from,
           supporting_signals, refuting_signals, diagnostic_evidence
      FROM hypotheses
     WHERE status = ANY($1::text[])
     ORDER BY produced_at DESC, id
     LIMIT $2
"""

# Per-question lineage expansion (bounded two-hop: hypothesis.derived_from →
# facts'/findings' derived_from → facts-cited-by-findings' derived_from) down
# to SIGNAL ids, plus the canonical entity ids linked to those signals
# (one-level merged_into fold — losers converge onto the elected row).
_QUESTION_LINEAGE_SQL = """
    WITH q AS (
        SELECT id, derived_from FROM hypotheses WHERE id = ANY($1::uuid[])
    ), l0 AS (
        SELECT q.id AS qid, unnest(q.derived_from) AS ref FROM q
    ), l1 AS (
        SELECT l0.qid, unnest(fx.derived_from) AS ref
          FROM facts fx JOIN l0 ON fx.id = l0.ref
        UNION
        SELECT l0.qid, unnest(ao.derived_from) AS ref
          FROM analyst_outputs ao JOIN l0 ON ao.id = l0.ref
    ), l2 AS (
        SELECT l1.qid, unnest(fx.derived_from) AS ref
          FROM facts fx JOIN l1 ON fx.id = l1.ref
    ), refs AS (
        SELECT qid, ref FROM l0
        UNION SELECT qid, ref FROM l1
        UNION SELECT qid, ref FROM l2
    ), qsig AS (
        SELECT DISTINCT r.qid, s.id AS sid
          FROM refs r JOIN signals s ON s.id = r.ref
    )
    SELECT qs.qid::text AS qid,
           array_agg(DISTINCT qs.sid) AS lineage_signal_ids,
           COALESCE(
             array_agg(DISTINCT COALESCE(ep.merged_into, sel.entity_id))
               FILTER (WHERE sel.entity_id IS NOT NULL),
             '{}'::uuid[]
           ) AS entity_ids
      FROM qsig qs
      LEFT JOIN signal_entity_links sel ON sel.signal_id = qs.sid
      LEFT JOIN entity_profiles ep ON ep.id = sel.entity_id
     GROUP BY qs.qid
"""

_SIGNAL_ENTITIES_SQL = """
    SELECT sel.signal_id::text AS sid,
           array_agg(DISTINCT COALESCE(ep.merged_into, sel.entity_id))
               AS entity_ids
      FROM signal_entity_links sel
      LEFT JOIN entity_profiles ep ON ep.id = sel.entity_id
     WHERE sel.signal_id = ANY($1::uuid[])
     GROUP BY sel.signal_id
"""

# GLOBAL (signal-side) entity document frequency over a recent stream window.
#
# Denominator = the ATTRIBUTED signals in the window (rows carrying ANY entity
# link), NOT every signal: an unlinked row is a MISSING OBSERVATION (the
# resolution sweep has not reached it, which is exactly the case this
# handler's NER fallback exists for), not evidence that a name is rare.
# Counting unlinked rows in the denominator deflates every df uniformly —
# measured on the live DB, only 224 of the 500 newest signals were attributed,
# so it would have understated every hub by a factor of ~2.2.
#
# Numerator is restricted to the entities that could POSSIBLY be shared (the
# question side's canonical set), so the result stays a few hundred rows
# regardless of how many entities the window mentions. The denominator is
# computed over the whole window either way — restricting it would be the
# same deflation in reverse.
_GLOBAL_ENTITY_DF_SQL = """
    WITH win AS (
        SELECT id FROM signals ORDER BY fetched_at DESC, id DESC LIMIT $1
    ), attributed AS (
        SELECT DISTINCT sel.signal_id
          FROM signal_entity_links sel
          JOIN win ON win.id = sel.signal_id
    ), df AS (
        SELECT COALESCE(ep.merged_into, sel.entity_id) AS eid,
               count(DISTINCT sel.signal_id)::bigint AS n
          FROM signal_entity_links sel
          JOIN attributed a ON a.signal_id = sel.signal_id
          LEFT JOIN entity_profiles ep ON ep.id = sel.entity_id
         WHERE COALESCE(ep.merged_into, sel.entity_id) = ANY($2::uuid[])
         GROUP BY 1
    )
    SELECT sample.attributed_signals,
           df.eid::text AS entity_id,
           df.n AS df
      FROM (SELECT count(*)::bigint AS attributed_signals FROM attributed) sample
      LEFT JOIN df ON TRUE
"""

_ENTITY_NAMES_SQL = """
    SELECT id, lower(btrim(canonical_name)) AS name
      FROM entity_profiles
     WHERE id = ANY($1::uuid[])
"""

# The desk's geo scope (the fusion model's geo plane) AND its display name
# (CW-2: the identity the bearing prompts now show). One row, two uses — the
# name was always sitting in the row the geo read already fetched.
_DESK_GEO_SQL = """
    SELECT descriptor_id, name, body -> 'scope' -> 'geo' AS geo
      FROM target_descriptors
     WHERE is_head = TRUE
       AND descriptor_id = ANY($1::text[])
"""

# FORWARD consumption walk from one question id to its live (non-superseded)
# consumer outputs. A consumer with no analyst_outputs row (journal entries)
# counts as live — supersession is only observable on analyst_outputs.
_FORWARD_WALK_SQL = """
    WITH RECURSIVE walk AS (
        SELECT oc.consumer_id, 1 AS depth
          FROM output_consumption oc
         WHERE oc.consumed_id = $1
        UNION
        SELECT oc.consumer_id, w.depth + 1
          FROM output_consumption oc
          JOIN walk w ON oc.consumed_id = w.consumer_id
         WHERE w.depth < $2
    )
    SELECT DISTINCT w.consumer_id
      FROM walk w
     WHERE NOT EXISTS (
           SELECT 1 FROM analyst_outputs ao
            WHERE ao.id = w.consumer_id
              AND ao.superseded_by IS NOT NULL
     )
     LIMIT $3
"""

# ``data`` (migration 0116) carries the bearing-gate stamp. With the gate OFF
# the writer binds '{}' — the column's own DEFAULT — so a gate-off run stores
# exactly the bytes 3.2.0 stored and the X-1 "absent option changes nothing"
# contract holds at the storage layer, not merely in the handler.
_INSERT_EDGE_SQL = """
    INSERT INTO bearing_edges
        (edge_kind, src_kind, src_id, src_as_of, dst_kind, dst_id, dst_as_of,
         weight, planes, provenance_class, matcher_version, data)
    VALUES ('bears_on', 'signal', $1, $2, 'hypothesis', $3, $4, $5,
            $6::text[], 'live', $7, $8::jsonb)
    ON CONFLICT (src_id, dst_id, edge_kind) DO NOTHING
"""

_INSERT_FLAG_SQL = """
    INSERT INTO review_flags (output_id, founded_on_id, moved_at, reason)
    VALUES ($1, $2, $3, $4)
    ON CONFLICT (output_id, founded_on_id) WHERE closed_at IS NULL DO NOTHING
"""

# The staleness-debt gauge: open flags whose flagged consumer is still a live
# (non-superseded) head. Closed-by-supersession flags are excluded by the
# closed_at test; flags whose consumer got superseded (but nobody closed the
# flag yet) are excluded by the liveness test — the debt only counts review
# work that still has a live product to re-review.
_STALENESS_DEBT_SQL = """
    SELECT count(*)::int AS debt
      FROM review_flags rf
     WHERE rf.closed_at IS NULL
       AND NOT EXISTS (
             SELECT 1 FROM analyst_outputs ao
              WHERE ao.id = rf.output_id
                AND ao.superseded_by IS NOT NULL
       )
"""
