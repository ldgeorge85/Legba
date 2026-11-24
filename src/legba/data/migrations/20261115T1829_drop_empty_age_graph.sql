-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 20261115T1829_drop_empty_age_graph.sql
--
-- Drops the Apache AGE graph `legba_graph` (34 label tables, 0 vertices, 0
-- edges on the reference deployment) IF AND ONLY IF it is empty when this
-- applies. Owner decision 2026-11-15 (design note DATA_LAYERS_2026-11-14,
-- section 6 item 6 and question 4): the empty graph database may be deleted;
-- `graph_arcs` superseded it, its writers were switched off, its readers
-- answered "unpopulated".
--
-- THIS MIGRATION WRITES NOTHING AND DUMPS NOTHING. The dump is a separate
-- script, `scripts/dump_age_graph.py --dump DIR`, which the roll runs BEFORE
-- the registry applies this file; an empty graph dumps to an empty file set,
-- and the script refuses to call a non-empty one "safe".
--
-- THE GUARD. For every label table of the graph the migration counts rows. Any
-- row at all aborts the migration with an exception naming the label and the
-- count, the transaction rolls back, nothing is dropped, and the ledger does
-- not record the file, so the next start retries (and refuses again) until a
-- person has looked. There is no "drop anyway" path here.
--
-- WHAT STAYS. The `age` EXTENSION and the `ag_catalog` schema (so every
-- connection's `LOAD 'age'` and the pool's search_path keep working). Removing
-- the extension is the later stock-Postgres image swap (SEAMS: age-extension-
-- remains). On a database where the graph is already absent this is a no-op,
-- so it is safe to re-apply and safe on a fresh database (0001 creates the
-- empty graph; this drops it).

LOAD 'age';
SET search_path = public, ag_catalog;

DO $$
DECLARE
    v_graph   CONSTANT text := 'legba_graph';
    v_label   record;
    v_rows    bigint;
    v_total   bigint := 0;
    v_tables  integer := 0;
BEGIN
    IF NOT EXISTS (SELECT 1 FROM ag_catalog.ag_graph WHERE name = v_graph) THEN
        RAISE NOTICE 'drop_empty_age_graph: % does not exist - nothing to drop', v_graph;
        RETURN;
    END IF;

    FOR v_label IN
        SELECT l.name AS label_name,
               l.relation::regclass::text AS rel_name
          FROM ag_catalog.ag_label l
          JOIN ag_catalog.ag_graph g ON g.graphid = l.graph
         WHERE g.name = v_graph
         ORDER BY l.name
    LOOP
        EXECUTE format('SELECT count(*) FROM %s', v_label.rel_name) INTO v_rows;
        v_tables := v_tables + 1;
        v_total  := v_total + v_rows;
        IF v_rows > 0 THEN
            RAISE EXCEPTION
                'drop_empty_age_graph REFUSED: label % of graph % holds % rows '
                '(the graph is not empty; it was removed on the premise that it '
                'holds nothing). Nothing was dropped. Dump it with '
                'scripts/dump_age_graph.py and decide before retrying.',
                v_label.label_name, v_graph, v_rows;
        END IF;
    END LOOP;

    PERFORM ag_catalog.drop_graph(v_graph, true);
    RAISE NOTICE 'drop_empty_age_graph: dropped % (% label tables, % rows)',
        v_graph, v_tables, v_total;
END
$$;
