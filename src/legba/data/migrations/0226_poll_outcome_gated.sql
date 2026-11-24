-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0226_poll_outcome_gated.sql
--
-- A POLL WHOSE EVERY ENTRY THE KEYWORD GATE DROPPED IS PRODUCTIVE, NOT EMPTY.
--
-- WHY
-- ---
-- SRC-3 (the `keyword_gate` ingestion stage / the RSS keyword_* sugar) drops
-- off-topic items in `SourceCore._process_one`, before enrichment. A poll
-- where the feed delivered N entries and the gate dropped all N wrote 0
-- signals, so the poll-outcome writer recorded `outcome='empty'` -- the same
-- word as "HTTP 200, the feed had nothing". Every reader that walks a run of
-- 'empty' rows (the liveness watchdog's empty-streak / cursor-or-filter-fault
-- check, `get_source_health`'s silent tallies, the stall diagnosis) then read
-- a narrow include-list feed doing exactly its job as a stalled one: a
-- guaranteed false escalation on any source whose gate is selective.
--
-- WHAT THIS ADDS
-- --------------
--   * `keyword_filtered integer` (nullable) on `source_poll_outcomes` -- how
--     many entries the keyword gate dropped on THIS poll. NULL means the
--     source is not gated (or the row predates this column): "not gated /
--     unknown", never a 0 by default. 0 means "gated, and it dropped nothing".
--   * `'gated'` joins the `outcome` vocabulary (the CHECK widened, the 0114
--     idiom): a poll that wrote 0 signals, collapsed 0 duplicates, did not
--     error, and had `keyword_filtered > 0`. It says "the feed delivered, and
--     everything it delivered was off-topic" -- distinct from 'empty' ("nothing
--     arrived"). 'success' keeps meaning "wrote or re-served content";
--     'empty' keeps its meaning exactly.
--
-- SAFETY
-- ------
-- Additive and idempotent, routed through the runner (no inline BEGIN/COMMIT --
-- the runner owns the transaction). `ADD COLUMN IF NOT EXISTS`; the CHECK is
-- DROP IF EXISTS + ADD, so a second run is a no-op in effect. Every existing
-- row ('empty' / 'error' / 'success') stays valid; no backfill (the drop
-- counts of past polls were never kept, and inventing them would be the
-- fabrication the NULL exists to stop). The table is ~150k rows, so the
-- constraint re-validation is instant. No new index: the readers are keyed on
-- (source_id, occurred_at DESC), unchanged.

ALTER TABLE public.source_poll_outcomes
    ADD COLUMN IF NOT EXISTS keyword_filtered integer;

ALTER TABLE public.source_poll_outcomes
    DROP CONSTRAINT IF EXISTS source_poll_outcomes_outcome_chk;
ALTER TABLE public.source_poll_outcomes
    ADD CONSTRAINT source_poll_outcomes_outcome_chk
    CHECK (outcome IN ('empty', 'error', 'success', 'gated'));
