-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0055_fact_contention.sql
--
-- Holes-B Wave 1 — the contested-claims SIDECAR (#101). When two credible
-- sources disagree on a `(subject, predicate)` value, both rows legitimately
-- coexist OPEN (the open-triple unique index `idx_facts_temporal_triple_open`
-- keys on `lower(value)`), but the disagreement is invisible at the fact layer.
-- This migration adds the first-class, DERIVED, RECOMPUTABLE group view of every
-- live dispute, plus thin marker columns on `facts` so a reader can tell a
-- genuine update apart from a live dispute.
--
-- WHY A SIDECAR (not columns-on-facts):
--   Per-value support is multi-valued (N distinct values, each with its own
--   source set / credibility sum / counts) and RECOMPUTED on every arbiter pass.
--   Modeling that on the single-row `facts` table would force a JSON blob of all
--   sibling values on every row (write amplification) or denormalized per-value
--   columns that can't hold a variable N. The sidecar is the right home BECAUSE
--   it can be dropped and rebuilt from the open `facts` rows at any time — that
--   recomputability is the test that proves it is derived, not primary.
--
-- WHAT:
--   * `fact_contention`        — one group per (subject_key, predicate_key); its
--                                lifecycle is open(contested) -> surfaced ->
--                                collapsed-when-down-to-one. UNIQUE on the triple.
--   * `fact_contention_values` — one row per distinct NON-junk value cluster in a
--                                group, carrying the aggregated support + the
--                                deterministic Q·C·R·F arbiter score. A junk
--                                cluster is recorded with `is_junk=true` +
--                                `junk_reason` (OPERATOR-REPORTABLE, never silently
--                                dropped) and excluded from the dispute count.
--   * markers on `public.facts` — `contested`, `contention_id`, `surfaced_winner`
--                                (thin, indexable, no blob).
--
-- DETECT-ONLY (Wave 2): the arbiter that populates these tables NEVER closes,
--   supersedes, or rewrites a `facts` row (invariant B15). It only sets the three
--   marker columns + the sidecar rows. No write-path behavior changes here.
--
-- SAFETY (idempotent, additive, forward-only — no data rewrite, no data repair):
--   `CREATE TABLE IF NOT EXISTS` / `ADD COLUMN IF NOT EXISTS` /
--   `CREATE INDEX IF NOT EXISTS` are all no-ops on re-apply and on a fresh
--   cold-start substrate. The `facts` marker columns are nullable / DEFAULT false
--   so every existing row is left unmarked (uncontested) and no row is rewritten.
--   The runner wraps this file in its own transaction and records it in
--   `legba_data_migrations` (no inline BEGIN/COMMIT here — same as 0054/0053).
--   CREATE-only/clean-slate policy honored (no data migration).

CREATE TABLE IF NOT EXISTS public.fact_contention (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    subject_key      text NOT NULL,            -- lower(subject), trimmed
    predicate_key    text NOT NULL,            -- normalize_predicate(lower(predicate))
    status           text NOT NULL DEFAULT 'contested'
                       CHECK (status IN ('contested', 'surfaced', 'collapsed')),
    surfaced_value   text,                     -- the arbiter's current winner (NULL = abstained / none)
    surfaced_fact_id uuid,                     -- the winning open fact row (NULL when abstained)
    value_count      int  NOT NULL DEFAULT 0,  -- distinct NON-junk value clusters in the group
    junk_count       int  NOT NULL DEFAULT 0,  -- distinct junk-excluded clusters (operator-reportable)
    opened_at        timestamptz NOT NULL DEFAULT now(),
    resolved_at      timestamptz,              -- last arbiter pass that set/changed a winner
    arbiter_version  text,
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_fact_contention_triple
    ON public.fact_contention (subject_key, predicate_key);

CREATE TABLE IF NOT EXISTS public.fact_contention_values (
    id                     uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    contention_id          uuid NOT NULL
                             REFERENCES public.fact_contention(id) ON DELETE CASCADE,
    value_key              text NOT NULL,       -- canonical cluster key (canon of the representative value)
    representative_fact_id uuid,                -- the open fact row carrying this value (the keeper / winner anchor)
    distinct_source_count  int  NOT NULL DEFAULT 0,   -- DISTINCT lineage, not row count (defeats a chatty source)
    source_credibility_sum real NOT NULL DEFAULT 0,   -- SUM of non-NULL facts.source_credibility (NULL skipped)
    confidence_max         real NOT NULL DEFAULT 0,
    confidence_mean        real NOT NULL DEFAULT 0,
    source_types           text[] NOT NULL DEFAULT '{}',
    supporting_fact_ids    uuid[] NOT NULL DEFAULT '{}',
    latest_asserted_at     timestamptz,
    arbiter_score          real,                -- last computed Q·C·R·F score
    surfaced_winner        boolean NOT NULL DEFAULT false,
    is_junk                boolean NOT NULL DEFAULT false,   -- junk-gated cluster (excluded from the dispute)
    junk_reason            text,                -- which fact_extractor gate fired (operator-reportable)
    updated_at             timestamptz NOT NULL DEFAULT now(),
    UNIQUE (contention_id, value_key)
);

CREATE INDEX IF NOT EXISTS idx_fact_contention_values_group
    ON public.fact_contention_values (contention_id);

ALTER TABLE public.facts
    ADD COLUMN IF NOT EXISTS contested       boolean NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS contention_id   uuid,
    ADD COLUMN IF NOT EXISTS surfaced_winner boolean NOT NULL DEFAULT false;

-- Partial index: only the (few) contested rows are indexed, so the arbiter's
-- "clear my prior markers for this group" sweep + downstream surfacing joins
-- stay cheap regardless of total `facts` size.
CREATE INDEX IF NOT EXISTS idx_facts_contested
    ON public.facts (contention_id) WHERE contested;
