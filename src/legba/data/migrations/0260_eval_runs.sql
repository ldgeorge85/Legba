-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0260_eval_runs.sql
--
-- THE PUBLISHED EVALUATION RECORD (E-3; resolves SEAMS #89).
-- 0255–0259 belong to other lanes.
--
-- WHY
-- ---
-- The console's Ledger page shows the standing evaluations (the blind test
-- and the ones after it) to signed-in readers, from `GET /v3/eval/runs`. A
-- run's results live as the harness's own files on the host; the owner
-- chooses which run is published, and the loader
-- (`scripts/publish_eval_run.py`) writes it here. Only the run's description
-- and its per-arm AGGREGATES are stored: there is no column for a question,
-- a gold answer, a model answer or any per-question text, and no per-question
-- table.
--
-- WHAT THIS ADDS
-- --------------
--   * `eval_runs` — one row per published VERSION of a run. `run_key` is the
--     stable id (the harness's run id); `version` counts from 1. A correction
--     is a NEW row (`version` + 1) whose `supersedes_id` names the row it
--     corrects and whose `correction_reason` says why; both rows are kept
--     (the descriptor-head / resolution-record pattern). A version is
--     superseded when a row names it — derived, never written onto it.
--     Withdrawal is the ONE change a published row takes: `withdrawn_at`,
--     `withdrawn_by` and `withdrawn_reason`, set together, once (the guard
--     trigger below); the record stays and the route lists it as withdrawn.
--   * `eval_run_arms` — one row per arm of a version: the harness label, the
--     plain-words label and description, the score on the run's primary
--     measure with its interval and n, the per-class scores with their n,
--     and the sample index where an arm was run twice. Written only in the
--     publishing transaction; never updated, never deleted.
--
-- THE SEAL. `content_sha256` over the version's canonical content (its
-- description and every arm), computed by `legba.data.eval_runs`.
--
-- SAFETY
-- ------
-- Additive and idempotent: CREATE TABLE / INDEX IF NOT EXISTS, CREATE OR
-- REPLACE FUNCTION, DROP TRIGGER IF EXISTS + CREATE TRIGGER. Applying twice
-- changes nothing. Routed through the filename-gated runner (no inline
-- BEGIN/COMMIT). Depends on nothing but pgcrypto's gen_random_uuid (core
-- since PG 13). Ships EMPTY.

CREATE TABLE IF NOT EXISTS public.eval_runs (
    id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    run_key               text NOT NULL,
    version               integer NOT NULL,
    supersedes_id         uuid REFERENCES public.eval_runs (id),
    correction_reason     text,
    title                 text NOT NULL,
    purpose               text,
    design_sha256         text NOT NULL,
    design_version        text NOT NULL,
    design_registered_at  timestamptz,
    as_of                 timestamptz NOT NULL,
    question_count        integer NOT NULL,
    question_classes      jsonb NOT NULL DEFAULT '[]'::jsonb,
    grader                text NOT NULL,
    temperature           double precision NOT NULL,
    gold_versions         jsonb NOT NULL,
    metric                text NOT NULL,
    metric_label          text NOT NULL,
    higher_is_better      boolean NOT NULL DEFAULT true,
    ci_level              double precision,
    caveats               jsonb NOT NULL DEFAULT '[]'::jsonb,
    deviations            jsonb NOT NULL DEFAULT '[]'::jsonb,
    source_schema         text NOT NULL,
    source_sha256         text NOT NULL,
    published_by          text NOT NULL,
    published_at          timestamptz NOT NULL DEFAULT now(),
    withdrawn_at          timestamptz,
    withdrawn_by          text,
    withdrawn_reason      text,
    content_sha256        text NOT NULL,
    CONSTRAINT eval_runs_key_shape CHECK (run_key ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$'),
    CONSTRAINT eval_runs_version_positive CHECK (version >= 1),
    -- Version 1 corrects nothing; every later version names what it corrects, and why.
    CONSTRAINT eval_runs_supersedes CHECK ((version = 1) = (supersedes_id IS NULL)),
    CONSTRAINT eval_runs_correction_reason CHECK (
        supersedes_id IS NULL OR length(btrim(coalesce(correction_reason, ''))) > 0
    ),
    CONSTRAINT eval_runs_title CHECK (length(btrim(title)) BETWEEN 1 AND 200),
    -- One sentence about the RUN, never a question of the test set.
    CONSTRAINT eval_runs_purpose CHECK (purpose IS NULL OR length(purpose) <= 300),
    CONSTRAINT eval_runs_design_sha256 CHECK (design_sha256 ~ '^[0-9a-f]{64}$'),
    CONSTRAINT eval_runs_source_sha256 CHECK (source_sha256 ~ '^[0-9a-f]{64}$'),
    CONSTRAINT eval_runs_content_sha256 CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
    CONSTRAINT eval_runs_question_count CHECK (question_count >= 1),
    CONSTRAINT eval_runs_temperature CHECK (temperature >= 0 AND temperature <= 2),
    CONSTRAINT eval_runs_ci_level CHECK (ci_level IS NULL OR (ci_level > 0 AND ci_level < 1)),
    CONSTRAINT eval_runs_lists CHECK (
        jsonb_typeof(question_classes) = 'array'
        AND jsonb_typeof(gold_versions) = 'array' AND jsonb_array_length(gold_versions) >= 1
        AND jsonb_typeof(caveats) = 'array'
        AND jsonb_typeof(deviations) = 'array'
    ),
    CONSTRAINT eval_runs_published_by CHECK (length(btrim(published_by)) > 0),
    -- Withdrawn is all three or none, with a reason.
    CONSTRAINT eval_runs_withdrawn CHECK (
        (withdrawn_at IS NULL AND withdrawn_by IS NULL AND withdrawn_reason IS NULL)
        OR (withdrawn_at IS NOT NULL AND length(btrim(coalesce(withdrawn_by, ''))) > 0
            AND length(btrim(coalesce(withdrawn_reason, ''))) > 0)
    )
);

-- The stable id's versions (the loader's next-version read; the route's grouping).
CREATE UNIQUE INDEX IF NOT EXISTS uq_eval_runs_key_version
    ON public.eval_runs USING btree (run_key, version);
-- A version is corrected at most once (the chain never forks).
CREATE UNIQUE INDEX IF NOT EXISTS uq_eval_runs_supersedes
    ON public.eval_runs USING btree (supersedes_id) WHERE supersedes_id IS NOT NULL;
-- The route's order: newest as-of first.
CREATE INDEX IF NOT EXISTS idx_eval_runs_as_of
    ON public.eval_runs USING btree (as_of DESC, run_key, version DESC);

CREATE TABLE IF NOT EXISTS public.eval_run_arms (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id         uuid NOT NULL REFERENCES public.eval_runs (id),
    position       integer NOT NULL,
    arm_key        text NOT NULL,
    label          text NOT NULL,
    description    text NOT NULL,
    sample_of      text,
    sample_index   integer,
    score          double precision,
    ci_low         double precision,
    ci_high        double precision,
    n              integer NOT NULL,
    by_class       jsonb NOT NULL DEFAULT '[]'::jsonb,
    CONSTRAINT eval_run_arms_key_shape CHECK (arm_key ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$'),
    CONSTRAINT eval_run_arms_label CHECK (length(btrim(label)) BETWEEN 1 AND 120),
    CONSTRAINT eval_run_arms_description CHECK (length(btrim(description)) BETWEEN 1 AND 400),
    CONSTRAINT eval_run_arms_position CHECK (position >= 0),
    CONSTRAINT eval_run_arms_n CHECK (n >= 0),
    CONSTRAINT eval_run_arms_interval CHECK (
        (ci_low IS NULL AND ci_high IS NULL) OR (ci_low IS NOT NULL AND ci_high IS NOT NULL AND ci_low <= ci_high)
    ),
    -- An arm run twice names the arm it repeats and which sample it is.
    CONSTRAINT eval_run_arms_sample CHECK (
        (sample_of IS NULL AND sample_index IS NULL)
        OR (sample_of IS NOT NULL AND sample_index IS NOT NULL AND sample_index >= 1)
    ),
    CONSTRAINT eval_run_arms_by_class CHECK (jsonb_typeof(by_class) = 'array')
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_eval_run_arms_run_key
    ON public.eval_run_arms USING btree (run_id, arm_key);
-- The route's arm read (run_id = ANY(...) ORDER BY run_id, position).
CREATE UNIQUE INDEX IF NOT EXISTS uq_eval_run_arms_run_position
    ON public.eval_run_arms USING btree (run_id, position);

-- The guard: DELETE refused on both tables; UPDATE refused on the arms; on a
-- run, UPDATE admitted ONLY as the one-time withdrawal (the three withdrawn_*
-- columns go from unset to set and nothing else changes).
CREATE OR REPLACE FUNCTION public.eval_runs_guard()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    -- Nested, not one AND: plpgsql resolves OLD.withdrawn_at even when the
    -- table test is false, and eval_run_arms has no such column.
    IF TG_OP = 'UPDATE' AND TG_TABLE_NAME = 'eval_runs' THEN
        IF (to_jsonb(OLD) ->> 'withdrawn_at') IS NULL
           AND (to_jsonb(NEW) ->> 'withdrawn_at') IS NOT NULL
           AND (to_jsonb(NEW) - 'withdrawn_at' - 'withdrawn_by' - 'withdrawn_reason')
             = (to_jsonb(OLD) - 'withdrawn_at' - 'withdrawn_by' - 'withdrawn_reason')
        THEN
            RETURN NEW;
        END IF;
    END IF;
    RAISE EXCEPTION '% is append-only (a run is corrected by a new version, or withdrawn once): % refused',
        TG_TABLE_NAME, TG_OP USING ERRCODE = 'check_violation';
END $$;

DROP TRIGGER IF EXISTS trg_eval_runs_guard ON public.eval_runs;
CREATE TRIGGER trg_eval_runs_guard
    BEFORE UPDATE OR DELETE ON public.eval_runs
    FOR EACH ROW EXECUTE FUNCTION public.eval_runs_guard();

DROP TRIGGER IF EXISTS trg_eval_run_arms_guard ON public.eval_run_arms;
CREATE TRIGGER trg_eval_run_arms_guard
    BEFORE UPDATE OR DELETE ON public.eval_run_arms
    FOR EACH ROW EXECUTE FUNCTION public.eval_runs_guard();

-- Arms are written only in the transaction that published their run.
CREATE OR REPLACE FUNCTION public.eval_run_arms_same_transaction()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM public.eval_runs r
         WHERE r.id = NEW.run_id AND r.published_at = now()
    ) THEN
        RAISE EXCEPTION 'eval_run_arms: an arm is written only with its run''s publication'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS trg_eval_run_arms_same_transaction ON public.eval_run_arms;
CREATE TRIGGER trg_eval_run_arms_same_transaction
    BEFORE INSERT ON public.eval_run_arms
    FOR EACH ROW EXECUTE FUNCTION public.eval_run_arms_same_transaction();

COMMENT ON TABLE public.eval_runs IS
    'Owner-published evaluation runs (mig 0260, E-3): one row per version; aggregates only, '
    'no per-question text. Corrected by a new version (supersedes_id), withdrawn once, never deleted.';
COMMENT ON TABLE public.eval_run_arms IS
    'The arms of a published evaluation run version (mig 0260): score, interval, n, per-class '
    'scores. Append-only; written in the publishing transaction.';
