-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0184_situation_events.sql
--
-- CONTINUITY PHASE 2 (planning/SITUATION_CONTINUITY_PLAN_2026-07-31.md, D1) —
-- the SITUATION TRAJECTORY LEDGER. Phase 1 gave the compositions a MEMORY (the
-- prior read + the open-situation register, as citable refs). It did not make
-- "what changed since the prior read" a QUERYABLE property: a composition could
-- only reconstruct trajectory in prose, from two static snapshots, every cycle.
-- This table is that property.
--
-- One row = "on <occurred_at>, situation S <delta>d, because <why>, on the
-- strength of exactly <derived_from>". Append-only, forever.
--
-- ── WHY A SIDECAR AND NOT COLUMNS ON `situations` (D1) ─────────────────────
-- `situations` is a 20-minute MATERIALIZATION: `situation_clustering` re-derives
-- name / status / intensity_score / event_count / last_event_at / valid_until
-- from the current member set on EVERY tick and writes them all through an
-- ON CONFLICT DO UPDATE. It is a snapshot of now, by construction. A trajectory
-- written into those columns would be overwritten within 20 minutes by a
-- handler that knows nothing about it. History has to live somewhere the
-- snapshot writer does not reach, which is here.
--
-- That is also why the trajectory STATE is `state_from`/`state_to` on the ledger
-- row rather than new values on `situations.status`. `situations.status`
-- (active | dormant | closed) is a recency axis owned end-to-end by
-- situation_clustering — "has this frame seen a member lately". The trajectory
-- state (watching | escalating | de_escalating | dormant | closed) is a
-- DIRECTION axis owned end-to-end by `situation_tracker`. Two writers on one
-- column, one of them stomping every 20 minutes, would make both axes a lie.
-- The current trajectory state is the newest ledger row's `state_to` — derived
-- from the log, so it can never drift from the log.
--
-- ── THE DELTA-REQUIRES-EVIDENCE RULE, IN THE SCHEMA ────────────────────────
-- The binding lesson from the world_context RAG rollback is that an UNCITED
-- prior is this platform's named failure mode, and the echo/anchoring lesson is
-- that a model asked "what changed?" will always find something. So a delta
-- CLAIM without new evidence is not representable:
--
--     CHECK (delta = 'unchanged_checkpoint' OR derived_from <> '{}')
--
-- `unchanged_checkpoint` is the one delta that asserts nothing and therefore
-- needs nothing — it records that the tracker LOOKED and found no movement (and
-- carries the dormancy transition). Every other delta must name the NEW evidence
-- that moved it. This is a CHECK, not a convention, because the writer that
-- would violate it is the LLM leg.
--
-- ── APPEND-ONLY, SCHEMA-ENFORCED (the review_flags/0107 discipline) ────────
-- A trajectory that can be rewritten is not a trajectory. Both mutation paths
-- fail loud at the database:
--   * DELETE — there is no correction path; a wrong row is superseded by the
--     next row, which is what an append-only log means.
--   * UPDATE — `occurred_at`, `delta` and `derived_from` are the claim itself.
-- 0107 enforces only DELETE on `review_flags` (which closes by supersession, so
-- it NEEDS its UPDATE path). This ledger closes by nothing, so both are barred.
--
-- ── `occurred_at` IS EVIDENCE TIME, NOT RUN TIME (D1, temporal collapse) ───
-- The second binding lesson. `created_at` is when the tracker wrote the row;
-- `occurred_at` is the `produced_at` of the newest evidence the delta rests on.
-- Every date a composition or the UI renders off this ledger comes from
-- `occurred_at`, so "escalated Tuesday" means the evidence is from Tuesday, not
-- that a cron ran Tuesday.
--
-- ── FKs ────────────────────────────────────────────────────────────────────
-- `situation_id` carries a REAL foreign key (the `hypotheses.situation_id`
-- precedent — situations are never deleted, and a ledger row pointing at a
-- situation that does not exist is not auditable). `source_output_id` and
-- `derived_from` deliberately carry NONE: they name `analyst_outputs` rows, and
-- the whole output-side lineage plane (0106 output_consumption, 0107
-- review_flags/bearing_edges) holds the same no-FK posture there.
--
-- SAFETY (idempotent, additive, forward-only): CREATE TABLE/INDEX IF NOT EXISTS,
-- CREATE OR REPLACE FUNCTION, the standard DROP TRIGGER IF EXISTS + CREATE
-- TRIGGER idiom, and one ON CONFLICT DO NOTHING vocabulary insert. No existing
-- table is touched. Re-apply and cold-start are both no-ops. The runner wraps
-- this file in its own transaction and records it in `legba_data_migrations`;
-- no inline BEGIN/COMMIT (same as 0107/0143).

CREATE TABLE IF NOT EXISTS public.situation_events (
    id               uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    situation_id     uuid        NOT NULL
                     REFERENCES public.situations(id),
    -- Evidence time (the newest cited item's produced_at), NEVER run time.
    occurred_at      timestamptz NOT NULL,
    delta            text        NOT NULL
                     CONSTRAINT situation_events_delta_vocab
                     CHECK (delta IN (
                         'escalates', 'de_escalates', 'broadens',
                         'unchanged_checkpoint'
                     )),
    -- The trajectory transition this row records. state_from = the state the
    -- ledger already showed; state_to = the state after this delta. A row where
    -- they are equal is a real, meaningful row (a delta that did not turn the
    -- direction), so no CHECK forbids it.
    state_from       text        NOT NULL
                     CONSTRAINT situation_events_state_from_vocab
                     CHECK (state_from IN (
                         'watching', 'escalating', 'de_escalating',
                         'dormant', 'closed'
                     )),
    state_to         text        NOT NULL
                     CONSTRAINT situation_events_state_to_vocab
                     CHECK (state_to IN (
                         'watching', 'escalating', 'de_escalating',
                         'dormant', 'closed'
                     )),
    -- One sentence, from the tracker's LLM leg, saying WHY. An empty why is an
    -- unreadable ledger row, so it is unrepresentable.
    why              text        NOT NULL
                     CONSTRAINT situation_events_why_nonempty
                     CHECK (btrim(why) <> ''),
    -- The NEW evidence (analyst_outputs ids) this delta rests on.
    derived_from     uuid[]      NOT NULL DEFAULT '{}'::uuid[],
    -- The `situation_update` finding whose verified prose carries this delta's
    -- claim. NOT NULL: a ledger row with no gradeable source is an ungradeable
    -- assertion, which is the thing this whole plane exists to refuse.
    source_output_id uuid        NOT NULL,
    created_at       timestamptz NOT NULL DEFAULT now(),
    -- THE delta-requires-evidence rule (D1). See the header.
    CONSTRAINT situation_events_delta_requires_evidence
        CHECK (delta = 'unchanged_checkpoint'
               OR derived_from <> '{}'::uuid[]),
    -- Writer idempotency: one ledger row per (situation, source finding). A
    -- re-run that re-emits the same situation_update lands nothing new.
    CONSTRAINT uq_situation_events_situation_source
        UNIQUE (situation_id, source_output_id)
);

-- "The trajectory of situation S, newest first" — the composition register
-- upgrade (D5), the /v3 trajectory route, and the tracker's own state read.
CREATE INDEX IF NOT EXISTS idx_situation_events_situation
    ON public.situation_events (situation_id, occurred_at DESC, created_at DESC);

-- "Which situations escalated recently?" — the `situation_escalation` alert
-- trigger class (D5). Partial: escalations are the minority of the ledger and
-- the only class the alert plane scans.
CREATE INDEX IF NOT EXISTS idx_situation_events_escalations
    ON public.situation_events (created_at DESC)
    WHERE delta = 'escalates';

-- Schema-enforced append-only posture. A trajectory that can be rewritten is
-- not a trajectory: a wrong row is superseded by the next row, never edited or
-- removed. Any DELETE/UPDATE — app bug, ad-hoc SQL, a future code path — fails
-- loud right here (the 0107 review_flags precedent, widened to UPDATE because
-- this ledger has no close/supersede column to legitimately move).
CREATE OR REPLACE FUNCTION public.situation_events_forbid_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION
        'situation_events rows are never updated or deleted — the ledger is '
        'append-only; correct a row by appending the next one';
END;
$$;

DROP TRIGGER IF EXISTS trg_situation_events_forbid_delete
    ON public.situation_events;
CREATE TRIGGER trg_situation_events_forbid_delete
    BEFORE DELETE ON public.situation_events
    FOR EACH ROW EXECUTE FUNCTION public.situation_events_forbid_mutation();

DROP TRIGGER IF EXISTS trg_situation_events_forbid_update
    ON public.situation_events;
CREATE TRIGGER trg_situation_events_forbid_update
    BEFORE UPDATE ON public.situation_events
    FOR EACH ROW EXECUTE FUNCTION public.situation_events_forbid_mutation();

-- `situation_tracker` is an EXTENSION analyst kind (not a member of the closed
-- AnalystKind enum). The RUNTIME process registers it in-code
-- (legba.data.analysts.__init__), but the descriptor REGISTRY seeds its
-- kind-name validator from `vocabulary_entries` and REPLACES the extension set
-- on every refresh — so without this row the registry rejects the descriptor
-- PUT with "unknown analyst kind". journal_assessor / entity_researcher /
-- signal_salience each got this row by hand at deploy time; declaring it here
-- means the schema and the code land together instead of leaving a step an
-- operator can forget.
INSERT INTO public.vocabulary_entries (family, value, notes)
VALUES (
    'analyst_kind',
    'situation_tracker',
    'Continuity Phase 2 — the situation trajectory ledger writer (0184).'
)
ON CONFLICT (family, value) DO NOTHING;
