-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0113_collection_requirements.sql
--
-- R-2 — the durable link from "we have a hole" to "go collect on it". Two
-- organs already SEE the hole from different sides: `collection_gap` (a live
-- monthly analyst naming starved desk×dimension cells) and the standing
-- `hypotheses.status='source_request'` backlog (an assessor's live "no source
-- covers X" flag, via the `request_source` write tool). Neither produced a
-- concrete collection ACTION. This table is that missing first-class object:
-- a COLLECTION REQUIREMENT — durable, queryable, provenance-carrying, and
-- reviewable by an operator — never an auto-registered source.
--
-- Written EXCLUSIVELY by the `collection_gap` analyst (extended, not a new
-- organ — see the handler module docstring). Content columns are set ONCE at
-- INSERT and never rewritten by the analyst (append-only-content); the small
-- disposition sidecar (`status` / `reviewed_by` / `reviewed_at` /
-- `disposition_note`) is the ONLY thing an operator ever updates, via the
-- `/api/v1/v3/collection-requirements` route — reviewing, dismissing, or
-- marking a proposal `registered` (after THEY separately add/activate the
-- real source through the existing descriptor-registration path). This table
-- never writes to `source_descriptors`; a proposal is not an activation.
--
-- Columns:
--   * natural_key   — idempotency identity. 'collection_gap:<desk>:<dimension>'
--                     for a starved scorecard cell, 'source_request:<hypothesis
--                     id>' for an assessor-flagged coverage gap. UNIQUE — a
--                     natural key is written AT MOST ONCE, ever (re-running the
--                     analyst over a still-starved cell never re-proposes it;
--                     see the handler for the pre-check + this constraint as
--                     the schema-enforced backstop).
--   * origin        — which upstream organ raised it ('collection_gap' |
--                     'source_request'), CHECK-enforced closed vocabulary.
--   * desk          — the target_id (country desk) this requirement serves, or
--                     NULL when the origin carried none.
--   * dimension     — the fixed scorecard dimension (collection_gap.DIMENSIONS)
--                     when known, else NULL (a source_request has no fixed
--                     dimension).
--   * topic         — human-readable "what's missing" (the banding reason /
--                     the assessor's stated need).
--   * rationale     — WHY it matters (persistence stats for a gap cell, the
--                     assessor's stated rationale for a source_request).
--   * evidence_kind / evidence_id — the CONCRETE evidence that raised this
--                     requirement (no FK — the 0106 no-FK posture; both tables
--                     can outlive a superseded/closed evidence row):
--                     'analyst_output' + the starved cell's own scorecard row
--                     id (`analyst_outputs.kind='scorecard'`) for a
--                     collection_gap-origin row — the card that showed the
--                     cell insufficient, not collection_gap's own monthly
--                     rollup finding (whose id does not exist yet at handler
--                     time); 'hypothesis' + the hypotheses row id for a
--                     source_request-origin row.
--   * source_classes_wanted — the S1-T8 vocabulary (reporting/analysis/
--                     official/state_media) that would plausibly feed it, per
--                     the collection-doctrine map (or the generic default for
--                     a source_request with no fixed dimension).
--   * candidate_sources — jsonb array of ALREADY-KNOWN source_descriptors rows
--                     (any lifecycle state) whose scope matches — "reuse
--                     before create": a paused/retired/draft match is a
--                     reactivation candidate; an active match is an honest
--                     "this exists but the cell is still starved" flag (a
--                     quality gap, not a registration gap). Each entry:
--                     {descriptor_id, state, source_class, license_class,
--                      match_reason}.
--   * suggested_fetch_url — a KNOWN url (from a non-active candidate's own
--                     registered config) the operator could sample with the
--                     existing guarded `web_fetch` single-URL GET before
--                     deciding whether to reactivate the feed. NULL when no
--                     candidate carries one.
--   * fillable / unfillable_reason — HONESTY (constraint #4): when no known
--                     candidate exists, fillable=FALSE and unfillable_reason
--                     names why ('no_known_feed' — the only value the analyst
--                     stamps automatically today; open text so an operator or
--                     a later organ can annotate 'licence_forbidden' /
--                     'needs_credentials' once that evidence exists). An
--                     unfillable requirement is never dropped — it is itself
--                     intelligence about our collection posture.
--   * priority_rank — this requirement's rank AT PROPOSAL TIME (smaller =
--                     higher priority) — an operator-triage aid, not a live
--                     metric.
--   * status        — 'proposed' (default) | 'reviewed' | 'registered' |
--                     'dismissed', CHECK-enforced closed vocabulary. Only an
--                     operator (via the API route) advances it.
--   * reviewed_by / reviewed_at — set together (paired CHECK, the review_flags
--                     idiom) when an operator dispositions the row.
--
-- SAFETY (idempotent, additive, forward-only): CREATE TABLE/INDEX IF NOT
-- EXISTS only; no existing table is touched; re-apply and cold-start are both
-- no-ops. The runner wraps this file in its own transaction and records it in
-- `legba_data_migrations` (no inline BEGIN/COMMIT — same as 0091/0105/0107).

CREATE TABLE IF NOT EXISTS public.collection_requirements (
    id                     uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    natural_key            text        NOT NULL UNIQUE,
    origin                 text        NOT NULL
                           CHECK (origin IN ('collection_gap', 'source_request')),
    desk                   text,
    dimension              text,
    topic                  text        NOT NULL,
    rationale              text        NOT NULL DEFAULT '',
    evidence_kind          text        NOT NULL
                           CHECK (evidence_kind IN ('analyst_output', 'hypothesis')),
    evidence_id            uuid        NOT NULL,
    source_classes_wanted  text[]      NOT NULL DEFAULT '{}',
    candidate_sources      jsonb       NOT NULL DEFAULT '[]'::jsonb,
    suggested_fetch_url    text,
    fillable               boolean     NOT NULL DEFAULT TRUE,
    unfillable_reason      text,
    priority_rank          integer     NOT NULL DEFAULT 0,
    status                 text        NOT NULL DEFAULT 'proposed'
                           CHECK (status IN ('proposed', 'reviewed', 'registered', 'dismissed')),
    reviewed_by            text,
    reviewed_at            timestamptz,
    disposition_note       text,
    created_at             timestamptz NOT NULL DEFAULT now(),
    updated_at             timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT collection_requirements_review_pair
        CHECK ((reviewed_by IS NULL) = (reviewed_at IS NULL)),
    CONSTRAINT collection_requirements_fillable_pair
        CHECK (fillable OR unfillable_reason IS NOT NULL)
);

-- The operator review-surface list read: open proposals, highest-priority
-- (lowest rank) first, newest tie-break.
CREATE INDEX IF NOT EXISTS idx_collection_requirements_open
    ON public.collection_requirements (priority_rank, created_at DESC)
    WHERE status IN ('proposed', 'reviewed');

-- "What requirement did evidence row E raise?" (provenance walk).
CREATE INDEX IF NOT EXISTS idx_collection_requirements_evidence
    ON public.collection_requirements (evidence_kind, evidence_id);

-- Per-desk read (a desk's open requirements).
CREATE INDEX IF NOT EXISTS idx_collection_requirements_desk
    ON public.collection_requirements (desk, status)
    WHERE desk IS NOT NULL;
