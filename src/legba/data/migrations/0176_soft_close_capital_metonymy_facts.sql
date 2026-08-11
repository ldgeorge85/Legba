-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0176_soft_close_capital_metonymy_facts.sql
--
-- DATA REPAIR (W2-C, 2026-08-03). CW-6 added a capital-as-government metonymy
-- guard to `fact_extractor` — news writes governments as their capitals
-- ("tensions between Madrid and Rabat"), NER reads the capital as a LOCATION,
-- and the pair lands in `facts` as a geographic claim about a CITY. The guard
-- stops NEW ones. It does nothing about the history, which is what this is.
--
-- MEASURED READ-ONLY ON THE LIVE SUBSTRATE, 2026-08-03, using the guard's OWN
-- predicate (the exact `_GOVERNMENT_METONYM_SUBJECTS` x `_STATE_ONLY_PREDICATES`
-- product, not an approximation of it):
--
--     facts matching the guard, all time                    376
--     ...still open (valid_until IS NULL, not superseded)    200
--     ...of those, source_type = 'ingestion'                 200   (100%)
--     ...extractor/backend                    fact_extractor/relation (100%)
--     open rows carrying contested = true                    171
--     distinct contention groups they feed                    40
--     contention groups with a surviving NON-metonymy fact      0
--
-- PROJECTED EFFECT, dry-run as a pure SELECT against the same database:
--
--     facts soft-closed                                      200
--     fact_contention_values rows marked is_junk             171
--     fact_contention groups collapsed                        40
--
-- The last line of the census is the one that decides the shape of this migration. Every one
-- of the 40 contention groups is metonymy debris END TO END — closing the facts
-- alone would stranded 40 open disputes with zero live members, and `claim_watch`
-- would go on harvesting "which value of 'border with' for 'madrid' is correct?"
-- out of them forever. That question class scored 0/20 in K-4 R3. So the repair
-- closes the facts AND retires the groups they were the whole content of.
--
-- The population is unambiguous on inspection — the top clusters are
-- `tehran border with Afghanistan/Iraq` (Iran's government), `washington member
-- of NATO` (the United States), `kiev conflict with Russia` (Ukraine),
-- `moscow member of Brazil`, `islamabad signed agreement with Iran`. None is a
-- fact about a city.
--
-- THE NEWEST ROW PREDATES THE GUARD. Arrivals run to 2026-08-03 17:30 UTC; the
-- runtime carrying CW-6 came up at 23:03 UTC the same day. Nothing in this
-- cohort was minted by a guarded extractor, and nothing has been minted since.
--
-- SOFT-CLOSE, NOT DELETE (the 0117 pattern, for the 0117 reason). `valid_until =
-- now()` ends the fact's validity while preserving the row, its lineage and its
-- receipts. The stamp `data.closed_by = 'mig_0176_capital_metonymy'` makes the
-- cohort exactly queryable, and reversing it is one statement:
--
--     UPDATE facts SET valid_until = NULL,
--            data = data - 'closed_by'
--      WHERE data->>'closed_by' = 'mig_0176_capital_metonymy';
--
-- WHY THE PREDICATE CANNOT OVER-REACH. It is the guard's, verbatim, and the
-- guard is deliberately narrow on both axes: CONTAINMENT AND LOCATION
-- PREDICATES ARE ABSENT ("capital of", "located in", "part of", "based in"), so
-- the city's real facts — "Madrid located in Spain", "Madrid capital of Spain" —
-- are untouched by construction; and CITY-STATES ARE ABSENT from the subject set
-- (Singapore, Monaco, Vatican City, Luxembourg City), where the city IS the
-- state and its inter-state relations are real. A drift-guard test
-- (`test_metonymy_repair_matches_the_live_guard`) asserts the two lists below
-- are byte-identical to the Python frozensets, so widening the guard later
-- cannot silently orphan this migration's cohort — or vice versa.
--
-- THE CLASS CANNOT RE-FORM. Two gates, not one. `fact_extractor` stops the
-- triple being minted (CW-6), and `fact_contention_arbiter._junk_reason` now
-- carries `_is_capital_metonymy` too — it reuses the extractor's gates and had
-- simply never been given this one, which is why the contention plane kept
-- clustering a class the fact plane already rejected.

-- The guard's two sets, verbatim. Kept in lockstep with
-- legba.data.filters.fact_extractor by a test.
CREATE TEMP TABLE _metonymy_subjects (subject_key text PRIMARY KEY) ON COMMIT DROP;
INSERT INTO _metonymy_subjects (subject_key) VALUES
  ('10 downing street'),('abidjan'),('abu dhabi'),('abuja'),('accra'),
  ('addis ababa'),('algiers'),('amman'),('ankara'),('ashgabat'),('asmara'),
  ('astana'),('asuncion'),('asunción'),('athens'),('baghdad'),('baku'),
  ('bamako'),('bangkok'),('beijing'),('beirut'),('belgrade'),('berlin'),
  ('bern'),('berne'),('bishkek'),('bogota'),('bogotá'),('brasilia'),
  ('brasília'),('bratislava'),('brazzaville'),('brussels'),('bucharest'),
  ('budapest'),('buenos aires'),('cairo'),('canberra'),('capitol hill'),
  ('caracas'),('chisinau'),('colombo'),('conakry'),('copenhagen'),('dakar'),
  ('damascus'),('dar es salaam'),('delhi'),('dhaka'),('djibouti city'),
  ('dodoma'),('doha'),('downing street'),('dublin'),('dushanbe'),('elysee'),
  ('freetown'),('hanoi'),('harare'),('havana'),('helsinki'),('islamabad'),
  ('jakarta'),('jerusalem'),('juba'),('kabul'),('kampala'),('kathmandu'),
  ('khartoum'),('kiev'),('kigali'),('kinshasa'),('kishinev'),('kremlin'),
  ('kuala lumpur'),('kuwait city'),('kyiv'),('la paz'),('lima'),('lisbon'),
  ('ljubljana'),('london'),('luanda'),('lusaka'),('madrid'),('managua'),
  ('manama'),('manila'),('maputo'),('mexico city'),('minsk'),('mogadishu'),
  ('monrovia'),('montevideo'),('moscow'),('muscat'),('n''djamena'),('nairobi'),
  ('naypyidaw'),('new delhi'),('niamey'),('nicosia'),('nouakchott'),
  ('nur-sultan'),('oslo'),('ottawa'),('ouagadougou'),('panama city'),('paris'),
  ('peking'),('pentagon'),('phnom penh'),('podgorica'),('port-au-prince'),
  ('prague'),('pretoria'),('pristina'),('pyongyang'),('quito'),('rabat'),
  ('ramallah'),('reykjavik'),('riga'),('riyadh'),('rome'),('san salvador'),
  ('sana''a'),('sanaa'),('santiago'),('sarajevo'),('seoul'),('skopje'),
  ('sofia'),('stockholm'),('taipei'),('tallinn'),('tashkent'),('tbilisi'),
  ('tegucigalpa'),('tehran'),('the elysee'),('the hague'),('the kremlin'),
  ('the pentagon'),('the white house'),('thimphu'),('tirana'),('tokyo'),
  ('tripoli'),('tunis'),('ulaanbaatar'),('valletta'),('vienna'),('vientiane'),
  ('vilnius'),('warsaw'),('washington'),('wellington'),('white house'),
  ('whitehall'),('yaounde'),('yaoundé'),('yerevan'),('zagreb');

-- The state-only predicates, PLUS the CamelCase keys that `normalize_predicate`
-- folds into them ('memberof' -> 'member of', 'alliedwith' -> 'allied with').
-- The guard normalizes before testing; a stored row may carry either surface,
-- so the SQL has to accept both or it silently misses the seed-driver spelling.
CREATE TEMP TABLE _metonymy_predicates (predicate_key text PRIMARY KEY) ON COMMIT DROP;
INSERT INTO _metonymy_predicates (predicate_key) VALUES
  ('allied with'),('alliedwith'),('ally of'),('annexed'),('at war with'),
  ('border with'),('borders'),('claims'),('conflict with'),
  ('diplomatic relations with'),('hostile toward'),('member of'),('memberof'),
  ('neighbor of'),('neighbour of'),('occupies'),('opponent of'),('recognises'),
  ('recognizes'),('sanctioned by'),('signed agreement with'),
  ('spokesperson for'),('war with');

-- The cohort, resolved once so all three statements below act on exactly the
-- same rows (a fact closed in step 1 must still be visible to steps 2 and 3).
CREATE TEMP TABLE _metonymy_facts ON COMMIT DROP AS
SELECT f.id, f.contention_id
  FROM facts f
  JOIN _metonymy_subjects s ON s.subject_key = lower(btrim(f.subject))
  JOIN _metonymy_predicates p ON p.predicate_key = lower(btrim(f.predicate))
 WHERE f.valid_until IS NULL
   AND f.superseded_by IS NULL;

-- 1) SOFT-CLOSE the facts. Row, lineage and receipts preserved; reversible.
UPDATE facts
   SET valid_until = now(),
       data = COALESCE(data, '{}'::jsonb)
              || '{"closed_by": "mig_0176_capital_metonymy"}'::jsonb
 WHERE id IN (SELECT id FROM _metonymy_facts);

-- 2) Record WHY on the contention value rows, using the arbiter's own
-- operator-reportable vocabulary (`is_junk` + `junk_reason`) so the exclusion
-- reads identically whether the arbiter made it live or this migration made it
-- retroactively. Never silently dropped — that is the arbiter's standing rule.
UPDATE fact_contention_values v
   SET is_junk = TRUE,
       junk_reason = 'capital_metonymy',
       updated_at = now()
 WHERE v.representative_fact_id IN (SELECT id FROM _metonymy_facts)
   AND NOT v.is_junk;

-- 3) COLLAPSE the groups this class was the entire content of. `collapsed` is
-- the arbiter's own terminal status (it is what the arbiter sets when a dispute
-- resolves), so no new vocabulary is minted and every existing reader — the
-- liveness filter, the surfacing path, claim_watch's harvest — already honors
-- it. Guarded by a NOT EXISTS on any surviving live member, so a group that has
-- a real fact in it is left open even if some of its values were metonymy.
-- Measured 2026-08-03: 40 groups qualify, 0 have a surviving member.
UPDATE fact_contention c
   SET status = 'collapsed',
       surfaced_value = NULL,
       surfaced_fact_id = NULL,
       resolved_at = now(),
       updated_at = now()
 WHERE c.status <> 'collapsed'
   AND EXISTS (
         SELECT 1 FROM _metonymy_facts m WHERE m.contention_id = c.id
       )
   AND NOT EXISTS (
         SELECT 1
           FROM facts f
          WHERE f.contention_id = c.id
            AND f.valid_until IS NULL
            AND f.superseded_by IS NULL
            AND f.id NOT IN (SELECT id FROM _metonymy_facts)
       );
