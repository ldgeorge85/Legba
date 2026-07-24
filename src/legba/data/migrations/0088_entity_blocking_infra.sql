-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
-- ==========================================================================
-- 0088 — Postgres-native blocking infrastructure for the entity_researcher (E3).
-- ==========================================================================
-- WHY:
--   E3 generates candidate MERGE pairs over entity_profiles (22k rows) without
--   an O(n^2) cross-join. The doctrine (Splink / OpenSanctions): BLOCK on cheap
--   deterministic keys, then only adjudicate the survivors. Three blocking
--   signals, all indexed, all scoped by entity_class + geo downstream:
--     1. an exact normalized BLOCK KEY (unaccent+lower+strip-punct+strip-
--        honorific, DISTINCT tokens sorted) — a high-precision block: "Ali
--        Khamenei" and "Ayatollah Ali Khamenei" share it, "Mojtaba Khamenei"
--        does NOT (so father/son never auto-block; they surface only via the
--        fuzzy signals below, as a GRAY pair the LLM adjudicates);
--     2. dmetaphone-per-token (fuzzystrmatch) — phonetic recall;
--     3. a pg_trgm similarity backstop on lower(canonical_name).
--   Blocking is NOT merging: two distinct referents that merely share a block
--   key ("the Atlantic" magazine vs "Atlantic" ocean) are a CANDIDATE the class
--   scope + E4 adjudicator keep apart. Recall here is cheap; precision is E4's.
--
-- WHAT:
--   * enable unaccent + fuzzystrmatch (pg_trgm already installed);
--   * f_unaccent(text) — an IMMUTABLE wrapper so unaccent can back a functional
--     index. unaccent() is only STABLE (it depends on the unaccent text-search
--     dictionary), but that dictionary is static in this deployment, so the
--     IMMUTABLE marking is safe here — the standard pattern for indexing it;
--   * entity_block_key(text) — IMMUTABLE normalized sorted-token key;
--   * a functional btree index on entity_block_key(canonical_name) and a GIN
--     pg_trgm index on lower(canonical_name), both partial on ACTIVE rows
--     (gc_status NOT merged/junk) — E3 only blocks live keepers.
--
-- REVERSIBLE: DROP the two indexes + the two functions (the extensions may stay;
--   dropping them is a separate op). IDEMPOTENT: CREATE EXTENSION/OR REPLACE/
--   CREATE INDEX IF NOT EXISTS. NON-DESTRUCTIVE (adds only). Routed through a
--   migration (new extension + DDL) per the house rule.
-- ==========================================================================

CREATE EXTENSION IF NOT EXISTS unaccent;
CREATE EXTENSION IF NOT EXISTS fuzzystrmatch;

-- IMMUTABLE unaccent wrapper (see header — safe under a static dictionary).
CREATE OR REPLACE FUNCTION public.f_unaccent(text)
    RETURNS text
    LANGUAGE sql IMMUTABLE PARALLEL SAFE STRICT
    AS $$ SELECT public.unaccent('public.unaccent', $1) $$;

-- Normalized blocking key: unaccent → lower → strip a short, domain-appropriate
-- honorific/title set + leading articles → strip non-alphanumerics → DISTINCT
-- tokens sorted → space-joined. Empty/whitespace input yields '' (never NULL),
-- which the partial index and E3 both filter out.
CREATE OR REPLACE FUNCTION public.entity_block_key(text)
    RETURNS text
    LANGUAGE sql IMMUTABLE PARALLEL SAFE
    AS $$
    SELECT COALESCE(
      array_to_string(
        (SELECT array_agg(tok ORDER BY tok)
           FROM (
             SELECT DISTINCT tok
               FROM unnest(
                 string_to_array(
                   regexp_replace(
                     regexp_replace(
                       lower(public.f_unaccent(btrim(COALESCE($1, '')))),
                       -- honorifics / titles / articles as WHOLE tokens
                       -- NB: strip 'the' (common entity-leading article: "the
                       -- Atlantic"/"the Hague") but NOT bare 'a'/'an' — those
                       -- mangle real names ("A Coruña", "An Nasiriyah") and
                       -- turn dotted acronyms into single-char junk tokens.
                       '\y(mr|mrs|ms|dr|prof|sir|gen|col|lt|sgt|sen|rep|hon|'
                       'president|pres|minister|ayatollah|sheikh|imam|rabbi|'
                       'the)\y',
                       ' ', 'g'),
                     '[^a-z0-9 ]', ' ', 'g'),   -- strip punctuation to spaces
                   ' ')
               ) AS tok
              WHERE tok <> ''
           ) d),
        ' '),
      '')
    $$;

-- Functional btree on the exact block key (ACTIVE keepers only).
CREATE INDEX IF NOT EXISTS idx_entity_profiles_block_key
    ON entity_profiles (entity_block_key(canonical_name))
    WHERE COALESCE(data->>'gc_status', '') NOT IN ('merged', 'junk');

-- pg_trgm GIN backstop for the similarity signal (ACTIVE keepers only).
CREATE INDEX IF NOT EXISTS idx_entity_profiles_name_trgm
    ON entity_profiles USING gin (lower(canonical_name) gin_trgm_ops)
    WHERE COALESCE(data->>'gc_status', '') NOT IN ('merged', 'junk');
