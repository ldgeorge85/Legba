-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0190_external_grades.sql
--
-- THE PLATFORM'S FIRST DB HOME FOR EXTERNAL TRUTH
-- (planning/EXTERNAL_GRADING_WIDTH_DESIGN_2026-09-05.md §3.1, decision 0.9).
--
-- ~84% of this fleet's LLM calls are the system grading itself: faithfulness
-- judges scoring our prose against our own citations, calibration trackers
-- scoring our own bands, lineage sweeps walking our own derived_from. Every one
-- is a CONSISTENCY check and not one can see a tower that is internally
-- immaculate and factually WRONG about the world. The only thing that has ever
-- produced TRUTH about the reads is an external, web-grounded round run four
-- times BY HAND — and those grades live in `planning/PROOF_ROUND_*/scoring/
-- *.jsonl`, outside the database entirely.
--
-- The one table that looked like a home is not one: `correctness_labels` holds
-- EIGHT ROWS ALL-TIME, every one written in a single backfill batch at
-- 2026-07-28 08:36:19, and `unit_reference_labels` holds one row, for a RETIRED
-- analyst, with an empty `canonical_source_ids`. This migration closes that gap
-- for the STANDING number and deliberately leaves the frozen rounds where they
-- are: `round_lineage.py` stays DB-less and pinned, because five human-graded
-- numbers per round are machinery-plus-constants, not a population.
--
-- One row = "at <graded_at>, rater <rater_role> from family <grader_family>,
-- running <grader_pipeline_version> under rubric <rubric_version>, judged claim
-- <claim_key> — a byte-identified span of read <graded_output_id> cut from desk
-- head <origin_head_id> — to be <verdict>, on the strength of <decisive_span>
-- at <decisive_url>".
--
-- ── WHY THE CLAIM KEY IS THE IDENTITY, AND WHAT IT IS ──────────────────────
-- `claim_key = sha256(fold(text) || origin_head_id || start || end)`. Under
-- `assembly.v1` a read is a list of VERBATIM quoted spans with byte-exact
-- origins, so the claim population is a FIELD in the payload rather than
-- something a model has to extract — and the key is therefore stable across a
-- replay a month later. That is not an elegance argument. It is what a disputed
-- CONTRADICTED verdict needs in order to be re-argued: the same date resolves
-- the same head, the same offsets, the same key, the same row.
--
-- The fold is the plane's ONE normalisation site (`data/provenance/
-- text_fold.py`), so a U+2011 the renderer inserted between two replays cannot
-- mint a second key for the same claim. That is the MECH-6 class — which moved
-- 58.2% of claims under one stamp — applied to IDENTITY rather than to a
-- comparator.
--
-- ── THREE POPULATIONS, NEVER POOLED ───────────────────────────────────────
--   * `assembly_span`        — the RECORD. A verbatim desk sentence. Grading it
--                              grades the DESKS: the assembly authors nothing
--                              and its construction error is 1.0 by arithmetic.
--                              Anyone reading this number as "the composition
--                              tier's accuracy" is reading it wrong.
--   * `assessment_sentence`  — the VOICE. The one composition-tier surface
--                              still making claims of its own after D-6.
--   * `legacy_prose`         — the flag-off / pre-flip arm, whose claims come
--                              off the LLM extraction leg.
-- They are three different acts of authorship and are never summed into one
-- headline. The CHECK below makes a fourth population unrepresentable rather
-- than merely discouraged.
--
-- ── HONESTY FIELDS ────────────────────────────────────────────────────────
--   * `verdict` carries FIVE values and only three of them score. NOT_FOUND is
--     a statement about the SEARCH, never about the world; UNCHECKED means the
--     search plane did not answer; UNCHECKABLE is decided BEFORE any query,
--     deterministically, for claims with no world truth-maker. All three are
--     excluded from numerator AND denominator, exactly the way
--     `correctness_axis.WEIGHTS` has no entry for `unresolvable`.
--   * `sample_fraction` — a partial day is never published as a whole one. When
--     the day's budget cannot cover the population the loop DEGRADES TO SAMPLED
--     mode, hash-gated and replayable, and the fraction is stamped here, on the
--     heartbeat, and on every published number.
--   * `search_degraded` / `search_liveness` / `search_provider` — the web is not
--     a constant. A week when SearXNG lost eight engines is not a week the world
--     got quieter, and the split key must split on the condition that changed.
--   * `decisive_source_tier` NULL on a decisive verdict is the VISIBLE
--     `tier_unknown` class (ruled): the verdict stands and is counted in its own
--     class rather than silently suppressed. R2-C4 case #10 was overturned
--     because `score_r2.py` never read `source_tier` at all; at width that is
--     470 chances a day to repeat it.
--   * `span_role` + `block_ordinal` — a false BLUF and a false footnote score
--     identically here, which is a real objection. Both fields ride every row so
--     a position-weighted number can be computed LATER, against a registered
--     rule, rather than headlined now on one somebody invented.
--   * `archive_ref` points into the GRADING archive, which is its own
--     content-addressed object path and is NEVER a `signal_id`. `evidence_
--     archive` (70,836 rows) is keyed on `signal_id`, and routing grading pages
--     through it would put the grader's own evidence into the desks' slices and
--     close the exact loop this program exists to open.
--   * `assembly_regime` is the A/B ARM and is NOT NULL with a `legacy` default,
--     so the arm is a closed vocabulary you can GROUP BY rather than a nullable
--     field that silently pools the two regimes.
--
-- ── APPEND-ONLY, SCHEMA-ENFORCED (the 0107/0184/0189 discipline) ───────────
-- Both mutation paths fail loud at the database. The reason is sharper here
-- than for the analytic ledgers: this table is the platform's claim to be
-- externally checkable, and the party it grades owns the database. A
-- measurement the measured party can quietly edit is not a measurement. A
-- superseding grade is a NEW ROW under a new `grader_pipeline_version` — which
-- is why that column is in the uniqueness key and why pooling across a bump is
-- forbidden in code as well as in prose.
--
-- ── THE UNIQUE, AND WHY THE FAMILY IS IN IT ───────────────────────────────
-- `UNIQUE (claim_key, grader_pipeline_version, grader_family)`. One row per
-- (claim, pipeline, family). A double-graded claim yields TWO rows differing
-- only in `grader_family` and `rater_role` — that is the instrument-validity
-- sample, and collapsing it into one row would delete the only evidence that
-- two families were ever asked. Re-running a tick is therefore idempotent: the
-- writer's ON CONFLICT DO NOTHING lands on this constraint.
--
-- ── SAFETY ────────────────────────────────────────────────────────────────
-- Additive only. CREATE TABLE IF NOT EXISTS, CREATE INDEX IF NOT EXISTS,
-- CREATE OR REPLACE FUNCTION, and the standard DROP TRIGGER IF EXISTS + CREATE
-- TRIGGER idiom. No existing table is touched, no existing row is read, and no
-- existing behaviour changes: nothing writes here until
-- `LEGBA_EXTERNAL_GRADING_WIDTH` is on, so applying this migration to a
-- flag-off deployment is a pure schema addition. Re-apply and cold-start are
-- both safe under the runner that re-globs every file. No inline BEGIN/COMMIT —
-- the runner owns the transaction.

CREATE TABLE IF NOT EXISTS public.external_grades (
    id                       uuid PRIMARY KEY DEFAULT gen_random_uuid(),

    -- identity: replayable a month later from the date alone -----------------
    -- sha256(fold(text) || origin_head_id || start || end). See the header.
    claim_key                text NOT NULL
                             CONSTRAINT external_grades_claim_key_nonempty
                             CHECK (btrim(claim_key) <> ''),
    population               text NOT NULL
                             CONSTRAINT external_grades_population_vocab
                             CHECK (population IN (
                                 'assembly_span',
                                 'assessment_sentence',
                                 'legacy_prose'
                             )),
    -- The READ that published the claim.
    graded_output_id         uuid NOT NULL,
    -- The DESK head the span was cut from (D-1 §0.3's depth-1 rule). NULL off
    -- the assembly, where the read is its own origin.
    origin_head_id           uuid,
    block_ordinal            integer,
    -- bluf | what_changed | body | indicator | sentence
    span_role                text,
    analyst_id               text NOT NULL,
    target_id                text,
    desk_key                 text,

    -- the claim -------------------------------------------------------------
    claim_text               text NOT NULL,
    claim_severity           text,
    -- THE A/B ARM. Closed and NOT NULL so a GROUP BY cannot silently pool.
    assembly_regime          text NOT NULL DEFAULT 'legacy',
    scope_bounded            boolean NOT NULL DEFAULT false,
    absence_shaped           boolean NOT NULL DEFAULT false,

    -- the world -------------------------------------------------------------
    verdict                  text NOT NULL
                             CONSTRAINT external_grades_verdict_vocab
                             CHECK (verdict IN (
                                 'SUPPORTED',
                                 'CONTRADICTED',
                                 'NOT_FOUND',
                                 'UNCHECKED',
                                 'UNCHECKABLE'
                             )),
    uncheckable_class        text
                             CONSTRAINT external_grades_uncheckable_vocab
                             CHECK (uncheckable_class IS NULL
                                    OR uncheckable_class IN (
                                        'scope_bounded',
                                        'provenance',
                                        'perspective'
                                    )),
    unchecked_reason         text,
    decisive_url             text,
    decisive_span            text,
    decisive_span_sha256     text,
    -- NULL on a decisive verdict IS the visible `tier_unknown` class.
    decisive_source_tier     integer,
    decisive_published_at    timestamptz,
    -- The GRADING archive's own object path. NEVER a signal_id — see header.
    archive_ref              text,
    source_urls              text[] NOT NULL DEFAULT '{}',
    search_provider          text,
    search_status            text,
    search_liveness          text,
    search_degraded          boolean NOT NULL DEFAULT false,

    -- the grader ------------------------------------------------------------
    grader_family            text NOT NULL
                             CONSTRAINT external_grades_grader_family_nonempty
                             CHECK (btrim(grader_family) <> ''),
    grader_component_id      text NOT NULL
                             CONSTRAINT external_grades_grader_component_nonempty
                             CHECK (btrim(grader_component_id) <> ''),
    grader_model_name        text,
    -- WHO SERVED IT. D5_VERDICT F4 measured the serving provider flipping 13.6%
    -- of pass/fail verdicts between two endpoints of the SAME model id.
    grader_served_by         text,
    grader_pipeline_version  text NOT NULL
                             CONSTRAINT external_grades_pipeline_nonempty
                             CHECK (btrim(grader_pipeline_version) <> ''),
    rubric_version           text NOT NULL
                             CONSTRAINT external_grades_rubric_nonempty
                             CHECK (btrim(rubric_version) <> ''),
    rater_role               text NOT NULL
                             CONSTRAINT external_grades_rater_role_vocab
                             CHECK (rater_role IN ('primary', 'audit')),

    -- stratification --------------------------------------------------------
    -- The read's cited signals by origin. RECORDED, never shown to the grader
    -- (G-5): handing it the read's own [N] sources would collapse this
    -- instrument into faithfulness. Honest-null today — `retrieval_origin` is
    -- NULL on all 23,774 signals fetched in the last 7 days.
    retrieval_origin_mix     jsonb NOT NULL DEFAULT '{}',
    -- The read's OWN admissible source window (G-3), never the grader's guess.
    read_evidence_window     jsonb NOT NULL DEFAULT '{}',
    -- 1.0 = the whole day. Anything less is a DEGRADED day and says so.
    sample_fraction          numeric(5,4) NOT NULL DEFAULT 1.0
                             CONSTRAINT external_grades_sample_fraction_unit
                             CHECK (sample_fraction > 0 AND sample_fraction <= 1),
    graded_at                timestamptz NOT NULL DEFAULT now(),
    derived_from             uuid[] NOT NULL DEFAULT '{}',

    -- An uncheckable_class without the UNCHECKABLE verdict (or the reverse) is
    -- half a fact, so it is unrepresentable rather than merely discouraged.
    CONSTRAINT external_grades_uncheckable_pair
        CHECK ((verdict = 'UNCHECKABLE') = (uncheckable_class IS NOT NULL)),

    -- One row per (claim, pipeline, family). The family is in the key because a
    -- double-graded claim is TWO rows and collapsing them would delete the only
    -- evidence that two families were ever asked.
    CONSTRAINT external_grades_claim_rater_unique
        UNIQUE (claim_key, grader_pipeline_version, grader_family)
);

-- THE HEADLINE QUERY: "the last 7 days, by population, by verdict" — which is
-- every published number's shape.
CREATE INDEX IF NOT EXISTS idx_external_grades_window
    ON public.external_grades (graded_at DESC, population, verdict);

-- The A/B arm and the per-desk roll-up.
CREATE INDEX IF NOT EXISTS idx_external_grades_regime
    ON public.external_grades (assembly_regime, graded_at DESC);

-- "Which read did this grade come from?" — the one-critique-per-read join and
-- the reads-API lineage walk.
CREATE INDEX IF NOT EXISTS idx_external_grades_output
    ON public.external_grades (graded_output_id, graded_at DESC);

-- The instrument-validity pass: the double-graded claims, found by claim rather
-- than scanned for. Partial to the audit rater, which is ~10% of the table.
CREATE INDEX IF NOT EXISTS idx_external_grades_audit_rater
    ON public.external_grades (claim_key)
    WHERE rater_role = 'audit';

-- Every CONTRADICTED, individually — they are rare by construction and each one
-- is an operator event.
CREATE INDEX IF NOT EXISTS idx_external_grades_contradicted
    ON public.external_grades (graded_at DESC)
    WHERE verdict = 'CONTRADICTED';

-- Schema-enforced append-only posture. See the header: a measurement the
-- measured party can quietly edit is not a measurement.
CREATE OR REPLACE FUNCTION public.external_grades_forbid_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION
        'external_grades rows are never updated or deleted — the external '
        'truth ledger is append-only; a superseding grade is a NEW ROW under a '
        'new grader_pipeline_version, so the population that produced any '
        'published number stays reconstructible';
END;
$$;

DROP TRIGGER IF EXISTS trg_external_grades_forbid_delete
    ON public.external_grades;
CREATE TRIGGER trg_external_grades_forbid_delete
    BEFORE DELETE ON public.external_grades
    FOR EACH ROW EXECUTE FUNCTION public.external_grades_forbid_mutation();

DROP TRIGGER IF EXISTS trg_external_grades_forbid_update
    ON public.external_grades;
CREATE TRIGGER trg_external_grades_forbid_update
    BEFORE UPDATE ON public.external_grades
    FOR EACH ROW EXECUTE FUNCTION public.external_grades_forbid_mutation();
