-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0078_nexus_junk_and_canonicalize.sql  (DQ M7/M8 — nexus-write audit 2026-07-06)
--
-- PROBLEM: nexus subject/object are FREE TEXT (no entity FK). The two nexus
--   gates (proposed_edge_governance co-occurrence promotion + relationship_reifier
--   typed reification) minted two classes of bad OPEN edges into the signed /
--   hostility graph + the world / escalation compositions:
--     M7 — a JUNK / VAGUE endpoint the gates' is_junk_entity did not yet cover:
--          a relative-time phrase ("United States co occurs with this week",
--          "... last week", "... past day", "... morning"), a vague bloc /
--          adjective / role singleton ("West hostile to the Islamic Revolution",
--          "IRNA co occurs with Islamic", "United States hostile to Leader",
--          "Europe co occurs with annual"), or a bare quantifier plural
--          ("Hundreds hostile to Israel", "Iraq involved in millions").
--     M8 — DEMONYM / surface-variant FRAGMENTATION (subject/object never rewritten
--          by the P4 entity fold):
--            (a) self-loops — an edge whose two endpoints are the SAME referent
--                ("Africa co occurs with African", "Iran co occurs with Iranians",
--                "Houthi co occurs with Houthis");
--            (b) split dyads — the SAME dyad fragmented across demonym/alias/plural
--                surface variants ("Russia|Ukrainian" + "Russian|Ukraine" +
--                "Russia|Ukraine"), read as independent corroboration → dyad-count
--                inflation in the world / escalation compositions.
--
-- PAIRED CODE FIX (keeps it out — same commit):
--   * src/legba/data/_entity_canon.py — is_junk_entity now rejects the vague
--     bloc/adjective/role singletons (_VAGUE_ENDPOINT_TOKENS) + quantifier
--     plurals (_QUANTIFIER_PLURAL_ENTITIES); NEW same_referent() folds a
--     continent/demonym/alias/plural self-loop (incl. the plain "Houthi"/
--     "Houthis" pair the canon does not lemma-map).
--   * proposed_edge_governance.py + relationship_reifier.py — BOTH producers now
--     drop an edge when same_referent(subject,object) is True (was a bare
--     lower() equality that missed "Houthi"/"Houthis"); is_junk_entity (which
--     both already call on each endpoint) now covers the M7 vague/quant classes.
--   Going forward both producers ALSO canonicalize each endpoint before the
--   write, so a fragmented dyad ("Russian"/"Ukrainian") collapses to the
--   canonical surface at write time and the open-triple UNIQUE index dedups it.
--
-- THIS MIGRATION closes (REVERSIBLE — no delete) exactly the frozen OPEN
--   source_type='agent' rows enumerated below, by PRIMARY KEY (the 0067 pattern
--   — computed + reviewed against live `legba` with the paired code's real
--   is_junk_entity / same_referent / canonicalize_entity, NOT re-derived in SQL):
--     M7  — valid_until=now()                     (88 junk-endpoint rows)
--     M8a — valid_until=now()                     (35 self-loop rows)
--     M8b — superseded_by=<canonical keeper>,     (76 fragment duplicates,
--           valid_until=now()                      70 canonical dyads kept)
--   Seed rows (source_type='seed') + the operator 'manual' row are NEVER matched.
--
-- CONSERVATIVE: M8b groups OPEN rows ONLY by IDENTICAL canonical triple
--   (canonicalize_entity(subject), rel_type, canonicalize_entity(object)) — two
--   DISTINCT real dyads are never merged; only demonym / alias / plural / article
--   variants that canonicalize identically. The kept row per dyad is the
--   canonical-surface row (else the highest-confidence one). 3 kept dyads
--   have no canonical-surface fragment, so their keeper retains a demonym surface
--   (HELD as-is — the dedup still collapses the dyad; a surface rewrite is left
--   out to keep this migration purely reversible/close-only). Listed below.
--   M7 is SCOPED to the audit's relative-time + vague/quant class ONLY; the older
--   number-word ("... co occurs with first/two") + clock/NWS/sports/len<=2 junk
--   endpoints the same code gate now also covers at write-time are NOT closed
--   here (out of M7 scope — see the session report; a broader sweep is a
--   separate operator call).
--
-- REVERSIBLE: valid_until->NULL (+ superseded_by->NULL for M8b) reopens every
--   row; NO backup needed (no hard-delete). IDEMPOTENT: the valid_until IS NULL /
--   superseded_by IS NULL guards make a re-run a no-op. Routed through
--   legba.data.migrate — the runner wraps this file in ONE transaction + records
--   the ledger row, so there is NO inline BEGIN/COMMIT (matching 0065/0067/0077).
--
-- ==========================================================================
-- MEASURED (live `legba`, read-only SELECTs, 2026-07-06; migration head 0077):
--   open agent nexuses ...................................... 2733
--   M7 junk-endpoint (temporal+vague+quant, a row can hit >1 class):
--     temporal-phrase endpoint ............................. 32
--     vague bloc/adjective/role endpoint ................... 40
--     quantifier-plural endpoint ........................... 20
--     UNION rows closed (M7) ............................... 88
--   M8a self-loop rows closed .............................. 35
--   M8b fragment duplicates closed ........................ 76  (70 dyads kept)
--   GRAND TOTAL rows closed ............................... 199
--   non-agent (seed/manual) rows matched (sanity) ......... 0
--
-- 20-ROW M7 SAMPLE reviewed before freezing (subject | predicate | object):
--   Niger                      | co occurs with | a decade                [temporal]
--   Islamist                   | co occurs with | a decade                [vague]
--   Islamist                   | co occurs with | Niger                   [vague]
--   First                      | co occurs with | last week               [temporal]
--   United States              | co occurs with | last week               [temporal]
--   Switzerland                | co occurs with | last week               [temporal]
--   Russia                     | co occurs with | West                    [vague]
--   Ukraine                    | co occurs with | West                    [vague]
--   Russia                     | co occurs with | Western                 [vague]
--   West                       | co occurs with | Western                 [vague]
--   Iran                       | co occurs with | Islamic                 [vague]
--   Russia                     | co occurs with | past day                [temporal]
--   Ukraine                    | co occurs with | past day                [temporal]
--   Yonhap                     | co occurs with | morning                 [temporal]
--   SEOUL                      | co occurs with | morning                 [temporal]
--   SEOUL                      | co occurs with | this week               [temporal]
--   Yonhap                     | co occurs with | this week               [temporal]
--   Putin                      | co occurs with | West                    [vague]
--   Nigeria                    | co occurs with | This Day                [temporal]
--   South Africa               | co occurs with | Thousands               [quant_plural]
--
-- M8a SELF-LOOPS closed (35):
--   Africa                 | co occurs with   | African               
--   American               | located in       | United States         
--   Americans              | co occurs with   | United States         
--   Asia                   | co occurs with   | Asian                 
--   Belarus                | co occurs with   | Belarusian            
--   British                | part of          | United Kingdom        
--   China                  | part of          | Chinese               
--   Chinese                | hostile to       | China                 
--   Chinese                | located in       | China                 
--   Colombia               | co occurs with   | Colombians            
--   Dutch                  | co occurs with   | Netherlands           
--   Europe                 | co occurs with   | European              
--   European Union         | co occurs with   | the European Union    
--   French                 | part of          | France                
--   French                 | located in       | France                
--   Houthi                 | co occurs with   | Houthis               
--   India                  | co occurs with   | Indians               
--   Iran                   | supplies weapons | Iranian               
--   Iran                   | co occurs with   | Iranians              
--   Israel                 | co occurs with   | Israelis              
--   Israeli                | part of          | Israel                
--   Israeli                | located in       | Israel                
--   Kenya                  | co occurs with   | Kenyan                
--   Lebanese               | located in       | Lebanon               
--   Liberia                | co occurs with   | Liberian              
--   Malawi                 | co occurs with   | Malawians             
--   Russia                 | co occurs with   | Russians              
--   Somali                 | co occurs with   | Somalia               
--   South Africa           | co occurs with   | South African         
--   South Africa           | co occurs with   | South Africans        
--   Swiss                  | co occurs with   | Switzerland           
--   Ukraine                | allied with      | Ukrainian             
--   Ukraine                | co occurs with   | Ukrainians            
--   US                     | co occurs with   | the United States     
--   Venezuela              | co occurs with   | Venezuelans           
--
-- M8b sample (keeper  <-  merged fragment surfaces; top 12 of 70 dyads):
--   keep [JD Vance hostile to Iran]  <-  JD Vance|Iran ; JD Vance|Iranian ; JD Vance|Iranian
--   keep [SEOUL located in South Korea]  <-  SEOUL|South Korea's ; SEOUL|South Korean
--   keep [Trump hostile to Iran]  <-  Trump|Iran ; Trump|Iranian
--   keep [Russia hostile to Ukraine]  <-  Russian|Ukraine ; Russia|Ukrainian
--   keep [Israeli hostile to Lebanese]  <-  Israeli|Lebanon ; Israel|Lebanese
--   keep [Iran allied with Pakistan]  <-  Iranian|Pakistan
--   keep [Iran party to United States]  <-  Iran|US
--   keep [Donald Trump party to Iran]  <-  Donald Trump|Iranian
--   keep [Tehran located in Iran]  <-  TEHRAN|Iranian
--   keep [Putin leader of Russia]  <-  Putin|Russian
--   keep [Donald Trump leader of United States]  <-  Donald Trump|America
--   keep [Iran co occurs with TEHRAN]  <-  Iranians|Tehran
--
-- M8b keepers HELD with a demonym surface (no canonical fragment existed):
--   Iran | co occurs with | the United States
--   Israeli | hostile to | Lebanese
--   Israeli | hostile to | Palestinians
-- ==========================================================================

-- --------------------------------------------------------------------------
-- M7 — close junk / vague / relative-time endpoint nexuses (valid_until=now()).
-- --------------------------------------------------------------------------
UPDATE nexuses n
SET valid_until = now(),
    updated_at  = now()
WHERE n.valid_until IS NULL
  AND n.superseded_by IS NULL
  AND n.source_type = 'agent'
  AND n.id = ANY (ARRAY[
      '042e14fe-d517-4c5e-8f6e-84991a041935','06b1d087-3e2f-473f-826f-549f418fa227','09fe917f-072a-4abb-b231-ddc52eaed91e','0b199121-9f6c-40a1-a09f-f293fc518089',
      '0d15084a-885d-4f3b-b1c5-a6192d4c10b9','17b700f2-3778-4781-80eb-de144f06f099','1b7d75f8-b43c-40f6-83e9-3d7b72d36f62','1bbb5f5f-185c-45b6-8342-17300871a1e4',
      '1c130d90-5d42-40c8-a7a8-eccbb18ea3bb','28d4a380-0b15-4878-b05a-9f0591daa257','28fdf54f-eb6b-40bc-9f6a-1fcadbd7e538','2c4993cb-2de1-4ed6-b1cd-ac2078c524f5',
      '32369a7b-4619-49e2-a648-de2f527027e6','331d7c35-a83e-419e-b513-d56edc9d4604','355b5d82-36fd-4247-9d72-971502d04386','3755c962-d835-4abe-9988-f7b6c3b310e3',
      '3b7c9d5e-1dc3-44f9-b970-51a6e85f2895','45e1de79-c28f-4ada-a6c7-607a4e8fd10c','4c65e0d5-c710-4ab1-8951-e42ea2a52e05','4cedae15-d5b0-49aa-9a7b-691c3ebb64a5',
      '4db469ba-4927-4313-a444-1f2e9a44dc03','4eb9cc59-94eb-4f7e-b9c5-2c2bc558dd48','513636ea-f2cb-4de2-ab00-20f8b9888068','518b263b-03d5-444e-8f4a-c708b46c8aa6',
      '52e0c7a6-3b84-4c8c-be44-bc2c0e9ebe71','54839e46-f1c1-4f8b-9bed-08b9c7d04cc2','55f33c23-8cb0-45f1-9c61-a15a80da3c8f','56de4aa9-979a-4bc9-9a19-917ff570b79d',
      '58344bd4-d876-4e83-a593-cf238d84821e','5fc8f011-5a2c-49a5-9a59-ae63945c26a5','6142f93e-977b-48c5-9749-47336267cb57','61e81912-5b7c-4485-b47a-b8adde028eab',
      '6690b74c-6db2-439f-9702-c3d36d6d3dd7','6f5221fd-86d2-48d9-8720-9400425f575c','6fad1607-9cb9-4fea-a2ee-3fa3fc0bbf00','6fcca0bb-d320-49d3-986d-ca9f75d01d51',
      '706f0da1-3df3-4df1-bbef-01cf91c2c052','70b97736-4f9a-42f1-9b49-da14b6171a6b','727a1e36-49b4-4c80-89ed-4ca5a8f37f06','74c4d79a-9018-4f97-aa04-6ec6f61b06e4',
      '7bc9c5e8-98da-413c-a3c1-7d56b50c4145','7be861b9-6905-4f65-8b3b-edd559eafb01','7e08f525-ef6f-4025-bad0-ceccbf6bf97c','7fecd0c3-af30-4d05-b12e-6b2c13b6c7ed',
      '814183b6-c638-4874-ab42-306b986f8ebc','82836320-f3f2-4819-83dd-17c1bf649163','83e135fd-4545-4326-823c-be52c448242d','8aaae747-7db2-479a-8914-2846449bee1e',
      '916ead27-222d-42a2-9325-8d0c139a1627','9406d807-356a-4bbd-a856-877593ae1dd2','9694bb45-8beb-4f6e-b527-873cc4e40484','99d0263a-0257-411e-a4ef-02e712aa7d3e',
      '9b522958-fda2-4dfc-b2e9-5516a984bdad','9dae488f-42af-4de0-9315-b4440d83d5a3','a3d9570f-0c35-4bd7-bebf-3c744173921e','a9bbfa3e-11b0-472c-8d2a-0d41131f3bd6',
      'a9cbefd7-0add-49d0-bb92-ac7f8507e032','b2309dbd-e96e-41fa-beae-8d96b37ea8af','b33e9c9e-84a2-4fa1-8a38-d43e064fedd4','b361e3ec-d073-4fd7-9ec0-e4144a6a340e',
      'b36a09c9-7edd-4cbe-bc26-8966c0b0ebff','b5d51d23-2eda-4593-96f7-817de545a1ce','b6b0a701-5f7c-4677-b938-78d95e52cc99','baa171b7-bee3-4cc5-8d17-d1f5713b4db2',
      'be0642f6-7313-4cb7-a5ca-81b8428b6e09','c0e6c3d1-f12f-4aac-a30b-15f352ffa60f','c4e8edf0-14b1-4e54-a2d6-5027ef6d78ab','c760b241-f894-469e-bce9-076ede941873',
      'cbc25070-2b3c-42a9-940a-f804e20423cf','ceafc976-2403-4c9d-8c6c-ed2f16c57895','cf1cc9cd-1a72-40db-91cc-d872f2fbef8b','d04b4c12-aa5e-43e6-b6dd-f42a4f6bd07d',
      'd22f0c5e-e30e-4952-b6c4-160e14ee9533','d3a24ffd-895a-48b4-bdfc-64e8a32685db','d72c1309-a7a6-41aa-b3e4-9f62ee2d1821','dbf899d2-df57-4662-b5fa-27cccdfe880b',
      'df6bed28-e392-439d-90bb-e72bf07c9d8e','e43517ca-8f05-4816-be78-82fd9b9946c1','e55c55e2-d49f-4286-ad1a-767a25c73167','e75c7493-f363-4941-896b-9ccf4e91264c',
      'e9fc78bf-1761-4b3a-9c00-6e8b09408eae','ead1f47a-9b40-4a8e-a215-7f6a40cf850a','ee1126a4-1505-423c-a298-e9f45ec147f2','f2986109-ef9a-45c5-8349-1109be0fe684',
      'f7e282b3-23cf-4192-8679-f540a709aa83','f8cb8f98-518a-4e43-94c2-73170ba88712','f9c0bd1d-eaf2-4b00-a811-f1e3a4bbcda4','fbe734d2-8d5e-4cea-a1b7-4ae167fbe8a0'
  ]::uuid[]);

-- --------------------------------------------------------------------------
-- M8a — close self-loop nexuses (subject/object same referent; valid_until=now()).
-- --------------------------------------------------------------------------
UPDATE nexuses n
SET valid_until = now(),
    updated_at  = now()
WHERE n.valid_until IS NULL
  AND n.superseded_by IS NULL
  AND n.source_type = 'agent'
  AND n.id = ANY (ARRAY[
      '020a2260-8277-4fca-98a0-4b46e17bfd8d','071e4c8f-d129-4aa5-a2ff-559a4b2e038a','1b50d75a-d84a-4bfe-90ef-e1f262371f70','1dd6a935-0923-4160-9d99-2a6bfeac9033',
      '21eec140-a402-46c7-af97-ce8ce6b75d92','241460d4-a051-487a-b852-24b25d38cc69','274b3b3c-d887-45bf-9d81-3d7ee86e2c0d','2ad8f084-5a59-4b06-afdd-b27575ccb043',
      '373c4630-6b89-47f4-9d14-1c7e442a27d0','472ac4ac-8a1b-494c-b93b-56b91e36e3dc','4b9fb1b2-ed80-41ee-81e1-9c96e2e7d2ef','4d9b10aa-d9fa-41fb-bde1-f9d22ce606e9',
      '4f83da8a-7486-4c6e-894c-ebb67a46ab7f','5160bb55-b647-463e-ab3c-387913899d05','585931f0-83d5-40d9-8926-2de8fd82445e','5b31af04-7eb4-4afa-aa56-9fd61228daaf',
      '672ac872-c48a-4e9d-8c99-5d6a87158136','70a1bdd9-1fd7-40e5-9149-b052bc30b3f1','77c21d1f-36bd-44e5-8574-66bc77f73454','7e6474c7-9f37-4c19-9370-3a177b69a4d4',
      '8433fcd4-ebef-4dd6-90ee-61b6ce2721b1','854bc1cc-668c-4af2-aaaf-c5729db6bbb3','9041cde3-a4ec-4ad2-a52d-5d070c00db06','a4392345-4aaa-45bb-be97-418c6c4113be',
      'a7e3bcfc-e900-40cc-93d1-c785bc478236','b0839cfd-db4c-4608-8533-7c6264cdc875','bb15d2a6-59fc-42ce-998d-ead08a8eeecc','c2bcdf03-c83a-43f0-ada9-ad427bbc48d0',
      'c5dffcd4-8bad-4ef7-a496-44abcb4bb5be','d6ca9f53-5825-4061-8dde-693f2c277739','e4611746-7f33-42e2-a5ee-6d8e09dfabe5','e6239bac-85c7-4491-abb1-883bd755e422',
      'ea7047ce-6af1-440f-8520-9911f35a9f75','ecc66ddd-0c27-4015-93f1-93edb353a26d','ff856089-25bc-49c3-a938-fcf96e337531'
  ]::uuid[]);

-- --------------------------------------------------------------------------
-- M8b — merge fragmented duplicates INTO their canonical keeper: point each
-- duplicate's superseded_by at the kept dyad row and close it. The (dup,keeper)
-- mapping is frozen below (keeper is NEVER itself a dup). REVERSIBLE.
-- --------------------------------------------------------------------------
UPDATE nexuses n
SET superseded_by = m.keeper,
    valid_until   = now(),
    updated_at    = now()
FROM (VALUES
        ('0524da39-ff47-4210-8bf7-c5480dce404a'::uuid, '6fc1586f-5a4b-4994-9063-9c8eb968d325'::uuid),
        ('0f86ef35-7501-4e13-9f95-40c611b24893'::uuid, 'f10d47b5-61ef-49b0-b7d2-f99e5490cb97'::uuid),
        ('1327a436-4ad2-40cc-bdcb-424954747e2e'::uuid, 'e5b427b7-3221-4e4c-812c-8e56e63b6943'::uuid),
        ('15d76a5f-3dbe-4651-9f9d-5335672515b9'::uuid, '50c9883c-d033-495f-aaaf-16ee4cfae3f8'::uuid),
        ('19faf960-6fb7-4505-b411-5d102bde68a9'::uuid, 'b0c4ac04-9c02-4a5f-9f07-fa335e838105'::uuid),
        ('1dcb12f6-e9db-4ae2-a476-92fee369b247'::uuid, '89c1b6c6-5201-401f-a1f1-bf4ba360eded'::uuid),
        ('1e385d64-3116-44ca-a2be-19a6837f9194'::uuid, 'c419bfee-5d24-4748-bbbe-3a3d031c0065'::uuid),
        ('1fe3c28d-897e-40bc-b556-7dc0b216b8fb'::uuid, 'bd6a304b-89db-4be5-b88a-aada0a28136f'::uuid),
        ('2b19cee7-744c-42d1-ae17-eb0885d1b767'::uuid, 'd6d4bba5-f6da-4ca7-b3a2-c81a2bf6ba08'::uuid),
        ('2c9bb326-cb14-4c88-8110-c9f61c799433'::uuid, '4431f6e0-b285-4591-a838-89b4bd0bae5b'::uuid),
        ('2ce9ba8f-601f-48b2-bcd3-c23aaadbc693'::uuid, 'ba7a30ed-c316-4711-af5e-6d6a4c439624'::uuid),
        ('2d853e83-273b-4d18-96f9-26bab6f99f46'::uuid, 'd29e0aca-0104-42d4-9cf2-0b0b11dbcaa5'::uuid),
        ('30536ffa-4c4e-4368-a0d7-6407de50c87e'::uuid, 'd4a3a389-3b96-4da2-ae04-58b33b75b6a3'::uuid),
        ('38d6c5b8-b76c-479e-be14-a3fccd660ee6'::uuid, 'deba761f-d4a1-4800-b59a-26b765415456'::uuid),
        ('3edb4b36-be8e-468d-a172-925cf92cdc7c'::uuid, '041c5c6b-fad0-4b6d-8638-c973f7b8cc42'::uuid),
        ('3fb5580c-0962-465b-8bcd-f728bb50ce22'::uuid, 'cfca3d95-66ec-4371-b005-cdd04212d615'::uuid),
        ('426378d3-f156-4b9c-a35c-6a81eff81f30'::uuid, '61ded7c2-c9f3-4828-ba0e-92569192b4ec'::uuid),
        ('44524479-b6a9-43fa-889a-db666fc49633'::uuid, '7341c19a-940d-4be3-9df0-a7e1ed2fe4cb'::uuid),
        ('4541c33f-05db-4c83-ac2a-ff914f3e1513'::uuid, 'f50a5071-e512-4cfd-bd18-59bd005aff49'::uuid),
        ('48e5292c-da6c-406c-9b06-eee591da00ec'::uuid, '87fc2ee1-ce6c-402c-a803-fd1a568de2ba'::uuid),
        ('4af14cd8-da08-412e-9f01-702ee2c149b6'::uuid, 'e34da2df-0f8a-4677-965b-bdd50c2ea64b'::uuid),
        ('4d50c207-eacb-4752-ac35-6eef41f8f433'::uuid, '602a685d-ea0a-4d4c-a494-d7aae04bf331'::uuid),
        ('4e22555e-8131-4e75-adab-855f29132b18'::uuid, 'dbf792e2-b4f9-43cc-9946-ee6910c911b8'::uuid),
        ('5200223d-7b2a-4d50-a69e-ca0539267b54'::uuid, 'c9805205-ea40-4ee4-94cc-63419272f1dc'::uuid),
        ('525f321c-439f-4f8a-bd07-5965673dae04'::uuid, '1289670e-5b50-4cae-a1fc-ff46933dd6ff'::uuid),
        ('5371969a-1eee-4662-a4fe-0a976c6976c9'::uuid, '8ae15878-f27a-451e-9699-6546468b80ab'::uuid),
        ('5636ce6d-0aed-439a-92f5-41b8fc448489'::uuid, 'e89bbb45-2c00-4518-b0b1-2c920192e572'::uuid),
        ('58e64c08-ee68-49e7-9f00-8931304330b3'::uuid, '9fcf4b67-412f-4b6c-ac9e-2ba1db4ad48c'::uuid),
        ('59ce22fe-ba4c-4300-af53-337724436b6b'::uuid, 'f2b9ca0d-b0c2-48c7-9a34-96239a01cd2f'::uuid),
        ('5c48c801-3777-49f4-9de9-15b48f757cb2'::uuid, '3f8243da-809b-45f9-ab9d-f5c35086e67b'::uuid),
        ('5ca3eb51-743d-4cb0-9873-b01b6148c8a9'::uuid, '3c01787f-1969-4696-a2c6-4de4e7c84336'::uuid),
        ('5e2a5d6c-0048-4437-9e9f-028fbeaa0057'::uuid, 'e5b427b7-3221-4e4c-812c-8e56e63b6943'::uuid),
        ('61712272-a65b-4e22-863c-648816f41d61'::uuid, 'eb649193-74b3-4519-bbdf-4ab3f90d48cb'::uuid),
        ('6278f053-db2f-40c4-abf0-4cbb4fe0ae7e'::uuid, '88d2bbd3-5df2-40b6-b2b8-2f7bb6abe9e5'::uuid),
        ('6508556e-4594-42d6-9b13-e570d1d374a8'::uuid, 'd4440da8-cfc3-45d1-8855-e7f9e5406394'::uuid),
        ('6530b660-b1e1-45b5-b3b3-87950d8a420e'::uuid, '2bad76be-e8a9-4a62-891d-105ea069d3d5'::uuid),
        ('664278c3-7727-481e-9268-62ee95621240'::uuid, 'fe31752f-6e80-47b4-8486-609d88c76dfa'::uuid),
        ('6a6456c6-00c0-4c92-9f4e-9092f0cbc5aa'::uuid, '4b7934ed-a76f-4d3e-b7ae-3ff84ab35933'::uuid),
        ('6d242b0d-2a7a-49c9-a3aa-5aa10bc55e4e'::uuid, '74417455-9044-4408-8f1b-9f7e21ac89c9'::uuid),
        ('70c0b7e9-85b4-441b-bf14-210b1eed4e29'::uuid, '3c96f289-5cd8-4ade-b717-7f78a1d40afa'::uuid),
        ('7338e0d6-9e75-4427-b91e-3072c1b30985'::uuid, '8f01fecd-cc41-4cb4-98a0-dc878d0bfe5f'::uuid),
        ('7fb3046d-187f-4a18-8471-58ce68348465'::uuid, '9c7353b5-299f-4dc4-b8b2-df57a2e42779'::uuid),
        ('80848883-cc31-4cc0-a25c-7cd07b20f0a0'::uuid, '779012bf-8681-4c1c-b226-4ee3f293c221'::uuid),
        ('857c1acd-7d64-45ee-a120-bdfa09588269'::uuid, '7d6851da-252c-4870-aaa0-a8aa3f45ce0a'::uuid),
        ('89f4338b-0b7b-4db0-a80d-72447d6badfc'::uuid, 'fd087b83-33e7-454c-a82b-963c82347155'::uuid),
        ('8cf496f1-7b53-4623-84e3-ddaa76f0e682'::uuid, '560fb590-2671-4a12-8963-7c2199baeddb'::uuid),
        ('983b9ea0-5673-4500-b78e-ecac6a308101'::uuid, 'bacd162e-cdbf-4a3d-96a4-fe0af8368f5c'::uuid),
        ('9b842185-dfbf-430a-ac25-1d8fb1e11453'::uuid, '563a1a17-d674-42fe-8432-d40a526fec44'::uuid),
        ('9cf5af99-0d30-424d-8238-a6d3a115409b'::uuid, '2db80c2a-67d3-4210-867d-5086511aab70'::uuid),
        ('9feb3def-e1db-4827-8b50-9ad6627615cc'::uuid, '792649f4-e92c-4704-9445-540bc5a559a1'::uuid),
        ('a3e2b44f-a4c9-4302-9000-2b936e0d02c5'::uuid, 'd6d4bba5-f6da-4ca7-b3a2-c81a2bf6ba08'::uuid),
        ('a7f6c4c2-9dfb-49bb-a032-eff727454023'::uuid, '5abb207c-7405-4443-b344-1e9578fca4ec'::uuid),
        ('a84fd840-2737-417a-9073-b06bf7f63a05'::uuid, '3c96f289-5cd8-4ade-b717-7f78a1d40afa'::uuid),
        ('abff5a55-68bd-40e7-a355-05361339c949'::uuid, '236e78a4-ed5b-472f-afec-409ca2908540'::uuid),
        ('ad7246fe-a60d-45f3-85f7-dff671c6c396'::uuid, '262f34d9-bf09-4bc6-b62a-4bed30062289'::uuid),
        ('c37e280c-5fbf-4439-8237-561623d18804'::uuid, '421abc65-79c8-425a-8fe8-8581668b1d44'::uuid),
        ('c9e417cf-9e27-4394-80b3-6d5802006bf4'::uuid, '05243408-a2a9-4f88-8720-bd2fb4ac83a8'::uuid),
        ('cd1370d6-c6ff-4f7b-a3ff-d6a328933769'::uuid, '680f7cce-9f4a-41cc-a652-13042c6834f5'::uuid),
        ('daad971e-5e68-4bd0-9663-1f9ca85febed'::uuid, 'fe31752f-6e80-47b4-8486-609d88c76dfa'::uuid),
        ('dda839d2-0750-4fe1-9226-846523161489'::uuid, 'dbe826c9-d879-4c5f-94e7-82665311b1ec'::uuid),
        ('df2db344-0f0a-424c-bde0-f09a995ccc1f'::uuid, '69111f81-4db2-4d79-a3a5-ebf343dcac9c'::uuid),
        ('df43e28d-94df-479c-8c19-9da2f186132d'::uuid, '14813890-88d8-4300-884f-38e59d0564ca'::uuid),
        ('e1193ad4-81c7-4cb8-a9bb-0ddfc9ed5859'::uuid, 'e2e1985c-b151-4598-bc27-32612f172285'::uuid),
        ('e28c6edf-3ba8-4b41-8b65-3e13572f8458'::uuid, 'e0ee1d93-2237-447a-a831-f8998d908966'::uuid),
        ('e4ec7bd0-8ac8-4967-8c33-392e1d5da614'::uuid, 'e5b427b7-3221-4e4c-812c-8e56e63b6943'::uuid),
        ('ea984653-6f1a-45db-a6f3-048092117b9c'::uuid, '23050b8d-08c7-44e5-b359-f2734bde339d'::uuid),
        ('eb155c8e-4329-4ae0-928e-d7e865ff9932'::uuid, '6731a6cb-6c2e-4665-b093-3b9ab2bce9cf'::uuid),
        ('eeb8d809-2051-4e9d-9b32-dbabf172fcc7'::uuid, 'd03a52d9-00ba-4595-85d5-98ea128e7acd'::uuid),
        ('f1af8deb-1f34-423e-b578-0cb3544344f2'::uuid, 'd29e0aca-0104-42d4-9cf2-0b0b11dbcaa5'::uuid),
        ('f2b5cfc7-8d48-4c7a-95c9-77647f6137cc'::uuid, 'ae4c9339-9223-4cde-ad8b-660ede0e5fcf'::uuid),
        ('f5be8829-faef-4c7e-9c45-a0177eea8555'::uuid, 'ed8d4420-96ae-4572-ac82-93eaecccc4f3'::uuid),
        ('f6032bd6-9846-42ea-b08d-a46938a0f8ad'::uuid, 'e8b8196d-98e6-4222-a695-d43e39b41fe7'::uuid),
        ('f7953d23-aab4-43d2-824f-429a227e4939'::uuid, '63a02738-c8e1-4c74-839f-3a5d445e9cfc'::uuid),
        ('f7993e29-22cf-4346-9198-e820f7404759'::uuid, 'fb749dc1-cc0e-4346-93af-25d6f4959c50'::uuid),
        ('fc238889-0bab-41df-b1e8-85655f934761'::uuid, '2628c5b5-c582-4a86-a422-682c8016d598'::uuid),
        ('ff1e9653-56bb-4471-93a9-1d3fb013fa41'::uuid, '28e29737-f476-40c3-8b00-d9b75e291fcf'::uuid)
) AS m(dup, keeper)
WHERE n.id = m.dup
  AND n.valid_until IS NULL
  AND n.superseded_by IS NULL
  AND n.source_type = 'agent';
