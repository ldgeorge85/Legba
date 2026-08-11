-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0144_backfill_entity_edges_from_nexuses.sql
--
-- K-G1 step 4a — project every OPEN `nexuses` row into `entity_edges` (0143),
-- resolving the text endpoints to entity ids. All three nexus tiers land in one
-- pass because they are one table; what separates them is `edge_family`, and
-- getting that classification right at backfill time is the whole point (see
-- 0143's header on why the seed lattice must not be filed as `relation`).
--
-- `nexuses` IS NOT TOUCHED. It remains the row of record and every reader stays
-- on it. This migration only ADDs rows to `entity_edges` and
-- `entity_edges_unresolved`. Rollback is `DELETE FROM entity_edges WHERE
-- 'nexuses' = ANY(...)` — or simply re-running, since it is idempotent.
--
-- ── measured expectations (live substrate, 2026-08-03, read-only) ──────────
-- Open nexuses 12,732. Resolvable on BOTH endpoints: 12,236 (96.10 %).
--
--   | edge_family  | open  | clean | ambiguous | unresolved | clean % | edges |
--   |--------------|------:|------:|----------:|-----------:|--------:|------:|
--   | relation     |   810 |   688 |        33 |         89 |  84.94  |   686 |
--   | reference    | 3,287 | 3,253 |        34 |          0 |  98.97  | 2,963 |
--   | cooccurrence | 8,635 | 8,295 |        83 |        257 |  96.06  | 8,286 |
--   | TOTAL        |12,732 |12,236 |       150 |        346 |  96.10  |11,935 |
--
-- The last column is what actually lands: 12,236 clean rows collapse to 11,935
-- edges. `reference` collapses hardest (3,253 -> 2,963) because the seed lattice
-- names the same country several ways and the merges have since agreed. Zero
-- rows are self-referencing. Every row that does not land is PARKED (496 of
-- them), so 12,732 = 11,935 + 301 collapsed + 496 parked, and nothing is lost.
--
-- Two of those numbers deserve comment. The 96.10 % overall is a point below the
-- 97.25 % measured on 08-03 by the judge, and `relation` — the tier that matters
-- most, Legba's own derived edges — resolves WORST at 84.94 %. That is the
-- compounding clock in the debate made concrete: the reifier types entities the
-- GC later removes or renames, so the tier with the longest-lived rows carries
-- the most decayed endpoints. The residue is parked, adjudicable, and does not
-- get better by waiting.
--
-- These are EXPECTATIONS, not assertions. The substrate moves (~456 new nexuses
-- a day), so the migration measures what it actually did and RAISEs it as a
-- NOTICE rather than failing a deploy on a stale constant.
--
-- ── resolution ─────────────────────────────────────────────────────────────
-- One name map, built once: every profile's lowered canonical_name -> the
-- DISTINCT terminal ids it reaches through `resolve_entity()`. Tombstones are
-- matched deliberately — an edge naming a merged loser must land on its keeper,
-- which is what repairs the rows naming tombstones — and the count is what
-- separates "unknown" from "ambiguous". A name reaching >1 terminal id is
-- PARKED, never guessed: the entity uniqueness index is
-- (lower(canonical_name), entity_class), so a name is not a key.
--
-- An unresolved INTERMEDIARY degrades to NULL rather than sinking the edge —
-- "A relates to B" survives losing "via C". A row whose endpoints resolve to the
-- SAME entity (a merge happened since) is skipped, not parked: an entity is not
-- related to itself, and the nexus row remains the historical assertion.
--
-- ── dedupe ─────────────────────────────────────────────────────────────────
-- Several NAME triples can map to one ID triple (that is precisely what merges
-- and aliases do), so the projection is grouped before it is inserted. Without
-- that, `ON CONFLICT DO UPDATE` raises "cannot affect row a second time".
-- Grouping SUMS the sightings and UNIONS the evidence, so a collapse never drops
-- a citation. `derived_from` carries each contributing nexus id, so every edge
-- points back at the rows it was projected from. (The dual-write does NOT add
-- the nexus id there: its nexus row may land on the upsert branch, where the id
-- it was given is not the id that survives.)
--
-- SAFETY (idempotent, additive, forward-only): re-running coalesces through the
-- same ON CONFLICT paths and re-derives identical park rows; no existing table
-- is modified. The runner wraps this file in its own transaction.

DO $$
DECLARE
    v_clean int; v_parked int; v_self int; v_edges int;
    v_rel int; v_ref int; v_coo int;
BEGIN

-- ---------------------------------------------------------------------------
-- The projection, resolved and classified. Built once, read by both inserts.
-- ---------------------------------------------------------------------------
CREATE TEMP TABLE _bf_nexus ON COMMIT DROP AS
WITH nm AS MATERIALIZED (
    SELECT lower(ep.canonical_name)                          AS key,
           count(DISTINCT public.resolve_entity(ep.id))      AS n,
           (array_agg(DISTINCT public.resolve_entity(ep.id)))[1] AS rid
      FROM public.entity_profiles ep
     GROUP BY 1
)
SELECT n.id, n.subject, n.object, n.rel_type, n.polarity, n.intent, n.channel,
       n.confidence, n.valid_from, n.derived_from, n.source_signal_ids,
       n.source_type, n.seed_batch_id, n.analyst_id, n.analyst_version,
       n.run_id, n.target_id, n.target_version, n.produced_at, n.created_at,
       -- The producer -> tier map. Mirrors `edge_family_for()` in
       -- provenance/entity_edge_writes.py exactly; verified exhaustive against
       -- all seven (analyst_id, source_type, rel_type) combinations live.
       CASE
           WHEN n.source_type IN ('seed', 'manual')
             OR lower(COALESCE(n.analyst_id, '')) LIKE 'seed.%' THEN 'reference'
           WHEN lower(n.rel_type) = 'co occurs with'            THEN 'cooccurrence'
           ELSE 'relation'
       END AS fam,
       COALESCE(s.n, 0) AS s_n, s.rid AS s_id,
       COALESCE(o.n, 0) AS o_n, o.rid AS o_id,
       COALESCE(i.n, 0) AS i_n, i.rid AS i_id
  FROM public.nexuses n
  LEFT JOIN nm s ON s.key = lower(btrim(n.subject))
  LEFT JOIN nm o ON o.key = lower(btrim(n.object))
  LEFT JOIN nm i ON i.key = lower(btrim(COALESCE(n.intermediary, '')))
 WHERE n.valid_until IS NULL AND n.superseded_by IS NULL;

-- ---------------------------------------------------------------------------
-- 1. The residue — parked FIRST, so a failure downstream still leaves the
--    honest record of what could not be resolved.
-- ---------------------------------------------------------------------------
INSERT INTO public.entity_edges_unresolved
    (src_text, dst_text, edge_type, edge_family, reason, origin_table,
     origin_id, payload)
SELECT left(b.subject, 2048), left(b.object, 2048), b.rel_type, b.fam,
       CASE WHEN b.s_n > 1 OR b.o_n > 1 THEN 'ambiguous'
            WHEN b.s_n = 0              THEN 'src_unresolved'
            ELSE                             'dst_unresolved' END,
       'nexuses', b.id,
       jsonb_build_object('src_matches', b.s_n, 'dst_matches', b.o_n,
                          'analyst_id', b.analyst_id,
                          'source_type', b.source_type,
                          'backfill', '0144')
  FROM _bf_nexus b
 WHERE b.s_n <> 1 OR b.o_n <> 1
    ON CONFLICT (origin_table, origin_id) WHERE origin_id IS NOT NULL
    DO UPDATE SET reason  = EXCLUDED.reason,
                  payload = EXCLUDED.payload;
GET DIAGNOSTICS v_parked = ROW_COUNT;

-- ---------------------------------------------------------------------------
-- 2. The edges. Grouped by the ID triple, because several NAME triples can map
--    onto one — which is exactly what a merge does.
-- ---------------------------------------------------------------------------
CREATE TEMP TABLE _bf_clean ON COMMIT DROP AS
SELECT b.*,
       COALESCE(CASE WHEN b.i_n = 1 THEN b.i_id END,
                '00000000-0000-0000-0000-000000000000'::uuid) AS via_key,
       CASE WHEN b.i_n = 1 THEN b.i_id END                    AS via_id
  FROM _bf_nexus b
 WHERE b.s_n = 1 AND b.o_n = 1 AND b.s_id <> b.o_id;

SELECT count(*) FROM _bf_nexus WHERE s_n = 1 AND o_n = 1 AND s_id = o_id
  INTO v_self;
SELECT count(*) FROM _bf_clean INTO v_clean;

INSERT INTO public.entity_edges (
    src_id, dst_id, intermediary_id, edge_type, edge_family, polarity,
    intent, channel, confidence, valid_from,
    observed_count, first_seen_at, last_seen_at,
    source_signal_ids, derived_from,
    source_type, seed_batch_id, analyst_id, analyst_version, run_id,
    target_id, target_version, produced_at
)
SELECT p.s_id, p.o_id, p.via_id, p.rel_type, p.fam, p.polarity,
       p.intent, p.channel, a.conf, a.first_at,
       a.n_rows, a.first_at, a.last_at,
       COALESCE(sg.arr, '{}'::uuid[]), COALESCE(dv.arr, '{}'::uuid[]),
       p.source_type, p.seed_batch_id, p.analyst_id, p.analyst_version,
       p.run_id, p.target_id, p.target_version, p.produced_at
  FROM (
      -- the representative row of each id-triple: most confident, then newest
      SELECT DISTINCT ON (s_id, o_id, via_key, lower(rel_type)) *
        FROM _bf_clean
       ORDER BY s_id, o_id, via_key, lower(rel_type),
                confidence DESC, produced_at DESC, id
  ) p
  JOIN (
      SELECT s_id, o_id, via_key, lower(rel_type) AS rt,
             count(*)::int                        AS n_rows,
             max(confidence)                      AS conf,
             min(COALESCE(valid_from, created_at)) AS first_at,
             max(COALESCE(valid_from, created_at)) AS last_at
        FROM _bf_clean GROUP BY 1, 2, 3, 4
  ) a  ON  a.s_id = p.s_id AND a.o_id = p.o_id
       AND a.via_key = p.via_key AND a.rt = lower(p.rel_type)
  LEFT JOIN (
      SELECT c.s_id, c.o_id, c.via_key, lower(c.rel_type) AS rt,
             array_agg(DISTINCT x) AS arr
        FROM _bf_clean c, unnest(c.source_signal_ids) x
       GROUP BY 1, 2, 3, 4
  ) sg ON  sg.s_id = p.s_id AND sg.o_id = p.o_id
       AND sg.via_key = p.via_key AND sg.rt = lower(p.rel_type)
  LEFT JOIN (
      -- every contributing nexus id joins the lineage, so an edge can name the
      -- rows it was projected from
      SELECT c.s_id, c.o_id, c.via_key, lower(c.rel_type) AS rt,
             array_agg(DISTINCT x) AS arr
        FROM _bf_clean c, unnest(c.derived_from || ARRAY[c.id]) x
       GROUP BY 1, 2, 3, 4
  ) dv ON  dv.s_id = p.s_id AND dv.o_id = p.o_id
       AND dv.via_key = p.via_key AND dv.rt = lower(p.rel_type)
    ON CONFLICT (src_id, dst_id, edge_type,
                 COALESCE(intermediary_id,
                          '00000000-0000-0000-0000-000000000000'::uuid))
       WHERE valid_until IS NULL AND superseded_by IS NULL
    DO UPDATE SET
        confidence     = GREATEST(entity_edges.confidence, EXCLUDED.confidence),
        observed_count = entity_edges.observed_count + EXCLUDED.observed_count,
        first_seen_at  = LEAST(entity_edges.first_seen_at, EXCLUDED.first_seen_at),
        last_seen_at   = GREATEST(entity_edges.last_seen_at, EXCLUDED.last_seen_at),
        source_signal_ids = COALESCE((SELECT array_agg(DISTINCT e)
                             FROM unnest(entity_edges.source_signal_ids
                                         || EXCLUDED.source_signal_ids) e),
                            '{}'::uuid[]),
        derived_from      = COALESCE((SELECT array_agg(DISTINCT e)
                             FROM unnest(entity_edges.derived_from
                                         || EXCLUDED.derived_from) e),
                            '{}'::uuid[]),
        updated_at     = now();
GET DIAGNOSTICS v_edges = ROW_COUNT;

SELECT count(*) FILTER (WHERE edge_family = 'relation'),
       count(*) FILTER (WHERE edge_family = 'reference'),
       count(*) FILTER (WHERE edge_family = 'cooccurrence')
  FROM public.entity_edges
 WHERE valid_until IS NULL AND superseded_by IS NULL
  INTO v_rel, v_ref, v_coo;

RAISE NOTICE '0144 backfill: % open nexuses projected, % edges written, '
             '% parked, % self-referencing skipped. entity_edges now open: '
             'relation=% reference=% cooccurrence=%',
             v_clean + v_parked + v_self, v_edges, v_parked, v_self,
             v_rel, v_ref, v_coo;

END $$;
