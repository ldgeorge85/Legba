-- 0034_seed_batches.sql — curated/authoritative seeding primitive (flavor b roots).
--
-- WHY: cold-starting a fresh instance with a real starting knowledge base
-- (current world leaders, alliance memberships, …) needs the seeded knowledge
-- to be DISTINGUISHABLE from live ingest/agent output so a seed set is
-- selectively refreshable + purgeable ("re-pull the leaders seed", "drop the
-- 2024 backfill"). See planning/SEEDING_SKETCH.md §"The cross-cutting
-- primitive". Without a batch marker, seeded rows are indistinguishable from
-- live and can never be re-synced.
--
-- The two halves of the primitive:
--   1. a `seed_batches` ledger row per import (source, kind, counts, manifest);
--   2. a nullable, indexed `seed_batch_id` FK on BOTH `facts` and `nexuses`
--      so every seeded row points back at the batch that produced it.
-- Seed writes also stamp `source_type` ∈ {seed, backfill} (both tables already
-- carry a free-text `source_type`; this migration adds it to `nexuses` which
-- lacked it, and documents the accepted values — there is no CHECK constraint
-- on either, so 'seed'/'backfill' are accepted as-is).
--
-- SAFETY: every statement is `CREATE TABLE/INDEX IF NOT EXISTS` /
-- `ADD COLUMN IF NOT EXISTS` — idempotent + backward-safe. New columns are
-- nullable with no default so existing rows and the existing write paths
-- (which do not project them) are unaffected; defaults preserve current
-- behavior. CREATE-only / clean-slate policy honored (no data migration).
-- Re-running against an already-migrated DB is a no-op.

-- ---------------------------------------------------------------------------
-- The seed-batch ledger.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.seed_batches (
    id           uuid DEFAULT gen_random_uuid() NOT NULL,

    -- The adapter that produced the batch (e.g. 'world_baseline'); free text.
    source       text NOT NULL,
    -- Coarse content tag the adapter declares (e.g. 'world_baseline',
    -- 'wikidata_leaders'); kept distinct from `source` so one adapter can emit
    -- multiple kinds over time.
    kind         text DEFAULT ''::text NOT NULL,
    -- Provenance class stamped on the rows of THIS batch — 'seed' (curated /
    -- authoritative) or 'backfill' (bulk historical). Mirrors
    -- facts/nexuses.source_type.
    source_type  text DEFAULT 'seed'::text NOT NULL,

    imported_at  timestamp with time zone DEFAULT now() NOT NULL,

    -- Per-run row tallies: {"facts": N, "nexuses": M, "entities": K, …}.
    counts       jsonb DEFAULT '{}'::jsonb NOT NULL,
    -- Free-form import manifest the adapter records (yaml sha, valid_from
    -- window, record list, dry_run flag, …) so a batch is reproducible /
    -- auditable. Refs by NATURAL KEY, not row ids.
    manifest     jsonb DEFAULT '{}'::jsonb NOT NULL,

    created_at   timestamp with time zone DEFAULT now() NOT NULL,

    CONSTRAINT seed_batches_pkey PRIMARY KEY (id)
);

-- Lookup support: "show / refresh / purge batches for source X".
CREATE INDEX IF NOT EXISTS idx_seed_batches_source
    ON public.seed_batches (source, imported_at DESC);

-- ---------------------------------------------------------------------------
-- The batch-marker FK on facts + nexuses.
-- ---------------------------------------------------------------------------
ALTER TABLE public.facts
    ADD COLUMN IF NOT EXISTS seed_batch_id uuid;

ALTER TABLE public.nexuses
    ADD COLUMN IF NOT EXISTS seed_batch_id uuid;

-- `nexuses` (0033) has no `source_type` column — facts does (0001). Add it so a
-- seeded nexus is as self-describing as a seeded fact (same accepted values).
ALTER TABLE public.nexuses
    ADD COLUMN IF NOT EXISTS source_type text DEFAULT 'agent'::text NOT NULL;

-- Partial indexes so "all rows of this batch" (refresh/purge) never seq-scans
-- once live volume grows. Partial-on-NOT-NULL keeps the index tiny (only the
-- minority of seeded rows are indexed).
CREATE INDEX IF NOT EXISTS idx_facts_seed_batch
    ON public.facts (seed_batch_id)
    WHERE seed_batch_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_nexuses_seed_batch
    ON public.nexuses (seed_batch_id)
    WHERE seed_batch_id IS NOT NULL;
