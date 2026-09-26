# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Event write-path SQL + link helpers (DATA MODEL V3 / P0).

A leaf holding the SQL constants and the link-table writers that
``provenance.writes._insert_event`` drives — kept here so writes.py stays
under its module-size ceiling, in the ``claim_watch_sql.py`` idiom (SQL text
lives beside the domain it serves). No imports beyond typing: this module is
loadable in the slim image and introduces no dependency on the canon chain.

The route itself — ``_insert_event`` — lives in ``provenance/writes.py``; it
owns the ``LEGBA_EVENTS`` gate, the upsert-key guards, and the
``INSERT ... ON CONFLICT`` on the events row. These helpers only ever run
inside that route's transaction, EXCEPT :func:`derive_event_geo` (called
BEFORE the INSERT/UPSERT to supply its ``geo``/``geo_lat``/``geo_lon``
params) and :func:`link_event_to_situations` (called by the event-clustering
handler after a successful write — the event/situation bridge, V3/P1).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

#: ``events`` upsert — the (event_signature, analyst_id) re-materialization
#: contract. Re-emitting a signature UPDATES the row in place: hot columns
#: refresh, ``derived_from`` unions, the earliest ``valid_from`` keeps.
#: ``lifecycle_state`` / ``lifecycle_changed_at`` / ``source_method`` are
#: deliberately NOT in the SET list — the ledger owns the state, and the row's
#: origin producer class is fixed at birth.
EVENT_INSERT_SQL: str = """
    INSERT INTO events (
        id, event_signature, analyst_id, title, summary,
        category, event_type, severity,
        lifecycle_state, lifecycle_changed_at,
        time_start, time_end,
        geo, geo_lat, geo_lon, locations,
        confidence, signal_count, distinct_source_count, oversized,
        valid_from, valid_until,
        source_method, source_type, derived_from,
        target_id, target_version, analyst_version, run_id,
        schema_uri, data, produced_at, origin_class
    ) VALUES (
        $1, $2, $3, $4, $5,
        $6, $7, $8,
        'emerging', $9,
        $10, $11,
        $12::text[], $13, $14, $15::text[],
        $16, $17, $18, $19,
        $20, $21,
        $22, $23, $24::uuid[],
        $25, $26, $27, $28,
        $29, $30::jsonb, $31,
        -- V3/P7: events are minted by the live lane only; 'live' is a
        -- literal, not a parameter, so no caller can stamp history.
        'live'
    )
    ON CONFLICT (event_signature, analyst_id) DO UPDATE SET
        data=EXCLUDED.data, title=EXCLUDED.title, summary=EXCLUDED.summary,
        category=EXCLUDED.category, event_type=EXCLUDED.event_type,
        severity=EXCLUDED.severity,
        time_start=EXCLUDED.time_start, time_end=EXCLUDED.time_end,
        geo=EXCLUDED.geo, geo_lat=EXCLUDED.geo_lat, geo_lon=EXCLUDED.geo_lon,
        locations=EXCLUDED.locations,
        confidence=EXCLUDED.confidence, signal_count=EXCLUDED.signal_count,
        distinct_source_count=EXCLUDED.distinct_source_count,
        oversized=EXCLUDED.oversized,
        valid_until=EXCLUDED.valid_until,
        valid_from=LEAST(events.valid_from, EXCLUDED.valid_from),
        derived_from=(
            SELECT array_agg(DISTINCT x)
              FROM unnest(events.derived_from || EXCLUDED.derived_from) AS x
        ),
        updated_at=NOW()
    RETURNING id, (xmax = 0) AS inserted
"""

#: Evidence links — idempotent on re-emit: relevance keeps the max seen and
#: ``linked_at`` keeps the FIRST evidence time (it is the signal's fetched_at,
#: so the same signal always carries the same value; LEAST is belt-and-braces
#: for a producer that stamped a drifted clock).
SIGNAL_EVENT_LINK_SQL: str = """
    INSERT INTO signal_event_links
        (signal_id, event_id, linked_at, relevance,
         source_class, source_kind, source_id, created_at)
    VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
    ON CONFLICT (signal_id, event_id) DO UPDATE SET
        relevance = GREATEST(signal_event_links.relevance, EXCLUDED.relevance),
        linked_at = LEAST(signal_event_links.linked_at, EXCLUDED.linked_at)
"""

#: Actor links — idempotent on re-emit: confidence keeps the max seen and
#: derived_from unions the motivating signal ids.
EVENT_ENTITY_LINK_SQL: str = """
    INSERT INTO event_entity_links
        (event_id, entity_id, role, confidence, derived_from)
    VALUES ($1, $2, $3, $4, $5::uuid[])
    ON CONFLICT (event_id, entity_id, role) DO UPDATE SET
        confidence = GREATEST(event_entity_links.confidence,
                              EXCLUDED.confidence),
        derived_from = (
            SELECT array_agg(DISTINCT x)
              FROM unnest(event_entity_links.derived_from
                          || EXCLUDED.derived_from) AS x
        )
"""

#: ``signal_count`` / ``distinct_source_count`` reconciliation — the ONE
#: definition of those two rollups: they are the ``signal_event_links``
#: membership of the event, counted from the link table itself, never a
#: number carried on the payload.
#:
#: Why the write cannot trust the payload: ``_event_payload`` counts
#: ``prior_signals | candidate.signal_ids`` — every MEMBER — while
#: ``signal_links_for`` skips a member whose ``fetched_at`` is NULL (a link
#: row needs an evidence time), so a candidate with an undated member
#: counted one more than it linked. And the events upsert re-runs on every
#: re-materialization of the same ``(event_signature, analyst_id)``, so a
#: single wrong number persists until something overwrites it.
#:
#: Run once per written event, inside ``_insert_event``'s transaction and
#: AFTER :func:`insert_event_links` — one bounded statement per event
#: (``idx_sel_event`` on ``(event_id, relevance DESC)``), never one per
#: member. The ``IS DISTINCT FROM`` guard makes the common re-emit (the
#: counts already agree) a no-op UPDATE rather than a row rewrite.
#: ``updated_at`` is deliberately NOT touched — the upsert above already
#: stamped it in this same transaction, and the tower's open-event read
#: orders on it.
EVENT_ROLLUP_RECONCILE_SQL: str = """
    UPDATE events e
       SET signal_count = c.n,
           distinct_source_count = c.d
      FROM (
        SELECT count(*)::int AS n,
               count(DISTINCT NULLIF(l.source_id, ''))::int AS d
          FROM signal_event_links l
         WHERE l.event_id = $1
      ) c
     WHERE e.id = $1
       AND (e.signal_count IS DISTINCT FROM c.n
            OR e.distinct_source_count IS DISTINCT FROM c.d)
"""

#: The 'opened' ledger row — written on the INSERT branch only, in the same
#: transaction as the events row, so every event opens its ledger or no event
#: lands. ``occurred_at`` is evidence time — the caller passes the newest
#: signal ``linked_at`` when links exist — and ``derived_from`` must be
#: non-empty — 'resolved' is the only
#: evidence-free transition (the CHECK refuses otherwise, and _insert_event
#: refuses earlier with a readable error).
EVENT_OPENED_SQL: str = """
    INSERT INTO event_lifecycle_events
        (event_id, occurred_at, transition, state_from, state_to,
         why, derived_from, analyst_id, analyst_version, run_id)
    VALUES ($1, $2, 'opened', 'emerging', 'emerging', $3,
            $4::uuid[], $5, $6, $7)
"""


#: derive_event_geo — the write-time geo resolution (spec §2.7): "the modal
#: ISO2 across the linked signals' signals.geo; the [point] from the
#: highest-relevance signal carrying data.geo.{lat,lon}". Read here as: the
#: majority (mode) country across every member signal's ``geo`` array
#: (ties broken alphabetically, for determinism/idempotency), then the
#: EARLIEST-evidenced member (by ``fetched_at``, the signal's own evidence
#: clock, then ``id`` for a stable tiebreak) BELONGING TO THAT COUNTRY that
#: carries real coordinates in ``payload->'geo'->{lat,lon}`` — never a
#: member of a different country (that would mislabel a point under the
#: wrong country), never a country centroid, never invented. NULL/empty
#: when no member carries any geo at all; ``geo_lat``/``geo_lon`` stay NULL
#: when the majority country's own members carry no coordinates even if a
#: minority country's member does.
DERIVE_EVENT_GEO_SQL: str = """
    WITH members AS (
        SELECT id, fetched_at, geo,
               (payload -> 'geo' ->> 'lat')::double precision AS geo_lat,
               (payload -> 'geo' ->> 'lon')::double precision AS geo_lon
          FROM signals
         WHERE id = ANY($1::uuid[])
    ),
    countries AS (
        SELECT country, count(*) AS n
          FROM members, unnest(members.geo) AS country
         WHERE country <> ''
         GROUP BY country
    ),
    majority AS (
        SELECT country FROM countries
         ORDER BY n DESC, country ASC
         LIMIT 1
    ),
    point AS (
        SELECT m.geo_lat, m.geo_lon
          FROM members m, majority
         WHERE majority.country = ANY(m.geo)
           AND m.geo_lat IS NOT NULL AND m.geo_lon IS NOT NULL
         ORDER BY m.fetched_at ASC, m.id ASC
         LIMIT 1
    )
    SELECT majority.country AS geo_country, point.geo_lat, point.geo_lon
      FROM majority
      LEFT JOIN point ON TRUE
"""


async def derive_event_geo(
    conn: Any, signal_ids: Any,
) -> tuple[list[str], float | None, float | None]:
    """``(geo, geo_lat, geo_lon)`` derived from a set of member signal ids.

    ``geo`` is ``[majority_country]`` (or ``[]`` when no member carries a
    ``signals.geo`` entry) — the single modal ISO2 across every member.
    ``geo_lat``/``geo_lon`` are the coordinates of the earliest-evidenced
    majority-country member that carries them, or ``None``/``None`` when
    none does. Never a country centroid, never invented — every non-NULL
    point traces to one real signal's own geocoded ``payload.geo``.

    Safe to call with an empty/None ``signal_ids`` (returns the all-NULL
    result) — a signature-only re-materialization with no evidence links
    still has to pass through here without special-casing at the call site.
    """
    ids = [i for i in (signal_ids or ()) if i is not None]
    if not ids:
        return [], None, None
    row = await conn.fetchrow(DERIVE_EVENT_GEO_SQL, ids)
    if row is None or row["geo_country"] is None:
        return [], None, None
    return [row["geo_country"]], row["geo_lat"], row["geo_lon"]


#: link_event_to_situations — the deterministic event/situation bridge (spec
#: §2.4/§6.3): an event links to every OPEN situation (``status <>
#: 'closed'``) whose derived ``situation_signature`` shares the event's own
#: ``<topic>`` — the target/desk identity (e.g. ``country_watch_il``), NOT
#: the entity-token segment: situations are minted with
#: ``finding_supersession._SITUATION_SIGNATURE_ENTITY_K == 0`` (no ``|``
#: segment at all — "the entity half was turned OFF for frames because it
#: fragmented them") while events are minted with
#: ``events.signature.EVENT_SIGNATURE_ENTITY_K == 3``, so matching on the
#: full ``topic|tokens`` block (as the doc's §2.4 prose reads in isolation)
#: NEVER matches on live data — measured live (2026-09-23, 1,124 events /
#: 248 open ``sig:`` situations): 0 matches on ``topic|tokens``, 985 events
#: / 238 situations / 7,359 pairs on ``topic`` alone, and the topic-only
#: pairs read as genuinely correlated (an Australia event topic
#: ``country_g20_au`` linking only to Australia situations). Both signature
#: families share one grammar — ``<prefix>:<topic>[|<tokens>]
#: #<marker>:<value>`` — minted independently by
#: ``events.signature.event_signature`` and
#: ``finding_supersession.derive_signature``/``with_dimension``; stripping
#: each one's own prefix, then everything from the first ``|`` or trailing
#: marker (``#dim:`` for a situation, ``#evt:`` for an event) onward, leaves
#: the comparable topic. Only ``sig:``-derived situations are eligible — an
#: explicit ``sit:`` key is a producer's own arbitrary string with no shared
#: topic grammar to match on. Idempotent (``ON CONFLICT DO NOTHING`` on the
#: ``(situation_id, event_id)`` PK) so re-running on every mint/relink of
#: the same event never duplicates a link.
LINK_EVENT_SITUATIONS_SQL: str = """
    INSERT INTO situation_event_links
        (situation_id, event_id, relevance, derived_from)
    SELECT s.id, $1, 1.0, $3::uuid[]
      FROM situations s
     WHERE s.status <> 'closed'
       AND s.situation_signature LIKE 'sig:%'
       AND split_part(
               regexp_replace(
                   regexp_replace(s.situation_signature, '^sig:', ''),
                   '#dim:.*$', ''
               ),
               '|', 1
           ) = $2
       AND (s.valid_from  IS NULL OR s.valid_from  <= $4)
       AND (s.valid_until IS NULL OR s.valid_until >= $4)
    ON CONFLICT (situation_id, event_id) DO NOTHING
    RETURNING situation_id
"""

#: The marker an event-signature's own topic segment ends at — read past by
#: :func:`event_topic_key`. Must equal ``events.signature.EVENT_MARKER``
#: (``"#evt:"``) and ``finding_supersession._SIGNATURE_EVENT_MARKER``; a test
#: pins the three equal. Kept as a literal (not imported) so this leaf stays
#: import-light — no dependency on the analyst/canon chain.
_EVENT_TOPIC_MARKER: str = "#evt:"


def event_topic_key(event_signature: Any) -> str | None:
    """The bare ``<topic>`` identity an ``evt:``-minted signature carries —
    stripped of its ``evt:`` prefix and truncated at whichever comes first,
    the ``|<entity tokens>`` segment or the trailing ``#evt:`` anchor — the
    same topic :data:`LINK_EVENT_SITUATIONS_SQL` extracts from a
    ``sig:``-minted situation signature, so the two compare equal when (and
    only when) they are about the same target/topic. Entity tokens are
    deliberately excluded from the comparison — see
    :data:`LINK_EVENT_SITUATIONS_SQL`'s banner for why matching on them
    would never match anything. ``None`` for anything not minted by
    :func:`legba.data.events.signature.event_signature` (a non-``evt:``
    signature has no shared grammar to match on).
    """
    text = str(event_signature or "")
    if not text.startswith("evt:"):
        return None
    body = text[len("evt:"):]
    cut = len(body)
    pipe_at = body.find("|")
    if pipe_at != -1:
        cut = min(cut, pipe_at)
    marker_at = body.find(_EVENT_TOPIC_MARKER)
    if marker_at != -1:
        cut = min(cut, marker_at)
    topic = body[:cut]
    return topic or None


async def link_event_to_situations(
    conn: Any,
    event_id: UUID,
    event_signature: Any,
    *,
    derived_from: Any,
    window_at: Any,
) -> int:
    """Idempotently link ``event_id`` to every matching OPEN situation.

    Returns the count of NEWLY-created links this call made (0 on a re-run
    that finds nothing new — the receipt-facing ``situations_linked``
    delta, not a running total). A signature with no shared topic grammar
    (:func:`event_topic_key` returns ``None``) links to nothing — there is
    no identity to match a situation on.
    """
    topic_key = event_topic_key(event_signature)
    if topic_key is None:
        return 0
    rows = await conn.fetch(
        LINK_EVENT_SITUATIONS_SQL,
        event_id,
        topic_key,
        list(derived_from or ()),
        window_at,
    )
    return len(rows)


async def insert_event_links(
    conn: Any, event_id: UUID, payload: Any, *, produced_at: Any,
) -> None:
    """Write the payload's signal + entity links for ``event_id``.

    Idempotent — a re-materialization upserts each link rather than
    duplicating it. Only called from ``_insert_event``, inside its
    transaction.
    """
    for link in getattr(payload, "signals", None) or ():
        await conn.execute(
            SIGNAL_EVENT_LINK_SQL,
            link.signal_id,
            event_id,
            link.linked_at,
            float(link.relevance),
            link.source_class,
            link.source_kind,
            link.source_id,
            produced_at,
        )
    for link in getattr(payload, "entities", None) or ():
        await conn.execute(
            EVENT_ENTITY_LINK_SQL,
            event_id,
            link.entity_id,
            link.role,
            float(link.confidence),
            list(link.derived_from),
        )


async def reconcile_event_rollups(conn: Any, event_id: UUID) -> None:
    """Set ``signal_count``/``distinct_source_count`` to the link table's truth.

    The post-condition of every event write: after this call the persisted
    rollups equal ``count(*)`` and ``count(DISTINCT source_id)`` over
    ``signal_event_links`` for ``event_id``. Only called from
    ``_insert_event``, inside its transaction, after
    :func:`insert_event_links` — see :data:`EVENT_ROLLUP_RECONCILE_SQL`.
    """
    await conn.execute(EVENT_ROLLUP_RECONCILE_SQL, event_id)


async def insert_event_opened(
    conn: Any,
    event_id: UUID,
    *,
    title: str,
    occurred_at: Any,
    evidence: list[UUID],
    prov: Any,
) -> None:
    """Append the 'opened' ledger row for a just-inserted event.

    ``why`` is the event's own title — the occurrence's name IS the honest
    statement of what opened (the CHECK requires non-empty, and the title is
    ``min_length=1`` on the payload). ``occurred_at`` is evidence time, and
    ``evidence`` is the lineage the requires-evidence CHECK demands; the caller
    refuses empty earlier.
    """
    await conn.execute(
        EVENT_OPENED_SQL,
        event_id,
        occurred_at,
        str(title)[:2000],
        list(evidence),
        prov.analyst_id,
        prov.analyst_version,
        prov.run_id,
    )
