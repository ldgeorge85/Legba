-- 0032_facts_decay_columns.sql — add the temporal/decay columns fact_decay needs.
--
-- WHY: the `fact_decay` deterministic handler
-- (data/analysts/deterministic_handlers/fact_decay.py) UPDATEs the `facts`
-- table referencing three columns that the flattened baseline
-- (0001_baseline.sql, facts at :480) never carried:
--
--   * valid_until            — explicit fact expiry (NULL = open-ended)
--   * superseded_by          — id of the fact that replaced this one
--   * confidence_components  — jsonb audit of confidence contributions (incl.
--                              the per-decay `decay` delta the handler writes)
--
-- Postgres validates column references at plan time, so the moment `facts`
-- has rows the handler's two UPDATEs raise `UndefinedColumn` (today masked by
-- the handler's try/except + 0 rows). Piece 2 lights up real `facts` rows at
-- ingest, so these columns MUST exist or decay silently no-ops on every row.
--
-- SAFETY: every statement is `ADD COLUMN IF NOT EXISTS` / `CREATE INDEX IF NOT
-- EXISTS` — idempotent + backward-safe. New columns are nullable with no
-- default, so existing rows (and the ingest-time `_insert_fact`, which does
-- not project them) are unaffected. Re-running against an already-migrated DB
-- is a no-op. CREATE-only/clean-slate policy honored (no data migration).

ALTER TABLE public.facts
    ADD COLUMN IF NOT EXISTS valid_until           timestamp with time zone,
    ADD COLUMN IF NOT EXISTS superseded_by         uuid,
    ADD COLUMN IF NOT EXISTS confidence_components  jsonb;

-- Partial index supporting the decay/expiry sweep predicates
-- (superseded_by IS NULL AND valid_until …). Cheap; keeps the maintenance
-- UPDATEs from scanning the full table once volume grows.
CREATE INDEX IF NOT EXISTS idx_facts_decay_sweep
    ON public.facts (updated_at)
    WHERE superseded_by IS NULL;

-- ---------------------------------------------------------------------------
-- PIECE B temporal-fact hardening: make the triple uniqueness OPEN-only.
--
-- WHY: the baseline `idx_facts_temporal_triple` (0001_baseline.sql :2311) is a
-- FULL unique index on (lower(subject), lower(predicate), lower(value),
-- COALESCE(valid_from, '1970-01-01')). Piece B now closes prior rows in place
-- (valid_until/superseded_by set) rather than deleting them, so a CLOSED row
-- keeps the SAME triple+valid_from index key as a later re-assert of that same
-- value. The `_insert_fact` / `_insert_ingestion_fact` ON CONFLICT upsert then
-- matches the CLOSED row instead of inserting a fresh open one — re-opening a
-- superseded row's confidence/lineage while it stays closed, and leaving a
-- dangling superseded_by pointer at a row id that was never inserted.
--
-- FIX: scope the uniqueness to OPEN rows only (valid_until IS NULL AND
-- superseded_by IS NULL). Closed rows then never participate in conflict
-- inference, so a re-assert always opens a new canonical row and the upsert
-- only ever lifts confidence/lineage on the single open row. Drop the old
-- full index and replace it with the partial-on-open one; the ON CONFLICT
-- clauses in both write paths name this predicate so the inference matches.
DROP INDEX IF EXISTS public.idx_facts_temporal_triple;

CREATE UNIQUE INDEX IF NOT EXISTS idx_facts_temporal_triple_open
    ON public.facts (
        lower(subject), lower(predicate), lower(value),
        COALESCE(valid_from, '1970-01-01 00:00:00+00'::timestamptz)
    )
    WHERE valid_until IS NULL AND superseded_by IS NULL;

-- Supersession-lookup support: supersede_prior_facts scans open rows for a
-- (lower(subject), lower(predicate)) pair to close value-changes. A dedicated
-- partial index keeps that UPDATE off a sequential scan as the open-fact set
-- grows (the temporal-triple index can only prefix-match it).
CREATE INDEX IF NOT EXISTS idx_facts_open_subject_predicate
    ON public.facts (lower(subject), lower(predicate))
    WHERE valid_until IS NULL AND superseded_by IS NULL;
