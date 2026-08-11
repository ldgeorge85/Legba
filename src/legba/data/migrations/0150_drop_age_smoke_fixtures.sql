-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0150_drop_age_smoke_fixtures.sql
--
-- Delete the 27 June-17 smoke-test fixtures from `legba_graph`
-- (graph-debate JUDGE_SYNTHESIS §4.3 item 2, live defect #2; roadmap K-G3).
--
-- WHY:
--   `legba_graph` has never held a production row. Everything in it was
--   written by the 2026-06-17 AGE smoke suite: 27 vertices (SearchOrg1,
--   SearchPerson1, Boss/Worker, TestMerge, CountryA/CountryB, NodeX/Y/Z,
--   PathA/B/C, Isolated1/2, Center/Neighbor1/2, CypherTest, PatternA/B/C,
--   FromCypher, plus NATO/OTAN/Russia/Ukraine) and 10 edges between them.
--
--   An empty graph that ERRORS is honest. A graph holding someone else's test
--   fixtures ANSWERS — `/api/v1/graph/path` walks them, finds nothing, and
--   renders a confident `detail="no path"` that is really a statement about a
--   smoke-test island. Deleting the fixtures plus the fail-loud change in
--   `graph_paths.shortest_path_with_broker` turns that silent lie into an
--   explicit `graph_unpopulated` error.
--
-- WHAT:
--   Delete the fixture edges, then the fixture vertices, from the label-
--   inheritance parents (`_ag_label_edge` / `_ag_label_vertex`) — a DELETE on
--   the parent reaches every inheriting label table, so no label needs naming.
--   The GRAPH and all 11 vertex / 21 edge LABEL definitions from
--   0001_baseline.sql + 0037_age_output_label.sql are left untouched: this
--   migration removes data, never schema. Re-running is a no-op.
--
-- SAFETY — the guard is the point:
--   The fixture signature is `properties.created_at` on 2026-06-17T18:2x (26
--   of 27) plus the one undated smoke vertex `{"name": "FromCypher",
--   "origin": "test"}`. If ANY vertex fails to match that signature, this
--   migration RAISES rather than deleting: a non-fixture vertex means the
--   graph has been fed since this was written, and a blind wipe would destroy
--   production data. Verified read-only against the live substrate on
--   2026-08-03: 27 total, 26 dated + 1 undated = 27 matched, 0 unmatched.

LOAD 'age';
SET search_path = public, ag_catalog;

DO $$
DECLARE
    v_total      bigint;
    v_fixtures   bigint;
    v_edges      bigint;
    v_vertices   bigint;
BEGIN
    IF NOT EXISTS (SELECT 1 FROM ag_catalog.ag_graph WHERE name = 'legba_graph') THEN
        RAISE NOTICE '0150: legba_graph does not exist — nothing to clean';
        RETURN;
    END IF;

    SELECT count(*),
           count(*) FILTER (
               WHERE properties::text LIKE '%"created_at": "2026-06-17T18:2%'
                  OR properties::text = '{"name": "FromCypher", "origin": "test"}'
           )
      INTO v_total, v_fixtures
      FROM legba_graph._ag_label_vertex;

    IF v_total = 0 THEN
        RAISE NOTICE '0150: legba_graph already holds no vertices — no-op';
        RETURN;
    END IF;

    IF v_total <> v_fixtures THEN
        RAISE EXCEPTION
            '0150 refusing to delete: legba_graph holds % vertices but only % '
            'carry the 2026-06-17 smoke-fixture signature. The graph has been '
            'fed since this migration was written — review before deleting.',
            v_total, v_fixtures;
    END IF;

    -- Edges first (FK-free heaps, but delete the dependents anyway so the
    -- receipt is countable and the intermediate state is never dangling).
    WITH gone AS (
        DELETE FROM legba_graph._ag_label_edge e
         WHERE EXISTS (
                   SELECT 1 FROM legba_graph._ag_label_vertex v
                    WHERE v.id IN (e.start_id, e.end_id)
                      AND (v.properties::text LIKE '%"created_at": "2026-06-17T18:2%'
                           OR v.properties::text = '{"name": "FromCypher", "origin": "test"}')
               )
        RETURNING 1
    )
    SELECT count(*) INTO v_edges FROM gone;

    WITH gone AS (
        DELETE FROM legba_graph._ag_label_vertex v
         WHERE v.properties::text LIKE '%"created_at": "2026-06-17T18:2%'
            OR v.properties::text = '{"name": "FromCypher", "origin": "test"}'
        RETURNING 1
    )
    SELECT count(*) INTO v_vertices FROM gone;

    RAISE NOTICE '0150: deleted % smoke-fixture vertices and % edges from legba_graph',
        v_vertices, v_edges;
END
$$;
