-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0221_claim_contentions.sql
--
-- Program 7a — the CONTRARY-EVIDENCE pass's record table
-- (planning/NEXT_ARC_CAPTURE_2026-09-23.md §A.1, §B1, §E 7a).
--
-- WHAT THIS TABLE IS FOR, AND THE ONE THING IT MUST NEVER HOLD. Every
-- retrieval this platform already runs points AT the claim: the standing
-- external auditor searches for the claim's own query and grades
-- SUPPORTED / CONTRADICTED / NOT_FOUND; the contested-facts arbiter compares
-- values that were already extracted; R2 (`claim_contradiction`) compares
-- claims that already came in; ACH resolves against the evidence base. Nothing
-- formulates the COUNTER-query. This table is where the counter-query's
-- RETRIEVAL lands.
--
-- It holds a RETRIEVAL OUTCOME, never a verdict on the claim. `stance` is a
-- statement about WHAT WAS FETCHED — "a page the platform holds asserts the
-- opposite", "a page narrows it", "nothing admissible came back", "the search
-- did not happen" — and it is derived in CODE from the fetched text against the
-- claim's own polarity. No model is asked whether the claim is true, here or
-- anywhere on this path: the model's only job upstream is to write a SEARCH
-- QUERY, and a query is not an assertion. The distinction is the whole design.
-- A verdict column would make this a second grader, and the platform already
-- has two (the faithfulness judge and the external audit) whose populations
-- must not be pooled with a third.
--
-- ─────────────────────────────────────────────────────────────────────────
-- 1. Identity — `claim_id` is the audit's own claim key
-- ─────────────────────────────────────────────────────────────────────────
-- `claim_id` is `_external_audit_claims.claim_key(text, origin_head_id, start,
-- end)` — sha256 over the FOLDED claim text and its byte-exact origin span,
-- the same key `external_grades.claim_key` carries. That is deliberate and it
-- is the prerequisite the capture names ("H3: a contention record must attach
-- to a claim id"): a contention row and an audit grade over the same published
-- sentence JOIN, so "we graded it SUPPORTED and retrieval found a page saying
-- the opposite" is one query rather than a manual reconciliation. It is a text
-- column, not a uuid, because the key is a content hash and is stable across a
-- replay a month later.
--
-- `finding_id` is the read the claim was published IN (the audit's
-- `graded_output_id`) and `block_ordinal` is its `[[ref:N]]` inside that read;
-- `origin_head_id` is the desk head the span was QUOTED FROM. Those are three
-- different joins and a surface that used the wrong one would silently show
-- nothing:
--
--   * the Inspector's claim chip and `GET /v3/contentions?finding_id=…` join on
--     (`finding_id`, `block_ordinal`), because the Inspector is rendering that
--     exact published read and
--     `|blocks| == |citations| == |distinct markers|` makes block N the
--     citation `[[ref:N]]`;
--   * the COMPOSITION tension rule joins on `claim_id`, because a composition
--     quoting the same desk-head span a day later is carrying the SAME claim —
--     the key is a hash of the folded text and its byte-exact origin, so it is
--     stable across cycles, while `finding_id` names only the one read the
--     record happened to be written against.
--
-- `target_id` / `desk_key` / `analyst_id` are the read's, carried so the
-- per-desk route needs no join at all.
--
-- ─────────────────────────────────────────────────────────────────────────
-- 2. The closed vocabularies
-- ─────────────────────────────────────────────────────────────────────────
-- `stance` — four values, and the split between the last two is the same one
-- the audit's UNCHECKED/NOT_FOUND split exists for: "we looked and found
-- nothing" and "we could not look" must never share a shape.
--
--   contradicts   a fetched page asserts the opposite of the claim on the
--                 claim's own polarity group (or, on the negation rule, denies
--                 a proposition the claim asserts)
--   qualifies     a fetched page agrees in direction but NARROWS the claim —
--                 partially, briefly, in one place, under a condition
--   none_found    the search answered and no fetched page was admissible
--   search_failed the retrieval did not happen: no counter-query could be
--                 formulated, the search plane refused, or every fetch failed
--
-- `derivation` — WHICH deterministic rule produced the stance, and it is a
-- column rather than a note because the two rules have very different standing:
--
--   polarity   the R2 closed polarity vocabulary (`claim_contradiction.
--              _POLARITY_GROUPS`), CALIBRATED against 1,592 live claims when R2
--              shipped. Only this derivation reaches a composition.
--   negation   the negation-anchored fallback for a claim that takes no side in
--              that vocabulary. It is NOT calibrated against a live corpus. It
--              is served to the route and to the Inspector chip — surfaces a
--              human reads — and is deliberately withheld from the composition
--              tension rule, because a false `contradicts` rendered into a
--              composition manufactures a disagreement for the fleet to write
--              up, which is exactly the failure R2 shipped zero pairs to avoid.
--   none       no stance rule applied (search_failed, or none_found with
--              nothing fetched).
--
-- `query_source` — `polarity` (the counter-query was built deterministically by
-- negating the claim's polarity group) or `model` (one bounded core-plane call
-- returned an opposing search query, re-validated in code against a paraphrase
-- guard before it was ever issued). The column is what makes the capture's
-- named risk — "is the counter-query the strongest opposing proposition, or a
-- paraphrase of the claim?" — a number somebody can compute rather than an
-- anxiety, and `query_novel_tokens` is that number's other half.
--
-- ─────────────────────────────────────────────────────────────────────────
-- 3. `refs` — a counter-ref the platform CITES is a page the platform HOLDS
-- ─────────────────────────────────────────────────────────────────────────
-- Each element of `refs` is one page that was actually fetched through the
-- governed `web_access` pack and its fences: `{url, sha256, chars,
-- published_at, extracted, fetched_at, status_code, stance, quote}`. `sha256`
-- is `research_evidence.content_hash_for` over the extracted text — the SAME
-- function the research plane hashes a landed page with, so the two spellings
-- can never disagree — and `published_at` is trafilatura's own metadata
-- resolution, a DISCOVERY that is null when the page states no date. Nothing
-- here is a URL we merely saw in a result list: a search snippet never becomes
-- a ref.
--
-- `ref_signal_ids` LINKS, it does not create. When the fetched page's content
-- hash already matches a `signals` row (the research pack landed it earlier,
-- `origin_class='web_retrieval'`) its id is recorded here. This pass writes NO
-- signals of its own, and that is a decision rather than an omission: a
-- contrary pass selects pages ADVERSARIALLY — it goes looking for the strongest
-- opposition — and landing that selection in the live corpus would move
-- freshness, source health, salience and calibration for every desk that reads
-- it, silently and in one direction. The standing auditor made the same call
-- (it records `decisive_url` + `decisive_span_sha256` on `external_grades` and
-- writes no signals); this table follows it.
--
-- ─────────────────────────────────────────────────────────────────────────
-- 4. The stamps, and what `receipt_id` actually points at
-- ─────────────────────────────────────────────────────────────────────────
-- `as_of` is the moment the row SPEAKS ABOUT (the pass's own clock for this
-- claim); `retrieved_at` is when the pages were fetched; `expires_at` is when
-- the next pass would have covered the claim, after which the composition
-- reader stops admitting the row and the route marks it expired. A reader is
-- never left to guess which clock it is holding — the platform has been bitten
-- by exactly that before.
--
-- `receipt_id` is the `analyst_traces.run_id` of the pass that wrote the row.
-- There is no `receipt_id` convention in this tree to match (there is none
-- anywhere), so this column states its join in a COMMENT rather than implying
-- one: `analyst_traces.run_id` is what carries `receipt_hash` /
-- `prev_receipt_hash`, the house's real receipt chain, and going through it is
-- what makes a contention row replayable back to the run that produced it.
--
-- IDEMPOTENT AND APPEND-ONLY IN EFFECT. `(claim_id, pipeline_version, as_of_day)`
-- is unique: one contention record per claim per pass version per UTC day, so a
-- re-run inside a day is a no-op and a later day appends honest history. The
-- `external_grades` precedent (unique on claim + pipeline + family) with a day
-- leg, because this pass runs daily and its answer can legitimately change when
-- the web does.
--
-- The runner wraps this file in its own transaction and records it in
-- `legba_data_migrations` (no inline BEGIN/COMMIT — same as 0204/0206/0207/
-- 0209/0220).

CREATE TABLE IF NOT EXISTS public.claim_contentions (
    id               uuid NOT NULL DEFAULT gen_random_uuid(),
    claim_id         text NOT NULL,
    claim_text       text NOT NULL,
    finding_id       uuid,
    origin_head_id   uuid,
    block_ordinal    integer,
    span_role        text,
    target_id        text,
    desk_key         text NOT NULL DEFAULT '',
    analyst_id       text NOT NULL DEFAULT '',
    query            text NOT NULL,
    query_source     text NOT NULL,
    query_novel_tokens integer NOT NULL DEFAULT 0,
    polarity_group   text,
    polarity_sign    smallint,
    rung             text NOT NULL DEFAULT '',
    stance           text NOT NULL,
    derivation       text NOT NULL DEFAULT 'none',
    reason           text NOT NULL DEFAULT '',
    statement        text NOT NULL DEFAULT '',
    refs             jsonb NOT NULL DEFAULT '[]'::jsonb,
    ref_signal_ids   uuid[] NOT NULL DEFAULT '{}',
    pipeline_version text NOT NULL,
    retrieved_at     timestamptz NOT NULL DEFAULT now(),
    as_of            timestamptz NOT NULL,
    as_of_day        date NOT NULL,
    expires_at       timestamptz NOT NULL,
    receipt_id       uuid,
    CONSTRAINT claim_contentions_pkey PRIMARY KEY (id),
    CONSTRAINT claim_contentions_claim_id_present CHECK (claim_id <> ''),
    CONSTRAINT claim_contentions_pipeline_present CHECK (pipeline_version <> ''),
    CONSTRAINT claim_contentions_stance_vocab CHECK (
        stance IN ('contradicts', 'qualifies', 'none_found', 'search_failed')
    ),
    CONSTRAINT claim_contentions_derivation_vocab CHECK (
        derivation IN ('polarity', 'negation', 'none')
    ),
    CONSTRAINT claim_contentions_query_source_vocab CHECK (
        query_source IN ('polarity', 'model', 'none')
    ),
    CONSTRAINT claim_contentions_polarity_sign_vocab CHECK (
        polarity_sign IS NULL OR polarity_sign IN (-1, 1)
    ),
    CONSTRAINT claim_contentions_refs_is_array CHECK (
        jsonb_typeof(refs) = 'array'
    ),
    -- A stance that names a fetched page MUST have one. The row cannot claim a
    -- contradiction it cannot show: this is the schema-level half of "a
    -- counter-ref the platform cites is a page it fetched".
    CONSTRAINT claim_contentions_evidence_present CHECK (
        stance NOT IN ('contradicts', 'qualifies')
        OR jsonb_array_length(refs) > 0
    ),
    -- ...and only those two stances may carry a stance-bearing derivation, so
    -- `derivation` can never be read as evidence on a row that found none.
    CONSTRAINT claim_contentions_derivation_pair CHECK (
        (derivation <> 'none') = (stance IN ('contradicts', 'qualifies'))
    ),
    CONSTRAINT claim_contentions_window CHECK (expires_at >= as_of)
);

-- The replay/idempotency key. See §4: one record per claim per pass version per
-- UTC day.
CREATE UNIQUE INDEX IF NOT EXISTS claim_contentions_claim_day_unique
    ON public.claim_contentions USING btree (claim_id, pipeline_version, as_of_day);

-- The Inspector chip's drill: every record for one claim, newest first.
CREATE INDEX IF NOT EXISTS claim_contentions_claim_idx
    ON public.claim_contentions USING btree (claim_id, as_of DESC);

-- `GET /v3/contentions?scope=<desk>` — one desk's records inside a window.
CREATE INDEX IF NOT EXISTS claim_contentions_desk_idx
    ON public.claim_contentions USING btree (desk_key, as_of DESC);

-- The COMPOSITION TENSION read, and the reason it is partial. The composition
-- asks one narrow question — "does any claim I am carrying have a live,
-- polarity-derived contradiction?" — over a handful of claim keys. A partial
-- index on exactly that predicate keeps the index the size of the contradicted
-- population (a small minority of rows, by design) rather than the size of the
-- table, and keeps the composition's per-cycle read off the `none_found` bulk
-- that is, by design, most of it.
CREATE INDEX IF NOT EXISTS claim_contentions_contradicts_idx
    ON public.claim_contentions USING btree (claim_id, as_of DESC)
    WHERE stance = 'contradicts' AND derivation = 'polarity';

-- The Inspector chip's own join: every record written against one published
-- read, by the ordinal the reader can see.
CREATE INDEX IF NOT EXISTS claim_contentions_finding_idx
    ON public.claim_contentions USING btree (finding_id, block_ordinal, as_of DESC);

COMMENT ON TABLE public.claim_contentions IS
    'Program 7a — the contrary-evidence pass. One RETRIEVAL OUTCOME per '
    'material claim: the counter-query that was issued, the rung it ran on, '
    'the pages that were actually fetched, and a deterministically derived '
    'stance. Never a verdict on the claim.';
COMMENT ON COLUMN public.claim_contentions.claim_id IS
    'The audit''s own claim key — sha256 over the folded claim text and its '
    'byte-exact origin span (_external_audit_claims.claim_key). Joins '
    'external_grades.claim_key.';
COMMENT ON COLUMN public.claim_contentions.origin_head_id IS
    'The desk head the claim''s span was QUOTED FROM. finding_id is the read '
    'it was published IN; block_ordinal is its [[ref:N]] inside that read.';
COMMENT ON COLUMN public.claim_contentions.block_ordinal IS
    'The claim''s [[ref:N]] ordinal inside finding_id. The Inspector chip '
    'joins on (finding_id, block_ordinal); the composition tension rule joins '
    'on claim_id, which is stable across cycles.';
COMMENT ON COLUMN public.claim_contentions.stance IS
    'The RETRIEVAL''s outcome, set in code from what was fetched against the '
    'claim''s polarity: contradicts | qualifies | none_found | search_failed. '
    'Not a judgement of the claim.';
COMMENT ON COLUMN public.claim_contentions.derivation IS
    'Which deterministic rule produced the stance. Only ''polarity'' (the R2 '
    'calibrated vocabulary) is admitted by the composition tension rule; '
    '''negation'' is the uncalibrated fallback and is human-surfaces-only.';
COMMENT ON COLUMN public.claim_contentions.query_source IS
    '''polarity'' = the counter-query was built by negating the claim''s '
    'polarity group; ''model'' = one bounded core-plane call proposed it and '
    'code re-validated it against the paraphrase guard.';
COMMENT ON COLUMN public.claim_contentions.query_novel_tokens IS
    'How many content tokens the counter-query carries that the claim does '
    'not. Zero is a paraphrase and is refused before the query is issued; the '
    'column is what makes counter-query quality measurable after the fact.';
COMMENT ON COLUMN public.claim_contentions.refs IS
    'The pages actually FETCHED through the web_access pack and its fences: '
    '{url, sha256, chars, published_at, extracted, fetched_at, status_code, '
    'stance, quote}. A search snippet never becomes a ref.';
COMMENT ON COLUMN public.claim_contentions.ref_signal_ids IS
    'signals.id for any fetched page whose content hash already matched a '
    'landed row. This pass LINKS and never writes signals — see the migration '
    'header §3.';
COMMENT ON COLUMN public.claim_contentions.as_of IS
    'The moment the row speaks about (the pass''s clock for this claim). '
    'retrieved_at is when the pages were fetched; expires_at is when the next '
    'pass would have covered the claim.';
COMMENT ON COLUMN public.claim_contentions.receipt_id IS
    'analyst_traces.run_id of the pass that wrote this row — the join to the '
    'receipt_hash / prev_receipt_hash chain. There is no other receipt_id '
    'convention in this tree; this column states its join rather than '
    'implying one.';
