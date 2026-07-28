-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0094_source_assurance_ledger.sql
--
-- P3-1 (source assurance ledger, layers 1+2 of the A6 program design —
-- planning/PROGRAM_RECOMMENDATIONS_2026-07-24.md §A6):
--
--   * `source_ratings`  — layer 2: explicit rubric grades per source, with the
--     Admiralty vocabulary (source reliability A–F × information credibility
--     1–6) as the DISPLAY vocabulary. Multi-rater from day one: the same
--     source can carry a public catalog rating AND a private corp/gov annex
--     rating CONCURRENTLY — identity of a "current" rating is
--     (source_id, rater, visibility_class), enforced by a partial unique
--     index over un-superseded rows. Every rating row carries its own
--     provenance (rater id, method, references, rated_at).
--   * `source_dossiers` — layer 1: cited descriptive facts about the source
--     (ownership, funding, state affiliation, type…) as compiled markdown +
--     structured references. One CURRENT dossier per source; history via the
--     same supersession chain.
--
-- Layer 3 (the EARNED track record computed from our own corroboration /
-- contention outcomes) is a LATER task gated on the contested-claims arbiter
-- tail — deliberately NOT in this migration.
--
-- HARD consumption rule (operator): grades NEVER touch the faithfulness
-- score. They feed weighting / flags / tie-breaks in later tasks; today the
-- only consumers are display surfaces (the assurance route + the source-list
-- `assurance_grade` projection). Nothing in the verify/judge path reads
-- these tables.
--
-- IDENTITY: `source_id` is the source DESCRIPTOR id (`source_descriptors.
-- descriptor_id`). Deliberately TEXT with NO foreign key: catalog seeds may
-- legitimately rate a source BEFORE it is registered (and a retired
-- descriptor's ratings remain as history), so the ledger must not be
-- FK-coupled to descriptor lifecycle.
--
-- SUPERSESSION (rating history): a new rating for the same (source_id,
-- rater, visibility_class) stamps the old row's `superseded_by` with the new
-- row's id — current = `superseded_by IS NULL`, history = walk the chain.
-- The self-FK is DEFERRABLE INITIALLY DEFERRED on purpose: the writer runs
-- UPDATE-old-then-INSERT-new inside one transaction (so the partial unique
-- index never sees two current rows), which requires the FK check to wait
-- until commit because the pointed-at row does not exist yet at UPDATE time.
--
-- COLUMN NAMING: the structured citation column is `refs` (JSONB array of
-- {url, title, …} objects) because `references` is a reserved word in SQL;
-- the wire format spells the field `references` per the A6 contract (see
-- source_assurance_api.py).
--
-- RUBRIC (JSONB, open but typed by convention — documented keys):
--   type              — outlet class (news_agency | state_media | ngo | …)
--   ownership         — who owns/funds it (free text, cited in the dossier)
--   state_affiliation — none | state_owned | state_aligned | state_funded | …
--   editorial_posture — declared/observed editorial stance (free text)
--   bias_notes        — free-text caveats a reader should know
-- Unknown keys are preserved (the loader warns, never drops) so private
-- annex raters can extend the rubric without a migration.
--
-- SAFETY (idempotent, additive, forward-only): CREATE TABLE/INDEX IF NOT
-- EXISTS only; no existing table is touched; re-apply and cold-start are
-- both no-ops. The runner wraps this file in its own transaction and records
-- it in `legba_data_migrations` (no inline BEGIN/COMMIT — same as 0091-0093).

CREATE TABLE IF NOT EXISTS public.source_ratings (
    id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_id             TEXT        NOT NULL,  -- source descriptor id (no FK on purpose)
    rater                 TEXT        NOT NULL,  -- e.g. 'catalog:worldmonitor' | 'operator:lewis' | 'annex:acme-corp'
    visibility_class      TEXT        NOT NULL DEFAULT 'public'
        CHECK (visibility_class IN ('public', 'private')),
    method                TEXT        NOT NULL
        CHECK (method IN ('catalog_seed', 'operator', 'derived')),
    -- Admiralty DISPLAY vocabulary; both nullable so a rubric-only rating
    -- (dossier facts graded later) is representable.
    admiralty_reliability TEXT
        CHECK (admiralty_reliability IN ('A', 'B', 'C', 'D', 'E', 'F')),
    admiralty_credibility TEXT
        CHECK (admiralty_credibility IN ('1', '2', '3', '4', '5', '6')),
    rubric                JSONB       NOT NULL DEFAULT '{}'::jsonb,
    refs                  JSONB       NOT NULL DEFAULT '[]'::jsonb,
    rated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    superseded_by         UUID REFERENCES public.source_ratings (id)
        DEFERRABLE INITIALLY DEFERRED
);

CREATE INDEX IF NOT EXISTS source_ratings_source_idx
    ON public.source_ratings (source_id);

-- Multi-rater identity: at most ONE current rating per
-- (source, rater, visibility class). A public catalog rating and a private
-- annex rating for the SAME source are distinct current rows; a re-rating by
-- the same rater must supersede, never duplicate.
CREATE UNIQUE INDEX IF NOT EXISTS source_ratings_current_uq
    ON public.source_ratings (source_id, rater, visibility_class)
    WHERE superseded_by IS NULL;

CREATE TABLE IF NOT EXISTS public.source_dossiers (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_id     TEXT        NOT NULL,  -- source descriptor id (no FK on purpose)
    dossier_md    TEXT        NOT NULL,  -- cited markdown, [N] markers resolving into refs
    refs          JSONB       NOT NULL DEFAULT '[]'::jsonb,
    compiled_by   TEXT        NOT NULL,  -- 'operator:<id>' | future 'analyst:<id>'
    compiled_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    superseded_by UUID REFERENCES public.source_dossiers (id)
        DEFERRABLE INITIALLY DEFERRED
);

CREATE INDEX IF NOT EXISTS source_dossiers_source_idx
    ON public.source_dossiers (source_id);

-- One CURRENT dossier per source (dossiers are descriptive facts, not
-- per-rater opinion — visibility classes live on ratings only today; a
-- private-dossier annex would be a later, explicit schema extension).
CREATE UNIQUE INDEX IF NOT EXISTS source_dossiers_current_uq
    ON public.source_dossiers (source_id)
    WHERE superseded_by IS NULL;
