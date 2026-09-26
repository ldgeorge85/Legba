-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0207_graph_arcs.sql
--
-- DATA MODEL V3 / P4b (spec §4.2) — the CROSS-LAYER GRAPH PROJECTION.
--
-- `graph_arcs` is ONE ROW PER ARC: a whole-rebuild projection of every
-- cross-layer arc in the substrate, plane-tagged, temporally queryable, with
-- a staleness scalar that fails loud. The builder is
-- `deterministic_handlers/graph_projector.py`: one `INSERT ... SELECT` per
-- source table into `graph_arcs_new`, then one `ALTER TABLE ... RENAME` swap
-- inside a transaction. There is NO incremental path and there never will be
-- — every dead mirror in this codebase is an incremental mirror with a delete
-- path that was never written; a structure only ever built by SELECT cannot
-- diverge in content, only in age, and age is one published scalar.
--
-- ── NO FOREIGN KEYS, DELIBERATELY ──────────────────────────────────────────
-- A projection is DISPOSABLE. Every endpoint here is a uuid read FROM a table
-- that does carry referential integrity; pinning FKs onto a rebuilt table
-- would (a) make the build order-sensitive and (b) let a retention sweep's
-- cascade reach INTO the projection, destroying a read model for a substrate
-- bookkeeping decision. Nothing is authoritative here — this is the one place
-- Decision 1's FK discipline does not apply. The `src_table`/`src_id` pair
-- points back at the authoritative row for any arc a reader wants to audit.
--
-- ── `plane` IS THE LOAD-BEARING COLUMN ─────────────────────────────────────
-- 74 % of the ~4.0 M arcs are output LINEAGE, not world state (spec §4.1
-- consequence 1): a world-graph query that defaults to "all arcs" would
-- report who cited whom as the state of the world — the edge_family lesson
-- (Decision 3) arriving in a second domain. `plane` is therefore NOT NULL and
-- `world` is the default filter on every world-facing reader.
--
-- ── DELIBERATE EXCLUSIONS (spec §4.1, each for a stated reason) ────────────
--   * proposed_edges — a candidate queue, not a graph (the judge's Decision
--     4); promoted rows already cross into entity_edges, which IS projected.
--   * journal_entries — off-chain by construction; a projection that included
--     it would let a graph walk surface a journal node, which is exactly the
--     property the journal design forbids.
--   * target/signal routing — fan-out is ephemeral; a signal is routed, never
--     copied.
--
-- The runner wraps this file in its own transaction and records it in
-- `legba_data_migrations` (no inline BEGIN/COMMIT — same as 0204/0206).

CREATE TABLE IF NOT EXISTS public.graph_arcs (
    from_kind   text NOT NULL,   -- signal|entity|fact|event|situation|finding|hypothesis|narrative|situation_event
    from_id     uuid NOT NULL,
    to_kind     text NOT NULL,
    to_id       uuid NOT NULL,
    arc_type    text NOT NULL,
    plane       text NOT NULL
                CHECK (plane IN ('world','evidence','lineage')),
    family      text,            -- entity_edges.edge_family, event_edges.edge_type, link role, …
    polarity    smallint NOT NULL DEFAULT 0
                CHECK (polarity IN (-1,0,1)),
    confidence  real,
    valid_from  timestamptz,
    valid_until timestamptz,     -- so a per-hop temporal predicate is EXPRESSIBLE
    src_table   text NOT NULL,   -- the authoritative table this arc was read from
    src_id      uuid,            -- the authoritative row, when the source has one
    PRIMARY KEY (from_kind, from_id, to_kind, to_id, arc_type)
);

COMMENT ON TABLE public.graph_arcs IS
    'DATA MODEL V3 / P4b: the whole-rebuild cross-layer graph projection. '
    'Disposable — rebuilt from source tables that DO carry integrity; that is '
    'why there are no foreign keys here. plane is NOT NULL and ''world'' is '
    'the default filter on every world-facing reader: 74% of arcs are output '
    'lineage, not world state. proposed_edges and journal_entries are '
    'excluded by construction (spec §4.1).';

-- "All OPEN arcs out of this node, on one plane" — the ego/walk read.
CREATE INDEX IF NOT EXISTS idx_graph_arcs_out
    ON public.graph_arcs (from_kind, from_id, plane)
    WHERE valid_until IS NULL;
CREATE INDEX IF NOT EXISTS idx_graph_arcs_in
    ON public.graph_arcs (to_kind, to_id, plane)
    WHERE valid_until IS NULL;
-- The world-plane neighbour join the scoped snapshots make.
CREATE INDEX IF NOT EXISTS idx_graph_arcs_world
    ON public.graph_arcs (from_id, to_id)
    WHERE plane = 'world' AND valid_until IS NULL;
-- "What did each source contribute" — the S-1 parity read and audit path.
CREATE INDEX IF NOT EXISTS idx_graph_arcs_source
    ON public.graph_arcs (src_table);

-- The singleton build receipt: one row, rewritten by every completed build.
-- `projected_at` is the published staleness scalar — a reader past
-- LEGBA_GRAPH_PROJECTION_MAX_AGE_SECONDS refuses with `projection_stale`.
-- `build_seconds` is the E4 trigger reading (the sitting's rebuild-cost gauge).
-- `source_counts` is the per-src_table actual the S-1 closed loop compares
-- against live counts.
CREATE TABLE IF NOT EXISTS public.graph_arcs_meta (
    id            boolean PRIMARY KEY DEFAULT true CHECK (id),
    projected_at  timestamptz NOT NULL,
    arc_count     bigint NOT NULL,
    source_counts jsonb NOT NULL DEFAULT '{}',
    build_seconds real NOT NULL
);

COMMENT ON TABLE public.graph_arcs_meta IS
    'DATA MODEL V3 / P4b: the one-row build receipt for graph_arcs. '
    'projected_at is the staleness scalar every reader publishes; '
    'build_seconds is the E4 trigger reading; source_counts is the '
    'expected-vs-actual parity surface for the S-1 loop.';
