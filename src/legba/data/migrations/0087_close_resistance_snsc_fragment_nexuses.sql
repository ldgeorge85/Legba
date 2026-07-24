-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
-- ==========================================================================
-- 0087 — close the bare "Resistance" / "SNSC" FRAGMENT-endpoint nexuses (E1).
-- ==========================================================================
-- WHY:
--   The M12 / reenrich telegram backfill (2026-07-09/10) fed raw markdown into
--   /extract, which minted the bare fragments "Resistance" (as a `person`) and
--   "SNSC" as distinct graph actors. The relationship_reifier then forged
--   agent nexuses off them — including `Resistance —hostile to→ United States`
--   and `SNSC —hostile to→ United States` — which LED the journal's j4 entry
--   and its "SNSC non-state actor going kinetic" speculation (the entity layer
--   producing analysis content, MASTER_PLAN §1).
--
--   Phase E stops NEW ones two ways, both live as of this train:
--     * "resistance" is now a _VAGUE_ENDPOINT_TOKENS junk term (_entity_canon)
--       — matched EXACT-on-stripped, so ONLY the bare "Resistance"/"the
--       Resistance" fragment is rejected, never "Axis of Resistance" /
--       "French Resistance"; and
--     * E1 canonicalize-at-write (resolve_keeper) folds "SNSC" onto its
--       "Supreme National Security Council" keeper before the write.
--   This migration retires the fragment edges ALREADY in the graph so a read
--   (and the next journal cycle) can't lead on them.
--
-- WHAT (close only — valid_until=now(); NO delete, NO superseded_by, because a
--   bare junk fragment has no canonical SURVIVOR to redirect onto):
--   every still-OPEN, agent-minted nexus whose subject OR object normalizes
--   (leading-article strip + lower) to EXACTLY 'resistance' or 'snsc'. At author
--   time (2026-07-10) that is these 10 rows — documented for audit; the query is
--   the source of truth, not this list:
--     00e71b1d  Abbas Araghchi           co occurs with  Resistance
--     fc2c7d21  Axis of Resistance        affiliated with Resistance
--     9d0c7481  Iran                      co occurs with  Resistance
--     6c3de8b2  Iran                      co occurs with  SNSC
--     d7899c6f  Mohammad Bagher Zolghadr  leader of       SNSC
--     2d962621  Resistance                co occurs with  the Islamic Revolution
--     e08e7edb  Resistance                co occurs with  the Resistance Front
--     b5d86c26  Resistance                hostile to      United States   (j4 lead)
--     7cd6d151  SNSC                      co occurs with  Supreme National Security Council
--     a4589462  SNSC                      hostile to      United States
--   DELIBERATELY LEFT OPEN (NOT bare fragments — full-surface coalition / person
--   terms that E4 reclassifies + E6 revisits, not junk to close blind here):
--     86a9b78c  Axis of Resistance   co occurs with  Iran
--     6d292966  Axis of Resistance   co occurs with  United States
--     066809f8  Islamic Jihad        allied with     the Axis of Resistance
--     370ccb3b  Khamenei             leader of       the Axis of Resistance
--
-- REVERSIBLE: set valid_until back to NULL to reopen every row. NO backup needed
--   (no hard-delete). IDEMPOTENT: the `valid_until IS NULL` guard makes a re-run
--   a no-op (and would also close any stray fragment edge minted before this
--   ran — desirable). Routed through a migration per the house rule (mass row
--   mutation goes through the migration runner's txn+ledger, never a raw UPDATE).
-- ==========================================================================

UPDATE nexuses n
SET valid_until = now(),
    updated_at  = now()
WHERE n.valid_until IS NULL
  AND n.superseded_by IS NULL
  AND n.source_type = 'agent'
  AND (
        lower(btrim(regexp_replace(n.subject, '^(the|a|an)\s+', '', 'i')))
          IN ('resistance', 'snsc')
     OR lower(btrim(regexp_replace(n.object,  '^(the|a|an)\s+', '', 'i')))
          IN ('resistance', 'snsc')
      );
