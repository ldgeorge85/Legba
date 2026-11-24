-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0227_consult_writes.sql
--
-- THE LEDGER OF OPERATOR-DIRECTED CHANGE (C-2b-1/frame; design C-2a §6, D11).
--
-- WHY
-- ---
-- Consult can now carry out an operator's instruction as a change to the
-- platform (a source paused, a cadence moved, a draft retired) through the
-- `consult_write` pack and ONE executor in the registry. Every such write —
-- executed, staged for the operator's confirmation, refused, failed,
-- cancelled, expired — needs a durable record that:
--   * outlives the conversation (a Tier B write executes AFTER its turn
--     closed, and `consult_turns` cascades on session delete), so the table
--     carries the session / turn / request ids as PLAIN values with NO FOREIGN
--     KEY: nothing here is ever deleted by a cascade;
--   * replays without the model: confirm, the typed code, cancel and revert
--     all execute the stored canonical `args`; a reload renders the cards and
--     receipts from here;
--   * is the input SPLIT-1's head export reads: every live head consult minted
--     has a row with its pre/post image.
-- `action_pack_invocations` stays the governor ledger and is linked by
-- `invocation_id`; the signed audit row is linked by `audit_id`.
--
-- WHAT THIS ADDS
-- --------------
--   * `consult_writes` — one row per write call that reached the executor.
--     `status` ∈ {staged, pending_confirmation, executed, refused, failed,
--     cancelled, expired} and moves FORWARD ONLY: a trigger refuses any change
--     out of a terminal status and any move back to `staged`. Columns of an
--     executed row other than status (its `activation` stamp, its
--     `reversed_by_write_id`) may still be set.
--   * the confirm code is stored HASHED (`confirm_code_hash`, salted with the
--     write id), never in clear.
--   * three indexes, named for their readers: by session (the reload), by
--     class + object (the per-object log, the head export, `inspect_object`),
--     and a partial index on pending rows by `expires_at` (the typed-code
--     intercept and the expiry sweep).
--
-- SAFETY
-- ------
-- Additive and idempotent (CREATE ... IF NOT EXISTS, CREATE OR REPLACE
-- FUNCTION, DROP TRIGGER IF EXISTS + CREATE TRIGGER); routed through the
-- runner (no inline BEGIN/COMMIT). No retention policy: this is the record of
-- operator-directed change.

CREATE TABLE IF NOT EXISTS public.consult_writes (
    id                    uuid PRIMARY KEY,
    session_id            uuid,
    user_turn_id          uuid,
    request_id            text,
    turn_index            integer,
    operator_words        text,
    plane                 text,
    model_component       text,
    class                 text NOT NULL,
    verb                  text NOT NULL,
    object_ref            text NOT NULL,
    args                  jsonb NOT NULL,
    args_sha256           text NOT NULL,
    tier                  text NOT NULL,
    status                text NOT NULL,
    confirm_code_hash     text,
    expires_at            timestamptz,
    confirmed_via         text,
    pre_image             jsonb,
    post_image            jsonb,
    reversal              jsonb,
    reverses_write_id     uuid,
    reversed_by_write_id  uuid,
    invocation_id         text,
    audit_id              uuid,
    activation            jsonb,
    error                 jsonb,
    receipt               jsonb,
    created_at            timestamptz NOT NULL DEFAULT now(),
    updated_at            timestamptz NOT NULL DEFAULT now(),
    executed_at           timestamptz,
    CONSTRAINT consult_writes_status_chk CHECK (status IN (
        'staged', 'pending_confirmation', 'executed', 'refused', 'failed',
        'cancelled', 'expired')),
    CONSTRAINT consult_writes_tier_chk CHECK (tier IN ('A', 'B')),
    CONSTRAINT consult_writes_confirmed_via_chk CHECK (
        confirmed_via IS NULL OR confirmed_via IN ('click', 'typed_code', 'revert_button')),
    CONSTRAINT consult_writes_pending_has_code_chk CHECK (
        status <> 'pending_confirmation'
        OR (confirm_code_hash IS NOT NULL AND expires_at IS NOT NULL))
);

CREATE INDEX IF NOT EXISTS idx_consult_writes_session
    ON public.consult_writes (session_id, created_at);
CREATE INDEX IF NOT EXISTS idx_consult_writes_object
    ON public.consult_writes (class, object_ref, created_at);
CREATE INDEX IF NOT EXISTS idx_consult_writes_pending_expiry
    ON public.consult_writes (expires_at)
    WHERE status = 'pending_confirmation';

CREATE OR REPLACE FUNCTION public.consult_writes_status_forward_only()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.status IS DISTINCT FROM OLD.status THEN
        IF OLD.status IN ('executed', 'refused', 'failed', 'cancelled', 'expired') THEN
            RAISE EXCEPTION
                'consult_writes %: status % is terminal (attempted %)',
                OLD.id, OLD.status, NEW.status
                USING ERRCODE = 'check_violation';
        END IF;
        IF NEW.status = 'staged' THEN
            RAISE EXCEPTION
                'consult_writes %: status cannot move back to staged (from %)',
                OLD.id, OLD.status
                USING ERRCODE = 'check_violation';
        END IF;
    END IF;
    NEW.updated_at := now();
    RETURN NEW;
END;
$$;

COMMENT ON FUNCTION public.consult_writes_status_forward_only() IS
    'C-2b-1 (0227): consult_writes.status moves forward only; terminal statuses never change.';

DROP TRIGGER IF EXISTS trg_consult_writes_status_forward_only ON public.consult_writes;
CREATE TRIGGER trg_consult_writes_status_forward_only
    BEFORE UPDATE ON public.consult_writes
    FOR EACH ROW EXECUTE FUNCTION public.consult_writes_status_forward_only();

COMMENT ON TABLE public.consult_writes IS
    'C-2b-1 (0227): operator-directed changes made through consult (design C-2a §6). '
    'No FKs on purpose: receipts outlive the conversation.';
