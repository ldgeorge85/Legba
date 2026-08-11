-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0145_backfill_entity_edges_from_promoted_edges.sql
--
-- K-G1 step 4b — project the PROMOTED `proposed_edges` into `entity_edges`.
--
-- ONLY `status='promoted'` CROSSES OVER. This is a decision, not an oversight:
-- `proposed_edges` holds 236,124 rows of which 174,241 are `pending`, 100 % are
-- untyped `co_occurs`, and the reifier drains them at ~0.13 % of their arrival
-- rate. They are a CANDIDATE QUEUE, not a graph, and absorbing them would
-- recreate the co-mention hairball inside the store meant to replace it. The
-- queue stays where it is.
--
-- `proposed_edges` IS NOT TOUCHED — no status flips, no deletes. Additive only.
--
-- ── measured expectations (live substrate, 2026-08-03, read-only) ──────────
-- Promoted rows 9,449. Resolvable on BOTH endpoints: 8,660 (91.65 %).
--
--   | outcome              |  rows | share  |
--   |----------------------|------:|-------:|
--   | clean                | 8,660 | 91.65 %|
--   | ambiguous (parked)   |   325 |  3.44 %|
--   | unresolved (parked)  |   469 |  4.96 %|
--   | self-referencing     |     8 |  0.08 %|
--
-- The clean rows collapse to 8,552 DISTINCT id triples — 108 name pairs that a
-- merge has since made the same edge. That collapse is the point.
--
-- Of those 8,552, roughly 8,122 coalesce onto edges 0144 already wrote from the
-- governance nexuses (see the overlap note below) and ~430 are new, taking the
-- `cooccurrence` tier from 8,286 to 8,716. Expected end state across both
-- backfills: relation 686 · reference 2,963 · cooccurrence 8,716 = 12,365 open
-- edges, with 1,290 parked rows (496 from 0144, 794 here).
--
-- Resolution is materially worse here (91.65 %) than for nexuses (96.10 %)
-- because promoted candidates are older and name entities the GC has since
-- removed. Parked, adjudicable, not guessed.
--
-- ── the overlap is deliberate ──────────────────────────────────────────────
-- 8,277 of the 8,571 open `proposed_edge_governance` nexuses match a promoted
-- candidate by name triple: governance promotes a candidate BY writing a nexus,
-- so 0144 and this file are two projections of largely the same edges. They MUST
-- converge rather than double-count, which is why `co_occurs` is canonicalized
-- to `co occurs with` — the exact surface `_canonical_rel_type` gives the nexus
-- path — so both land on the same `uq_entity_edges_open` key and coalesce. The
-- ON CONFLICT SUMS `observed_count`: an edge asserted by both the candidate and
-- the promotion it produced has genuinely been seen twice.
--
-- `edge_family` is `cooccurrence` for the untyped `co_occurs` rows (all of them
-- today) and `relation` for anything typed, so a future typed promotion is
-- classified correctly without a schema change.
--
-- SAFETY (idempotent, additive, forward-only): re-running coalesces through the
-- same ON CONFLICT paths. The runner wraps this file in its own transaction.

DO $$
DECLARE
    v_clean int; v_parked int; v_self int; v_edges int; v_coo int;
BEGIN

CREATE TEMP TABLE _bf_pe ON COMMIT DROP AS
WITH nm AS MATERIALIZED (
    SELECT lower(ep.canonical_name)                          AS key,
           count(DISTINCT public.resolve_entity(ep.id))      AS n,
           (array_agg(DISTINCT public.resolve_entity(ep.id)))[1] AS rid
      FROM public.entity_profiles ep
     GROUP BY 1
)
SELECT pe.id, pe.source_entity, pe.target_entity, pe.confidence,
       pe.evidence_text, pe.derived_from, pe.analyst_id, pe.analyst_version,
       pe.run_id, pe.target_id, pe.target_version, pe.produced_at,
       pe.created_at,
       -- one canonical surface, so these converge with the governance-written
       -- nexus edges from 0144 instead of shadowing them
       CASE WHEN lower(pe.relationship_type) IN ('co_occurs', 'cooccurswith',
                                                 'co-occurs', 'co occurs with')
            THEN 'co occurs with' ELSE lower(pe.relationship_type) END AS rel_type,
       CASE WHEN lower(pe.relationship_type) IN ('co_occurs', 'cooccurswith',
                                                 'co-occurs', 'co occurs with')
            THEN 'cooccurrence' ELSE 'relation' END                    AS fam,
       COALESCE(s.n, 0) AS s_n, s.rid AS s_id,
       COALESCE(o.n, 0) AS o_n, o.rid AS o_id
  FROM public.proposed_edges pe
  LEFT JOIN nm s ON s.key = lower(btrim(pe.source_entity))
  LEFT JOIN nm o ON o.key = lower(btrim(pe.target_entity))
 WHERE pe.status = 'promoted';

-- 1. The residue, parked first.
INSERT INTO public.entity_edges_unresolved
    (src_text, dst_text, edge_type, edge_family, reason, origin_table,
     origin_id, payload)
SELECT left(b.source_entity, 2048), left(b.target_entity, 2048),
       b.rel_type, b.fam,
       CASE WHEN b.s_n > 1 OR b.o_n > 1 THEN 'ambiguous'
            WHEN b.s_n = 0              THEN 'src_unresolved'
            ELSE                             'dst_unresolved' END,
       'proposed_edges', b.id,
       jsonb_build_object('src_matches', b.s_n, 'dst_matches', b.o_n,
                          'analyst_id', b.analyst_id, 'backfill', '0145')
  FROM _bf_pe b
 WHERE b.s_n <> 1 OR b.o_n <> 1
    ON CONFLICT (origin_table, origin_id) WHERE origin_id IS NOT NULL
    DO UPDATE SET reason  = EXCLUDED.reason,
                  payload = EXCLUDED.payload;
GET DIAGNOSTICS v_parked = ROW_COUNT;

-- 2. The edges.
CREATE TEMP TABLE _bf_pe_clean ON COMMIT DROP AS
SELECT * FROM _bf_pe WHERE s_n = 1 AND o_n = 1 AND s_id <> o_id;

SELECT count(*) FROM _bf_pe WHERE s_n = 1 AND o_n = 1 AND s_id = o_id
  INTO v_self;
SELECT count(*) FROM _bf_pe_clean INTO v_clean;

INSERT INTO public.entity_edges (
    src_id, dst_id, edge_type, edge_family, polarity, intent, channel,
    confidence, valid_from, observed_count, first_seen_at, last_seen_at,
    source_signal_ids, derived_from, evidence_set,
    source_type, analyst_id, analyst_version, run_id,
    target_id, target_version, produced_at
)
SELECT p.s_id, p.o_id, p.rel_type, p.fam,
       -- bare co-occurrence is neutral by construction: two entities in one
       -- document is not an alignment
       0, '', 'direct',
       a.conf, a.first_at, a.n_rows, a.first_at, a.last_at,
       COALESCE(sg.arr, '{}'::uuid[]), COALESCE(dv.arr, '{}'::uuid[]),
       CASE WHEN p.evidence_text <> ''
            THEN jsonb_build_object('evidence_text', left(p.evidence_text, 1200),
                                    'promoted_from_proposed_edge', p.id::text)
            ELSE jsonb_build_object('promoted_from_proposed_edge', p.id::text)
       END,
       'agent', p.analyst_id, p.analyst_version, p.run_id,
       p.target_id, p.target_version, p.produced_at
  FROM (
      SELECT DISTINCT ON (s_id, o_id, rel_type) *
        FROM _bf_pe_clean
       ORDER BY s_id, o_id, rel_type, confidence DESC, produced_at DESC, id
  ) p
  JOIN (
      SELECT s_id, o_id, rel_type,
             count(*)::int                          AS n_rows,
             max(confidence)                        AS conf,
             min(COALESCE(produced_at, created_at)) AS first_at,
             max(COALESCE(produced_at, created_at)) AS last_at
        FROM _bf_pe_clean GROUP BY 1, 2, 3
  ) a  ON a.s_id = p.s_id AND a.o_id = p.o_id AND a.rel_type = p.rel_type
  LEFT JOIN (
      SELECT c.s_id, c.o_id, c.rel_type, array_agg(DISTINCT x) AS arr
        FROM _bf_pe_clean c, unnest(c.derived_from) x
       GROUP BY 1, 2, 3
  ) sg ON sg.s_id = p.s_id AND sg.o_id = p.o_id AND sg.rel_type = p.rel_type
  LEFT JOIN (
      SELECT c.s_id, c.o_id, c.rel_type,
             array_agg(DISTINCT x) AS arr
        FROM _bf_pe_clean c, unnest(c.derived_from || ARRAY[c.id]) x
       GROUP BY 1, 2, 3
  ) dv ON dv.s_id = p.s_id AND dv.o_id = p.o_id AND dv.rel_type = p.rel_type
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
        evidence_set   = COALESCE(entity_edges.evidence_set, EXCLUDED.evidence_set),
        updated_at     = now();
GET DIAGNOSTICS v_edges = ROW_COUNT;

SELECT count(*) FROM public.entity_edges
 WHERE edge_family = 'cooccurrence'
   AND valid_until IS NULL AND superseded_by IS NULL
  INTO v_coo;

RAISE NOTICE '0145 backfill: % promoted candidates projected, % edges written '
             '(insert or coalesce), % parked, % self-referencing skipped. '
             'entity_edges cooccurrence now open: %',
             v_clean + v_parked + v_self, v_edges, v_parked, v_self, v_coo;

END $$;
