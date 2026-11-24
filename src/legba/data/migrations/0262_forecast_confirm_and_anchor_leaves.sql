-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0262_forecast_confirm_and_anchor_leaves.sql
--
-- CONFIRMING A FORECAST RESOLUTION FROM THE CONSOLE, AND PROVABLE SEALS (F-1).
-- 0255–0261 belong to other lanes.
--
-- WHY
-- ---
-- (1) The resolution record (0244) takes four acts: proposal, confirmation,
--     override, correction. The console's write routes add a fifth that the
--     record could not hold: a person REJECTS a proposal with a reason. A
--     rejection closes nothing — the question stays open and the resolver
--     keeps reading it — but it is an act, so it is a row, never an edit.
--     The routes also record WHICH ROLE acted (owner / admin / contributor),
--     so a reader can be told "confirmed by an admin" without a name.
-- (2) The daily `receipt_anchor` folds every forecast seal into a Merkle
--     root and timestamps the root outside (OpenTimestamps). The root alone
--     cannot prove that ONE seal is inside it: that needs the ordered leaves
--     the root was folded from, and the leaves change every day (chain heads
--     move, rows are added). From the anchor after this migration on, the
--     handler stores each anchored tree's ordered leaves, so an inclusion
--     proof (the leaf, its sibling path, the root) can be served for any leaf
--     of any stored day. Days anchored BEFORE this migration have no stored
--     leaves; nothing here rewrites or back-fills a past anchor.
--
-- WHAT THIS ADDS
-- --------------
--   * `forecast_resolution_records.actor_role` (nullable) — the product role
--     of the person who made a confirmation / override / correction /
--     rejection through the routes: owner | admin | contributor. NULL on a
--     proposal, on the rule's own settlement and on host-script acts.
--   * the record-kind vocabulary widened by `rejection`; the CHECKs that keep
--     a closing act honest are restated so a rejection carries a reason and
--     the proposal it rejects, and writes no resolution.
--   * `receipt_anchor_trees` — one row per anchored day whose leaves are
--     stored: the root (equal to that day's `receipt_anchors.root_hash`),
--     the leaf count and the anchor contract version.
--   * `receipt_anchor_leaves` — the ordered leaves of a stored tree: position,
--     the leaf hash, and what it is a leaf of (kind, ref, the content hash it
--     binds), so anyone holding the rows can rebuild the leaf and the root.
--   * Both new tables are append-only (the 0242 `ledger_append_only_guard`).
--
-- SAFETY
-- ------
-- Additive and idempotent: ADD COLUMN IF NOT EXISTS; each CHECK is dropped
-- IF EXISTS and added again (every restated CHECK admits every row the 0244
-- version admitted); CREATE TABLE / INDEX IF NOT EXISTS; DROP TRIGGER IF
-- EXISTS + CREATE TRIGGER. Applying twice changes nothing. Routed through the
-- filename-gated runner (no inline BEGIN/COMMIT). Requires 0212
-- (receipt_anchors), 0242 (the guard function) and 0244. Ships EMPTY.

-- ---------------------------------------------------------------------------
-- (1) The resolution record: a rejection act, and the acting role.
-- ---------------------------------------------------------------------------

ALTER TABLE public.forecast_resolution_records
    ADD COLUMN IF NOT EXISTS actor_role text;

ALTER TABLE public.forecast_resolution_records
    DROP CONSTRAINT IF EXISTS forecast_records_actor_role_vocab;
ALTER TABLE public.forecast_resolution_records
    ADD CONSTRAINT forecast_records_actor_role_vocab CHECK (
        actor_role IS NULL OR actor_role IN ('owner', 'admin', 'contributor')
    );

ALTER TABLE public.forecast_resolution_records
    DROP CONSTRAINT IF EXISTS forecast_records_kind_vocab;
ALTER TABLE public.forecast_resolution_records
    ADD CONSTRAINT forecast_records_kind_vocab CHECK (
        record_kind IN ('proposal', 'confirmation', 'override', 'correction', 'rejection')
    );

-- Only a proposal (and the rejection that names one) may carry a reading
-- that is not yes / no / void: every act that closes a question names one.
ALTER TABLE public.forecast_resolution_records
    DROP CONSTRAINT IF EXISTS forecast_records_act_outcome;
ALTER TABLE public.forecast_resolution_records
    ADD CONSTRAINT forecast_records_act_outcome CHECK (
        record_kind IN ('proposal', 'rejection') OR outcome IN ('yes', 'no', 'void')
    );

-- A closing act wrote a resolution row; a rejection wrote none.
ALTER TABLE public.forecast_resolution_records
    DROP CONSTRAINT IF EXISTS forecast_records_act_resolution;
ALTER TABLE public.forecast_resolution_records
    ADD CONSTRAINT forecast_records_act_resolution CHECK (
        record_kind IN ('proposal', 'rejection') OR resolution_id IS NOT NULL
    );

ALTER TABLE public.forecast_resolution_records
    DROP CONSTRAINT IF EXISTS forecast_records_reason;
ALTER TABLE public.forecast_resolution_records
    ADD CONSTRAINT forecast_records_reason CHECK (
        record_kind NOT IN ('override', 'correction', 'rejection')
        OR length(btrim(coalesce(reason, ''))) > 0
    );

ALTER TABLE public.forecast_resolution_records
    DROP CONSTRAINT IF EXISTS forecast_records_rejection_shape;
ALTER TABLE public.forecast_resolution_records
    ADD CONSTRAINT forecast_records_rejection_shape CHECK (
        record_kind <> 'rejection'
        OR (basis_record_id IS NOT NULL AND resolution_id IS NULL)
    );

-- The act that answers a proposal (confirmation, override, rejection) is
-- read by the proposal it names.
CREATE INDEX IF NOT EXISTS idx_forecast_records_basis
    ON public.forecast_resolution_records USING btree (basis_record_id)
    WHERE basis_record_id IS NOT NULL;

-- ---------------------------------------------------------------------------
-- (2) The anchored trees' ordered leaves.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS public.receipt_anchor_trees (
    day             date PRIMARY KEY,
    root_hash       text NOT NULL,
    leaf_count      integer NOT NULL,
    anchor_version  text NOT NULL,
    recorded_at     timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT receipt_anchor_trees_root CHECK (root_hash ~ '^[0-9a-f]{64}$'),
    CONSTRAINT receipt_anchor_trees_count CHECK (leaf_count >= 0)
);

CREATE TABLE IF NOT EXISTS public.receipt_anchor_leaves (
    day           date NOT NULL REFERENCES public.receipt_anchor_trees (day),
    position      integer NOT NULL,
    leaf_hash     text NOT NULL,
    -- analyst_head | forecast_question | forecast_number | forecast_resolution
    -- | forecast_resolution_record | descriptor_audit_log
    kind          text NOT NULL,
    -- the analyst id, the row id, or the audit chain_seq
    ref           text NOT NULL,
    -- the hash the leaf binds: receipt_hash, content_sha256 or row_hash
    content_hash  text NOT NULL,
    PRIMARY KEY (day, position),
    CONSTRAINT receipt_anchor_leaves_position CHECK (position >= 0),
    CONSTRAINT receipt_anchor_leaves_hash CHECK (leaf_hash ~ '^[0-9a-f]{64}$')
);

-- A proof is looked up by the leaf it proves: the first stored day holding it.
CREATE INDEX IF NOT EXISTS idx_receipt_anchor_leaves_leaf
    ON public.receipt_anchor_leaves USING btree (leaf_hash, day);

DROP TRIGGER IF EXISTS trg_receipt_anchor_trees_append_only
    ON public.receipt_anchor_trees;
CREATE TRIGGER trg_receipt_anchor_trees_append_only
    BEFORE UPDATE OR DELETE ON public.receipt_anchor_trees
    FOR EACH ROW EXECUTE FUNCTION public.ledger_append_only_guard();

DROP TRIGGER IF EXISTS trg_receipt_anchor_leaves_append_only
    ON public.receipt_anchor_leaves;
CREATE TRIGGER trg_receipt_anchor_leaves_append_only
    BEFORE UPDATE OR DELETE ON public.receipt_anchor_leaves
    FOR EACH ROW EXECUTE FUNCTION public.ledger_append_only_guard();
