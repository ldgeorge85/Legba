-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0263_entity_alias_keys.sql
--
-- THE ENTITY NAME LOOKUP GETS A KEY TABLE.
--
-- WHY
-- ---
-- Two keeper-election probes find an entity by a name it carries:
--   * the EXACT-ALIAS probe (`_entity_resolve._EXACT_ALIAS_SQL`):
--     lower(btrim(alias)) = lower(btrim(name)) over every element of
--     `entity_profiles.data->'merged_aliases'`;
--   * the NORMALIZED fallback (`_entity_resolve._ALIAS_SQL` and the ingestion
--     resolver's copy in `entity_resolution.py`): the article/whitespace
--     normalized form of `canonical_name` OR of any alias = lookup_key(name).
-- Neither predicate can use an index, so every call reads the whole
-- `entity_profiles` table (243k rows on 2026-11-14, +7.5% a week; up to 0.57 s
-- a call; 227 whole-table scans in a 350 s window).
--
-- WHAT THIS ADDS
-- --------------
--   * `entity_alias_keys(key_kind, key, entity_id)` -- one row per distinct
--     (kind, normalized key, entity). Two kinds:
--       'alias_lower' = lower(btrim(alias))                      (aliases only)
--       'norm'        = regexp_replace(regexp_replace(lower(btrim(s)),
--                         '^(the|a|an)\s+', '', 'g'), '\s+', ' ', 'g')
--                       for s = canonical_name and every alias
--     These are, character for character, the expressions the scans evaluate,
--     computed in the same database, so a key-table probe matches exactly the
--     rows the scan's predicate matches. The table holds KEY MEMBERSHIP ONLY:
--     the active filter (gc_status) and the tie-break (class priority,
--     created_at) are read from `entity_profiles` through its primary key at
--     lookup time, so a class change or a retirement can never leave this table
--     stale.
--   * `entity_alias_keys_for(canonical_name, data)` -- the one definition of the
--     keys; the trigger, the backfill and the verifier all call it.
--   * Triggers on `entity_profiles` that rewrite an entity's keys in the SAME
--     transaction as every insert and every update that changes
--     `canonical_name` or `data->'merged_aliases'` (merges, splits, alias
--     edits, renames, by any writer). A delete (or an id change) cascades
--     through the FK. A
--     retirement (gc_status) needs no key change: it is filtered at read time.
--   * A key longer than 2000 bytes is not stored (a btree entry has a hard size
--     limit and a failed key insert would fail the profile write). The reader
--     sends any probe that could reach that length to the scan (see
--     `_entity_resolve._TABLE_PROBE_MAX_BYTES`), so the cap never changes an
--     answer. The live maximum on 2026-11-14 was 688 bytes.
--
-- NOT `entity_aliases`: that table (0220) is the per-collection provider
-- naming map, documented as "never read by entity resolution". Mixing the two
-- would let a provider's naming quirk re-shape the live graph.
--
-- SAFETY
-- ------
-- Additive and idempotent (IF NOT EXISTS / CREATE OR REPLACE), routed through
-- the runner (no inline BEGIN/COMMIT). No backfill here: the table starts empty
-- and is filled by scripts/entity_alias_keys_backfill.py --apply; the reader
-- stays on the scan (`LEGBA_ENTITY_ALIAS_LOOKUP` unset = scan) until the
-- verifier reports zero drift. Reversal: set the switch back to scan; the
-- table and triggers are inert for readers.

CREATE TABLE IF NOT EXISTS public.entity_alias_keys (
    key_kind   text NOT NULL,
    key        text NOT NULL,
    entity_id  uuid NOT NULL
                 REFERENCES public.entity_profiles (id)
                 ON DELETE CASCADE ON UPDATE CASCADE,
    CONSTRAINT entity_alias_keys_pkey PRIMARY KEY (key_kind, key, entity_id),
    CONSTRAINT entity_alias_keys_kind_vocab
        CHECK (key_kind IN ('alias_lower', 'norm'))
);

CREATE INDEX IF NOT EXISTS entity_alias_keys_entity_idx
    ON public.entity_alias_keys USING btree (entity_id);

COMMENT ON TABLE public.entity_alias_keys IS
    'P-6: the lookup keys of entity_profiles (normalized canonical name and '
    'merged_aliases) -> entity id, maintained by trigger in the same '
    'transaction as every profile write. Key membership only; active filter '
    'and tie-break are read from entity_profiles. Derived: rebuildable from '
    'entity_profiles by scripts/entity_alias_keys_backfill.py.';

CREATE OR REPLACE FUNCTION public.entity_alias_keys_for(p_canonical text, p_data jsonb)
RETURNS TABLE (key_kind text, key text)
LANGUAGE sql STABLE AS $$
    SELECT DISTINCT k.key_kind, k.key
      FROM (
        SELECT 'norm'::text AS key_kind,
               regexp_replace(regexp_replace(
                   lower(btrim(p_canonical)),
                   '^(the|a|an)\s+', '', 'g'),
                   '\s+', ' ', 'g') AS key
        UNION ALL
        SELECT 'norm'::text,
               regexp_replace(regexp_replace(
                   lower(btrim(al)),
                   '^(the|a|an)\s+', '', 'g'),
                   '\s+', ' ', 'g')
          FROM jsonb_array_elements_text(
                 CASE WHEN jsonb_typeof(p_data->'merged_aliases') = 'array'
                      THEN p_data->'merged_aliases' ELSE '[]'::jsonb END
               ) AS al
        UNION ALL
        SELECT 'alias_lower'::text, lower(btrim(al))
          FROM jsonb_array_elements_text(
                 CASE WHEN jsonb_typeof(p_data->'merged_aliases') = 'array'
                      THEN p_data->'merged_aliases' ELSE '[]'::jsonb END
               ) AS al
      ) AS k
     WHERE k.key IS NOT NULL
       AND octet_length(k.key) <= 2000
$$;

CREATE OR REPLACE FUNCTION public.entity_alias_keys_sync()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'UPDATE' THEN
        DELETE FROM public.entity_alias_keys WHERE entity_id = OLD.id;
    END IF;
    INSERT INTO public.entity_alias_keys (key_kind, key, entity_id)
    SELECT k.key_kind, k.key, NEW.id
      FROM public.entity_alias_keys_for(NEW.canonical_name, NEW.data) AS k
    ON CONFLICT DO NOTHING;
    RETURN NULL;
END $$;

CREATE OR REPLACE TRIGGER entity_alias_keys_sync_insert
    AFTER INSERT ON public.entity_profiles
    FOR EACH ROW EXECUTE FUNCTION public.entity_alias_keys_sync();

-- Fires only when a column the keys are computed from is in the SET list AND
-- its value actually changed (the ingestion upsert's ON CONFLICT branch sets
-- neither column and never fires it).
CREATE OR REPLACE TRIGGER entity_alias_keys_sync_update
    AFTER UPDATE OF canonical_name, data ON public.entity_profiles
    FOR EACH ROW
    WHEN (OLD.canonical_name IS DISTINCT FROM NEW.canonical_name
          OR (OLD.data -> 'merged_aliases') IS DISTINCT FROM (NEW.data -> 'merged_aliases'))
    EXECUTE FUNCTION public.entity_alias_keys_sync();
