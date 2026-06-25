-- Legba Seed Export
-- Exports only structural/foundational data, NOT derived state.
-- Includes: facts, entities, sources, graph
-- Excludes: signals, events, situations, hypotheses, watchlist, journal, briefs

\echo '=== Legba Seed Export ==='
\echo ''

-- 1. Sources (full table — needed for ingestion)
\echo 'Exporting sources...'
\copy (SELECT * FROM sources WHERE status = 'active') TO '/tmp/seed_sources.csv' WITH CSV HEADER;

-- 2. Entity profiles (full table)
\echo 'Exporting entity_profiles...'
\copy (SELECT * FROM entity_profiles) TO '/tmp/seed_entities.csv' WITH CSV HEADER;

-- 3. Facts (active only, no superseded)
\echo 'Exporting facts...'
\copy (SELECT * FROM facts WHERE superseded_by IS NULL) TO '/tmp/seed_facts.csv' WITH CSV HEADER;

-- 4. Entity profile versions (needed for integrity)
\echo 'Exporting entity_profile_versions...'
\copy (SELECT * FROM entity_profile_versions WHERE entity_id IN (SELECT id FROM entity_profiles)) TO '/tmp/seed_entity_versions.csv' WITH CSV HEADER;

\echo ''
\echo '=== Export complete ==='
