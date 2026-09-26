-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0201_fact_contention_input_fingerprint.sql
--
-- One column, so the hourly arbiter can tell an unchanged dispute from a
-- changed one without re-deciding it.
--
-- THE DEFECT (measured live 2026-09-20, 48 h of runtime + sidecar logs)
-- --------------------------------------------------------------------
-- `fact_contention_arbiter` recomputes every contention group from the open
-- facts on every hourly pass. At 131 255 open facts / 22 208 groups / 103 107
-- value clusters one pass measured: 2.2 s of scan, 107 s of Python clustering
-- and scoring, 247 s of per-group earned-track-record queries, and ~339 000
-- write round trips at ~1.6 ms each. Across the 24 passes in the log window the
-- turn was held a median of 651 s (min 443, max 994).
--
-- Dapr actors are turn-based with reentrancy disabled. For those minutes the
-- AnalystActor's turn queue was held, so the reconciler's ENSURE_ACTIVE heal
-- blew its 20 s deadline every resync (`actor_turn.budget_exceeded
-- op=reconcile.activate`, 124 in 24 h), the heal breaker opened
-- (`action_executor.heal_suppressed`, 144), and all 24 cadence fires in that
-- window landed on *actor is closed, cannot handle remind/run_cadence*.
-- Activation itself measures ~17 ms — it was never slow, it was queued.
--
-- Nearly none of that work was needed. The arbiter opens ~15 groups an hour;
-- the other ~22 190 were re-decided into byte-identical rows plus a fresh
-- now().
--
-- WHAT THIS ADDS
-- --------------
-- `input_fingerprint` — a sha256 over exactly the inputs a group's stored
-- answer depends on: its member fact rows (id, value, confidence, source type,
-- source credibility, produced_at, lineage), the tunables that gate the
-- decision, whether the soak window has elapsed, and a coarse age bucket
-- (LEGBA_CONTENTION_REFRESH_HOURS, default 24 h) that forces one full
-- recompute per group per day so an absolute-floor crossing cannot go
-- unnoticed. The recipe, and why the age bucket is sound rather than a fudge,
-- are argued in fact_contention_pass.py.
--
-- A pass computes each group's fingerprint from the raw scanned rows BEFORE
-- clustering. A match skips the group whole — no clustering, no earned-weight
-- query, no writes. A miss runs the existing pipeline unchanged and stamps the
-- new fingerprint here.
--
-- NULLABLE, no default, no backfill. Every existing row reads as "unknown",
-- which fails the comparison and is recomputed on the next pass — so the
-- column fills itself in, bounded by the new per-pass wall-clock budget
-- (LEGBA_CONTENTION_PASS_BUDGET_SECONDS, default 120 s). Nothing else reads
-- the column, so an operator who sets LEGBA_CONTENTION_REFRESH_HOURS=0 gets the
-- pre-fix every-group-every-pass behavior back with the column simply ignored.
--
-- WHAT THIS DELIBERATELY DOES NOT DO
-- ----------------------------------
-- No index. The arbiter reads the whole table in one round trip at pass start
-- and matches in memory; an index on a column only ever compared for equality
-- against a value the reader already holds would be write cost for no read.

ALTER TABLE public.fact_contention
    ADD COLUMN IF NOT EXISTS input_fingerprint text;

COMMENT ON COLUMN public.fact_contention.input_fingerprint IS
    'sha256 of the inputs that produced this group''s stored answer (member '
    'rows + decision tunables + soak state + age bucket). A pass whose '
    'recomputed fingerprint matches skips the group untouched. NULL = unknown, '
    'recompute. See fact_contention_pass.group_fingerprint.';
