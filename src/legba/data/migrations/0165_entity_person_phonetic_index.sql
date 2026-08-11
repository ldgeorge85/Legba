-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
-- ==========================================================================
-- 0165 — phonetic PREFILTER index for the person transliteration guard (V-G6).
-- ==========================================================================
-- WHY:
--   NER emitted a person entity from Arabic source text — `آفي بالوط`, the
--   Arabic transliteration of the Israeli officer Avi Bluth — and the resolver
--   minted it as a NEW profile, "Avi Balut". One officer became two, then two
--   dismissals, and the split rode through country_composition and
--   region_composition into the world read with every layer passing verify
--   (2026-08-03 adjudication section 6). The live table holds FOUR rows for that
--   one man: "Avi Blot", "Bluth", "Avi Balut", "Avi Bluth".
--
--   The resolver's mint-time ladder is exact-string all the way down: an
--   exact-name probe, then an article/alias-normalized probe. Neither computes a
--   similarity, so `lookup_key('avi bluth') <> lookup_key('avi balut')` and the
--   fork is guaranteed. The guard added alongside this migration is a third,
--   person-only probe using per-token double metaphone plus a bounded edit
--   distance — the "dmetaphone-per-token (fuzzystrmatch) — phonetic recall"
--   blocking signal 0088's own header declared and never implemented.
--
-- WHAT / WHY AN INDEX:
--   Measured on the live substrate (read-only): the guard's exact predicate over
--   the 22.5k person rows costs ~113 ms per new-person mention on a bitmap scan,
--   against ~1,000 new person rows a day — 2 minutes of ingestion-path DB time
--   daily, for a check that is supposed to be cheap. A functional btree on the
--   whole-name PHONETIC SKELETON (double metaphone of the space-stripped,
--   lowercased name), partial on ACTIVE person rows, narrows the candidate set
--   to a handful before the exact predicate runs:
--
--     20,512 active person rows -> 7,362 distinct skeletons
--     mean bucket 3, p95 bucket 8, max 300
--     the Avi Bluth / Avi Balut bucket: 3 rows
--
--   PREFILTER RECALL, measured honestly: of the 720 pairs the exact predicate
--   accepts across the live person table, 698 (97%) share a whole-name skeleton.
--   The 3% the prefilter cannot see keep TODAY'S behaviour — a new row is minted,
--   exactly as before — so a prefilter miss is never a regression, only a fix
--   not applied. A two-key variant (primary + alt skeleton) was measured at 98%:
--   one extra point of recall for a second index, and not taken.
--
-- REVERSIBLE: DROP INDEX. IDEMPOTENT: CREATE INDEX IF NOT EXISTS.
--   NON-DESTRUCTIVE (adds only). fuzzystrmatch is already installed by 0088.
-- ==========================================================================

CREATE EXTENSION IF NOT EXISTS fuzzystrmatch;

-- The whole-name PHONETIC SKELETON. IMMUTABLE so it can back a functional index
-- (dmetaphone itself is IMMUTABLE; the wrapper only spells the normalization
-- once so the index expression and the query expression cannot drift apart).
-- Empty/whitespace input yields '' rather than NULL, matching entity_block_key.
-- ``public.dmetaphone`` is SCHEMA-QUALIFIED for the same reason 0088 qualifies
-- ``public.unaccent``: on a FRESH database this body is parsed in the same
-- command batch that created the extension, and the unqualified name does not
-- resolve there (it fails the whole migration on a new deployment while passing
-- on an existing one — the worst shape of bug to ship).
CREATE OR REPLACE FUNCTION public.entity_phonetic_key(text)
    RETURNS text
    LANGUAGE sql IMMUTABLE PARALLEL SAFE
    AS $$
    SELECT COALESCE(
        public.dmetaphone(regexp_replace(lower(btrim(COALESCE($1, ''))), '\s+', '', 'g')),
        ''
    )
    $$;

-- Partial on ACTIVE PERSON rows only — the guard is person-scoped (see the
-- handler note: numeric and directional tokens carry real meaning in org and
-- location surfaces, so the same predicate needs separate validation there).
CREATE INDEX IF NOT EXISTS idx_entity_profiles_person_phonetic
    ON entity_profiles (public.entity_phonetic_key(canonical_name))
    WHERE entity_class = 'person'
      AND merged_into IS NULL
      AND COALESCE(data->>'gc_status', '') NOT IN ('merged', 'junk');
