-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0077_close_semantic_junk_facts.sql  (DQ M1/M2/M3 — fact-write audit 2026-07-06)
--
-- PROBLEM: the P5 junk-fact gate (0065 + fact_extractor.py) closed MECHANICALLY
--   malformed surfaces (bare quantifier tokens, possessive fragments). The live
--   audit found the ingestion relation-extractor STILL laundering three classes
--   of semantic / type junk the P5 gate was never scoped for:
--     M1 — direction-inverted / bad-type membership ("NATO member of Turkiye"
--          inverted; "Russia member of 188,000 barrels" quantity object);
--     M2 — demonym + relative-temporal-phrase SUBJECTS ("Chinese founded by Jin
--          Mingri", "250 years ago founded by …", "December last year …");
--     M3 — nationality-adjective VALUES that become FALSE geographic facts
--          ("Kyiv capital of Russian", "US conflict with Iranian").
--
-- PAIRED CODE FIX (keeps it out — same commit):
--   * src/legba/data/filters/fact_extractor.py — a demonym-SUBJECT reject
--     (M2), a 'member of'/'part of' PERSON-object reject + an org->country
--     inverted-membership reject (M1), and a demonym/region-adjective VALUE
--     normalization to the country/continent lemma (M3, "Russian" -> "Russia").
--   * src/legba/data/_entity_canon.py — _is_temporal_surface is now relative-
--     phrase aware ("250 years ago", "December last year"), so is_junk_entity
--     (which the fact gate calls on both endpoints) drops temporal subjects.
--
-- THIS MIGRATION closes (valid_until=now(); NO delete) the OPEN ingestion facts
--   matching the M1/M2/M3 MECHANICAL patterns ONLY:
--     (1) demonym / region-adjective SUBJECT or VALUE (whole trimmed surface is
--         a curated NATIONAL demonym or continental adjective; the bare country
--         names — "United States"/"Russia" — are NOT in the list);
--     (2) relative-temporal-phrase SUBJECT or VALUE ("250 years ago", "last
--         week", "December last year", "the 21st century", "past 24 hours");
--     (3) 'member of'/'part of' with a NUMERIC-leading OBJECT ("… member of
--         188,000 barrels") — a quantity is never an org/place;
--     (4) org-acronym SUBJECT 'member of' a COUNTRY value (the inversion:
--         "NATO member of Turkiye", "UN member of Iran").
--   It does NOT attempt to pattern-match all SEMANTIC junk (e.g. the Taylor
--   Swift interview-misread-as-employment, "Guam part of the Northern
--   Marianas", "Beijing member of Pacific") — those are left to the code gate +
--   natural staleness (per the audit). Only source_type='ingestion' is touched
--   (seed / agent / manual facts are NEVER matched).
--
-- CONSERVATIVE: the demonym list is a closed curated set of nationality /
--   continental adjectives; the temporal patterns are anchored on the whole
--   trimmed surface; the numeric-object rule is gated to member/part predicates;
--   the inversion rule requires BOTH an org-acronym subject AND a country value.
--   A 20-row sample and the per-pattern counts were reviewed before apply (see
--   the header below); NO real fact is in scope, and NO non-ingestion row
--   matches the predicate (verified: the same WHERE minus the source_type guard
--   returns 0 seed/curated/agent/manual rows).
--
-- REVERSIBLE (valid_until back to NULL re-opens — no backup needed, unlike the
--   entity hard-delete in 0063/0076). IDEMPOTENT (the valid_until IS NULL guard
--   makes a re-run a no-op). Routed through legba.data.migrate — the runner
--   wraps this file in ONE transaction + records the ledger row, so there is NO
--   inline BEGIN/COMMIT here (matching 0065/0076). TEMP TABLEs are ON COMMIT
--   DROP inside that single wrapping transaction.
--
-- ==========================================================================
-- MEASURED (live `legba`, read-only SELECTs, 2026-07-06; migration head 0076):
--   open ingestion facts .................................... 3,013
--   per-pattern matches (OVERLAP — a row can match >1):
--     demonym / region-adjective SUBJECT .................... 158
--     demonym / region-adjective VALUE ..................... 188
--     relative-temporal SUBJECT ............................ 15
--     relative-temporal VALUE .............................. 14
--     member/part + numeric-leading OBJECT ................. 3
--     org-acronym SUBJECT member-of COUNTRY (inversion) .... 2   (NATO->Turkiye, UN->Iran)
--   UNION TOTAL closed by this migration ................... 368
--   non-ingestion rows matched (sanity) ................... 0
--
-- 20-ROW SAMPLE reviewed before freezing (subject | predicate | value):
--   250 years ago | founded by | Independence Day          [temporal subject]
--   250 years ago | founded by | The United States         [temporal subject]
--   27 - year - old | member of | French                   [demonym value]
--   35 years ago | signed agreement with | the Treaty on … [temporal subject]
--   Adama Paris | located in | Senegalese                  [demonym value]
--   Afghan | border with | Pakistan                        [demonym subject]
--   Afghan | conflict with | Pakistan                      [demonym subject]
--   Afghan | located in | Taliban                          [demonym subject]
--   Afghan | member of | the European Commission           [demonym subject]
--   Africa | part of | African                             [demonym value]
--   African | founded by | Entre Nous                      [region-adj subject]
--   African | located in | France                          [region-adj subject]
--   African | member of | Germany                          [region-adj subject]
--   African | supplies to | Europe                         [region-adj subject]
--   Afro B | member of | British                           [demonym value]
--   Albanian | located in | Albania                        [demonym subject]
--   Albanian | located in | US                             [demonym subject]
--   Algeria | opponent of | Swiss                          [demonym value]
--   Alibaba | part of | Chinese                            [demonym value]
--   Al Jazeera's | employed by | Israeli                   [demonym value]
-- ==========================================================================

-- --------------------------------------------------------------------------
-- FROZEN STOPLISTS (VALUES temp tables — dropped at COMMIT)
-- --------------------------------------------------------------------------

-- Curated NATIONAL demonyms + continental adjectives (mirrors
-- _entity_canon._DEMONYM_MAP keys + _REGION_ADJECTIVE_MAP keys, EXCLUDING the
-- bare continent name "europe" which is a legitimate place value). A bare
-- adjective here is never a typed entity — as a SUBJECT it is a mis-extraction,
-- as a VALUE it produces a false geographic fact.
CREATE TEMP TABLE _dem(w text) ON COMMIT DROP;
INSERT INTO _dem(w) VALUES
      ('afghan'),('african'),('albanian'),('algerian'),('american'),('angolan'),
      ('antarctic'),('argentine'),('argentinian'),('armenian'),('asian'),('australian'),
      ('austrian'),('azerbaijani'),('bahraini'),('bangladeshi'),('belarusian'),('belgian'),
      ('beninese'),('bhutanese'),('bolivian'),('bosnian'),('botswanan'),('brazilian'),
      ('british'),('bruneian'),('bulgarian'),('burmese'),('burundian'),('cambodian'),
      ('cameroonian'),('canadian'),('chadian'),('chilean'),('chinese'),('colombian'),
      ('congolese'),('costa rican'),('croatian'),('cuban'),('cypriot'),('czech'),
      ('danish'),('djiboutian'),('dominican'),('dutch'),('ecuadorian'),('egyptian'),
      ('emirati'),('eritrean'),('estonian'),('ethiopian'),('european'),('fijian'),
      ('filipino'),('finnish'),('french'),('gabonese'),('gambian'),('georgian'),
      ('german'),('ghanaian'),('greek'),('guatemalan'),('guinean'),('haitian'),
      ('honduran'),('hungarian'),('icelandic'),('indian'),('indonesian'),('iranian'),
      ('iraqi'),('irish'),('israeli'),('italian'),('ivorian'),('jamaican'),
      ('japanese'),('jordanian'),('kazakh'),('kenyan'),('kosovar'),('kuwaiti'),
      ('kyrgyz'),('laotian'),('latvian'),('lebanese'),('liberian'),('libyan'),
      ('lithuanian'),('luxembourgish'),('macedonian'),('madagascan'),('malagasy'),('malawian'),
      ('malaysian'),('maldivian'),('malian'),('maltese'),('mauritanian'),('mexican'),
      ('moldovan'),('mongolian'),('montenegrin'),('moroccan'),('mozambican'),('myanmarese'),
      ('namibian'),('nepalese'),('nepali'),('new zealander'),('nicaraguan'),('nigerian'),
      ('nigerien'),('north american'),('north korean'),('norwegian'),('oceanian'),('omani'),
      ('pakistani'),('palestinian'),('panamanian'),('papua new guinean'),('paraguayan'),('peruvian'),
      ('philippine'),('polish'),('portuguese'),('qatari'),('romanian'),('russian'),
      ('rwandan'),('salvadoran'),('saudi'),('senegalese'),('serbian'),('sierra leonean'),
      ('singaporean'),('slovak'),('slovenian'),('somali'),('south african'),('south american'),
      ('south korean'),('spanish'),('sri lankan'),('sudanese'),('swedish'),('swiss'),
      ('syrian'),('taiwanese'),('tajik'),('tanzanian'),('thai'),('togolese'),
      ('trinidadian'),('tunisian'),('turkish'),('turkmen'),('ugandan'),('ukrainian'),
      ('uruguayan'),('uzbek'),('venezuelan'),('vietnamese'),('yemeni'),('zambian'),
      ('zimbabwean');

-- Supranational / institutional ORG acronyms + short names (single-token, so
-- the org-suffix gazetteer misses them) that are unambiguously organizations.
-- Used ONLY for the inverted-membership rule (org 'member of' a COUNTRY).
CREATE TEMP TABLE _org(w text) ON COMMIT DROP;
INSERT INTO _org(w) SELECT unnest(ARRAY[
    'nato','opec','asean','brics','ecowas','mercosur','gcc','imf','wto','eu',
    'un','european union','united nations','african union','arab league',
    'shanghai cooperation organization','unesco','unicef','unhcr']);

-- Country surfaces (iso_countries name/official + a few curated common ASCII
-- aliases the gazetteer stores differently — "türkiye" vs "Turkiye" — so the
-- inversion rule recognises the country VALUE).
CREATE TEMP TABLE _ctry(w text) ON COMMIT DROP;
INSERT INTO _ctry(w)
    SELECT lower(name) FROM iso_countries
    UNION SELECT lower(official) FROM iso_countries WHERE official <> ''
    UNION SELECT unnest(ARRAY[
        'turkey','turkiye','us','usa','u.s.','uk','russia','iran',
        'south korea','north korea','drc','uae','czechia','palestine','kosovo']);

-- --------------------------------------------------------------------------
-- CLOSE the OPEN ingestion facts matching the M1/M2/M3 mechanical patterns.
-- ONE statement; valid_until=now() (REVERSIBLE); source_type='ingestion' guard.
-- --------------------------------------------------------------------------
UPDATE facts f
SET valid_until = now(),
    updated_at  = now()
WHERE f.valid_until IS NULL
  AND f.source_type = 'ingestion'
  AND (
        -- M2 demonym / region-adjective SUBJECT ; M3 demonym VALUE
        lower(btrim(f.subject)) IN (SELECT w FROM _dem)
     OR lower(btrim(f.value))   IN (SELECT w FROM _dem)
        -- M2 relative-temporal-phrase SUBJECT or VALUE
     OR btrim(f.subject) ~* '^[0-9]+ +(seconds?|minutes?|hours?|days?|weeks?|months?|years?|decades?|centuries) +(ago|earlier|later|back)$|^(the +)?(last|next|this|previous|coming|past) +(week|month|year|day|decade|weekend|quarter|hour|morning|evening|night)s?$|^(january|february|march|april|may|june|july|august|september|october|november|december) +(of +)?(last|this|next|previous) +year$|^(last|next|this|previous|coming) +(january|february|march|april|may|june|july|august|september|october|november|december)$|^(the +)?[0-9]{1,3}(st|nd|rd|th) +century$|^(the +)?past +[0-9]+ +(hours?|days?|weeks?|months?|years?)$'
     OR btrim(f.value)   ~* '^[0-9]+ +(seconds?|minutes?|hours?|days?|weeks?|months?|years?|decades?|centuries) +(ago|earlier|later|back)$|^(the +)?(last|next|this|previous|coming|past) +(week|month|year|day|decade|weekend|quarter|hour|morning|evening|night)s?$|^(january|february|march|april|may|june|july|august|september|october|november|december) +(of +)?(last|this|next|previous) +year$|^(last|next|this|previous|coming) +(january|february|march|april|may|june|july|august|september|october|november|december)$|^(the +)?[0-9]{1,3}(st|nd|rd|th) +century$|^(the +)?past +[0-9]+ +(hours?|days?|weeks?|months?|years?)$'
        -- M1 'member of'/'part of' with a NUMERIC-leading (quantity) OBJECT
     OR (lower(btrim(f.predicate)) IN ('member of','part of')
         AND btrim(f.value) ~ '^[0-9]')
        -- M1 org-acronym SUBJECT 'member of' a COUNTRY value (the inversion)
     OR (lower(btrim(f.predicate)) = 'member of'
         AND lower(btrim(f.subject)) IN (SELECT w FROM _org)
         AND lower(btrim(f.value))   IN (SELECT w FROM _ctry))
  );
