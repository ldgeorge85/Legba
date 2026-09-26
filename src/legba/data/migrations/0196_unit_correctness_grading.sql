-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0196_unit_correctness_grading.sql
--
-- THE CORRECTNESS INSTRUMENT GETS A HOME IN THE DATABASE
-- (LEDGER_RESET_2026-09-16 §3, Program 2 tracks R2/G1/G3).
--
-- Program 1 proved by hand, on 2026-09-16, that three grader families reading
-- ANNEX C v4 agree with each other (pooled 0.8444 on a fresh 30-atom draw,
-- every pair above the 0.70 floor — VERDICT_P1v4.md) and then pointed that
-- instrument at ONE live country: Israel, 54 claims, 8 desks + the composition.
-- It found two false published claims. All of it lived in
-- `planning/PROGRAM1_2026-09-16/`, outside the database, run by hand.
--
-- These four tables are where that stops being a hand-run.
--
-- ── WHY NEW TABLES AND NOT `unit_reference_labels` (0057/0191) ──────────────
-- REUSE-BEFORE-CREATE was applied and it says NO, for a reason of GRAIN rather
-- than taste. `unit_reference_labels` holds A-1's ATTENTION reference: <=5 items
-- for ONE (unit, target) for ONE UTC DAY, built from one web_search's first
-- page, whose question is "did the desk ENGAGE what its own slice carried". The
-- reference this migration stores answers a different question over a different
-- window: 30-odd DEVELOPMENTS across 8 DIMENSIONS over a 14-DAY window, each
-- with a decisive span, an outlet and a date, built blind to the substrate —
-- R3's shape, the one the correctness rubric grades against. One row per
-- country per window, not per unit per day. Forcing the second onto the first's
-- unique index (unit_analyst_id, target_id, window_start) would mean either
-- inventing a unit for a country-level object or writing the same reference
-- eight times. And pooling them is worse than clumsy: `attention_rate` and
-- `correctness_share` are different measurements, and a table whose rows mean
-- two things is a table someone eventually averages.
--
-- ── WHAT EACH TABLE IS ─────────────────────────────────────────────────────
--
--   unit_references        one INDEPENDENT reference: a country, a window, the
--                          developments a blind builder found. Track R2 writes
--                          these; G1 only READS them. A reference is an INPUT
--                          to the measurement and is never derived from a read.
--
--   unit_correctness       one row = "at <as_of>, unit <analyst_id> on target
--                          <target_id>, in head <head_id>, graded against
--                          reference <reference_id> under rubric <rubric_sha>,
--                          made <n_claims> claims of which <n_contains> the
--                          reference bore out and <n_contradicts> it
--                          contradicted".
--
--   unit_correctness_claims  the per-claim ledger under it — every claim's
--                          text, every family's label, the adjudicated
--                          outcome, and the decisive spans. A share nobody can
--                          re-argue claim by claim is a share nobody should
--                          act on; this is the re-argument surface.
--
--   grader_calibrations    the GATE. One row = "on <created_at>, models
--                          <model_ids> reading rubric <rubric_sha> over packet
--                          <packet_sha> agreed at pooled <pooled> / pairwise
--                          <pairwise>, and that <gate_pass>ed". The grader
--                          REFUSES to run without a passing row covering its
--                          own rubric and models — G3's re-gate, enforced in
--                          code (`_correctness_calibration.py`) rather than
--                          remembered.
--
-- ── THE SHARES ARE NULLABLE, AND THAT IS THE POINT ─────────────────────────
-- `correctness_share` is NULL — never 0.0 — when the reference bore on nothing
-- the unit said. Zero means "every decided claim was contradicted"; NULL means
-- "nothing was decided". A NOT NULL DEFAULT 0 here would have turned the eight
-- desks whose reference touched nothing into eight desks measured wrong, which
-- is the single most consequential column decision in this file. Both shares
-- travel with the n they rest on (`n_claims`, and contains+contradicts implied
-- by the counts) so a reader never has to reconstruct a denominator.
--
-- ── IDEMPOTENCE ────────────────────────────────────────────────────────────
-- `uq_unit_correctness_run` on (analyst_id, target_id, head_id, reference_id,
-- rubric_sha) makes a re-run a SCHEMA-level no-op rather than a handler
-- convention: the same head, graded against the same reference under the same
-- rubric, is the same measurement, and writing it twice would double a
-- population somebody later takes a mean over. The handler reads the table
-- first so a re-run also does not BURN the paid calls it would then discard.
--
-- SAFETY (idempotent, additive, forward-only): every statement is IF NOT
-- EXISTS. The runner wraps this file in its own transaction and records it in
-- `legba_data_migrations` (no inline BEGIN/COMMIT here).

-- ---------------------------------------------------------------------------
-- 1. THE REFERENCE (track R2 writes; G1 reads)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS public.unit_references (
    id                  uuid PRIMARY KEY,
    target_id           text        NOT NULL,
    window_start        timestamptz NOT NULL,
    window_end          timestamptz NOT NULL,
    built_at            timestamptz NOT NULL,
    builder             text        NOT NULL,
    ref_json            jsonb       NOT NULL,
    span_verified_rate  numeric,
    thin_dimensions     text[]      NOT NULL DEFAULT '{}',
    sha256              text        NOT NULL,
    created_at          timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_unit_references_window
        CHECK (window_end > window_start),
    CONSTRAINT ck_unit_references_span_rate
        CHECK (span_verified_rate IS NULL
               OR (span_verified_rate >= 0 AND span_verified_rate <= 1)),
    CONSTRAINT ck_unit_references_sha
        CHECK (sha256 ~ '^[0-9a-f]{64}$')
);

COMMENT ON TABLE public.unit_references IS
    'G1/R2: one INDEPENDENT reference per (country target, window) — R3''s '
    'shape: ref_developments[] (summary, decisive_span, outlet, publish_date) '
    'plus a band per dimension, built BLIND to the substrate. The correctness '
    'grader reads these and never writes one: a reference derived from the '
    'read it grades is not a reference.';
COMMENT ON COLUMN public.unit_references.builder IS
    'Who built it, and how — e.g. ''opus-web-lane'' (route A), '
    '''core-plane-searxng'' (route B), ''operator'' (route C). The route is '
    'part of the number''s provenance: a reference the core plane built is a '
    'different instrument from one an out-of-plane model built.';
COMMENT ON COLUMN public.unit_references.span_verified_rate IS
    'Share of ref_developments whose decisive_span was verified verbatim '
    'against the archived page text at build time. NULL = the builder did not '
    'report one, which is not the same as zero.';
COMMENT ON COLUMN public.unit_references.thin_dimensions IS
    'Dimensions carrying fewer than two developments. A thin dimension makes '
    'every claim on it structurally likelier to read `silent`, so coverage on '
    'it must be read with this array in hand.';
COMMENT ON COLUMN public.unit_references.sha256 IS
    'Digest of the reference document as loaded. It is the join between a '
    'published share and the exact evidence it was computed against.';

-- ONE row per (target, reference document). A reload of the same file is a
-- no-op rather than a second population member.
CREATE UNIQUE INDEX IF NOT EXISTS uq_unit_references_target_sha
    ON public.unit_references (target_id, sha256);

-- The grader's own lookup: the reference for this target whose window CONTAINS
-- the as-of stamp, newest build wins.
CREATE INDEX IF NOT EXISTS idx_unit_references_target_window
    ON public.unit_references (target_id, window_start DESC, window_end DESC);

-- ---------------------------------------------------------------------------
-- 2. THE PER-UNIT NUMBER
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS public.unit_correctness (
    id                 uuid PRIMARY KEY,
    analyst_id         text        NOT NULL,
    target_id          text        NOT NULL,
    head_id            uuid        NOT NULL,
    as_of              timestamptz NOT NULL,
    reference_id       uuid        NOT NULL
        REFERENCES public.unit_references (id) ON DELETE RESTRICT,
    rubric_sha         text        NOT NULL,
    grain              text        NOT NULL DEFAULT 'desk',
    n_claims           integer     NOT NULL,
    n_contains         integer     NOT NULL DEFAULT 0,
    n_contradicts      integer     NOT NULL DEFAULT 0,
    n_silent           integer     NOT NULL DEFAULT 0,
    n_split            integer     NOT NULL DEFAULT 0,
    n_unparseable      integer     NOT NULL DEFAULT 0,
    n_single_family    integer     NOT NULL DEFAULT 0,
    correctness_share  numeric,
    coverage_share     numeric,
    families           jsonb       NOT NULL DEFAULT '{}'::jsonb,
    cost_usd           numeric     NOT NULL DEFAULT 0,
    created_at         timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_unit_correctness_grain
        CHECK (grain IN ('desk', 'composition')),
    CONSTRAINT ck_unit_correctness_counts
        CHECK (n_claims >= 0 AND n_contains >= 0 AND n_contradicts >= 0
               AND n_silent >= 0 AND n_split >= 0 AND n_unparseable >= 0
               AND n_single_family >= 0
               AND n_contains + n_contradicts + n_silent + n_split
                   + n_unparseable = n_claims),
    CONSTRAINT ck_unit_correctness_shares
        CHECK ((correctness_share IS NULL
                OR (correctness_share >= 0 AND correctness_share <= 1))
               AND (coverage_share IS NULL
                    OR (coverage_share >= 0 AND coverage_share <= 1))),
    CONSTRAINT ck_unit_correctness_cost
        CHECK (cost_usd >= 0),
    CONSTRAINT ck_unit_correctness_rubric_sha
        CHECK (rubric_sha ~ '^[0-9a-f]{64}$')
);

COMMENT ON TABLE public.unit_correctness IS
    'G1: one bounded unit''s correctness against an independent reference, at '
    'one as-of stamp. correctness_share = contains/(contains+contradicts); '
    'coverage_share = (contains+contradicts)/n_claims. NEITHER is readable '
    'without the other.';
COMMENT ON COLUMN public.unit_correctness.correctness_share IS
    'NULL — never 0.0 — when the reference bore on nothing the unit said. Zero '
    'means every decided claim was contradicted; NULL means nothing was '
    'decided. The difference is the whole measurement.';
COMMENT ON COLUMN public.unit_correctness.coverage_share IS
    'How much of what the unit said the reference bears on AT ALL. A '
    'correctness share of 1.0 at a coverage of 0.05 is one claim confirmed and '
    'nineteen the reference never touched.';
COMMENT ON COLUMN public.unit_correctness.n_split IS
    'Claims where no two families agreed. Counted in the coverage denominator '
    'and in NEITHER numerator — never broken by a tie-break rule, never '
    'quietly dropped (LEDGER_RESET §3).';
COMMENT ON COLUMN public.unit_correctness.n_single_family IS
    'Claims carrying ONE family''s label — every claim when the daily ceiling '
    'is $0, and every `silent` claim under the cost triage even when it is '
    'not. A single-family label is a real label and a weaker one; this count '
    'is what makes that visible rather than averaged away.';
COMMENT ON COLUMN public.unit_correctness.families IS
    '{"F0": {"model": ..., "n_calls": ..., "cost_usd": ..., "labels": {...}}, '
    '...} plus "single_family" and the ceiling in force. The per-family label '
    'distribution is kept UNPOOLED so a family that read the unit differently '
    'is visible.';
COMMENT ON COLUMN public.unit_correctness.cost_usd IS
    'What this unit''s grading actually cost, from the handlers'' own usage. '
    'F0 is the $0 core plane; F2/F3 are OpenRouter and are governed by '
    'LEGBA_GRADER_DAILY_CEILING_USD (default 0 = never called).';

-- IDEMPOTENCE. Same head, same reference, same rubric = the same measurement.
CREATE UNIQUE INDEX IF NOT EXISTS uq_unit_correctness_run
    ON public.unit_correctness
       (analyst_id, target_id, head_id, reference_id, rubric_sha);

-- The read surface: this target's numbers, newest first.
CREATE INDEX IF NOT EXISTS idx_unit_correctness_target_as_of
    ON public.unit_correctness (target_id, as_of DESC);

-- The per-unit history: one desk's number over time.
CREATE INDEX IF NOT EXISTS idx_unit_correctness_analyst_target
    ON public.unit_correctness (analyst_id, target_id);

-- The daily-ceiling read: what has this instrument spent today.
CREATE INDEX IF NOT EXISTS idx_unit_correctness_created_at
    ON public.unit_correctness (created_at DESC);

-- ---------------------------------------------------------------------------
-- 3. THE PER-CLAIM LEDGER
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS public.unit_correctness_claims (
    id               uuid PRIMARY KEY,
    correctness_id   uuid NOT NULL
        REFERENCES public.unit_correctness (id) ON DELETE CASCADE,
    claim_id         text NOT NULL,
    grain            text NOT NULL,
    claim_text       text NOT NULL,
    label_by_family  jsonb NOT NULL DEFAULT '{}'::jsonb,
    adjudicated      text NOT NULL,
    n_families       smallint NOT NULL DEFAULT 0,
    single_family    boolean NOT NULL DEFAULT false,
    spans            jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at       timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_unit_correctness_claims_grain
        CHECK (grain IN ('desk', 'composition')),
    -- A CLOSED vocabulary. `split` and `unparseable` are outcomes, not labels,
    -- and a fourth label is unrepresentable rather than merely discouraged.
    CONSTRAINT ck_unit_correctness_claims_adjudicated
        CHECK (adjudicated IN ('contains', 'contradicts', 'silent', 'split',
                               'unparseable')),
    CONSTRAINT ck_unit_correctness_claims_families
        CHECK (n_families >= 0 AND n_families <= 8)
);

COMMENT ON TABLE public.unit_correctness_claims IS
    'G1: the per-claim ledger under one unit_correctness row — the surface on '
    'which a disputed share is re-argued claim by claim.';
COMMENT ON COLUMN public.unit_correctness_claims.claim_id IS
    'DR-<8hex> for a desk claim, CR-<8hex> for a composition claim: '
    'sha256(analyst_id|created_at|norm(text))[:8]. DISTINCT namespaces so the '
    'two grains can never pool in a reader that keys on the id alone.';
COMMENT ON COLUMN public.unit_correctness_claims.label_by_family IS
    '{"F0": "contains", "F2": "silent", ...} — every family that answered, '
    'UNPOOLED. An absent family did not answer; it did not agree.';
COMMENT ON COLUMN public.unit_correctness_claims.spans IS
    '{"F0": {"decisive_span": ..., "core_claim": ..., "reason": ..., '
    '"span_unverified": bool, "span_failure": ...}, ...}. A span that failed '
    'the verbatim check leaves the LABEL STANDING and is flagged — the label '
    'is what the scorer gates on, and a span quibble must never cost a label '
    'the grader actually gave (PREREG_P1 Amendment 2).';

CREATE UNIQUE INDEX IF NOT EXISTS uq_unit_correctness_claims_claim
    ON public.unit_correctness_claims (correctness_id, claim_id);

CREATE INDEX IF NOT EXISTS idx_unit_correctness_claims_adjudicated
    ON public.unit_correctness_claims (adjudicated, grain);

-- ---------------------------------------------------------------------------
-- 4. THE GATE (G3)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS public.grader_calibrations (
    id            uuid PRIMARY KEY,
    rubric_sha    text        NOT NULL,
    model_ids     jsonb       NOT NULL,
    pooled        numeric,
    pairwise      jsonb       NOT NULL DEFAULT '[]'::jsonb,
    gate_pass     boolean     NOT NULL,
    packet_sha    text        NOT NULL,
    n_atoms       integer,
    pooled_bar    numeric     NOT NULL DEFAULT 0.75,
    pairwise_bar  numeric     NOT NULL DEFAULT 0.70,
    notes         text,
    created_at    timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_grader_calibrations_pooled
        CHECK (pooled IS NULL OR (pooled >= 0 AND pooled <= 1)),
    CONSTRAINT ck_grader_calibrations_rubric_sha
        CHECK (rubric_sha ~ '^[0-9a-f]{64}$'),
    -- A PASS with no measured pooled rate is not a pass. An empty-overlap
    -- calibration is UNMEASURED, and that must be unrepresentable as a gate.
    CONSTRAINT ck_grader_calibrations_pass_needs_a_number
        CHECK (NOT gate_pass OR pooled IS NOT NULL)
);

COMMENT ON TABLE public.grader_calibrations IS
    'G3: one calibration run. The grader refuses to publish a number unless a '
    'gate_pass row exists whose rubric_sha matches and whose model_ids cover '
    'every model the run will use. Re-gate on a FRESH draw whenever the '
    'rubric, a model id or the claim grain changes.';
COMMENT ON COLUMN public.grader_calibrations.model_ids IS
    '{"F0": "gpt-oss-120b", "F2": "meta-llama/llama-3.3-70b-instruct", ...}. '
    'The coverage test is a SUPERSET test: a run may use a SUBSET of the '
    'calibrated models (F0 alone at a $0 ceiling is inside what was measured), '
    'never a model the gate never saw.';
COMMENT ON COLUMN public.grader_calibrations.packet_sha IS
    'The byte-identical packet the families were graded on. Two calibrations '
    'over the same packet are the same draw; a re-gate must move this.';

CREATE INDEX IF NOT EXISTS idx_grader_calibrations_lookup
    ON public.grader_calibrations (rubric_sha, gate_pass, created_at DESC);

-- A given (rubric, packet, model set) is ONE calibration. Re-running the same
-- draw and writing a second row would let a second roll of the same dice look
-- like independent confirmation.
CREATE UNIQUE INDEX IF NOT EXISTS uq_grader_calibrations_draw
    ON public.grader_calibrations (rubric_sha, packet_sha, model_ids);
