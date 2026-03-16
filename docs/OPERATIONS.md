# Legba — Operations Runbook

*Operational procedures, deployment, monitoring, and maintenance.*

---

## 1. Environment

- **Host:** Debian 12, 8 vCPU, 16GB RAM
- **IP:** `<your-host>`
- **Working dir:** `<project-root>`
- **Docker Compose project:** `legba` (always use `-p legba`)
- **All commands run on host** — do NOT install packages on host, use containers

---

## 2. Container Stack

| Container | Image | Purpose | Ports |
|-----------|-------|---------|-------|
| supervisor | legba-supervisor | Agent lifecycle, heartbeat, audit | — |
| agent | legba-agent | Ephemeral, one per cycle | — |
| ui | legba-ui | Operator console (FastAPI+htmx) | 8501 |
| redis | redis:7-alpine | Transient state, journal, reports | 6379 |
| postgres | apache/age:latest | Structured data, entity graph | 5432 |
| qdrant | qdrant/qdrant:latest | Episodic/semantic memory | 6333-6334 |
| nats | nats:2-alpine | Event bus, messaging | 4222, 8222 |
| opensearch | opensearchproject/opensearch:2 | Events, documents | 9200, 9600 |
| opensearch-audit | opensearchproject/opensearch:2 | Audit logs (agent-inaccessible) | 9201 |

---

## 3. Common Operations

### Start / Stop
```bash
docker compose -p legba up -d          # Start everything
docker compose -p legba down            # Stop everything (keep volumes)
docker compose -p legba stop supervisor # Stop supervisor (pauses cycles)
docker compose -p legba logs supervisor -f  # Follow supervisor logs
```

### Check Status
```bash
docker compose -p legba ps                              # Container health
docker compose -p legba logs supervisor --tail 30        # Recent cycles
docker exec legba-redis-1 redis-cli GET legba:cycle_number  # Current cycle
```

### Full Reset (wipe all data, fresh start)
```bash
# 1. Back up volumes first (see Backups section)
# 2. Tear down everything
docker compose -p legba down -v
# 3. Clean any remaining volumes (compose sometimes leaves orphans)
docker volume ls --format '{{.Name}}' | grep legba | xargs -r docker volume rm
# 4. Rebuild and start
docker compose -p legba build agent supervisor
docker compose -p legba up -d
```

### Hot Deploy (code changes, no data reset)
```bash
# 1. Build new images
docker compose -p legba build agent supervisor
# 2. Stop supervisor (waits for current cycle to finish)
docker compose -p legba stop supervisor
# 3. Clear the agent code volume (entrypoint re-seeds from image)
docker run --rm -v legba_agent_code:/agent alpine rm -rf /agent/src /agent/pyproject.toml
# 4. Restart supervisor (it launches agent with new code)
docker compose -p legba up -d supervisor
```

### UI-only Deploy (no cycle disruption)
```bash
docker compose -p legba build ui
docker compose -p legba up -d ui
```

---

## 4. Backups

### Create Volume Backup
```bash
docker compose -p legba stop supervisor   # Pause cycles first
docker run --rm \
  -v legba_pg_data:/data/pg:ro \
  -v legba_redis_data:/data/redis:ro \
  -v legba_qdrant_data:/data/qdrant:ro \
  -v legba_opensearch_data:/data/os:ro \
  -v legba_opensearch_audit_data:/data/os_audit:ro \
  -v legba_log_data:/data/logs:ro \
  -v legba_agent_code:/data/agent_code:ro \
  -v legba_nats_data:/data/nats:ro \
  -v legba_workspace_data:/data/workspace:ro \
  -v legba_shared_data:/data/shared:ro \
  -v legba_airflow_dags:/data/airflow:ro \
  -v "$(pwd)/backups":/backup \
  alpine tar czf /backup/legba_volumes_$(date +%Y-%m-%d)_LABEL.tar.gz -C /data .
docker compose -p legba up -d supervisor  # Resume
```

### Existing Backups
| File | Date | Contents |
|------|------|----------|
| `backups/skynet_volumes_2026-03-03.tar.gz` | 2026-03-03 | Pre-rebirth (Skynet era) |
| `backups/legba_volumes_2026-03-06_pre-narrative.tar.gz` | 2026-03-06 | Pre-narrative/reports phase |
| `backups/legba_volumes_2026-03-07_pre-quality-fixes.tar.gz` | 2026-03-07 | Pre-quality/grounding fixes (194 cycles) |

---

## 5. Monitoring & Debugging

### Quick Health Check
```bash
# All containers healthy?
docker compose -p legba ps
# Current cycle number
docker exec legba-redis-1 redis-cli GET legba:cycle_number
# Data counts
docker exec legba-postgres-1 psql -U legba -d legba -c \
  "SELECT 'entities' AS t, COUNT(*) FROM entity_profiles UNION ALL \
   SELECT 'events', COUNT(*) FROM events UNION ALL \
   SELECT 'sources', COUNT(*) FROM sources UNION ALL \
   SELECT 'goals_active', COUNT(*) FROM goals WHERE status='active';"
# Qdrant episode count
curl -s http://localhost:6333/collections/legba_short_term | python3 -c "import sys,json; print(json.load(sys.stdin)['result']['points_count'])"
```

### Data Store Access

**Redis:**
```bash
docker exec legba-redis-1 redis-cli KEYS 'legba:*'
docker exec legba-redis-1 redis-cli GET legba:journal | python3 -m json.tool
docker exec legba-redis-1 redis-cli GET legba:latest_report | python3 -c "import sys,json; d=json.loads(sys.stdin.read()); print(d['content'][:2000])"
docker exec legba-redis-1 redis-cli GET legba:reflection_forward | python3 -m json.tool
```

**Postgres:**
```bash
docker exec legba-postgres-1 psql -U legba -d legba
# Tables: entity_profiles, entity_profile_versions, events, event_entity_links, facts, goals, sources, modifications
# Data is in JSONB 'data' column: SELECT data->>'name' AS name FROM entity_profiles LIMIT 10;
# Goals: SELECT data->>'description', data->>'progress_pct', status FROM goals ORDER BY created_at;
```

**AGE Graph:**
```bash
docker exec legba-postgres-1 psql -U legba -d legba -c "
  LOAD 'age'; SET search_path = ag_catalog, public;
  SELECT * FROM cypher('legba_graph', \$\$
    MATCH (n) RETURN labels(n) AS type, count(n) AS cnt
  \$\$) AS (type agtype, cnt agtype);"
```

**Qdrant:**
```bash
curl -s http://localhost:6333/collections | python3 -m json.tool
# Collections: legba_short_term (episodes), legba_long_term (promoted), legba_facts
curl -s -X POST http://localhost:6333/collections/legba_short_term/points/scroll \
  -H 'Content-Type: application/json' \
  -d '{"limit": 3, "with_payload": true, "with_vector": false}' | python3 -m json.tool
```

**OpenSearch:**
```bash
curl -s http://localhost:9200/_cat/indices?v                    # Main indices
curl -s http://localhost:9201/_cat/indices?v                    # Audit indices
curl -s 'http://localhost:9200/legba-events-*/_search?size=3&pretty'  # Recent events
```

### Dump Cycle Prompts
```bash
# Get full prompts from a specific cycle
docker run --rm -v legba_log_data:/logs alpine sh -c \
  'cat /logs/archive/cycle_000042/*.jsonl' | \
  python3 -c "import sys,json; [print(json.dumps(json.loads(l),indent=2)) for l in sys.stdin if 'llm_call' in l]"
```

---

## 6. Operator Messaging

### Send Messages to Agent
```bash
# Regular message (agent reads at ORIENT, addresses when convenient)
docker compose -p legba exec supervisor python -m legba.supervisor.cli --shared /shared send "Your message"
# Directive (agent MUST address before other work)
docker compose -p legba exec supervisor python -m legba.supervisor.cli --shared /shared send --directive "Focus on X"
# Read outbound messages
docker compose -p legba exec supervisor python -m legba.supervisor.cli --shared /shared read
# Agent status
docker compose -p legba exec supervisor python -m legba.supervisor.cli --shared /shared status
```

---

## 7. Web UI Access

All UI is behind SSH tunnel:
```bash
ssh -L 8501:localhost:8501 -L 8503:localhost:8503 -L 5601:localhost:5601 -L 8080:localhost:8080 user@<your-host>
```

| UI | URL | Purpose |
|----|-----|---------|
| Operator Console v2 | http://localhost:8503 | **Recommended.** Multi-panel workstation (React + Dockview). Graph, map, timeline, analytics, consult. |
| Operator Console v1 | http://localhost:8501 | Legacy (FastAPI + htmx). Feature-frozen — no new development. |
| OpenSearch Dashboards | http://localhost:5601 | Ad-hoc data exploration |
| Airflow | http://localhost:8080 | DAG management |

---

## 8. Failure Modes

What happens when each service is unavailable:

| Service | Impact | Recovery | Agent Behavior |
|---------|--------|----------|----------------|
| **Redis** | Cycle state lost, journal/reports inaccessible, ingestion counters dead | `docker compose -p legba restart redis` | Agent crashes at WAKE (can't read cycle_number). Supervisor restarts it. |
| **Postgres** | No structured data: events, entities, facts, goals, sources all unavailable | `docker compose -p legba restart postgres` | Agent crashes at WAKE (service connection). Data persists in volume. |
| **Qdrant** | No episodic memory (short-term, long-term, fact embeddings) | `docker compose -p legba restart qdrant` | Agent runs but ORIENT has no memory context. Degrades gracefully — continues without memories. |
| **OpenSearch** | No full-text search, no event index, no journal archive | `docker compose -p legba restart opensearch` | Agent runs but `os_search` and `event_search` tools fail. Degrades gracefully. |
| **OpenSearch Audit** | Audit logging stops. No cycle history in audit index. | `docker compose -p legba restart opensearch-audit` | Agent unaffected (audit is supervisor-side). Supervisor logs warning but continues. |
| **NATS** | No operator messaging, no pub/sub events | `docker compose -p legba restart nats` | Agent runs but inbox is empty. No operator directives delivered. Messages queue until NATS recovers. |
| **Airflow** | No scheduled pipeline execution | `docker compose -p legba restart airflow` | Agent unaffected (Airflow is optional). DAGs don't run until restored. |
| **Ingestion service** | No automated feed fetching. Agent falls back to manual ACQUIRE. | `docker compose -p legba restart ingestion` | Agent detects missing heartbeat in ORIENT. ACQUIRE cycles do their own feed_parse calls. |
| **LLM API** | No reasoning capability. Cycle fails. | Check API endpoint health | Supervisor detects heartbeat failure after timeout. Retries next cycle. No data loss. |
| **UI v1/v2** | No operator console. Agent unaffected. | `docker compose -p legba restart ui` / `ui-v2` | Zero impact on agent. Data continues accumulating. |

**Multi-service failure**: If Postgres AND Redis are both down, the agent cannot start at all. Supervisor will retry indefinitely. All other combinations degrade gracefully.

**Host reboot**: All containers restart via `restart: unless-stopped`. Agent resumes from last persisted cycle_number in Redis. No data loss (all volumes are persistent).

---

## 9. Safe Operations During Active Cycles

### Safe (no disruption)
- Reading any database (Postgres, Redis, Qdrant, OpenSearch)
- Running queries via UI console or `psql`
- Sending operator messages via CLI
- Building Docker images (`docker compose -p legba build ...`)
- Taking backups (`scripts/backup.sh` is live-safe)
- Viewing logs
- Restarting UI containers (`ui`, `ui-v2`)
- Restarting Airflow

### Safe with brief interruption
- Restarting OpenSearch (agent degrades for ~30s during restart)
- Restarting NATS (messages may be lost during restart window)
- Restarting Qdrant (memory queries fail briefly)

### Requires supervisor stop first
- Clearing agent code volume (Hot Deploy procedure)
- Restarting Postgres (agent will crash mid-cycle if Postgres drops)
- Restarting Redis (agent will crash mid-cycle)
- Running schema migrations (safe but better with agent paused)
- Bulk data operations (DELETE, UPDATE on events/entities/facts)

### Never do while running
- `docker compose -p legba down -v` (destroys all data)
- `docker volume rm legba_*` (same)
- Modifying `.env` without restarting affected containers

---

## 10. UI v2 Deploy

```bash
docker compose -p legba build ui-v2
docker compose -p legba up -d ui-v2
```

No agent disruption. The v2 UI runs independently.

---

## 11. Disk Space Management

The host has 74GB total. Main consumers:
- Docker images (~15GB active)
- Docker build cache (can grow to 30GB+)
- Volume data (~1-2GB per run)
- Backups (~65MB-500MB each)

```bash
df -h /                           # Check free space
docker system df                  # Docker space usage
docker builder prune -f           # Clean build cache (safe, recoverable)
docker image prune -f             # Clean dangling images
```

---

## 12. Troubleshooting

### Agent Won't Start
```bash
docker compose -p legba logs supervisor --tail 50    # Check supervisor errors
docker compose -p legba logs agent --tail 50 2>&1    # Check agent container (if it exists)
```

### Cycle Failures
Supervisor logs show `Agent completed in Xs` for success, `Agent timeout` or error messages for failures. Check:
1. LLM API reachable? Supervisor logs will show 400/500 errors
2. Services healthy? `docker compose -p legba ps` — all should show "healthy"
3. Volume issues? Clear agent code volume and restart (see Hot Deploy)

### OpenSearch Memory Issues
OpenSearch is memory-hungry. If it OOMs:
```bash
docker compose -p legba restart opensearch opensearch-audit
```

### Stale Volumes After `down -v`
Compose sometimes fails to remove all volumes. Always follow up:
```bash
docker volume ls --format '{{.Name}}' | grep legba | xargs -r docker volume rm
```

---

## 13. Configuration Reference

### Key .env Variables
| Key | Default | Description |
|-----|---------|-------------|
| OPENAI_API_KEY | — | LLM API key |
| OPENAI_BASE_URL | — | LLM API endpoint |
| OPENAI_MODEL | InnoGPT-1 | Model name |
| LLM_TEMPERATURE | 1.0 | Must be 1.0 for GPT-OSS |
| AGENT_MAX_CONTEXT_TOKENS | 120000 | Context budget per call |
| AGENT_MAX_REASONING_STEPS | 20 | Max tool calls per cycle |
| AGENT_MISSION_REVIEW_INTERVAL | 15 | Introspection every N cycles |
| SUPERVISOR_HEARTBEAT_TIMEOUT | 600 | Cycle timeout (seconds) |
| CONSULT_LLM_PROVIDER | *(agent's provider)* | Consultation engine LLM provider (`anthropic` or `vllm`) |
| CONSULT_API_KEY | *(agent's key)* | Consultation engine API key. Must be set to enable `/consult`. |
| CONSULT_API_BASE | *(agent's base)* | Consultation engine API base URL (vLLM only) |
| CONSULT_MODEL | *(agent's model)* | Consultation engine model name |
| CONSULT_TEMPERATURE | *(agent's temp)* | Consultation engine temperature |
| CONSULT_MAX_TOKENS | *(agent's max)* | Consultation engine max output tokens |
| CONSULT_TIMEOUT | *(agent's timeout)* | Consultation engine request timeout (seconds) |

### Key Cycle Constants
| Constant | Location | Value | Purpose |
|----------|----------|-------|---------|
| REPORT_INTERVAL | cycle.py | 5 | Status report every N cycles |
| _JOURNAL_MAX_ENTRIES | cycle.py | 30 | Max raw journal entries before trim |
| Long-term promote threshold | cycle.py (_persist) | 0.6 | Auto-promote episodes with significance >= this |
| Source dedup limit | source_tools.py | 500 | Sources checked for duplicate detection |
| Bootstrap threshold | assembler.py | 5 | World briefing injected for cycles 1-N |

### Key Redis Keys
| Key | Type | Contents |
|-----|------|----------|
| legba:cycle_number | string | Current cycle number |
| legba:journal | string (JSON) | Journal entries + consolidation |
| legba:latest_report | string (JSON) | Most recent analysis report |
| legba:report_history | string (JSON) | List of past reports (max 20) |
| legba:reflection_forward | string (JSON) | Last cycle's self-assessment + suggestion |
| legba:goal_work_tracker | string (JSON) | Per-goal cycle counts and progress tracking |
| legba:ui:messages | list | Outbound messages for UI display |
