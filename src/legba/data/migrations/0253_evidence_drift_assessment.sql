-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0253_evidence_drift_assessment.sql
--
-- D-2: the drift ASSESSMENT — what kind of change a classified page change
-- was, written once per change and reused by every unit that cites the page
-- (legba.data.evidence_drift_assess / _assess_model / _assess_store). Fills
-- D-1's CLAIM_JUDGE_SEAM (SEAMS row 79).
--
--   * `evidence_drift_assessments` — one row per (classification, assessor
--     stamp): the change type from a granular closed list (`mixed` carries
--     its components), acknowledged (correction_notice | update_stamp |
--     silent), the scope (before -> after pairs, claims changed, share
--     changed), the timing (hours from publication or first read, bucketed),
--     a one-line summary — all OBSERVED — and `history_rewrite`, DERIVED in
--     code only, with the inputs that produced it. Attached to the CHANGE,
--     never to a dependant. INSERT-ONLY; versioned by the stamp (rules
--     version | prompt version | model id, or `|code_only`). The TYPE is
--     set by code alone; acknowledged is the highest of four levels
--     (correction_notice > visible_update_stamp > metadata_date_only >
--     silent) with the signals that fired; a second derived label,
--     `late_unexplained_change`; the model writes only the summary and may
--     store a verified `model_type_doubt` beside the code type.
--   * `evidence_drift_claim_effects` — per flag and assessment, whether the
--     cited text survives / is gone / changed (with the after-text) / is not
--     checkable, by CODE; the model's answer for the not-checkable case sits
--     in its own columns and never overwrites the code result. INSERT-ONLY.
--   * `evidence_drift_auto_closes` — the tracked auto-closes: which flag, on
--     which assessment and claim effect, under which rule version, by which
--     system principal. INSERT-ONLY.
--   * `evidence_drift_would_auto_close` — the SHADOW count: every flag the
--     auto-close rule WOULD close, recorded whether or not the switch is on,
--     per rule version. Never changes the flag. INSERT-ONLY.
--   * RE-OPEN: D-1's flag trigger refuses backward moves and is NOT weakened.
--     A re-opened flag is a NEW flag row carrying `reopened_from` (the
--     resolved flag), `reopened_by` and `reopen_note`; the "one flag per
--     (drift row, dependant)" uniqueness becomes "one ORIGINAL flag per
--     (drift row, dependant)" plus "one re-open per flag". The guard function
--     is replaced only to add the three new columns to the never-change set.
--
-- SAFETY (idempotent, additive, forward-only): four NEW tables with indexes
-- and insert-only triggers; three nullable columns on evidence_drift_flags
-- (0252, not yet live when this lands with it); its UNIQUE constraint swapped
-- for a partial unique index over the same columns; the guard function
-- replaced with a STRICTER one. CREATE/ADD ... IF NOT EXISTS only. The runner
-- wraps this file in its own transaction.

CREATE TABLE IF NOT EXISTS public.evidence_drift_assessments (
    id                     bigserial PRIMARY KEY,
    classification_id      bigint NOT NULL REFERENCES public.evidence_drift_classifications (id),
    drift_id               bigint NOT NULL REFERENCES public.fetch_drift_observations (id),
    address                text NOT NULL,
    source_id              text,
    host                   text,
    -- the assessor
    assessor               text NOT NULL CHECK (assessor IN ('code_only', 'code_and_model')),
    assessor_stamp         text NOT NULL CHECK (length(assessor_stamp) BETWEEN 1 AND 300),
    rules_version          text NOT NULL,
    prompt_version         text,
    model_id               text,
    model_status           text NOT NULL DEFAULT 'not_run' CHECK (model_status IN (
                             'not_run', 'ok', 'parse_failed', 'call_failed')),
    model_output           jsonb,
    -- OBSERVED
    change_type            text NOT NULL CHECK (change_type IN (
                             'spelling_or_name_correction', 'update_new_facts', 'clarification',
                             'attribution_change', 'reframing', 'claim_removed', 'claim_reversed',
                             'fact_corrected', 'retraction', 'page_gone', 'content_replaced',
                             'mixed', 'unassessable')),
    component_types        text[] NOT NULL DEFAULT '{}'::text[],
    type_source            text NOT NULL CHECK (type_source = 'code'),
    code_type              text NOT NULL,
    code_components        text[] NOT NULL DEFAULT '{}'::text[],
    unassessable_reason    text CHECK (unassessable_reason IS NULL OR length(unassessable_reason) <= 500),
    acknowledged           text NOT NULL CHECK (acknowledged IN (
                             'correction_notice', 'visible_update_stamp', 'metadata_date_only', 'silent')),
    acknowledged_signals   text[] NOT NULL DEFAULT '{}'::text[],
    acknowledged_source    text NOT NULL CHECK (acknowledged_source = 'code'),
    acknowledged_evidence  jsonb NOT NULL DEFAULT '{}'::jsonb,
    model_type_doubt       jsonb,
    scope_pairs            jsonb NOT NULL DEFAULT '[]'::jsonb,
    removed_claims         jsonb NOT NULL DEFAULT '[]'::jsonb,
    claims_changed         integer NOT NULL DEFAULT 0 CHECK (claims_changed >= 0),
    share_changed          real CHECK (share_changed IS NULL OR share_changed BETWEEN 0 AND 1),
    interval_hours         real,
    timing_bucket          text NOT NULL CHECK (timing_bucket IN ('same_day', 'days', 'weeks_plus', 'unknown')),
    timing_basis           text NOT NULL CHECK (timing_basis IN ('published', 'first_read', 'unknown')),
    standing_hours         real,
    summary                text NOT NULL CHECK (length(summary) BETWEEN 1 AND 300),
    observations           jsonb NOT NULL DEFAULT '{}'::jsonb,
    -- DERIVED (code only)
    history_rewrite        boolean NOT NULL DEFAULT false,
    history_rewrite_inputs jsonb NOT NULL DEFAULT '{}'::jsonb,
    late_unexplained_change boolean NOT NULL DEFAULT false,
    late_unexplained_inputs jsonb NOT NULL DEFAULT '{}'::jsonb,
    assessed_at            timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT evidence_drift_assessments_once UNIQUE (classification_id, assessor_stamp),
    CONSTRAINT evidence_drift_assessments_mixed_shape CHECK (
        (change_type = 'mixed') = (cardinality(component_types) >= 2)),
    CONSTRAINT evidence_drift_assessments_unassessable_shape CHECK (
        (change_type = 'unassessable') = (unassessable_reason IS NOT NULL))
);

CREATE INDEX IF NOT EXISTS evidence_drift_assessments_classification_idx
    ON public.evidence_drift_assessments (classification_id, assessed_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS evidence_drift_assessments_source_idx
    ON public.evidence_drift_assessments (source_id, assessed_at DESC);
CREATE INDEX IF NOT EXISTS evidence_drift_assessments_address_idx
    ON public.evidence_drift_assessments (address, assessed_at DESC);

CREATE OR REPLACE FUNCTION public.evidence_drift_assessment_insert_only()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '% is insert-only: an assessment is a record of what was observed (%)',
        TG_TABLE_NAME, TG_OP USING ERRCODE = 'restrict_violation';
END $$;

DROP TRIGGER IF EXISTS evidence_drift_assessments_insert_only ON public.evidence_drift_assessments;
CREATE TRIGGER evidence_drift_assessments_insert_only
    BEFORE UPDATE OR DELETE ON public.evidence_drift_assessments
    FOR EACH ROW EXECUTE FUNCTION public.evidence_drift_assessment_insert_only();

COMMENT ON TABLE public.evidence_drift_assessments IS
    'D-2: what kind of change one classified page change was (OBSERVED: type '
    '(code only), acknowledged level and signals, scope, timing, summary; DERIVED '
    'in code: history_rewrite, late_unexplained_change). One per (classification, '
    'assessor stamp). Insert-only. Never states motive.';

-- -- re-open: a new flag row, never a backward move ---------------------------------

ALTER TABLE public.evidence_drift_flags
    ADD COLUMN IF NOT EXISTS reopened_from bigint REFERENCES public.evidence_drift_flags (id),
    ADD COLUMN IF NOT EXISTS reopened_by   text CHECK (reopened_by IS NULL OR length(reopened_by) BETWEEN 1 AND 256),
    ADD COLUMN IF NOT EXISTS reopen_note   text CHECK (reopen_note IS NULL OR length(reopen_note) <= 2000);

ALTER TABLE public.evidence_drift_flags DROP CONSTRAINT IF EXISTS evidence_drift_flags_once;
CREATE UNIQUE INDEX IF NOT EXISTS evidence_drift_flags_once_original
    ON public.evidence_drift_flags (drift_id, dependant_kind, dependant_id) WHERE reopened_from IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS evidence_drift_flags_reopened_once
    ON public.evidence_drift_flags (reopened_from) WHERE reopened_from IS NOT NULL;

-- D-1's guard, unchanged in what it refuses, plus the three re-open columns in
-- the never-change set.
CREATE OR REPLACE FUNCTION public.evidence_drift_flags_guard()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'evidence_drift_flags rows are never deleted (flag %)', OLD.id
            USING ERRCODE = 'restrict_violation';
    END IF;
    IF (NEW.classification_id, NEW.drift_id, NEW.address, NEW.earlier_version_id,
        NEW.drift_class, NEW.dependant_kind, NEW.dependant_id, NEW.depth, NEW.created_at)
       IS DISTINCT FROM
       (OLD.classification_id, OLD.drift_id, OLD.address, OLD.earlier_version_id,
        OLD.drift_class, OLD.dependant_kind, OLD.dependant_id, OLD.depth, OLD.created_at)
       OR NEW.later_version_id IS DISTINCT FROM OLD.later_version_id
       OR NEW.cited_span IS DISTINCT FROM OLD.cited_span
       OR NEW.span_check IS DISTINCT FROM OLD.span_check
       OR NEW.span_in_earlier IS DISTINCT FROM OLD.span_in_earlier
       OR NEW.span_in_later IS DISTINCT FROM OLD.span_in_later
       OR NEW.reopened_from IS DISTINCT FROM OLD.reopened_from
       OR NEW.reopened_by IS DISTINCT FROM OLD.reopened_by
       OR NEW.reopen_note IS DISTINCT FROM OLD.reopen_note THEN
        RAISE EXCEPTION 'evidence_drift_flags identity columns never change (flag %)', OLD.id
            USING ERRCODE = 'restrict_violation';
    END IF;
    IF OLD.state = 'resolved'
       OR (OLD.state = 'reviewed' AND NEW.state = 'open')
       OR (OLD.state = NEW.state AND NEW.state <> 'open') THEN
        RAISE EXCEPTION 'evidence_drift_flags state moves forward only (% -> %, flag %)',
            OLD.state, NEW.state, OLD.id USING ERRCODE = 'restrict_violation';
    END IF;
    RETURN NEW;
END $$;

-- -- per-citer effect ---------------------------------------------------------------

CREATE TABLE IF NOT EXISTS public.evidence_drift_claim_effects (
    id                  bigserial PRIMARY KEY,
    flag_id             bigint NOT NULL REFERENCES public.evidence_drift_flags (id),
    assessment_id       bigint NOT NULL REFERENCES public.evidence_drift_assessments (id),
    claim_effect        text NOT NULL CHECK (claim_effect IN (
                          'cited_text_survives', 'cited_text_gone', 'cited_text_changed', 'not_checkable')),
    after_text          text CHECK (after_text IS NULL OR length(after_text) <= 2000),
    basis               jsonb NOT NULL DEFAULT '{}'::jsonb,
    model_status        text NOT NULL DEFAULT 'not_run' CHECK (model_status IN (
                          'not_run', 'ok', 'parse_failed', 'call_failed')),
    model_effect        text CHECK (model_effect IS NULL OR model_effect IN (
                          'survives', 'changed', 'gone', 'cannot_tell')),
    model_after_text    text CHECK (model_after_text IS NULL OR length(model_after_text) <= 2000),
    model_output        jsonb,
    created_at          timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT evidence_drift_claim_effects_once UNIQUE (flag_id, assessment_id),
    CONSTRAINT evidence_drift_claim_effects_model_only_when_uncheckable CHECK (
        model_status = 'not_run' OR claim_effect = 'not_checkable')
);

CREATE INDEX IF NOT EXISTS evidence_drift_claim_effects_assessment_idx
    ON public.evidence_drift_claim_effects (assessment_id);

DROP TRIGGER IF EXISTS evidence_drift_claim_effects_insert_only ON public.evidence_drift_claim_effects;
CREATE TRIGGER evidence_drift_claim_effects_insert_only
    BEFORE UPDATE OR DELETE ON public.evidence_drift_claim_effects
    FOR EACH ROW EXECUTE FUNCTION public.evidence_drift_assessment_insert_only();

COMMENT ON TABLE public.evidence_drift_claim_effects IS
    'D-2: per flag and assessment, whether the cited text survives / is gone / '
    'changed / not checkable, by CODE; the model answers only not_checkable, '
    'in its own columns. Insert-only.';

-- -- the tracked auto-close ----------------------------------------------------------

CREATE TABLE IF NOT EXISTS public.evidence_drift_auto_closes (
    flag_id             bigint PRIMARY KEY REFERENCES public.evidence_drift_flags (id),
    assessment_id       bigint NOT NULL REFERENCES public.evidence_drift_assessments (id),
    claim_effect_id     bigint NOT NULL REFERENCES public.evidence_drift_claim_effects (id),
    rule_version        text NOT NULL,
    principal           text NOT NULL CHECK (principal LIKE 'system::%'),
    closed_at           timestamptz NOT NULL DEFAULT now()
);

DROP TRIGGER IF EXISTS evidence_drift_auto_closes_insert_only ON public.evidence_drift_auto_closes;
CREATE TRIGGER evidence_drift_auto_closes_insert_only
    BEFORE UPDATE OR DELETE ON public.evidence_drift_auto_closes
    FOR EACH ROW EXECUTE FUNCTION public.evidence_drift_assessment_insert_only();

COMMENT ON TABLE public.evidence_drift_auto_closes IS
    'D-2: flags the drift assessor resolved itself (spelling/name correction, '
    'cited text survives by the code check), with the assessment, the claim '
    'effect, the rule version and the system principal. Insert-only; a '
    're-open is a new flag row with reopened_from.';

-- -- the auto-close SHADOW count -------------------------------------------------------

CREATE TABLE IF NOT EXISTS public.evidence_drift_would_auto_close (
    flag_id             bigint NOT NULL REFERENCES public.evidence_drift_flags (id),
    rule_version        text NOT NULL,
    assessment_id       bigint NOT NULL REFERENCES public.evidence_drift_assessments (id),
    claim_effect_id     bigint NOT NULL REFERENCES public.evidence_drift_claim_effects (id),
    switch_on           boolean NOT NULL,
    recorded_at         timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (flag_id, rule_version)
);

CREATE INDEX IF NOT EXISTS evidence_drift_would_auto_close_recorded_idx
    ON public.evidence_drift_would_auto_close (recorded_at DESC);

DROP TRIGGER IF EXISTS evidence_drift_would_auto_close_insert_only ON public.evidence_drift_would_auto_close;
CREATE TRIGGER evidence_drift_would_auto_close_insert_only
    BEFORE UPDATE OR DELETE ON public.evidence_drift_would_auto_close
    FOR EACH ROW EXECUTE FUNCTION public.evidence_drift_assessment_insert_only();

COMMENT ON TABLE public.evidence_drift_would_auto_close IS
    'D-2: the shadow count — every flag the auto-close rule would close, recorded '
    'whether or not LEGBA_DRIFT_AUTO_CLOSE is on (switch_on says which). Never '
    'changes the flag. Insert-only.';
