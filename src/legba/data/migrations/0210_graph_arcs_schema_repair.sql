-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0210_graph_arcs_schema_repair.sql
--
-- P4b-D1 (2026-09-23). The projector's first live builds ran with UNQUALIFIED
-- table names under the runtime pool's `search_path = ag_catalog, "$user",
-- public`: `CREATE TABLE graph_arcs_new` landed in ag_catalog, the swap renamed
-- public.graph_arcs (migration 0207) to graph_arcs_old and DROPPED it, and the
-- projection lived at ag_catalog.graph_arcs — readable only through the search
-- path. The projector, the readers and the route are schema-qualified from
-- this roll on; this migration moves the live projection back to public so the
-- first qualified build finds `public.graph_arcs` to LIKE-copy and swap.
--
-- Idempotent and guarded: it moves the table only when the AGE-schema copy
-- exists AND public has none; it never drops a populated table. Stale staging
-- or old tables from an interrupted build are removed in both schemas.
DO $$
BEGIN
    IF to_regclass('ag_catalog.graph_arcs') IS NOT NULL
       AND to_regclass('public.graph_arcs') IS NULL THEN
        ALTER TABLE ag_catalog.graph_arcs SET SCHEMA public;
        RAISE NOTICE '0210: ag_catalog.graph_arcs moved to public';
    ELSIF to_regclass('ag_catalog.graph_arcs') IS NOT NULL THEN
        RAISE NOTICE '0210: both ag_catalog.graph_arcs and public.graph_arcs exist — left as is (operator decision)';
    END IF;
    DROP TABLE IF EXISTS ag_catalog.graph_arcs_new;
    DROP TABLE IF EXISTS ag_catalog.graph_arcs_old;
    DROP TABLE IF EXISTS public.graph_arcs_new;
    DROP TABLE IF EXISTS public.graph_arcs_old;
END $$;
