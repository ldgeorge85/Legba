-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0143_entity_edges.sql
--
-- K-G1 step 1 — the id-keyed edge store. THE unanimous irreversible from the
-- three-way graph debate (planning/graph_debate/JUDGE_SYNTHESIS.md §4.1): A, B
-- and C proposed three different engines and the same table. This file is that
-- table, plus its unresolved-endpoint park, plus the merge-fold function. It
-- creates NO readers and NO writers — `entity_edges` lands empty and stays empty
-- until 0144/0145 backfill it and the dual-write lands. Existing readers stay on
-- `nexuses` for the whole of this tranche.
--
-- WHY ids and not names. Every edge surface in this system is keyed by TEXT:
-- `nexuses.subject/object`, `proposed_edges.source_entity/target_entity`,
-- `facts.subject/value`. A name is not a key — the entity uniqueness index is
-- (lower(canonical_name), entity_class), so one lowered name can name several
-- profiles, and a merge TOMBSTONES the loser rather than deleting it, so every
-- name-keyed row that named the loser silently keeps naming a tombstone. There
-- is no repoint path for `proposed_edges` at all. `entity_edges` replaces the
-- string with `entity_profiles.id` behind a REAL foreign key, which is what
-- makes the orphan class structurally impossible rather than swept.
--
-- ── the four tiers (edge_family) ───────────────────────────────────────────
-- The load-bearing column, and the judge's amendment to B's three-way split.
-- Measured on the live substrate: 86 % of the OPEN SIGNED edge set is imported
-- Wikidata country->IGO membership at polarity +1, so `structural_balance` is
-- currently reporting a balance ratio that is overwhelmingly a statement about
-- which countries co-belong to the UN, Interpol and the OPCW. Filing the seed
-- lattice as `relation` would carry that defect into the new store forever.
--   * relation     — Legba's OWN derived typed edges (`relationship_reifier`,
--                    rel_type <> co-occurrence). The world graph proper.
--   * reference    — the imported seed lattice (wikidata / sipri /
--                    world_baseline / manual). True, static, and NOT evidence of
--                    world-state alignment: excluded from signed/balance
--                    analytics by default.
--   * cooccurrence — auto-promoted co-mention (`proposed_edge_governance` and
--                    promoted `proposed_edges`). Two entities appearing in the
--                    same document is not a relationship: OFF by default.
--   * structural   — reserved for a later fold of the bearing/echo edge class.
-- The CHECK is the closed vocabulary; `vocabulary_entries` carries the same four
-- values as the registry the rest of the system reads.
--
-- ── what is NOT here ───────────────────────────────────────────────────────
-- 1. Polymorphic endpoints. A's `src_kind/src_id/dst_kind/dst_id` shape cannot
--    carry a foreign key — verified against the house's own precedent,
--    `bearing_edges`, which has none. `signal_entity_links` already has a uuid
--    FK with ON DELETE CASCADE and is the one edge table with zero orphans;
--    absorbing it would be a downgrade. entity->entity only, real FKs.
-- 2. Candidates. `proposed_edges` stays a candidate queue. Only `promoted` rows
--    cross over (0145), as `cooccurrence`.
-- 3. Readers. Not one. See the file header.
--
-- SAFETY (idempotent, additive, forward-only): CREATE TABLE/INDEX IF NOT EXISTS
-- and CREATE OR REPLACE FUNCTION only. The one DROP is `idx_nexuses_open_triple`,
-- a fully redundant three-column prefix of the four-column UNIQUE
-- `idx_nexuses_triple_open` on the same table and predicate (debate finding #21,
-- severity P3) — dropping it removes a duplicate write cost and no read path.
-- The runner wraps this file in its own transaction and records it in
-- `legba_data_migrations`; no inline BEGIN/COMMIT (same as 0107/0122).

-- ---------------------------------------------------------------------------
-- 1. entity_edges — the id-keyed edge store
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS public.entity_edges (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),

    -- IDENTITY. The whole point of the table. CASCADE on the endpoints is what
    -- makes a dangling edge unrepresentable. The intermediary is a weaker claim
    -- ("via X") and degrades to NULL rather than taking the edge with it.
    --
    -- STANDING CONTRACT ON THIS CASCADE: any code path that hard-DELETEs an
    -- entity_profiles row MUST count the edges it destroys into its receipt
    -- (`edges_cascaded`) BEFORE deleting. A silent cascade is exactly the
    -- silent-absence class this codebase is eliminating. As of this migration
    -- there is NO such runtime path to instrument — `entity_gc` deletes only
    -- `signal_entity_links`, and the profile deletes live in one-off migrations
    -- (0063, 0076) and `scripts/backfill_entity_canonicalization.py`. The
    -- requirement is recorded here rather than implemented against a caller
    -- that does not exist; whoever adds the first runtime deleter owns it.
    src_id            uuid NOT NULL
                      REFERENCES public.entity_profiles(id) ON DELETE CASCADE,
    dst_id            uuid NOT NULL
                      REFERENCES public.entity_profiles(id) ON DELETE CASCADE,
    intermediary_id   uuid
                      REFERENCES public.entity_profiles(id) ON DELETE SET NULL,

    -- TYPE + TIER.
    edge_type         text NOT NULL,
    edge_family       text NOT NULL
                      CONSTRAINT entity_edges_family_ck
                      CHECK (edge_family IN
                             ('relation', 'reference', 'cooccurrence', 'structural')),
    polarity          smallint NOT NULL DEFAULT 0
                      CONSTRAINT entity_edges_polarity_ck
                      CHECK (polarity IN (-1, 0, 1)),
    intent            text NOT NULL DEFAULT '',
    channel           text NOT NULL DEFAULT 'direct',

    -- TEMPORAL. Same supersession contract as `facts`/`nexuses`: an edge is
    -- OPEN when valid_until IS NULL AND superseded_by IS NULL. Nothing is
    -- deleted; a fold supersedes, it does not remove.
    valid_from        timestamptz,
    valid_until       timestamptz,
    superseded_by     uuid REFERENCES public.entity_edges(id) ON DELETE SET NULL,

    -- OBSERVATION. `observed_count` + `last_seen_at` make decay EVIDENTIAL — an
    -- edge decays because nobody reported it again, which is what decay is
    -- supposed to mean — rather than a function of row age.
    confidence        real NOT NULL DEFAULT 0.5,
    observed_count    int  NOT NULL DEFAULT 1,
    first_seen_at     timestamptz NOT NULL DEFAULT now(),
    last_seen_at      timestamptz NOT NULL DEFAULT now(),

    -- EVIDENCE. No edge in the system is citable today. `source_signal_ids`
    -- gives the verify/judge plane a per-edge handle; `derived_from` carries the
    -- originating nexus_id / fact_id / proposed_edge_id; `evidence_set` carries
    -- free-form corroboration (e.g. a promoted candidate's evidence_text).
    source_signal_ids uuid[] NOT NULL DEFAULT '{}',
    derived_from      uuid[] NOT NULL DEFAULT '{}',
    evidence_set      jsonb,

    -- PROVENANCE. The standard house envelope, same column names as `nexuses`.
    source_type       text NOT NULL DEFAULT 'agent',
    seed_batch_id     uuid,
    analyst_id        text,
    analyst_version   text,
    run_id            uuid,
    target_id         text,
    target_version    text,
    schema_uri        text NOT NULL
                      DEFAULT 'iglu:legba/entity_edge/jsonschema/1-0-0',
    data              jsonb NOT NULL DEFAULT '{}',
    produced_at       timestamptz NOT NULL DEFAULT now(),
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now(),

    -- An entity is not related to itself. The merge fold relies on this being
    -- enforced: rather than repoint an edge into a self-loop it closes it.
    CONSTRAINT entity_edges_no_self CHECK (src_id <> dst_id)
);

-- One OPEN edge per (src, dst, type, intermediary). This is the writer's
-- idempotency handle and the fold's collision key. Closed/superseded history is
-- outside the predicate, so an edge may recur across time without colliding.
CREATE UNIQUE INDEX IF NOT EXISTS uq_entity_edges_open
    ON public.entity_edges
    (src_id, dst_id, edge_type,
     COALESCE(intermediary_id, '00000000-0000-0000-0000-000000000000'::uuid))
    WHERE valid_until IS NULL AND superseded_by IS NULL;

-- Ego-graph reads, both directions. The IN-side index is the one
-- `proposed_edges` never had (debate finding #20: its only index is the
-- source-side unique triple, so an undirected multi-hop walk degrades).
CREATE INDEX IF NOT EXISTS idx_entity_edges_out
    ON public.entity_edges (src_id, edge_family, confidence DESC)
    WHERE valid_until IS NULL AND superseded_by IS NULL;
CREATE INDEX IF NOT EXISTS idx_entity_edges_in
    ON public.entity_edges (dst_id, edge_family, confidence DESC)
    WHERE valid_until IS NULL AND superseded_by IS NULL;

-- Signed-graph analytics (structural balance). Family-agnostic by index; the
-- CALLER filters the family, which is how balance stops measuring the IGO
-- lattice.
CREATE INDEX IF NOT EXISTS idx_entity_edges_signed
    ON public.entity_edges (src_id, dst_id)
    WHERE polarity <> 0 AND valid_until IS NULL AND superseded_by IS NULL;

-- "which edges cite signal S" — the per-edge citation handle.
CREATE INDEX IF NOT EXISTS idx_entity_edges_evidence
    ON public.entity_edges USING gin (source_signal_ids);

-- Watermark reads for any future projector/gauge.
CREATE INDEX IF NOT EXISTS idx_entity_edges_watermark
    ON public.entity_edges (updated_at);

-- Evidential decay sweep: "open edges nobody has re-reported since T".
CREATE INDEX IF NOT EXISTS idx_entity_edges_decay
    ON public.entity_edges (last_seen_at)
    WHERE valid_until IS NULL AND superseded_by IS NULL;

-- Family reads and the backfill's own receipt counts.
CREATE INDEX IF NOT EXISTS idx_entity_edges_family
    ON public.entity_edges (edge_family)
    WHERE valid_until IS NULL AND superseded_by IS NULL;

COMMENT ON TABLE public.entity_edges IS
    'K-G1: the id-keyed entity<->entity edge store. Endpoints are '
    'entity_profiles.id behind real FKs (never names), so a merge repoints '
    'rather than strands and an orphan edge is unrepresentable. edge_family '
    'separates Legba-derived relations from the imported seed lattice and the '
    'co-mention cloud. Readers migrate off `nexuses` in a later train.';

COMMENT ON COLUMN public.entity_edges.edge_family IS
    'relation = derived typed (the world graph proper) | reference = imported '
    'seed lattice, excluded from signed/balance analytics by default | '
    'cooccurrence = co-mention, OFF by default | structural = reserved.';

-- ---------------------------------------------------------------------------
-- 2. entity_edges_unresolved — the park. Never a silent drop.
-- ---------------------------------------------------------------------------
--
-- A backfill or a dual-write that cannot resolve an endpoint name to an entity
-- id parks the row HERE with the reason, and the source row is left untouched.
-- Nothing is guessed and nothing is dropped: an unresolvable endpoint is a
-- measurement, and it stays adjudicable later. `reason` is an open vocabulary
-- whose live values are 'src_unresolved' | 'dst_unresolved' | 'ambiguous'.

CREATE TABLE IF NOT EXISTS public.entity_edges_unresolved (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    src_text     text NOT NULL,
    dst_text     text NOT NULL,
    edge_type    text NOT NULL,
    edge_family  text NOT NULL,
    reason       text NOT NULL,
    origin_table text NOT NULL,
    origin_id    uuid,
    payload      jsonb NOT NULL DEFAULT '{}',
    created_at   timestamptz NOT NULL DEFAULT now()
);

-- Two dedup keys, because the two producers park two different things.
--   * A BACKFILL parks a specific historical row, so it dedupes by origin id:
--     a re-run refreshes rather than accumulating, and the park count stays a
--     true measurement of the residue.
--   * The DUAL-WRITE parks a NAME PAIR that does not resolve. The nexus upsert
--     may pass a row id that never lands (its ON CONFLICT branch keeps the
--     existing row), so an id key would inflate the park by one row per retry
--     of the same unresolvable pair. It dedupes on the triple instead.
CREATE UNIQUE INDEX IF NOT EXISTS uq_entity_edges_unresolved_origin
    ON public.entity_edges_unresolved (origin_table, origin_id)
    WHERE origin_id IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_entity_edges_unresolved_triple
    ON public.entity_edges_unresolved
    (origin_table, lower(src_text), lower(dst_text), lower(edge_type))
    WHERE origin_id IS NULL;

CREATE INDEX IF NOT EXISTS idx_entity_edges_unresolved_reason
    ON public.entity_edges_unresolved (reason, created_at DESC);

COMMENT ON TABLE public.entity_edges_unresolved IS
    'K-G1: endpoints that could not be resolved to an entity id — parked with a '
    'reason, never dropped and never guessed. The park rate is the honest '
    'residue of every backfill and of the dual-write.';

-- ---------------------------------------------------------------------------
-- 3. resolve_entity_name(text) — name -> terminal survivor id
-- ---------------------------------------------------------------------------
--
-- The name-keyed surfaces resolve through here and nowhere else. Two-stage, and
-- both stages matter:
--   1. match lower(canonical_name) across ALL profiles INCLUDING tombstones —
--      an edge that names a merged loser must land on the keeper, not be lost;
--   2. chase merged_into to the terminal survivor via resolve_entity() (0086,
--      cycle-safe).
-- AMBIGUITY IS NOT RESOLVED BY GUESSING. If the name resolves to more than one
-- distinct terminal id (measured live: 62 of 47,926 lowered name keys), this
-- returns NULL and the caller parks the row with reason='ambiguous'. Picking
-- "the one with more mentions" would manufacture an edge nobody asserted.
-- STABLE: no writes, same answer within a statement.

CREATE OR REPLACE FUNCTION public.resolve_entity_name(p_name text)
RETURNS uuid
LANGUAGE sql
STABLE
AS $$
    -- array_agg, not min(): Postgres has no min(uuid) aggregate.
    SELECT CASE WHEN count(DISTINCT r.rid) = 1
                THEN (array_agg(DISTINCT r.rid))[1] END
      FROM (
          SELECT public.resolve_entity(ep.id) AS rid
            FROM public.entity_profiles ep
           WHERE lower(ep.canonical_name) = lower(btrim(p_name))
      ) r;
$$;

COMMENT ON FUNCTION public.resolve_entity_name(text) IS
    'K-G1: resolve an edge endpoint NAME to the terminal surviving entity id. '
    'Matches tombstones too (so a merged loser lands on its keeper), then '
    'chases merged_into via resolve_entity(). Returns NULL when the name is '
    'unknown OR ambiguous across profiles — the caller parks, never guesses.';

-- ---------------------------------------------------------------------------
-- 4. fold_entity_edges(uuid) — the merge fold
-- ---------------------------------------------------------------------------
--
-- Called from the entity-merge path INSIDE the transaction that sets
-- `merged_into`, so a merge either repoints its edges or does not happen. Not a
-- sweep, not a cron, not best-effort: the existing `_compact_merged_edges`
-- rewrites 200 name-keyed losers per run behind `except Exception: warning`,
-- which is why stale endpoints are still outstanding.
--
-- ORDER IS LOAD-BEARING. Two schema invariants make the naive
-- "repoint-then-dedupe" sequence fail outright, and both failures are silent
-- only in a draft:
--   * `uq_entity_edges_open` is a plain (non-deferrable) unique index, so it is
--     enforced at the END OF EACH STATEMENT. Repointing first would collide the
--     instant a loser edge and a keeper edge share a triple.
--   * `entity_edges_no_self` is a table CHECK enforced on every row always, so
--     an edge between loser and keeper CANNOT be repointed at all — it must be
--     closed in place. Its endpoints stay pointing at the tombstone, which the
--     FK permits (a merge tombstones, it does not delete) and which is honest:
--     the row is closed history.
-- So: close the would-be self-edges, roll the duplicates onto their keeper,
-- supersede the duplicate losers (removing them from the partial unique index),
-- and only THEN repoint what is left.
--
-- REVERSIBILITY. Duplicates are superseded with a `superseded_by` pointer
-- rather than merged away, so `_unmerge_pair` can walk back. A string rewrite
-- cannot be reversed — the original string is gone.

CREATE OR REPLACE FUNCTION public.fold_entity_edges(p_loser uuid)
RETURNS TABLE (repointed int, superseded int, self_closed int)
LANGUAGE plpgsql
AS $$
DECLARE
    v_keeper uuid;
    v_self   int := 0;
    v_super  int := 0;
    v_point  int := 0;
BEGIN
    repointed := 0; superseded := 0; self_closed := 0;

    -- Terminal survivor, cycle-safe (0086). A loser that resolves to itself is
    -- not a tombstone yet — the caller has not set merged_into, or this is a
    -- no-op re-run.
    v_keeper := public.resolve_entity(p_loser);
    IF v_keeper IS NULL OR v_keeper = p_loser THEN
        RETURN NEXT;
        RETURN;
    END IF;

    -- (a) Close every OPEN edge that repointing would turn into a self-loop.
    --     Closed in place: the CHECK forbids the repoint, and the row is
    --     history, not a live claim.
    UPDATE public.entity_edges
       SET valid_until = now(), updated_at = now()
     WHERE valid_until IS NULL AND superseded_by IS NULL
       AND (src_id = p_loser OR dst_id = p_loser)
       AND CASE WHEN src_id = p_loser THEN v_keeper ELSE src_id END
         = CASE WHEN dst_id = p_loser THEN v_keeper ELSE dst_id END;
    GET DIAGNOSTICS v_self = ROW_COUNT;

    -- (b-d) ONE statement, because the group election must be computed exactly
    --     once and read by three consumers. `grp` partitions the OPEN edges
    --     touching EITHER endpoint by the triple they will have AFTER
    --     repointing and elects the oldest row of each group as its keeper.
    --     Both endpoints must be in scope: a loser edge (L,X) and an existing
    --     keeper edge (K,X) both land on (K,X). MATERIALIZED pins one
    --     evaluation, so the keeper elected for the roll-up is the same keeper
    --     the supersede points at.
    --
    --     `roll` folds the group's evidence onto its keeper. Evidence is SUMMED
    --     and UNIONED, never lost: observed_count adds (three sightings folded
    --     onto one edge is an edge seen three times), confidence takes the max,
    --     the signal and lineage arrays union, and the observation window widens
    --     to cover every member. It is a data-modifying CTE, which Postgres runs
    --     to completion whether or not the primary query reads it.
    --
    --     The primary UPDATE then supersedes the duplicates — which is what
    --     frees the partial unique index before the repoint in (e). The two
    --     UPDATE targets are disjoint by construction (keepers vs non-keepers),
    --     so no row is written twice in one statement.
    WITH grp AS MATERIALIZED (
        SELECT e.id,
               first_value(e.id) OVER w AS keep_id,
               e.observed_count, e.confidence, e.source_signal_ids,
               e.derived_from, e.first_seen_at, e.last_seen_at
          FROM public.entity_edges e
         WHERE e.valid_until IS NULL AND e.superseded_by IS NULL
           AND (e.src_id IN (p_loser, v_keeper)
                OR e.dst_id IN (p_loser, v_keeper)
                -- The intermediary is part of the unique key, so an edge whose
                -- ONLY link to the merge is "via the loser" can still collide
                -- with a "via the keeper" edge once (e) repoints it. It has to
                -- be in the candidate set or that collision reaches the index.
                OR e.intermediary_id IN (p_loser, v_keeper))
        WINDOW w AS (
            PARTITION BY
                CASE WHEN e.src_id = p_loser THEN v_keeper ELSE e.src_id END,
                CASE WHEN e.dst_id = p_loser THEN v_keeper ELSE e.dst_id END,
                e.edge_type,
                COALESCE(CASE WHEN e.intermediary_id = p_loser THEN v_keeper
                              ELSE e.intermediary_id END,
                         '00000000-0000-0000-0000-000000000000'::uuid)
            ORDER BY e.first_seen_at ASC, e.id ASC
        )
    ),
    dup AS (
        SELECT keep_id FROM grp GROUP BY keep_id HAVING count(*) > 1
    ),
    agg AS (
        SELECT g.keep_id,
               sum(g.observed_count)::int AS obs,
               max(g.confidence)          AS conf,
               min(g.first_seen_at)       AS first_at,
               max(g.last_seen_at)        AS last_at
          FROM grp g JOIN dup d ON d.keep_id = g.keep_id
         GROUP BY g.keep_id
    ),
    agg_sigs AS (
        SELECT g.keep_id, array_agg(DISTINCT s) AS sigs
          FROM grp g JOIN dup d ON d.keep_id = g.keep_id,
               unnest(g.source_signal_ids) s
         GROUP BY g.keep_id
    ),
    agg_derv AS (
        SELECT g.keep_id, array_agg(DISTINCT dv) AS derv
          FROM grp g JOIN dup d ON d.keep_id = g.keep_id,
               unnest(g.derived_from) dv
         GROUP BY g.keep_id
    ),
    roll AS (
        UPDATE public.entity_edges e
           SET observed_count    = a.obs,
               confidence        = a.conf,
               first_seen_at     = a.first_at,
               last_seen_at      = a.last_at,
               source_signal_ids = COALESCE(s.sigs, e.source_signal_ids),
               derived_from      = COALESCE(dv.derv, e.derived_from),
               updated_at        = now()
          FROM agg a
          LEFT JOIN agg_sigs s  ON s.keep_id  = a.keep_id
          LEFT JOIN agg_derv dv ON dv.keep_id = a.keep_id
         WHERE e.id = a.keep_id
        RETURNING e.id
    )
    UPDATE public.entity_edges e
       SET superseded_by = g.keep_id,
           valid_until   = now(),
           updated_at    = now()
      FROM grp g
     WHERE e.id = g.id AND g.id <> g.keep_id;
    GET DIAGNOSTICS v_super = ROW_COUNT;

    -- (e) Repoint what remains — open AND closed rows, so history points at the
    --     survivor too. The `<> v_keeper` guards are what keep the no-self CHECK
    --     satisfied; the rows they skip are exactly the ones (a) already closed.
    UPDATE public.entity_edges
       SET src_id = v_keeper, updated_at = now()
     WHERE src_id = p_loser AND dst_id <> v_keeper;
    GET DIAGNOSTICS v_point = ROW_COUNT;

    UPDATE public.entity_edges
       SET dst_id = v_keeper, updated_at = now()
     WHERE dst_id = p_loser AND src_id <> v_keeper;
    GET DIAGNOSTICS repointed = ROW_COUNT;
    v_point := v_point + repointed;

    -- The intermediary is a weak claim; repoint it wherever it named the loser.
    UPDATE public.entity_edges
       SET intermediary_id = v_keeper, updated_at = now()
     WHERE intermediary_id = p_loser;

    repointed   := v_point;
    superseded  := v_super;
    self_closed := v_self;
    RETURN NEXT;
END;
$$;

COMMENT ON FUNCTION public.fold_entity_edges(uuid) IS
    'K-G1: repoint a merged loser''s edges onto the terminal survivor inside the '
    'merge transaction. Closes would-be self-edges, coalesces duplicates '
    '(observed_count summed, confidence maxed, evidence arrays unioned) and '
    'supersedes the losers reversibly. Returns (repointed, superseded, '
    'self_closed) for the merge receipt.';

-- ---------------------------------------------------------------------------
-- 5. Vocabulary — the tier map, registered where the rest of the system reads
-- ---------------------------------------------------------------------------

INSERT INTO public.vocabulary_entries (family, value, notes)
VALUES
    ('edge_family', 'relation',
     'Legba-derived typed edges (relationship_reifier). The world graph proper; ON by default.'),
    ('edge_family', 'reference',
     'Imported seed lattice (wikidata/sipri/world_baseline/manual). Excluded from signed analytics by default.'),
    ('edge_family', 'cooccurrence',
     'Co-mention edges (proposed_edge_governance, promoted candidates). OFF by default.'),
    ('edge_family', 'structural',
     'Reserved for a later fold of the bearing/echo edge class.')
ON CONFLICT (family, value) DO NOTHING;

-- ---------------------------------------------------------------------------
-- 6. Hygiene — drop the redundant nexus index (debate finding #21)
-- ---------------------------------------------------------------------------
--
-- `idx_nexuses_open_triple` (lower(subject), lower(coalesce(intermediary,'')),
-- lower(object)) is a strict three-column prefix of the four-column UNIQUE
-- `idx_nexuses_triple_open` over the identical partial predicate. Postgres can
-- serve every read the former satisfies from the latter; keeping both pays a
-- second index write on every nexus insert for nothing.

DROP INDEX IF EXISTS public.idx_nexuses_open_triple;
