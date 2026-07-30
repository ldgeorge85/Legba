-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0106_output_consumption.sql
--
-- KW-1 (forward-consumption index). Materialized FORWARD lineage edges,
-- written AT CONSUMPTION TIME: "composed/derived output C read finding/head F
-- in role <context>". The existing ``derived_from[]`` column answers the
-- BACKWARD question (what did C read?) only by scanning every consumer; this
-- table is the inverted index that answers the FORWARD question (WHO consumed
-- F?) in one indexed probe — the substrate for the review-flag plane
-- (migration 0107) and a later behavior-identical fast path under the F-1
-- freshness-advisory BFS.
--
-- One row per (consumer, consumed, context):
--   * consumer_id   — the composed/derived output row (the composition head /
--                     journal entry). NO foreign key: consumers live across
--                     tables (analyst_outputs, journal_entries) and the
--                     codebase's standing posture is lineage-by-uuid with no
--                     cross-table FKs (``derived_from[]`` has none either).
--   * consumed_id   — the sub-finding/head/signal row the consumer read.
--                     Same no-FK posture.
--   * consumed_at   — when the consumption happened (the compose/run time,
--                     stamped by the writer in the same flow as the output
--                     write).
--   * consumer_kind — the analyst KIND of the consumer (e.g.
--                     'meta_findings_synthesizer', 'journal_assessor') so a
--                     forward walk can rank/filter consumers without joining
--                     back to the consumer row.
--   * context       — the ROLE the consumed row played, open vocabulary:
--                       'composition_basis'     — a load-bearing (verified,
--                                                 above-floor) input head
--                       'composition_periphery' — a below-floor/unverified
--                                                 periphery row (C-TIER
--                                                 two-tier split)
--                       'journal_slice'         — a row of the journal's
--                                                 rendered priming slice
--
-- The (consumer_id, consumed_id, context) PRIMARY KEY doubles as the
-- uniqueness guarantee (a re-run writes a NEW consumer row id, so replays
-- never collide; ON CONFLICT DO NOTHING makes the writer idempotent within a
-- run) and as the BACKWARD-walk index (consumer_id first).
--
-- SAFETY (idempotent, additive, forward-only): CREATE TABLE/INDEX IF NOT
-- EXISTS only; no existing table is touched; re-apply and cold-start are both
-- no-ops. The runner wraps this file in its own transaction and records it in
-- `legba_data_migrations` (no inline BEGIN/COMMIT — same as 0091/0105).

CREATE TABLE IF NOT EXISTS public.output_consumption (
    consumer_id   uuid        NOT NULL,
    consumed_id   uuid        NOT NULL,
    consumed_at   timestamptz NOT NULL DEFAULT now(),
    consumer_kind text        NOT NULL,
    context       text        NOT NULL,
    PRIMARY KEY (consumer_id, consumed_id, context)
);

-- The FORWARD walk ("who consumed F, and when?") — consumed_id FIRST, with
-- consumed_at so "was F consumed before it moved at T" resolves in the index.
CREATE INDEX IF NOT EXISTS idx_output_consumption_forward
    ON public.output_consumption (consumed_id, consumed_at DESC);
