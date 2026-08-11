-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0120_soft_close_poisoned_journal_rows.sql
--
-- DATA REPAIR (2026-08-02 engine review, P5 §1). Two junk classes have been
-- accumulating in `journal_entries` and neither was ever remediated:
--
--   (a) TOOL-JSON ENVELOPES (3 rows). A gather-timeout left NARRATE emitting a
--       raw tool-call envelope instead of prose, and it was persisted verbatim
--       — title and body both literally `{"tool": "...", ...}`. The 07-31
--       02:07Z consolidation is the loud one (`get_source_health`, 3,882
--       chars); its siblings are a 07-15 `entry` (`graph_insights`) and a 07-08
--       consolidation (`list_nexuses`). The #236 shape guard now prevents
--       recurrence — it did not clean what was already written.
--
--   (b) EMPTY STUBS (4 rows). Bodies that are a placeholder and nothing else:
--       "(empty consolidation)" 07-18, "(empty lens read)" + "(empty chorus
--       diff)" + "(empty entry)" 07-27. Fallbacks now cover the lens tiers;
--       entry/consolidation still have none, so this class can recur.
--
-- Both classes are POISON, not merely ugly: the journal's own memory reads the
-- recent `entry` and `lens` rows back to itself each window, so a tool-call
-- envelope sitting in that corpus is fed to the narrator as if it were prose.
--
-- SOFT-CLOSE, NOT DELETE (house rule): `valid_until` ends the row's validity
-- while the row, its lineage and its receipts stay intact — reversible by
-- nulling `valid_until` on any subset later shown good. `data.closed_by`
-- makes the cohort queryable, and matching on the CONTENT MARKERS rather than
-- pinned uuids means this documents the defect rather than a row list.
--
-- COALESCE on valid_until: 3 of the 7 are already superseded by the normal
-- daily consolidation chain, and their original close time is real history —
-- do not overwrite it, only stamp the cohort. `NOT (data ? 'closed_by')`
-- keeps the migration idempotent.
--
-- Verified read-only before writing: matches exactly 7 of 116 journal rows,
-- 3 tool-JSON + 4 empty stubs, zero false positives.

UPDATE journal_entries
   SET valid_until = COALESCE(valid_until, now()),
       data = data || '{"closed_by": "mig_0120_poisoned_journal_rows"}'::jsonb,
       updated_at = now()
 WHERE NOT (data ? 'closed_by')
   AND (
        -- (a) body opens as a JSON object AND carries a tool-call key.
        (body ~ '^\s*\{' AND body LIKE '%"tool"%')
        -- (b) body is nothing but an "(empty ...)" placeholder.
        OR body ~ '^\(empty [a-z ]+\)\s*$'
   );
