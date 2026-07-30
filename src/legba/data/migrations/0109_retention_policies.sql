-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0109_retention_policies.sql
--
-- C2 "one janitor" (2026-07-28 coherence pass, WAVE C123): the substrate had
-- grown two LITERAL mirrors — `signals_retention` (migration 0036) and
-- `analyst_traces_retention` (migration 0101, whose own module header says it
-- "mirrors signals_retention exactly") — each hand-rolling its own TTL
-- constant, env-var name, batch-size default and keep-class list in Python.
-- This migration adds the ONE config surface both now read at run time: a
-- `retention_policies` row per target, executed by the shared
-- `retention_sweep` engine (deterministic_handlers/retention_sweep.py).
--
-- COLUMNS:
--   policy_name       — the stable identity (equals the sub_handler name that
--                        owns this policy today: 'signals_retention' /
--                        'analyst_traces_retention'). PK.
--   table_name        — the target table/domain, for operator legibility
--                        (documentation only; the actual DELETE/adapter SQL
--                        lives in the Python adapter keyed by policy_name).
--   ttl_days          — the policy's DEFAULT TTL. 0 (the DEFAULT) DISABLES
--                        the sweep — deleting substrate data is an operator
--                        decision, so every seeded row here ships INERT,
--                        matching the D4 / S-6 precedent exactly.
--   keep_classes      — retention_class values this policy NEVER purges
--                        regardless of age (empty array = the target has no
--                        such column / no exemption class, e.g.
--                        analyst_traces). Signals seeds `retain_always` +
--                        `evidence_hold` (the evidence_archiver upgrade
--                        target — see evidence_archiver.py's RETENTION
--                        HONESTY note).
--   batch_size        — per-run LIMIT on the age-ordered purge scan (bounds
--                        lock time at scale).
--   enabled           — an operator kill-switch INDEPENDENT of ttl_days (both
--                        default TRUE/0 respectively, so the seeded default
--                        behavior is unchanged: disabled because ttl_days<=0,
--                        not because of this flag). Lets an operator disable
--                        a policy without losing its configured TTL.
--   env_fallback_var  — the LEGBA_* env var name the engine reads when a run's
--                        options carry no explicit ttl_days (cadence fires
--                        inject ONLY {"sub_handler": ...} — the descriptor
--                        schema forbids a method.options block — so the env
--                        var remains the operator's real opt-in lever, exactly
--                        as before this migration; see signals_retention.py /
--                        analyst_traces_retention.py module docs).
--   description       — free-text operator note (what/why, migration refs).
--   created_by/created_at/updated_at — house-style provenance columns.
--
-- SCOPE NOTE (C2, deliberate): only `signals_retention` and
-- `analyst_traces_retention` are seeded/migrated onto this table now. Two
-- candidates surveyed and NOT folded here (their own janitor stays as-is):
--   * `nexus_decay` — a confidence-DECAY stamp (GREATEST(confidence-0.05,
--     0.1) on stale nexuses), never a DELETE. It is not a TTL purge, so it
--     does not fit this policy shape as-is; a future decay-policy table
--     could sit alongside this one, or this table could grow a `mode` column
--     ('purge' | 'decay') — nothing here precludes either path.
--   * archive retention — declared, unbuilt (evidence_archiver.py's
--     RETENTION HONESTY note + docs/SEAMS.md): archived objects have no TTL
--     today and nothing deletes them. A future archive-GC sweep is a
--     straightforward NEW policy_name + Python adapter against this same
--     table (table_name='evidence_archive', keep_classes for a legal hold,
--     etc.) — the schema needs no shape change to accommodate it.
--
-- SAFETY (idempotent, additive, forward-only): CREATE TABLE IF NOT EXISTS +
-- an ON CONFLICT DO NOTHING seed insert only; no existing table is touched;
-- re-apply and cold-start are both no-ops. The runner wraps this file in its
-- own transaction and records it in `legba_data_migrations` (no inline
-- BEGIN/COMMIT — same as 0091/0101/0105).

CREATE TABLE IF NOT EXISTS public.retention_policies (
    policy_name      text PRIMARY KEY,
    table_name       text NOT NULL,
    ttl_days         integer NOT NULL DEFAULT 0,
    keep_classes     text[] NOT NULL DEFAULT '{}'::text[],
    batch_size       integer NOT NULL DEFAULT 5000
                     CHECK (batch_size > 0),
    enabled          boolean NOT NULL DEFAULT TRUE,
    env_fallback_var text,
    description      text,
    created_by       text NOT NULL DEFAULT 'system',
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now()
);

-- The engine looks up one row per run by policy_name (its PK); the enabled
-- partial index keeps a future "list active policies" operator surface off a
-- full scan as the table grows past these two seed rows.
CREATE INDEX IF NOT EXISTS idx_retention_policies_enabled
    ON public.retention_policies (enabled)
    WHERE enabled;

-- Seed the two C2-migrated policies. Both ship DISABLED (ttl_days=0) —
-- byte-identical to the pre-migration Python constants
-- (signals_retention._DEFAULT_TTL_DAYS / analyst_traces_retention.
-- _DEFAULT_TTL_DAYS, both 0).
INSERT INTO public.retention_policies
    (policy_name, table_name, ttl_days, keep_classes, batch_size, enabled,
     env_fallback_var, description)
VALUES
    ('signals_retention', 'signals', 0,
     ARRAY['retain_always', 'evidence_hold']::text[], 5000, TRUE,
     'LEGBA_SIGNALS_RETENTION_TTL_DAYS',
     'TTL purge of aged signals + their value-referenced '
     'signal_entity_links/signal_aliases children (no DB-level FK to '
     'signals). Off by default — deleting signals is an operator decision. '
     'D4 / migration 0036 / signals_retention.py.'),
    ('analyst_traces_retention', 'analyst_traces', 0,
     '{}'::text[], 5000, TRUE,
     'LEGBA_ANALYST_TRACES_TTL_DAYS',
     'TTL purge of aged analyst_traces rows (analyst_critiques CASCADE; '
     'output_dead_letter.run_id SET NULL). Off by default. Keep any '
     'operator-set TTL well above the 7-day cadence-health window read by '
     'runtime_telemetry_api / the liveness watchdog. S-6 / migration 0101 / '
     'analyst_traces_retention.py.')
ON CONFLICT (policy_name) DO NOTHING;
