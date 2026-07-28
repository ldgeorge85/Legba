-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0099_source_track_records.sql
--
-- P3-3 (A6 layer 3 — the EARNED source track record; the MEASURED half of the
-- source assurance ledger; planning/PROGRAM_RECOMMENDATIONS_2026-07-24.md §A6):
-- the per-source, recomputable-from-our-own-substrate record of "when this
-- source's claims were contested, how often did accumulating evidence side
-- with it?" — the operator's key insight that grades should be MEASURED, not
-- just asserted.
--
-- NUMBERING NOTE: 0096/0097/0098 are held by concurrent/prior branches; 0099
-- is this branch's number. The runner discovers by sorted glob, so a gap is
-- harmless (same note as 0098).
--
-- WHY A SIDECAR (not columns on the source descriptor / source_ratings) — the
-- 0055 fact_contention + 0098 fact_decay_states precedent: this record is
-- DERIVED and fully RECOMPUTABLE from the RESOLVED `fact_contention` groups
-- (0055/0097 sidecar) + the fact->signal->source lineage. Drop the table,
-- re-run `source_track_record`, identical content: that recomputability is the
-- proof it is derived, not primary. Keeping it off `source_ratings` also keeps
-- the two layers honestly separate — layer 2 is ASSERTED rubric (Admiralty),
-- layer 3 is MEASURED outcome; a source can carry either, both, or neither.
--
-- IDENTITY: `source_id` is the source DESCRIPTOR id (`signals.source_id` /
-- `source_descriptors.descriptor_id`). Deliberately TEXT PK with NO foreign
-- key (same rationale as 0094): a record may exist for a source whose
-- descriptor was retired, and the record is keyed on the id carried by the
-- backing signals, not on descriptor lifecycle. One CURRENT record per source
-- (PK); the daily analyst refreshes it wholesale (upsert current set + delete
-- sources no longer seen), so there is no supersession chain — history lives
-- in the trace, not here (the record is a live readout).
--
-- THE MATH (all computed in Python by source_track_record.py, stored here):
--   * wins / losses          — over RESOLVED contentions (status='surfaced'),
--                              a WIN = the source carried the surfaced-winner
--                              value cluster; a LOSS = it carried ONLY losing
--                              (non-winner, non-junk) clusters. One outcome per
--                              (contention, source) so a chatty source counts
--                              once.
--   * contested_total        — wins + losses (the win-rate denominator).
--   * win_rate_raw           — wins / contested_total; NULL at zero sample.
--   * win_rate_smoothed      — Beta-Bernoulli posterior mean with a neutral
--                              Beta(2,2) prior: (wins+2)/(contested_total+4).
--                              Prior-DAMPED toward 0.5 by sample size, so a
--                              source with 2 contests is never rated extreme.
--                              THIS is the value the (flag-gated, default-OFF)
--                              arbiter tie-break seam consumes.
--   * win_rate_lower         — Wilson score interval lower bound (z=1.96): the
--                              CONSERVATIVE display estimate.
--   * low_sample             — contested_total < the sample-size floor (5).
--   * corroborated / total   — corroboration outcomes: of the value clusters
--                              this source carried in resolved groups, how many
--                              had >= 2 distinct backing sources (independent
--                              corroboration). corroboration_rate = ratio.
--   * lag_hours / sample_as_of — the CIRCULARITY-GUARD lag: only contentions
--                              surfaced STRICTLY BEFORE (computed_at - lag) fed
--                              this record. Recorded so the readout is honest
--                              about which slice it measured.
--
-- HARD CONSUMPTION RULE (operator, A6): this record feeds WEIGHTING /
-- TIE-BREAK / flags / display ONLY — NEVER the faithfulness score (trust !=
-- groundedness). Nothing in the verify/judge path reads this table. The
-- arbiter's consumption (the `_earned_track_record_weight` seam) is behind
-- `LEGBA_CONTENTION_EARNED_WEIGHT` (default 0 = OFF) and NEVER reads this
-- stored aggregate — it recomputes live, EXCLUDING the contention being
-- decided (the acyclicity guard). This table is the DISPLAY/EXPOSURE surface
-- (the assurance route `earned` section + the `/sources` `earned_win_rate`
-- projection), never the tie-break input.
--
-- SAFETY (idempotent, additive, forward-only): CREATE TABLE/INDEX IF NOT
-- EXISTS only; no existing table is touched; re-apply and cold-start are both
-- no-ops. The runner wraps this file in its own transaction and records it in
-- `legba_data_migrations` (no inline BEGIN/COMMIT — same as 0091-0098).

CREATE TABLE IF NOT EXISTS public.source_track_records (
    source_id           text PRIMARY KEY,        -- source descriptor id (no FK on purpose)
    wins                int  NOT NULL DEFAULT 0,
    losses              int  NOT NULL DEFAULT 0,
    contested_total     int  NOT NULL DEFAULT 0,  -- wins + losses (win-rate denominator)
    win_rate_raw        real,                     -- wins / contested_total; NULL at zero sample
    win_rate_smoothed   real NOT NULL DEFAULT 0.5,-- Beta(2,2) posterior mean (prior-damped)
    win_rate_lower      real NOT NULL DEFAULT 0.0,-- Wilson score lower bound (conservative)
    low_sample          boolean NOT NULL DEFAULT true,
    corroborated        int  NOT NULL DEFAULT 0,  -- carried clusters with >= 2 distinct sources
    corroboration_total int  NOT NULL DEFAULT 0,  -- carried clusters in resolved groups
    corroboration_rate  real,                     -- corroborated / total; NULL at zero
    lag_hours           real NOT NULL DEFAULT 72, -- circularity lag applied at compute time
    sample_as_of        timestamptz NOT NULL DEFAULT now(), -- (computed_at - lag) cutoff used
    computed_at         timestamptz NOT NULL DEFAULT now()
);

-- Contested sources first (the interesting ones), then by measured strength —
-- the ordering the assurance surfaces read for a "most/least earned" list.
CREATE INDEX IF NOT EXISTS source_track_records_rank_idx
    ON public.source_track_records (contested_total DESC, win_rate_lower DESC);
