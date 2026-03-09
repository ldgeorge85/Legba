#!/usr/bin/env bash
# Legba Diagnostics — run on the host to dump all service state.
# Usage: bash scripts/diagnostics.sh  (run from project root)
set -euo pipefail

DEPLOY="$(cd "$(dirname "$0")/.." && pwd)"
SEP="================================================================================"

section() { echo -e "\n${SEP}\n## $1\n${SEP}"; }

# ── Containers ──────────────────────────────────────────────────────────────────
section "CONTAINERS"
docker compose -f "$DEPLOY/docker-compose.yml" --profile airflow --profile dashboards ps \
  --format "table {{.Name}}\t{{.Status}}\t{{.Ports}}" 2>/dev/null || echo "(compose ps failed)"

# Check for active agent container
echo ""
echo "Agent container:"
docker ps --filter name=legba-agent-cycle --format "{{.Names}}  {{.Status}}  {{.RunningFor}}" 2>/dev/null || echo "  (none)"

# ── Redis ───────────────────────────────────────────────────────────────────────
section "REDIS"
echo "Cycle number: $(docker exec legba-redis-1 redis-cli GET legba:cycle_number 2>/dev/null || echo 'N/A')"
echo "Keys:"
docker exec legba-redis-1 redis-cli KEYS "legba:*" 2>/dev/null || echo "  (unavailable)"

# ── PostgreSQL — Tables ─────────────────────────────────────────────────────────
section "POSTGRES — GOALS"
docker exec legba-postgres-1 psql -U legba -d legba -c \
  "SELECT id, status, goal_type, priority, parent_id, created_at,
          data->>'description' AS description
   FROM goals ORDER BY created_at;" 2>/dev/null || echo "(unavailable)"

section "POSTGRES — FACTS"
docker exec legba-postgres-1 psql -U legba -d legba -c \
  "SELECT id, subject, predicate, value, confidence, source_cycle, created_at
   FROM facts ORDER BY created_at;" 2>/dev/null || echo "(unavailable)"

section "POSTGRES — SELF-MODIFICATIONS"
docker exec legba-postgres-1 psql -U legba -d legba -c \
  "SELECT id, cycle_number, status, file_path,
          data->>'action' AS action, data->>'description' AS description,
          created_at
   FROM modifications ORDER BY created_at;" 2>/dev/null || echo "(unavailable)"

# ── PostgreSQL — AGE Graph ──────────────────────────────────────────────────────
section "GRAPH — NODE COUNTS"
docker exec legba-postgres-1 psql -U legba -d legba -c "
  SET search_path = ag_catalog, public;
  SELECT * FROM cypher('legba_graph',
    \$\$MATCH (n) RETURN labels(n) AS type, count(*) AS cnt\$\$
  ) AS (type agtype, cnt agtype);" 2>/dev/null || echo "(unavailable)"

section "GRAPH — ALL NODES (name + type)"
docker exec legba-postgres-1 psql -U legba -d legba -c "
  SET search_path = ag_catalog, public;
  SELECT * FROM cypher('legba_graph',
    \$\$MATCH (n) RETURN labels(n) AS type, n.name AS name\$\$
  ) AS (type agtype, name agtype);" 2>/dev/null || echo "(unavailable)"

section "GRAPH — ALL EDGES"
docker exec legba-postgres-1 psql -U legba -d legba -c "
  SET search_path = ag_catalog, public;
  SELECT * FROM cypher('legba_graph',
    \$\$MATCH (a)-[r]->(b) RETURN a.name AS source, type(r) AS rel, b.name AS target\$\$
  ) AS (source agtype, rel agtype, target agtype);" 2>/dev/null || echo "(unavailable)"

# ── Qdrant ──────────────────────────────────────────────────────────────────────
section "QDRANT — COLLECTIONS"
for coll in legba_short_term legba_long_term legba_facts; do
  count=$(curl -sf "http://localhost:6333/collections/${coll}" 2>/dev/null \
    | python3 -c "import sys,json; print(json.load(sys.stdin)['result']['points_count'])" 2>/dev/null \
    || echo "N/A")
  echo "  ${coll}: ${count} points"
done

section "QDRANT — SHORT-TERM MEMORY (payloads)"
curl -sf "http://localhost:6333/collections/legba_short_term/points/scroll" \
  -H "Content-Type: application/json" \
  -d '{"limit":20,"with_payload":true,"with_vector":false}' 2>/dev/null \
  | python3 -m json.tool 2>/dev/null || echo "(unavailable)"

# ── OpenSearch (Agent) ──────────────────────────────────────────────────────────
section "OPENSEARCH — AGENT INDICES"
curl -sf "http://localhost:9200/_cat/indices?v" 2>/dev/null || echo "(unavailable)"

# ── OpenSearch (Audit) ──────────────────────────────────────────────────────────
section "OPENSEARCH — AUDIT INDICES"
curl -sf "http://localhost:9201/_cat/indices?v" 2>/dev/null || echo "(unavailable)"

section "OPENSEARCH — AUDIT DOC COUNT"
curl -sf "http://localhost:9201/legba-audit-*/_count" 2>/dev/null || echo '{"count":0}'

section "OPENSEARCH — AUDIT AGGREGATIONS"
curl -sf "http://localhost:9201/legba-audit-*/_search?size=0" \
  -H "Content-Type: application/json" \
  -d '{
    "aggs": {
      "by_cycle": {"terms": {"field": "cycle", "size": 50}},
      "by_event": {"terms": {"field": "event", "size": 20}}
    }
  }' 2>/dev/null | python3 -m json.tool 2>/dev/null || echo "(no audit data yet)"

# ── Cycle Logs (disk) ──────────────────────────────────────────────────────────
section "CYCLE LOGS — ON DISK"
echo "Active logs:"
docker exec legba-supervisor-1 ls -lh /logs/*.jsonl 2>/dev/null || echo "  (none)"
echo ""
echo "Archived:"
docker exec legba-supervisor-1 find /logs/archive -name "*.jsonl" -exec ls -lh {} \; 2>/dev/null || echo "  (none)"

# ── Supervisor Logs ─────────────────────────────────────────────────────────────
section "SUPERVISOR — RECENT LOGS (last 50 lines)"
docker compose -f "$DEPLOY/docker-compose.yml" logs supervisor --tail 50 2>/dev/null || echo "(unavailable)"

# ── NATS ────────────────────────────────────────────────────────────────────────
section "NATS — STATUS"
docker compose -f "$DEPLOY/docker-compose.yml" exec supervisor \
  python -m legba.supervisor.cli --shared /shared status 2>/dev/null || echo "(unavailable)"

# ── Summary ─────────────────────────────────────────────────────────────────────
section "SUMMARY"
cycle=$(docker exec legba-redis-1 redis-cli GET legba:cycle_number 2>/dev/null || echo "?")
goals=$(docker exec legba-postgres-1 psql -U legba -d legba -tAc "SELECT count(*) FROM goals;" 2>/dev/null || echo "?")
facts=$(docker exec legba-postgres-1 psql -U legba -d legba -tAc "SELECT count(*) FROM facts;" 2>/dev/null || echo "?")
mods=$(docker exec legba-postgres-1 psql -U legba -d legba -tAc "SELECT count(*) FROM modifications;" 2>/dev/null || echo "?")
graph_nodes=$(docker exec legba-postgres-1 psql -U legba -d legba -tAc "
  SET search_path = ag_catalog, public;
  SELECT * FROM cypher('legba_graph',
    \$\$MATCH (n) RETURN count(n)\$\$
  ) AS (cnt agtype);" 2>/dev/null | grep -v '^SET$' | tr -d ' ' || echo "?")
graph_edges=$(docker exec legba-postgres-1 psql -U legba -d legba -tAc "
  SET search_path = ag_catalog, public;
  SELECT * FROM cypher('legba_graph',
    \$\$MATCH ()-[r]->() RETURN count(r)\$\$
  ) AS (cnt agtype);" 2>/dev/null | grep -v '^SET$' | tr -d ' ' || echo "?")
stm=$(curl -sf "http://localhost:6333/collections/legba_short_term" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['result']['points_count'])" 2>/dev/null || echo "?")
ltm=$(curl -sf "http://localhost:6333/collections/legba_long_term" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['result']['points_count'])" 2>/dev/null || echo "?")
audit=$(curl -sf "http://localhost:9201/legba-audit-*/_count" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['count'])" 2>/dev/null || echo "0")
agent_running=$(docker ps --filter name=legba-agent-cycle --format "{{.RunningFor}}" 2>/dev/null || echo "no")

cat <<SUMMARY
  Cycle:            ${cycle}
  Agent running:    ${agent_running:-no}
  Goals:            ${goals}
  Facts:            ${facts}
  Self-mods:        ${mods}
  Graph nodes:      ${graph_nodes}
  Graph edges:      ${graph_edges}
  Short-term mem:   ${stm} vectors
  Long-term mem:    ${ltm} vectors
  Audit docs:       ${audit}
SUMMARY

echo ""
echo "Done. $(date -u +%Y-%m-%dT%H:%M:%SZ)"
