# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The open-event read `event_clustering` pays before it matches anything.

Both of that handler's matching legs — the front slice and the tower — start
by fetching one producer's open events with their member/entity aggregates,
and every tick pays that read in full before the first candidate is compared.
It lives here rather than in ``event_clustering.py`` because P1g's set-based
rewrite (below) carries the equivalence argument that makes it reviewable, and
that argument is longer than the query; the handler imports the two names.

Nothing here writes. The row shape — thirteen columns, that ORDER BY — is the
contract ``_event_matcher.prepare_open_events`` and the handler's write path
read, so a change to the projection is a change to both. P1g dropped the
fourteenth, ``lifecycle_state``: the matcher's fold, the category/geo
pre-filter and every ``matched``-row read in the write path were audited
column by column and none of them touches it (the lifecycle phase measures
state from its own query, before this read runs).
"""

from __future__ import annotations

from typing import Any

#: This producer's open events with their member/entity aggregates.
#:
#: P1g (2026-09-24) made this set-based. It was two correlated LATERALs — one
#: ``array_agg`` pass per event row over ``signal_event_links``, another over
#: ``event_entity_links`` joined to ``entity_profiles`` through
#: ``public.resolve_entity()`` — so the aggregate was re-entered once per event
#: and ``resolve_entity()`` ran once per LINK row. Live EXPLAIN (ANALYZE) for
#: ``tower_backfill`` (1,040 rows, LIMIT 5000): 7,255 ms, 1,961,407 shared
#: buffer hits, before a single tower candidate was compared.
#:
#: Now the bounded event set is picked FIRST (``picked``, MATERIALIZED, with
#: the same ORDER BY/LIMIT), then each link table is swept ONCE and grouped by
#: ``event_id`` over that set, and both aggregates are LEFT JOINed back — an
#: event with no surviving link rows produces no group and reads NULL, exactly
#: as ``array_agg`` over an empty LATERAL did.
#:
#: ``resolve_entity()`` is STABLE and walks ``entity_profiles.merged_into``, so
#: within one statement it is a pure function of its argument: applying it once
#: per DISTINCT linked entity (``resolved``) instead of once per link row is
#: the same mapping (live: 54,018 calls instead of 229,231). The CASE skips it
#: outright when the linked profile redirects nowhere — with
#: ``merged_into IS NULL`` the function's own recursive CTE terminates on its
#: anchor row and returns that row's id, which is what ``src.id`` is (live:
#: 1,386 of 149,628 profiles carry a redirect). A linked id with no profile row
#: is dropped by ``JOIN entity_profiles src`` exactly as the old inner join
#: dropped it after ``resolve_entity()``'s fall back to its own argument.
#:
#: Live EXPLAIN (ANALYZE) after: 1,264 ms / 141,381 buffer hits for
#: ``tower_backfill``, 40 ms / 15,927 for ``event_clustering`` (186 rows, was
#: 241 ms / 25,148); both producers' full result sets are byte-identical to the
#: LATERAL form's (md5 over 1,040 and 186 rows, taken in one snapshot).
OPEN_EVENTS_SQL = """
    WITH picked AS MATERIALIZED (
        SELECT e.id, e.event_signature, e.title, e.category, e.geo,
               e.time_start, e.time_end, e.updated_at
          FROM events e
         WHERE e.analyst_id = $1
         ORDER BY e.updated_at DESC, e.id ASC
         LIMIT $2
    ),
    stats AS (
        SELECT sel.event_id,
               array_agg(sel.signal_id ORDER BY sel.signal_id) AS signal_ids,
               array_agg(DISTINCT NULLIF(sel.source_id, ''))
                   FILTER (WHERE NULLIF(sel.source_id, '') IS NOT NULL)
                   AS source_ids,
               max(sel.linked_at) AS latest_linked_at
          FROM signal_event_links sel
         WHERE sel.event_id IN (SELECT id FROM picked)
         GROUP BY sel.event_id
    ),
    ent_links AS MATERIALIZED (
        SELECT eel.event_id, eel.entity_id
          FROM event_entity_links eel
         WHERE eel.event_id IN (SELECT id FROM picked)
    ),
    resolved AS MATERIALIZED (
        SELECT d.entity_id,
               CASE WHEN src.merged_into IS NULL THEN src.id
                    ELSE public.resolve_entity(d.entity_id)
               END AS resolved_id
          FROM (SELECT DISTINCT entity_id FROM ent_links) d
          JOIN entity_profiles src ON src.id = d.entity_id
    ),
    named AS (
        SELECT r.entity_id, ep.canonical_name
          FROM resolved r
          JOIN entity_profiles ep ON ep.id = r.resolved_id
    ),
    ents AS (
        SELECT l.event_id,
               array_agg(l.entity_id ORDER BY l.entity_id) AS entity_ids,
               array_agg(n.canonical_name ORDER BY n.canonical_name)
                   AS entity_names
          FROM ent_links l
          JOIN named n ON n.entity_id = l.entity_id
         GROUP BY l.event_id
    )
    SELECT e.id, e.event_signature, e.title, e.category, e.geo,
           e.time_start, e.time_end, e.updated_at,
           stats.signal_ids, stats.source_ids, stats.latest_linked_at,
           ents.entity_ids, ents.entity_names
      FROM picked e
      LEFT JOIN stats ON stats.event_id = e.id
      LEFT JOIN ents ON ents.event_id = e.id
     ORDER BY e.updated_at DESC, e.id ASC
"""


async def fetch_open_events(
    conn: Any, *, analyst_id: str, limit: int
) -> list[dict[str, Any]]:
    """Existing event identities for this producer, including resolved rows."""
    return [
        dict(row)
        for row in await conn.fetch(OPEN_EVENTS_SQL, analyst_id, int(limit))
    ]


__all__ = ["OPEN_EVENTS_SQL", "fetch_open_events"]
