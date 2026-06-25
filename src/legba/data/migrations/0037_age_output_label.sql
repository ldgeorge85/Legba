-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0037_age_output_label.sql
--
-- AGE :DerivedFrom mirroring — Output vlabel + DerivedFrom elabel
-- (graph-and-data Wave-1b, item 2; REVIEW_CONSOLIDATED_2026-06-16 §3.6 / D3).
--
-- WHY:
--   Analyst-output provenance is the `analyst_outputs.derived_from uuid[]`
--   array (the relational source of truth; recursive-CTE lineage works today).
--   The `age_hook` parameter on provenance.writes.write_analyst_output was
--   SCAFFOLDED but never called, and the graph carried no Output-lineage edges.
--   Wiring `(:Output)-[:DerivedFrom]->(:Output)` lets lineage be walked as a
--   graph (variable-depth MATCH) alongside the CTE. Per D3 the edge MERGE rides
--   the same write connection / is operator-gated (LEGBA_AGE_DERIVED_FROM), so
--   it never silently taxes the analyst-write critical path.
--
-- WHAT:
--   Seed the `Output` vertex label and `DerivedFrom` edge label into the
--   existing `legba_graph` (the 9 vlabels + 14 elabels from 0001 are unchanged;
--   these two are additive). The hook MERGEs Output vertices keyed by the
--   output row's UUID (property `id`) and a DerivedFrom edge per parent UUID.
--
-- SAFETY (idempotent, additive, CREATE-only):
--   Each label is created only if absent (the exact guard 0001 uses). No
--   existing label/graph is touched; re-applying is a no-op. No row mutation.

LOAD 'age';
SET search_path = ag_catalog, "$user", public;

-- Output vertex label (provenance graph node — one per analyst_outputs row).
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM ag_catalog.ag_label
        WHERE name = 'Output'
          AND graph = (SELECT graphid FROM ag_catalog.ag_graph WHERE name = 'legba_graph')
    ) THEN
        PERFORM ag_catalog.create_vlabel('legba_graph', 'Output');
    END IF;
END
$$;

-- DerivedFrom edge label (output → parent-output provenance edge).
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM ag_catalog.ag_label
        WHERE name = 'DerivedFrom'
          AND graph = (SELECT graphid FROM ag_catalog.ag_graph WHERE name = 'legba_graph')
    ) THEN
        PERFORM ag_catalog.create_elabel('legba_graph', 'DerivedFrom');
    END IF;
END
$$;
