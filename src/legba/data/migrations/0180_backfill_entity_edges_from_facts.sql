-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0180_backfill_entity_edges_from_facts.sql
--
-- K-G1 step 4c — the THIRD and last backfill: project the RELATIONAL `facts`
-- population into `entity_edges` (0143). 0144 took `nexuses`, 0145 took the
-- promoted `proposed_edges`; this file takes the surface both of those missed,
-- and it is the largest single source of Legba-DERIVED typed edges in the
-- substrate.
--
-- WHY FACTS ARE AN EDGE SURFACE AT ALL. `facts` is a subject-predicate-value
-- triple store, and most of it is genuinely attributive — "Iran / population /
-- 89000000" has no second entity and is not an edge. But one extractor backend
-- writes ENTITY-TO-ENTITY triples into the same table: `fact_extractor` with
-- `data->>'backend' = 'relation'`, whose `value` column is another entity's
-- name rather than a literal. Those rows are edges wearing a fact's schema, and
-- until now they were invisible to every graph reader in the system — the
-- reifier only ever promoted a fraction of them into `nexuses`.
--
-- ── the source set, and why it is EXACT rather than a predicate guess ──────
-- The relation backend STAMPS ITSELF (`data->>'backend' = 'relation'`), so the
-- derived tier needs no predicate allowlist and cannot drift when the extractor
-- learns a new predicate. Measured live 2026-08-03: 10,912 open rows over 18
-- distinct predicates, all `source_type='ingestion'`.
--
-- The seed adapters do NOT stamp a backend, so their relational rows are
-- selected by predicate — and only three of their predicates are relational
-- ('leader of', 'head of state', 'head of government', 709 open rows). Every
-- other seed predicate is attributive and stays out. This asymmetry is
-- deliberate: an exact structural marker where one exists, a closed and
-- measured vocabulary where it does not. A seed predicate that is not on the
-- list is not silently dropped — it was never an edge.
--
-- ── measured expectations (live substrate, 2026-08-03, read-only) ──────────
-- Relational open facts 11,621 = 10,912 derived + 709 seed.
-- Resolvable on BOTH endpoints to DISTINCT entities: 8,737 of the 10,912
-- derived rows (80.07 %).
--
-- The derived tier resolving at 80 % — worse than nexuses (96.10 %) and worse
-- than promoted candidates (91.65 %) — is the SAME compounding clock 0144's
-- header names, read at its source. `facts` rows are the oldest and least
-- curated entity references in the substrate: the extractor names whatever the
-- NER emitted, and the GC has since merged or removed a fifth of it. That is
-- a measurement of extraction quality, not a defect in this migration, and it
-- is PARKED where the next train can adjudicate it (see 0181).
--
-- These are EXPECTATIONS, not assertions. The substrate moves, so the migration
-- measures what it actually did and RAISEs it as a NOTICE rather than failing a
-- deploy on a stale constant.
--
-- ── families, and the one place this file differs from 0144 ────────────────
--   * `relation`  — the `backend='relation'` extractor rows. Legba's own
--                   derived typed edges: the world graph proper.
--   * `reference` — the seed adapters' three relational predicates. Imported,
--                   true, static, and excluded from signed analytics by
--                   default, exactly as 0143's header requires.
-- No fact produces `cooccurrence` (co-mention never lands in `facts`) and none
-- produces `structural`. `edge_family` is NEVER overwritten on conflict: if a
-- nexus already minted this triple, its tier stands.
--
-- ── polarity ───────────────────────────────────────────────────────────────
-- A fact carries no polarity column, so the sign is derived from the predicate
-- by the SAME rules the rest of the system signs through — `polarity_from()` in
-- `deterministic_handlers/structural_balance.py`, whose POLARITY table is the
-- single source of truth shared by the reifier (producer) and structural
-- balance (consumer). The map below is that table read through the fact
-- predicates' lowercase surface, and it is CONSERVATIVE: a predicate whose
-- alignment claim is not unambiguous signs 0 (neutral), which is the table's
-- own default and which excludes it from balance rather than guessing a side.
--
-- Signing 'located in', 'border with', 'operates in', 'capital of',
-- 'headquarters in', 'controls', 'supplies to' and 'founded by' as 0 is the
-- load-bearing half of that: they are structural or historical facts about the
-- world, not statements that two actors are aligned, and the whole reason
-- `edge_family` exists is that this system has already been burned once by
-- counting a structural lattice as evidence of alignment.
--
-- ── dedup ──────────────────────────────────────────────────────────────────
-- Several fact rows can name one id triple (that is what merges and repeated
-- extraction do), so the projection is grouped before it is inserted, SUMMING
-- sightings and UNIONING evidence. Facts that collide with an edge 0144/0145
-- already wrote coalesce onto it through the same ON CONFLICT path — an edge
-- asserted by both a fact and a nexus has genuinely been observed twice — and
-- `derived_from` gains the contributing fact ids, so every edge can name the
-- rows it was projected from.
--
-- SAFETY (idempotent, additive, forward-only): `facts` IS NOT TOUCHED — no
-- closes, no updates, no deletes. Re-running coalesces through the same ON
-- CONFLICT paths and re-derives identical park rows. The runner wraps this file
-- in its own transaction.

DO $$
DECLARE
    v_clean int; v_parked int; v_self int; v_edges int;
    v_src int; v_rel int; v_ref int;
BEGIN

-- ---------------------------------------------------------------------------
-- The projection, resolved, classified and signed. Built once, read by both
-- inserts.
-- ---------------------------------------------------------------------------
CREATE TEMP TABLE _bf_fact ON COMMIT DROP AS
WITH nm AS MATERIALIZED (
    -- One name map, built once. Tombstones are matched deliberately — a fact
    -- naming a merged loser must land on its keeper — then chased to the
    -- terminal survivor by resolve_entity() (0086, cycle-safe). A name reaching
    -- more than one terminal id is AMBIGUOUS and is parked, never guessed.
    SELECT lower(ep.canonical_name)                          AS key,
           count(DISTINCT public.resolve_entity(ep.id))      AS n,
           (array_agg(DISTINCT public.resolve_entity(ep.id)))[1] AS rid
      FROM public.entity_profiles ep
     GROUP BY 1
)
SELECT f.id, f.subject, f.value, lower(btrim(f.predicate)) AS predicate,
       f.confidence, f.valid_from, f.derived_from, f.evidence_set,
       f.source_type, f.seed_batch_id, f.analyst_id, f.analyst_version,
       f.run_id, f.target_id, f.target_version, f.produced_at, f.created_at,
       CASE WHEN f.data->>'backend' = 'relation' THEN 'relation'
            ELSE 'reference' END AS fam,
       -- The sign, mirroring polarity_from()'s POLARITY table. Anything not
       -- listed is 0 — neutral, and excluded from balance by the consumer.
       CASE lower(btrim(f.predicate))
           WHEN 'ally of'               THEN  1
           WHEN 'member of'             THEN  1
           WHEN 'part of'               THEN  1
           WHEN 'leader of'             THEN  1
           WHEN 'head of state'         THEN  1
           WHEN 'head of government'    THEN  1
           WHEN 'subsidiary of'         THEN  1
           WHEN 'spokesperson for'      THEN  1
           WHEN 'employed by'           THEN  1
           WHEN 'signed agreement with' THEN  1
           WHEN 'conflict with'         THEN -1
           WHEN 'opponent of'           THEN -1
           WHEN 'sanctioned by'         THEN -1
           ELSE 0
       END::smallint AS pol,
       COALESCE(s.n, 0) AS s_n, s.rid AS s_id,
       COALESCE(o.n, 0) AS o_n, o.rid AS o_id
  FROM public.facts f
  LEFT JOIN nm s ON s.key = lower(btrim(f.subject))
  LEFT JOIN nm o ON o.key = lower(btrim(f.value))
 WHERE f.valid_until IS NULL
   AND f.superseded_by IS NULL
   AND (
        -- the derived tier: an exact structural marker
        f.data->>'backend' = 'relation'
        -- the seed tier: a closed, measured relational vocabulary
        OR (COALESCE(f.data->>'backend', '') <> 'relation'
            AND lower(btrim(f.predicate)) IN
                ('leader of', 'head of state', 'head of government'))
   );

SELECT count(*) FROM _bf_fact INTO v_src;

-- ---------------------------------------------------------------------------
-- 1. The residue — parked FIRST, so a failure downstream still leaves the
--    honest record of what could not be resolved.
-- ---------------------------------------------------------------------------
INSERT INTO public.entity_edges_unresolved
    (src_text, dst_text, edge_type, edge_family, reason, origin_table,
     origin_id, payload)
SELECT left(b.subject, 2048), left(b.value, 2048), b.predicate, b.fam,
       CASE WHEN b.s_n > 1 OR b.o_n > 1 THEN 'ambiguous'
            WHEN b.s_n = 0              THEN 'src_unresolved'
            ELSE                             'dst_unresolved' END,
       'facts', b.id,
       jsonb_build_object('src_matches', b.s_n, 'dst_matches', b.o_n,
                          'analyst_id', b.analyst_id,
                          'source_type', b.source_type,
                          'backfill', '0180')
  FROM _bf_fact b
 WHERE b.s_n <> 1 OR b.o_n <> 1
    ON CONFLICT (origin_table, origin_id) WHERE origin_id IS NOT NULL
    DO UPDATE SET reason  = EXCLUDED.reason,
                  payload = EXCLUDED.payload;
GET DIAGNOSTICS v_parked = ROW_COUNT;

-- ---------------------------------------------------------------------------
-- 2. The edges. Grouped by the ID triple, because several fact rows can map
--    onto one — which is what repeated extraction and merges both do.
-- ---------------------------------------------------------------------------
CREATE TEMP TABLE _bf_fact_clean ON COMMIT DROP AS
SELECT * FROM _bf_fact WHERE s_n = 1 AND o_n = 1 AND s_id <> o_id;

SELECT count(*) FROM _bf_fact WHERE s_n = 1 AND o_n = 1 AND s_id = o_id
  INTO v_self;
SELECT count(*) FROM _bf_fact_clean INTO v_clean;

INSERT INTO public.entity_edges (
    src_id, dst_id, edge_type, edge_family, polarity, intent, channel,
    confidence, valid_from, observed_count, first_seen_at, last_seen_at,
    source_signal_ids, derived_from, evidence_set,
    source_type, seed_batch_id, analyst_id, analyst_version, run_id,
    target_id, target_version, produced_at
)
SELECT p.s_id, p.o_id, p.predicate, p.fam, p.pol, '', 'direct',
       a.conf, a.first_at, a.n_rows, a.first_at, a.last_at,
       '{}'::uuid[], COALESCE(dv.arr, '{}'::uuid[]),
       jsonb_build_object('projected_from', 'facts',
                          'fact_predicate', p.predicate),
       p.source_type, p.seed_batch_id, p.analyst_id, p.analyst_version,
       p.run_id, p.target_id, p.target_version, p.produced_at
  FROM (
      -- the representative row of each id triple: most confident, then newest
      SELECT DISTINCT ON (s_id, o_id, predicate) *
        FROM _bf_fact_clean
       ORDER BY s_id, o_id, predicate, confidence DESC, produced_at DESC, id
  ) p
  JOIN (
      SELECT s_id, o_id, predicate,
             count(*)::int                          AS n_rows,
             max(confidence)                        AS conf,
             min(COALESCE(valid_from, created_at))  AS first_at,
             max(COALESCE(valid_from, created_at))  AS last_at
        FROM _bf_fact_clean GROUP BY 1, 2, 3
  ) a  ON a.s_id = p.s_id AND a.o_id = p.o_id AND a.predicate = p.predicate
  LEFT JOIN (
      -- every contributing fact id joins the lineage, so an edge can name the
      -- rows it was projected from
      SELECT c.s_id, c.o_id, c.predicate, array_agg(DISTINCT x) AS arr
        FROM _bf_fact_clean c, unnest(c.derived_from || ARRAY[c.id]) x
       GROUP BY 1, 2, 3
  ) dv ON dv.s_id = p.s_id AND dv.o_id = p.o_id AND dv.predicate = p.predicate
    ON CONFLICT (src_id, dst_id, edge_type,
                 COALESCE(intermediary_id,
                          '00000000-0000-0000-0000-000000000000'::uuid))
       WHERE valid_until IS NULL AND superseded_by IS NULL
    DO UPDATE SET
        confidence     = GREATEST(entity_edges.confidence, EXCLUDED.confidence),
        observed_count = entity_edges.observed_count + EXCLUDED.observed_count,
        first_seen_at  = LEAST(entity_edges.first_seen_at, EXCLUDED.first_seen_at),
        last_seen_at   = GREATEST(entity_edges.last_seen_at, EXCLUDED.last_seen_at),
        derived_from      = COALESCE((SELECT array_agg(DISTINCT e)
                             FROM unnest(entity_edges.derived_from
                                         || EXCLUDED.derived_from) e),
                            '{}'::uuid[]),
        -- Never clobber a richer evidence blob (a promoted candidate's
        -- evidence_text) with this file's thinner projection marker.
        evidence_set   = COALESCE(entity_edges.evidence_set, EXCLUDED.evidence_set),
        updated_at     = now();
GET DIAGNOSTICS v_edges = ROW_COUNT;

SELECT count(*) FILTER (WHERE edge_family = 'relation'),
       count(*) FILTER (WHERE edge_family = 'reference')
  FROM public.entity_edges
 WHERE valid_until IS NULL AND superseded_by IS NULL
  INTO v_rel, v_ref;

RAISE NOTICE '0180 backfill: % relational facts projected, % edges written '
             '(insert or coalesce), % parked, % self-referencing skipped. '
             'entity_edges now open: relation=% reference=%',
             v_src, v_edges, v_parked, v_self, v_rel, v_ref;

END $$;
