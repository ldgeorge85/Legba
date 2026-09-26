-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0203_event_edges.sql — DATA MODEL V3 / P0, event-to-event and
-- situation-to-event edges (spec §2.1, §2.2, §2.4).
--
-- NEW TABLES (two, additive; nothing existing is touched):
--
--   * `event_edges` — a relation between two events. Direction is REAL:
--     `caused_by` reads "src was caused by dst", `part_of` reads "src is a
--     sub-event of dst", `evolves_from` reads "src succeeds dst".
--     `correlated_with` and `contradicts` are SYMMETRIC and are stored
--     canonically — `src_event_id < dst_event_id` by uuid ordering, enforced
--     by the CHECK on those two types — the same defect class as the
--     Russia|Russian × Ukraine|Ukrainian dyad inflation that migration 0078
--     cleaned out of nexuses. caused_by and contradicts are admitted by the
--     vocabulary but are NOT produced until SEAM #56 (attribution /
--     contradiction semantics) lands — P0 and P1 mint deterministic edges
--     only (correlated_with, part_of, evolves_from).
--
--     The frame is the house temporal contract: an OPEN edge is
--     (valid_until IS NULL AND superseded_by IS NULL), uniqueness holds only
--     among open rows, and closing an edge is an UPDATE, not a delete — same
--     shape as entity_edges (0143).
--
--   * `situation_event_links` — the bridge between the frame world and the
--     occurrence world (§2.4): a situation tracks many events, an event may
--     be tracked by more than one situation. situation_events (0184) is NOT
--     reused — it is the trajectory ledger with a NOT NULL source_output_id
--     and its own UNIQUE key; event membership is a three-column junction.
--     P0 writes no rows; the surface exists for P1's materializer and for
--     provenance reads.
--
-- SAFETY: CREATE IF NOT EXISTS everywhere; re-apply is a no-op.

-- ---------------------------------------------------------------------------
-- 1. event_edges — temporal relations between events
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS public.event_edges (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    src_event_id  uuid NOT NULL REFERENCES public.events(id) ON DELETE CASCADE,
    dst_event_id  uuid NOT NULL REFERENCES public.events(id) ON DELETE CASCADE,
    edge_type     text NOT NULL CHECK (edge_type IN
        ('part_of','caused_by','evolves_from','correlated_with','contradicts')),
    confidence    real NOT NULL DEFAULT 0.5,
    why           text NOT NULL DEFAULT '',
    derived_from  uuid[] NOT NULL DEFAULT '{}',
    valid_from    timestamptz,
    valid_until   timestamptz,
    superseded_by uuid REFERENCES public.event_edges(id) ON DELETE SET NULL,
    analyst_id    text,
    analyst_version text,
    run_id        uuid,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT event_edges_no_self CHECK (src_event_id <> dst_event_id),
    -- Symmetric types are stored canonically (smaller uuid first) so the same
    -- relation cannot land twice, once in each direction. Direction is real
    -- for part_of / caused_by / evolves_from, so the CHECK exempts them.
    CONSTRAINT event_edges_symmetric_canonical CHECK (
        edge_type NOT IN ('correlated_with','contradicts')
        OR src_event_id < dst_event_id)
);

COMMENT ON TABLE public.event_edges IS
    'DATA MODEL V3 / P0: a relation between two events. Direction is real — '
    'src caused_by dst, src part_of dst, src evolves_from dst — except '
    'correlated_with / contradicts, which are symmetric and stored canonically '
    '(src_event_id < dst_event_id, enforced by CHECK) so a pair cannot land '
    'twice. Open edge = valid_until IS NULL AND superseded_by IS NULL; '
    'uniqueness holds only among open rows (the 0143 contract). caused_by and '
    'contradicts are not produced until SEAM #56 lands; P0/P1 mint '
    'correlated_with / part_of / evolves_from only.';

CREATE UNIQUE INDEX IF NOT EXISTS uq_event_edges_open
    ON public.event_edges (src_event_id, dst_event_id, edge_type)
    WHERE valid_until IS NULL AND superseded_by IS NULL;
CREATE INDEX IF NOT EXISTS idx_event_edges_out
    ON public.event_edges (src_event_id, edge_type)
    WHERE valid_until IS NULL AND superseded_by IS NULL;
CREATE INDEX IF NOT EXISTS idx_event_edges_in
    ON public.event_edges (dst_event_id, edge_type)
    WHERE valid_until IS NULL AND superseded_by IS NULL;

-- ---------------------------------------------------------------------------
-- 2. situation_event_links — the frame/occurrence junction
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS public.situation_event_links (
    situation_id uuid NOT NULL REFERENCES public.situations(id) ON DELETE CASCADE,
    event_id     uuid NOT NULL REFERENCES public.events(id)     ON DELETE CASCADE,
    relevance    real NOT NULL DEFAULT 1.0,
    derived_from uuid[] NOT NULL DEFAULT '{}',
    created_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (situation_id, event_id)
);

COMMENT ON TABLE public.situation_event_links IS
    'DATA MODEL V3 / P0: the junction from a situation (a persistent frame '
    'composed of findings) to an event (a bounded occurrence evidenced by '
    'signals). situation_events is deliberately NOT reused — it is the 0184 '
    'trajectory ledger with a NOT NULL source_output_id and its own UNIQUE '
    'key, not a membership table. P0 writes no rows; the surface exists for '
    'P1''s materializer and for provenance reads.';
