# Legba — Platform Reference

*Continuously operating autonomous intelligence platform.*
*Last updated: 2026-03-10 | Research cycles, tool loop resilience, GUID dedup, self-modification guidance*

---

## 1. What It Is

Legba is an autonomous AI agent that runs continuously, pursuing open-ended goals with minimal human intervention. It is not a chatbot or a task runner — it is a persistent system that reasons, acts, remembers, and improves itself over time.

The operator provides a seed goal. The agent then operates indefinitely: ingesting data, building a knowledge graph, producing analytical products, and expanding its own capabilities.

**Current mission:** Continuous Global Situational Awareness — an always-on intelligence platform that ingests, correlates, and analyzes global events, producing structured briefings, detecting patterns, and flagging significant developments.

**Key numbers:** 86 Python source files, 241 tests, **50 built-in tools** across 15 builtin modules, 7 platform services, 10 Docker containers.

---

## 2. Architecture

### Container Topology

```
Host VM (Debian 12, 8 vCPU, 16GB RAM)
|
+-- Docker Compose (project: legba)
|   |
|   +-- Supervisor Container
|   |   - Manages agent lifecycle (launch/kill per cycle)
|   |   - Heartbeat validation (LLM liveness check)
|   |   - Auto-rollback on bad self-modifications
|   |   - Log drain to host + audit OpenSearch
|   |   - NATS pub/sub for operator comms
|   |
|   +-- Agent Container (ephemeral, one per cycle)
|   |   - PYTHONPATH=/agent/src (self-modifiable)
|   |   - Cycle: WAKE > ORIENT > [MISSION REVIEW] > PLAN > REASON+ACT > REFLECT > NARRATE > PERSIST
|   |   - 43 built-in tools + cycle_complete pseudo-tool
|   |
|   +-- Platform Services (long-lived)
|   |   - Redis :6379         -- Transient state (counters, flags, registers)
|   |   - Postgres+AGE :5432  -- Structured data, entity graph (Cypher)
|   |   - Qdrant :6333        -- Semantic search (episodes, fact embeddings)
|   |   - NATS :4222          -- Event bus, messaging, data ingestion
|   |   - OpenSearch :9200    -- Bulk data, full-text search, aggregations
|   |   - OpenSearch Audit :9201 -- Audit logs (agent cannot access)
|   |   - Airflow :8080       -- Scheduled pipelines, DAG orchestration
|   |
|   +-- Operator Console :8501  -- Read-only web UI (FastAPI + htmx)
|   |
|   +-- Optional: OpenSearch Dashboards :5601
|
+-- External: LLM API (configurable — GPT-OSS 120B or Anthropic Claude)
```

### LLM Provider System

The LLM provider is modular — selected via `LLM_PROVIDER` env var (`vllm` or `anthropic`). Both providers use the same prompt templates, tool definitions, and `{"actions": [...]}` JSON format. The difference is in message formatting and API protocol.

**Embeddings** are always via vLLM (embedding-inno1, 1024d) regardless of LLM provider — Anthropic has no embedding API. Configured separately via `EMBEDDING_API_BASE` / `EMBEDDING_API_KEY`.

#### Provider: vLLM (GPT-OSS 120B) — `LLM_PROVIDER=vllm`

- **Model:** GPT-OSS 120B MoE via vLLM, OpenAI-compatible API (endpoint name: InnoGPT-1)
- **Context:** 128k tokens | **Temperature:** 1.0 (required)
- **Wire format:** `POST /v1/chat/completions` with single `{"role": "user"}` message
- **System role:** Not supported — `format.py:to_chat_messages()` combines system+user into one message
- **`reasoning: high`** at start of content enables extended reasoning mode
- **Response cleaning:** `strip_harmony_response()` strips Harmony markers from output
- **No max_tokens** — server manages budget (exception: liveness sends 200)

Known behaviors:
- Reasoning mode expects exactly 2 output messages → `{"actions": [...]}` wrapper prevents multi-message errors
- `reasoning` levels: `high`, `medium`, `low` only (no `off`)
- ~1-2% of steps hit multi-message 400 errors, handled by forced-final

#### Provider: Anthropic (Claude) — `LLM_PROVIDER=anthropic`

- **Model:** claude-sonnet-4-20250514 (200k context)
- **Temperature:** 0.7 | **Timeout:** 300s
- **Wire format:** `POST https://api.anthropic.com/v1/messages` with `x-api-key` auth
- **System role:** Proper top-level `system` field (not in messages array)
- **`reasoning:` directives** stripped by `_strip_reasoning_directive()` (GPT-OSS specific)
- **`max_tokens` required** by Anthropic API — always sent (default 16384)
- **Response:** content blocks array → text extracted, usage mapped (`input_tokens` → `prompt_tokens`)
- **stop_reason** normalized: `end_turn` → `stop`, `max_tokens` → `length`
- **Rate limit:** respects `retry-after` header on 429; retries on 429, 500, 529

#### Information Layers

The prompt explicitly separates three types of content:
1. **Identity** — persona, analytical framework (how to think). Marked as Section 1 in SYSTEM_PROMPT.
2. **Factual content** — world briefing, context injections, tool results, memory. Marked as real-world facts that supersede training priors.
3. **Tools** — interface to the real world. Tool results are ground truth.

This separation prevents the model from treating factual briefings as fiction or creative content.

### Parallel Instances

Multiple Legba instances can run side-by-side with different LLM providers:

```bash
# GPT-OSS 120B instance (existing)
docker compose -p legba up -d

# Claude instance (uses docker-compose.claude.yml)
docker compose -p legba-claude -f docker-compose.claude.yml up -d
```

Each instance has fully isolated data stores (separate Docker volumes), shifted host ports, and configurable container names via env vars:
- `LEGBA_AGENT_IMAGE` — agent Docker image name
- `LEGBA_DOCKER_NETWORK` — Docker network name
- `LEGBA_AGENT_CONTAINER` — agent container name
- `LEGBA_VOLUME_PREFIX` — volume name prefix (e.g., `legba_claude`)

**Tool loop resilience:**
- API error retry with exponential backoff (up to 2 attempts) before breaking to REFLECT/PERSIST
- Format retry: re-prompts up to 2 times on unparseable LLM responses instead of silently exiting
- Sliding window condensation (size 8) keeps context growth in check
- Context budget enforcement in the assembler (120K token max)

#### Tool Calls

The agent outputs tool calls as a single JSON object with an actions array:

```json
{"actions": [{"tool": "tool_name", "args": {"param1": "value1"}}]}
```

Multiple concurrent calls:
```json
{"actions": [{"tool": "a", "args": {...}}, {"tool": "b", "args": {...}}]}
```

**Why a wrapper:** GPT-OSS's reasoning mode expects exactly 2 output messages (reasoning + final). When tool calls were separate JSON lines, the model sometimes wrapped each in its own Harmony message block, triggering `"Expected 2 output messages, but got N"` errors. The single-object wrapper ensures all tool calls are in one final message.

Parsed by `tool_parser.py` — supports `{"actions": [...]}` (primary) and bare `{"tool": ...}` (backward compat).

#### Key LLM Files

| File | Role |
|------|------|
| `llm/format.py` | Message dataclass, `to_chat_messages()` (combines into single user msg), `strip_harmony_response()` |
| `llm/provider.py` | VLLMProvider: `/v1/chat/completions`, temp 1.0, no max_tokens, retry logic (500/502/503/429 with exponential backoff) |
| `llm/tool_parser.py` | Balanced-brace JSON extraction from LLM output |
| `llm/client.py` | LLMClient: single-turn tool loop with sliding window (size 8), working memory, graceful error handling |

### Agent Cycle

```
1. WAKE      -- Read challenge, load seed goal + world briefing, connect services, register tools, drain inbox
2. ORIENT    -- Retrieve memories (episodic + semantic), load goals, query facts, graph summary, journal context
3. MISSION   -- Every 15 cycles: strategic review of goal tree, defer/abandon stuck goals
4. PLAN      -- LLM selects goal focus, decides approach. Priority order: event ingestion > entity enrichment > analysis > source discovery
5. ACT       -- Tool loop (up to 20 steps): LLM reasons > calls tools > feeds results > repeats
6. REFLECT   -- LLM evaluates: significance (calibrated 0-1 scale), facts learned, entities, goal progress, memories to promote
7. NARRATE   -- LLM writes 1-3 short journal entries (personal stream of consciousness)
8. PERSIST   -- Store episode, publish outbox, auto-complete goals at 100%, promote memories, heartbeat, exit

Special cycles:
- Every 5 cycles (non-introspection):  RESEARCH — entity enrichment, gap-filling, conflict resolution
- Every 15 cycles:                     INTROSPECTION — deep audit, journal consolidation, world assessment
```

Each step in the REASON+ACT loop rebuilds the full [system, user] message pair (no multi-turn growth). A sliding window keeps the 8 most recent tool results in full, condensing older ones to one-line summaries. Re-grounding prompts inject every 8 steps to keep the LLM on track.

**Planned-tool filtering:** The PLAN phase outputs a `Tools:` line. Only those tools get full parameter definitions during REASON; all others are name+description only (with `explain_tool` for on-demand lookup). Saves ~5-10k tokens per cycle.

**Goal work tracker:** Redis-persisted tracker records how many cycles each goal has been worked on and when it last advanced. Goals worked ≥3 cycles without progress are tagged `STALLED` in the planning context. Stale goal alerts are sent to the operator.

**Reporting reminders:** Every 5 cycles, a reminder is injected prompting the agent to produce a structured intelligence brief (published to NATS outbound / messages page).

### Research Cycles (every 5 cycles)

Every 5 cycles (when not an introspection cycle), the agent runs a dedicated research phase instead of the normal PLAN → ACT flow. Research cycles focus on deepening the knowledge base rather than ingesting new events.

```
WAKE → ORIENT → RESEARCH (REASON+ACT with filtered tools) → REFLECT → NARRATE → PERSIST
```

The research prompt includes an **entity health summary** — a SQL-generated table showing each entity's completeness score, event involvement count, and assertion count. The agent picks 3-5 high-priority targets and researches them:

1. **Identify targets** — entities with low completeness but high event involvement (many events, thin profiles)
2. **Research** — Wikipedia API (`/api/rest_v1/page/summary/`), official sources, news profiles
3. **Update profiles** — fill gaps in entity profiles with sourced assertions
4. **Strengthen graph** — add missing relationships discovered during research
5. **Resolve conflicts** — fix contradictory facts, merge near-duplicates

Research cycles use a restricted tool set (no feed ingestion): `http_request`, graph tools, memory tools, entity tools, `os_search`, event query tools, `cycle_complete`.

### Introspection Cycles (every 15 cycles)

Instead of the normal PLAN > ACT > REFLECT flow, introspection cycles run a deep self-assessment:

```
1. Knowledge Graph Audit        -- Node/edge counts, isolated node cleanup, relationship quality
2. Cross-Domain Pattern Analysis -- Connections between regions/domains
3. Entity Completeness           -- Profile gap detection
4. Goal Health Assessment        -- Stuck/stale goal identification
5. Data Quality Audit            -- Check for duplicate/contradictory facts, use memory_supersede to fix
6. Self-Review                   -- Review own code/prompts, implement concrete improvements
7. Journal Consolidation         -- Weave recent journal entries into a single narrative
8. World Assessment Report       -- Full intelligence assessment ("presidential daily brief")
```

### Journal / Narrative System

Legba maintains a personal stream of consciousness across cycles — the thread of self-continuity. Not structured data, not analysis, not mission reporting. This is where the agent thinks as itself: theories, hunches, evolving understanding, questions it can't answer yet, reflections on its own experience.

- **Every cycle (NARRATE step):** Short LLM call produces 1-3 journal entries (1-2 sentences each). The prompt requires genuine inner experience, forbids repeating phrases from prior entries, and separates journal from cycle reporting.
- **Introspection cycles:** All accumulated entries are consolidated into a single narrative — Legba's current inner landscape. Old entries are cleared after consolidation.
- **ORIENT injection:** The latest consolidation + recent entries are injected into the PLAN phase, giving the agent experiential continuity.
- **Report separation:** Journal informs report *voice and continuity* but is explicitly not a source of facts for analysis reports.
- **Storage:** Redis (`legba:journal` register) for working context (max 30 entries between consolidations). OpenSearch (`legba-journal` index) for permanent archive of all entries and consolidations, searchable by cycle and content.
- **UI:** `/journal` page shows only the latest consolidation.

### Analysis Reports

Full "state of the world" intelligence assessments generated during introspection cycles. Structure:
- Executive Summary (2-3 sentences)
- Regional Situation (key actors, posture, developments per region)
- Emerging Patterns (cross-domain connections, escalation indicators)
- Watch Items (rapid-development situations, info gaps, predictions with confidence)
- Coverage Assessment (well-covered vs sparse domains, source quality)

Reports are substantial documents (1000-3000+ words). Stored in Redis (`report_history`, `latest_report`). Published to NATS outbound for the messages page.

**Data grounding (quality fix 2026-03-07):** Reports are built from queried data, not LLM memory. The report generation queries:
- Actual graph relationships (LeaderOf, HostileTo, AlliedWith, etc.) with entity names
- Entity profiles with summaries from Postgres
- Recent events with full detail (actors, locations, summaries) from OpenSearch/Postgres
- Coverage regions from graph Country nodes

The prompt explicitly forbids fabrication: the LLM may only reference entities, leaders, events, and relationships present in the injected data. Journal narrative is included but clearly labeled as "experiential perspective for voice/continuity only — not a source of facts."

- **UI:** `/reports` page with list + detail views, full markdown rendering.

### Bootstrap & World Briefing

**Problem:** GPT-OSS 120B's training data cuts off around mid-2024. The agent starts blind to ~18 months of world history.

**Solution:** A World State Briefing (`seed_goal/world_briefing.txt`) is loaded during bootstrap cycles (1-5) and injected into the user message. It covers:
- Current world leaders as of March 2026
- Active conflicts (Ukraine-Russia, Sudan, Israel-Gaza-Iran, South China Sea, etc.)
- US domestic events (immigration enforcement, tariff war, Supreme Court decisions)
- Major geopolitical developments (Venezuela, nuclear arms control, Iran crisis)
- Technology/AI developments, notable deaths, other significant events

The briefing is explicitly marked as **factual content** that supersedes training priors — not fiction or hypothetical scenarios.

The bootstrap addon in `templates.py` guides the agent through a structured catch-up:
- **Cycle 1**: Orient, decompose mission into goals, store critical facts from briefing
- **Cycles 2-3**: Build entity profiles, graph relationships, register diverse news sources
- **Cycles 4-5**: Begin live feed ingestion, entity resolution, pattern identification

After cycle 5, the world briefing is no longer injected and the agent operates on live data.

### Data Flow

```
Supervisor                    Agent                      Services              LLM
    |                           |                           |                   |
    | 1. challenge.json         |                           |                   |
    +-------------------------->|                           |                   |
    |                       WAKE|                           |                   |
    |                           +--connect--> Redis, PG, Qdrant, NATS, OS      |
    |                      ORIENT                           |                   |
    |                           +--memories, goals, facts--> Qdrant, PG        |
    |                       PLAN+--completion--------------->|                  |
    |                    ACT    |                           |                   |
    |                           +--tool calls: http, graph, memory, events...  |
    |                    REFLECT+--completion--------------->|                  |
    |                    PERSIST|                           |                   |
    |                           +--store_episode-----------> Qdrant            |
    |                           +--heartbeat(nonce:cycle)---> LLM              |
    |                           +--response.json            |                  |
    |  3. validate heartbeat    |                           |                  |
    |  4. drain logs to audit   |                           |                  |
```

---

## 3. Prompt Architecture

### System Message (~20k chars, every call)

Built by `PromptAssembler._build_system_text()`:

1. **Reasoning level** — `reasoning: high` (first line, picked up by chat template)
2. **Identity** (section 1): Legba persona, analytical soul, worldview, reasoning patterns
3. **Information Layers** — explicit separation of identity vs factual content vs tools
4. **How You Work** (section 2): Cycle lifecycle, container model, cycle number
5. **What You Can Do** (section 3): Tool categories overview
6. **Critical Behaviors** (section 4): Anti-patterns (no chatbot behavior, no planning without acting)
7. **Your Purpose** (section 5): Intelligence analyst framing
8. **Guidance addons**: Memory management, efficiency, analytics, orchestration, SA guidance, entity guidance
9. **Bootstrap addon** (cycles 1-5 only): Early orientation referencing world briefing

Note: System and user messages are combined into a single `{"role": "user"}` message by `to_chat_messages()` before sending to the LLM.

### User Message (varies by phase)

**PLAN phase:** [World briefing (bootstrap only)] + Goal context + memories + graph summary + journal context + inbox + reflection forward + plan prompt

**REASON phase:** [World briefing (bootstrap only)] + Goal context + memories + graph summary + inbox + cycle plan + working memory + tool definitions + calling instructions (tool defs placed last, closest to generation)

**REFLECT phase:** Lightweight system prompt + cycle plan + working memory + results summary

**NARRATE phase:** Cycle summary + prior journal entries → JSON array of 1-3 short entries

**Mission review** (every 15 cycles): Strategic review prompt with goal tree and performance metrics

**Journal consolidation** (introspection): All entries since last consolidation → single narrative synthesis

**Analysis report** (introspection): Graph summary + key relationships + entity profiles + recent events (with actors/locations) + coverage regions + narrative (voice only) → data-grounded world assessment

### Key Prompt Files

| File | Role |
|------|------|
| `prompt/templates.py` | All prompt template strings (system, plan, reflect, SA, entity guidance) |
| `prompt/assembler.py` | PromptAssembler: builds [system, user] message pairs per phase, token budgeting, world briefing injection |

### Prompt Injection Points

| Section | Template Variable | When |
|---------|-------------------|------|
| System identity | `SYSTEM_PROMPT` | Every call |
| Information layers | (in `SYSTEM_PROMPT`) | Every call |
| World briefing | `seed_goal/world_briefing.txt` | Bootstrap cycles (1-5), plan + reason phases |
| Plan guidance | `PLAN_PROMPT` | PLAN phase |
| Tool call format | `TOOL_CALLING_INSTRUCTIONS` | REASON phase |
| Goal context | `GOAL_CONTEXT_TEMPLATE` | PLAN + REASON |
| Memory context | `MEMORY_CONTEXT_TEMPLATE` | PLAN + REASON |
| Reflect | `REFLECT_PROMPT` | REFLECT phase |
| Mission review | `MISSION_REVIEW_PROMPT` | Every 15 cycles |
| Research | `RESEARCH_PROMPT` | Every 5 cycles (non-introspection) |
| SA guidance | `SA_GUIDANCE` | System addon |
| Entity guidance | `ENTITY_GUIDANCE` | System addon |
| Re-grounding | `REGROUND_PROMPT` | Every 8 tool steps |
| Reporting | `REPORTING_REMINDER` | Every 5 cycles (structured intelligence brief format) |
| Narrate | `NARRATE_PROMPT` | NARRATE phase (reasoning: high) |
| Journal consolidation | `JOURNAL_CONSOLIDATION_PROMPT` | Introspection (reasoning: high) |
| Analysis report | `ANALYSIS_REPORT_PROMPT` | Introspection |
| Liveness | `LIVENESS_PROMPT` | PERSIST phase |

---

## 4. Memory Architecture

| Layer | Store | What it holds | Access pattern |
|-------|-------|--------------|----------------|
| **Registers** | Redis | Cycle state, counters, flags, journal, reports | Sync per-cycle |
| **Short-term episodic** | Qdrant | Recent actions/observations (1 per cycle) | Embedding similarity |
| **Long-term episodic** | Qdrant | Significant past events, lessons (auto-promoted at significance ≥ 0.6) | Embedding similarity (decayed) |
| **Structured knowledge** | Postgres | Facts, goals, modifications, sources, events, entity profiles | SQL queries |
| **Entity graph** | Apache AGE | Entities + relationships (Cypher topology) | Cypher queries |
| **Entity profiles** | Postgres (JSONB) | Rich profiles with versioned assertions | SQL + JSONB |
| **Bulk data** | OpenSearch | Documents, event indices, aggregations | Full-text + structured search |

### Entity Intelligence Layer

Structured profiles for entities (countries, organizations, persons, military units, etc.). Profiles accumulate **sourced assertions** — individual claims with confidence scores, source event links, and timestamps. Assertions are organized into sections and superseded when higher-confidence information arrives.

**Entity resolution cascade:** exact canonical name > alias match > case-insensitive > fuzzy (>85%) > create stub.

**Profiles live in Postgres, not AGE.** AGE vertex properties are flat key-value scalars. Rich profile data goes in `entity_profiles` table. AGE holds relationship topology.

### Knowledge Graph

Apache AGE on Postgres. **30 canonical relationship types** with 70+ aliases normalized at the storage layer:

| Category | Types |
|----------|-------|
| Geopolitical | AlliedWith, HostileTo, TradesWith, SanctionedBy, SuppliesWeaponsTo |
| Organizational | MemberOf, LeaderOf, OperatesIn, LocatedIn |
| Geographic | BordersWith, OccupiedBy |
| Diplomatic | SignatoryTo |
| Economic | ProducesResource, ImportsFrom, ExportsTo, FundedBy |
| General | AffiliatedWith, PartOf, CreatedBy, MaintainedBy, RelatedTo |
| Technical | UsesArchitecture, UsesPersistence, HasSafety, HasLimitation, HasFeature, Extends, DependsOn, AlternativeTo, InspiredBy |

**Temporal edges:** Relationships support `since` and `until` properties.

---

## 5. Tool System (50 Tools)

### Core (17 tools)
| Tool | Category |
|------|----------|
| `fs_read`, `fs_write`, `fs_list` | Filesystem |
| `exec` | Shell execution |
| `http_request` | HTTP (Legba-SA User-Agent, auto-retries 403/405 with browser UA) |
| `memory_store`, `memory_query`, `memory_promote`, `memory_supersede` | Memory (episodic + long-term) |
| `graph_store`, `graph_query` | Graph (AGE/Cypher, temporal edges) |
| `goal_create`, `goal_list`, `goal_update`, `goal_decompose` | Goals |
| `code_test` | Self-modification (syntax validation) |
| `spawn_subagent` | Delegation (own 128k context) |

### NATS Messaging (5 tools)
`nats_publish`, `nats_subscribe`, `nats_create_stream`, `nats_queue_summary`, `nats_list_streams`

### OpenSearch (6 tools)
`os_create_index`, `os_index_document`, `os_search`, `os_aggregate`, `os_delete_index`, `os_list_indices`

### Analytical Toolkit (5 tools)
| Tool | Backend |
|------|---------|
| `anomaly_detect` | PyOD (IForest, LOF, KNN) |
| `forecast` | statsforecast (AutoARIMA) |
| `nlp_extract` | spaCy (entities, noun chunks, sentences) |
| `graph_analyze` | NetworkX (centrality, PageRank, community, paths) |
| `correlate` | scikit-learn (correlation, clustering, PCA) |

### Orchestration (5 tools)
`workflow_define`, `workflow_trigger`, `workflow_status`, `workflow_list`, `workflow_pause`

### SA: Source & Feed Tools (5 tools)
`feed_parse`, `source_add`, `source_list`, `source_update`, `source_remove`

### SA: Event Tools (3 tools)
`event_store` (dual Postgres + OpenSearch, auto geo-resolution), `event_query`, `event_search`

### SA: Entity Intelligence Tools (3 tools)
`entity_profile` (with tags), `entity_inspect`, `entity_resolve`

### Inline Cycle Tools (3 tools)
| Tool | Purpose |
|------|---------|
| `note_to_self` | Write observations to working memory |
| `explain_tool` | On-demand full parameter lookup for tools outside the planned set |
| `cycle_complete` | Signal early exit from tool loop (pseudo-tool) |

### Planned-Tool Filtering

The PLAN phase outputs a `Tools:` line listing which tools the agent expects to use. During REASON, only those tools get full parameter definitions in the system prompt; all others are listed as name + description only, with `explain_tool` available for on-demand lookup. This significantly reduces context usage (~5-10k tokens saved per cycle).

### Tool Utilization (as of cycle 57)

**Heavily used (>20 calls/17 cycles):** entity_resolve, event_store, http_request, feed_parse, graph_query, entity_profile, graph_store, source_add, source_update, event_search, source_list, entity_inspect

**Lightly used:** memory_query, memory_store, explain_tool, goal_list, note_to_self, nats_publish, goal_create, goal_update

**Never used (registered but not yet invoked by the agent):** All analytics tools, all orchestration tools, all raw OpenSearch tools, most NATS tools, spawn_subagent, fs_read/write/list, exec, code_test, memory_promote, memory_supersede, goal_decompose, source_remove

---

## 6. Situational Awareness Mission

### Event Pipeline
Sources > `feed_parse` > `event_store` (dual Postgres + OpenSearch) > `entity_resolve` (link actors/locations) > `graph_store` (relationship topology).

Events have: title, summary, full_content, event_timestamp, source_id, source_url, guid, category (conflict/political/economic/technology/health/environment/social/disaster/other), actors[], locations[], tags[], confidence, language.

**Deduplication pipeline:** RSS GUID fast-path (exact match on `guid` column) → title similarity (≥50% word overlap within ±1 day window, or last 100 events when no timestamp provided).

Time-partitioned OpenSearch indices: `legba-events-YYYY.MM`.

### Source Management
Sources have multi-dimensional trust metadata: reliability (0-1), bias_label, ownership_type, geo_origin, timeliness (0-1), coverage_scope.

**Source lifecycle:** Prompt guidance instructs the agent to retry 403/405 responses with a browser User-Agent before giving up, then disable the source via `source_update(status=disabled)` if it still fails. Duplicate detection in `source_add` checks up to 500 existing sources by normalized URL and name.

**Source health stats:** During ORIENT, the cycle queries total sources, sources with events, and total events from Postgres. These stats are injected into the planning context. When utilization is below 50%, a warning is shown and the planning prompt deprioritizes adding new sources.

**HTTP User-Agent fallback:** Both `feed_parse` and `http_request` default to the `Legba-SA/1.0` bot User-Agent. On 403 or 405 responses, they automatically retry once with a Chrome browser User-Agent string before returning the error.

### Entity Resolution Flow
```
event_store("Russia launches missile at Ukraine")
  > entity_resolve(name="Russia", event_id=..., role="actor")
      > resolves to EntityProfile(canonical_name="Russia", type=country)
  > entity_resolve(name="Ukraine", event_id=..., role="target")
      > creates EventEntityLink(event_id, entity_id, role="target")
```

---

## 7. Operator Console UI

Server-rendered web interface for inspecting system state. Read-only.

**Stack:** FastAPI + Jinja2 + htmx + Tailwind CSS (CDN). No npm, no JS build step.

**Access:** `ssh -L 8501:localhost:8501 user@<your-host>` then `http://localhost:8501`

| Page | URL | Data Source |
|------|-----|-------------|
| Dashboard | `/` | Redis, Postgres, response.json. Auto-refreshes 30s. |
| Entity Explorer | `/entities` | Postgres entity profiles with search + type filter |
| Event Explorer | `/events` | Postgres + OpenSearch full-text search |
| Source Registry | `/sources` | Postgres source table with status/type filters |
| Goals | `/goals` | Postgres goal tree with status/progress |
| Cycle Monitor | `/cycles` | Audit OpenSearch (cycle aggregation, tool calls, LLM calls) |
| Messages | `/messages` | NATS outbound (status reports, alerts, analysis reports). Markdown rendered. |
| Journal | `/journal` | Redis — latest journal consolidation (Legba's inner voice, read-only) |
| Reports | `/reports`, `/reports/{n}` | Redis — analysis report list + detail view, full markdown rendering |
| Graph | `/graph` | Apache AGE — interactive knowledge graph visualization |

---

## 8. Self-Modification & Safety

**Agent can modify:** source code (`/agent/src`), prompt templates, tool implementations, DAG definitions, memory strategies.

**Immutable:** supervisor, seed goal, heartbeat mechanism, log drain, audit OpenSearch.

**Safety model:**
1. Structural isolation — agent cannot reach supervisor, seed goal read-only
2. Heartbeat liveness — dedicated LLM call in PERSIST: nonce + cycle_number
3. Auto-rollback — supervisor reverts self-modifications that break the agent
4. Full I/O logging — every prompt/response captured in JSONL, indexed in audit OpenSearch
5. Audit isolation — audit OpenSearch inaccessible to agent
6. Graceful shutdown — soft timeout with stop-flag negotiation (600s soft, ping extends)

---

## 9. Operations

### Quickstart
```bash
docker compose -p legba build
docker compose -p legba up -d    # Starts full stack
```

**Important:** Always use `-p legba` for correct network naming (`legba_default`).

### Agent Code Volume Gotcha
The agent entrypoint seeds `/agent/src` from the Docker image on first boot, then reuses the volume. Code changes require clearing the volume before restart:
```bash
docker run --rm -v legba_agent_code:/agent alpine rm -rf /agent/src /agent/pyproject.toml
docker compose -p legba build agent
docker compose -p legba up -d supervisor
```

### Full Reset (wipe all data)
```bash
docker compose -p legba down -v
# Clean any remaining volumes:
docker volume ls --format '{{.Name}}' | grep legba | xargs -r docker volume rm
docker compose -p legba build agent
docker compose -p legba up -d
```

### Send Messages / Read Responses
```bash
docker compose -p legba exec supervisor python -m legba.supervisor.cli --shared /shared send "message text"
docker compose -p legba exec supervisor python -m legba.supervisor.cli --shared /shared send --directive "Focus on X"
docker compose -p legba exec supervisor python -m legba.supervisor.cli --shared /shared read
docker compose -p legba exec supervisor python -m legba.supervisor.cli --shared /shared status
```

### Monitor
```bash
docker compose -p legba logs supervisor -f
```

### Web Interfaces (SSH tunnel)
```bash
ssh -L 8501:localhost:8501 -L 5601:localhost:5601 -L 8080:localhost:8080 user@<your-host>
```
- **Operator Console** (localhost:8501)
- OpenSearch Dashboards (localhost:5601)
- Airflow UI (localhost:8080)

### Configuration (.env / .env.claude)
| Key | Default | Description |
|-----|---------|-------------|
| LLM_PROVIDER | vllm | LLM provider: `vllm` or `anthropic` |
| OPENAI_API_KEY | — | LLM API key (Anthropic key for Claude) |
| OPENAI_BASE_URL | — | LLM API endpoint (ignored for Anthropic) |
| OPENAI_MODEL | InnoGPT-1 | Model name (e.g., `claude-sonnet-4-20250514`) |
| LLM_TEMPERATURE | 1.0 | Sampling temperature (1.0 for GPT-OSS, 0.7 for Claude) |
| LLM_MAX_TOKENS | 4096 | Max output tokens (required for Anthropic) |
| LLM_MAX_CONTEXT_TOKENS | 128000 | Model context window size |
| EMBEDDING_API_BASE | (api_base) | Embedding endpoint (separate for Anthropic) |
| EMBEDDING_API_KEY | (api_key) | Embedding API key |
| AGENT_MAX_CONTEXT_TOKENS | 120000 | Context budget per assembled prompt |
| AGENT_MAX_REASONING_STEPS | 20 | Max tool calls per cycle |
| AGENT_MISSION_REVIEW_INTERVAL | 15 | Strategic review every N cycles |
| SUPERVISOR_HEARTBEAT_TIMEOUT | 300 | Cycle soft timeout (seconds, extends via ping) |
| LEGBA_AGENT_IMAGE | legba-agent | Agent Docker image name |
| LEGBA_DOCKER_NETWORK | legba_default | Docker network name |
| LEGBA_AGENT_CONTAINER | legba-agent-cycle | Agent container name |
| LEGBA_VOLUME_PREFIX | legba | Volume name prefix |

---

## 10. Logging & Debugging

### Cycle Logs
Every cycle produces a JSONL file in the log drain volume (`legba_log_data`). Each entry has: timestamp, cycle number, event type, and event-specific data.

Event types: `llm_call` (full prompt + response + usage), `tool_call` (args + result), `phase` (cycle phase transitions), `error`, `memory`, `self_modification`.

After each cycle, the supervisor archives logs to `/logs/archive/cycle_NNNNNN/` and indexes them in audit OpenSearch.

### Dumping Prompts
To extract full LLM prompts from a cycle for debugging:
```bash
docker run --rm -v legba_log_data:/logs alpine sh -c 'cat /logs/archive/cycle_000NNN/*.jsonl' \
  | python3 -c "import sys,json; [print(json.dumps(json.loads(l),indent=2)) for l in sys.stdin if 'llm_call' in l]"
```

### Quick LLM Stats
Extract finish reasons and token counts from a cycle:
```bash
docker exec legba-supervisor-1 python3 -c "
import json, os
for f in sorted(os.listdir('/logs/archive/cycle_000NNN')):
    for line in open(f'/logs/archive/cycle_000NNN/{f}'):
        r = json.loads(line)
        if r.get('event') == 'llm_call':
            u = r.get('usage', {})
            print(f'{r[\"purpose\"]}: finish={r.get(\"finish_reason\")} prompt={u.get(\"prompt_tokens\")} comp={u.get(\"completion_tokens\")}')
"
```

---

## 11. Implementation Status

### Completed

| Phase | What |
|-------|------|
| Phases 0-11 | Core platform (supervisor, agent, memory, graph, tools, self-mod, NATS, OpenSearch, analytics, Airflow) |
| Phases A-N | LLM tuning (Harmony format, tool parsing, prompt engineering, context management) |
| SA-1 | Data Pipeline Foundation (event schema, source registry, RSS parser, tools) |
| SA-EI | Entity Intelligence Layer (profiles, assertions, temporal edges, resolution) |
| UI | Operator Console (dashboard, entity/event/source/cycle explorers) |
| Rebirth | Platform rename, identity rework, LLM connector simplification |
| Prompt Detox | Removed project-cataloging inventory, reframed prompts for SA mission |
| LLM Reformat | Switched to ernie pattern (/v1/chat/completions, single user msg, no Harmony wrapping) |
| World Briefing | Bootstrap knowledge seeding with world state through Feb 2026 |
| Narrative & Reports | Journal/narrative system, analysis reports, auto-complete goals, memory promotion fix, status report reformat |
| Quality & Grounding | Report data-grounding (anti-hallucination), journal anti-repetition, planning rebalance (enrichment > collection), source health stats, UA retry on 403/405, long-term memory threshold 0.8→0.6, fact validation fix, source dedup limit 100→500, source lifecycle guidance |

| SA-2 | Source reliability tracking, geo-resolution (pycountry + GeoNames), entity tags, event dedup (50% word overlap), temporal OpenSearch indexing, audit mapping fix, AGE Cypher syntax guidance |
| Multi-Provider | Modular LLM provider system (vLLM + Anthropic), parallel instances, supervisor configurability, embedding decoupling, Graph UI edge fix |
| Agent Quality | Tool loop resilience (API retry with backoff, format retry), self-modification guidance (system prompt section + introspection nudge), introspection data quality audit + self-review steps |
| Data Pipeline | RSS GUID tracking (fast-path dedup), event dedup without timestamp (last 100 fallback), goal dedup in goal_create, source cleanup (81→38), fact predicate normalization (100+ aliases), fact triple unique index, journal archiving to OpenSearch |
| Research Cycles | Dedicated research phase every 5 cycles — entity enrichment via Wikipedia/reference sources, entity health summary, gap-filling, conflict resolution |

### Planned

| Phase | What |
|-------|------|
| SA-3 | Analysis, Alerting & Output (anomaly detection, briefings, alerts, trend analysis) |
| UI CRUD | Entity edit/merge, event delete/edit, facts delete/edit, memory delete, graph edge management, source full edit |

---

## 12. Project Structure

```
legba/
+-- docker-compose.yml
+-- docker/                          -- Dockerfiles + entrypoints
+-- seed_goal/
|   +-- goal.txt                     -- Active: SA mission
|   +-- identity.txt                 -- Legba self-concept
|   +-- operating_principles.txt     -- Analytical tradecraft
|   +-- world_briefing.txt           -- World state briefing (mid-2024 to Feb 2026)
+-- src/legba/
|   +-- shared/
|   |   +-- schemas/                 -- Pydantic models (cycle, goals, memory, tools, events, entities, sources, comms, modifications)
|   |   +-- config.py               -- LegbaConfig (temperature default 1.0)
|   +-- agent/
|   |   +-- main.py                  -- Entry point
|   |   +-- cycle.py                 -- WAKE>ORIENT>PLAN>ACT>REFLECT>NARRATE>PERSIST + journal, reports, introspection
|   |   +-- log.py                   -- CycleLogger (JSONL structured logging)
|   |   +-- llm/                     -- format.py, provider.py, client.py, tool_parser.py
|   |   +-- memory/                  -- manager.py, registers.py, episodic.py, structured.py, graph.py, opensearch.py
|   |   +-- goals/                   -- Goal CRUD + decomposition
|   |   +-- tools/
|   |   |   +-- registry.py
|   |   |   +-- executor.py
|   |   |   +-- builtins/            -- 15 modules (50 tools) + geo.py utility
|   |   +-- selfmod/                 -- Self-modification engine + rollback
|   |   +-- comms/                   -- NATS client, Airflow client
|   |   +-- prompt/
|   |       +-- templates.py         -- All prompt templates (includes information layers framing)
|   |       +-- assembler.py         -- Context assembly + token budget + world briefing injection
|   +-- supervisor/
|   |   +-- main.py, lifecycle.py, heartbeat.py, comms.py, cli.py, audit.py, drain.py
|   +-- ui/
|       +-- app.py, stores.py, messages.py, static/
|       +-- routes/                    -- dashboard, entities, events, sources, goals, cycles, messages, journal, reports, graph
|       +-- templates/                 -- Jinja2 (base.html + per-page dirs)
+-- tests/                           -- 241 tests (unit + integration + graph)
+-- docs/
    +-- LEGBA.md                     -- This document (platform reference)
    +-- CODE_MAP.md                  -- Full code map with function flows
    +-- OPERATIONS.md               -- Ops runbook: deployment, resets, monitoring
    +-- PROMPT_DUMP.md              -- Full assembled prompts by phase
    +-- PROMPT_GUIDE.md
    +-- PROMPT_REFERENCE.md
    +-- GRACEFUL_SHUTDOWN.md
    +-- PHASE_SA_SITUATIONAL_AWARENESS.md
    +-- PHASE_NARRATIVE_REPORTS.md
    +-- archive/                     -- Historical docs
```

---

## 13. Technology Stack

| Component | Technology |
|-----------|-----------|
| Runtime | Python 3.12 (async), Docker Compose |
| LLM | GPT-OSS 120B MoE (128k ctx) via vLLM (endpoint: InnoGPT-1) |
| Vector search | Qdrant (1024d embeddings, time-decayed similarity) |
| Structured data | PostgreSQL 18 + Apache AGE |
| Full-text search | OpenSearch 2.x |
| Messaging | NATS + JetStream |
| Orchestration | Apache Airflow |
| Analytics | PyOD, statsforecast, spaCy, NetworkX, scikit-learn |
| RSS/Feed | feedparser, trafilatura |
| Operator Console | FastAPI, Jinja2, htmx, Tailwind CSS |
| Data models | Pydantic v2 |
