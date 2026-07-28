-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0098_fact_decay_states.sql
--
-- C4 (substrate dynamics — fact confidence decay + corroborations-as-
-- sightings; planning/PROGRAM_RECOMMENDATIONS_2026-07-24.md §C4): the
-- per-fact DECAY READOUT sidecar the `fact_decay_scan` deterministic analyst
-- stamps daily.
--
-- NUMBERING NOTE: 0097 is reserved by concurrent work; 0098 is this branch's
-- number. The runner discovers by sorted glob, so a gap is harmless.
--
-- WHY A SIDECAR (not columns on `facts`) — the 0055 fact_contention
-- precedent: the readout is DERIVED and fully RECOMPUTABLE (stored
-- confidence × the per-class MISP retention curve at days-since-last-
-- sighting), refreshed wholesale on every scan. Keeping it off `facts` means
-- the scan NEVER touches a facts row — `confidence` / `updated_at` /
-- `confidence_components` stay exactly as the write path left them (the C4
-- hard rule: decay is a readout, never a mutation — unlike the legacy
-- `fact_decay` sweep, which subtracts from the stored scalar). Drop the
-- table, re-run the scan, identical content: that recomputability is the
-- proof it is derived, not primary.
--
-- SIGHTINGS: `last_sighting_at` is DERIVED at scan time — max backing-signal
-- observation time (COALESCE(signals.fetched_at, signals.created_at) over
-- facts.derived_from, which BOTH fact producers union corroborating signal
-- ids into on every same-triple re-assert), falling back to facts.created_at
-- (birth = first sighting). It is recorded here as part of the readout
-- (which derivation fed the curve), NOT as a new write path on `facts`.
--
-- DECAY STATE VOCABULARY (closed, CHECK-enforced): fresh | aging | stale |
-- revoke_candidate — reaction points on the retention curve, plus the
-- ABSOLUTE revoke threshold on decayed_confidence (the MISP score cutoff).
-- Consumers: the flag-gated grounding seam (`LEGBA_FACT_DECAY_WEIGHTING`,
-- default OFF — when ON, revoke_candidate rows are excluded from the
-- grounding preamble and decayed_confidence annotates the rendered lines).
--
-- IDENTITY + LIFECYCLE: one row per OPEN fact (fact_id PK). The FK is
-- ON DELETE CASCADE so a hard fact delete (migrations route bulk DELETEs)
-- never strands a readout; rows for facts that CLOSE (superseded/expired)
-- are pruned by the scan itself.
--
-- SAFETY (idempotent, additive, forward-only): CREATE TABLE/INDEX IF NOT
-- EXISTS only; no existing table is touched; re-apply and cold-start are both
-- no-ops. The runner wraps this file in its own transaction and records it in
-- `legba_data_migrations` (no inline BEGIN/COMMIT — same as 0094/0096).

CREATE TABLE IF NOT EXISTS public.fact_decay_states (
    fact_id             uuid PRIMARY KEY
                          REFERENCES public.facts(id) ON DELETE CASCADE,
    -- The derived readout (NEVER written back to facts.confidence).
    decayed_confidence  real NOT NULL,
    decay_state         text NOT NULL
                          CHECK (decay_state IN
                                 ('fresh', 'aging', 'stale', 'revoke_candidate')),
    -- Curve inputs at stamp time (audit: which class/lifetime produced this).
    decay_class         text NOT NULL,
    retention           real NOT NULL,       -- raw curve factor in [0, 1]
    lifetime_days       real NOT NULL,       -- effective (source-type multiplier applied)
    -- The derived sighting that fed the curve + which derivation produced it.
    last_sighting_at    timestamptz,
    sighting_source     text NOT NULL DEFAULT 'created_at'
                          CHECK (sighting_source IN ('signal', 'created_at')),
    -- The stored confidence AT STAMP TIME (audit trail proving non-mutation:
    -- compare against facts.confidence at read time).
    stored_confidence   real NOT NULL,
    computed_at         timestamptz NOT NULL DEFAULT now(),
    run_id              uuid,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now()
);

-- The summary/consumption axes: counts per state; revoke-candidate lookups.
CREATE INDEX IF NOT EXISTS idx_fact_decay_states_state
    ON public.fact_decay_states (decay_state);
