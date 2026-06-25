-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0046_source_poll_outcomes.sql
--
-- DQ-H5b (#88) — record a provenance row for every NON-productive source poll
-- (empty-fetch HTTP-200-but-0-signals, OR error) so the H5 cadence watchdog and
-- the UI can surface WHY a source has gone silent.
--
-- WHY:
--   The DQ-H5 watchdog (liveness_watchdog.check_source_cadence_once) DETECTS a
--   silent source by comparing now() to max(signals.fetched_at) per source, but
--   it cannot say WHY: a feed that returns HTTP-200-with-0-items, or one whose
--   fetch 4xx's / parse-fails (the handler swallows those and records health
--   internally — no exception escapes the poll), writes ZERO signals AND ZERO
--   error rows today. The watchdog's own alert body (liveness_watchdog.py)
--   explicitly asks the operator to "check for an empty-fetch / error
--   provenance row" — this table is that row.
--
--   A PRODUCTIVE poll (>=1 signal written) is self-evidencing via its signals
--   rows and is intentionally NOT logged here, so this table stays small (only
--   the interesting silent/failed polls) and never dwarfs `signals`.
--
-- WHAT (idempotent — CREATE / ALTER ... IF NOT EXISTS only; additive, no data
-- migration). `health_state` carries the handler's own diagnosis for the poll
-- ('healthy' = genuine empty feed, 'degraded' = transient/timeout, 'unhealthy'
-- = 4xx / parse-fail); `outcome` is the coarse 'empty' vs 'error' rollup the
-- watchdog keys on.

CREATE TABLE IF NOT EXISTS public.source_poll_outcomes (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_id       TEXT        NOT NULL,
    source_version  TEXT,
    owner_tenant    TEXT        NOT NULL DEFAULT 'default',
    outcome         TEXT        NOT NULL,   -- 'empty' | 'error' (CHECK below)
    health_state    TEXT,                   -- 'healthy' | 'degraded' | 'unhealthy' | NULL
    capped          BOOLEAN     NOT NULL DEFAULT FALSE,
    signals_written INTEGER     NOT NULL DEFAULT 0,
    error           TEXT,                   -- exception / handler last_error, when known
    occurred_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE public.source_poll_outcomes
    DROP CONSTRAINT IF EXISTS source_poll_outcomes_outcome_chk;
ALTER TABLE public.source_poll_outcomes
    ADD CONSTRAINT source_poll_outcomes_outcome_chk
    CHECK (outcome IN ('empty', 'error'));

-- The watchdog read is "latest outcome per source": (source_id, occurred_at DESC).
CREATE INDEX IF NOT EXISTS source_poll_outcomes_source_time_idx
    ON public.source_poll_outcomes (source_id, occurred_at DESC);
