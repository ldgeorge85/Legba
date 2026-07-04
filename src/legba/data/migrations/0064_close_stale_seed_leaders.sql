-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0064_close_stale_seed_leaders.sql  (DQ Phase 5 / facts-nexuses finding
--   "facts / seed tier staleness + supersession")
--
-- PROBLEM: seed batch 94c4499f (produced 2026-06-19) keyed 'leader of'
--   supersession on the PERSON (subject), not the OFFICE (country), so when a
--   later batch seeded the current head the stale prior holder stayed OPEN
--   alongside the correct successor — direct both-open contradictions in the
--   highest-trust tier that feed the leadership_transition units. Six offices
--   have a stale 94c4499f 'leader of <country>' row coexisting with a
--   correct, already-open successor row from a newer batch.
--
-- PAIRED CODE FIX (the durable stop): the seed refresh (wikidata_leaders)
--   supersession key for a functional role must be (office/role, country), not
--   (subject, predicate), so a re-seed closes the prior office-holder. Seam
--   documented in the DQ ledger; this migration cleans the six already-written
--   contradictions.
--
-- THIS MIGRATION closes (valid_until=now(), superseded_by=<successor> where the
--   successor is unambiguous) exactly the six stale rows below, addressed BY
--   PRIMARY KEY. Each row's replacement is a genuinely-different current head
--   that is already OPEN (verified live 2026-07-03). CONSERVATIVE by
--   construction: it touches ONLY these six frozen ids and only while they are
--   still open — it does NOT close accent-variant duplicates (Brazil/Turkey,
--   left to the entity-canon path) or role-split ambiguities (Iran president vs
--   supreme leader, Saudi king vs crown prince), which are NOT clean
--   successions.
--
-- REVERSIBLE: a close is `valid_until=now()` + `superseded_by`; restoring
--   `valid_until=NULL, superseded_by=NULL` on these ids re-opens them. No row is
--   deleted.
--
-- IDEMPOTENT: the `valid_until IS NULL` guard makes a re-run a no-op (after the
--   first pass the six rows are closed). On a fresh substrate without batch
--   94c4499f the temp table joins nothing -> clean no-op. Routed through the
--   migration runner (wraps in ONE transaction + ledger row; NO inline
--   BEGIN/COMMIT, matching 0062/0063).
--
-- MEASURED (live `legba`, 2026-07-03): 6 rows matched.
--   86ac89c3 Joe Biden -> United States            (successor Donald Trump)
--   71b998e8 Justin Trudeau -> Canada              (successor Mark Carney)
--   ea0539d8 Olaf Scholz -> Germany                (2 open successors -> no PK)
--   b5b77863 Fumio Kishida -> Japan                (2 open successors -> no PK)
--   5fdfa51a Yoon Suk Yeol -> South Korea          (successor Lee Jae Myung)
--   d5c1fb93 Andres Manuel Lopez Obrador -> Mexico (successor Claudia Sheinbaum)

UPDATE facts f
SET valid_until   = now(),
    superseded_by = m.successor_id,
    updated_at    = now()
FROM (
    VALUES
      ('86ac89c3-7255-4dfd-b2fd-cc3c06a34c23'::uuid, '1918fa04-845b-43af-a9b3-73b69a838aa9'::uuid),
      ('71b998e8-9439-4e95-a5cc-37d01b19b900'::uuid, '3ba0a791-90f3-441b-9e3d-7acac90085a3'::uuid),
      ('ea0539d8-d6e7-4100-93ef-d8c6e19278f7'::uuid, NULL::uuid),
      ('b5b77863-ddf8-47d5-b61f-7bc64f1159cb'::uuid, NULL::uuid),
      ('5fdfa51a-0630-40cb-92bb-2b2b03677ae3'::uuid, '4dea6cef-d75c-4650-9bfd-61bd10d5cbee'::uuid),
      ('d5c1fb93-6729-4be5-b9a1-50acceba752e'::uuid, 'e121c69c-bd2c-4eaf-a602-1f586d30a911'::uuid)
) AS m(loser_id, successor_id)
WHERE f.id = m.loser_id
  AND f.valid_until IS NULL;
