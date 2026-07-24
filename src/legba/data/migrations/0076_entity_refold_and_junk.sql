-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0076_entity_refold_and_junk.sql  (DQ M4/M5 — entity write-path cleanup)
--
-- Pairs with the code fixes shipped in the same commit:
--   * src/legba/data/analysts/deterministic_handlers/entity_resolution.py —
--     the entity PRE-LOOKUP is now ALIAS/ARTICLE-AWARE: after the fast exact
--     probe misses it article/case/whitespace-normalizes canonical_name AND
--     every merged_alias and converges an incoming variant onto the ACTIVE
--     keeper (adopting the keeper's surface) instead of forking a new row (M4).
--   * src/legba/data/_entity_canon.py — is_junk_entity now rejects number+unit
--     quantity ("188,000 barrels"), possessive-KINSHIP ("Donald Trump's son"),
--     and bare temporal ("last week", "Today") surfaces (M5); + a conservative
--     REGION->location / sports-team + org-acronym->organization relabel (M6).
--
-- PROBLEM (live audit 2026-07-06): the P4 merge (0063) de-fragmented entities,
--   but the forward pre-lookup keyed on exact lower(canonical_name) only — blind
--   to a leading article and to a keeper's merged_aliases — so ingestion
--   RE-SPAWNED competing rows for already-folded surfaces ("the Strait of Hormuz"
--   vs keeper "Strait of Hormuz"; "the Axis of Resistance" (24 links) now
--   out-weighs its keeper (1 link)). Separately, ~40 junk entities/day
--   (numeric-quantity / possessive / temporal) reached the graph — the P5 FACT
--   junk gate was never ported to the entity path.
--
-- NOTE: the P4 gc_status re-animation guard is MOOT here — 0063 HARD-DELETED
--   losers, so 0/12,228 live rows carry data->>'gc_status'. The alias/article-
--   aware pre-lookup (code) is the real forward fix; this migration cleans the
--   ~53 re-fragments + the junk that accumulated since 0063.
--
-- ==========================================================================
-- MEASURED (live `legba`, read-only SELECTs, 2026-07-06; head 0075):
--   * entity_profiles total ................................ 12,228
--   * article/case-normalized collision clusters ........... 78
--       - ARTICLE-VARIANT clusters re-folded here .......... 59  (59 losers)
--       - same-raw-name / different-CLASS clusters HELD .... 11  (see below)
--       - temporal-junk clusters routed to junk-delete ..... 8   (both members)
--   * JUNK rows removed (M5 classes) ....................... 64
--       - number+unit quantity ............................. 21
--       - bare temporal / duration ......................... 42
--       - possessive-kinship ............................... 1  ("Donald Trump's son")
--   Net entity_profiles row delta: -123 (59 merged losers + 64 junk).
--
-- 20-ROW SAMPLE reviewed before freezing (loser -> survivor, links):
--   the Strait of Hormuz(4)   -> Strait of Hormuz(291)   [renamed to "Strait of Hormuz"]
--   the Axis of Resistance(1) -> Axis of Resistance(24)  [survivor renamed]
--   the Middle East(4)        -> Middle East(139)
--   the West Bank(2)          -> West Bank(59)
--   the Gaza Strip(2)         -> Gaza Strip(10)
--   the Persian Gulf(1)       -> Persian Gulf(21)
--   the Red Sea(2)            -> Red Sea(7)
--   the Supreme Court(1)      -> Supreme Court(38)
--   The White House(1)        -> White House(73)
--   The Foreign Ministry(1)   -> Foreign Ministry(144)
--   the National Assembly(1)  -> National Assembly(32)
--   Russian Security Council(11) -> the Russian Security Council(42)
--   JUNK: "188,000 barrels", "770 bln won", "four million euros",
--         "10 million barrels", "Donald Trump's son", "last week", "Today",
--         "the 21st century" — all removed.
--
-- HELD FOR REVIEW (NOT merged — distinct/ambiguous, same RAW surface, different
--   class; NOT article variants; the P4 class-homonym residue is a separate
--   concern from M4 re-fragmentation and some are genuinely ambiguous):
--     Hezbollah(entity 70 / person 46), Turkey(country 141 / entity 85 /
--     location 5), Norman(location 498 / entity 305), Rubio(entity 37 /
--     person 25), Starmer(entity 42 / person 31), Houston(entity 56 /
--     location 35), Kharkov(location 4 / entity 1), Popovic(person 4 /
--     entity 1), Neil(entity 1 / person 1), Cape Verde(country 70 /
--     location 3), Prince Edward Road(person 1 / location 1).
--   Also NOT re-typed by this migration: survivors whose most-links class is
--   sub-optimal (Middle East / Strait of Hormuz / Pacific Ocean = person;
--   West Bank = organization). The paired M6 code fix types NEW mentions
--   correctly; a targeted re-type migration can follow if desired.
--
-- ==========================================================================
-- HARD-DELETE — REVERSIBILITY IS AN OPERATOR PRE-APPLY BACKUP, NOT A TOMBSTONE.
-- ==========================================================================
--   Mirrors 0063: loser + junk rows are DELETEd from entity_profiles; their
--   signal_entity_links + entity_profile_versions cascade away via the
--   ON DELETE CASCADE FKs. There is NO in-table undo. BEFORE APPLYING, take a
--   pg_dump backup of the three affected tables:
--
--     pg_dump -U legba -d legba -Fc \
--       -t entity_profiles -t signal_entity_links -t entity_profile_versions \
--       -f entity_refold_0076_preapply.dump
--
--   (proposed_edges / facts / nexuses reference entities by TEXT name, not id,
--   so a deleted loser/junk name simply orphans its proposed_edges — entity_gc
--   quarantines those on its next tick; acceptable dup/junk cleanup.)
--
-- APPLY-WINDOW QUIESCENCE (recommended): applying is a SINGLE sub-second
--   transaction, but a concurrent entity_resolution write during the window
--   could insert a signal_entity_link pointing at a loser row that step (4)
--   then CASCADE-deletes — a one-time, self-healing transient (the next tick
--   re-links the signal onto the survivor by name). Apply during a quiet window
--   or briefly pause the entity_resolution cadence. Not a correctness
--   requirement — the transaction is atomic and leaves no dangling live row.
--
-- IDEMPOTENT: the link re-point is INSERT..ON CONFLICT DO NOTHING; the loser /
--   junk DELETEs find nothing on a second pass; the alias union de-dups; the
--   merge-version append is NOT-EXISTS-guarded on event='merge_0076'; the
--   version bump fires only while the survivor sits below its merge-version
--   number; the article-strip rewrite is a no-op once the article is gone. The
--   migrate runner also skips already-applied files via the ledger.
--
-- HOUSE RULE: routed through legba.data.migrate (raw mass-DELETE trips the
--   safety classifier). The runner wraps this file in ONE transaction + records
--   the ledger row — so there is NO inline BEGIN/COMMIT here (matching 0063).
--   TEMP TABLEs are ON COMMIT DROP inside that single wrapping transaction.
--
-- ORDER IS LOAD-BEARING (mirrors 0063):
--   (1)  re-point loser links onto the survivor,
--   (2)  copy loser names/derived_from onto the survivor  -- BEFORE the losers
--   (3)  append the survivor merge-version row (at version+1),  -- are deleted
--   (3b) bump the survivor's entity_profiles.version to that number,
--   (4)  DELETE the loser rows (CASCADE takes their leftover links + versions),
--   (5)  strip a leading article from the survivor surface (loser slot freed),
--   (6)  DELETE the junk rows (CASCADE takes their links + versions).

-- ==========================================================================
-- FROZEN DECISIONS (VALUES temp tables — dropped at COMMIT)
-- ==========================================================================
CREATE TEMP TABLE _merge_map (loser_id uuid, survivor_id uuid) ON COMMIT DROP;
INSERT INTO _merge_map (loser_id, survivor_id) VALUES
    ('6a6d720f-dbb2-481b-88ca-69b15ce4c34c'::uuid, 'd5333486-b218-4b10-bcec-98325e2ac5a7'::uuid),  -- the Amal Movement -> keeper
    ('7cd2e527-aede-4805-b0d9-49f1c5e6f961'::uuid, '11b68609-519e-4f35-809f-15188627741a'::uuid),  -- the Arabian Sea -> keeper
    ('a09fac54-2729-45e2-a676-729b892809ba'::uuid, '1ea85ac7-ad6a-4e57-8b64-4e6b53657e87'::uuid),  -- Atlas Lions -> keeper
    ('dd4cd684-6eb8-4633-8825-a3a9cf97efd2'::uuid, 'af803dec-aca5-472c-89c1-414751c2f69a'::uuid),  -- Axis of Resistance -> keeper
    ('ff5b733c-2886-4097-a11f-fe5f61fe818e'::uuid, '3a4f5458-6b01-4564-9230-ee994762c868'::uuid),  -- Azawad Liberation Front -> keeper
    ('4f628d3e-1a0f-4d43-9dfa-db52df64106b'::uuid, '0cb7fd61-921d-45e5-a31c-643c2d05c812'::uuid),  -- Chinese Communist Party -> keeper
    ('809244dd-640c-41e9-92f7-e3c87099d1bf'::uuid, '5643172d-ad66-42c8-b067-006a10842713'::uuid),  -- Coast Guard -> keeper
    ('17e846b5-f71c-4782-b8c7-8f2e9de692d2'::uuid, 'e027f45f-278d-4270-b441-16d74770497b'::uuid),  -- the Communist Party -> keeper
    ('b1ec432d-2339-4e91-bcc8-e92e4443e616'::uuid, '121bdce1-27be-4850-a904-437f6e4fbf69'::uuid),  -- The defense ministry -> keeper
    ('9955f50c-d850-42f5-88c6-ba62e7983364'::uuid, '4a96970e-40ed-469a-981f-2813cde6be32'::uuid),  -- Democratic Republic of Congo -> keeper
    ('01610381-8480-4662-afdc-9673b3cdaf09'::uuid, 'd35e036d-4e7d-495d-b16a-5793d4afe484'::uuid),  -- the Democratic Republic of the Congo -> keeper
    ('5ee8e9a4-90de-4a70-bfe3-7adb498dcc6a'::uuid, '639c03cd-d80f-42b2-84d3-48d3c8bce7b6'::uuid),  -- the Donetsk People's Republic -> keeper
    ('2eac6b31-5c8b-4af6-a9e4-cab14300f236'::uuid, '6d0285c9-5f3d-41b2-92cb-f257d94fd77c'::uuid),  -- the East Coast -> keeper
    ('331d708e-cf88-4087-b0eb-5bf8fb68690b'::uuid, '9527673d-78a2-4d02-b5d7-736bc2ddd5ed'::uuid),  -- European Parliament -> keeper
    ('2a9f3ff4-5f15-4c00-908e-5447e501dd1b'::uuid, 'afc9e911-1668-4748-91d4-aea232cf1083'::uuid),  -- the FIFA World Cup 2026 -> keeper
    ('91b3005a-954e-4d97-a5b5-f6c464f78cc4'::uuid, '34ef23c0-f1d8-40cf-8587-632f7ef8b29e'::uuid),  -- The Foreign Ministry -> keeper
    ('d41dc742-e149-470a-a836-cf4dd3a7dc25'::uuid, '2806ce27-bcc1-4b6d-b943-47a8ebc14d67'::uuid),  -- the Fourth of July -> keeper
    ('2b379f1e-e084-4454-b714-4bf515dcc67b'::uuid, '4351434b-4145-4770-8bab-fe7041256793'::uuid),  -- the Gaza Strip -> keeper
    ('ec969354-c561-4400-af9e-e9ab15657463'::uuid, 'f2a3713f-6741-46f8-bfae-090125677379'::uuid),  -- General Staff -> keeper
    ('cd558e6d-d855-4bf4-9207-5af80581b9df'::uuid, '8a050b06-c773-4880-b449-830ddfab1ece'::uuid),  -- a Geran-2 Seeker -> keeper
    ('fffe6461-ba58-443b-bd02-a158ca758db6'::uuid, 'c6331daa-f5a2-493e-9cc4-08599b33f6de'::uuid),  -- the Government Media Office -> keeper
    ('7f4779fa-9026-4641-b675-ba1142f3371d'::uuid, '06286b32-4d5a-4ccd-8a0a-1151a1cda449'::uuid),  -- the Grand Mosalla -> keeper
    ('ad8f028a-6479-4aff-afad-8a235227791c'::uuid, 'b80f44ba-201a-4c68-ace0-038c0ab888b5'::uuid),  -- High Court -> keeper
    ('78ab25b5-b157-4430-8fdb-00d9780c1637'::uuid, '18ac5e5a-4fe0-4e55-8201-cbfb73d30851'::uuid),  -- Horn of Africa -> keeper
    ('3134eeaa-befb-447f-a0fe-25d1d7c3dbbd'::uuid, 'e7b07ac4-8d5c-4ec2-aae6-b0f7a7966f8a'::uuid),  -- the Imam Khomeini Mosalla -> keeper
    ('8b9d0fce-d080-4285-ba43-8672cb5040f4'::uuid, '95c38f8b-090f-4959-8c5b-07d1aa478ab8'::uuid),  -- Indian Embassy -> keeper
    ('41508e61-c1a4-4ec2-80f2-16883a77bfad'::uuid, 'cd92f805-234a-4ff6-bb90-62e829592700'::uuid),  -- the Iran National Science Foundation -> keeper
    ('6b0e4875-1807-406d-8e6f-d1b215388513'::uuid, 'b5e50874-7374-407c-9a38-2df79991375e'::uuid),  -- Islamic Revolution Guards Corps -> keeper
    ('657b4322-30d8-4600-b931-474dfa64935d'::uuid, '60a41ccc-2a83-4834-acdb-57b00267c767'::uuid),  -- the Kharkov Region -> keeper
    ('c976f6bf-b7e7-4c97-933d-6b0311133adc'::uuid, '07c6cfa0-cc9e-4262-9e35-fd8bb382cc7f'::uuid),  -- Krasnodar Region -> keeper
    ('58600ad9-9b25-4610-84ac-e4e99c6187cb'::uuid, '12b6d191-96ed-4fa4-b248-ee179805d860'::uuid),  -- Lalique Museum -> keeper
    ('367508a8-9ea4-4d30-98ba-76e3edaf0200'::uuid, '2449035e-b198-468c-8b57-f71aef0a4f1d'::uuid),  -- the Lugansk People's Republic -> keeper
    ('337111e1-4069-4394-8fdd-6645d5a1ac93'::uuid, '90ef2a8b-b981-4cd3-a810-4ee6d85087dd'::uuid),  -- the Madlanga Commission -> keeper
    ('2e8b76fd-1d76-4dd0-ae84-1cade2dd5c49'::uuid, '147f2760-edc8-4dde-825d-cbac496fe676'::uuid),  -- Memorandum of Understanding -> keeper
    ('505119fd-b82d-4197-9ec9-ae79496727c2'::uuid, 'c89cd2ce-d5ed-4418-aabf-3f68a5c252fa'::uuid),  -- the Middle East -> keeper
    ('6d0bd0b1-6dd1-42eb-bb16-ca17a76bc7f8'::uuid, '72865aee-42a2-474c-bf01-18bdba726268'::uuid),  -- Ministry of Health -> keeper
    ('d3a8c91e-3d3d-4d60-b728-eb5bdf8b8739'::uuid, '32b0e09b-c194-48b4-9966-25570908ee03'::uuid),  -- the National Assembly -> keeper
    ('0e7c16ad-371c-4161-9feb-1710273a4b34'::uuid, '90d3c277-8934-4faf-89c7-0566e0dc1592'::uuid),  -- National Mall -> keeper
    ('00b4e3fc-35eb-44e6-8d5c-6026bc0486ed'::uuid, '4fcccb23-59ee-4746-a6ea-9eb21145ea8d'::uuid),  -- the NATO Summit -> keeper
    ('ea287b27-a11c-4dc4-aaaf-08033b0577d3'::uuid, 'd40edf93-2410-4a6e-b5b6-e5a89e79940c'::uuid),  -- New York Times -> keeper
    ('a6fab66a-9768-4b18-91b7-5d1670ca942d'::uuid, 'e908907e-16ce-4605-85d7-2fce6daeb667'::uuid),  -- the Northern Mariana Islands -> keeper
    ('88a7cd8c-5078-4dae-9069-8005c0ef4f4e'::uuid, '9790bf06-cd24-4789-b3d1-5e98feed11fb'::uuid),  -- Organization of the Petroleum Exporting Countries -> keeper
    ('9b7da3d3-d5c5-4598-93d0-bd386bef84ec'::uuid, '3958042c-8aa3-4acc-868b-6063cd12dd22'::uuid),  -- the Pacific Ocean -> keeper
    ('42bbedee-554f-416c-946c-34d4978e30ac'::uuid, '67d53493-be1e-4293-b530-1417cf0d48a4'::uuid),  -- the Persian Gulf -> keeper
    ('488825b4-9c95-404f-b7f2-5a88fae09ed5'::uuid, 'ac93e1f5-abb1-4020-848e-ae104b0cab66'::uuid),  -- the Red Sea -> keeper
    ('605b38d8-c864-4447-a069-caabf2800edc'::uuid, '8731bef3-ce6c-4c1e-b5b8-49095ab84cdf'::uuid),  -- the Revolutionary War -> keeper
    ('2ebcca7f-5685-4e55-94ac-6eefb9030a23'::uuid, '5322aa73-80a1-4da0-b409-2c10eea8ff5b'::uuid),  -- the Russian Federation -> keeper
    ('ba0ebce4-ed07-4670-afb9-64b3af4fc1aa'::uuid, 'aee01761-1428-440f-8593-22e5b854a505'::uuid),  -- Russian Security Council -> keeper
    ('7a06145e-1587-4c89-9ff9-7ba13737495c'::uuid, 'e70987ab-9411-48e4-9185-3e5eb88cdc72'::uuid),  -- Shanghai Cooperation Organization -> keeper
    ('c76d4fce-621d-493e-a919-24e05a7bc0ef'::uuid, '1e144cc1-48a5-4367-a996-6042bdc35dd5'::uuid),  -- Strait of Hormuz -> keeper
    ('78a20cc9-c501-4897-8529-686a18b14514'::uuid, '52ebe39a-beea-4ee9-bb20-9f6af728414e'::uuid),  -- the Supreme Court -> keeper
    ('5092b863-3b22-4444-b980-dac4d652b51f'::uuid, '83619fa1-a464-4412-99d4-7d88d0a3dafe'::uuid),  -- Tigray People's Liberation Front -> keeper
    ('699936e1-c67f-40d4-872b-fba25a537d38'::uuid, 'f546667e-bcd6-42f0-afaa-2239f7b899b9'::uuid),  -- Ukrainian Defense Forces -> keeper
    ('c613566c-06d1-4ee7-9935-12e5316ba0d8'::uuid, 'e3206605-1d07-4cc2-9014-43ee16797b89'::uuid),  -- Ukrainian Navy -> keeper
    ('c3f115f3-9887-454f-8a31-7aa4100b09de'::uuid, '47a28f16-4e43-432a-8201-e6a4b0fade79'::uuid),  -- the University of Trieste -> keeper
    ('1c1673a2-5fa9-4d07-af92-0c1499b795bf'::uuid, '3e7a7363-766e-47ff-89bf-b2b0c122f3d9'::uuid),  -- the West Bank -> keeper
    ('eba8ed60-588e-4d75-adc3-ddeda6a468e6'::uuid, '188965a2-4ebe-4e09-bc0f-60e3ed3cd99f'::uuid),  -- The White House -> keeper
    ('fb638395-e7ff-4b38-b47e-7a4cf76067d7'::uuid, 'bd0f160d-1a76-4c70-adda-5bc087387940'::uuid),  -- The Zimbabwe Republic Police -> keeper
    ('80b09d5f-adc7-488f-a036-c5a11313d603'::uuid, '92081333-d75e-4233-9bd4-cfa61d2613fd'::uuid);  -- the Zion Church -> keeper

CREATE TEMP TABLE _junk (entity_id uuid) ON COMMIT DROP;
INSERT INTO _junk (entity_id) VALUES
    ('785cde58-1297-4486-9482-2f153a4b4266'::uuid),  -- 1,000 trillion won
    ('e5001402-03e7-40e7-be42-4fdddd338742'::uuid),  -- 10 million barrels
    ('10da4a9c-d221-4dfa-bc9a-84575d079df8'::uuid),  -- 115 acres
    ('119c9b3b-3e74-4923-8818-3cf169e28c85'::uuid),  -- 1.34 million hectares
    ('48c002b1-2762-40dc-909c-672d008ab84e'::uuid),  -- 15 million yen
    ('46fdc08e-0367-4d4f-ac75-0456055b9900'::uuid),  -- 160 bln won
    ('9b98d1a2-f168-471a-9539-e4678a90795d'::uuid),  -- 188,000 barrels
    ('919de50d-400a-4d4f-9081-0c1a1da45066'::uuid),  -- 1.94 trillion yuan
    ('804b5481-bff6-402b-8028-ba5ec2df692d'::uuid),  -- 19 acres
    ('d968bcf4-e213-4b2c-a7ce-d23b5a192360'::uuid),  -- 19 million barrels
    ('a050802d-b593-471c-b879-eaf496513f08'::uuid),  -- 20 M barrels
    ('286e363e-1e39-4f82-b244-5c3195e59f6f'::uuid),  -- 21st century
    ('c3de3290-e9fd-4ae4-a8b3-85edcfb313d5'::uuid),  -- 2 M barrels
    ('7716722e-f514-47dd-a6ef-db6bbbb77e58'::uuid),  -- 32.2 trillion won
    ('b41017aa-a1e5-4f8c-a45d-9f230991626c'::uuid),  -- 55.3 million bpd
    ('fa75a54d-bbb4-42ce-8206-d44adc529383'::uuid),  -- 70 bln euros
    ('3e5654d7-6137-42fc-b37a-d768514390ae'::uuid),  -- 770 bln won
    ('a7e671f0-f255-4359-802a-671f58c8c5ec'::uuid),  -- 8.1 million barrels
    ('62bd4579-5165-48ad-bd63-4b77ad5cc9aa'::uuid),  -- 885 bln won
    ('e5b1f322-66cf-48ec-b3a9-ae98a275fa41'::uuid),  -- 900 hectares
    ('5dd7ab9f-42c5-4f80-9c13-4645dd3f823f'::uuid),  -- a day
    ('cd0fd121-96ef-498f-83b8-281e2c6a6cd3'::uuid),  -- afternoon
    ('4c45bb63-4f83-4bb0-b141-421b857924a4'::uuid),  -- a month
    ('ea2a0989-a4d2-4486-bdfa-79e2b8c6eaa0'::uuid),  -- a second day
    ('4fca46df-1743-442d-8a20-8579ea514e17'::uuid),  -- a week
    ('d823e80f-c507-4567-a84a-7327c3e15dd3'::uuid),  -- a year
    ('bbc28013-038a-4809-aaf1-7b55be9dbbe0'::uuid),  -- coming days
    ('c827f310-58fb-420e-aad9-3fb54edb60af'::uuid),  -- Donald Trump's son
    ('d9ff732d-f73f-4b09-91a3-c9f710160bae'::uuid),  -- evening
    ('06266596-e10c-430f-9c47-8c5a68b6ba36'::uuid),  -- first day
    ('bcbb3494-3eb6-4996-a91f-6d037d2d807a'::uuid),  -- four million euros
    ('754f90eb-b9e7-4b45-b548-1a48428c47bd'::uuid),  -- last day
    ('920b5852-fbea-4dac-a927-f73522ba4dbb'::uuid),  -- last month
    ('a53c5913-3e42-43f8-8c09-269c40d611f3'::uuid),  -- last week
    ('615a2284-dc5d-4c2f-9caa-141325fc9bee'::uuid),  -- last year
    ('8a9020ec-d67e-4f5e-ac96-7c631323b00e'::uuid),  -- morning
    ('8fe8e619-7b91-4e69-8282-a7856ebd5e4d'::uuid),  -- next month
    ('f944ba4b-3821-40d9-ac2e-72674c3747e8'::uuid),  -- next week
    ('e61a309b-84c6-4b9d-bd51-cfc006531f7a'::uuid),  -- next year
    ('96b6c7e3-e9df-4e2b-9536-d35807badb08'::uuid),  -- night
    ('a036d3e7-5928-4142-8460-eff69a85c64b'::uuid),  -- past 24 hours
    ('35d30b2a-3fed-42a8-b671-41fcb80b5060'::uuid),  -- previous year
    ('f6fce680-4769-44ce-975c-e96c15622690'::uuid),  -- seven tons
    ('7d406a92-0e74-4a5e-9e8c-996ed2ff6d61'::uuid),  -- the 14th century
    ('c4379db4-0e10-47ef-8db7-7a423956a114'::uuid),  -- the 2025
    ('f20e4a46-6e35-489a-932e-8a7808f01b76'::uuid),  -- the 2026
    ('a6de9132-8475-4f1c-83c7-a9dc05f3ceb3'::uuid),  -- the 21st century
    ('4539c754-58b8-43c2-8d72-3b9a15d0fc97'::uuid),  -- the first day
    ('b97c6039-d9b3-43e1-abb6-692670d00eee'::uuid),  -- the first days
    ('d0842081-6c3f-4eb3-b78b-63834a02b71c'::uuid),  -- the first year
    ('af9aa805-71fe-47ff-977f-bcaabfd781f6'::uuid),  -- the fourth year
    ('98b2051b-2d57-4c12-ac9c-fa191671b798'::uuid),  -- The past 100 days
    ('2e002fb1-e293-4105-9b4f-ed40e42377c0'::uuid),  -- the past 18 months
    ('64d40e62-20d5-47cb-aead-353d7cb1391f'::uuid),  -- the past 24 hours
    ('71ab4ed1-98f4-4643-8a33-931b3cd2823c'::uuid),  -- the second day
    ('818c823e-8f6e-49a8-bf40-052a25dce2a6'::uuid),  -- the third day
    ('a42e3f9c-1483-43a5-acdc-e06f67ef7a95'::uuid),  -- The week
    ('7573b2a0-13d2-42d2-854c-e2ac73ab67d3'::uuid),  -- the weekend
    ('b03c6028-3633-4e09-940f-b58016bdf3ce'::uuid),  -- the Year
    ('065d7d75-3cb3-4dad-8c90-6dc0ca15cc81'::uuid),  -- third day
    ('7de5c5e1-b170-4822-bcfb-a9649aeaf675'::uuid),  -- Today
    ('f14c38f9-f79d-4dcc-94b0-a45453c2cdcb'::uuid),  -- tomorrow
    ('55168c78-9c3f-4a8b-a283-5465246dd9cf'::uuid),  -- weekend
    ('d9138ab2-2de7-443b-9ca3-7bc1d83cb71c'::uuid);  -- yesterday

CREATE INDEX ON _merge_map (loser_id);
CREATE INDEX ON _merge_map (survivor_id);

-- ==========================================================================
-- (1) RE-POINT links from every loser onto its survivor. GENERIC join so links
--     added after the snapshot are re-pointed too. PK (signal_id, entity_id,
--     role) -> ON CONFLICT DO NOTHING collapses a signal already linked to both.
--     (The losers' now-redundant original links are removed by the CASCADE when
--     the loser rows are DELETEd in step 4.)
-- ==========================================================================
INSERT INTO signal_entity_links
    (signal_id, entity_id, role, confidence, analyst_id, analyst_version, run_id, created_at)
SELECT sel.signal_id, m.survivor_id, sel.role, sel.confidence,
       sel.analyst_id, sel.analyst_version, sel.run_id, sel.created_at
  FROM signal_entity_links sel
  JOIN _merge_map m ON sel.entity_id = m.loser_id
ON CONFLICT (signal_id, entity_id, role) DO NOTHING;

-- ==========================================================================
-- (2) SURVIVOR PROVENANCE — read the losers BEFORE they are deleted: union every
--     loser's canonical_name into the survivor's data.merged_aliases and every
--     loser's derived_from marker into the survivor's derived_from (deduped,
--     ORDER BY for determinism).
-- ==========================================================================
UPDATE entity_profiles s
   SET data = jsonb_set(
                  COALESCE(s.data, '{}'::jsonb),
                  '{merged_aliases}',
                  (
                    SELECT COALESCE(jsonb_agg(DISTINCT a ORDER BY a), '[]'::jsonb)
                      FROM jsonb_array_elements_text(
                             COALESCE(s.data->'merged_aliases', '[]'::jsonb) || agg.alias_json
                           ) AS a
                  )
              ),
       derived_from = (
           SELECT COALESCE(array_agg(DISTINCT d ORDER BY d), '{}'::uuid[])
             FROM unnest(s.derived_from || agg.derived) AS d
       ),
       updated_at = now()
  FROM (
      SELECT m.survivor_id AS sid,
             COALESCE(
                 jsonb_agg(DISTINCT to_jsonb(l.canonical_name))
                     FILTER (WHERE l.canonical_name IS NOT NULL),
                 '[]'::jsonb
             ) AS alias_json,
             COALESCE(array_agg(DISTINCT ld) FILTER (WHERE ld IS NOT NULL), '{}'::uuid[]) AS derived
        FROM _merge_map m
        JOIN entity_profiles l ON l.id = m.loser_id
        LEFT JOIN LATERAL unnest(l.derived_from) AS ld ON TRUE
       GROUP BY m.survivor_id
  ) agg
 WHERE s.id = agg.sid;

-- ==========================================================================
-- (3) SURVIVOR MERGE-VERSION ROW in entity_profile_versions (append-only) at
--     version+1. Preserves the merge record even though the losers' own version
--     rows cascade away in step 4, and keeps (entity_id, version) monotonic +
--     unique. NOT-EXISTS-guarded on (entity_id, event='merge_0076') for
--     idempotency. Step 3b then advances entity_profiles.version to match.
-- ==========================================================================
INSERT INTO entity_profile_versions
    (entity_id, version, data, analyst_id, analyst_version, run_id)
SELECT s.id, s.version + 1,
       jsonb_build_object(
           'canonical_name', s.canonical_name,
           'entity_class',   s.entity_class,
           'merged_aliases', COALESCE(s.data->'merged_aliases', '[]'::jsonb),
           'merged_losers',  (SELECT count(*) FROM _merge_map m WHERE m.survivor_id = s.id),
           'event',          'merge_0076',
           'migration',      '0076_entity_refold_and_junk'
       ),
       '0076_entity_refold_and_junk', NULL, NULL
  FROM entity_profiles s
 WHERE s.id IN (SELECT DISTINCT survivor_id FROM _merge_map)
   AND NOT EXISTS (
       SELECT 1 FROM entity_profile_versions v
        WHERE v.entity_id = s.id
          AND v.data->>'event' = 'merge_0076'
   );

-- ==========================================================================
-- (3b) VERSION BUMP — advance every merged survivor's entity_profiles.version to
--     its merge-version number so (entity_id, version) stays monotonic + unique.
--     Guarded to fire once: only a survivor still BELOW its merge_0076 version is
--     bumped, so a forced re-run is a no-op.
-- ==========================================================================
UPDATE entity_profiles s
   SET version    = s.version + 1,
       updated_at = now()
 WHERE s.id IN (SELECT DISTINCT survivor_id FROM _merge_map)
   AND s.version < (
       SELECT v.version FROM entity_profile_versions v
        WHERE v.entity_id = s.id
          AND v.data->>'event' = 'merge_0076'
   );

-- ==========================================================================
-- (4) HARD-DELETE the loser rows. The ON DELETE CASCADE FKs remove their leftover
--     signal_entity_links (already re-pointed in step 1) and their
--     entity_profile_versions in the same statement.
-- ==========================================================================
DELETE FROM entity_profiles WHERE id IN (SELECT loser_id FROM _merge_map);

-- ==========================================================================
-- (5) SURVIVOR ARTICLE-STRIP — with the loser slot freed, strip a single leading
--     article from any merged survivor's surface ("the Strait of Hormuz" ->
--     "Strait of Hormuz", "The White House" -> "White House") so the canonical
--     display is clean and the forward exact-match path converges on it.
--     entity_type/entity_class are UNCHANGED (this migration re-folds, it does
--     not re-type). Guarded: only fires when a leading article is present, the
--     remainder is >= 2 chars, and no OTHER live row already holds the stripped
--     (lower(name), class) — a blocked rewrite is a safe no-op (merge still
--     stands). Idempotent: a survivor with no leading article is untouched.
-- ==========================================================================
UPDATE entity_profiles s
   SET canonical_name = regexp_replace(s.canonical_name, '^(the|a|an)\s+', '', 'i'),
       updated_at     = now()
  FROM (SELECT DISTINCT survivor_id FROM _merge_map) m
 WHERE s.id = m.survivor_id
   AND s.canonical_name ~* '^(the|a|an)\s+'
   AND length(regexp_replace(s.canonical_name, '^(the|a|an)\s+', '', 'i')) >= 2
   AND NOT EXISTS (
       SELECT 1 FROM entity_profiles o
        WHERE lower(o.canonical_name) = lower(regexp_replace(s.canonical_name, '^(the|a|an)\s+', '', 'i'))
          AND o.entity_class = s.entity_class
          AND o.id <> s.id
   );

-- ==========================================================================
-- (6) HARD-DELETE the junk rows (number+unit quantity / possessive-kinship /
--     bare temporal). The CASCADE removes their links + versions.
-- ==========================================================================
DELETE FROM entity_profiles WHERE id IN (SELECT entity_id FROM _junk);
