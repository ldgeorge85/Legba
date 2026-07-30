-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0112_retrieval_origin.sql
--
-- R-3b (search provider layer, planning/SEARCH_PROVIDER_REVIEW_2026-07-28.md
-- §6.1 + §6.5): the RETRIEVAL-ORIGIN axis — where a piece of evidence came
-- from, as distinct from who published it and what its licence permits.
--
-- WHY A NEW CONCEPT AND NOT A NEW `source_class` VALUE
-- ----------------------------------------------------
-- `SourceClass` (schemas/source.py) is `reporting | analysis | official |
-- state_media` — an EDITORIAL AUTHORITY class, consumed by
-- `signal_salience.AUTHORITY_RANK` (official:4, reporting:3, analysis:2,
-- state_media:1, unknown:0) and `collection_gap.SOURCE_CLASSES_BY_DIMENSION`.
-- Adding a `web_search` member to that Literal would be a CATEGORY ERROR twice
-- over: a Reuters article found via search is still `reporting`, and an
-- unrecognised value silently drops to authority rank 0 — corrupting salience
-- for every row carrying it. The origin axis is orthogonal and gets its own
-- field. (`grep origin_class` returned zero hits repo-wide before this.)
--
-- WHY IT MATTERS ENOUGH TO BE A COLUMN
-- -------------------------------------
-- A search hit is a fundamentally different evidentiary object from a curated
-- feed item: unvetted, unranked by us, from an unbounded domain set, and
-- SELECTED BY AN UPSTREAM ENGINE'S RELEVANCE MODEL — which is an adversarially
-- gameable input (SEO poisoning aimed at exactly the queries an OSINT platform
-- asks). Two downstream gates need to read it, and both fail badly without it:
--
--   * CALIBRATION (§6.1). Web-retrieved evidence is cheap and abundant; it will
--     dominate volume within weeks of search going live. Untagged, the headline
--     EXOGENOUS Brier silently degrades into "how well do we predict things
--     that are easy to search", and no existing test fails. The code-side half
--     of the fix is the `web_evidence` resolution label placed in the WEAK tier
--     (calibration_tracking) — `hypotheses.resolved_by` is already `text`
--     (migration 0038), so that half needs NO DDL and none is written here.
--   * ARCHIVE RETENTION (§6.5). `evidence_archiver`'s licence gate fails OPEN —
--     unknown/unset `license_class` ARCHIVES — justified by a one-time LIC-1
--     audit of ~48 curated sources. That rationale does not survive an
--     unbounded, unreviewed open-web domain set. The gate now inverts for
--     web-origin rows only, which requires knowing the origin.
--
-- VALUES (a convention, enforced in code, not by a CHECK — the vocabulary will
-- grow with the provider set and a CHECK would turn each addition into a
-- migration):
--   NULL / absent          — a CURATED registered source. The honest default:
--                            every existing row is exactly this, so there is NO
--                            backfill and no lie about rows written before the
--                            concept existed.
--   'curated_source'       — an explicit statement of the same thing.
--   'web_search:<component_id>'
--                          — retrieved via the search provider named. The
--                            component id is carried so a later audit can ask
--                            WHICH provider introduced which claims (the same
--                            discipline as `judge_llm_ref` on the critique row).
--
-- The column mirrors `license_class`'s carriage exactly: stamped on the signal
-- payload at ingest, projected as an OpenSearch keyword facet, and readable as
-- a column for the gates that must not depend on a jsonb read. Both are read
-- by `legba.data.retrieval_origin.resolve_retrieval_origin`, which is the ONE
-- resolver — so the archive gate and the corpus facet can never disagree.
--
-- SAFETY (idempotent, additive, forward-only): ADD COLUMN IF NOT EXISTS +
-- CREATE INDEX IF NOT EXISTS + a DROP-then-ADD of one CHECK whose replacement
-- is a strict SUPERSET of the original (no existing row can violate it).
-- Re-apply and cold-start are both no-ops. NUMBERING: 0112 is this branch's
-- assigned slot (0109/0110/0111 are reserved for another wave); the runner
-- discovers by sorted glob, so the gap is harmless. The runner wraps this file
-- in its own transaction and records it in `legba_data_migrations` (no inline
-- BEGIN/COMMIT — same as 0091-0108).

-- ---------------------------------------------------------------------------
-- 1. The signal's retrieval origin.
-- ---------------------------------------------------------------------------

ALTER TABLE public.signals
    ADD COLUMN IF NOT EXISTS retrieval_origin text;

COMMENT ON COLUMN public.signals.retrieval_origin IS
    'Where this evidence was RETRIEVED from, orthogonal to source_class '
    '(editorial authority) and license_class (retention rights). NULL = a '
    'curated registered source (the default; no backfill). '
    '''web_search:<component_id>'' = discovered by that search provider — '
    'unvetted, from an unbounded domain set, selected by an upstream relevance '
    'model. Read via legba.data.retrieval_origin.resolve_retrieval_origin.';

-- Partial: the interesting rows are the non-curated minority, and keeping the
-- index off the NULL majority costs nothing on the hot ingest path.
CREATE INDEX IF NOT EXISTS signals_retrieval_origin_idx
    ON public.signals (retrieval_origin)
    WHERE retrieval_origin IS NOT NULL;

-- ---------------------------------------------------------------------------
-- 2. The archival ledger records the origin the gate evaluated.
-- ---------------------------------------------------------------------------
--
-- Same rationale as the existing `license_class` column on this table: record
-- WHAT THE GATE SAW, so a future policy flip can re-evaluate exactly the
-- affected rows mechanically instead of by memory.

ALTER TABLE public.evidence_archive
    ADD COLUMN IF NOT EXISTS retrieval_origin text;

COMMENT ON COLUMN public.evidence_archive.retrieval_origin IS
    'The retrieval_origin the P2-2 licence gate evaluated for this signal at '
    'archive/skip time. NULL = curated registered source (fail-OPEN posture: '
    'unknown licence archives). ''web_search:<id>'' = web-retrieved, which '
    'inverts the default to fail-CLOSED (unknown licence does NOT archive '
    'bytes; metadata is still recorded).';

-- ---------------------------------------------------------------------------
-- 3. A distinct terminal status for the web-origin fail-closed skip.
-- ---------------------------------------------------------------------------
--
-- `skipped_license` already means "a REVIEWED licence class forbade retention".
-- The web-origin skip is a different verdict — "we never reviewed this domain,
-- so we are not keeping its bytes" — and conflating them would make the ledger
-- unable to answer "how much did the fail-closed default cost us?", which is
-- exactly the question that decides whether to move to ledger-on-first-sight.
-- The replacement CHECK is a strict superset, so no existing row can violate it.

ALTER TABLE public.evidence_archive
    DROP CONSTRAINT IF EXISTS evidence_archive_status_check;

ALTER TABLE public.evidence_archive
    ADD CONSTRAINT evidence_archive_status_check CHECK (status IN (
        'archived',
        'failed',
        'skipped_license',
        'skipped_size',
        -- R-3b: web-retrieval origin + unreviewed licence ⇒ bytes NOT archived.
        'skipped_license_unreviewed'
    ));
