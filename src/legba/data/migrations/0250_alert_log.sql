-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0250_alert_log.sql
--
-- The single alert path (lane L-1, 2026-11-13): ONE alert record.
--
-- WHY
-- ---
-- Before this there was no alert record: three shapes (analyst_outputs
-- kind='alert', alert_sink_deliveries doubling as watchdog state and
-- escalation emit record, descriptor_dead_letter) plus host scripts that
-- posted to the push service with no row at all. Every producer now calls
-- legba.data.alerts.raise_alert.raise_alert, which writes here.
--
-- THE TWO TABLES
-- --------------
--   * alerts        — one row per EPISODE: a kind about a subject, opened by
--                     the first raise, updated by later raises with the same
--                     dedupe_key while it is open (occurrences, last_raised_at,
--                     severity), closed by its producer (a recovery) or by the
--                     alert log after the kind's quiet window. At most one open
--                     episode per dedupe_key (the partial unique index).
--   * alert_events  — one row per RAISE: what was raised, by whom, and the
--                     paging DECISION with its reason and the page-rule entry
--                     that matched (paged / logged / held / deduped). Nothing
--                     is decided without a row here.
--
-- The delivery ledger stays alert_sink_deliveries (one row per sink outcome,
-- unchanged shape); every push decided by the page rule now names its episode
-- and its raise (alert_id, alert_event_id). Additive only; no existing column
-- or row changes.

CREATE TABLE IF NOT EXISTS public.alerts (
    id              uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    kind            text        NOT NULL,
    subject_kind    text        NOT NULL,
    subject_id      text        NOT NULL DEFAULT '',
    dedupe_key      text        NOT NULL,
    state           text        NOT NULL DEFAULT 'open',
    severity        text        NOT NULL,
    max_severity    text        NOT NULL,
    summary         text        NOT NULL,
    detail          text        NOT NULL DEFAULT '',
    evidence_refs   jsonb       NOT NULL DEFAULT '[]'::jsonb,
    raised_by       text        NOT NULL,
    opened_at       timestamptz NOT NULL DEFAULT now(),
    last_raised_at  timestamptz NOT NULL DEFAULT now(),
    closed_at       timestamptz,
    close_reason    text,
    occurrences     integer     NOT NULL DEFAULT 1,
    paged_count     integer     NOT NULL DEFAULT 0,
    paged_severity  text,
    last_paged_at   timestamptz,
    CONSTRAINT alerts_state_vocab CHECK (state IN ('open', 'closed')),
    CONSTRAINT alerts_severity_ladder CHECK (
        severity IN ('info', 'low', 'medium', 'high', 'critical')
        AND max_severity IN ('info', 'low', 'medium', 'high', 'critical')
        AND (paged_severity IS NULL
             OR paged_severity IN ('info', 'low', 'medium', 'high', 'critical'))
    ),
    CONSTRAINT alerts_closed_has_time CHECK ((state = 'closed') = (closed_at IS NOT NULL))
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_alerts_open_dedupe_key
    ON public.alerts (dedupe_key) WHERE state = 'open';
CREATE INDEX IF NOT EXISTS idx_alerts_open_last_raised
    ON public.alerts (last_raised_at) WHERE state = 'open';
CREATE INDEX IF NOT EXISTS idx_alerts_closed_at
    ON public.alerts (closed_at DESC) WHERE state = 'closed';
CREATE INDEX IF NOT EXISTS idx_alerts_kind_subject
    ON public.alerts (kind, subject_id, opened_at DESC);

CREATE TABLE IF NOT EXISTS public.alert_events (
    id              uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    alert_id        uuid        REFERENCES public.alerts (id) ON DELETE CASCADE,
    raised_at       timestamptz NOT NULL DEFAULT now(),
    kind            text        NOT NULL,
    severity        text        NOT NULL,
    subject_kind    text        NOT NULL,
    subject_id      text        NOT NULL DEFAULT '',
    dedupe_key      text        NOT NULL,
    action          text        NOT NULL,
    summary         text        NOT NULL,
    evidence_refs   jsonb       NOT NULL DEFAULT '[]'::jsonb,
    raised_by       text        NOT NULL,
    decision        text        NOT NULL,
    decision_reason text        NOT NULL,
    rule_entry      text,
    rule_version    text,
    outcomes        jsonb,
    CONSTRAINT alert_events_action_vocab CHECK (
        action IN ('opened', 'updated', 'closed', 'close_without_episode')
    ),
    CONSTRAINT alert_events_decision_vocab CHECK (
        decision IN ('paged', 'logged', 'held', 'deduped')
    ),
    CONSTRAINT alert_events_severity_ladder CHECK (
        severity IN ('info', 'low', 'medium', 'high', 'critical')
    ),
    CONSTRAINT alert_events_paged_names_entry CHECK (
        decision <> 'paged' OR rule_entry IS NOT NULL
    )
);

CREATE INDEX IF NOT EXISTS idx_alert_events_raised_at
    ON public.alert_events (raised_at DESC);
CREATE INDEX IF NOT EXISTS idx_alert_events_alert_id
    ON public.alert_events (alert_id, raised_at);
CREATE INDEX IF NOT EXISTS idx_alert_events_paged
    ON public.alert_events (raised_at DESC) WHERE decision = 'paged';

ALTER TABLE public.alert_sink_deliveries
    ADD COLUMN IF NOT EXISTS alert_id uuid,
    ADD COLUMN IF NOT EXISTS alert_event_id uuid;

CREATE INDEX IF NOT EXISTS idx_asd_alert_event_id
    ON public.alert_sink_deliveries (alert_event_id) WHERE alert_event_id IS NOT NULL;
