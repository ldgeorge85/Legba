-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0103_desk_baselines.sql
--
-- P3-7 (A7 borrowable #7 — the CAST-recipe per-desk STATISTICAL BASELINE;
-- planning/PROGRAM_RECOMMENDATIONS_2026-07-24.md §A7): a falsifiable
-- quantitative PRIOR per desk (g20 + watch) that each desk's LLM reads can
-- agree or argue with, and that the P1-3 baseline_deviation trigger + the eval
-- surfacing can read. Built over OUR OWN substrate (signals + analyst_outputs)
-- — NO external data, NO ACLED dependency; the ACLED CAST methodology is only
-- the shape (feature recipe → robust baseline expectation → deviation).
--
-- HARD HONESTY FRAME (this table is NOT a forecast): the rows here are a
-- descriptive statistical baseline — the trailing-window expected rate + an
-- uncertainty band + whether the CURRENT window deviates. This does NOT reopen
-- forecasting-as-claim (frozen — DROP D2 / A4 succeeds it). No Brier, no skill
-- score, no probability-of-event: `expected` is a trailing mean rate, the
-- deviation is the useful anomaly signal, and nothing here is ever surfaced as
-- a free-text prediction. The `desk_baseline` deterministic analyst writes it;
-- the /eval/desk_baselines route projects it.
--
-- WHY A SIDECAR (not columns on the target descriptor / a payload row) — the
-- 0098 fact_decay_states / 0099 source_track_records precedent: every value
-- here is DERIVED and fully RECOMPUTABLE from the trailing signals +
-- analyst_outputs (drop the table, re-run `desk_baseline`, identical content).
-- That recomputability is the proof it is a live READOUT, not primary state.
-- One CURRENT baseline per (desk_id, metric) (PK); the daily analyst refreshes
-- it wholesale (upsert the current set + prune desks no longer present), so
-- there is no supersession chain — history lives in the trace, not here.
--
-- IDENTITY: `desk_id` is the target DESCRIPTOR id (`target_descriptors.
-- descriptor_id`, e.g. country_g20_us). Deliberately TEXT with NO foreign key
-- (the 0094/0099 rationale): the readout is keyed on the desk id, not on
-- descriptor lifecycle, and a retired desk simply stops being recomputed and
-- is pruned. `metric` ∈ {signal_volume_24h, high_sev_findings_24h} — the same
-- two metrics the P1-3 baseline_deviation trigger measures, so this table is a
-- faithful persistent mirror of that ephemeral computation.
--
-- THE MATH (all computed in Python by desk_baseline.py, stored here — a robust
-- dependency-light estimator; lightgbm/scipy are NOT in the image, so this is
-- pure-stdlib statistics, NOT a heavy gradient model):
--   * expected        — trailing-window MEAN daily rate over the baseline days
--                       (bucket 0 = current 24h; buckets 1..N = the baseline).
--                       The point prior for the next window; mean-centred so
--                       the band shape matches the P1-3 trigger it feeds.
--   * center_median   — trailing MEDIAN (a spike-robust alternative centre;
--                       stored for the honest read, not the band centre).
--   * robust_sigma    — max(sample stddev, sqrt(mean)). The sqrt(mean) is the
--                       POISSON floor (a count process with rate λ has σ=√λ):
--                       it stops a steady desk's σ collapsing to ≈0 and the
--                       band collapsing with it — the one deliberate robustness
--                       improvement over the trigger's raw stddev.
--   * band_low/high   — expected ∓ n_sigma·robust_sigma (low floored at 0).
--   * current         — the observed current-24h window count (bucket 0).
--   * deviation       — 'within' | 'above' | 'below':
--                         above = current clears band_high AND the ABSOLUTE
--                                 min-count floor (mirrors alert_trigger_scan.
--                                 baseline_exceeds — a quiet desk's 2-vs-0.1
--                                 blip can never read as a deviation);
--                         below = current under band_low AND the BASELINE mean
--                                 itself clears the floor (an unusually-quiet
--                                 desk is only a signal when it normally is not
--                                 quiet — collection-gap flavour);
--                         within otherwise.
--   * deviation_sigma — signed (current − expected)/robust_sigma; NULL when
--                       robust_sigma = 0. The "running Kσ above/below" number
--                       the eval surfacing shows.
--   * min_current_floor — the absolute floor applied (10 signals / 3 high-sev,
--                       mirrored from the trigger); recorded for auditability.
--   * insufficient_history — HONESTY FLAG: the band rests on thin history
--                       (fewer than the min active-days had any events). It
--                       does NOT suppress an absolute-floor 'above' (a real
--                       spike over the floor is still a real deviation) — it
--                       only warns the reader the band is weak.
--   * spillover_current — neighbour-desk feature: the summed current-24h signal
--                       volume of the geographically-adjacent desks (a coarse
--                       static land-border adjacency, in-code, used ONLY as a
--                       feature input — never surfaced as a geographic claim).
--   * features        — jsonb bag of the rest of the CAST feature recipe:
--                       lags {1,7,28}, rolling means {7,28}, active/nonzero
--                       days, hours_since_last_high_sev, neighbour desk ids.
--                       Kept in one jsonb (not more flat columns) because it is
--                       the audit provenance of the estimate, not a query axis.
--
-- CONSUMPTION: the deviation feeds the P1-3 baseline_deviation trigger (as a
-- persistent prior it MAY read) + gives a desk LLM read a falsifiable number to
-- argue with; it NEVER touches the faithfulness score and is NEVER rendered as
-- a forecast. Nothing in the verify/judge path reads this table.
--
-- SAFETY (idempotent, additive, forward-only): CREATE TABLE/INDEX IF NOT
-- EXISTS only; no existing table is touched; re-apply and cold-start are both
-- no-ops. NUMBERING: 0100/0102 are held by concurrent/prior branches; 0103 is
-- this branch's number — the runner discovers by sorted glob, so a gap is
-- harmless (same note as 0099). The runner wraps this file in its own
-- transaction and records it in `legba_data_migrations` (no inline
-- BEGIN/COMMIT — same as 0091-0101).

CREATE TABLE IF NOT EXISTS public.desk_baselines (
    desk_id              text NOT NULL,             -- target descriptor id (no FK on purpose)
    metric               text NOT NULL,             -- signal_volume_24h | high_sev_findings_24h
    geo                  jsonb NOT NULL DEFAULT '[]'::jsonb,  -- the desk's scope.geo ISO2 list (audit)
    baseline_days        int  NOT NULL DEFAULT 28,  -- trailing baseline depth used
    n_sigma              real NOT NULL DEFAULT 2.0, -- band half-width in robust sigmas
    expected             real NOT NULL DEFAULT 0,   -- trailing MEAN rate (the prior)
    center_median        real NOT NULL DEFAULT 0,   -- trailing median (robust centre readout)
    robust_sigma         real NOT NULL DEFAULT 0,   -- max(sample stddev, sqrt(mean)) — Poisson-floored
    band_low             real NOT NULL DEFAULT 0,   -- max(0, expected - n_sigma*robust_sigma)
    band_high            real NOT NULL DEFAULT 0,   -- expected + n_sigma*robust_sigma
    current              real NOT NULL DEFAULT 0,   -- observed current-24h window count
    deviation            text NOT NULL DEFAULT 'within',  -- within | above | below
    deviation_sigma      real,                      -- signed (current-expected)/robust_sigma; NULL if σ=0
    min_current_floor    real NOT NULL DEFAULT 0,   -- absolute floor applied (mirrors the trigger)
    sample_days          int  NOT NULL DEFAULT 0,   -- baseline buckets used
    active_days          int  NOT NULL DEFAULT 0,   -- baseline buckets with > 0 events
    insufficient_history boolean NOT NULL DEFAULT true,   -- band rests on thin history (honesty flag)
    spillover_current    real NOT NULL DEFAULT 0,   -- neighbour-desk current-24h signal volume (feature)
    features             jsonb NOT NULL DEFAULT '{}'::jsonb,  -- lags / rolling means / neighbours (audit)
    computed_at          timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (desk_id, metric)
);

-- The eval surfacing reads "most-deviating first": non-within rows, then by the
-- magnitude of the running deviation. A partial index over the non-within rows
-- keeps that list off a full-table scan as the desk set grows.
CREATE INDEX IF NOT EXISTS desk_baselines_deviation_idx
    ON public.desk_baselines (deviation, abs(deviation_sigma) DESC)
    WHERE deviation <> 'within';
