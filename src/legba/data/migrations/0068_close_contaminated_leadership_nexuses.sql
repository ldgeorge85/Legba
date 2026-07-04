-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0068_close_contaminated_leadership_nexuses.sql  (DQ Phase 5 / facts-nexuses
--   finding "nexuses / endpoint hygiene")
--
-- PROBLEM (two mechanical classes):
--   (A) Citation-marker residue in nexus ENDPOINT names — '[N]' / '【N】' /
--       '/*[N]*/' left in a subject or object surface ('Masoud Pezeshkian
--       /*[1]*/ leader of Iran /*[2]*/', 'Donald Trump[1 -> United States[2',
--       'Israel [1 -> Iran [1', 'Lee [2 -> South Korea [2', 'Macron[1] ->
--       French[1]'). No real entity name contains a citation bracket; a clean
--       duplicate already exists so the (s,o,rel) supersession never fired.
--   (B) Stale leadership re-assertion from retrospective coverage — three
--       surface forms of the FORMER Iranian supreme leader ('Ali Khamenei',
--       'Ayatollah Khamenei', 'Seyyed Ali Khamenei') all OPEN as 'leader of
--       Iran', contradicting the seed tier (the current head is Mojtaba
--       Khamenei, whose row is left OPEN). The three surface forms prevented
--       (s,o,rel) supersession from collapsing them.
--
-- PAIRED CODE FIX (keeps it out): the reifier write path strips citation
--   residue from entity surfaces before write, and a 'leader of' agent edge that
--   contradicts an open seed fact is deferred to the seed tier (temporal-
--   collapse guard). Seam documented in the DQ ledger.
--
-- THIS MIGRATION closes (valid_until=now(); NO delete):
--   (A) every OPEN nexus whose subject OR object carries citation-bracket
--       residue ('[' / ']' / '【' / '】' / '/*') — a MECHANICAL, self-adjusting
--       match (a legitimate endpoint never contains these);
--   (B) the three stale former-supreme-leader 'leader of Iran' rows, BY PRIMARY
--       KEY (Mojtaba Khamenei's current row is NOT in scope).
--
-- REVERSIBLE (valid_until back to NULL). IDEMPOTENT (valid_until IS NULL guard).
--   Routed through the migration runner (ONE transaction + ledger row; NO inline
--   BEGIN/COMMIT).
--
-- MEASURED (live `legba`, 2026-07-03): (A) 7 bracket-residue rows
--   (c7dd8ed0, 5c78638e, 47857cbf, f9b2b5e1, 5c821d37, 86f55212, 8be06211);
--   (B) 3 rows (f95868cc, 9614ce2a, 1efac71c).

-- (A) Mechanical: close any OPEN nexus with citation-bracket residue in an
--     endpoint surface.
UPDATE nexuses n
SET valid_until = now(),
    updated_at  = now()
WHERE n.valid_until IS NULL
  AND (
        n.subject ~ '(\[|\]|【|】|/\*)'
     OR n.object  ~ '(\[|\]|【|】|/\*)'
  );

-- (B) By-PK: close the three stale former-supreme-leader 'leader of Iran' rows.
UPDATE nexuses n
SET valid_until = now(),
    updated_at  = now()
WHERE n.valid_until IS NULL
  AND n.id = ANY (ARRAY[
      'f95868cc-62de-4a15-94ea-63a6a052dbf4',
      '9614ce2a-9c93-4a57-8c71-4e64000a997c',
      '1efac71c-14a9-4cb5-ba18-b309ac13184d'
  ]::uuid[]);
