-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0107_review_flags_bearing_edges.sql
--
-- KW-1 (review-flag plane). Two sidecar tables over the forward-consumption
-- index (migration 0106). Both are NON-MUTATING: neither ever touches the
-- flagged/linked outputs themselves — detect-and-mark, never rewrite (the
-- standing arbiter discipline).
--
-- ── review_flags ───────────────────────────────────────────────────────────
-- One row = the marker "consumer C was founded on F, which MOVED at T, and C
-- predates T — a human/later-cycle should re-review C". Append-only:
--   * output_id     — C, the consumer whose foundation moved (uuid, no FK —
--                     the 0106 no-FK posture).
--   * founded_on_id — F, the consumed foundation that moved.
--   * moved_at      — T, when F moved (superseded / decayed / contradicted).
--   * reason        — human-readable why (open vocabulary, e.g.
--                     'foundation_superseded', 'foundation_decayed').
--   * closed_by/closed_at — flags are closed by SUPERSESSION ONLY: when a
--                     LATER output supersedes C (or re-composes over the moved
--                     foundation), the closer stamps closed_by = that later
--                     output's id + closed_at. NEVER deleted — there is no
--                     DELETE path anywhere, and the trigger below makes the
--                     posture schema-enforced, not conventional. The paired
--                     CHECK makes a half-closed row (one of closed_by/
--                     closed_at without the other) unrepresentable.
-- The partial UNIQUE index gives the flag writer natural idempotency: at most
-- ONE OPEN flag per (output, foundation) pair — a re-scan upserts nothing new
-- while the prior flag is still open; a NEW flag for the same pair is
-- representable again only after the old one closed (a genuinely new episode).
--
-- ── bearing_edges ──────────────────────────────────────────────────────────
-- Dated, typed edges "NEW evidence bears on OLD claim" — the matcher-produced
-- link the review plane walks. Every column NOT NULL: an edge without dates,
-- weight, planes, or provenance is UNREPRESENTABLE by schema (the design-doc
-- invariant), because an undated/unprovenanced bearing edge cannot be audited
-- or decayed.
--   * edge_kind        — 'bears_on' (default; open for later kinds e.g.
--                        'supersedes_claim').
--   * src_*            — the NEW evidence side: kind ('finding' | 'signal' |
--                        'fact' — open vocabulary), row uuid, and its as-of
--                        time (produced/fetched).
--   * dst_*            — the OLD claim side, same shape.
--   * weight           — matcher confidence in the bearing (real).
--   * planes           — WHICH matcher planes produced it, e.g.
--                        {vector,entity,geo}; CHECK-enforced non-empty (an
--                        edge no plane produced is not an edge).
--   * provenance_class — 'live' (production matcher run) | 'exemplar'
--                        (curated/gold example), CHECK-enforced closed
--                        vocabulary — the never-pool-gold discipline needs the
--                        class distinguishable at the schema layer.
--   * matcher_version  — which matcher build emitted it (reproducibility).
-- UNIQUE (src_id, dst_id, edge_kind): one edge of a kind per directed pair;
-- a re-match refreshes via ON CONFLICT rather than accumulating duplicates.
--
-- SAFETY (idempotent, additive, forward-only): CREATE TABLE/INDEX IF NOT
-- EXISTS + CREATE OR REPLACE FUNCTION only; the DROP TRIGGER IF EXISTS +
-- CREATE TRIGGER pair is the standard idempotent-trigger idiom and touches
-- only the trigger THIS file owns. No existing table is touched; re-apply and
-- cold-start are both no-ops. The runner wraps this file in its own
-- transaction and records it in `legba_data_migrations` (no inline
-- BEGIN/COMMIT — same as 0091/0106).

CREATE TABLE IF NOT EXISTS public.review_flags (
    id            uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    output_id     uuid        NOT NULL,
    founded_on_id uuid        NOT NULL,
    moved_at      timestamptz NOT NULL,
    reason        text        NOT NULL,
    created_at    timestamptz NOT NULL DEFAULT now(),
    closed_by     uuid,
    closed_at     timestamptz,
    -- Closed = BOTH stamps present; open = both absent. Half-closed rows are
    -- unrepresentable.
    CONSTRAINT review_flags_close_pair
        CHECK ((closed_by IS NULL) = (closed_at IS NULL))
);

-- At most one OPEN flag per (consumer, foundation) pair — writer idempotency.
CREATE UNIQUE INDEX IF NOT EXISTS uq_review_flags_open_pair
    ON public.review_flags (output_id, founded_on_id)
    WHERE closed_at IS NULL;

-- "Which consumers are flagged over foundation F?" (forward review walk).
CREATE INDEX IF NOT EXISTS idx_review_flags_founded_on
    ON public.review_flags (founded_on_id, created_at DESC);

-- "Open flags, newest first" — the operator/review-surface list read.
CREATE INDEX IF NOT EXISTS idx_review_flags_open
    ON public.review_flags (created_at DESC)
    WHERE closed_at IS NULL;

-- Schema-enforced never-delete posture: flags close by supersession
-- (closed_by/closed_at), they do not disappear. Any DELETE — app bug, ad-hoc
-- SQL, future code path — fails loud here.
CREATE OR REPLACE FUNCTION public.review_flags_forbid_delete()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION
        'review_flags rows are never deleted — close by supersession '
        '(set closed_by = the later output id + closed_at)';
END;
$$;

DROP TRIGGER IF EXISTS trg_review_flags_forbid_delete ON public.review_flags;
CREATE TRIGGER trg_review_flags_forbid_delete
    BEFORE DELETE ON public.review_flags
    FOR EACH ROW EXECUTE FUNCTION public.review_flags_forbid_delete();

CREATE TABLE IF NOT EXISTS public.bearing_edges (
    id               uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    edge_kind        text        NOT NULL DEFAULT 'bears_on',
    src_kind         text        NOT NULL,
    src_id           uuid        NOT NULL,
    src_as_of        timestamptz NOT NULL,
    dst_kind         text        NOT NULL,
    dst_id           uuid        NOT NULL,
    dst_as_of        timestamptz NOT NULL,
    weight           real        NOT NULL,
    planes           text[]      NOT NULL
                     CONSTRAINT bearing_edges_planes_nonempty
                     CHECK (planes <> '{}'::text[]),
    provenance_class text        NOT NULL
                     CONSTRAINT bearing_edges_provenance_class
                     CHECK (provenance_class IN ('live', 'exemplar')),
    matcher_version  text        NOT NULL,
    created_at       timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_bearing_edges_pair UNIQUE (src_id, dst_id, edge_kind)
);

-- "What NEW evidence bears on old claim D?" — the review plane's read.
CREATE INDEX IF NOT EXISTS idx_bearing_edges_dst
    ON public.bearing_edges (dst_id, created_at DESC);
