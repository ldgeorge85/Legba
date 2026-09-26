-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0204_event_lifecycle_events.sql — DATA MODEL V3 / P0, the append-only event
-- lifecycle ledger (spec §2.3). This is 0184_situation_events.sql's contract
-- applied to events, verbatim in shape: one row per transition, EVIDENCE time
-- in occurred_at, decision time in created_at, the two vocabulary CHECKs
-- mirroring the Python frozensets in src/legba/data/events/lifecycle.py, the
-- requires-evidence CHECK ('resolved' is the only evidence-free transition —
-- it asserts that nothing arrived; everything else claims a change and
-- therefore requires the evidence that made it), and DELETE/UPDATE barred by
-- trigger.
--
-- The lifecycle FSM itself:
--
--   none      -> emerging    promotion                                 opened
--   emerging  -> developing  signal_count >= 3                         advanced
--   developing-> active      signal_count >= 5 AND confidence >= 0.6   advanced
--   active    -> evolving    24h link rate >= 2x the 7d baseline,       accelerated
--                            or a new actor / new ISO2 attaches
--   evolving  -> active      rate back inside [0.5x, 2x] baseline       stabilised
--   emerging  -> resolved    no new link in 48 h                       resolved
--   developing-> resolved    no new link in 72 h                       resolved
--   active, evolving -> resolved   no new link in 7 days               resolved
--   resolved  -> developing  a new signal links                        reactivated
--
-- 'reactivated' is a TRANSITION, not a state — there is no sixth state. And
-- silence never reactivates and never closes twice: a resolved event that
-- stays quiet writes nothing.
--
-- ROLLBACK NOTE: event_lifecycle_events.event_id is ON DELETE CASCADE per spec,
-- and the forbid-mutation triggers mean a `DELETE FROM events` CASCADE fires
-- the trigger and fails LOUD. Rolling the tower backfill back out is therefore
-- `TRUNCATE event_lifecycle_events` (or DROP TABLE) BEFORE `DELETE FROM
-- events` — the ledger never loses rows to an inattentive script, which is the
-- designed posture.
--
-- SAFETY: CREATE IF NOT EXISTS / CREATE OR REPLACE FUNCTION / DROP TRIGGER IF
-- EXISTS + CREATE TRIGGER — re-apply and cold-start are both no-ops.

CREATE TABLE IF NOT EXISTS public.event_lifecycle_events (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    event_id     uuid NOT NULL REFERENCES public.events(id) ON DELETE CASCADE,
    occurred_at  timestamptz NOT NULL,          -- EVIDENCE time, not run time
    transition   text NOT NULL
                 CONSTRAINT event_lifecycle_events_transition_vocab
                 CHECK (transition IN
                        ('opened','advanced','accelerated',
                         'stabilised','resolved','reactivated')),
    state_from   text NOT NULL
                 CONSTRAINT event_lifecycle_events_state_from_vocab
                 CHECK (state_from IN
                        ('emerging','developing','active','evolving','resolved')),
    state_to     text NOT NULL
                 CONSTRAINT event_lifecycle_events_state_to_vocab
                 CHECK (state_to IN
                        ('emerging','developing','active','evolving','resolved')),
    -- One sentence saying WHY. An empty why is an unreadable ledger row, so it
    -- is unrepresentable.
    why          text NOT NULL
                 CONSTRAINT event_lifecycle_events_why_nonempty
                 CHECK (btrim(why) <> ''),
    derived_from uuid[] NOT NULL DEFAULT '{}',
    analyst_id   text,
    analyst_version text,
    run_id       uuid,
    created_at   timestamptz NOT NULL DEFAULT now(),   -- DECISION time
    -- 'resolved' is the SILENCE transition: it asserts that nothing arrived, so
    -- it is the only one that may be evidence-free. Everything else claims a
    -- change and therefore requires the evidence that made it. Structurally,
    -- not by convention — situation_events_delta_requires_evidence, retyped.
    CONSTRAINT event_lifecycle_requires_evidence
        CHECK (transition = 'resolved' OR derived_from <> '{}')
);

COMMENT ON TABLE public.event_lifecycle_events IS
    'DATA MODEL V3 / P0: append-only event lifecycle ledger — one row per '
    'transition, occurred_at is EVIDENCE time (the newest linked signal''s '
    'clock), created_at is decision time. The ledger is the record; '
    'events.lifecycle_state is a fast-SQL convenience projected from it. '
    '''resolved'' is the only evidence-free transition (it asserts silence); '
    'every other row must carry derived_from. DELETE and UPDATE fail loud at '
    'the trigger — a wrong row is superseded by the next row, never edited.';

-- "The lifecycle of event E, newest first."
CREATE INDEX IF NOT EXISTS idx_ele_event
    ON public.event_lifecycle_events (event_id, occurred_at DESC);

-- "How often do resolved events come back" is one query. Partial:
-- reactivations are the rarest rows in the ledger and the only class the
-- reliability question reads.
CREATE INDEX IF NOT EXISTS idx_ele_reactivations
    ON public.event_lifecycle_events (created_at DESC)
    WHERE transition = 'reactivated';

-- Schema-enforced append-only posture, verbatim from 0184. A history that can
-- be rewritten is not a history: a wrong row is superseded by the next row,
-- never edited or removed. Any DELETE/UPDATE — app bug, ad-hoc SQL, a future
-- code path — fails loud right here.
CREATE OR REPLACE FUNCTION public.event_lifecycle_events_forbid_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION
        'event_lifecycle_events rows are never updated or deleted — the '
        'ledger is append-only; correct a row by appending the next one';
END;
$$;

COMMENT ON FUNCTION public.event_lifecycle_events_forbid_mutation() IS
    'V3/P0: bar UPDATE/DELETE on event_lifecycle_events — the lifecycle ledger '
    'is append-only (0184_situation_events precedent).';

DROP TRIGGER IF EXISTS trg_event_lifecycle_events_forbid_delete
    ON public.event_lifecycle_events;
CREATE TRIGGER trg_event_lifecycle_events_forbid_delete
    BEFORE DELETE ON public.event_lifecycle_events
    FOR EACH ROW EXECUTE FUNCTION public.event_lifecycle_events_forbid_mutation();

DROP TRIGGER IF EXISTS trg_event_lifecycle_events_forbid_update
    ON public.event_lifecycle_events;
CREATE TRIGGER trg_event_lifecycle_events_forbid_update
    BEFORE UPDATE ON public.event_lifecycle_events
    FOR EACH ROW EXECUTE FUNCTION public.event_lifecycle_events_forbid_mutation();
