-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0202_events.sql — DATA MODEL V3 / P0, the event surface (spec §2.1).
--
-- NEW TABLES (three, additive; nothing existing is touched):
--
--   * `events` — one real-world occurrence, evidenced by one or more signals.
--     NOT a situation: a situation is a frame ("a thing we are watching"),
--     composed out of findings and living until it goes quiet; an event is an
--     occurrence ("a thing that happened"), evidenced by signals and bounded in
--     time. `analyst_id` is the PRODUCER id — 'event_clustering' when P1 lands,
--     'tower_backfill' for 0205 — and it is NOT NULL because NULLs are distinct
--     in a unique index and would silently duplicate (the writes.py:858 guard,
--     made structural). (event_signature, analyst_id) is the re-materialization
--     key: the same signature minted twice upserts in place, and two producers'
--     readings of one occurrence are deliberately TWO rows — reconciled later
--     by a correlated_with event_edge, never by the key.
--
--   * `signal_event_links` — the evidence edge. NO foreign key on signal_id,
--     deliberately: it is the shape `signal_entity_links` already ships, and a
--     retention sweep must never cascade-delete provenance. The orphan class
--     the previous design had (events with zero signals) is closed by the
--     §2.6 invariant instead — a rule a receipt can count, not a delete nobody
--     sees. `linked_at` carries NO default: it is EVIDENCE time (the signal's
--     own fetched_at), so a writer that does not know it must fail, not invent
--     wall-clock now().
--
--   * `event_entity_links` — the actor/place edge, resolved entity_id keyed.
--     The id-keyed path (entity_edges, 0143) is the precedent: links store the
--     profile UUID, never the name string. `fold_event_entity_links` below is
--     the merge-machinery half of that: when entity_researcher merges a loser
--     into a keeper, event actors repoint through resolve_entity() inside the
--     merge transaction — an event's actor can never keep pointing at a
--     tombstone.
--
-- WHAT THIS FILE DOES NOT DO (P0 deliberately ships no consumer):
--   * no reader, no API route, no UI surface;
--   * no writer is enabled — every live write goes through
--     writes._insert_event, which is gated behind LEGBA_EVENTS (default off);
--   * lifecycle_state is only ever written as 'emerging' on the write path —
--     the FSM lives in src/legba/data/events/lifecycle.py and the ledger in
--     0204. The column is a fast-SQL convenience; the ledger is the record.
--
-- SAFETY: CREATE IF NOT EXISTS everywhere; re-apply is a no-op.

-- ---------------------------------------------------------------------------
-- 1. events — the bounded occurrence
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS public.events (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),

    -- IDENTITY. The upsert key is the situations contract verbatim: one row per
    -- (signature, producer), re-materialized in place, never duplicated.
    event_signature text NOT NULL,
    analyst_id      text NOT NULL,        -- NOT NULL: NULLs are distinct in a unique
                                          -- index and would silently duplicate
                                          -- (the writes.py:858 guard, made structural)
    title           text NOT NULL,
    summary         text NOT NULL DEFAULT '',   -- NEVER evidence. See spec §2.5.

    -- CLASSIFICATION — the previous design's vocabulary, unchanged.
    category    text NOT NULL DEFAULT '',
    event_type  text NOT NULL DEFAULT 'incident'
        CHECK (event_type IN ('incident','development','shift','threshold')),
    severity    text NOT NULL DEFAULT 'medium'
        CHECK (severity IN ('critical','high','medium','low','routine')),

    -- LIFECYCLE. Current state is ALSO derivable from event_lifecycle_events;
    -- this column is the fast-SQL convenience, and the ledger is the record.
    -- Same split as situations.status vs situation_events.state_to.
    lifecycle_state text NOT NULL DEFAULT 'emerging'
        CHECK (lifecycle_state IN
               ('emerging','developing','active','evolving','resolved')),
    lifecycle_changed_at timestamptz NOT NULL DEFAULT now(),

    -- WHEN — validity time. An event spans; a signal is a point.
    time_start timestamptz,
    time_end   timestamptz,

    -- WHERE — the shape `signals` and `facts` already use, so the map needs no join
    geo        text[] NOT NULL DEFAULT '{}',     -- ISO2, signals.geo shape
    geo_lat    double precision,
    geo_lon    double precision,
    locations  text[] NOT NULL DEFAULT '{}',     -- free-text place surfaces

    -- QUALITY
    confidence            real NOT NULL DEFAULT 0.5
        CHECK (confidence >= 0.0 AND confidence <= 1.0),
    signal_count          int  NOT NULL DEFAULT 0,
    distinct_source_count int  NOT NULL DEFAULT 0,
    oversized             boolean NOT NULL DEFAULT false,  -- §2.4, the B1 guard

    -- TEMPORAL FRAME — the house supersession contract
    valid_from    timestamptz,
    valid_until   timestamptz,
    superseded_by uuid REFERENCES public.events(id) ON DELETE SET NULL,

    -- PROVENANCE — the standard envelope
    source_method text NOT NULL DEFAULT 'clustering'
        CHECK (source_method IN ('clustering','tower','manual')),
    source_type   text NOT NULL DEFAULT 'agent',
    derived_from  uuid[] NOT NULL DEFAULT '{}',
    target_id text, target_version text, analyst_version text, run_id uuid,
    schema_uri  text NOT NULL DEFAULT 'iglu:legba/event/jsonschema/1-0-0',
    data        jsonb NOT NULL DEFAULT '{}',
    produced_at timestamptz NOT NULL DEFAULT now(),
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT events_span_ordered
        CHECK (time_end IS NULL OR time_start IS NULL OR time_end >= time_start)
);

COMMENT ON TABLE public.events IS
    'DATA MODEL V3 / P0: a bounded real-world occurrence evidenced by signals. '
    'Keyed by (event_signature, analyst_id) — the signature is the identity and '
    'the analyst_id is the producer (''event_clustering'' live, '
    '''tower_backfill'' for the 0205 backfill), so two producers'' readings of '
    'one occurrence are two rows reconciled by a correlated_with edge, never '
    'merged by the key. lifecycle_state is a fast-SQL convenience; the ledger '
    'that owns the history is event_lifecycle_events (0204). summary is prose '
    'and NEVER evidence: every claim chains to a signal via '
    'signal_event_links.';

-- The upsert key _insert_event's ON CONFLICT targets.
CREATE UNIQUE INDEX IF NOT EXISTS uq_events_signature_analyst
    ON public.events (event_signature, analyst_id);

CREATE INDEX IF NOT EXISTS idx_events_lifecycle
    ON public.events (lifecycle_state, lifecycle_changed_at DESC);
CREATE INDEX IF NOT EXISTS idx_events_asof
    ON public.events (valid_from, valid_until);
CREATE INDEX IF NOT EXISTS idx_events_time
    ON public.events (time_start DESC);
CREATE INDEX IF NOT EXISTS idx_events_geo
    ON public.events USING gin (geo);
CREATE INDEX IF NOT EXISTS idx_events_derived
    ON public.events USING gin (derived_from);
CREATE INDEX IF NOT EXISTS idx_events_target
    ON public.events (target_id) WHERE target_id IS NOT NULL;

-- ---------------------------------------------------------------------------
-- 2. signal_event_links — the evidence edge (no FK on signal_id, deliberately)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS public.signal_event_links (
    signal_id uuid NOT NULL,
    event_id  uuid NOT NULL REFERENCES public.events(id) ON DELETE CASCADE,
    relevance real NOT NULL DEFAULT 1.0
        CHECK (relevance >= 0.0 AND relevance <= 1.0),
    -- the three source axes, denormalized at link time so an event's
    -- cross-class composition is one query, never a four-table join (§2.8).
    -- source_class is the closed four-class vocabulary: reporting / analysis /
    -- official / state_media; the Python payload constrains it at write time.
    source_class text NOT NULL DEFAULT 'reporting',
    source_kind  text NOT NULL DEFAULT '',     -- the HANDLER: rss / telegram_channel / …
    source_id    text NOT NULL DEFAULT '',
    linked_at   timestamptz NOT NULL,          -- EVIDENCE time (the signal's fetched_at)
    created_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (signal_id, event_id)
);

COMMENT ON TABLE public.signal_event_links IS
    'DATA MODEL V3 / P0: which signal evidences which event. signal_id '
    'deliberately carries NO foreign key — the shape signal_entity_links '
    'ships, so a retention sweep can never cascade-delete provenance. '
    'linked_at is EVIDENCE time (the signal''s fetched_at), never the write''s '
    'wall clock, and has no default: a writer that does not know the evidence '
    'time must fail rather than invent one — the lifecycle FSM''s silence '
    'clocks run on max(linked_at).';

CREATE INDEX IF NOT EXISTS idx_sel_event
    ON public.signal_event_links (event_id, relevance DESC);

-- ---------------------------------------------------------------------------
-- 3. event_entity_links — actors / places, id-keyed
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS public.event_entity_links (
    event_id  uuid NOT NULL REFERENCES public.events(id)          ON DELETE CASCADE,
    entity_id uuid NOT NULL REFERENCES public.entity_profiles(id) ON DELETE CASCADE,
    role text NOT NULL DEFAULT 'actor'
        CHECK (role IN ('actor','target','location','observer','victim','mediator')),
    confidence   real NOT NULL DEFAULT 0.5,
    derived_from uuid[] NOT NULL DEFAULT '{}',
    created_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (event_id, entity_id, role)
);

COMMENT ON TABLE public.event_entity_links IS
    'DATA MODEL V3 / P0: which entity plays which role in which event. '
    'Id-keyed like entity_edges (0143): the stored value is the profile UUID, '
    'and an entity merge repoints every link through '
    'fold_event_entity_links() inside the merge transaction — a link can never '
    'keep pointing at a tombstone. derived_from carries the signal ids that '
    'motivated the link. CASCADE on both FKs, like signal_entity_links.';

CREATE INDEX IF NOT EXISTS idx_eel_entity
    ON public.event_entity_links (entity_id, role);

-- ---------------------------------------------------------------------------
-- 4. fold_event_entity_links(uuid) — the merge half of the actor edge
-- ---------------------------------------------------------------------------
--
-- Companion to fold_entity_edges (0143) for the event-actor table. Called by
-- entity_researcher.merge_pair INSIDE the merge transaction, immediately
-- beside the fold_entity_edges() call and AFTER the merged_into UPDATE — so
-- resolve_entity() reads the redirect this transaction just wrote. A loser
-- that is an event actor has its links repointed to resolve_entity()'s
-- terminal survivor; a (event_id, role) link the keeper already carries
-- coalesces — confidence maxes and derived_from unions onto the keeper row,
-- then the loser row is deleted (a link row is a materialization, not a
-- ledger). Returns the count of link rows touched, so the merge receipt can
-- say exactly what moved. Without this, events strand on tombstones exactly as
-- 10,646 proposed_edges rows did.
--
-- NOT flag-gated: with zero event_entity_links rows it is a zero-cost no-op,
-- and repointing is maintenance, not an event write.

CREATE OR REPLACE FUNCTION public.fold_event_entity_links(p_loser uuid)
RETURNS integer
LANGUAGE plpgsql
AS $$
DECLARE
    v_keeper uuid;
    v_merged integer := 0;
    v_moved  integer := 0;
BEGIN
    v_keeper := public.resolve_entity(p_loser);
    IF v_keeper IS NULL OR v_keeper = p_loser THEN
        RETURN 0;
    END IF;

    -- Pass 1: where the keeper already carries the same (event_id, role) link,
    -- fold the loser row onto it (confidence maxes, evidence unions) and delete
    -- the loser row — the PK would otherwise collide with pass 2's repoint.
    WITH absorbed AS (
        UPDATE public.event_entity_links k
           SET confidence   = GREATEST(k.confidence, l.confidence),
               derived_from = (
                   SELECT array_agg(DISTINCT x)
                     FROM unnest(k.derived_from || l.derived_from) AS x
               )
          FROM public.event_entity_links l
         WHERE l.entity_id = p_loser
           AND k.event_id  = l.event_id
           AND k.role      = l.role
           AND k.entity_id = v_keeper
        RETURNING l.event_id, l.role
    )
    DELETE FROM public.event_entity_links l
     USING absorbed a
     WHERE l.entity_id = p_loser
       AND l.event_id  = a.event_id
       AND l.role      = a.role;
    GET DIAGNOSTICS v_merged = ROW_COUNT;

    -- Pass 2: every remaining loser link repoints to the terminal survivor.
    UPDATE public.event_entity_links
       SET entity_id = v_keeper
     WHERE entity_id = p_loser;
    GET DIAGNOSTICS v_moved = ROW_COUNT;

    RETURN v_merged + v_moved;
END;
$$;

COMMENT ON FUNCTION public.fold_event_entity_links(uuid) IS
    'V3/P0: repoint event_entity_links off a merged entity onto '
    'resolve_entity()''s terminal survivor, coalescing (event_id, role) '
    'duplicates onto the keeper row (confidence maxes, derived_from unions, '
    'loser row deleted — a link is a materialization, not a ledger). Called by '
    'entity_researcher.merge_pair inside the merge transaction, after the '
    'merged_into UPDATE; returns the count of links touched for the receipt.';
