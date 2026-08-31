-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0189_read_events.sql
--
-- THE ORACLE WAGER'S INSTRUMENT (planning/CAMPAIGN_2026-08-29/
-- PREMISE_REASON_TO_EXIST.md §5, Option 1 — "instrument reading (opens,
-- drills)"). The premise review's central finding is a measurement gap, not
-- an opinion: this schema carries ~80 tables that receipt every WRITE the
-- platform performs — analyst_traces, output_consumption, review_flags,
-- bearing_edges, situation_events, budget_ledger — and NOT ONE that records
-- a READ. "Zero finding-level drills in 15 days" is an inference off Caddy
-- access logs, which is the best evidence available and is not good enough to
-- settle a 90-day wager. This table makes the reading half of "a self-driving
-- organ the operator reads" a first-class, queryable fact.
--
-- One row = "at <occurred_at>, in workspace <workspace>, session
-- <session_nonce> performed <event_kind>, on <subject_kind>/<subject_id> if
-- the act had a subject, for <dwell_ms> if we could cheaply observe it".
--
-- ── WHY THIS IS THE DUMBEST TABLE IN THE SCHEMA, ON PURPOSE ────────────────
-- Every other ledger here earns its complexity by constraining an LLM writer.
-- This one's writer is a browser on the operator's own machine, emitting at
-- UI event rate, over a network that will sometimes fail. So the design goals
-- invert:
--
--   * NO FOREIGN KEYS. `subject_id` names findings (analyst_outputs), signals,
--     entities, situations, sources — five different tables — and telemetry
--     must survive the subject being retired, tombstoned (0175), merged
--     (0185 repoint) or garbage-collected. A read receipt is a record that the
--     operator LOOKED at something, and that stays true after the something is
--     gone. It is also why `subject_id` is `text`, not `uuid`: a panel open
--     names a kind (`system.wall`), a brief read names a slug, a citation
--     drill names a ref token. Coercing those into a uuid column would mean
--     dropping the ones that do not fit, which is the same as not measuring.
--
--   * NO JOINS AT WRITE TIME. The ingest endpoint does one multi-row INSERT
--     and returns. It never resolves a subject, never checks a kind against a
--     product table, never touches the descriptor registry. Telemetry that can
--     be slow enough to notice is telemetry the operator will ask us to turn
--     off, and a read plane that adds latency to reading is self-defeating.
--
--   * NO AUTH IDENTITY. Single-operator, single-tenant (the glass-tower ruling
--     is explicit: NOT community-managed). `session_nonce` is a random string
--     the client mints per browser session — enough to tell "one long morning"
--     from "eight separate visits", which is the only cardinality the wager's
--     question needs. It is deliberately NOT a user id, a device id, or
--     anything that survives a tab close.
--
-- ── APPEND-ONLY, SCHEMA-ENFORCED (the 0107/0184 discipline) ────────────────
-- Both mutation paths fail loud at the database. The reason is sharper here
-- than for the analytic ledgers: this table exists to grade the OPERATOR's
-- behaviour, and the operator is also the only person who can reach the
-- database. A measurement the measured party can quietly edit is not a
-- measurement. Day 90's verdict has to rest on a log that nobody — including
-- a well-meaning cleanup script — could have retouched. Retention is by
-- wholesale partition-free DELETE only through an explicit future migration,
-- not by ad-hoc statement.
--
-- ── `occurred_at` IS CLIENT TIME, AND THAT IS DELIBERATE ───────────────────
-- The 0184 lesson is "evidence time, never run time". The analogue here: the
-- evidence is the operator's ATTENTION, which happened in the browser, not
-- when a batched POST drained. Events batch and debounce (up to a few seconds)
-- and `sendBeacon` can land after a tab closes, so `received_at` — server
-- clock, defaulted — is kept alongside as the audit column. Every rollup and
-- every day-90 number reads `occurred_at`. A CHECK bounds client clock skew so
-- a wrong laptop clock cannot silently backdate a month of reading into a
-- single day: events more than 1 day in the future are refused outright.
--
-- ── THE VOCABULARY IS CLOSED, AND SMALL ────────────────────────────────────
-- Eight kinds, each mapping to a surface the wager actually names:
--   workspace_open  — a stance switch (the six-stance bar, train A)
--   brief_read      — the Morning Read landing mounted. THE headline metric:
--                     "did the operator open the morning product today?"
--   panel_open      — any dock panel mounted by an opener
--   finding_open    — a finding selected into the inspector
--   lineage_walk    — the provenance panel's lineage/why tabs entered
--   citation_drill  — a citation chip followed to its source
--   consult_open    — the operator-pull surface (decaying 64→53→18)
--   brief_read/…    — see above
-- A CHECK, not a lookup table: this vocabulary changes only when a surface
-- changes, which is a code change anyway, and an open text column would let a
-- typo in a UI emitter silently create a ninth kind that no rollup counts.
--
-- SAFETY (idempotent, additive, forward-only): CREATE TABLE/INDEX IF NOT
-- EXISTS, CREATE OR REPLACE FUNCTION, the standard DROP TRIGGER IF EXISTS +
-- CREATE TRIGGER idiom. No existing table is touched. Re-apply and cold-start
-- are both no-ops. The runner wraps this file in its own transaction and
-- records it in `legba_data_migrations`; no inline BEGIN/COMMIT.

CREATE TABLE IF NOT EXISTS public.read_events (
    id            uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    -- Attention time (client clock, when the operator did the thing).
    occurred_at   timestamptz NOT NULL,
    -- Server clock, when the batch landed. Audit only — never rolled up.
    received_at   timestamptz NOT NULL DEFAULT now(),
    event_kind    text        NOT NULL
                  CONSTRAINT read_events_kind_vocab
                  CHECK (event_kind IN (
                      'panel_open',
                      'workspace_open',
                      'finding_open',
                      'lineage_walk',
                      'citation_drill',
                      'consult_open',
                      'brief_read'
                  )),
    -- What was read. Nullable as a PAIR: a workspace switch has no subject,
    -- a finding open has one. Half a subject is a bug, so the CHECK below
    -- makes "kind without id" and "id without kind" both unrepresentable.
    subject_kind  text        NULL
                  CONSTRAINT read_events_subject_kind_nonempty
                  CHECK (subject_kind IS NULL OR btrim(subject_kind) <> ''),
    -- text, not uuid — see the header. Findings are uuids; panels are kinds;
    -- citations are ref tokens. All of them are subjects.
    subject_id    text        NULL
                  CONSTRAINT read_events_subject_id_nonempty
                  CHECK (subject_id IS NULL OR btrim(subject_id) <> ''),
    -- The stance the operator was standing in (train A's six workspaces).
    -- NOT NULL: every read happens somewhere, and "which stance does the
    -- operator actually live in" is one of the wager's real questions.
    workspace     text        NOT NULL
                  CONSTRAINT read_events_workspace_nonempty
                  CHECK (btrim(workspace) <> ''),
    -- Client-minted per browser session. Distinguishes one long morning from
    -- eight visits. Never an identity.
    session_nonce text        NOT NULL
                  CONSTRAINT read_events_session_nonce_nonempty
                  CHECK (btrim(session_nonce) <> ''),
    -- Milliseconds of attention, where a close/blur made it cheap to observe.
    -- Nullable because most opens never report one, and a zero-filled dwell
    -- would drag every average toward a lie.
    dwell_ms      integer     NULL
                  CONSTRAINT read_events_dwell_nonneg
                  CHECK (dwell_ms IS NULL OR dwell_ms >= 0),
    -- Subjects arrive whole or not at all.
    CONSTRAINT read_events_subject_pair
        CHECK ((subject_kind IS NULL) = (subject_id IS NULL)),
    -- Clock-skew bound. A laptop set a year ahead must not be able to poison
    -- the day-90 readout; the ingest endpoint rejects such a batch loudly.
    CONSTRAINT read_events_not_far_future
        CHECK (occurred_at < now() + interval '1 day')
);

-- THE ROLLUP INDEX — "events by kind by day, last 30d", which is the only
-- query the scoreboard runs and the shape every wager readout takes.
CREATE INDEX IF NOT EXISTS idx_read_events_occurred_kind
    ON public.read_events (occurred_at DESC, event_kind);

-- "Did the operator open the morning product today / on how many of the last
-- 90 days?" — THE headline wager metric, so it gets its own partial index
-- rather than scanning the whole log for the minority kind.
CREATE INDEX IF NOT EXISTS idx_read_events_brief_read
    ON public.read_events (occurred_at DESC)
    WHERE event_kind = 'brief_read';

-- "How many distinct reading sessions, and how long were they?" — the
-- session-shape half of the readout.
CREATE INDEX IF NOT EXISTS idx_read_events_session
    ON public.read_events (session_nonce, occurred_at);

-- "Which findings actually got drilled?" — the §2.2 question (59,771
-- preserved artifacts backing a trust operation nobody performs). Partial to
-- the two kinds that constitute a drill.
CREATE INDEX IF NOT EXISTS idx_read_events_subject
    ON public.read_events (subject_kind, subject_id, occurred_at DESC)
    WHERE subject_id IS NOT NULL;

-- Schema-enforced append-only posture. See the header: this ledger grades the
-- operator, and the operator owns the database, so the guard is the whole
-- point rather than a formality.
CREATE OR REPLACE FUNCTION public.read_events_forbid_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION
        'read_events rows are never updated or deleted — the read log is '
        'append-only; it is the evidence base for the oracle wager and must '
        'not be retouchable by the party it measures';
END;
$$;

DROP TRIGGER IF EXISTS trg_read_events_forbid_delete
    ON public.read_events;
CREATE TRIGGER trg_read_events_forbid_delete
    BEFORE DELETE ON public.read_events
    FOR EACH ROW EXECUTE FUNCTION public.read_events_forbid_mutation();

DROP TRIGGER IF EXISTS trg_read_events_forbid_update
    ON public.read_events;
CREATE TRIGGER trg_read_events_forbid_update
    BEFORE UPDATE ON public.read_events
    FOR EACH ROW EXECUTE FUNCTION public.read_events_forbid_mutation();
