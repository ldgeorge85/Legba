-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0208_invocation_duration.sql — DATA MODEL V3 / P4a: one additive nullable
-- column on the pack-invocation ledger so pre-registered trigger gauge E2
-- (JUDGE_SYNTHESIS §4.2 — "p95 latency of the shipped graph tools over >=100
-- real invocations in a rolling 30 days") is READABLE at the 2026-11-03
-- sitting. Today it is not: the ledger carries cost_usd / units / outcome and
-- no duration column, so the latency leg of the engine trigger cannot be
-- computed at all.
--
-- The stamp lands in agency.run_pack_tool's settle step (governor.settle
-- gains a duration_ms parameter): every invocation row that is settled —
-- completed, handler-crashed, or timed-out — carries its wall-clock
-- milliseconds measured across the resolve → govern → dispatch pipeline.
-- Blocked calls never land an invocation row (they are governor_events
-- only), so there is no blocked-row duration to stamp.
--
-- Nullable, no default, no backfill BY DESIGN: a synthetic duration for a
-- pre-column row would fabricate evidence. Rows written before this column
-- read NULL and are excluded from the p95 — the gauge reports the n it
-- actually measured, which is the honest answer.
--
-- Numbering note (spec §7.1 "On the out-of-order pair"): 0208 is reserved for
-- P4a and lands before 0206/0207 in wall-clock time; the two files are
-- independent and the runner keys on filename, so this is safe.
--
-- SAFETY: ADD COLUMN IF NOT EXISTS — re-apply and cold-start are both no-ops.

ALTER TABLE public.action_pack_invocations
    ADD COLUMN IF NOT EXISTS duration_ms integer;

COMMENT ON COLUMN public.action_pack_invocations.duration_ms IS
    'V3/P4a: wall-clock milliseconds of the invocation (resolve → govern → '
    'dispatch → settle), stamped by agency.run_pack_tool''s settle step. NULL '
    'on rows written before migration 0208 and on rows still ''admitted'' '
    '(unsettled). Feeds graph-engine trigger gauge E2 (p95 over >=100 '
    'invocations / 30d); a NULL is never read as 0.';
