-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0061: make alert DELIVERY durably auditable — repurpose alert_sink_deliveries.
--
-- WHY repurpose (not a new table): the escalate path's delivery record was
-- IN-MEMORY only (ChannelEmitter.emitted[] + the action_pack_invocations row).
-- The emit lands on NATS `channels.escalations` and NOWHERE durable, so
-- "who got alerted?" was unanswerable — a gap against the verified-honesty
-- thesis. The pre-existing `alert_sink_deliveries` table already models exactly
-- this ("a delivery of an analyst_outputs row to an operator-facing sink"):
-- it FK's `alert_row_id -> analyst_outputs(id)` (= the finding/output id) and
-- carries status/error/attempted_at/delivered_at/payload_summary. But every row
-- in it today came from the RETIRED country_assessor `alert` OUTPUT KIND path
-- (the only descriptor that ever bound `outputs: - kind: alert`), so the table
-- is functionally dead. Rather than leave a misleading dead table AND cut a
-- second near-identical one, we give it a LIVE purpose: it becomes the unified
-- per-delivery audit written by the ChannelEmitter escalate/incident EDGE
-- (and still usable by the dormant alert output kind if it is ever re-enabled).
--
-- The channel-emit edge needs four fields the sink-retry schema lacked — the
-- honesty columns the escalate gate resolves (target country, resolved
-- severity, the verify-FOLDED effective confidence) plus the pack channel name.
-- We ADD them (nullable, so the dormant alert-output-kind writer is unaffected)
-- and relax the three NOT NULLs the emit edge cannot fill (the analyst
-- descriptor identity is not in scope at the ChannelEmitter seam; a channel
-- emit may also reference no persisted finding).
--
-- Append-only + idempotent: ADD COLUMN IF NOT EXISTS / DROP NOT NULL / CREATE
-- INDEX IF NOT EXISTS all no-op on a re-run. No data is rewritten or deleted —
-- the historical retired-country_assessor rows stay (their new columns read
-- NULL, which plainly distinguishes them from live channel-emit rows).

-- Relax the columns the ChannelEmitter delivery edge cannot supply.
ALTER TABLE public.alert_sink_deliveries ALTER COLUMN alert_row_id DROP NOT NULL;
ALTER TABLE public.alert_sink_deliveries ALTER COLUMN descriptor_id DROP NOT NULL;
ALTER TABLE public.alert_sink_deliveries ALTER COLUMN descriptor_version DROP NOT NULL;

-- The channel-emit honesty columns (all nullable — the alert-output-kind writer
-- leaves them NULL; the ChannelEmitter escalate/incident edge fills them).
ALTER TABLE public.alert_sink_deliveries
    ADD COLUMN IF NOT EXISTS channel_name text;
ALTER TABLE public.alert_sink_deliveries
    ADD COLUMN IF NOT EXISTS target_id text;
ALTER TABLE public.alert_sink_deliveries
    ADD COLUMN IF NOT EXISTS severity text;
ALTER TABLE public.alert_sink_deliveries
    ADD COLUMN IF NOT EXISTS effective_confidence real;

-- "Who got alerted about target X, most recent first" — the audit read path.
CREATE INDEX IF NOT EXISTS idx_asd_target_emitted
    ON public.alert_sink_deliveries (target_id, attempted_at DESC);
