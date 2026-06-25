#!/bin/bash
# Legba Seed Export v2
# Exports ALL foundational + analytical state for a complete seed.
# Run against the OLD instance (restored from backup) before wiping.
#
# Includes: facts, entities (with geo), sources, graph (Cypher),
#           situations, hypotheses, watchlist
# Excludes: signals, events, signal_event_links (derived during burn-in)

set -e
SEED_DIR="${LEGBA_SEED_DIR:-/usr/local/deployments/legba/seed_data}"
PG="docker exec legba-postgres-1 psql -U legba -d legba"

echo "=== Legba Seed Export v2 ==="
echo ""

# ---- 1. Postgres tables (pg_dump --data-only) ----
echo "[1/6] Exporting Postgres tables (sources, entities, facts, entity_versions)..."
docker exec legba-postgres-1 pg_dump -U legba -d legba \
    --data-only --disable-triggers \
    -t sources -t entity_profiles -t entity_profile_versions \
    -t facts \
    -F c -f /tmp/seed_tables.dump 2>&1
docker cp legba-postgres-1:/tmp/seed_tables.dump "$SEED_DIR/seed_tables.dump"
echo "  Done."

# ---- 2. Situations ----
echo "[2/6] Exporting situations..."
$PG -c "\copy (SELECT * FROM situations WHERE status = 'active') TO '/tmp/seed_situations.csv' WITH CSV HEADER" 2>&1
docker cp legba-postgres-1:/tmp/seed_situations.csv "$SEED_DIR/seed_situations.csv"
NSIT=$($PG -t -c "SELECT count(*) FROM situations WHERE status='active'")
echo "  Exported $NSIT active situations."

# ---- 3. Hypotheses ----
echo "[3/6] Exporting hypotheses..."
$PG -c "\copy (SELECT * FROM hypotheses WHERE status = 'active') TO '/tmp/seed_hypotheses.csv' WITH CSV HEADER" 2>&1
docker cp legba-postgres-1:/tmp/seed_hypotheses.csv "$SEED_DIR/seed_hypotheses.csv"
NHYP=$($PG -t -c "SELECT count(*) FROM hypotheses WHERE status='active'")
echo "  Exported $NHYP active hypotheses."

# ---- 4. Watchlist ----
echo "[4/6] Exporting watchlist..."
$PG -c "\copy (SELECT * FROM watchlist WHERE active = true) TO '/tmp/seed_watchlist.csv' WITH CSV HEADER" 2>&1
docker cp legba-postgres-1:/tmp/seed_watchlist.csv "$SEED_DIR/seed_watchlist.csv"
NW=$($PG -t -c "SELECT count(*) FROM watchlist WHERE active=true")
echo "  Exported $NW active watchlist items."

# ---- 5. Graph (Cypher → CSV, not pg_dump) ----
echo "[5/6] Exporting graph via Cypher..."

# Export nodes
$PG -c "
LOAD 'age'; SET search_path = ag_catalog, public;
\copy (
    SELECT * FROM cypher('legba_graph', \$\$
        MATCH (n) RETURN id(n), n.name, n.entity_type, properties(n)
    \$\$) AS (id agtype, name agtype, entity_type agtype, props agtype)
) TO '/tmp/seed_graph_nodes.csv' WITH CSV HEADER
" 2>&1
docker cp legba-postgres-1:/tmp/seed_graph_nodes.csv "$SEED_DIR/seed_graph_nodes.csv"

# Export edges
$PG -c "
LOAD 'age'; SET search_path = ag_catalog, public;
\copy (
    SELECT * FROM cypher('legba_graph', \$\$
        MATCH (a)-[r]->(b) RETURN id(a), id(b), type(r), a.name, b.name, properties(r)
    \$\$) AS (from_id agtype, to_id agtype, rel_type agtype, from_name agtype, to_name agtype, props agtype)
) TO '/tmp/seed_graph_edges.csv' WITH CSV HEADER
" 2>&1
docker cp legba-postgres-1:/tmp/seed_graph_edges.csv "$SEED_DIR/seed_graph_edges.csv"

NNODES=$($PG -t -c "LOAD 'age'; SET search_path = ag_catalog, public; SELECT * FROM cypher('legba_graph', \$\$MATCH (n) RETURN count(n)\$\$) AS (c agtype);")
NEDGES=$($PG -t -c "LOAD 'age'; SET search_path = ag_catalog, public; SELECT * FROM cypher('legba_graph', \$\$MATCH ()-[r]->() RETURN count(r)\$\$) AS (c agtype);")
echo "  Exported $NNODES nodes, $NEDGES edges."

# ---- 6. Schema (for reference, not used in import) ----
echo "[6/6] Exporting schema..."
docker exec legba-postgres-1 pg_dump -U legba -d legba --schema-only -F p -f /tmp/seed_schema.sql 2>&1
docker cp legba-postgres-1:/tmp/seed_schema.sql "$SEED_DIR/seed_schema.sql"
echo "  Done."

# ---- Summary ----
echo ""
echo "=== Export Summary ==="
ls -lh "$SEED_DIR"/seed_*.{dump,csv,sql} 2>/dev/null
echo ""
echo "=== Counts ==="
$PG -t -c "
SELECT 'sources' as t, count(*) FROM sources WHERE status='active'
UNION ALL SELECT 'entities', count(*) FROM entity_profiles
UNION ALL SELECT 'facts', count(*) FROM facts WHERE superseded_by IS NULL
UNION ALL SELECT 'situations', count(*) FROM situations WHERE status='active'
UNION ALL SELECT 'hypotheses', count(*) FROM hypotheses WHERE status='active'
UNION ALL SELECT 'watchlist', count(*) FROM watchlist WHERE active=true
ORDER BY t;
"
echo "Graph: $NNODES nodes, $NEDGES edges"
echo ""
echo "=== Export complete ==="
