-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0264_drop_unused_indexes.sql
--
-- Thirty-two secondary indexes nobody reads (lane P-8, the data-layers review
-- 03c §1.6 / §6 change 8). Each was listed by scripts/report_unused_indexes.py
-- against live on 2026-11-14 (statistics window since 2026-08-29, 77 days):
-- zero scans over at least 30 days of observation, NOT unique, backing NO
-- constraint, NOT on graph_arcs (rebuilt by swap every hour, so its counters
-- cannot judge an index), NOT on an owner-held table (facts, the forecast
-- generations, the alert and byte stores), NOT on an empty table, NOT named
-- anywhere in code outside the migrations and NOT pinned by a test. Together
-- about 308 MB on live, 293 MB of it in five indexes on the large tables:
--   idx_entity_profiles_derived_from   174 MB  gin (derived_from)
--   analyst_traces_input_gin            68 MB  gin (input_row_refs)
--   idx_entity_profiles_name_trgm       23 MB  gin trigram, live entities
--   idx_entity_profiles_block_key       11 MB  entity_block_key(), live entities
--   signals_entity_classes_gin           9 MB  gin (entity_classes)
-- Every index named here is re-creatable from its original definition with
-- `scripts/drop_unused_indexes.py --recreate` (the definitions are the live
-- pg_get_indexdef output, pinned to this file by
-- tests/data_pkg/test_schema_hygiene.py).
--
-- SAFETY ON LIVE-SIZED TABLES (the 0247 / 0251 / 0257 pattern, reversed)
-- ---------------------------------------------------------------------
-- The runner executes this file inside ONE transaction, so nothing here can be
-- CONCURRENTLY. Each index is dropped here ONLY when its table is small (a
-- fresh substrate, a test database, and on live every table below except
-- signals, entity_profiles, analyst_traces, entity_edges and governor_events:
-- at most 50,000 rows). On a larger table this file skips the drop with a
-- NOTICE, and the merger drops the SAME index out of transaction with
-- `scripts/drop_unused_indexes.py --drop` (DROP INDEX CONCURRENTLY IF EXISTS;
-- writers are never blocked). Re-apply is a no-op; no inline BEGIN/COMMIT.

SET LOCAL lock_timeout = '10s';

DO $$
BEGIN
    -- Small tables on live as on a fresh database.
    IF (SELECT count(*) FROM (SELECT 1 FROM public.action_pack_descriptors LIMIT 50001) s) <= 50000 THEN
        DROP INDEX IF EXISTS public.action_pack_descriptors_schema_idx;
    END IF;
    IF (SELECT count(*) FROM (SELECT 1 FROM public.alert_sink_deliveries LIMIT 50001) s) <= 50000 THEN
        DROP INDEX IF EXISTS public.idx_asd_target_emitted;
    ELSE
        RAISE NOTICE '0264: alert_sink_deliveries has more than 50000 rows; run scripts/drop_unused_indexes.py --drop (CONCURRENTLY).';
    END IF;
    IF (SELECT count(*) FROM (SELECT 1 FROM public.budget_ledger LIMIT 50001) s) <= 50000 THEN
        DROP INDEX IF EXISTS public.budget_ledger_cost_estimate_idx;
    ELSE
        RAISE NOTICE '0264: budget_ledger has more than 50000 rows; run scripts/drop_unused_indexes.py --drop (CONCURRENTLY).';
    END IF;
    IF (SELECT count(*) FROM (SELECT 1 FROM public.collection_descriptors LIMIT 50001) s) <= 50000 THEN
        DROP INDEX IF EXISTS public.collection_descriptors_schema_idx;
    END IF;
    IF (SELECT count(*) FROM (SELECT 1 FROM public.collection_requirements LIMIT 50001) s) <= 50000 THEN
        DROP INDEX IF EXISTS public.idx_collection_requirements_open;
    END IF;
    IF (SELECT count(*) FROM (SELECT 1 FROM public.correctness_labels LIMIT 50001) s) <= 50000 THEN
        DROP INDEX IF EXISTS public.idx_correctness_labels_unit;
    END IF;
    IF (SELECT count(*) FROM (SELECT 1 FROM public.desk_baselines LIMIT 50001) s) <= 50000 THEN
        DROP INDEX IF EXISTS public.desk_baselines_deviation_idx;
    END IF;
    IF (SELECT count(*) FROM (SELECT 1 FROM public.discovery_state LIMIT 50001) s) <= 50000 THEN
        DROP INDEX IF EXISTS public.discovery_state_descriptor_idx;
        DROP INDEX IF EXISTS public.discovery_state_discovery_idx;
    END IF;
    IF (SELECT count(*) FROM (SELECT 1 FROM public.entity_edges_unresolved LIMIT 50001) s) <= 50000 THEN
        DROP INDEX IF EXISTS public.idx_entity_edges_unresolved_reason;
    ELSE
        RAISE NOTICE '0264: entity_edges_unresolved has more than 50000 rows; run scripts/drop_unused_indexes.py --drop (CONCURRENTLY).';
    END IF;
    IF (SELECT count(*) FROM (SELECT 1 FROM public.events LIMIT 50001) s) <= 50000 THEN
        DROP INDEX IF EXISTS public.idx_events_asof;
    ELSE
        RAISE NOTICE '0264: events has more than 50000 rows; run scripts/drop_unused_indexes.py --drop (CONCURRENTLY).';
    END IF;
    IF (SELECT count(*) FROM (SELECT 1 FROM public.hypotheses LIMIT 50001) s) <= 50000 THEN
        DROP INDEX IF EXISTS public.idx_hypotheses_analyst_id;
    ELSE
        RAISE NOTICE '0264: hypotheses has more than 50000 rows; run scripts/drop_unused_indexes.py --drop (CONCURRENTLY).';
    END IF;
    IF (SELECT count(*) FROM (SELECT 1 FROM public.narrative_echo_edges LIMIT 50001) s) <= 50000 THEN
        DROP INDEX IF EXISTS public.narrative_echo_edges_systematic_idx;
    END IF;
    IF (SELECT count(*) FROM (SELECT 1 FROM public.narratives LIMIT 50001) s) <= 50000 THEN
        DROP INDEX IF EXISTS public.narratives_lead_source_idx;
    END IF;
    IF (SELECT count(*) FROM (SELECT 1 FROM public.retention_policies LIMIT 50001) s) <= 50000 THEN
        DROP INDEX IF EXISTS public.idx_retention_policies_enabled;
    END IF;
    IF (SELECT count(*) FROM (SELECT 1 FROM public.review_flags LIMIT 50001) s) <= 50000 THEN
        DROP INDEX IF EXISTS public.idx_review_flags_open;
    END IF;
    IF (SELECT count(*) FROM (SELECT 1 FROM public.seed_batches LIMIT 50001) s) <= 50000 THEN
        DROP INDEX IF EXISTS public.idx_seed_batches_source;
    END IF;
    IF (SELECT count(*) FROM (SELECT 1 FROM public.situations LIMIT 50001) s) <= 50000 THEN
        DROP INDEX IF EXISTS public.idx_situations_analyst_id;
    END IF;
    IF (SELECT count(*) FROM (SELECT 1 FROM public.source_credibility LIMIT 50001) s) <= 50000 THEN
        DROP INDEX IF EXISTS public.idx_source_credibility_last_updated;
        DROP INDEX IF EXISTS public.idx_source_credibility_score;
    END IF;
    IF (SELECT count(*) FROM (SELECT 1 FROM public.source_layers LIMIT 50001) s) <= 50000 THEN
        DROP INDEX IF EXISTS public.idx_source_layers_source;
    END IF;
    IF (SELECT count(*) FROM (SELECT 1 FROM public.stack_credentials LIMIT 50001) s) <= 50000 THEN
        DROP INDEX IF EXISTS public.stack_credentials_created_idx;
    END IF;

    -- The five large tables on live: dropped here only on a small database.
    IF (SELECT count(*) FROM (SELECT 1 FROM public.analyst_traces LIMIT 50001) s) <= 50000 THEN
        DROP INDEX IF EXISTS public.analyst_traces_input_gin;
    ELSE
        RAISE NOTICE '0264: analyst_traces has more than 50000 rows; analyst_traces_input_gin is NOT dropped here. Run scripts/drop_unused_indexes.py --drop (CONCURRENTLY).';
    END IF;
    IF (SELECT count(*) FROM (SELECT 1 FROM public.entity_edges LIMIT 50001) s) <= 50000 THEN
        DROP INDEX IF EXISTS public.idx_entity_edges_decay;
        DROP INDEX IF EXISTS public.idx_entity_edges_evidence;
    ELSE
        RAISE NOTICE '0264: entity_edges has more than 50000 rows; its two unused indexes are NOT dropped here. Run scripts/drop_unused_indexes.py --drop (CONCURRENTLY).';
    END IF;
    IF (SELECT count(*) FROM (SELECT 1 FROM public.entity_profiles LIMIT 50001) s) <= 50000 THEN
        DROP INDEX IF EXISTS public.idx_entity_profiles_analyst_id;
        DROP INDEX IF EXISTS public.idx_entity_profiles_block_key;
        DROP INDEX IF EXISTS public.idx_entity_profiles_derived_from;
        DROP INDEX IF EXISTS public.idx_entity_profiles_name_trgm;
        DROP INDEX IF EXISTS public.idx_entity_profiles_target_id;
    ELSE
        RAISE NOTICE '0264: entity_profiles has more than 50000 rows; its five unused indexes are NOT dropped here. Run scripts/drop_unused_indexes.py --drop (CONCURRENTLY).';
    END IF;
    IF (SELECT count(*) FROM (SELECT 1 FROM public.governor_events LIMIT 50001) s) <= 50000 THEN
        DROP INDEX IF EXISTS public.governor_events_account_idx;
    ELSE
        RAISE NOTICE '0264: governor_events has more than 50000 rows; governor_events_account_idx is NOT dropped here. Run scripts/drop_unused_indexes.py --drop (CONCURRENTLY).';
    END IF;
    IF (SELECT count(*) FROM (SELECT 1 FROM public.signals LIMIT 50001) s) <= 50000 THEN
        DROP INDEX IF EXISTS public.signals_entity_classes_gin;
    ELSE
        RAISE NOTICE '0264: signals has more than 50000 rows; signals_entity_classes_gin is NOT dropped here. Run scripts/drop_unused_indexes.py --drop (CONCURRENTLY).';
    END IF;
END
$$;
