-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0244_forecast_resolution_records.sql
--
-- THE RESOLUTION RECORD.
-- 0241 stays reserved for the consult turn record; 0243 is the evidence archive tiering.
--
-- WHY
-- ---
-- 0242 gave the forecast ledger its outcome table (`forecast_resolutions`, one
-- active row per question) and a proposals table whose `status` is mutable and
-- whose outcome vocabulary has no room for "the rule cannot decide this". A
-- resolution must say what it rests on: the evidence rows and spans, which
-- clause of the sealed criterion each satisfies, what was searched when nothing
-- was found, the void rule that applies, and which human judgement it waits
-- on. And it must never be edited: a confirmation, an override or a correction
-- is a NEW row that names the row it answers.
--
-- WHAT THIS ADDS
-- --------------
--   * `forecast_resolution_records` — append-only, one row per act:
--       proposal      the resolver job's reading at an as-of (yes | no | void |
--                     undetermined | needs_judgement), with evidence, clauses,
--                     judgements, the searched scope and the void rule;
--       confirmation  someone accepts a proposal's yes | no | void (a rule for
--                     the machine-resolvable kinds, the operator otherwise);
--                     carries the `forecast_resolutions` row it wrote;
--       override      the operator decides against, or beyond, the rule — a
--                     judgement the rule cannot make; carries its
--                     `override_log` row and a reason;
--       correction    a later act replaces an earlier confirmation/override;
--                     `supersedes_id` + a reason.
--     `fingerprint` makes the job idempotent: the same reading of the same
--     question on the same evidence is one row however often the job runs.
--   * The ledger's status transition is unchanged: the store's `resolve()`
--     writes `forecast_resolutions` and sets the question's `status` — the
--     one column the 0242 seal trigger lets change.
--
-- THE SEAL. `content_sha256` from insert; `receipt_anchor` folds every
-- record's hash into the day's Merkle root beside the 0242 leaves.
-- UPDATE and DELETE are refused for every role (the 0242
-- `ledger_append_only_guard`).
--
-- SAFETY
-- ------
-- Additive and idempotent: CREATE TABLE / INDEX IF NOT EXISTS, DROP TRIGGER IF
-- EXISTS + CREATE TRIGGER. Applying twice changes nothing. Routed through the
-- filename-gated runner (no inline BEGIN/COMMIT). Requires 0242 (the guard
-- function, the questions, resolutions and override_log tables). Ships EMPTY.

CREATE TABLE IF NOT EXISTS public.forecast_resolution_records (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    question_id       uuid NOT NULL REFERENCES public.forecast_questions (id),
    record_kind       text NOT NULL,
    outcome           text NOT NULL,
    resolver_kind     text NOT NULL,
    resolver_version  text NOT NULL,
    as_of             timestamptz NOT NULL,
    recorded_at       timestamptz NOT NULL DEFAULT now(),
    actor             text NOT NULL,
    evidence          jsonb NOT NULL DEFAULT '[]'::jsonb,
    clauses           jsonb NOT NULL DEFAULT '[]'::jsonb,
    judgements        jsonb NOT NULL DEFAULT '[]'::jsonb,
    searched          jsonb NOT NULL DEFAULT '{}'::jsonb,
    void_rule         jsonb,
    basis_record_id   uuid REFERENCES public.forecast_resolution_records (id),
    supersedes_id     uuid REFERENCES public.forecast_resolution_records (id),
    resolution_id     uuid REFERENCES public.forecast_resolutions (id),
    override_log_id   uuid REFERENCES public.override_log (id),
    reason            text,
    fingerprint       text NOT NULL,
    content_sha256    text NOT NULL,
    CONSTRAINT forecast_records_kind_vocab CHECK (
        record_kind IN ('proposal', 'confirmation', 'override', 'correction')
    ),
    CONSTRAINT forecast_records_outcome_vocab CHECK (
        outcome IN ('yes', 'no', 'void', 'undetermined', 'needs_judgement')
    ),
    -- Only a proposal may say "the rule cannot decide": every act that closes
    -- a question names yes, no or void.
    CONSTRAINT forecast_records_act_outcome CHECK (
        record_kind = 'proposal' OR outcome IN ('yes', 'no', 'void')
    ),
    -- A closing act wrote a resolution row.
    CONSTRAINT forecast_records_act_resolution CHECK (
        record_kind = 'proposal' OR resolution_id IS NOT NULL
    ),
    -- An override and a correction carry a reason; an override its log row.
    CONSTRAINT forecast_records_reason CHECK (
        record_kind NOT IN ('override', 'correction')
        OR length(btrim(coalesce(reason, ''))) > 0
    ),
    CONSTRAINT forecast_records_override_logged CHECK (
        record_kind <> 'override' OR override_log_id IS NOT NULL
    ),
    CONSTRAINT forecast_records_correction_supersedes CHECK (
        record_kind <> 'correction' OR supersedes_id IS NOT NULL
    ),
    CONSTRAINT forecast_records_sha256 CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
    CONSTRAINT forecast_records_fingerprint CHECK (fingerprint ~ '^[0-9a-f]{64}$')
);

-- Idempotency: one row per (question, reading).
CREATE UNIQUE INDEX IF NOT EXISTS uq_forecast_records_fingerprint
    ON public.forecast_resolution_records USING btree (question_id, fingerprint);
-- The newest act per question (the scorecard's and the job's read).
CREATE INDEX IF NOT EXISTS idx_forecast_records_question_recorded
    ON public.forecast_resolution_records USING btree (question_id, recorded_at DESC);

DROP TRIGGER IF EXISTS trg_forecast_resolution_records_append_only
    ON public.forecast_resolution_records;
CREATE TRIGGER trg_forecast_resolution_records_append_only
    BEFORE UPDATE OR DELETE ON public.forecast_resolution_records
    FOR EACH ROW EXECUTE FUNCTION public.ledger_append_only_guard();
