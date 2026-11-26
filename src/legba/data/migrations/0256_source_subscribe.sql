-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0256_source_subscribe.sql
--
-- THE SUBSCRIBE ACQUISITION MODE a source Legba holds a
-- connection open to, receiving records as the upstream emits them, on the
-- same spine, sinks and receipts as a poll.
--
-- WHAT THIS ADDS
-- --------------
--   * `stream jsonb` (nullable) on `source_poll_outcomes`. A subscribed source
--     writes ONE outcome row per window (`subscribe.outcome_window_seconds`),
--     the poll-outcome equivalent the health and firing views already read.
--     The column carries what a window of a held connection did: the
--     connection state (connected / quiet / reconnecting / stalled /
--     refused / stopped), the transport, events received, malformed events,
--     reconnects, stalls, events dropped on queue overflow, gaps declared,
--     whether the last connect resumed, and the last event id. NULL on every
--     poll row: a poll writes the statement it always wrote.
--   * `source_stream_gaps` — one row per DECLARED gap in what a subscribed
--     source received. Kinds:
--       overflow   the bounded receive queue was full; `dropped` events were
--                  discarded between `after_event_id` and the first event
--                  accepted again (`first_dropped_id` .. `last_dropped_id`);
--       no_resume  a reconnect could not resume (the transport, the
--                  descriptor or the stream offered no resume point): what the
--                  upstream emitted between `opened_at` (disconnect) and
--                  `closed_at` (reconnect) was not received; `dropped` is NULL
--                  because the count is unknowable;
--       shutdown   the runtime stopped with received events still queued on a
--                  stream that cannot resume; `dropped` is their count.
--     A gap is never silent: the window row's `stream.gaps` counts it, and the
--     row here says where it is.
--
-- SAFETY
-- ------
-- Additive and idempotent (IF NOT EXISTS), routed through the runner (no
-- inline BEGIN/COMMIT). No backfill: nothing subscribed before this lane.

ALTER TABLE public.source_poll_outcomes
    ADD COLUMN IF NOT EXISTS stream jsonb;

COMMENT ON COLUMN public.source_poll_outcomes.stream IS
    'I-4: a subscribed source''s window (state, transport, events, malformed, '
    'reconnects, stalls, dropped, gaps, resumed, last_event_id). NULL for polls.';

CREATE TABLE IF NOT EXISTS public.source_stream_gaps (
    id                bigserial PRIMARY KEY,
    source_id         text        NOT NULL,
    source_version    text,
    owner_tenant      text        NOT NULL DEFAULT 'default',
    transport         text        NOT NULL,
    gap_kind          text        NOT NULL CHECK (gap_kind IN ('overflow', 'no_resume', 'shutdown')),
    opened_at         timestamptz NOT NULL,
    closed_at         timestamptz,
    after_event_id    text,           -- the last event id received before the gap
    first_dropped_id  text,           -- overflow / shutdown: first discarded event's id
    last_dropped_id   text,
    dropped           integer CHECK (dropped IS NULL OR dropped >= 0),  -- NULL = unknowable
    detail            jsonb,
    recorded_at       timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT source_stream_gaps_ordered CHECK (closed_at IS NULL OR closed_at >= opened_at)
);

CREATE INDEX IF NOT EXISTS source_stream_gaps_source_idx
    ON public.source_stream_gaps (source_id, opened_at DESC);

COMMENT ON TABLE public.source_stream_gaps IS
    'I-4: declared gaps in a subscribed source''s received stream (queue '
    'overflow, a reconnect that could not resume, a shutdown with queued '
    'events on a non-resumable stream). Insert-only by the stream connector.';
