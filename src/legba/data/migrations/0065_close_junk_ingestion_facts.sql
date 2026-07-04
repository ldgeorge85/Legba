-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0065_close_junk_ingestion_facts.sql  (DQ Phase 5 / facts-nexuses finding
--   "facts / extraction quality")
--
-- PROBLEM: the ingestion relation-extractor laundered sentence-fragment surfaces
--   into facts — bare determiner/quantifier tokens as a subject or value
--   ('half', 'Thousands', 'hundreds', 'first', 'one') and tokenizer
--   possessive artifacts (a surface ending in a SPACE + "'s", e.g. "FRANCE 24
--   's", "Timor - Leste 's", "Donald Trump 's"). These are MECHANICALLY
--   malformed entity surfaces, not real facts.
--
-- PAIRED CODE FIX (keeps it out): src/legba/data/filters/fact_extractor.py —
--   the quantity-endpoint gate now covers the plural quantity nouns
--   ('thousands'/'hundreds'/'dozens'/'millions'/'half') it missed, and a new
--   always-on possessive-fragment gate drops a trailing " 's" surface at write.
--
-- THIS MIGRATION closes (valid_until=now(); NO delete) the OPEN ingestion facts
--   whose subject OR value matches the MECHANICAL junk patterns ONLY:
--     (1) bare determiner / quantifier / ordinal token (whole trimmed surface
--         equals one of a curated list — NEVER a real geopolitical entity;
--         'United States' / 'US' are NOT in the list);
--     (2) pure-numeric surface (digits + numeric punctuation, no letters);
--     (3) trailing spaced possessive artifact (" 's" at end).
--   It does NOT attempt to pattern-match SEMANTIC junk ('Germany employed by
--   Nagelsmann') — that is left to the code fix + natural staleness (per the
--   finding). Only the source_type='ingestion' tier is touched (seed / agent /
--   manual facts are never matched).
--
-- CONSERVATIVE: the bare-token list is a closed set of quantity/determiner
--   words; the possessive pattern requires the tokenizer's SPACE before "'s"
--   (a legitimate name never ends in " 's"). A 30-row sample and the match count
--   were reviewed before apply (see the DQ ledger); no real fact is in scope.
--
-- REVERSIBLE (valid_until back to NULL re-opens). IDEMPOTENT (valid_until IS
--   NULL guard -> re-run no-op). Routed through the migration runner (ONE
--   transaction + ledger row; NO inline BEGIN/COMMIT).
--
-- MEASURED (live `legba`, 2026-07-03): 156 open ingestion facts matched
--   (bare_token 30, pure_numeric 0, trailing_spaced_possessive 126; overlap
--   folded). Examples: 'half employed by Russian', 'Thousands located in South
--   Africa', 'Asia member of first', 'Abu Dhabi ''s located in UAE',
--   'Angela Diffley employed by FRANCE 24 ''s'.

WITH junktok(w) AS (
    VALUES
      ('half'),('halves'),('thousands'),('hundreds'),('dozens'),('dozen'),
      ('millions'),('billions'),('thousand'),('hundred'),('million'),('billion'),
      ('first'),('second'),('third'),('fourth'),('fifth'),
      ('some'),('many'),('several'),('few'),('both'),('most'),('another'),
      ('one'),('two'),('three'),
      ('the'),('a'),('an'),('this'),('that'),('these'),('those')
)
UPDATE facts f
SET valid_until = now(),
    updated_at  = now()
WHERE f.valid_until IS NULL
  AND f.source_type = 'ingestion'
  AND (
        lower(btrim(f.subject)) IN (SELECT w FROM junktok)
     OR lower(btrim(f.value))   IN (SELECT w FROM junktok)
     OR btrim(f.subject) ~ '^[0-9][0-9.,%''-]*$'
     OR btrim(f.value)   ~ '^[0-9][0-9.,%''-]*$'
     OR f.subject ~ '[[:space:]]''s$'
     OR f.value   ~ '[[:space:]]''s$'
  );
