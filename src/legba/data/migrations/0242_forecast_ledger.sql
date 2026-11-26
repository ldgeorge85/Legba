-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0242_forecast_ledger.sql
--
-- THE EXOGENOUS FORECAST LEDGER. 0241 stays reserved for the consult turn record.
--
-- WHY
-- ---
-- The Story's third promise: every probability names what resolves it and
-- when, and the Brier record is kept against the world, by date. The acute
-- hazard pilot (`acute_forecasts`, 0212) scores Legba's own hazard calls; this
-- ledger holds EXOGENOUS questions — a named authority, document, series or
-- reported event decides them — and every issuer's number on each one: the
-- operator, the scheduled tower, the consult, the frontier comparison arm, the
-- decision-model arm and, later, the think tank's seats and actor models.
--
-- WHAT THIS ADDS
-- --------------
--   * `forecast_questions` — one row per question: the question text, its
--     resolver kind (a six-kind vocabulary, plus the two endogenous
--     kinds stored with exogenous=false), the resolver spec, the window, the
--     due date and grace, the pre-registered void rules, the subject keys,
--     `sealed_at`, and `status` (open | resolved | void — the ONE mutable
--     column once sealed).
--   * `forecast_numbers` — one row per (question, issuer kind, issuer ref):
--     the probability, clamped 0.01..0.99 by the store, or NULL with an
--     `absent_reason` (an issuer that had no number is recorded as absent,
--     never imputed); the as-of of the evidence, the rationale (short text
--     and/or a reference) and `sealed_at`.
--   * `forecast_resolution_proposals` — what a resolver job proposes for the
--     operator to accept (`status` is mutable).
--   * `forecast_resolutions` — the outcome (yes | no | void), who resolved it,
--     the evidence refs, `early` (resolved before due, evidence required);
--     ONE active row per question, a correction is a new row plus an
--     `override_log` row and the old row's `active` flips false.
--   * `forecast_scores` — the view: every number with a p against the active
--     yes/no resolution, (p - o)^2.
--   * `scorecard_entries` — the operator scorecard's rows.
--   * `override_log` — every override, with who and a reason.
--
-- THE SEAL. A question and each number carry `content_sha256` from
-- insert (the store hashes canonical JSON; a number's hash binds the
-- question's hash, so it cannot be moved onto other words). `receipt_anchor`
-- folds every ledger hash into the day's Merkle root, so "issued before the
-- outcome" is externally checkable. Once `sealed_at` is set:
--   * a sealed question refuses every change but `status`;
--   * a sealed number refuses every UPDATE; a sealed question refuses a new
--     number;
--   * DELETE is refused on every ledger table, sealed or not.
-- Before the seal a question or number may be restated (UPDATE) — the
-- operator's condition text and numbers arrive after the questions do.
-- The triggers bind every role; TRUNCATE (tests only) does not fire them.
--
-- SAFETY
-- ------
-- Additive and idempotent: CREATE TABLE / INDEX IF NOT EXISTS, CREATE OR
-- REPLACE FUNCTION / VIEW, DROP TRIGGER IF EXISTS + CREATE TRIGGER. Applying
-- twice changes nothing. Routed through the filename-gated runner (no inline
-- BEGIN/COMMIT). The tables ship EMPTY: the forecasts are operator data loaded
-- by scripts/load_forecasts.py from the gitignored seeds/forecasts.yaml.

CREATE TABLE IF NOT EXISTS public.forecast_questions (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    slug            text NOT NULL,
    question        text NOT NULL,
    question_class  text NOT NULL DEFAULT 'general',
    resolver_kind   text NOT NULL,
    resolver_spec   jsonb NOT NULL,
    window_start    timestamptz,
    window_end      timestamptz,
    due_at          timestamptz NOT NULL,
    grace_hours     integer NOT NULL DEFAULT 72,
    condition       text,
    void_rules      text,
    exogenous       boolean NOT NULL,
    subject_keys    text[] NOT NULL DEFAULT '{}',
    status          text NOT NULL DEFAULT 'open',
    created_by      text NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    sealed_at       timestamptz,
    content_sha256  text NOT NULL,
    CONSTRAINT forecast_questions_slug_key UNIQUE (slug),
    CONSTRAINT forecast_questions_resolver_kind_vocab CHECK (resolver_kind IN (
        'event_reported', 'official_document', 'official_result',
        'series_threshold', 'count_threshold', 'scheduled_event_status',
        'indicator_state', 'desk_convergence'
    )),
    -- The endogenous kinds score Legba's own desks: stored, never headline.
    CONSTRAINT forecast_questions_endogenous_kinds CHECK (
        NOT exogenous OR resolver_kind NOT IN ('indicator_state', 'desk_convergence')
    ),
    CONSTRAINT forecast_questions_status_vocab CHECK (
        status IN ('open', 'resolved', 'void')
    ),
    CONSTRAINT forecast_questions_window_order CHECK (
        window_start IS NULL OR window_end IS NULL OR window_start <= window_end
    ),
    CONSTRAINT forecast_questions_grace_nonneg CHECK (grace_hours >= 0),
    CONSTRAINT forecast_questions_sha256 CHECK (content_sha256 ~ '^[0-9a-f]{64}$')
);

-- What resolves next: the Ledger's default order and the resolver job's scan.
CREATE INDEX IF NOT EXISTS idx_forecast_questions_status_due
    ON public.forecast_questions USING btree (status, due_at);
CREATE INDEX IF NOT EXISTS idx_forecast_questions_due
    ON public.forecast_questions USING btree (due_at);

CREATE TABLE IF NOT EXISTS public.forecast_numbers (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    question_id     uuid NOT NULL REFERENCES public.forecast_questions (id),
    issuer_kind     text NOT NULL,
    issuer_ref      text NOT NULL,
    p               numeric(4, 3),
    absent_reason   text,
    as_of           timestamptz NOT NULL,
    issued_at       timestamptz NOT NULL DEFAULT now(),
    rationale       text,
    rationale_ref   text,
    detail          jsonb NOT NULL DEFAULT '{}'::jsonb,
    sealed_at       timestamptz,
    content_sha256  text NOT NULL,
    CONSTRAINT forecast_numbers_issuer_key UNIQUE (question_id, issuer_kind, issuer_ref),
    CONSTRAINT forecast_numbers_issuer_kind_vocab CHECK (issuer_kind IN (
        'operator', 'tower', 'consult', 'synthesis', 'seat', 'actor_model',
        'frontier_arm', 'clef_arm'
    )),
    -- A probability is a hedge: 0.01..0.99 (the acute pilot's clamp rule).
    CONSTRAINT forecast_numbers_p_range CHECK (p IS NULL OR (p >= 0.01 AND p <= 0.99)),
    -- A number, or a stated absence — never both, never neither.
    CONSTRAINT forecast_numbers_p_or_absent CHECK ((p IS NULL) = (absent_reason IS NOT NULL)),
    CONSTRAINT forecast_numbers_sha256 CHECK (content_sha256 ~ '^[0-9a-f]{64}$')
);

CREATE INDEX IF NOT EXISTS idx_forecast_numbers_question
    ON public.forecast_numbers USING btree (question_id, issuer_kind);

CREATE TABLE IF NOT EXISTS public.forecast_resolution_proposals (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    question_id       uuid NOT NULL REFERENCES public.forecast_questions (id),
    proposed_outcome  text NOT NULL,
    evidence_refs     text[] NOT NULL DEFAULT '{}',
    proposer          text NOT NULL,
    proposed_at       timestamptz NOT NULL DEFAULT now(),
    status            text NOT NULL DEFAULT 'proposed',
    CONSTRAINT forecast_proposals_outcome_vocab CHECK (
        proposed_outcome IN ('yes', 'no', 'void')
    ),
    CONSTRAINT forecast_proposals_status_vocab CHECK (
        status IN ('proposed', 'accepted', 'rejected')
    )
);

CREATE INDEX IF NOT EXISTS idx_forecast_proposals_question_status
    ON public.forecast_resolution_proposals USING btree (question_id, status);

CREATE TABLE IF NOT EXISTS public.forecast_resolutions (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    question_id     uuid NOT NULL REFERENCES public.forecast_questions (id),
    outcome         text NOT NULL,
    resolved_at     timestamptz NOT NULL DEFAULT now(),
    resolved_by     text NOT NULL,
    evidence_refs   text[] NOT NULL DEFAULT '{}',
    early           boolean NOT NULL DEFAULT false,
    proposal_id     uuid REFERENCES public.forecast_resolution_proposals (id),
    note            text,
    active          boolean NOT NULL DEFAULT true,
    content_sha256  text NOT NULL,
    CONSTRAINT forecast_resolutions_outcome_vocab CHECK (outcome IN ('yes', 'no', 'void')),
    -- Resolved before the due date only with evidence named.
    CONSTRAINT forecast_resolutions_early_evidence CHECK (
        NOT early OR cardinality(evidence_refs) > 0
    ),
    CONSTRAINT forecast_resolutions_sha256 CHECK (content_sha256 ~ '^[0-9a-f]{64}$')
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_forecast_resolutions_active
    ON public.forecast_resolutions USING btree (question_id) WHERE active;

CREATE TABLE IF NOT EXISTS public.scorecard_entries (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    week            date NOT NULL,
    topic           text NOT NULL,
    operator_claim  text NOT NULL,
    legba_claim     text NOT NULL,
    outcome         text,
    verdict         text NOT NULL DEFAULT 'open',
    evidence_refs   text[] NOT NULL DEFAULT '{}',
    entered_by      text NOT NULL,
    entered_at      timestamptz NOT NULL DEFAULT now(),
    content_sha256  text NOT NULL,
    CONSTRAINT scorecard_entries_verdict_vocab CHECK (
        verdict IN ('legba', 'operator', 'both', 'neither', 'open')
    ),
    CONSTRAINT scorecard_entries_sha256 CHECK (content_sha256 ~ '^[0-9a-f]{64}$')
);

CREATE INDEX IF NOT EXISTS idx_scorecard_entries_week
    ON public.scorecard_entries USING btree (week);

CREATE TABLE IF NOT EXISTS public.override_log (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    at              timestamptz NOT NULL DEFAULT now(),
    actor           text NOT NULL,
    object_kind     text NOT NULL,
    object_id       uuid NOT NULL,
    action          text NOT NULL,
    before_sha256   text,
    after_sha256    text,
    reason          text NOT NULL,
    CONSTRAINT override_log_reason_present CHECK (length(btrim(reason)) > 0)
);

CREATE INDEX IF NOT EXISTS idx_override_log_object
    ON public.override_log USING btree (object_kind, object_id);

-- ---------------------------------------------------------------------------
-- The append-only guards
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION public.forecast_questions_guard()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'forecast_questions is append-only: DELETE of % refused', OLD.slug
            USING ERRCODE = 'check_violation';
    END IF;
    IF OLD.sealed_at IS NOT NULL
       AND (to_jsonb(NEW) - 'status') IS DISTINCT FROM (to_jsonb(OLD) - 'status') THEN
        RAISE EXCEPTION 'forecast question % is sealed: only status may change', OLD.slug
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS trg_forecast_questions_guard ON public.forecast_questions;
CREATE TRIGGER trg_forecast_questions_guard
    BEFORE UPDATE OR DELETE ON public.forecast_questions
    FOR EACH ROW EXECUTE FUNCTION public.forecast_questions_guard();

CREATE OR REPLACE FUNCTION public.forecast_numbers_guard()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    q_sealed timestamptz;
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'forecast_numbers is append-only: DELETE refused'
            USING ERRCODE = 'check_violation';
    END IF;
    IF TG_OP = 'INSERT' THEN
        SELECT sealed_at INTO q_sealed
          FROM public.forecast_questions WHERE id = NEW.question_id;
        IF q_sealed IS NOT NULL THEN
            RAISE EXCEPTION 'forecast question % is sealed: no new number', NEW.question_id
                USING ERRCODE = 'check_violation';
        END IF;
        RETURN NEW;
    END IF;
    IF OLD.sealed_at IS NOT NULL THEN
        RAISE EXCEPTION 'forecast number % is sealed: UPDATE refused', OLD.id
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS trg_forecast_numbers_guard ON public.forecast_numbers;
CREATE TRIGGER trg_forecast_numbers_guard
    BEFORE INSERT OR UPDATE OR DELETE ON public.forecast_numbers
    FOR EACH ROW EXECUTE FUNCTION public.forecast_numbers_guard();

CREATE OR REPLACE FUNCTION public.forecast_resolutions_guard()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'forecast_resolutions is append-only: DELETE refused'
            USING ERRCODE = 'check_violation';
    END IF;
    -- The one permitted change: an active row retired by a correction.
    IF (to_jsonb(NEW) - 'active') IS DISTINCT FROM (to_jsonb(OLD) - 'active')
       OR (NOT OLD.active AND NEW.active) THEN
        RAISE EXCEPTION 'forecast resolution % is append-only: only active true -> false may change', OLD.id
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS trg_forecast_resolutions_guard ON public.forecast_resolutions;
CREATE TRIGGER trg_forecast_resolutions_guard
    BEFORE UPDATE OR DELETE ON public.forecast_resolutions
    FOR EACH ROW EXECUTE FUNCTION public.forecast_resolutions_guard();

CREATE OR REPLACE FUNCTION public.forecast_proposals_guard()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'forecast_resolution_proposals is append-only: DELETE refused'
            USING ERRCODE = 'check_violation';
    END IF;
    IF (to_jsonb(NEW) - 'status') IS DISTINCT FROM (to_jsonb(OLD) - 'status') THEN
        RAISE EXCEPTION 'forecast proposal % : only status may change', OLD.id
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS trg_forecast_proposals_guard ON public.forecast_resolution_proposals;
CREATE TRIGGER trg_forecast_proposals_guard
    BEFORE UPDATE OR DELETE ON public.forecast_resolution_proposals
    FOR EACH ROW EXECUTE FUNCTION public.forecast_proposals_guard();

-- scorecard_entries and override_log: no UPDATE, no DELETE, ever.
CREATE OR REPLACE FUNCTION public.ledger_append_only_guard()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '% is append-only: % refused', TG_TABLE_NAME, TG_OP
        USING ERRCODE = 'check_violation';
END $$;

DROP TRIGGER IF EXISTS trg_scorecard_entries_append_only ON public.scorecard_entries;
CREATE TRIGGER trg_scorecard_entries_append_only
    BEFORE UPDATE OR DELETE ON public.scorecard_entries
    FOR EACH ROW EXECUTE FUNCTION public.ledger_append_only_guard();

DROP TRIGGER IF EXISTS trg_override_log_append_only ON public.override_log;
CREATE TRIGGER trg_override_log_append_only
    BEFORE UPDATE OR DELETE ON public.override_log
    FOR EACH ROW EXECUTE FUNCTION public.ledger_append_only_guard();

-- ---------------------------------------------------------------------------
-- The score view
-- ---------------------------------------------------------------------------

CREATE OR REPLACE VIEW public.forecast_scores AS
SELECT n.id                 AS number_id,
       n.question_id,
       q.slug,
       q.exogenous,
       n.issuer_kind,
       n.issuer_ref,
       n.p,
       r.outcome,
       CASE r.outcome WHEN 'yes' THEN 1 ELSE 0 END AS o,
       power(n.p - CASE r.outcome WHEN 'yes' THEN 1 ELSE 0 END, 2) AS brier,
       q.due_at,
       r.resolved_at
  FROM public.forecast_numbers n
  JOIN public.forecast_questions q ON q.id = n.question_id
  JOIN public.forecast_resolutions r ON r.question_id = n.question_id AND r.active
 WHERE r.outcome IN ('yes', 'no')
   AND n.p IS NOT NULL;
