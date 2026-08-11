-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0160_retire_below_bar_proposed_edges.sql
--
-- K-G2 RETENTION. The pending co-mention queue is ~176,000 rows and grows
-- ~9,941/day, and 92.1% of it rests on a SINGLE independent source — two names
-- in one article, which is the null hypothesis, not a relationship. Under the
-- qualification bar this migration's sibling change installs
-- (`edge_qualification`, bar 0.42 with a hard floor of
-- 2 independent sources), only ~12,000 of those rows can
-- ever earn a typing call. The rest are not work; they are sediment, and they
-- were never going to be looked at.
--
-- The policy (docs/TYPING_BAKEOFF_2026-08-03.md §2.6, §7.4):
--   * above the bar               -> KEEP (it is the work queue);
--   * below the bar, < 30 days -> KEEP (a slow-burning story gets a
--                                    month to earn a second source);
--   * below the bar, >= 30 days -> RETIRE.
--
-- "Stale" is measured from the NEWEST BACKING SIGNAL, not from row creation, so
-- a candidate that gains support restarts its clock. That is why this cannot be
-- the existing `_reject_stale_thin` rule, which ages out on `produced_at` and
-- on raw `confidence` alone: a well-corroborated row that never promoted, or any
-- row above confidence 0.45 and below the promote threshold, is never aged out
-- by it at all.
--
-- RETIRE, NOT DELETE. `status = 'retired'` is a NEW terminal value beside
-- pending / promoted / rejected / orphaned, following the `entity_gc` precedent:
-- mint a status outside the pending work-set, flip it, stamp `reviewed_at`,
-- retain the row. Deliberately NOT 'rejected' — that means a human or the
-- governance pass refused the pair. Retirement is weaker and different: the pair
-- never earned enough independent support to be worth a GPU call and stopped
-- accruing. Folding the two together would erase the distinction, and the
-- distinction is the audit value. The co-mention evidence stays addressable and
-- a pair that re-earns support returns through the normal producer path.
--
-- `proposed_edges.status` carries no CHECK constraint (0001_baseline.sql:647 —
-- bare `text NOT NULL DEFAULT 'pending'`), so no DDL is needed. Every read path
-- that matters is already safe: the reifier's selection and `entity_gc`'s
-- orphan quarantine filter `status = 'pending'`, and `entities_api` filters
-- `status = 'promoted'`.
--
-- MEASURED READ-ONLY IMMEDIATELY BEFORE WRITING (2026-08-03, live substrate):
--   pending rows                                       176,168
--   of those, below-bar AND >= 30 days stale (retire)     34,870
-- The K-G2 report measured 34,548 earlier the same day; the pool moves
-- continuously (only `pending` grows), so the exact count at apply time will
-- differ again. The RULE is fixed; the count is whatever the rule selects.
--
-- The statement below is generated verbatim by
-- `edge_qualification.retirement_update_sql()` — a migration cannot import
-- Python, so it is inlined, and
-- `tests/data_pkg/test_reifier_retention.py::test_migration_sql_is_the_module_sql`
-- pins the two byte-for-byte. The recurring `proposed_edge_governance` age-out
-- leg calls the SAME generator with a per-run LIMIT, so the one-shot and the
-- ongoing rule cannot drift apart.
--
-- Idempotent: the UPDATE re-asserts `status = 'pending'` and the inner select
-- only reads pending rows, so a second run matches 0. On a fresh substrate with
-- an empty queue it is a clean no-op.

UPDATE proposed_edges
   SET status = 'retired', reviewed_at = now()
 WHERE status = 'pending'
   AND id IN (
SELECT r.id FROM (
SELECT s.id, s.source_entity, s.target_entity, s.independent_sources,
       s.qual_score, s.age_days
  FROM (
WITH desk_geo AS (
    SELECT coalesce(array_agg(DISTINCT g), '{}') AS codes
      FROM target_descriptors td
      CROSS JOIN LATERAL jsonb_array_elements_text(
          coalesce(td.body->'scope'->'geo', '[]'::jsonb)) AS g
     WHERE td.is_head AND td.state = 'active' AND td.abstraction_level = 'L1'
), desk_subject AS (
    SELECT lower(btrim(
             CASE WHEN td.name LIKE '%—%'
                  THEN reverse(split_part(reverse(td.name), '—', 1))
                  ELSE td.name END)) AS subject
      FROM target_descriptors td
     WHERE td.is_head AND td.state = 'active' AND td.abstraction_level = 'L1'
), desk_names AS (
    SELECT subject FROM desk_subject WHERE subject <> ''
    UNION
    SELECT btrim(split_part(subject, ',', 1)) FROM desk_subject
     WHERE subject LIKE '%,%' AND btrim(split_part(subject, ',', 1)) <> ''
), cand AS (
    SELECT pe.id, pe.source_entity, pe.target_entity, pe.confidence,
           pe.produced_at, pe.derived_from
      FROM proposed_edges pe
     WHERE pe.status = 'pending'
), expanded AS (
    SELECT c.id AS cid, s.source_id, s.geo, s.fetched_at,
           coalesce(nullif(s.content_hash, ''), s.canonical_signal_id::text,
                    s.id::text) AS content_key
      FROM cand c
      CROSS JOIN LATERAL unnest(c.derived_from) AS d(sig_id)
      JOIN signals s ON s.id = d.sig_id
), dedup AS (
    SELECT DISTINCT ON (cid, content_key)
           cid, content_key, source_id, geo, fetched_at
      FROM expanded ORDER BY cid, content_key, fetched_at ASC
), support AS (
    SELECT d.cid,
           count(DISTINCT d.source_id) AS independent_sources,
           -- source_id is 'source.<publisher>.<feed>'; the PUBLISHER is
           -- segment 2, so aljazeera.arabic + aljazeera.world fold to one.
           count(DISTINCT split_part(d.source_id, '.', 2)) AS source_families,
           max(d.fetched_at) AS newest_signal_at,
           bool_or(d.geo && (SELECT codes FROM desk_geo)) AS desk_geo_hit
      FROM dedup d GROUP BY d.cid
), mentions AS (
    SELECT lower(ep.canonical_name) AS name, count(*) AS n
      FROM signal_entity_links sel
      JOIN entity_profiles ep ON ep.id = sel.entity_id
     GROUP BY 1
), evidence AS (
    SELECT c.id, c.source_entity, c.target_entity, c.confidence, c.produced_at,
           coalesce(sp.independent_sources, 0)  AS independent_sources,
           coalesce(sp.source_families, 0)      AS source_families,
           least(coalesce(ms.n, 0), coalesce(mo.n, 0)) AS weaker_mentions,
           coalesce(sp.desk_geo_hit, false)     AS desk_geo_hit,
           (lower(c.source_entity) IN (SELECT subject FROM desk_names)
            OR lower(c.target_entity) IN (SELECT subject FROM desk_names))
                                                AS desk_entity_hit,
           EXTRACT(EPOCH FROM (now() - coalesce(sp.newest_signal_at,
                                                c.produced_at))) / 86400.0
                                                AS age_days
      FROM cand c
      LEFT JOIN support sp  ON sp.cid = c.id
      LEFT JOIN mentions ms ON ms.name = lower(c.source_entity)
      LEFT JOIN mentions mo ON mo.name = lower(c.target_entity)
)
SELECT e.*, (0.45 * (CASE WHEN independent_sources <= 1 THEN 0.0 ELSE least(1.0, (independent_sources - 1)::numeric / 3.0) END) + 0.2 * (CASE WHEN independent_sources <= 1 THEN 0.0 ELSE least(source_families, independent_sources)::numeric / independent_sources END) + 0.2 * (CASE WHEN weaker_mentions <= 0 THEN 0.0 ELSE least(1.0, ln(1.0 + weaker_mentions) / ln(501.0)) END) + 0.15 * (CASE WHEN desk_entity_hit THEN 1.0 WHEN desk_geo_hit THEN 0.6 ELSE 0.0 END)) AS qual_score
  FROM evidence e
) AS s
 WHERE NOT (s.independent_sources >= 2 AND s.qual_score >= 0.42)
   AND s.age_days >= 30

) AS r
);
