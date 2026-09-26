-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0205_backfill_events_from_tower.sql — DATA MODEL V3 / P0, the bounded tower
-- backfill (spec §7.2). Mints `source_method='tower'` events from the two
-- candidate sources that already sit in the substrate, signs them with the
-- Postgres twin of the Python signature (``public.event_signature`` below is
-- the same last-mile fold as ``events.signature.EVENT_SIGNATURE_SQL``), and
-- writes each event's 'opened' ledger row in the same transaction — an event
-- and its ledger open land together or not at all.
--
-- THE TWO PATHS (spec §2.4 / §7.2)
-- ------------------------------
--   Path 1 — `situation_events` rows that carry evidence:
--     delta <> 'unchanged_checkpoint' AND derived_from <> '{}'. The DB CHECK
--     already guarantees every such row carries verified findings. Signals are
--     resolved through the ledger row's finding ids (analyst_outputs ->
--     derived_from -> signals, the claim_watch lineage expansion).
--
--   Path 2 — verified findings: kind='finding', live head (superseded_by IS
--     NULL), produced within the last 30 days, verify-passed at the house
--     floor (effective_confidence = LEAST(confidence, newest faithfulness
--     critique's overall_score) >= 0.50 — the meta_findings_synthesizer fold),
--     citing >= 2 signals that still resolve. Collapsed by event_signature —
--     the yield is distinct occurrences, not findings.
--
-- MEASURED AT REVIEW (orchestrator, 2026-09-22 17:55-17:59Z, the live tower,
-- 0202-0205 applied inside one ROLLED-BACK transaction; the DO block's own
-- NOTICE is the reading):
--   path1 situation_events candidates = 1,469 · path2 findings candidates = 289
--   parked (zero resolvable signals) = 71 · parked (signature unmintable) = 0
--   DISTINCT signatures = 985 (ceiling 5,000) → events upserted = 985
--   signal_event_links = 105,801 · event_entity_links = 220,085 · 'opened' = 985
--   wall clock ≈ 4 min. Spec §7.2's 2026-09-21 prior was: path 1 = 1,436 of
-- 3,214; path 2 raw pool = 18,309 pre-bar. The hard ceiling
-- _BACKFILL_MAX_EVENTS = 5000 applies to the DISTINCT-signature yield.
--
-- The exact dry-run queries the orchestrator runs on the live tower:
--
--   -- path 1 candidates (expect ~1,436 by the 2026-09-21 measurement):
--   SELECT count(*) FROM situation_events se
--    WHERE se.delta <> 'unchanged_checkpoint'
--      AND se.derived_from <> '{}'::uuid[];
--
--   -- path 2 candidates (verify-passed findings, 30d, floor):
--   SELECT count(*) FROM analyst_outputs f
--    JOIN LATERAL (
--        SELECT (cr.data->>'overall_score')::real AS faithfulness_score
--          FROM analyst_outputs cr
--         WHERE cr.kind = 'critique'
--           AND cr.data->>'analyzed_output_id' = f.id::text
--           AND cr.data->>'overall_score' IS NOT NULL
--           AND cr.title LIKE 'Faithfulness verify%'
--         ORDER BY cr.produced_at DESC, cr.id DESC LIMIT 1
--    ) v ON TRUE
--    WHERE f.kind = 'finding' AND f.superseded_by IS NULL
--      AND f.produced_at > now() - interval '30 days'
--      AND LEAST(f.confidence, v.faithfulness_score) >= 0.50;
--
--   -- the DISTINCT-signature yield (the number the ceiling guards) is only
--   -- knowable after the signature function runs: run steps 1-4 of the DO
--   -- block below in a rolled-back transaction, then
--   --   SELECT count(*) FROM _bf0205_events;
--   -- (or comment out steps 5-10 on a scratch restore).
--
-- The DO block measures what it actually did and RAISEs it as a NOTICE at
-- the end (the 0144 idiom) — that apply-time receipt is the acceptance
-- evidence, alongside `SELECT source_method, count(*) FROM events GROUP BY 1`.
--
-- CANDIDATE-QUALITY RULES
-- -----------------------
--   * signature collapse is the dedupe: path-1 and path-2 candidates minting
--     the same event_signature land on ONE tower event with unioned evidence
--     (one producer — 'tower_backfill'); the (signature, analyst_id) key makes
--     a clustering write on the same occurrence a DELIBERATE second row.
--   * a candidate whose signature cannot be minted (no topic AND no entity
--     token) or whose evidence resolves to ZERO live signals is PARKED — the
--     source row is never touched, and the parked count + reason is reported
--     in the NOTICE (the entity_edges_unresolved precedent: never dropped,
--     counted with its why, adjudicable later).
--   * _BACKFILL_MAX_EVENTS = 5000: above it the migration RAISES with its
--     counts rather than writing (spec §7.2 — refuses rather than floods).
--
-- IDEMPOTENT: ON CONFLICT on the (signature, analyst) upsert key unions
-- derived_from and refreshes rollups; link inserts are ON CONFLICT merges;
-- 'opened' rows write only where none exists. A re-run is a repair.
--
-- ROLLBACK: `TRUNCATE event_lifecycle_events` first (its append-only trigger
-- refuses the cascade), then `DELETE FROM events WHERE source_method='tower'`
-- — the link-table cascades take the rest.

-- ---------------------------------------------------------------------------
-- 0. public.event_signature — the Postgres twin of events.signature
-- ---------------------------------------------------------------------------
--
-- The signature grammar:  evt:<topic>|<top-K folded entity tokens>#evt:<anchor>
--
-- The entity fold here is identity_fold's LAST MILE over CANONICAL surfaces
-- (entity_profiles.canonical_name): the write path already ran the alias /
-- demonym / collapse maps and the junk gate, so the twin reproduces the
-- article strip (with its never-blank guard), lowercase, non-alnum collapse,
-- the length>2 gate and the literal junk sets — and stops there, by contract.
-- The twin is asserted row-for-row against the Python builder by
-- test_event_signature_python_and_sql_agree; keep the two edits together.
-- p_polity feeds ANCHOR_TOKEN_SQL's twin verbatim (the '#evt:' slot).

CREATE OR REPLACE FUNCTION public.event_signature(
    p_topic    text,
    p_entities text[],
    p_polity   text,
    p_k        integer
)
RETURNS text
LANGUAGE sql
IMMUTABLE
AS $$
    WITH toks AS (
        SELECT DISTINCT
               regexp_replace(
                   lower(
                       CASE WHEN btrim(e.n) ~* '^(the|a|an)\y'
                             AND char_length(btrim(regexp_replace(btrim(e.n),
                                   '^(the|a|an)\y\s*', '', 'i'))) >= 2
                            THEN btrim(regexp_replace(btrim(e.n),
                                   '^(the|a|an)\y\s*', '', 'i'))
                            ELSE btrim(e.n)
                       END),
                   '[^a-z0-9]+', '', 'g'
               ) AS tok
          FROM unnest(COALESCE(p_entities, '{}'::text[])) AS e(n)
         WHERE char_length(btrim(e.n)) > 2
           AND lower(btrim(e.n)) <> ALL (ARRAY[
               'tv','radio','online',
               'the','a','an','and','or','but','nor','of','to','in','on','at',
               'by','for','from','with','as','into','onto','off','out','up',
               'down',
               'zero','one','two','three','four','five','six','seven','eight',
               'nine','ten','eleven','twelve','thirteen','fourteen','fifteen',
               'sixteen','seventeen','eighteen','nineteen','twenty','thirty',
               'forty','fifty','sixty','seventy','eighty','ninety','hundred',
               'thousand','million','billion','trillion','dozen','couple',
               'first','second','third','fourth','fifth','sixth','seventh',
               'eighth','ninth','tenth','eleventh','twelfth','thirteenth',
               'twentieth','thirtieth',
               'west','western','eastern','northern','southern',
               'islamic','islamist','leader','leaders','leadership',
               'annual','annually','yearly','daily','weekly','monthly',
               'quarterly','biannual','semiannual','biennial','resistance',
               'parl','fed',
               'hundreds','thousands','millions','billions','trillions','dozens'
           ]::text[])
    ),
    picked AS (
        SELECT tok FROM toks WHERE tok <> '' ORDER BY tok COLLATE "C"
        LIMIT GREATEST(p_k, 0)
    ),
    agg AS (
        SELECT string_agg(tok, ',' ORDER BY tok COLLATE "C") AS tail,
               (array_agg(tok ORDER BY tok COLLATE "C"))[1] AS first_tok
          FROM picked
    ),
    topic AS (
        SELECT NULLIF(lower(btrim(COALESCE(p_topic, ''))), '') AS t
    )
    SELECT CASE
               WHEN COALESCE((SELECT t FROM topic),
                             (SELECT first_tok FROM agg)) IS NULL THEN NULL
               ELSE 'evt:'
                    || COALESCE((SELECT t FROM topic),
                                (SELECT first_tok FROM agg))
                    || COALESCE('|' || (SELECT tail FROM agg), '')
                    || '#evt:'
                    || COALESCE(
                         NULLIF(
                           btrim(
                             left(
                               btrim(
                                 regexp_replace(
                                   lower(btrim(COALESCE(p_polity, ''))),
                                   '[^a-z0-9]+', '_', 'g'
                                 ),
                                 '_'
                               ),
                               64
                             ),
                             '_'
                           ),
                           ''
                         ),
                         '_domestic'
                       )
           END
$$;

COMMENT ON FUNCTION public.event_signature(text, text[], text, integer) IS
    'V3/P0: the Postgres twin of legba.data.events.signature.event_signature — '
    'mint evt:<topic>|<top-K identity-folded entity tokens>#evt:<anchor> over '
    'CANONICAL entity surfaces (identity_fold''s last mile + the literal junk '
    'sets + the length>2 gate). Asserted row-for-row against the Python '
    'builder by test_event_signature_python_and_sql_agree.';

-- ---------------------------------------------------------------------------
-- The backfill — one DO block, everything inside the migration's transaction
-- ---------------------------------------------------------------------------

DO $$
DECLARE
    _BACKFILL_MAX_EVENTS CONSTANT integer := 5000;
    v_p1_candidates    integer;
    v_p2_candidates    integer;
    v_parked_no_signal integer;
    v_parked_no_sig    integer;
    v_distinct         integer;
    v_events           integer;
    v_sig_links        integer;
    v_ent_links        integer;
    v_opened           integer;
BEGIN
    -- ---------------------------------------------------------------
    -- 1. Candidate pool (both paths, one shape)
    -- ---------------------------------------------------------------
    CREATE TEMP TABLE _bf0205_cand ON COMMIT DROP AS
    -- Path 1: evidence-bearing situation_events rows. lineage = the finding
    -- ids the ledger row cites (its derived_from).
    SELECT 'situation_events'::text AS cand_kind,
           se.id                    AS cand_id,
           lower(btrim(
               COALESCE(
                   NULLIF(split_part(
                       split_part(
                           regexp_replace(s.situation_signature, '^sig:', ''),
                           '|', 1),
                       '#', 1), ''),
                   NULLIF(s.category, ''),
                   ''
               )))                                        AS topic,
           left(se.why, 512)                              AS title,
           lower(btrim(COALESCE(s.category, '')))         AS category,
           se.occurred_at                                 AS occurred_at,
           se.derived_from                                AS lineage,
           s.target_id, s.target_version,
           se.occurred_at                                 AS produced_at,
           jsonb_build_object(
               'backfill', '0205', 'path', 'situation_events',
               'situation_id', s.id::text,
               'situation_event_id', se.id::text,
               'delta', se.delta, 'situation_name', s.name) AS data
      FROM public.situation_events se
      JOIN public.situations s ON s.id = se.situation_id
     WHERE se.delta <> 'unchanged_checkpoint'
       AND se.derived_from <> '{}'::uuid[];

    SELECT count(*) INTO v_p1_candidates FROM _bf0205_cand;

    -- Path 2: verify-passed findings, 30 days, floor, live head.
    INSERT INTO _bf0205_cand
    SELECT 'finding', f.id,
           lower(btrim(COALESCE(
               NULLIF(f.data->>'category', ''),
               NULLIF(f.data->>'topic', ''),
               NULLIF(f.data->>'situation_kind', ''),
               NULLIF(f.data->>'event_type', ''),
               NULLIF(f.data->'data'->>'category', ''),
               NULLIF(f.data->'data'->>'topic', ''),
               NULLIF(f.data->'data'->>'situation_kind', ''),
               NULLIF(f.data->'data'->>'event_type', ''),
               '')))                                        AS topic,
           left(f.title, 512)                               AS title,
           lower(btrim(COALESCE(f.data->>'category', '')))  AS category,
           f.produced_at                                    AS occurred_at,
           ARRAY[f.id]::uuid[]                              AS lineage,
           f.target_id, f.target_version,
           f.produced_at,
           jsonb_build_object(
               'backfill', '0205', 'path', 'finding',
               'finding_id', f.id::text,
               'finding_analyst', f.analyst_id,
               'effective_confidence',
                   LEAST(f.confidence, v.faithfulness_score)) AS data
      FROM public.analyst_outputs f
      JOIN LATERAL (
          SELECT (cr.data->>'overall_score')::real AS faithfulness_score
            FROM public.analyst_outputs cr
           WHERE cr.kind = 'critique'
             AND cr.data->>'analyzed_output_id' = f.id::text
             AND cr.data->>'overall_score' IS NOT NULL
             AND cr.title LIKE 'Faithfulness verify%'
           ORDER BY cr.produced_at DESC, cr.id DESC
           LIMIT 1
      ) v ON TRUE
     WHERE f.kind = 'finding'
       AND f.superseded_by IS NULL
       AND f.produced_at > now() - interval '30 days'
       AND LEAST(f.confidence, v.faithfulness_score) >= 0.50;

    SELECT count(*) - v_p1_candidates INTO v_p2_candidates FROM _bf0205_cand;

    -- ---------------------------------------------------------------
    -- 2. Resolve each candidate's signals (the claim_watch lineage
    --    expansion: seed refs + one hop through analyst_outputs / facts)
    -- ---------------------------------------------------------------
    CREATE TEMP TABLE _bf0205_signals ON COMMIT DROP AS
    WITH l0 AS (
        SELECT c.cand_id, unnest(c.lineage) AS ref FROM _bf0205_cand c
    ), l1 AS (
        SELECT l0.cand_id, unnest(ao.derived_from) AS ref
          FROM l0 JOIN public.analyst_outputs ao ON ao.id = l0.ref
        UNION
        SELECT l0.cand_id, unnest(fx.derived_from) AS ref
          FROM l0 JOIN public.facts fx ON fx.id = l0.ref
    ), refs AS (
        SELECT cand_id, ref FROM l0
        UNION SELECT cand_id, ref FROM l1
    )
    SELECT DISTINCT r.cand_id, s.id AS signal_id, s.fetched_at,
           s.source_id, s.geo
      FROM refs r
      JOIN public.signals s ON s.id = r.ref;

    -- Confidence for path-1 candidates: the max member-finding confidence
    -- (path-2 candidates carry their own effective_confidence in data).
    UPDATE _bf0205_cand c
       SET data = data || jsonb_build_object(
               'finding_confidence', sub.conf)
      FROM (
          SELECT c2.cand_id, max(ao.confidence) AS conf
            FROM _bf0205_cand c2
            CROSS JOIN LATERAL unnest(c2.lineage) AS f(id)
            JOIN public.analyst_outputs ao ON ao.id = f.id
           WHERE c2.cand_kind = 'situation_events'
           GROUP BY c2.cand_id
      ) sub
     WHERE c.cand_id = sub.cand_id;

    -- ---------------------------------------------------------------
    -- 3. Entity surfaces + anchor polity per candidate
    -- ---------------------------------------------------------------
    CREATE TEMP TABLE _bf0205_ents ON COMMIT DROP AS
    SELECT DISTINCT bs.cand_id, ep.canonical_name
      FROM _bf0205_signals bs
      JOIN public.signal_entity_links sel ON sel.signal_id = bs.signal_id
      JOIN public.entity_profiles ep
        ON ep.id = public.resolve_entity(sel.entity_id);

    CREATE TEMP TABLE _bf0205_anchor ON COMMIT DROP AS
    SELECT DISTINCT ON (cand_id) cand_id, iso2
      FROM (
          SELECT bs.cand_id, g AS iso2, count(*) AS n
            FROM _bf0205_signals bs
            CROSS JOIN LATERAL unnest(bs.geo) AS g
           WHERE btrim(g) <> ''
           GROUP BY bs.cand_id, g
      ) ranked
     ORDER BY cand_id, n DESC, iso2 ASC;

    -- ---------------------------------------------------------------
    -- 4. Sign + collapse. The signal bar is per-path: a situation_events
    --    candidate needs >= 1 resolvable signal (its CHECK'd findings are
    --    the evidence; the signals are the chain), a finding candidate needs
    --    >= 2 (spec §2.4 — "cites >= 2 resolvable signals"). A candidate
    --    under its bar, or whose signature cannot be minted, is PARKED —
    --    counted here, source row untouched (the entity_edges_unresolved
    --    precedent: never dropped, always counted with its why).
    -- ---------------------------------------------------------------
    CREATE TEMP TABLE _bf0205_signed ON COMMIT DROP AS
    SELECT c.*,
           public.event_signature(
               c.topic,
               (SELECT array_agg(e.canonical_name)
                  FROM _bf0205_ents e WHERE e.cand_id = c.cand_id),
               (SELECT a.iso2 FROM _bf0205_anchor a
                 WHERE a.cand_id = c.cand_id),
               3)                                   AS event_signature,
           (SELECT count(*) FROM _bf0205_signals s
             WHERE s.cand_id = c.cand_id)           AS n_signals
      FROM _bf0205_cand c;

    SELECT count(*) INTO v_parked_no_signal
      FROM _bf0205_signed
     WHERE (cand_kind = 'situation_events' AND n_signals < 1)
        OR (cand_kind = 'finding' AND n_signals < 2);
    SELECT count(*) INTO v_parked_no_sig
      FROM _bf0205_signed
     WHERE event_signature IS NULL
       AND NOT ((cand_kind = 'situation_events' AND n_signals < 1)
             OR (cand_kind = 'finding' AND n_signals < 2));

    CREATE TEMP TABLE _bf0205_events ON COMMIT DROP AS
    SELECT sg.event_signature,
           (array_agg(sg.title ORDER BY sg.occurred_at DESC, sg.cand_id))[1]
                                                          AS title,
           (array_agg(sg.category ORDER BY sg.occurred_at DESC, sg.cand_id))[1]
                                                          AS category,
           max(sg.occurred_at)                            AS occurred_at,
           min(sg.produced_at)                            AS first_produced_at,
           max(COALESCE(
                   (sg.data->>'effective_confidence')::real,
                   (sg.data->>'finding_confidence')::real, 0.5))
                                                          AS confidence,
           (array_agg(sg.target_id ORDER BY sg.occurred_at DESC, sg.cand_id))[1]
                                                          AS target_id,
           (array_agg(sg.target_version ORDER BY sg.occurred_at DESC, sg.cand_id))[1]
                                                          AS target_version,
           (SELECT array_agg(DISTINCT x)
              FROM _bf0205_signed s2
              CROSS JOIN LATERAL unnest(s2.lineage) AS x
             WHERE s2.event_signature = sg.event_signature) AS lineage,
           (SELECT array_agg(DISTINCT s.signal_id)
              FROM _bf0205_signed s2
              JOIN _bf0205_signals s ON s.cand_id = s2.cand_id
             WHERE s2.event_signature = sg.event_signature) AS signal_ids,
           (SELECT array_agg(DISTINCT g)
              FROM _bf0205_signed s2
              JOIN _bf0205_signals s ON s.cand_id = s2.cand_id
              CROSS JOIN LATERAL unnest(s.geo) AS g
             WHERE s2.event_signature = sg.event_signature) AS geo,
           (SELECT count(DISTINCT s.source_id)
              FROM _bf0205_signed s2
              JOIN _bf0205_signals s ON s.cand_id = s2.cand_id
             WHERE s2.event_signature = sg.event_signature) AS n_sources,
           (SELECT min(s.fetched_at)
              FROM _bf0205_signed s2
              JOIN _bf0205_signals s ON s.cand_id = s2.cand_id
             WHERE s2.event_signature = sg.event_signature) AS time_start,
           (SELECT max(s.fetched_at)
              FROM _bf0205_signed s2
              JOIN _bf0205_signals s ON s.cand_id = s2.cand_id
             WHERE s2.event_signature = sg.event_signature) AS time_end,
           jsonb_build_object(
               'backfill', '0205',
               'paths', (SELECT array_agg(DISTINCT s2.cand_kind)
                           FROM _bf0205_signed s2
                          WHERE s2.event_signature = sg.event_signature),
               'sources', (SELECT array_agg(s2.cand_id::text ORDER BY s2.cand_id::text)
                             FROM _bf0205_signed s2
                            WHERE s2.event_signature = sg.event_signature),
               'details', jsonb_agg(sg.data ORDER BY sg.occurred_at DESC))
                                                          AS data
      FROM _bf0205_signed sg
     WHERE sg.event_signature IS NOT NULL
       AND ((sg.cand_kind = 'situation_events' AND sg.n_signals >= 1)
         OR (sg.cand_kind = 'finding' AND sg.n_signals >= 2))
     GROUP BY sg.event_signature;

    SELECT count(*) INTO v_distinct FROM _bf0205_events;

    -- ---------------------------------------------------------------
    -- 5. The ceiling — refuse rather than flood (spec §7.2)
    -- ---------------------------------------------------------------
    IF v_distinct > _BACKFILL_MAX_EVENTS THEN
        RAISE EXCEPTION
            '0205 backfill refuses: % distinct event signatures exceeds '
            '_BACKFILL_MAX_EVENTS=% (path1 candidates=%, path2 candidates=%, '
            'parked no-signals=%, parked no-signature=%). Tighten the bars or '
            'raise the ceiling deliberately — do not let this flood.',
            v_distinct, _BACKFILL_MAX_EVENTS, v_p1_candidates,
            v_p2_candidates, v_parked_no_signal, v_parked_no_sig;
    END IF;

    -- ---------------------------------------------------------------
    -- 6. Write events (upsert on the signature key), capture id map
    -- ---------------------------------------------------------------
    CREATE TEMP TABLE _bf0205_eid ON COMMIT DROP AS
    WITH ins AS (
        INSERT INTO public.events (
            event_signature, analyst_id, title, summary, category,
            event_type, severity,
            lifecycle_state, lifecycle_changed_at,
            time_start, time_end, geo,
            confidence, signal_count, distinct_source_count, oversized,
            valid_from, source_method, source_type, derived_from,
            target_id, target_version, analyst_version, data, produced_at
        )
        SELECT e.event_signature, 'tower_backfill', e.title, '', e.category,
               'incident', 'medium',
               'emerging', e.occurred_at,
               e.time_start, e.time_end,
               COALESCE(e.geo, '{}'::text[]),
               e.confidence,
               COALESCE(cardinality(e.signal_ids), 0),
               COALESCE(e.n_sources, 0),
               false,
               e.time_start, 'tower', 'agent',
               (SELECT array_agg(DISTINCT x)
                  FROM unnest(COALESCE(e.lineage,'{}'::uuid[])
                              || COALESCE(e.signal_ids,'{}'::uuid[])) AS x),
               e.target_id, e.target_version, '0205',
               e.data, now()
          FROM _bf0205_events e
        ON CONFLICT (event_signature, analyst_id) DO UPDATE SET
            derived_from = (
                SELECT array_agg(DISTINCT x)
                  FROM unnest(events.derived_from || EXCLUDED.derived_from) AS x),
            signal_count = GREATEST(events.signal_count, EXCLUDED.signal_count),
            distinct_source_count = GREATEST(events.distinct_source_count,
                                             EXCLUDED.distinct_source_count),
            confidence = GREATEST(events.confidence, EXCLUDED.confidence),
            data = events.data || EXCLUDED.data,
            updated_at = now()
        RETURNING id, event_signature
    )
    SELECT event_signature, id AS event_id FROM ins;

    SELECT count(*) INTO v_events FROM _bf0205_eid;

    -- ---------------------------------------------------------------
    -- 7. signal_event_links — evidence edges at EVIDENCE time
    --    (linked_at = the signal's fetched_at), source axes denormalized
    -- ---------------------------------------------------------------
    INSERT INTO public.signal_event_links
        (signal_id, event_id, linked_at, relevance,
         source_class, source_kind, source_id)
    SELECT DISTINCT s.signal_id, m.event_id, s.fetched_at, 1.0,
           CASE WHEN sd.body->'scope'->>'source_class' IN
                     ('reporting','analysis','official','state_media')
                THEN sd.body->'scope'->>'source_class'
                ELSE 'reporting' END,
           COALESCE(sd.kind, ''),
           s.source_id
      FROM _bf0205_signed sg
      JOIN _bf0205_signals s ON s.cand_id = sg.cand_id
      JOIN _bf0205_eid m ON m.event_signature = sg.event_signature
      LEFT JOIN public.source_descriptors sd
             ON sd.descriptor_id = s.source_id AND sd.is_head
     WHERE sg.event_signature IS NOT NULL
       AND ((sg.cand_kind = 'situation_events' AND sg.n_signals >= 1)
         OR (sg.cand_kind = 'finding' AND sg.n_signals >= 2))
    ON CONFLICT (signal_id, event_id) DO NOTHING;

    GET DIAGNOSTICS v_sig_links = ROW_COUNT;

    -- ---------------------------------------------------------------
    -- 8. event_entity_links — actors, resolve_entity() terminal ids
    -- ---------------------------------------------------------------
    INSERT INTO public.event_entity_links
        (event_id, entity_id, role, confidence, derived_from)
    SELECT m.event_id, public.resolve_entity(sel.entity_id), 'actor',
           max(sel.confidence),
           array_agg(DISTINCT s.signal_id)
      FROM _bf0205_signed sg
      JOIN _bf0205_signals s ON s.cand_id = sg.cand_id
      JOIN public.signal_entity_links sel ON sel.signal_id = s.signal_id
      JOIN _bf0205_eid m ON m.event_signature = sg.event_signature
     WHERE sg.event_signature IS NOT NULL
       AND ((sg.cand_kind = 'situation_events' AND sg.n_signals >= 1)
         OR (sg.cand_kind = 'finding' AND sg.n_signals >= 2))
     GROUP BY m.event_id, public.resolve_entity(sel.entity_id)
    ON CONFLICT (event_id, entity_id, role) DO UPDATE SET
        confidence = GREATEST(event_entity_links.confidence, EXCLUDED.confidence),
        derived_from = (
            SELECT array_agg(DISTINCT x)
              FROM unnest(event_entity_links.derived_from
                          || EXCLUDED.derived_from) AS x);

    GET DIAGNOSTICS v_ent_links = ROW_COUNT;

    -- ---------------------------------------------------------------
    -- 9. 'opened' ledger rows — every event opens its ledger, once
    -- ---------------------------------------------------------------
    INSERT INTO public.event_lifecycle_events
        (event_id, occurred_at, transition, state_from, state_to,
         why, derived_from, analyst_id, analyst_version)
    SELECT m.event_id, e.occurred_at, 'opened', 'emerging', 'emerging',
           left(e.title, 2000),
           (SELECT array_agg(DISTINCT x)
              FROM unnest(COALESCE(e.lineage,'{}'::uuid[])
                          || COALESCE(e.signal_ids,'{}'::uuid[])) AS x),
           'tower_backfill', '0205'
      FROM _bf0205_eid m
      JOIN _bf0205_events e ON e.event_signature = m.event_signature
     WHERE NOT EXISTS (
         SELECT 1 FROM public.event_lifecycle_events l
          WHERE l.event_id = m.event_id AND l.transition = 'opened');

    GET DIAGNOSTICS v_opened = ROW_COUNT;

    -- ---------------------------------------------------------------
    -- 10. The receipt — measured, not expected (0144 idiom)
    -- ---------------------------------------------------------------
    RAISE NOTICE '0205 tower backfill: path1 situation_events candidates=%, '
                 'path2 findings candidates=%, parked (zero resolvable '
                 'signals)=%, parked (signature unmintable)=%, distinct '
                 'signatures=% (ceiling %), events upserted=%, '
                 'signal_event_links=%, event_entity_links=%, '
                 '''opened'' ledger rows=%',
        v_p1_candidates, v_p2_candidates, v_parked_no_signal,
        v_parked_no_sig, v_distinct, _BACKFILL_MAX_EVENTS, v_events,
        v_sig_links, v_ent_links, v_opened;
END $$;
