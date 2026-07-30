-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
-- ==========================================================================
-- 0106 — entity_block_key: strip LEADING standalone articles (a/an).
-- ==========================================================================
-- WHY:
--   The E4a human-labeled merge-quality pass (2026-07-28) measured the entity
--   merge machinery at pairwise precision 1.000 / recall 0.347, and the single
--   largest miss class is ARTICLE TWINS: "X" vs "the X"/"An X" surface variants
--   of one referent. 0088 already strips "the" as a whole token, but "a"/"an"
--   were deliberately left alone ("A Coruña", "An Nasiriyah", dotted acronyms).
--   The labeling pass shows the cost of that caution is real recall: article
--   variants often never even share an exact block key, so they never become
--   exact-key candidates at all. This migration narrows the 0088 exception:
--   a SINGLE LEADING standalone "a"/"an" token is now stripped (anchored — a
--   mid-name "an" survives: "Deir an Nur" keeps all three tokens), because a
--   leading English article is never content-bearing while the transliterated
--   Arabic/Galician articles it can collide with ("An Nasiriyah" -> "Nasiriyah",
--   "A Coruña" -> "Coruña") denote the SAME place as their bare form anyway.
--   Content-bearing function words (of/for/...) are NOT touched.
--
--   Precision is preserved by the unchanged auto-band guards downstream
--   (legba.data._entity_candidates): auto_merge still requires a MULTI-token
--   key, a specific same class, NO person side, an ORDER-SENSITIVE token match
--   (the anagram guard), no geo conflict, and non-junk endpoints. A key that
--   collapses to one token ("A Team" -> "team") stays GRAY (LLM-adjudicated).
--
-- WHAT:
--   * CREATE OR REPLACE public.entity_block_key(text) — identical to 0088 plus
--     one anchored regexp_replace stripping a leading a/an token;
--   * rebuild idx_entity_profiles_block_key — the functional index stores the
--     OLD computed keys, so it must be dropped + recreated or index scans would
--     keep serving pre-0106 keys for existing rows.
--
-- REVERSIBLE: re-run the 0088 CREATE OR REPLACE body + rebuild the index.
-- IDEMPOTENT: CREATE OR REPLACE / DROP INDEX IF EXISTS / CREATE INDEX IF NOT
-- EXISTS. NON-DESTRUCTIVE: no table data is touched.
-- ==========================================================================

-- Normalized blocking key (0088, narrowed here): unaccent → lower → strip
-- honorifics/titles + the whole-token article 'the' + (NEW, 0106/0108) a
-- SINGLE LEADING standalone 'a'/'an' → strip non-alphanumerics → DISTINCT
-- tokens sorted → space-joined. Empty/whitespace input still yields '' (never
-- NULL), same as 0088 — the partial index and E3 both filter that out.
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
                     -- 0106: strip ONE LEADING standalone article a/an
                     -- (anchored — a mid-name 'an' survives; 'the' is already
                     -- stripped as a whole token above). Applied AFTER the
                     -- honorific + punctuation passes so "Dr A Team" and
                     -- "'A Team" fold the same way the Python order-sensitive
                     -- mirror (_entity_candidates._ordered_tokens) does.
                     regexp_replace(
                       regexp_replace(
                         lower(public.f_unaccent(btrim(COALESCE($1, '')))),
                         -- honorifics / titles / articles as WHOLE tokens (0088)
                         '\y(mr|mrs|ms|dr|prof|sir|gen|col|lt|sgt|sen|rep|hon|'
                         'president|pres|minister|ayatollah|sheikh|imam|rabbi|'
                         'the)\y',
                         ' ', 'g'),
                       '[^a-z0-9 ]', ' ', 'g'),  -- strip punctuation to spaces
                     '^\s*(a|an)\y', ' '),
                   ' ')
               ) AS tok
              WHERE tok <> ''
           ) d),
        ' '),
      '')
    $$;

-- The functional index stores values computed by the OLD function body —
-- rebuild it so index scans agree with the new key. Same partial predicate
-- as 0088 (ACTIVE rows only).
DROP INDEX IF EXISTS idx_entity_profiles_block_key;
CREATE INDEX IF NOT EXISTS idx_entity_profiles_block_key
    ON entity_profiles (entity_block_key(canonical_name))
    WHERE COALESCE(data->>'gc_status', '') NOT IN ('merged', 'junk');
