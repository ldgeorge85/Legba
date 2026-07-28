-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0105_watchlist.sql
--
-- P5-6 (Watchlist v2). Operator-defined STANDING WATCHES — "watch THIS
-- entity / topic / place" — the personal layer over the P1-3 system-defined
-- trigger set. One row per watch; the `watchlist_hit` trigger class inside
-- `alert_trigger_scan` evaluates active rows every scan and alerts on any
-- VERIFIED finding touching the watched thing, regardless of desk/severity
-- (unless the watch itself sets a `min_severity` floor).
--
-- A TABLE, deliberately NOT a descriptor family: watches are operator DATA
-- (cheap to add/remove, no versioning/lifecycle/audit-chain ceremony), not
-- system CONFIG. The CRUD surface is /api/v1/v3/watchlist (watchlist_api).
--
-- Columns:
--   * kind    — 'entity' | 'text' | 'geo' (closed vocabulary, CHECK-enforced).
--   * pattern — the kind-shaped spec (validated at the route; the scan is
--               defensive about junk):
--       entity : {"name": "<canonical or alias>"} and/or {"entity_id": "<uuid>"}
--       text   : {"query": "<plain terms>"}
--       geo    : {"countries": ["IR", ...]}  XOR
--                {"lat": <f>, "lon": <f>, "radius_km": <f>}
--   * label        — the operator's name for the watch; carried on every alert.
--   * min_severity — NULL = any (the default posture: a watch pages on ANY
--                    verified touch); else the minimum resolved finding
--                    severity (info|low|medium|high|critical ladder).
--   * active       — soft-delete flag. DELETE via the route flips this to
--                    FALSE (the row + its watermark history survive, so
--                    re-activating never re-pages already-seen hits).
--
-- No-refire state lives in `alert_trigger_watermarks` (migration 0091, open
-- trigger_class vocabulary) under trigger_class='watchlist_hit' with
-- watermark_key='<watch_id>|<finding_id>' — this table stays pure operator
-- intent.
--
-- SAFETY (idempotent, additive, forward-only): CREATE TABLE/INDEX IF NOT
-- EXISTS only; no existing table is touched; re-apply and cold-start are both
-- no-ops. The runner wraps this file in its own transaction and records it in
-- `legba_data_migrations` (no inline BEGIN/COMMIT — same as 0091/0103).

CREATE TABLE IF NOT EXISTS public.watchlist (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    kind         text NOT NULL
                 CHECK (kind IN ('entity', 'text', 'geo')),
    pattern      jsonb NOT NULL DEFAULT '{}'::jsonb,
    label        text NOT NULL,
    min_severity text
                 CHECK (min_severity IS NULL
                        OR min_severity IN
                           ('info', 'low', 'medium', 'high', 'critical')),
    created_by   text NOT NULL DEFAULT 'operator',
    active       boolean NOT NULL DEFAULT TRUE,
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now()
);

-- The scan reads WHERE active every ~10 minutes; the list route orders by
-- created_at. Both stay off a full scan as the table grows.
CREATE INDEX IF NOT EXISTS idx_watchlist_active_kind
    ON public.watchlist (active, kind);
CREATE INDEX IF NOT EXISTS idx_watchlist_created
    ON public.watchlist (created_at DESC);
