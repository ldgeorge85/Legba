-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0089_signal_salience_schema.sql
--
-- S-1a (MASTER_PLAN 2026-07-10, Phase S — the salience layer). Adds the
-- per-signal CONSEQUENCE score + its forward-progress scan index for the new
-- `signal_salience` deterministic sweep (an $0-core-plane LLM sweep that scores
-- each raw text signal for {event_class, actor_rank, magnitude, authority} so
-- the tower can rank by CONSEQUENCE, not only recency).
--
-- WHY: consequence does not exist as DATA anywhere today — signals carry no
-- magnitude, facts/findings carry only confidence (support != consequence), and
-- both the journal slice AND meta_findings_synthesizer._prepare_input_rows are
-- NEWEST-FIRST. Recency was the only ranking in the whole tower, which is what
-- let a tabloid frame (Graham) and a meme outrank a head-of-state event
-- (Khamenei). This column is the substrate that Phase S consumption (S-2) and
-- the advisory judge (S-3) read.
--
-- signals.salience (nullable jsonb):
--   The scorer's verdict per row:
--     {event_class, actor_rank, magnitude float|null, authority,
--      confidence float, model_id, scored_at, degraded bool?}
--   The MODEL supplies event_class / actor_rank / magnitude; `authority` is
--   stamped DETERMINISTICALLY from the source's S1-T8 source_class (never
--   model-chosen — that is the anti-tabloid-authority guard). A NON-NULL salience
--   (incl. a `degraded:true` marker on an unparseable row) means "examined", so
--   the sweep is idempotent + forward-progressing and never re-scores or retries
--   a poison row forever — mirroring signals.summarized_at (migration 0081) /
--   signals.entities_resolved_at (baseline).
--
-- idx_signals_unscored_salience (partial, on fetched_at DESC):
--   The NEWEST-FIRST scan predicate of the sweep — WHERE salience IS NULL AND
--   modality = 'text' — so the per-tick SELECT is index-only over the shrinking
--   un-scored recent-text backlog and recent signals (the ones consumption
--   ranks) are scored first. Mirrors idx_signals_unsummarized (migration 0081)
--   but DESC (recency-prioritized; the sweep bounds itself to a recent window).
--
-- IDEMPOTENT: ADD COLUMN IF NOT EXISTS + CREATE INDEX IF NOT EXISTS, so a re-run
--   against an already-migrated DB (the live dev rig) is a clean no-op. Routed
--   through the filename-gated migration runner (ONE txn + ledger per file; NO
--   inline BEGIN/COMMIT).
--
-- MEASURED (live `legba`, 2026-07-13, migration head 0088): 48,333 signals with
--   modality='text'; 4,257 in the last 72h. At ~300 scored/tick hourly the
--   recent-window backlog drains in ~14 ticks, and per-tick capacity (~7.2k/day)
--   far exceeds the ~1.4k/day text inflow, so the window stays fully covered.

ALTER TABLE public.signals
    ADD COLUMN IF NOT EXISTS salience jsonb;

CREATE INDEX IF NOT EXISTS idx_signals_unscored_salience
    ON public.signals USING btree (fetched_at DESC)
    WHERE salience IS NULL AND modality = 'text';
