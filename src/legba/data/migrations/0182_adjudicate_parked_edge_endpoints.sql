-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0182_adjudicate_parked_edge_endpoints.sql
--
-- W3-A step 4 — adjudicate `entity_edges_unresolved`, the park where every
-- backfill and the dual-write record an endpoint they could not resolve.
--
-- 0143 built the park on the principle that "an unresolvable endpoint is a
-- MEASUREMENT, and it stays adjudicable later". This file is the "later": it
-- makes the park SELF-DRAINING (a parked row whose endpoints have since become
-- resolvable is minted as a real edge and the park row retired) and it retires
-- rows that are no longer adjudicable at all.
--
-- ── the adjudication, measured live read-only 2026-08-03 ───────────────────
-- 1,286 parked rows, and the honest headline is that only one of the three
-- classes is mechanically actionable — the other two are entity-resolution
-- work that this migration must NOT pretend to do.
--
--   | class                     |  rows | disposition                        |
--   |---------------------------|------:|------------------------------------|
--   | resolves exactly NOW      |     0 | MINT the edge, retire the park row |
--   | ambiguous                 |   480 | residual — reported, never guessed |
--   | dead name, punctuation    |    29 | residual — NOT auto-resolved       |
--   | dead name, no near match  |   777 | residual — reported                |
--
-- WHY ZERO RESOLVE TODAY, and why the mechanism ships anyway. Every park row
-- was written by a producer that had already tried `resolve_entity_name()`, and
-- nothing since (including the V-G6 merge repairs) has minted a profile under
-- any of these names. So the re-adjudication is a no-op on this substrate — but
-- it is the correct STANDING mechanism: a park row becomes resolvable the
-- moment an entity is created or merged under its name, and without this the
-- park only ever grows. It runs idempotently on every deploy.
--
-- ── the 480 AMBIGUOUS rows are not what the name suggests ──────────────────
-- 28 distinct name keys drive all 480. Inspecting them, almost none is a
-- genuine two-referents-one-name collision; they are CROSS-CLASS DUPLICATE
-- PROFILES — the same real thing minted twice because the uniqueness index is
-- (lower(canonical_name), entity_class):
--
--   "golan heights"   -> Golan Heights (location) + the Golan Heights (person)
--   "trump"           -> Donald Trump (entity)    + Trump (person)
--   "charles iii"     -> two live profiles, location + person
--   "the czech republic" -> Czechia (country)     + the Czech Republic (location)
--
-- That is the V-G6 defect class (one Israeli officer became four rows), and the
-- fix is an ENTITY MERGE, not an edge-endpoint decision. Picking a side here
-- would manufacture an edge nobody asserted — exactly what 0143's
-- `resolve_entity_name` returns NULL to prevent. Reported, left parked.
--
-- ── the 806 DEAD names, and why 29 of them stay parked on purpose ──────────
-- 169 name keys match no profile at all. Of those, a handful differ from a
-- live profile only by the NER tokenizer's hyphen spacing:
--
--   "choe son-hui"  vs  "Choe Son - hui"
--   "mercedes-benz" vs  "Mercedes - Benz"
--   "al-tanf"       vs  "al - Tanf"
--
-- 29 parked rows would resolve if that normalization were applied. They are NOT
-- resolved here. This is the transliteration class, and the W3-C adjudication
-- measured 68.3 % precision on it — a bulk apply would silently mint wrong
-- edges at roughly one in three, and an edge minted wrongly is worse than an
-- edge parked honestly. The count is REPORTED so the entity-resolution train
-- can size the work; the decision belongs there, per-candidate.
--
-- ── retention ─────────────────────────────────────────────────────────────
-- A park row is adjudicable only while the assertion behind it still stands.
-- Two cases retire, both measuring 0 today and both certain to accrue:
--   * the origin row was hard-deleted — there is nothing left to adjudicate;
--   * the origin assertion was WITHDRAWN (its nexus closed or superseded, its
--     proposed_edge left `promoted`), so the park row records a claim the
--     substrate no longer makes.
-- Dual-write parks (origin_id IS NULL) are NOT retired by age here: they dedupe
-- on the name triple and refresh `created_at` on every retry, so a TTL is the
-- right tool and it is registered in `retention_policies` rather than hard-coded
-- — DISABLED by default, because deleting a measurement is an operator call.
--
-- SAFETY (idempotent, forward-only): the mint path goes through the same ON
-- CONFLICT key as every other backfill, so a re-run coalesces. The runner wraps
-- this file in its own transaction.

DO $$
DECLARE
    v_minted int; v_drained int; v_stale int;
    v_ambig int; v_dead int; v_punct int; v_total int;
BEGIN

-- ---------------------------------------------------------------------------
-- 1. Re-adjudicate: which parked rows resolve on BOTH endpoints today?
-- ---------------------------------------------------------------------------
CREATE TEMP TABLE _park ON COMMIT DROP AS
WITH nm AS MATERIALIZED (
    SELECT lower(ep.canonical_name)                          AS key,
           count(DISTINCT public.resolve_entity(ep.id))      AS n,
           (array_agg(DISTINCT public.resolve_entity(ep.id)))[1] AS rid
      FROM public.entity_profiles ep
     GROUP BY 1
)
SELECT u.id, u.src_text, u.dst_text, u.edge_type, u.edge_family,
       u.reason, u.origin_table, u.origin_id,
       COALESCE(s.n, 0) AS s_n, s.rid AS s_id,
       COALESCE(d.n, 0) AS d_n, d.rid AS d_id
  FROM public.entity_edges_unresolved u
  LEFT JOIN nm s ON s.key = lower(btrim(u.src_text))
  LEFT JOIN nm d ON d.key = lower(btrim(u.dst_text));

SELECT count(*) FROM _park INTO v_total;

-- Mint the edges that can now be minted. `edge_family` is carried from the park
-- row — it was classified by the producer at park time and that classification
-- does not become wrong just because the endpoint later resolved.
INSERT INTO public.entity_edges (
    src_id, dst_id, edge_type, edge_family, polarity, intent, channel,
    confidence, observed_count, evidence_set, source_type
)
SELECT p.s_id, p.d_id, p.edge_type, p.edge_family, 0, '', 'direct',
       0.5, 1,
       jsonb_build_object('recovered_from_park', p.id::text,
                          'origin_table', p.origin_table,
                          'parked_reason', p.reason),
       'agent'
  FROM (
      SELECT DISTINCT ON (s_id, d_id, lower(edge_type)) *
        FROM _park
       WHERE s_n = 1 AND d_n = 1 AND s_id <> d_id
       ORDER BY s_id, d_id, lower(edge_type), id
  ) p
    ON CONFLICT (src_id, dst_id, edge_type,
                 COALESCE(intermediary_id,
                          '00000000-0000-0000-0000-000000000000'::uuid))
       WHERE valid_until IS NULL AND superseded_by IS NULL
    DO UPDATE SET
        observed_count = entity_edges.observed_count + 1,
        last_seen_at   = now(),
        updated_at     = now();
GET DIAGNOSTICS v_minted = ROW_COUNT;

-- Drain every park row that resolved — INCLUDING the ones whose endpoints now
-- resolve to the SAME entity. A merge has made that pair a self-reference; it
-- is settled, not pending, and leaving it parked would overstate the residue
-- forever.
DELETE FROM public.entity_edges_unresolved u
 USING _park p
 WHERE u.id = p.id AND p.s_n = 1 AND p.d_n = 1;
GET DIAGNOSTICS v_drained = ROW_COUNT;

-- ---------------------------------------------------------------------------
-- 2. Retention — retire rows that are no longer ADJUDICABLE.
-- ---------------------------------------------------------------------------
DELETE FROM public.entity_edges_unresolved u
 WHERE u.origin_id IS NOT NULL
   AND (
        -- the origin row is gone entirely
        (u.origin_table = 'nexuses'
         AND NOT EXISTS (SELECT 1 FROM public.nexuses n WHERE n.id = u.origin_id))
     OR (u.origin_table = 'proposed_edges'
         AND NOT EXISTS (SELECT 1 FROM public.proposed_edges e WHERE e.id = u.origin_id))
     OR (u.origin_table = 'facts'
         AND NOT EXISTS (SELECT 1 FROM public.facts f WHERE f.id = u.origin_id))
        -- or the assertion behind it was WITHDRAWN
     OR (u.origin_table = 'nexuses'
         AND EXISTS (SELECT 1 FROM public.nexuses n
                      WHERE n.id = u.origin_id
                        AND (n.valid_until IS NOT NULL
                             OR n.superseded_by IS NOT NULL)))
     OR (u.origin_table = 'proposed_edges'
         AND EXISTS (SELECT 1 FROM public.proposed_edges e
                      WHERE e.id = u.origin_id AND e.status <> 'promoted'))
     OR (u.origin_table = 'facts'
         AND EXISTS (SELECT 1 FROM public.facts f
                      WHERE f.id = u.origin_id
                        AND (f.valid_until IS NOT NULL
                             OR f.superseded_by IS NOT NULL)))
   );
GET DIAGNOSTICS v_stale = ROW_COUNT;

-- A TTL for the dual-write parks, registered rather than hard-coded and OFF by
-- default: they refresh `created_at` on every retry of the same unresolvable
-- pair, so a stale one means the producer stopped asserting it — but deleting a
-- measurement is an operator decision, not a migration's.
INSERT INTO public.retention_policies
    (policy_name, table_name, ttl_days, batch_size, enabled, env_fallback_var,
     description)
VALUES (
    'entity_edges_unresolved_retention', 'entity_edges_unresolved', 0, 5000,
    false, 'LEGBA_EDGE_PARK_TTL_DAYS',
    'TTL purge of aged DUAL-WRITE park rows (origin_id IS NULL) in '
    'entity_edges_unresolved. Those rows dedupe on the name triple and refresh '
    'created_at on every retry, so an aged one means the producer stopped '
    'asserting that pair. Backfill parks (origin_id NOT NULL) are NOT covered — '
    'they are retired by origin-row liveness in migration 0182, not by age. OFF '
    'by default: the park is a measurement of resolution quality, and deleting '
    'it hides the thing it exists to show. W3-A / migration 0182.')
ON CONFLICT (policy_name) DO NOTHING;

-- ---------------------------------------------------------------------------
-- 3. The residual — measured, named, and left alone.
-- ---------------------------------------------------------------------------
SELECT count(*) FILTER (WHERE s_n > 1 OR d_n > 1),
       count(*) FILTER (WHERE (s_n = 0 OR d_n = 0))
  FROM _park p
 WHERE EXISTS (SELECT 1 FROM public.entity_edges_unresolved u WHERE u.id = p.id)
  INTO v_ambig, v_dead;

-- The transliteration/punctuation class, COUNTED not applied: a parked row
-- whose unresolved endpoint matches exactly one live profile once the NER
-- tokenizer's hyphen spacing is normalized away. W3-C measured 68.3 % precision
-- on this class, so a bulk apply would mint a wrong edge roughly one time in
-- three. Sized here for the entity-resolution train; decided there.
WITH nz AS (
    SELECT lower(btrim(regexp_replace(regexp_replace(
               btrim(ep.canonical_name),
               '[[:space:]]*-[[:space:]]*', '-', 'g'),
               '[[:space:]]+', ' ', 'g'))) AS key,
           count(DISTINCT public.resolve_entity(ep.id)) AS n
      FROM public.entity_profiles ep
     GROUP BY 1
)
SELECT count(*)
  FROM public.entity_edges_unresolved u
  LEFT JOIN nz zs ON zs.key = lower(btrim(regexp_replace(regexp_replace(
                       btrim(u.src_text), '[[:space:]]*-[[:space:]]*', '-', 'g'),
                       '[[:space:]]+', ' ', 'g')))
  LEFT JOIN nz zd ON zd.key = lower(btrim(regexp_replace(regexp_replace(
                       btrim(u.dst_text), '[[:space:]]*-[[:space:]]*', '-', 'g'),
                       '[[:space:]]+', ' ', 'g')))
 WHERE u.reason <> 'ambiguous'
   AND COALESCE(zs.n, 0) = 1 AND COALESCE(zd.n, 0) = 1
  INTO v_punct;

RAISE NOTICE '0182 park adjudication: % parked on entry — % edges minted, '
             '% rows drained as resolved, % retired as no-longer-adjudicable. '
             'RESIDUAL: % ambiguous (cross-class duplicate profiles — an ENTITY '
             'MERGE decision, never an endpoint guess), % dead-name, of which % '
             'are the punctuation/transliteration class (counted, NOT applied: '
             'W3-C measured 68.3%% precision).',
             v_total, v_minted, v_drained, v_stale, v_ambig, v_dead, v_punct;

END $$;
