-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 20261125T1000_proposed_edge_markers.sql
--
-- A small "this pair was finally refused" table, so the full candidate rows
-- that were refused can be removed without the pair being proposed and judged
-- again.
--
-- WHY. `proposed_edges` holds one row per candidate relationship between two
-- entities. Most are rejected or retired, and each keeps its evidence text and
-- lineage forever although nothing reads them: the only thing a finally
-- refused row still does is occupy the unique key
-- `uq_proposed_edges_triple` (lower(source), lower(target), relationship
-- type), which is what stops the producer proposing the same pair again.
-- That key is all the marker keeps.
--
-- THE KEY is the same triple, lowercased once at write time (`source_key`,
-- `target_key`) so the primary key is a plain btree and a lookup is an
-- equality on three columns. `final_status` is `rejected` or `retired`;
-- `reached_at` is when the row got there (its `reviewed_at`). `confidence` is
-- kept because the reifier's cut-out search adds up the confidence of every
-- co-occurrence edge a name has, finally refused ones included, and must keep
-- giving the same ranking once the full rows are gone.
--
-- HOW IT IS FILLED. A trigger on `proposed_edges` writes the marker whenever a
-- row becomes (or is inserted as) rejected or retired, so every writer that
-- finalises a rejection - the governance sweeps, the entity merge, a hand
-- UPDATE - is covered without each one knowing about this table. It removes
-- the marker again if the same row later leaves a final state (a merge folds a
-- live duplicate onto it). Existing rows are copied by
-- scripts/marker_final_rejections.py, which then deletes them from
-- `proposed_edges`. Nothing here deletes a candidate row, and nothing here
-- backfills: this file creates the table, the index and the trigger only.
--
-- NEVER DELETED FROM. A marker row is removed only by the trigger above (a row
-- that leaves a final state) or by hand.
--
-- SAFETY: CREATE ... IF NOT EXISTS / CREATE OR REPLACE only, and the triggers
-- are dropped and re-created by name, so applying twice changes nothing. No
-- BEGIN/COMMIT (the runner wraps the file).

CREATE TABLE IF NOT EXISTS public.proposed_edge_markers (
    source_key        text        NOT NULL,
    target_key        text        NOT NULL,
    relationship_type text        NOT NULL,
    final_status      text        NOT NULL,
    reached_at        timestamptz NOT NULL DEFAULT now(),
    confidence        real        NOT NULL DEFAULT 0,
    CONSTRAINT proposed_edge_markers_pkey
        PRIMARY KEY (source_key, target_key, relationship_type),
    CONSTRAINT proposed_edge_markers_final_status_check
        CHECK (final_status IN ('rejected', 'retired'))
);

-- The reifier's cut-out search looks a name up on either side of the pair; the
-- primary key serves the source side, this serves the target side.
CREATE INDEX IF NOT EXISTS idx_proposed_edge_markers_target
    ON public.proposed_edge_markers (target_key, relationship_type);

COMMENT ON TABLE public.proposed_edge_markers IS
    'One row per candidate relationship pair that was finally rejected or retired: the lowercased unique key of proposed_edges plus the final state and when it was reached. A pair with a marker is never proposed again.';

CREATE OR REPLACE FUNCTION public.proposed_edge_marker_sync()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.status IN ('rejected', 'retired') THEN
        -- First final state wins: a second route to the same pair does not
        -- rewrite when it was first refused.
        INSERT INTO public.proposed_edge_markers
            (source_key, target_key, relationship_type, final_status,
             reached_at, confidence)
        VALUES (lower(NEW.source_entity), lower(NEW.target_entity),
                NEW.relationship_type, NEW.status,
                COALESCE(NEW.reviewed_at, now()), COALESCE(NEW.confidence, 0))
        ON CONFLICT (source_key, target_key, relationship_type) DO NOTHING;
    ELSIF TG_OP = 'UPDATE'
          AND OLD.status IN ('rejected', 'retired')
          AND lower(OLD.source_entity) = lower(NEW.source_entity)
          AND lower(OLD.target_entity) = lower(NEW.target_entity)
          AND OLD.relationship_type = NEW.relationship_type THEN
        -- The same row left its final state: the pair is live again, so it
        -- must not stay blocked by a marker for a refusal that no longer holds.
        DELETE FROM public.proposed_edge_markers
         WHERE source_key = lower(OLD.source_entity)
           AND target_key = lower(OLD.target_entity)
           AND relationship_type = OLD.relationship_type;
    END IF;
    RETURN NULL;
END $$;

DROP TRIGGER IF EXISTS proposed_edge_marker_ins ON public.proposed_edges;
CREATE TRIGGER proposed_edge_marker_ins
    AFTER INSERT ON public.proposed_edges
    FOR EACH ROW
    WHEN (NEW.status IN ('rejected', 'retired'))
    EXECUTE FUNCTION public.proposed_edge_marker_sync();

DROP TRIGGER IF EXISTS proposed_edge_marker_upd ON public.proposed_edges;
CREATE TRIGGER proposed_edge_marker_upd
    AFTER UPDATE OF status, source_entity, target_entity, relationship_type
    ON public.proposed_edges
    FOR EACH ROW
    WHEN (NEW.status IN ('rejected', 'retired')
          OR OLD.status IN ('rejected', 'retired'))
    EXECUTE FUNCTION public.proposed_edge_marker_sync();
