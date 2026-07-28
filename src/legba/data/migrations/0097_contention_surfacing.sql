-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0097_contention_surfacing.sql
--
-- P3-2 — the contested-claims arbiter TAIL (#101: soak -> weighted tie-break ->
-- coexistence semantics -> surfacing). The detect-only arbiter (0055 sidecar)
-- already surfaces a winner via `status`/`surfaced_value`/`surfaced_fact_id`;
-- this migration adds the COEXISTENCE-SEMANTICS record of HOW and WHEN that
-- winner was surfaced, plus the tie-break verdict cache:
--
--   * `fact_contention.surfaced_by`       — 'deterministic' | 'llm' (documented
--     vocabulary, no CHECK so a future decider class needs no DDL). NULL when
--     nothing is surfaced.
--   * `fact_contention.surfaced_at`       — when the CURRENT winner was first
--     surfaced (stable across passes while the decision stands; the
--     reversibility clock — evidence newer than this is "new evidence").
--   * `fact_contention.surface_rationale` — one operator-readable line: the
--     deterministic score/weight receipt, or the LLM verdict justification +
--     model id.
--   * `fact_contention.surface_history`   — jsonb array, NEWEST FIRST, of
--     superseded surface decisions (appended whenever the surfaced winner
--     changes or the group re-opens/collapses; capped by the arbiter at
--     `SURFACE_HISTORY_CAP`). The loser facts are NEVER touched — this is the
--     coexistence audit trail, not a supersession chain.
--   * `fact_contention_tiebreak`          — the LLM tie-break verdict CACHE,
--     keyed (contention_id, evidence_fingerprint): one row per contention per
--     evidence state, so an unchanged question is NEVER re-asked (mirrors the
--     `entity_judgement` re-adjudication cache). Only GENUINE verdicts are
--     cached ('pick' or an explicit model ABSTAIN -> 'unsure'); transport
--     failures are never cached so a recovered LLM can retry.
--
-- DETECT-ONLY (invariant B15) is UNCHANGED: everything here lives on the
-- sidecar; no `facts` column is added or written by this migration.
--
-- SAFETY (idempotent, additive, forward-only): ADD COLUMN IF NOT EXISTS /
-- CREATE TABLE IF NOT EXISTS are no-ops on re-apply and on a cold-start
-- substrate. The one UPDATE below is a bounded, one-shot backfill of the NEW
-- columns only (rows surfaced BEFORE this migration were deterministic Q·C·R·F
-- surfaces; their surface time is the recorded `resolved_at`) — it matches
-- zero rows on re-apply (`surfaced_at IS NULL` guard) and zero rows on a fresh
-- substrate. The runner wraps this file in its own transaction and records it
-- in `legba_data_migrations` (no inline BEGIN/COMMIT — same as 0091-0096).

ALTER TABLE public.fact_contention
    ADD COLUMN IF NOT EXISTS surfaced_by       text,
    ADD COLUMN IF NOT EXISTS surfaced_at       timestamptz,
    ADD COLUMN IF NOT EXISTS surface_rationale text,
    ADD COLUMN IF NOT EXISTS surface_history   jsonb NOT NULL DEFAULT '[]'::jsonb;

CREATE TABLE IF NOT EXISTS public.fact_contention_tiebreak (
    id                   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    contention_id        uuid NOT NULL
                           REFERENCES public.fact_contention(id) ON DELETE CASCADE,
    evidence_fingerprint text NOT NULL,   -- sha256 over the group's per-side evidence
    verdict              text NOT NULL
        CHECK (verdict IN ('pick', 'unsure')),
    winner_value_key     text,            -- NULL when verdict = 'unsure'
    justification        text,            -- the model's one-line why (recorded verbatim)
    model_id             text,            -- which model answered (self-hosted plane)
    decided_at           timestamptz NOT NULL DEFAULT now(),
    UNIQUE (contention_id, evidence_fingerprint)
);

-- One-shot backfill of the NEW columns for pre-0097 surfaced groups (all were
-- deterministic Q·C·R·F surfaces; `resolved_at` is the recorded surface pass).
UPDATE public.fact_contention
   SET surfaced_by = 'deterministic',
       surfaced_at = COALESCE(resolved_at, updated_at)
 WHERE status = 'surfaced'
   AND surfaced_fact_id IS NOT NULL
   AND surfaced_at IS NULL;
