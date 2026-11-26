-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- alert_page_rule_replay.sql — export the last 7 days (edit the one interval) of alert-like records
-- (the three earlier alerting systems) as one JSON line per would-be RAISE, with what
-- actually happened to it on the push service. READ-ONLY: run it inside a
-- read-only transaction, e.g.
--
--   { echo "BEGIN READ ONLY;"; cat scripts/alert_page_rule_replay.sql; echo "ROLLBACK;"; } \
--     | docker exec -i legba-postgres-1 psql -U legba -d legba -X -q -At > raises.jsonl
--
-- then: PYTHONPATH=src python3 scripts/alert_page_rule_replay.py raises.jsonl
--
-- `actual` is the ntfy ledger outcome(s) for the record ('delivered',
-- 'skipped_cooldown', 'skipped_level_only', 'skipped_below_severity'), or
-- empty when nothing reached the push service.

\copy (WITH since AS (SELECT now() - interval '7 days' AS t), ntfy AS (SELECT d.id, d.attempted_at, d.channel_name, d.status, d.alert_row_id, d.target_id, d.payload_summary FROM alert_sink_deliveries d, since WHERE d.sink_kind = 'ntfy' AND d.attempted_at >= since.t) SELECT json_build_object('src', 'trigger_scan', 'ts', o.produced_at, 'kind', o.data -> 'data' ->> 'trigger_class', 'severity', COALESCE(o.severity, o.data -> 'data' ->> 'severity'), 'subject', COALESCE(o.target_id, ''), 'ref', o.id::text, 'batch', o.data -> 'data' ->> 'push_batch', 'hold', CASE WHEN COALESCE((o.data -> 'data' ->> 'guard_suppressed')::boolean, false) THEN 'steady-state guard' WHEN COALESCE((o.data -> 'data' ->> 'budget_deferred')::boolean, false) THEN 'daily page budget' END, 'actual', (SELECT string_agg(n.status, ',') FROM ntfy n WHERE n.alert_row_id = o.id OR (n.payload_summary -> 'batch_alert_ids') ? o.id::text)) FROM analyst_outputs o, since WHERE o.analyst_id = 'alert_trigger_scan' AND o.schema_uri LIKE '%/alert/%' AND o.produced_at >= since.t UNION ALL SELECT json_build_object('src', 'escalate', 'ts', a.attempted_at, 'kind', 'escalations', 'severity', a.severity, 'subject', COALESCE(a.target_id, ''), 'ref', a.alert_row_id::text, 'actual', (SELECT string_agg(n.status, ',') FROM ntfy n WHERE n.alert_row_id = a.alert_row_id AND n.attempted_at BETWEEN a.attempted_at - interval '5 seconds' AND a.attempted_at + interval '2 minutes')) FROM alert_sink_deliveries a, since WHERE a.sink_kind = 'alert' AND a.attempted_at >= since.t UNION ALL SELECT json_build_object('src', 'watchdog', 'ts', w.attempted_at, 'kind', w.channel_name, 'severity', w.severity, 'subject', COALESCE(w.sink_target, 'pipeline'), 'state', COALESCE(w.payload_summary ->> 'state', 'entered'), 'ref', w.id::text, 'actual', (SELECT string_agg(n.status, ',') FROM (SELECT n.status FROM ntfy n WHERE n.channel_name = w.channel_name AND n.attempted_at BETWEEN w.attempted_at AND w.attempted_at + interval '30 seconds' ORDER BY n.attempted_at LIMIT 1) n)) FROM alert_sink_deliveries w, since WHERE w.sink_kind = 'liveness_watchdog' AND w.attempted_at >= since.t UNION ALL SELECT json_build_object('src', 'runtime_other', 'ts', n.attempted_at, 'kind', n.channel_name, 'severity', 'high', 'subject', COALESCE(n.target_id, n.payload_summary ->> 'summary', ''), 'ref', n.id::text, 'actual', n.status) FROM ntfy n WHERE n.channel_name IN ('analyst_hard_fail', 'forecast_resolution')) TO STDOUT
