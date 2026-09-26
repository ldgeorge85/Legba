-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0213_events_geo_backfill.sql — DATA MODEL V3 / P1, two reader surfaces that
-- shipped plumbed but dark (spec §2.7 geo, §2.4/§6.3 the situation bridge).
--
-- PART 1 — events.geo_lat / geo_lon backfill
-- --------------------------------------------
-- The event writer (``_insert_event`` / ``events._writes.derive_event_geo``,
-- this same release) never derived a minted event's position from its member
-- signals, so every existing event's ``geo_lat`` / ``geo_lon`` sat NULL — the
-- map's events layer (deliberately no centroid fallback, spec §2.7) rendered
-- nothing for any of them. This backfills the SAME rule the live writer now
-- applies, set-based, from ``signal_event_links`` -> ``signals``:
--
--   geo_country (intermediate, not persisted separately — there is no
--                 ``events.geo_country`` column) = the MODAL (most-common)
--                 ISO2 across every linked member's own ``signals.geo``
--                 array (ties broken alphabetically);
--   geo_lat/lon = the coordinates of the EARLIEST-evidenced member (by
--                 ``signals.fetched_at``, then ``id``) BELONGING TO THAT
--                 MAJORITY COUNTRY that carries real coordinates in
--                 ``payload -> 'geo' -> {lat,lon}`` — never a member of a
--                 different country, never a country centroid, never
--                 invented; NULL stays NULL where no member carries one.
--
-- geo_lat / geo_lon only fill where currently NULL (never overwrite a
-- manually-set or already-derived point). The existing ``events.geo``
-- text[] column (the country array) is deliberately left untouched by both
-- this migration and the live writer — it is already populated by a
-- broader, existing entity/text-gazetteer derivation
-- (``event_clustering._event_payload``) that this backfill has no reason to
-- second-guess; only the point was ever unconditionally NULL.
--
-- MEASURED (orchestrator, 2026-09-23, live tower, read-only dry-run against
-- the same query this migration runs, two readings a few minutes apart):
-- events = 1,124 then 1,127 · geo_lat/lon backfilled by this migration's
-- query = 1,117 then 1,120. (The lane brief's own prior reading, taken
-- earlier the same day, was 1,101 of 1,107.) All four readings are the same
-- measurement taken at different times of a fleet that never stops writing
-- — the exact count at apply time will differ again; the NOTICE this DO
-- block raises is the authoritative reading for any given apply.
--
-- PART 2 — situation_event_links backfill (the frame/occurrence bridge)
-- -----------------------------------------------------------------------
-- 0203 shipped the ``situation_event_links`` junction table but "P0 writes no
-- rows; the surface exists for P1's materializer" — nothing ever wrote to
-- it, so it sat at 0 rows: the Situation panel's tracked-events list was
-- empty and ``query_events(situation_id=...)`` returned nothing for every
-- situation. This backfills the SAME deterministic rule the live linker
-- (``events._writes.link_event_to_situations``, this same release) now
-- applies on every event mint/relink: link an event to every OPEN situation
-- (``status <> 'closed'``) whose derived ``situation_signature`` shares the
-- event's own bare ``<topic>`` (the target/desk identity — e.g.
-- ``country_watch_il``), within the situation's own ``[valid_from,
-- valid_until]`` window (an unbounded side is NULL and never excludes).
--
-- THE TOPIC-ONLY MATCH, AND WHY NOT topic|tokens: both signature families
-- share one grammar — ``<prefix>:<topic>[|<entity tokens>]#<marker>:<value>``
-- — but situations are minted with
-- ``finding_supersession._SITUATION_SIGNATURE_ENTITY_K == 0`` (no ``|``
-- segment at all: "the entity half was turned OFF for frames because it
-- fragmented them") while events are minted with
-- ``events.signature.EVENT_SIGNATURE_ENTITY_K == 3``. Matching on the full
-- ``topic|tokens`` block — the more literal reading of spec §2.4's prose —
-- measured ZERO matches on the live tower (no situation signature ever
-- carries a ``|`` segment to agree with). Matching on the bare topic alone
-- measured 985 events / 238 open situations / 7,359 links, and the pairs
-- read as genuinely correlated (e.g. every event whose topic is
-- ``country_g20_au`` links only to Australia situations, never to an
-- unrelated desk) — this migration and the live Python twin
-- (``events._writes.LINK_EVENT_SITUATIONS_SQL``) both implement the
-- topic-only rule; a test asserts the two agree.
--
-- Only ``sig:``-derived situations are eligible — an explicit ``sit:`` key
-- is a producer's own arbitrary string with no shared topic grammar to match
-- on (``situation_signature LIKE 'sig:%'`` below).
--
-- MEASURED (orchestrator, 2026-09-23, live tower): open ``sig:`` situations
-- = 248 · events with a minted ``evt:`` signature = 1,124 · links inserted
-- by this migration = 7,359 (985 distinct events, 238 distinct situations).
--
-- IDEMPOTENT: PART 1 only touches rows currently NULL; PART 2 is
-- ``ON CONFLICT (situation_id, event_id) DO NOTHING`` on the table's own PK.
-- A re-run (this migration, or the live writers on the rows it touched) is a
-- no-op / repair, never a duplicate.
--
-- ROLLBACK: PART 2 — ``DELETE FROM situation_event_links``. PART 1 has no
-- clean rollback (it only fills NULLs; the prior NULL values are not
-- recoverable from the row itself) — re-running the live writer on an
-- affected event's next mint/relink is the forward repair; there was never a
-- prior non-NULL value to restore to.
--
-- SAFETY: both parts are plain, re-runnable ``UPDATE``/``INSERT`` statements
-- over ``IF EXISTS``-safe base tables already created by 0202/0203; nothing
-- here is DDL, so a partial/interrupted apply leaves no half-built schema.

DO $$
DECLARE
    n_geo integer;
    n_links integer;
BEGIN

-- ---------------------------------------------------------------------------
-- PART 1 — events.geo_lat / geo_lon
-- ---------------------------------------------------------------------------

WITH member_countries AS (
    SELECT sel.event_id, s.geo AS country_arr
      FROM signal_event_links sel
      JOIN signals s ON s.id = sel.signal_id
),
countries AS (
    SELECT event_id, country, count(*) AS n
      FROM member_countries, unnest(country_arr) AS country
     WHERE country <> ''
     GROUP BY event_id, country
),
ranked_countries AS (
    SELECT event_id, country,
           row_number() OVER (
               PARTITION BY event_id ORDER BY n DESC, country ASC
           ) AS rk
      FROM countries
),
majority AS (
    SELECT event_id, country FROM ranked_countries WHERE rk = 1
),
member_points AS (
    SELECT sel.event_id, s.id AS signal_id, s.fetched_at, s.geo,
           (s.payload -> 'geo' ->> 'lat')::double precision AS geo_lat,
           (s.payload -> 'geo' ->> 'lon')::double precision AS geo_lon
      FROM signal_event_links sel
      JOIN signals s ON s.id = sel.signal_id
),
ranked_points AS (
    SELECT mp.event_id, mp.geo_lat, mp.geo_lon,
           row_number() OVER (
               PARTITION BY mp.event_id
               ORDER BY mp.fetched_at ASC, mp.signal_id ASC
           ) AS rk
      FROM member_points mp
      JOIN majority m ON m.event_id = mp.event_id
     WHERE m.country = ANY(mp.geo)
       AND mp.geo_lat IS NOT NULL AND mp.geo_lon IS NOT NULL
),
point AS (
    SELECT event_id, geo_lat, geo_lon FROM ranked_points WHERE rk = 1
),
resolved AS (
    SELECT e.id AS event_id,
           point.geo_lat, point.geo_lon
      FROM events e
      JOIN majority ON majority.event_id = e.id
      JOIN point ON point.event_id = e.id
)
UPDATE events e
   SET geo_lat = resolved.geo_lat,
       geo_lon = resolved.geo_lon,
       updated_at = now()
  FROM resolved
 WHERE resolved.event_id = e.id
   AND e.geo_lat IS NULL
   AND e.geo_lon IS NULL;
GET DIAGNOSTICS n_geo = ROW_COUNT;
RAISE NOTICE '0213 PART 1: backfilled geo_lat/geo_lon on % events', n_geo;

-- ---------------------------------------------------------------------------
-- PART 2 — situation_event_links (the topic-only bridge; see banner)
-- ---------------------------------------------------------------------------

WITH ev AS (
    SELECT id AS event_id,
           split_part(regexp_replace(event_signature, '^evt:', ''), '|', 1)
               AS topic,
           derived_from,
           COALESCE(valid_from, produced_at) AS anchor_at
      FROM events
     WHERE event_signature LIKE 'evt:%'
),
sit AS (
    SELECT id AS situation_id,
           split_part(
               regexp_replace(
                   regexp_replace(situation_signature, '^sig:', ''),
                   '#dim:.*$', ''
               ),
               '|', 1
           ) AS topic,
           valid_from, valid_until
      FROM situations
     WHERE status <> 'closed'
       AND situation_signature LIKE 'sig:%'
),
matched AS (
    SELECT DISTINCT ev.event_id, sit.situation_id, ev.derived_from
      FROM ev
      JOIN sit ON ev.topic = sit.topic AND ev.topic <> ''
     WHERE (sit.valid_from  IS NULL OR sit.valid_from  <= ev.anchor_at)
       AND (sit.valid_until IS NULL OR sit.valid_until >= ev.anchor_at)
)
INSERT INTO situation_event_links (situation_id, event_id, relevance, derived_from)
SELECT situation_id, event_id, 1.0, derived_from
  FROM matched
ON CONFLICT (situation_id, event_id) DO NOTHING;
GET DIAGNOSTICS n_links = ROW_COUNT;
RAISE NOTICE '0213 PART 2: inserted % situation_event_links rows', n_links;

END $$;
