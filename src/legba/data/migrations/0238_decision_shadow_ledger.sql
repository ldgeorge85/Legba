-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0238_decision_shadow_ledger.sql
--
-- `decision_shadow_ledger` — one row per decision a SHADOW model was asked
-- beside the model that actually decided.
--
-- WHY
-- ---
-- claim_watch's bearing gate asks a small chat model, per candidate edge,
-- whether new evidence bears on an open question (≈ 32,000 calls a week). A
-- DECISION model behind the same gateway (`POST /decide`) answers
-- that shape of question as a typed choice with a probability per option. It
-- is measured before it decides anything: with `bearing_shadow_ref` set, every
-- pair the gate asked is asked of the shadow too, AFTER the gate's edges are
-- written, and the pair lands here. `scripts/decision_shadow_report.py` reads
-- agreement, the 2x2 confusion and the calibration by p_yes decile.
--
-- Nothing reads this table to decide anything. A row is either an answer
-- (`shadow_choice` set) or a failure (`shadow_error` set: an error class and
-- status, never a provider message — a body can echo the request).
--
-- `leg` names the decision being shadowed ('bearing_shadow' today), so a
-- second shadowed decision is a new leg, not a new table. `edge_ref` names the
-- judged pair (`signal:<id>->question:<id>`); `state_sha256` is the digest of
-- the exact state text sent, so two models are provably compared on one text.
-- `target_id` is the question's desk (the run's target when the question has
-- none). No foreign keys: the ledger must outlive the rows it names.
--
-- SAFETY
-- ------
-- Additive: a new table and its indexes. Re-apply is a no-op (IF NOT EXISTS);
-- routed through the runner (no inline BEGIN/COMMIT).

CREATE TABLE IF NOT EXISTS public.decision_shadow_ledger (
    id                   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    occurred_at          timestamptz NOT NULL DEFAULT now(),
    run_id               uuid,
    analyst_id           text,
    target_id            text,
    question_id          uuid,
    edge_ref             text NOT NULL,
    leg                  text NOT NULL,
    gate_component_id    text NOT NULL,
    gate_verdict         text NOT NULL,
    shadow_component_id  text NOT NULL,
    shadow_choice        text,
    shadow_p_yes         numeric,
    shadow_confidence    numeric,
    shadow_latency_ms    integer,
    shadow_error         text,
    state_sha256         text NOT NULL,
    CONSTRAINT decision_shadow_ledger_gate_vocab CHECK (
        gate_verdict IN ('yes', 'no', 'unavailable')
    ),
    -- An answer or a failure, never both and never neither.
    CONSTRAINT decision_shadow_ledger_answer_or_error CHECK (
        (shadow_choice IS NULL) = (shadow_error IS NOT NULL)
    ),
    CONSTRAINT decision_shadow_ledger_p_yes_range CHECK (
        shadow_p_yes IS NULL OR (shadow_p_yes >= 0 AND shadow_p_yes <= 1)
    )
);

-- The report: one leg over a window, overall and per analyst.
CREATE INDEX IF NOT EXISTS decision_shadow_ledger_leg_occurred
    ON public.decision_shadow_ledger (leg, occurred_at);

CREATE INDEX IF NOT EXISTS decision_shadow_ledger_analyst_occurred
    ON public.decision_shadow_ledger (analyst_id, occurred_at);

COMMENT ON TABLE public.decision_shadow_ledger IS
    'W11-F108: one row per decision a shadow model was asked beside the model '
    'that decided (leg bearing_shadow = the claim_watch bearing gate vs a '
    '/decide model). Measurement only; nothing reads it to decide (mig 0238).';
