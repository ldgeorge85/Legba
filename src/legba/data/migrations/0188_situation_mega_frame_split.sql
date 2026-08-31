-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0188_situation_mega_frame_split.sql
--
-- #64 — THE MEGA-FRAME SPLIT. Re-key the stored fleet onto the dimensioned
-- situation signature and SPLIT the country-absorbing frames the old key built.
--
-- ── THE DEFECT ─────────────────────────────────────────────────────────────
-- `finding_supersession` derived a topic-only key (`_SITUATION_SIGNATURE_
-- ENTITY_K = 0` → `sig:<topic>`), and `_topic` resolves a unit finding's topic
-- to its `category`, which for a country unit IS the target id. Every dimension
-- on a desk therefore clustered into ONE situation, and because `situations` is
-- keyed `(situation_signature, analyst_id)` with that analyst_id being the
-- CLUSTERING handler's — one value fleet-wide — the signature carried the whole
-- of a frame's identity. The DB has confirmed the shape twice: the FRAME
-- program's read (2026-08-20) found exactly one open frame per desk across all
-- 33 desks (`sig:country_g20_ar` 377 events, `sig:country_g20_us` 436,
-- `sig:country_watch_cd` 367), and at the H1 census the AR frame stood at 364
-- members of which 42 were the maritime-pilots story. The register is the right
-- instrument with the wrong unit of identity: a frame whose unit is "a country"
-- cannot answer what is happening in that country with more than one row.
--
-- ── THE FORWARD FIX, AND WHY THIS FILE IS STILL NEEDED ─────────────────────
-- The signature now carries the producing dimension —
-- `sig:<topic>#dim:<analyst_id>` (`finding_supersession.with_dimension`). Both
-- live readers normalize a key of any vintage onto that form, so the mega-frame
-- stops being FED the moment the code deploys, with or without this migration.
-- What the code cannot do is fix what is already STORED: the mega-frame ROW
-- keeps the old key, stops being upserted (nothing groups under that key any
-- more), and freezes at whatever `status`/`intensity` it last held — active, at
-- intensity up to 60, forever, in the table every register read pulls from. This
-- migration re-homes those rows.
--
-- ── WHAT IT DOES, IN ORDER ─────────────────────────────────────────────────
-- (1) Re-stamps `analyst_outputs.situation_signature` on derived-key findings
--     inside the clustering horizon, so the stored column agrees with what the
--     live path computes instead of relying on the read-time normalization.
-- (2) Re-stamps the matching `finding_supersessions` audit rows, so a link row
--     keeps naming the signature its cluster actually has.
-- (3) SPLITS each open mega-frame into one row per producing dimension.
--
-- ── THE SPLIT RULE ─────────────────────────────────────────────────────────
-- Members come from `situations.derived_from` — the materialized member array,
-- which IS the attachment relation the rest of the tower joins on (the tracker's
-- `_NEW_EVIDENCE_SQL`, `hypothesis_lifecycle`). Each member is bucketed by its
-- producing `analyst_id`, folded to the same token `dimension_token()` computes
-- in Python (the two spellings are asserted equal row-for-row by
-- `test_dimension_token_python_and_sql_agree`; a token the two spell differently
-- is a duplicate frame under `uq_situations_signature_analyst`, which is the one
-- drift that would silently undo this repair). A member whose producing row is
-- gone, or which never carried an analyst_id, buckets to `_unattributed` and
-- gets its own frame — an unattributed read is a fact about our bookkeeping, not
-- evidence about somebody else's dimension.
--
-- THE PARENT ROW KEEPS ITS ID, and that is the whole reason there are no
-- orphaned trajectory rows. `situation_events` is append-only ENFORCED — 0184
-- installs triggers that fail loud on both UPDATE and DELETE — so a ledger row
-- cannot be re-homed, only left where it is. So the parent id survives and one
-- dimension inherits it: the dimension that produced the PLURALITY OF THE
-- EVIDENCE THE LEDGER ACTUALLY CITED, tie-broken by member count and then by
-- token, so the ledger stays attached to the sub-frame its own rows are about.
-- (`hypotheses.situation_id` points here too and is likewise never disturbed.)
-- Every other dimension gets a NEW row carrying `data.trajectory_parent_id`, so
-- a reader can always walk from a split frame to the ledger it came out of.
--
-- ── WHAT THE SPLIT CONSERVES ───────────────────────────────────────────────
-- MEMBERS, exactly: the buckets partition `derived_from`, duplicates included,
-- so the cardinalities sum to the original. `event_count` is re-based onto that
-- array rather than being carried or apportioned; for a frame clipped at
-- `_MAX_MEMBERS` (500) that RESTATES a count that was already a count of
-- something else, and the next 20-minute tick re-derives it from the live
-- lookback regardless.
-- INTENSITY MASS, by construction: each row takes the parent's
-- `intensity_score` scaled by its share of the members. A frame that was paging
-- at 22 does not become seven frames paging at 22 for the twenty minutes before
-- the next tick re-derives them — which is the one way this migration could have
-- manufactured an alert storm out of a bookkeeping change.
-- THE OPENING: a child inherits the parent's `valid_from` rather than taking its
-- own earliest member. The split does not reset any frame's clock — the history
-- is the parent's history and the child is a partition of it, not a new
-- situation — and under H1 that is also the conservative direction: an
-- uncorroborated child decays from the true opening instead of looking young.
--
-- ── SCOPE ──────────────────────────────────────────────────────────────────
-- OPEN frames only (`superseded_by IS NULL AND status <> 'closed'`) — the H1
-- census reads 17 active / 32 dormant / 40 closed, so this is the 49 rows every
-- live register read actually pulls. A closed frame is settled history and its
-- legacy key is a correct record of what was materialized at the time; re-keying
-- history is what the CREATE-only policy exists to prevent. The visible
-- consequence, stated rather than discovered later: should a closed legacy frame
-- pick up a fresh member, it re-opens as new dimensioned rows instead of
-- flipping its own status back. That is the honest outcome — the thing that
-- would have re-opened was the desk-blob, and the desk-blob is what this file
-- is retiring.
--
-- ── IDEMPOTENT ─────────────────────────────────────────────────────────────
-- Every statement is guarded by the ABSENCE of the `#dim:` marker, which every
-- statement adds. A second run selects nothing. (1) and (2) are additionally
-- guarded by `IS DISTINCT FROM`; (3) inserts under `ON CONFLICT DO NOTHING` on
-- the real unique index and re-keys the parent only when the target key is free,
-- so a partially-shaped substrate degrades to "left alone", never to a duplicate
-- or a constraint violation. On a fresh cold-start substrate every statement
-- matches zero rows.

-- ---------------------------------------------------------------------------
-- (1) The findings' stored signature column.
--
-- Bounded to 120 days: both `finding_supersession` and `situation_clustering`
-- read a 30-day lookback, so nothing older can re-enter a cluster, and an
-- unbounded rewrite of an append-heavy table inside a deploy transaction is a
-- lock this change has no need to take.
-- ---------------------------------------------------------------------------
UPDATE analyst_outputs
   SET situation_signature = situation_signature
       || '#dim:'
       || CASE
            WHEN btrim(coalesce(analyst_id, ''), E' \t\n\r\f\v') = ''
              THEN '_unattributed'
            ELSE left(
                   translate(lower(btrim(analyst_id, E' \t\n\r\f\v')), '#|', '__'),
                   64)
          END
 WHERE kind = 'finding'
   AND situation_signature LIKE 'sig:%'
   AND position('#dim:' in situation_signature) = 0
   AND produced_at > now() - INTERVAL '120 days';

-- ---------------------------------------------------------------------------
-- (2) The supersession audit trail, keyed off the SUPERSEDED row's producer.
--     Both rows of a link share an analyst_id by construction (`_cluster`
--     partitions on it), so either end gives the same token.
-- ---------------------------------------------------------------------------
UPDATE finding_supersessions fs
   SET situation_signature = fs.situation_signature
       || '#dim:'
       || CASE
            WHEN btrim(coalesce(ao.analyst_id, ''), E' \t\n\r\f\v') = ''
              THEN '_unattributed'
            ELSE left(
                   translate(lower(btrim(ao.analyst_id, E' \t\n\r\f\v')), '#|', '__'),
                   64)
          END
  FROM analyst_outputs ao
 WHERE ao.id = fs.superseded_finding_id
   AND fs.situation_signature LIKE 'sig:%'
   AND position('#dim:' in fs.situation_signature) = 0;

-- ---------------------------------------------------------------------------
-- (3) The split itself — ONE statement, so the INSERT of the new dimensions and
--     the re-key of the parent cannot half-happen and cannot see each other's
--     rows (they must not: the keeper dimension is by construction none of the
--     inserted ones, and a guard that could see them would be reading its own
--     write).
--
-- The CTE chain, read top to bottom:
--   parents  — the open, derived-key frames still on the pre-#64 signature.
--   members  — their member ids, each carrying its producer's dimension token
--              and the columns a provisional child row needs.
--   buckets  — one row per (frame, dimension): the member array and its shape.
--   cited    — how many of the frame's LEDGER-CITED findings each dimension
--              produced. This is what decides which dimension inherits the id.
--   shaped   — buckets + the keeper flag, one deterministic keeper per frame.
--   ins      — the non-keeper dimensions, as new rows.
-- and the trailing UPDATE re-keys the parent in place.
-- ---------------------------------------------------------------------------
WITH parents AS (
    SELECT s.id, s.situation_signature, s.analyst_id, s.derived_from,
           s.intensity_score, s.status, s.name, s.last_event_at,
           s.valid_from, s.valid_until, s.target_id, s.target_version,
           s.analyst_version, s.schema_uri, s.run_id, s.data,
           cardinality(s.derived_from) AS member_total
      FROM situations s
     WHERE s.situation_signature LIKE 'sig:%'
       AND position('#dim:' in s.situation_signature) = 0
       AND s.superseded_by IS NULL
       AND s.status <> 'closed'
       AND cardinality(s.derived_from) > 0
),
members AS (
    SELECT p.id AS parent_id,
           m.member_id,
           m.ord,
           CASE
             WHEN btrim(coalesce(ao.analyst_id, ''), E' \t\n\r\f\v') = ''
               THEN '_unattributed'
             ELSE left(
                    translate(lower(btrim(ao.analyst_id, E' \t\n\r\f\v')),
                              '#|', '__'),
                    64)
           END AS dim,
           ao.produced_at,
           ao.title
      FROM parents p
      CROSS JOIN LATERAL unnest(p.derived_from) WITH ORDINALITY AS m(member_id, ord)
      LEFT JOIN analyst_outputs ao ON ao.id = m.member_id
),
buckets AS (
    SELECT parent_id,
           dim,
           array_agg(member_id ORDER BY ord)              AS member_ids,
           count(*)                                       AS member_count,
           max(produced_at)                               AS newest_at,
           (array_agg(title ORDER BY produced_at DESC NULLS LAST, member_id DESC)
              FILTER (WHERE title IS NOT NULL))[1]        AS newest_title
      FROM members
     GROUP BY parent_id, dim
),
cited AS (
    SELECT m.parent_id, m.dim, count(*) AS cited_count
      FROM situation_events e
      CROSS JOIN LATERAL unnest(e.derived_from) AS c(cited_id)
      JOIN members m
        ON m.parent_id = e.situation_id AND m.member_id = c.cited_id
     WHERE e.delta <> 'unchanged_checkpoint'
     GROUP BY m.parent_id, m.dim
),
shaped AS (
    SELECT p.id, p.situation_signature, p.analyst_id, p.intensity_score,
           p.status, p.name, p.last_event_at, p.valid_from, p.valid_until,
           p.target_id, p.target_version, p.analyst_version, p.schema_uri,
           p.run_id, p.data, p.member_total,
           b.dim,
           b.member_ids,
           b.member_count,
           b.newest_at,
           b.newest_title,
           (b.dim = (
               SELECT b2.dim
                 FROM buckets b2
                 LEFT JOIN cited c2
                        ON c2.parent_id = b2.parent_id AND c2.dim = b2.dim
                WHERE b2.parent_id = p.id
                ORDER BY coalesce(c2.cited_count, 0) DESC,
                         b2.member_count DESC,
                         b2.dim
                LIMIT 1
           )) AS is_keeper
      FROM parents p
      JOIN buckets b ON b.parent_id = p.id
),
-- (3a) The NON-keeper dimensions become new rows. `ON CONFLICT DO NOTHING` on
--      the real partial unique index: if a dimensioned frame somehow already
--      exists for this key the live upsert owns it and this file must not touch
--      it.
ins AS (
INSERT INTO situations
    (id, data, name, status, category, last_event_at, event_count,
     intensity_score, target_id, target_version, analyst_id, analyst_version,
     produced_at, derived_from, schema_uri, run_id,
     situation_signature, valid_from, valid_until)
SELECT gen_random_uuid(),
       -- The parent's payload minus everything that described the PARENT's
       -- evidence clock: a child has no ledger rows of its own, and carrying
       -- `last_corroborated_at` onto a frame the ledger has never moved is
       -- exactly the substitution H1 exists to forbid. The next tick recomputes
       -- them honestly.
       (b.data
          - 'last_corroborated_at' - 'corroboration_count'
          - 'persistence' - 'evidence_anchor_at')
         || jsonb_build_object(
              'situation_signature', b.situation_signature || '#dim:' || b.dim,
              'member_finding_ids', to_jsonb(
                  ARRAY(SELECT x::text FROM unnest(b.member_ids) AS x)),
              'dimension', b.dim,
              'trajectory_parent_id', b.id::text,
              'split_from', jsonb_build_object(
                  'migration', '0188',
                  'parent_situation_id', b.id::text,
                  'parent_signature', b.situation_signature,
                  'parent_member_count', b.member_total,
                  'split_share', round(
                      b.member_count::numeric / nullif(b.member_total, 0), 6),
                  'dimension', b.dim)),
       left(coalesce(nullif(btrim(b.newest_title), ''), b.name), 512),
       b.status,
       -- `category` is re-derived exactly as `_topic_from_signature` does it:
       -- the topic BEFORE the dimension marker, so `_target_for_category` keeps
       -- resolving the country home for every split frame.
       btrim(split_part(substring(b.situation_signature from 5), '|', 1)),
       coalesce(b.newest_at, b.last_event_at),
       b.member_count,
       (b.intensity_score * b.member_count::double precision
            / nullif(b.member_total, 0))::real,
       b.target_id, b.target_version, b.analyst_id, b.analyst_version,
       now(), b.member_ids, b.schema_uri, b.run_id,
       b.situation_signature || '#dim:' || b.dim,
       b.valid_from, b.valid_until
  FROM shaped b
 WHERE NOT b.is_keeper
ON CONFLICT (situation_signature, analyst_id)
    WHERE situation_signature IS NOT NULL
DO NOTHING
RETURNING 1
)
-- (3b) The keeper dimension re-keys the PARENT row in place — same id, same
--      ledger, same hypotheses, same `created_at`. Guarded by NOT EXISTS so a
--      key already claimed by a live row is left to that row; the parent then
--      keeps its legacy key and the next tick materializes the dimension
--      normally (a stranded blob, not a corrupted one).
UPDATE situations s
   SET situation_signature = b.situation_signature || '#dim:' || b.dim,
       derived_from        = b.member_ids,
       event_count         = b.member_count,
       intensity_score     = (b.intensity_score * b.member_count::double precision
                                 / nullif(b.member_total, 0))::real,
       name                = left(
           coalesce(nullif(btrim(b.newest_title), ''), b.name), 512),
       last_event_at       = coalesce(b.newest_at, b.last_event_at),
       data                = b.data || jsonb_build_object(
           'situation_signature', b.situation_signature || '#dim:' || b.dim,
           'member_finding_ids', to_jsonb(
               ARRAY(SELECT x::text FROM unnest(b.member_ids) AS x)),
           'dimension', b.dim,
           'split_from', jsonb_build_object(
               'migration', '0188',
               'parent_situation_id', b.id::text,
               'parent_signature', b.situation_signature,
               'parent_member_count', b.member_total,
               -- The share of the parent's members (and therefore of its
               -- intensity) this row kept. Statement (4) re-bases the hypothesis
               -- plane against exactly this number, so it is STORED rather than
               -- recomputed: twenty minutes after this migration the next
               -- clustering tick re-derives `event_count` and the factor would
               -- no longer be recoverable from the row.
               'split_share', round(
                   b.member_count::numeric / nullif(b.member_total, 0), 6),
               'dimension', b.dim,
               'kept_ledger', true)),
       updated_at          = now()
  FROM shaped b
 WHERE s.id = b.id
   AND b.is_keeper
   -- `ins` is deliberately not referenced: a data-modifying CTE always runs to
   -- completion whether or not the primary query reads it, and referencing it
   -- here would only invite the misreading that the UPDATE can see its rows.
   AND NOT EXISTS (
       SELECT 1 FROM situations o
        WHERE o.situation_signature = b.situation_signature || '#dim:' || b.dim
          AND o.analyst_id IS NOT DISTINCT FROM b.analyst_id
          AND o.id <> s.id
   );

-- ---------------------------------------------------------------------------
-- (4) THE HYPOTHESIS PLANE — a semantics-migration guard, not an enhancement.
--
-- THE HAZARD, and it is the largest thing this file could have broken.
-- `hypothesis_lifecycle._test_standing_hypotheses` adjudicates a standing
-- hypothesis by comparing the frame's intensity NOW against the
-- `intensity_at_emit` it snapshotted when the hypothesis was minted:
--
--     _classify_move(intensity_now, base)   # _INTENSITY_MOVE_EPS = 0.25
--
-- A negative move past that epsilon appends every later member finding to
-- `refuting_signals`, and the balance drives refutation. Statement (3) divides a
-- split frame's intensity by its share of the members — a country frame at 60
-- lands near 7 — so EVERY standing hypothesis on EVERY split frame would read a
-- ~50-point collapse with no world event behind it and refute itself on the next
-- lifecycle tick. 4,405 live hypotheses carry a `situation_id`. That is a
-- mass-refutation event manufactured by a bookkeeping change, and it is exactly
-- the class 0187 guarded for the banding plane and nobody had guarded here.
--
-- THE FIX is the conservation property the rest of this migration already has,
-- applied one table further out: re-base the stored snapshot by the SAME share
-- the frame kept, so `intensity_now - intensity_at_emit` compares like with like
-- and the split is invisible to the lifecycle. Nothing is re-adjudicated and no
-- verdict moves; the comparison simply stops straddling a scale change.
--
-- The pre-split value is KEPT alongside (`intensity_at_emit_pre_0188`) rather
-- than overwritten — an operator has to be able to see that the number was
-- restated and what it was.
--
-- Only the KEEPER rows matter: hypotheses point at `situations.id`, and the
-- keeper is the only split row that has one. Idempotent via `rebased_by`.
-- ---------------------------------------------------------------------------
UPDATE hypotheses h
   SET diagnostic_evidence = (
           SELECT jsonb_agg(
                      CASE
                        WHEN jsonb_typeof(a.e) = 'object'
                             AND a.e ? 'intensity_at_emit'
                             AND NOT (a.e ? 'rebased_by')
                          THEN a.e || jsonb_build_object(
                                 'intensity_at_emit',
                                 to_jsonb(round(
                                     (a.e->>'intensity_at_emit')::numeric
                                       * f.share, 4)),
                                 'intensity_at_emit_pre_0188',
                                 a.e->'intensity_at_emit',
                                 'rebased_by', '0188')
                        ELSE a.e
                      END
                      ORDER BY a.ord
                  )
             FROM jsonb_array_elements(h.diagnostic_evidence)
                  WITH ORDINALITY AS a(e, ord)
       ),
       updated_at = now()
  FROM (
      SELECT s.id,
             (s.data->'split_from'->>'split_share')::numeric AS share
        FROM situations s
       WHERE s.data->'split_from'->>'migration' = '0188'
         AND s.data->'split_from'->>'kept_ledger' = 'true'
  ) f
 WHERE h.situation_id = f.id
   AND f.share IS NOT NULL
   AND f.share < 1
   AND jsonb_typeof(h.diagnostic_evidence) = 'array'
   AND EXISTS (
       SELECT 1
         FROM jsonb_array_elements(h.diagnostic_evidence) AS e
        WHERE jsonb_typeof(e) = 'object'
          AND e ? 'intensity_at_emit'
          AND NOT (e ? 'rebased_by')
   );

COMMENT ON COLUMN situations.situation_signature IS
    'The clustering key. Derived keys carry their producing dimension since '
    '#64: sig:<topic>[|<entities>]#dim:<analyst_id>. The topic before the '
    'marker is what resolves target_id; migration 0188 split the '
    'country-absorbing frames the pre-#64 topic-only key built.';
