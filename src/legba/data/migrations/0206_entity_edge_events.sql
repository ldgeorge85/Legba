-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0206_entity_edge_events.sql
--
-- DATA MODEL V3 / P3 (spec §3.5) — the ENTITY-EDGE TRANSITION LEDGER.
-- One row = "on <occurred_at>, edge E <transition>d, because <why>, on the
-- strength of exactly <derived_from>". Append-only, forever.
--
-- ── WHAT THE LEDGER RECORDS — AND WHAT IT DOES NOT ─────────────────────────
-- TRANSITIONS only, never observations (spec §3.5, verified against live
-- 2026-09-21): a row per re-observation would be ~2,900/week of
-- low-information noise already summarised by entity_edges.observed_count /
-- last_seen_at. A row per transition is ~420/day — the shape changes that
-- make an edge's CURRENT state legible: minted ('observed'), polarity
-- flipped, retyped, closed, folded away by a merge, reopened.
--
-- ── WHAT IS ALREADY LOST ───────────────────────────────────────────────────
-- The dual-write has folded same-triple re-observations in place since 0143 —
-- confidence lifted, evidence unioned, observed_count incremented, the
-- re-observation itself discarded. The ~8,872 folds that already happened
-- (spec §3.5's verified count, 2026-09-21) are NOT recoverable: the evidence
-- they carried was merged, and there is no journal of which write carried
-- which. This ledger starts the history; it does not pretend to reconstruct
-- the past.
--
-- ── WHICH TABLES GET THIS PROPERTY ─────────────────────────────────────────
-- entity_edges ONLY. `facts`, `nexuses` and `situations` do NOT receive a
-- transition ledger here (spec §3.5): facts/nexuses supersede into full
-- closed rows that ARE their own history, and situations already have the
-- situation_events trajectory ledger (0184). entity_edges is the gap: its
-- folds destroy history in place.
--
-- ── THE GATE ───────────────────────────────────────────────────────────────
-- Writes are gated behind LEGBA_EDGE_TRANSITION_LEDGER (default OFF) in
-- provenance/entity_edge_writes.py, inside the SAME transaction as the edge
-- upsert — a ledger row and the transition it records commit or roll back
-- together. Flag off: the write path is byte-identical and the table stays
-- empty, which is the deployable-with-flags-off contract.
--
-- ── APPEND-ONLY, SCHEMA-ENFORCED (the 0184 situation_events contract,
-- verbatim) ────────────────────────────────────────────────────────────────
-- A transition ledger that can be rewritten is not a ledger. Both mutation
-- paths fail loud at the database: no DELETE (a wrong row is superseded by
-- the next row, which is what append-only means) and no UPDATE
-- (occurred_at / transition / derived_from ARE the claim).
--
-- ── occurred_at IS EVIDENCE TIME ───────────────────────────────────────────
-- Same rule as 0184: created_at is when the writer committed the row;
-- occurred_at is the produced_at of the assertion that caused the
-- transition. "Flipped Tuesday" means the evidence is from Tuesday, not
-- that a cron ran Tuesday.
--
-- SAFETY (idempotent, additive, forward-only): CREATE TABLE/INDEX IF NOT
-- EXISTS, CREATE OR REPLACE FUNCTION, the standard DROP TRIGGER IF EXISTS +
-- CREATE TRIGGER idiom. No existing table is touched. Re-apply and
-- cold-start are both no-ops. The runner wraps this file in its own
-- transaction and records it in `legba_data_migrations`; no inline
-- BEGIN/COMMIT (same as 0107/0143/0184).

CREATE TABLE IF NOT EXISTS public.entity_edge_events (
    id            uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    edge_id       uuid        NOT NULL
                  REFERENCES public.entity_edges(id) ON DELETE CASCADE,
    occurred_at   timestamptz NOT NULL,            -- VALIDITY time (evidence time)
    transition    text        NOT NULL
        CONSTRAINT entity_edge_events_transition_vocab
        CHECK (transition IN ('observed','polarity_flip','retyped',
                              'closed','folded','reopened')),
    polarity_from smallint,
    polarity_to   smallint,
    edge_type_from text,
    edge_type_to   text,
    why           text        NOT NULL
        CONSTRAINT entity_edge_events_why_nonempty CHECK (btrim(why) <> ''),
    -- The evidence (edge ids, signal ids) the transition rests on.
    derived_from  uuid[]      NOT NULL DEFAULT '{}'::uuid[],
    analyst_id    text,
    analyst_version text,
    run_id        uuid,
    created_at    timestamptz NOT NULL DEFAULT now(),  -- DECISION time
    -- A first observation may legitimately cite nothing; every other
    -- transition must name the evidence that moved it (the 0184
    -- delta-requires-evidence rule, same shape).
    CONSTRAINT entity_edge_events_evidence
        CHECK (transition = 'observed' OR derived_from <> '{}'::uuid[])
);

-- "The history of edge E, newest first" — the trajectory read.
CREATE INDEX IF NOT EXISTS idx_entity_edge_events_edge
    ON public.entity_edge_events (edge_id, occurred_at DESC);

-- "What transitioned recently?" — the temporal scan.
CREATE INDEX IF NOT EXISTS idx_entity_edge_events_when
    ON public.entity_edge_events (occurred_at DESC);

-- Append-only, schema-enforced — the situation_events (0184) contract
-- verbatim: any DELETE/UPDATE fails loud right here.
CREATE OR REPLACE FUNCTION public.entity_edge_events_forbid_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION
        'entity_edge_events rows are never updated or deleted — the ledger is '
        'append-only; correct a row by appending the next one';
END;
$$;

DROP TRIGGER IF EXISTS trg_entity_edge_events_forbid_delete
    ON public.entity_edge_events;
CREATE TRIGGER trg_entity_edge_events_forbid_delete
    BEFORE DELETE ON public.entity_edge_events
    FOR EACH ROW EXECUTE FUNCTION public.entity_edge_events_forbid_mutation();

DROP TRIGGER IF EXISTS trg_entity_edge_events_forbid_update
    ON public.entity_edge_events;
CREATE TRIGGER trg_entity_edge_events_forbid_update
    BEFORE UPDATE ON public.entity_edge_events
    FOR EACH ROW EXECUTE FUNCTION public.entity_edge_events_forbid_mutation();
