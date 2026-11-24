-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0261_alert_ack_snooze.sql
--
-- S-2 (the phone's "what needs me", 2026-11-14): a person may ACKNOWLEDGE or
-- SNOOZE an alert EPISODE (one ``alerts`` row, migration 0250).
--
-- WHY
-- ---
-- "Needs you" (GET /v3/since?include=needs_you, S-1) lists open episodes. A
-- person who has seen one, or wants it out of the way for a few hours, needs
-- to say so once, for everyone: the instance is shared, so an acknowledgement
-- clears the item for every reader. Neither act closes the episode, changes
-- what the producer records, or changes what the page rule pushes.
--
-- WHAT IS ADDED
-- -------------
--   * on ``alerts`` — the CURRENT acknowledgement and snooze, nullable:
--       acked_at / acked_by / ack_note / ack_severity
--       snoozed_at / snoozed_by / snooze_option / snoozed_until / snooze_severity
--     ``*_severity`` is the BASELINE: the higher of the episode's severity and
--     the severity it had paged at, when the person acted. The episode comes
--     back into "needs you" when it pages at a severity ABOVE that baseline
--     (``paged_severity`` only rises when a page goes out). A later episode
--     for the same dedupe key is a new row, so it starts unacknowledged.
--     ``snooze_option`` is one of a CLOSED list; ``snoozed_until`` is NULL
--     only for 'until_closed'. No free-form far-future date can be stored.
--   * ``alert_episode_actions`` — one row per acknowledge / un-acknowledge /
--     snooze / un-snooze: who (the stable actor string, never a credential),
--     when, the optional note, and the signed audit row that covers it. The
--     sibling of ``alert_events`` (one row per raise); kept apart because a
--     person's act is not a raise and must not move the raise counts. Rows
--     outlive the episode's closing. ``alert_id`` names the episode WITHOUT a
--     foreign key, as ``alert_sink_deliveries.alert_id`` does: episodes are
--     closed, never deleted, in production, and an FK would make the
--     existing ``TRUNCATE alert_events, alerts`` test resets fail.
--
-- SAFETY
-- ------
-- Additive and idempotent: ADD COLUMN IF NOT EXISTS, CREATE ... IF NOT
-- EXISTS, and each CHECK dropped-if-exists then re-added under its name (the
-- 0255 pattern). Nothing existing changes; every new column is NULL on every
-- existing row, which the CHECKs accept. Routed through the runner (no inline
-- BEGIN / COMMIT).

ALTER TABLE public.alerts
    ADD COLUMN IF NOT EXISTS acked_at        timestamptz,
    ADD COLUMN IF NOT EXISTS acked_by        text,
    ADD COLUMN IF NOT EXISTS ack_note        text,
    ADD COLUMN IF NOT EXISTS ack_severity    text,
    ADD COLUMN IF NOT EXISTS snoozed_at      timestamptz,
    ADD COLUMN IF NOT EXISTS snoozed_by      text,
    ADD COLUMN IF NOT EXISTS snooze_option   text,
    ADD COLUMN IF NOT EXISTS snoozed_until   timestamptz,
    ADD COLUMN IF NOT EXISTS snooze_severity text;

ALTER TABLE public.alerts DROP CONSTRAINT IF EXISTS alerts_ack_shape;
ALTER TABLE public.alerts
    ADD CONSTRAINT alerts_ack_shape CHECK (
        (acked_at IS NULL AND acked_by IS NULL AND ack_note IS NULL
         AND ack_severity IS NULL)
        OR (acked_at IS NOT NULL AND acked_by IS NOT NULL
            AND ack_severity IN ('info', 'low', 'medium', 'high', 'critical')
            AND (ack_note IS NULL OR char_length(ack_note) <= 280))
    );

ALTER TABLE public.alerts DROP CONSTRAINT IF EXISTS alerts_snooze_shape;
ALTER TABLE public.alerts
    ADD CONSTRAINT alerts_snooze_shape CHECK (
        (snoozed_at IS NULL AND snoozed_by IS NULL AND snooze_option IS NULL
         AND snoozed_until IS NULL AND snooze_severity IS NULL)
        OR (snoozed_at IS NOT NULL AND snoozed_by IS NOT NULL
            AND snooze_severity IN ('info', 'low', 'medium', 'high', 'critical')
            AND (
                (snooze_option = 'until_closed' AND snoozed_until IS NULL)
                OR (snooze_option IN ('1h', '4h', '24h')
                    AND snoozed_until IS NOT NULL
                    AND snoozed_until <= snoozed_at + interval '24 hours 1 minute')
            ))
    );

CREATE TABLE IF NOT EXISTS public.alert_episode_actions (
    id              uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    alert_id        uuid        NOT NULL,
    at              timestamptz NOT NULL DEFAULT now(),
    action          text        NOT NULL,
    actor           text        NOT NULL,
    actor_kind      text        NOT NULL,
    actor_role      text        NOT NULL,
    note            text,
    snooze_option   text,
    snoozed_until   timestamptz,
    episode_severity text       NOT NULL,
    baseline_severity text,
    audit_id        uuid,
    CONSTRAINT alert_episode_actions_action_vocab CHECK (
        action IN ('acknowledged', 'unacknowledged', 'snoozed', 'unsnoozed')
    ),
    CONSTRAINT alert_episode_actions_note_len CHECK (
        note IS NULL OR char_length(note) <= 280
    ),
    CONSTRAINT alert_episode_actions_snooze_vocab CHECK (
        snooze_option IS NULL OR snooze_option IN ('1h', '4h', '24h', 'until_closed')
    ),
    CONSTRAINT alert_episode_actions_severity_ladder CHECK (
        episode_severity IN ('info', 'low', 'medium', 'high', 'critical')
        AND (baseline_severity IS NULL
             OR baseline_severity IN ('info', 'low', 'medium', 'high', 'critical'))
    )
);

CREATE INDEX IF NOT EXISTS idx_alert_episode_actions_alert
    ON public.alert_episode_actions (alert_id, at);

COMMENT ON TABLE public.alert_episode_actions IS
    'Acknowledge / snooze history per alert episode (mig 0261, S-2): one row '
    'per act, naming the stable actor (never a credential) and the signed '
    'descriptor_audit_log row (namespace alert). Written only through '
    'POST/DELETE /api/v1/v3/system/alerts/{episode_id}/ack|snooze.';
COMMENT ON COLUMN public.alerts.ack_severity IS
    'S-2 baseline: the higher of severity and paged_severity when acknowledged; '
    'the episode re-enters "needs you" when paged_severity rises above it.';
COMMENT ON COLUMN public.alerts.snooze_option IS
    'S-2: 1h | 4h | 24h | until_closed (a closed list; no free-form date).';
