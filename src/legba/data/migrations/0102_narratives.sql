-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0102_narratives.sql
--
-- P4-1 (reified narrative objects) + P4-2 (source-echo propagation graph);
-- planning/PROGRAM_RECOMMENDATIONS_2026-07-24.md §A11 — the substrate-native
-- narrative analysis the operator amended in (NOT account-level platform
-- forensics, which is blocked on firehose data we do not hold). We already run
-- network analysis over a reified structure (signed nexuses, structural_balance
-- triads, the contention arbiter); this reifies NARRATIVES the same way.
--
-- WHAT A NARRATIVE IS
--   A narrative = a CONTESTED-CLAIM FAMILY: a claim + its variants. That is
--   EXACTLY one `fact_contention` group (migration 0055/0097) — a disputed
--   (subject_key, predicate_key) whose competing `fact_contention_values`
--   clusters ARE the variants. A narrative row is 1:1 with a contention group
--   (contention_id is the PK), enriched with the PROPAGATION dimension the
--   arbiter does not compute: which sources carry it, when each first published,
--   who led and who echoed, over what span.
--
-- WHY A SIDECAR (the source_track_records / fact_decay_states precedent, 0099/
-- 0098): every column here is DERIVED and fully RECOMPUTABLE from the contention
-- sidecar + the fact->signal->source lineage
-- (`fact_contention_values.supporting_fact_ids -> facts.derived_from ->
-- signals.source_id`, with the carrier's publish time at
-- `signals.payload->>'published_at'`, `fetched_at` as proxy). Drop both tables,
-- re-run the `narrative_mapper` deterministic analyst, identical content: that
-- recomputability is the proof narratives are a DERIVED READOUT, not primary.
-- Keeping them off `fact_contention` also keeps the honest wall: the arbiter is
-- the DETECT-ONLY owner of the dispute; the mapper is a read-only projection
-- over it that NEVER writes a facts / fact_contention / fact_contention_values
-- row (the never-mutate-facts invariant B15 the arbiter carries, extended).
--
-- HONESTY (carried verbatim in the analyst finding + the /v3/narratives route +
-- these comments):
--   * DETECT-ONLY. The mapper reads the contention sidecar + lineage and writes
--     ONLY these two derived tables + its own summary finding. It never mutates
--     a fact, a contention, or a value cluster.
--   * ECHO-LEAD IS DESCRIPTIVE, NOT CAUSAL. "Source B published this narrative
--     after source A, within N hours" is an observable publish-order timing
--     statement — NOT evidence that B copied A, nor a coordination claim. Both
--     may draw on a common wire, a shared origin event, or independent
--     reporting. Nothing here asserts coordination beyond co-carriage timing.
--   * PUBLISH TIME IS BEST-EFFORT. The echo GRAPH (P4-2) is computed ONLY from
--     PUBLISH-DATED carriers (both sides carry a real `published_at`) — a
--     two-tier honesty split mirroring geo_convergence_scan (point-trustworthy
--     vs country tier). A narrative's first/last-seen may fall back to
--     `fetched_at` when publish time is absent, but a fetch-time-only pair NEVER
--     mints an echo edge (we fetch many sources in one poll batch regardless of
--     their publish order — fetch order is not publish order).
--
-- SAFETY (idempotent, additive, forward-only): CREATE TABLE/INDEX IF NOT EXISTS
-- only; no existing table is touched; re-apply and cold-start are both no-ops.
-- The runner wraps this file in its own transaction and records it in
-- `legba_data_migrations` (no inline BEGIN/COMMIT — same as 0091-0101).
--
-- NUMBERING NOTE: 0100 is held by a concurrent branch; 0102 is this branch's
-- assigned slot. The runner discovers by sorted glob, so a gap is harmless.

-- ---------------------------------------------------------------------------
-- narratives — one reified contested-claim family (P4-1).
-- ---------------------------------------------------------------------------
-- Wholesale-refreshed each `narrative_mapper` run: upsert the current reified
-- set (keyed on contention_id), prune contention_ids no longer reified. No
-- supersession chain — the row is a LIVE readout (history lives in the trace).
CREATE TABLE IF NOT EXISTS public.narratives (
    contention_id          uuid PRIMARY KEY,          -- the fact_contention group id (1:1; no FK — recomputable)
    subject_key            text NOT NULL,             -- disputed subject (human-label half)
    predicate_key          text NOT NULL,             -- disputed predicate (human-label half)
    status                 text NOT NULL,             -- mirror of fact_contention.status: contested | surfaced | collapsed
    surfaced_value         text,                      -- the arbiter's CURRENT winner value (NULL = abstained / none surfaced)
    variant_count          int  NOT NULL DEFAULT 0,   -- distinct NON-junk value clusters (the competing positions/variants)
    carrier_source_count   int  NOT NULL DEFAULT 0,   -- distinct sources carrying ANY variant (via the lineage)
    publish_dated_source_count int NOT NULL DEFAULT 0,-- carriers with a real published_at (the echo-graph-eligible subset)
    signal_count           int  NOT NULL DEFAULT 0,   -- distinct carrier signals spanning the narrative
    fact_count             int  NOT NULL DEFAULT 0,   -- distinct carrier facts spanning the narrative
    first_seen_at          timestamptz,               -- earliest carrier time (published preferred, else fetched); narrative birth
    last_seen_at           timestamptz,               -- latest carrier time (most-recent activity)
    span_hours             real,                       -- (last_seen - first_seen) in hours
    lead_source_id         text,                       -- the publish-dated source that published FIRST (NULL if none publish-dated)
    lead_first_seen_at     timestamptz,                -- that lead source's first PUBLISH time
    max_echo_lag_hours     real,                       -- widest publish-dated follower lag vs the lead (echo-tail length)
    carriers               jsonb NOT NULL DEFAULT '[]'::jsonb,  -- ordered per-source detail (see narrative_mapper.py)
    variants               jsonb NOT NULL DEFAULT '[]'::jsonb,  -- per value-cluster detail
    opened_at              timestamptz,                -- fact_contention.opened_at (dispute-detection time)
    contention_surfaced_at timestamptz,                -- fact_contention.surfaced_at (winner surfaced time)
    computed_at            timestamptz NOT NULL DEFAULT now()
);

-- Recency-ranked list (the route's default ordering: most-recent activity first).
CREATE INDEX IF NOT EXISTS narratives_last_seen_idx
    ON public.narratives (last_seen_at DESC NULLS LAST);
-- Filter active disputes vs surfaced.
CREATE INDEX IF NOT EXISTS narratives_status_idx
    ON public.narratives (status);
-- Lead-source lookup (join to the echo graph's leader side).
CREATE INDEX IF NOT EXISTS narratives_lead_source_idx
    ON public.narratives (lead_source_id)
    WHERE lead_source_id IS NOT NULL;

-- ---------------------------------------------------------------------------
-- narrative_echo_edges — the source-echo propagation graph (P4-2).
-- ---------------------------------------------------------------------------
-- A DIRECTED aggregate over the narrative population: leader_source -> follower_
-- source, computed ONLY from publish-dated co-carriage. "Across the narratives
-- both sources carried (with real publish times on both), how often did the
-- follower publish AFTER the leader, and within echo_window_hours?" — the
-- descriptive counterpart of the arbiter, like structural_balance reads the
-- nexus graph. Two directed rows per unordered pair (A->B and B->A carry the
-- SAME co_carried but their OWN lead_count / lags / echo_ratio) — the asymmetry
-- IS the signal: A->B with high echo_ratio + low lag = "B systematically echoes
-- A within N hours" (a description, never a coordination verdict).
--
-- Wholesale-refreshed each run alongside narratives (upsert + prune).
CREATE TABLE IF NOT EXISTS public.narrative_echo_edges (
    leader_source_id     text NOT NULL,
    follower_source_id   text NOT NULL,
    co_carried           int  NOT NULL DEFAULT 0,   -- narratives BOTH sources carried, publish-dated on both (symmetric)
    lead_count           int  NOT NULL DEFAULT 0,   -- of co_carried, the leader published STRICTLY first (directional)
    follow_within_count  int  NOT NULL DEFAULT 0,   -- of lead_count, the follower echoed within echo_window_hours
    echo_ratio           real,                       -- follow_within_count / co_carried; NULL at zero co_carried
    median_lag_hours     real,                       -- median follow lag over the lead_count narratives; NULL if none
    mean_lag_hours       real,
    min_lag_hours        real,
    max_lag_hours        real,
    echo_window_hours    real NOT NULL,              -- the window follow_within_count was computed under
    systematic           boolean NOT NULL DEFAULT false,  -- co_carried>=floor AND echo_ratio>=ratio_floor (see handler)
    computed_at          timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (leader_source_id, follower_source_id)
);

-- The "who systematically echoes whom" read: systematic edges by echo strength.
CREATE INDEX IF NOT EXISTS narrative_echo_edges_systematic_idx
    ON public.narrative_echo_edges (echo_ratio DESC, co_carried DESC)
    WHERE systematic;
-- All out-edges from a leader (the leader-centric route filter).
CREATE INDEX IF NOT EXISTS narrative_echo_edges_leader_idx
    ON public.narrative_echo_edges (leader_source_id);
-- All in-edges to a follower (who does this source echo?).
CREATE INDEX IF NOT EXISTS narrative_echo_edges_follower_idx
    ON public.narrative_echo_edges (follower_source_id);
