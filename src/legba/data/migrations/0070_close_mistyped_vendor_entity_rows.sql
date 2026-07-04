-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0070_close_mistyped_vendor_entity_rows.sql  (DQ Phase 5 / facts-nexuses
--   finding "nexuses / vendor entity typing")
--
-- PROBLEM: a legitimate mainstream-news topic (US technology export policy)
--   entered the substrate, but the entity resolver mis-typed the corporate and
--   product-name surfaces (typing a company as person/location, a product name
--   as person) and derived a handful of nonsense facts plus one implausible
--   state-vs-company hostile-dyad edge (a regulatory action reified as
--   'hostile to' at high confidence). The underlying topic is real; the defect
--   is entity typing / junk-derived rows, not the topic.
--
-- PAIRED CODE FIX (keeps it out): the entity resolver's NER-class gate must
--   type a company as `organization` and a product/model surface as a
--   product/drop (never person/location), and hostile-dyad polarity must be
--   restricted to state/org political actors. Seam documented in the DQ ledger.
--
-- THIS MIGRATION closes (valid_until=now(); NO delete) the small set of junk
--   rows, addressed BY PRIMARY KEY ONLY (the referent surfaces are NOT written
--   into this file — the rows are identified purely by their UUIDs, verified
--   OPEN live 2026-07-03):
--     * 5 nonsense derived facts (inverted / cross-referent containment /
--       spurious corporate relations);
--     * 1 mis-typed state-vs-company hostile-dyad nexus (empty source lineage).
--   The profile-merge / re-type of the fragmented entity_profiles is DEFERRED
--   to the entity-canon path (out of this facts/nexuses close family) and
--   reported for manual handling.
--
-- REVERSIBLE (valid_until back to NULL). IDEMPOTENT (valid_until IS NULL guard).
--   Routed through the migration runner (ONE transaction + ledger row; NO inline
--   BEGIN/COMMIT).
--
-- MEASURED (live `legba`, 2026-07-03): 5 facts + 1 nexus matched.

-- 5 nonsense derived facts (by PK).
UPDATE facts f
SET valid_until = now(),
    updated_at  = now()
WHERE f.valid_until IS NULL
  AND f.id = ANY (ARRAY[
      '77334595-5c63-4916-b35c-55d49c1c70d7',
      '9b75becb-7097-4ad2-958b-ad779b7dc289',
      '0ef11e03-98dc-4098-bf60-33a9dcf01be3',
      'fd3602dc-65a3-4eab-aa42-e59d1e973ee4',
      '732b1a38-3293-4354-a00c-129656c8bd67'
  ]::uuid[]);

-- 1 mis-typed state-vs-company hostile-dyad nexus (by PK).
UPDATE nexuses n
SET valid_until = now(),
    updated_at  = now()
WHERE n.valid_until IS NULL
  AND n.id = '63cc6947-b067-43da-a502-7357b4d47ada'::uuid;
