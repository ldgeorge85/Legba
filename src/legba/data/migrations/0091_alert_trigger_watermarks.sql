-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0091_alert_trigger_watermarks.sql
--
-- P1-3 (verification-gated alerting, trigger set v1). Durable last-seen
-- watermarks for the `alert_trigger_scan` deterministic analyst so a verified
-- state TRANSITION fires exactly once — never refired on the next scan, and
-- never lost to a process restart (the reason this is a table, not actor
-- state or an in-process set).
--
-- One row per (trigger_class, watermark_key):
--   * trigger_class — 'band_crossing' | 'verified_finding' | 'contention_flip'
--                     | 'baseline_deviation' (open vocabulary; the handler
--                     owns the values).
--   * watermark_key — the transition identity within the class:
--       band_crossing      : '<desk>|<dimension>'  (state carries the last-seen
--                            band + scorecard row id, so from→to is derivable
--                            and a repeat of the same band can never refire)
--       verified_finding   : the finding uuid (append-style; pruned after the
--                            scan window ages out — see the handler)
--       contention_flip    : the fact_contention uuid (state carries the
--                            last-seen status + surfaced_fact_id fingerprint)
--       baseline_deviation : '<desk>|<metric>' (state carries the last
--                            exceeding flag — rising-edge firing)
--       '_seeded'          : per-class seed marker; its presence means the
--                            class completed its FIRST scan (which seeds
--                            watermarks silently and fires nothing).
--   * state    — small jsonb fingerprint of the last-seen value (band, status,
--                exceeding flag, ...). Never large.
--   * fired_at — last time this key actually fired an alert (NULL = observed /
--                seeded but never fired). Audit convenience only.
--
-- SAFETY (idempotent, additive, forward-only): CREATE TABLE/INDEX IF NOT
-- EXISTS only; no existing table is touched; re-apply and cold-start are both
-- no-ops. The runner wraps this file in its own transaction and records it in
-- `legba_data_migrations` (no inline BEGIN/COMMIT — same as 0089/0090).

CREATE TABLE IF NOT EXISTS public.alert_trigger_watermarks (
    trigger_class text NOT NULL,
    watermark_key text NOT NULL,
    state         jsonb NOT NULL DEFAULT '{}'::jsonb,
    fired_at      timestamptz,
    first_seen    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (trigger_class, watermark_key)
);

-- The verified_finding class appends one row per fired/seen finding id and
-- prunes rows older than its scan window; this index keeps that prune (and any
-- per-class age sweep) off a full-table scan.
CREATE INDEX IF NOT EXISTS idx_alert_trigger_watermarks_age
    ON public.alert_trigger_watermarks (trigger_class, first_seen);
