-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0240_scheduled_events.sql
--
-- THE ALWAYS-ON CALENDAR (W11-F141).
--
-- WHY
-- ---
-- When `search_signals` failed on a live consult, the run asserted "nothing
-- else scheduled in the window" from MODEL MEMORY — and there was no way for
-- any consult to tell "nothing is scheduled" apart from "I could not look".
-- The platform holds no register of FUTURE dated things (summits, votes,
-- deadlines, strikes); every substrate table records what HAPPENED or what is
-- being WATCHED. This table is that register: operator- and seed-entered
-- scheduled events every consult gets in its run context.
--
-- WHAT THIS ADDS
-- --------------
--   * `scheduled_events` — one row per known future dated thing:
--       title, the occurrence span [starts_at, ends_at) (ends_at NULL = a
--       point event), a scope tag (a country/target code or 'global'), the
--       free-form subject_keys a scoped read matches on, an optional
--       source_url, a confidence class (confirmed | reported | tentative),
--       and provenance ('operator' | 'seed' | 'signal:<id>' for the later
--       lane that pulls events out of reporting — none here).
--   * `retired_at` — the soft-delete: a cancelled/rescheduled event is
--     retired, never deleted, so a consult that cited it keeps a record.
--   * three indexes, named for their readers: the window scan (starts_at),
--     the scoped window scan (scope, starts_at), and a UNIQUE (title,
--     starts_at) that makes the loader's re-runs idempotent.
--
-- SAFETY
-- ------
-- Additive and idempotent (CREATE ... IF NOT EXISTS); routed through the
-- filename-gated migration runner (ONE txn + ledger per file; no inline
-- BEGIN/COMMIT). The table ships EMPTY: the machinery is in-repo, the
-- calendar data is an operator-entered / gitignored-seed record (house rule
-- 8 — no seed DATA in the repo).

CREATE TABLE IF NOT EXISTS public.scheduled_events (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    title        text NOT NULL,
    starts_at    timestamptz NOT NULL,
    ends_at      timestamptz,
    scope        text NOT NULL DEFAULT 'global',
    subject_keys text[] NOT NULL DEFAULT '{}',
    source_url   text,
    confidence   text NOT NULL DEFAULT 'confirmed'
                 CHECK (confidence IN ('confirmed', 'reported', 'tentative')),
    provenance   text NOT NULL DEFAULT 'operator',
    created_at   timestamptz NOT NULL DEFAULT now(),
    retired_at   timestamptz
);

CREATE INDEX IF NOT EXISTS idx_scheduled_events_starts_at
    ON public.scheduled_events USING btree (starts_at);

CREATE INDEX IF NOT EXISTS idx_scheduled_events_scope_starts_at
    ON public.scheduled_events USING btree (scope, starts_at);

-- The loader's idempotence key: re-running a seed file re-writes nothing and
-- a same-title same-instant correction is a NEW row the operator reconciles,
-- not a silent overwrite.
CREATE UNIQUE INDEX IF NOT EXISTS uq_scheduled_events_title_start
    ON public.scheduled_events USING btree (title, starts_at);
