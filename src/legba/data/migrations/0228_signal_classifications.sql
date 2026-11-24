-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0228_signal_classifications.sql
--
-- THE ROW-LEVEL `data` BAG ON SIGNALS, FOR `data.classifications` (W7 TAG-1).
--
-- WHY
-- ---
-- A tag is a classification only when it names a registered vocabulary family
-- and a value of it: `<family>:<value>` (`legba.data.tags`). The substrate rows
-- carry the binding as `data.classifications` — `[{family, value, status}]`,
-- status in {bound, unclassified, legacy_src}, plus `raw` when unclassified.
-- Facts, situations, events and entity profiles already carry a row-level
-- `data jsonb`, and their producers now stamp it at write time — no column is
-- needed there. `signals` has none (its `payload` is the source's own record,
-- not ours to annotate), so the reclassification of the raw `signals.tags`
-- values (`scripts/reclassify_tags.py --apply`) needs a place to write.
--
-- WHAT THIS ADDS
-- --------------
--   * `signals.data jsonb` — nullable, NO default. NULL = never classified
--     (every row today; a row with no tags stays NULL after the apply too).
--     `signals.tags` itself is NOT rewritten: the raw strings stay as the
--     provenance they are, and `has_tag()` compares a signal tag under its
--     namespaced key (`src:<tag>` for a bare one) instead.
--
-- SAFETY
-- ------
-- Additive and idempotent (`ADD COLUMN IF NOT EXISTS`), routed through the
-- runner (no inline BEGIN/COMMIT). A nullable column with no default is a
-- catalog-only change on Postgres 11+: no table rewrite, no scan, so the
-- ACCESS EXCLUSIVE lock is held for milliseconds on a ~300k-row table. No
-- index: no reader filters on it yet (the slice side — TAX-1 — brings its own
-- index with a live EXPLAIN). No backfill here: the apply is an operator act,
-- in batches of 1,000 with a resume cursor.

ALTER TABLE public.signals
    ADD COLUMN IF NOT EXISTS data jsonb;

COMMENT ON COLUMN public.signals.data IS
    'Row-level annotation bag (migration 0228). data.classifications = the '
    'namespaced binding of signals.tags: [{family, value, status, raw?}], '
    'status in (bound, unclassified, legacy_src). NULL = never classified.';
