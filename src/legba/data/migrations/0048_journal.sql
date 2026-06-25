-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0048_journal.sql
--
-- Journal Assessor (planning/JOURNAL_ASSESSOR_PLAN.md §3.3 / §7.3 / §8) — the
-- 11th OutputKind `journal`. The journal is Legba's first-person reflective
-- voice: the ONE analyst pointed at the whole organism (including itself). It
-- writes EXACTLY ONE row family — `journal` (entry | consolidation) — into the
-- dedicated `journal_entries` table, fully OFF the fact/finding/nexus chain.
--
-- WHY a dedicated table (NOT an analyst_outputs `kind='journal'` row): a journal
-- claim is a *perspective over* the provenance chain, never a *member of* it.
-- Letting the journal write facts/nexuses would pollute the temporal-supersession
-- and grounding machinery with lyrical, perspective-laden assertions. The off-
-- chain invariant (§3.1) is the single most important property in the design and
-- is enforced two ways: (1) at the grant layer (the analyst is granted ONLY the
-- journal read pack) and (2) at the chain layer — journal rows carry an
-- ALWAYS-EMPTY `derived_from` and the table is deliberately NOT registered in the
-- lineage catalog (`lineage_api._SUBSTRATE_TABLES`), so a downstream lineage walk
-- FROM a fact/situation/nexus can NEVER surface a journal node (§3.5).
--
-- Both `journal_entries` (§8) and `journal_proposals` (§7.3) are created here even
-- though the propose_* toolset is Wave 4 — the plan pins both in 0048.
--
-- WHAT (idempotent — CREATE ... IF NOT EXISTS only; additive, no data migration).

-- ---------------------------------------------------------------------------
-- journal_entries — the one append-only, supersession-tracked store (§8).
-- The old Redis (working set) + OpenSearch (archive) split collapses into one
-- provenance-tracked store: the append-only table IS the permanent record.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.journal_entries (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    entry_kind      TEXT        NOT NULL,            -- 'entry' | 'consolidation' (the discriminator)
    title           TEXT        NOT NULL,
    body            TEXT        NOT NULL,            -- the narrative (markdown w/ inline [[ref:uuid]] markers)
    claims          JSONB       NOT NULL DEFAULT '[]'::jsonb,   -- per-claim binding: [{text_span, refs[], kind}] (§3.6)
    cited_substrate_refs UUID[] NOT NULL DEFAULT '{}',          -- flat union of all refs (query convenience)
    period_start    TIMESTAMPTZ NOT NULL,
    period_end      TIMESTAMPTZ NOT NULL,
    honesty_flags   TEXT[]      NOT NULL DEFAULT '{}',          -- forced deterministically (§10)
    -- temporal supersession (consolidation only) — mirrors facts/nexuses
    valid_from      TIMESTAMPTZ NOT NULL DEFAULT now(),
    valid_until     TIMESTAMPTZ,                     -- closed when a newer consolidation supersedes
    -- A PLAIN uuid (NOT a self-FK) — exactly the facts/nexuses pattern (migration
    -- 0032 / 0033). supersede_prior_consolidation closes the prior row pointing
    -- at the NEW id BEFORE the new row is inserted (close-prior-then-insert, the
    -- supersede_prior_facts contract); a self-FK would reject that ordering with
    -- a "key is not present" violation since the new row doesn't exist yet.
    superseded_by   UUID,
    -- standard provenance columns (mirror analyst_outputs)
    target_id       TEXT,
    target_version  TEXT,
    analyst_id      TEXT,
    analyst_version TEXT,
    produced_at     TIMESTAMPTZ NOT NULL,
    -- ALWAYS EMPTY for journal rows (§3.5): the direction-asymmetric lineage node.
    -- Citations live ONLY in `claims` / `cited_substrate_refs` (the UP-only walk).
    derived_from    UUID[]      NOT NULL DEFAULT '{}',
    schema_uri      TEXT        NOT NULL DEFAULT 'iglu:legba/journal/jsonschema/1-0-0',
    run_id          UUID,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- The "current inner landscape" = the single open consolidation row.
CREATE INDEX IF NOT EXISTS idx_journal_open_consolidation
    ON public.journal_entries (produced_at DESC)
    WHERE entry_kind = 'consolidation' AND valid_until IS NULL AND superseded_by IS NULL;

-- Enforce AT MOST ONE open consolidation (the supersession invariant; this is
-- what makes `supersede_prior_consolidation` race/replay safe — a concurrent or
-- replayed run that tries to open a second consolidation while one is still open
-- raises rather than double-opens). The `(true)` index expression is the global
-- partial-unique trick: every open-consolidation row indexes the same key.
CREATE UNIQUE INDEX IF NOT EXISTS uq_journal_single_open_consolidation
    ON public.journal_entries ((true))
    WHERE entry_kind = 'consolidation' AND valid_until IS NULL AND superseded_by IS NULL;

-- The "what changed since" / recent-entries reads scan by period_end.
CREATE INDEX IF NOT EXISTS idx_journal_entries_period
    ON public.journal_entries (period_end DESC);

-- ---------------------------------------------------------------------------
-- journal_proposals — the human-gated review queue (§7.3). Created now (the
-- propose_* toolset is Wave 4); NO producer writes it in Wave 0. The journal's
-- two-things-only rule (§7.1): it writes ONLY entries + consolidations directly;
-- everything else it wants to affect it PROPOSES into this queue, never to a live
-- table. A human always sits between the journal's voice and any change (§7.5).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.journal_proposals (
    id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    proposal_kind        TEXT        NOT NULL,        -- self_revision | correction | change
    proposed_by_analyst_id TEXT      NOT NULL,        -- journal_assessor
    run_id               UUID,                        -- the journal run that raised it
    rationale            TEXT        NOT NULL DEFAULT '',  -- in-voice "why I think this"
    diff                 JSONB       NOT NULL DEFAULT '{}'::jsonb,  -- the proposed change
    cited_substrate_refs UUID[]      NOT NULL DEFAULT '{}',         -- the lineage warrant
    status               TEXT        NOT NULL DEFAULT 'pending',    -- pending | accepted | rejected | archived
    decided_by           TEXT,                        -- operator
    decision_reason      TEXT,                        -- required on reject
    decided_at           TIMESTAMPTZ,
    produced_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- The operator review surface scans the pending queue.
CREATE INDEX IF NOT EXISTS idx_journal_proposals_pending
    ON public.journal_proposals (produced_at DESC)
    WHERE status = 'pending';
