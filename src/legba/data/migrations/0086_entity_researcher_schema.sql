-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0086_entity_researcher_schema.sql
--
-- The SCHEMA FOUNDATION for the `entity_researcher` (E2b + E5 of
-- planning/MASTER_PLAN_2026-07-10.md). This migration only lands the DDL
-- surfaces the researcher writes against; the analyst, the blocking/candidate
-- generator (E3), and the write-time canonicalization code land separately.
--
-- WHAT (four pieces):
--
--   * `entity_alias`      (E2b) — the write-time canonicalization surface. Today
--                          folded surface forms are stashed ad-hoc in the keeper
--                          row's `data->'merged_aliases'` JSON array (probed by a
--                          jsonb-containment scan in entity_resolution.py /
--                          _entity_resolve.py). That JSON blob is a NO-SCHEMA alias
--                          table: it cannot carry per-alias provenance (who decided,
--                          which model, what confidence) and it cannot be indexed for
--                          an O(1) write-time probe. This table PROMOTES that surface
--                          to a real, indexable, provenance-carrying relation keyed on
--                          the normalized surface form → the canonical entity id.
--
--   * `entity_judgement`  (E2b) — the pairwise-verdict CACHE. The researcher's LLM
--                          adjudicates gray-band candidate pairs (same / not_same /
--                          unsure). Without a cache, an adjudicated pair whose losing
--                          surface keeps re-appearing at ingest would be re-sent to the
--                          LLM every pass (the "junk regenerating" cost). Keyed on a
--                          DETERMINISTIC, order-independent `pair_key` (the two
--                          normalized surfaces sorted + joined, so (A,B) == (B,A)), a
--                          verdict is recorded ONCE and re-read cheaply on every later
--                          pass. This is the nomenklatura / decision-cache pattern.
--
--   * `entity_profiles.merged_into` (E5) — the tombstone+redirect column. A MERGE does
--                          NOT rewrite or delete the loser: it stamps the loser's
--                          `merged_into` = the survivor id. The loser row becomes a
--                          TOMBSTONE that redirects to the survivor (the Wikidata
--                          redirect pattern). Reversible: un-merge = clear the column.
--                          Self-FK ON DELETE SET NULL so hard-deleting a survivor (the
--                          pg_dump-then-delete path, MP:DEC-B) leaves its tombstones
--                          dangling-but-null rather than cascade-deleting live history.
--
--   * `resolve_entity(uuid)` (E5) — the redirect-chasing SQL function. A read that
--                          lands on a tombstone must follow `merged_into` to the
--                          TERMINAL survivor. A cycle-safe recursive CTE (bounded depth)
--                          chases the chain and returns the terminal id (or the input id
--                          unchanged when the row is not a tombstone). The convenience
--                          view `entity_profiles_resolved` exposes each row's resolved id
--                          alongside it. Reads that must not surface a tombstone should
--                          route the id through `resolve_entity()`.
--
-- WHY these two tables (not columns on entity_profiles):
--   Aliases and pairwise judgements are BOTH many-per-entity / many-per-pair and
--   carry their own provenance (decided_by / model_id / confidence / decided_at).
--   Modeling either on the single-row `entity_profiles` table would force an
--   unbounded JSON blob per row (write amplification, no per-row provenance, not
--   indexable for the write-time probe) — exactly the `merged_aliases` limitation
--   this migration exists to retire. The relations are the right home; each can be
--   rebuilt/curated independently of the entity rows.
--
-- SAFETY (idempotent, additive, forward-only — no data rewrite, no data repair):
--   `CREATE TABLE IF NOT EXISTS` / `ADD COLUMN IF NOT EXISTS` /
--   `CREATE INDEX IF NOT EXISTS` / `CREATE OR REPLACE FUNCTION` /
--   `CREATE OR REPLACE VIEW` are all no-ops (or clean replacements) on re-apply and
--   on a fresh cold-start substrate. The new `merged_into` column is nullable with no
--   default, so every existing entity row is left unmarked (a live, non-tombstone
--   entity) and no row is rewritten. This migration reads/writes NOTHING in the
--   ad-hoc `data->'merged_aliases'` blob — the E2a curation + E4/E5 code migrate that
--   data separately.
--
--   The runner wraps this file in its own transaction, sets search_path=public, and
--   records it in `legba_data_migrations` (filename-gated, discovered by the *.sql
--   glob in legba.data.migrate — NO code change, NO inline BEGIN/COMMIT, NO inline
--   ledger insert here — same convention as 0083/0084/0085/0055).
--   CREATE-only / clean-slate policy honored (no data migration).

-- ---------------------------------------------------------------------------
-- 1. entity_alias (E2b) — normalized surface form -> canonical entity id
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS public.entity_alias (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    alias_norm   text NOT NULL,          -- normalized surface form (lower/unaccent/strip-punct); the write-time lookup key
    canonical_id uuid NOT NULL
                   REFERENCES public.entity_profiles(id) ON DELETE CASCADE,
    alias_kind   text NOT NULL DEFAULT 'other'
                   CHECK (alias_kind IN (
                       'transliteration', 'honorific_stripped', 'acronym',
                       'native_script', 'fragment_expansion', 'exact', 'other'
                   )),
    decided_by   text NOT NULL DEFAULT 'rule'
                   CHECK (decided_by IN ('rule', 'llm', 'human')),
    model_id     text,                   -- model id when decided_by='llm'
    confidence   real,
    source_note  text,                   -- free-text provenance / justification
    decided_at   timestamptz NOT NULL DEFAULT now(),
    -- one canonical target per distinct normalized surface (a surface may still
    -- legitimately appear once per canonical id, but not twice for the SAME id)
    UNIQUE (alias_norm, canonical_id)
);

-- The write-time probe: given a freshly-normalized ingest surface, look up its
-- canonical id. Index on the lookup key so the probe stays index-only.
CREATE INDEX IF NOT EXISTS idx_entity_alias_norm
    ON public.entity_alias USING btree (alias_norm);

-- ---------------------------------------------------------------------------
-- 2. entity_judgement (E2b) — pairwise verdict cache (order-independent key)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS public.entity_judgement (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    pair_key      text NOT NULL,         -- deterministic: two normalized surfaces sorted + joined, so (A,B)==(B,A)
    entity_a      uuid,                  -- nullable FK: a verdict may predate the row / a row may later be deleted
    entity_b      uuid,
    verdict       text NOT NULL
                   CHECK (verdict IN ('same', 'not_same', 'unsure')),
    justification text,
    decided_by    text NOT NULL DEFAULT 'llm'
                   CHECK (decided_by IN ('rule', 'llm', 'human')),
    model_id      text,
    confidence    real,
    decided_at    timestamptz NOT NULL DEFAULT now(),
    -- the cache invariant: one cached verdict per (order-independent) pair
    UNIQUE (pair_key)
);

-- The cache probe: before adjudicating a candidate pair, look up its pair_key.
-- The UNIQUE constraint already backs this with an index; the explicit index is
-- a no-op documentation of the read path and idempotent under IF NOT EXISTS.
CREATE INDEX IF NOT EXISTS idx_entity_judgement_pair_key
    ON public.entity_judgement USING btree (pair_key);

-- ---------------------------------------------------------------------------
-- 3. entity_profiles.merged_into (E5) — tombstone + redirect
-- ---------------------------------------------------------------------------
--
-- A row with merged_into set is a TOMBSTONE redirecting to the survivor. Self-FK
-- ON DELETE SET NULL: hard-deleting a survivor (pg_dump-then-delete, MP:DEC-B)
-- must not cascade-delete its tombstones — it nulls their redirect instead,
-- degrading them gracefully to ordinary (now-terminal) rows.

ALTER TABLE public.entity_profiles
    ADD COLUMN IF NOT EXISTS merged_into uuid
        REFERENCES public.entity_profiles(id) ON DELETE SET NULL;

-- Partial index over only the (few) tombstones: keeps the researcher's
-- "list my tombstones / find rows redirecting to X" scans cheap regardless of
-- total entity_profiles size.
CREATE INDEX IF NOT EXISTS idx_entity_profiles_merged_into
    ON public.entity_profiles USING btree (merged_into)
    WHERE merged_into IS NOT NULL;

-- ---------------------------------------------------------------------------
-- 4. resolve_entity(uuid) (E5) — cycle-safe redirect chaser + convenience view
-- ---------------------------------------------------------------------------
--
-- Chases merged_into redirects to the TERMINAL survivor id. Returns the input id
-- unchanged when the row is not a tombstone (or does not exist). CYCLE-SAFE: a
-- corrupt A->B->A (or longer) loop is bounded two ways — a depth cap (~16, far
-- beyond any legitimate merge chain) AND the recursive CTE's own CYCLE clause on
-- the visited id — so it always terminates and returns a deterministic id rather
-- than looping forever. STABLE (same inputs -> same output within a statement;
-- no writes). Schema-qualified to public.

CREATE OR REPLACE FUNCTION public.resolve_entity(p_id uuid)
RETURNS uuid
LANGUAGE sql
STABLE
AS $$
    WITH RECURSIVE chain(id, next_id, depth) AS (
        -- anchor: the row itself (next_id = where it redirects, if anywhere)
        SELECT ep.id, ep.merged_into, 0
          FROM public.entity_profiles ep
         WHERE ep.id = p_id
        UNION ALL
        -- step: follow the redirect to the next row, bounded by a depth cap
        SELECT ep.id, ep.merged_into, c.depth + 1
          FROM chain c
          JOIN public.entity_profiles ep ON ep.id = c.next_id
         WHERE c.next_id IS NOT NULL
           AND c.depth < 16
    ) CYCLE id SET is_cycle USING path
    -- the terminal row is the deepest non-cyclic one reached; fall back to the
    -- input id when p_id names no row at all (so a read never null-drops an id).
    SELECT COALESCE(
        (SELECT id FROM chain WHERE NOT is_cycle ORDER BY depth DESC LIMIT 1),
        p_id
    );
$$;

COMMENT ON FUNCTION public.resolve_entity(uuid) IS
    'E5: chase entity_profiles.merged_into tombstone redirects to the terminal '
    'survivor id (cycle-safe, bounded depth 16). Returns the input id unchanged '
    'when the row is not a tombstone or does not exist. Reads that must not '
    'surface a tombstone should route the id through this function.';

-- Convenience view: each entity row alongside its resolved (terminal) id. A read
-- that wants the survivor without calling the function per-row can join here.
-- (Kept minimal — the function is the canonical entry point.)
CREATE OR REPLACE VIEW public.entity_profiles_resolved AS
    SELECT
        ep.*,
        public.resolve_entity(ep.id) AS resolved_id
      FROM public.entity_profiles ep;
