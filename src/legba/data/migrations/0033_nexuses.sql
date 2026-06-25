-- 0033_nexuses.sql — PIECE A: first-class reified typed Nexus table.
--
-- WHY: the old (pre-pivot) system reified an indirect relationship — A → typed
-- intermediary → B — as a FIRST-CLASS, queryable, decaying row carrying a
-- canonical POLARITY sign, intent, confidence, and temporal bounds (see
-- docs/archive/REIFIED_RELATIONSHIPS.md + NEXUS_AND_TEMPORAL.md). The
-- source-first substrate ships flat findings + untyped CoOccursWith edges and
-- never reifies the *signed, intent-typed* relationship — the single biggest
-- structural gap (DATA_ANALYSIS_DEEP_REVIEW_2026-06-16 Rank 1, D1 LOCKED:
-- Nexus = FULL first-class).
--
-- The dormant `nexus_decay` sub-handler
-- (data/analysts/deterministic_handlers/nexus_decay.py) already UPDATEs a
-- `nexuses` table referencing `confidence` / `created_at` / `valid_until` — a
-- table that NEVER existed in any migration (it was dead code that would fail
-- loud the moment it fired). This migration lands the real table so the decay
-- sweep + the structural-balance / proxy-chain refinement consumers light up.
--
-- TABLE SHAPE: mirrors the `facts` temporal pattern (0001 facts + 0032
-- valid_until/superseded_by) so the reifier's write path is a faithful copy of
-- `_insert_fact` (open-only triple uniqueness + value/polarity-change
-- supersession). The triple key is (subject, intermediary, object, rel_type) —
-- `intermediary` is NULL for a direct A→B relationship.
--
-- SAFETY: every statement is `CREATE TABLE IF NOT EXISTS` / `CREATE INDEX IF
-- NOT EXISTS` — idempotent + backward-safe. CREATE-only / clean-slate policy
-- honored (no data migration). Re-running against an already-migrated DB is a
-- no-op.

CREATE TABLE IF NOT EXISTS public.nexuses (
    id                uuid DEFAULT gen_random_uuid() NOT NULL,

    -- The reified triple. `subject` PartyTo the relationship; `object` is
    -- Targeted by it; `intermediary` (nullable) is the proxy/cut-out the
    -- relationship is ConductedVia. NULL intermediary = direct A→B.
    subject           text NOT NULL,
    intermediary      text,
    object            text NOT NULL,

    -- Typed label + canonical POLARITY sign. `rel_type` is the canonical
    -- predicate (HostileTo / AlliedWith / SuppliesWeaponsTo / …); `label` is a
    -- human-readable summary. `polarity` is the canonical sign the
    -- structural-balance theory uses: +1 supportive, -1 antagonistic, 0
    -- neutral/dual-use (excluded from triadic balance).
    rel_type          text NOT NULL,
    label             text DEFAULT ''::text NOT NULL,
    polarity          smallint DEFAULT 0 NOT NULL,
    -- Why the (intermediated) relationship exists — supportive / hostile /
    -- dual-use / neutral (the old Nexus `intent` field).
    intent            text DEFAULT ''::text NOT NULL,
    -- direct | proxy | covert | institutional (the old Nexus `channel` field;
    -- kept so the proxy-chain / priority consumers can filter).
    channel           text DEFAULT 'direct'::text NOT NULL,

    confidence        real DEFAULT 1.0 NOT NULL,

    -- Temporal lifecycle — mirrors facts (0001 valid_from + 0032
    -- valid_until/superseded_by). "What holds now" = the single OPEN row
    -- (valid_until IS NULL AND superseded_by IS NULL).
    valid_from        timestamp with time zone,
    valid_until       timestamp with time zone,
    superseded_by     uuid,

    -- Provenance. `derived_from` is the universal lineage array (the facts /
    -- proposed_edges parent ids); `source_signal_ids` is the convenience slice
    -- of the raw signals that evidenced the relationship.
    derived_from      uuid[] DEFAULT '{}'::uuid[] NOT NULL,
    source_signal_ids uuid[] DEFAULT '{}'::uuid[] NOT NULL,

    data              jsonb DEFAULT '{}'::jsonb NOT NULL,

    -- Universal provenance columns (mirror facts / analyst_outputs).
    target_id         text,
    target_version    text,
    analyst_id        text,
    analyst_version   text,
    produced_at       timestamp with time zone DEFAULT now() NOT NULL,
    schema_uri        text DEFAULT 'iglu:legba/nexus/jsonschema/1-0-0'::text NOT NULL,
    run_id            uuid,
    created_at        timestamp with time zone DEFAULT now() NOT NULL,
    updated_at        timestamp with time zone DEFAULT now() NOT NULL,

    CONSTRAINT nexuses_pkey PRIMARY KEY (id),
    CONSTRAINT nexuses_polarity_ck CHECK (polarity IN (-1, 0, 1))
);

-- Open-only partial UNIQUE on the typed triple — exactly the facts
-- `idx_facts_temporal_triple_open` pattern. `intermediary` is NULL for direct
-- edges, so COALESCE it to a sentinel for the index key (a NULL component
-- would make the unique index never collide on direct A→B re-asserts). Scoped
-- to OPEN rows only so a CLOSED (superseded) row keeps the same triple key as
-- a later re-assert WITHOUT participating in conflict inference — the
-- _insert_nexus ON CONFLICT upsert can only land on the single open row.
CREATE UNIQUE INDEX IF NOT EXISTS idx_nexuses_triple_open
    ON public.nexuses (
        lower(subject),
        lower(COALESCE(intermediary, '')),
        lower(object),
        lower(rel_type)
    )
    WHERE valid_until IS NULL AND superseded_by IS NULL;

-- Supersession-lookup support: supersede_prior_nexuses scans open rows for a
-- (subject, intermediary, object, rel_type) match to close value/polarity
-- changes. Keeps that UPDATE off a sequential scan as the open set grows.
CREATE INDEX IF NOT EXISTS idx_nexuses_open_triple
    ON public.nexuses (
        lower(subject), lower(COALESCE(intermediary, '')), lower(object)
    )
    WHERE valid_until IS NULL AND superseded_by IS NULL;

-- Decay-sweep index — the nexus_decay handler decays stale open rows by
-- created_at. Mirrors facts `idx_facts_decay_sweep`.
CREATE INDEX IF NOT EXISTS idx_nexuses_decay_sweep
    ON public.nexuses (created_at)
    WHERE superseded_by IS NULL;

-- Polarity/typed-edge read support for the structural-balance + proxy-chain
-- consumers (they pull SIGNED open nexuses incident to a seed entity set).
CREATE INDEX IF NOT EXISTS idx_nexuses_signed_open
    ON public.nexuses (rel_type)
    WHERE valid_until IS NULL AND superseded_by IS NULL AND polarity <> 0;
