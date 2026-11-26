-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0268_desk_rows_shadow.sql
--
-- `desk_rows_shadow` — one row per desk run a contract-v2 SHADOW was run
-- beside.
--
-- WHY
-- ---
-- A desk (an inline_target unit) writes one prose finding. Contract v2 asks
-- the same desk, on the same handed slice and the same rendered prompt, for
-- ROWS instead — fact rows (one claim, its subject, its exact quotes),
-- absence rows, assessment rows resting on fact rows, restatements of the
-- prior read — gates every row before anything is written, runs one bounded
-- repair round and the clean-context verifier, and renders the read from the
-- rows (`legba.data.desk_rows_shadow`). Before it may become what readers see
-- it is MEASURED, on live runs, beside the prose it would replace. This table
-- holds that measurement.
--
-- NOTHING READS IT. It is not a finding, not a statement and not knowledge:
-- no reader surface, Consult tool, slice, indicator, alert, composition or
-- downstream analyst consumes it (tests/data_pkg/test_desk_rows_shadow_isolation.py
-- proves the table name appears in no module but its writer). It is NOT the
-- persistent statement layer, whose storage and naming are under review.
--
-- `counts` is counts only (rows by outcome, failure reasons, repair yield,
-- verdicts, calls, seconds). `record` is the full shadow (rows, outcomes with
-- reasons, the rendered read, the computed collection block, the slice
-- scope). `status` is 'written' or why not ('reply_unparseable', 'no_call',
-- 'timeout', 'error:<class>'). No foreign keys: the ledger outlives the runs
-- it names.
--
-- SAFETY
-- ------
-- Additive: a new table and its indexes. Re-apply is a no-op (IF NOT EXISTS);
-- routed through the runner (no inline BEGIN/COMMIT). Written only when
-- LEGBA_DESK_ROWS_SHADOW names a desk (unset = OFF, the shipped default).

CREATE TABLE IF NOT EXISTS public.desk_rows_shadow (
    id                bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    occurred_at       timestamptz NOT NULL DEFAULT now(),
    run_id            text,
    analyst_id        text NOT NULL,
    target_id         text,
    contract_version  text NOT NULL,
    gate_version      text NOT NULL,
    status            text NOT NULL,
    counts            jsonb NOT NULL DEFAULT '{}'::jsonb,
    record            jsonb NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS desk_rows_shadow_analyst_occurred
    ON public.desk_rows_shadow (analyst_id, occurred_at);

CREATE INDEX IF NOT EXISTS desk_rows_shadow_run
    ON public.desk_rows_shadow (run_id);

COMMENT ON TABLE public.desk_rows_shadow IS
    'D-1: one row per desk run shadowed under output contract v2 (rows, gates, '
    'one repair round, verifier, rendered read). Measurement only; nothing '
    'reads it (mig 0268).';
