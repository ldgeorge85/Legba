-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0195_consult_turn_recovery.sql
--
-- Make a cut consult run RECOVERABLE, and make what it cost VISIBLE.
--
-- THE RUN THIS IS FOR
-- -------------------
-- Session f12ebbd4-cac1-4692-ab10-1e045b48b2ae, request c8a0105c, 2026-09-16
-- 09:43-09:49Z. Eleven Anthropic calls on a Fable-priced route, ten rounds,
-- fifty tool calls, 283 cited refs — and then the final synthesis was cut and
-- its partial text discarded. The operator received 487 characters of apology
-- and paid roughly ten dollars for it.
--
-- Re-reading that turn afterwards, two things were missing and both are
-- columns:
--
--   1. There was no way to ask for the answer again over the evidence already
--      gathered, because nothing on the row identified the RUN (`request_id`
--      lived only in the registry's in-process dict, dropped at restart) and
--      nothing held the prompt the loop had built.
--   2. There was no record of what the run COST. `steps` carried a per-call
--      token count and nothing summed it; no tokens, no dollars, no model.
--
-- WHAT THIS ADDS (all to `consult_turns`, all additive, all idempotent)
-- ---------------------------------------------------------------------
--   * `request_id text` — the run that produced this turn. The bridge between
--     a live run and its persisted evidence, and the key the recovery endpoint
--     (`POST /api/v1/consult/runs/{request_id}/synthesize`) resolves on. NULL
--     for every pre-existing row, which is why that endpoint also accepts a
--     turn id.
--   * `parent_turn_id uuid` — a re-synthesised turn points at the turn whose
--     evidence it re-read. Self-referential FK, ON DELETE SET NULL so removing
--     an original never cascades away the better answer that replaced it.
--   * `synthesis_status text NOT NULL DEFAULT 'complete'` — 'complete' (the
--     model finished), 'partial' (it was cut and the prefix was delivered) or
--     'none' (it never produced a token). Pre-existing rows default to
--     'complete', which is WRONG for the c8a0105c turn specifically and right
--     for the overwhelming majority; the UI therefore still falls back to
--     scanning `steps` for `degraded_final` on rows whose status is the
--     default and whose trace disagrees. No CHECK constraint, matching 0192's
--     discipline: the vocabulary is enforced by a closed Literal in code, and
--     a CHECK would turn every addition into a migration.
--   * `replay_transcript jsonb` — the synthesis request AS SENT (system string
--     + full message list), recorded before the call so that a run cut mid-
--     synthesis is exactly the run whose prompt survives. This is what lets a
--     replay send the same prompt rather than a summary of the evidence. Only
--     written for runs that turn out to need recovery; a run that answered has
--     nothing to replay and stores NULL.
--   * `usage jsonb NOT NULL DEFAULT '{}'` — the run's own spend receipt:
--     calls, input/output tokens, estimated USD, and the ceilings they ran
--     against. Denormalised onto the turn deliberately: the budget ledger
--     aggregates by component and cannot answer "what did THIS answer cost".
--
-- SIZE
-- ----
-- `replay_transcript` is the only heavy column and it is bounded twice: the
-- loop compacts older rounds to refs + digest before the synthesis is built,
-- and `consult_transcript.MAX_TRANSCRIPT_CHARS` (600k) refuses to store a
-- record past that, marking it unusable so a replay falls back to the rebuild
-- path rather than sending a truncated prompt. A ten-round compacted run
-- measures ~60 KB. TOAST handles it; no separate table is warranted for a
-- column this sparse.
--
-- SAFETY
-- ------
-- Every statement is `IF NOT EXISTS`. No column is dropped, no type changed,
-- no existing value rewritten. Re-running is a no-op. Nothing here is required
-- by the read path: every consumer treats a NULL/absent value as "this turn
-- predates the column", which is true.

ALTER TABLE public.consult_turns
    ADD COLUMN IF NOT EXISTS request_id text;

ALTER TABLE public.consult_turns
    ADD COLUMN IF NOT EXISTS parent_turn_id uuid;

ALTER TABLE public.consult_turns
    ADD COLUMN IF NOT EXISTS synthesis_status text NOT NULL DEFAULT 'complete';

ALTER TABLE public.consult_turns
    ADD COLUMN IF NOT EXISTS replay_transcript jsonb;

ALTER TABLE public.consult_turns
    ADD COLUMN IF NOT EXISTS usage jsonb NOT NULL DEFAULT '{}'::jsonb;

-- The self-FK is added separately and guarded: `ADD CONSTRAINT` has no
-- IF NOT EXISTS in PostgreSQL, so re-running would raise 42710 without this.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'consult_turns_parent_turn_id_fkey'
    ) THEN
        ALTER TABLE public.consult_turns
            ADD CONSTRAINT consult_turns_parent_turn_id_fkey
            FOREIGN KEY (parent_turn_id)
            REFERENCES public.consult_turns(id)
            ON DELETE SET NULL;
    END IF;
END
$$;

-- Partial: `request_id` is NULL on every row written before this migration,
-- and the index exists to serve exactly one lookup — resolve a run to its
-- turn — which never asks for NULL.
CREATE INDEX IF NOT EXISTS idx_consult_turns_request
    ON public.consult_turns (request_id)
    WHERE request_id IS NOT NULL;

-- Serves the recovery UI's "show me the re-synthesis of this turn" read.
CREATE INDEX IF NOT EXISTS idx_consult_turns_parent
    ON public.consult_turns (parent_turn_id)
    WHERE parent_turn_id IS NOT NULL;
