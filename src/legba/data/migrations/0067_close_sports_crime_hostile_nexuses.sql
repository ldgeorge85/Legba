-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0067_close_sports_crime_hostile_nexuses.sql  (DQ Phase 5 / facts-nexuses
--   finding "nexuses / hostility reification")
--
-- PROBLEM: implausible country-country 'hostile to' nexuses reified from World
--   Cup fixtures and a crime story survived the D14 sports gate (match coverage
--   without an explicit sports token did not match _SPORTS_CONTEXT_RE). All 11
--   identified bad rows remain OPEN, feeding the signed graph and escalation
--   composition with fake antagonism. Anchor: 7c5581a7 'Ukraine hostile to
--   Monaco' (a parcel bomb injuring a Ukrainian oligarch IN Monaco — nationality
--   adjective + venue conflated into state hostility).
--
-- PAIRED CODE FIX (keeps it out): relationship_reifier.py D14 gate —
--   _SPORTS_CONTEXT_RE extended (clash, knockout, fullback, derail, squad,
--   coach, stadium, N-N scorelines) so match coverage without an explicit token
--   is still gated to co-occurrence.
--
-- THIS MIGRATION closes (valid_until=now(); NO delete) exactly the 11 frozen
--   rows below, addressed BY PRIMARY KEY. Each is a sports fixture or a crime
--   story mis-typed as interstate hostility (verified live 2026-07-03). Real
--   conflict dyads (Russia/Ukraine, Israel/Iran, Hezbollah/Israel, ...) are NOT
--   in scope.
--
-- REVERSIBLE (valid_until back to NULL). IDEMPOTENT (valid_until IS NULL guard).
--   Routed through the migration runner (ONE transaction + ledger row; NO inline
--   BEGIN/COMMIT).
--
-- MEASURED (live `legba`, 2026-07-03): 11 rows matched.
--   7c5581a7 Ukraine->Monaco        d12c6828 Morocco->Brazil
--   fc83acc8 Norway->Ivory Coast    788eda6e Scotland->Morocco
--   04957ea6 South Africa->S.Korea  cfd72e1c Spain->Saudi Arabia
--   a9daec26 Thailand->Australia    e571420c Turkey->Paraguay
--   388cb557 Ecuador->Curacao       fe3c68ff DR Congo->England
--   7d57c33d Iran->Group G

UPDATE nexuses n
SET valid_until = now(),
    updated_at  = now()
WHERE n.valid_until IS NULL
  AND n.id = ANY (ARRAY[
      '7c5581a7-363a-40f4-8ccd-3e50bdf5c282',
      'd12c6828-6afb-4b3d-a7ae-09a2dd5e579c',
      'fc83acc8-1e18-4212-9b39-915569de6649',
      '788eda6e-48ce-423f-8c15-7391545b624e',
      '04957ea6-bec8-45fc-a147-50cbf10f1f57',
      'cfd72e1c-5647-4655-b962-00f9710c9367',
      'a9daec26-a16f-40bd-af7c-f730a880a00e',
      'e571420c-0323-43f1-8a2a-3779cb80d1f5',
      '388cb557-7ced-4552-a1e7-8974152b509d',
      'fe3c68ff-cd81-4dbb-9b28-d1000deadaa0',
      '7d57c33d-cd9a-4764-b292-a79aabf41ac9'
  ]::uuid[]);
