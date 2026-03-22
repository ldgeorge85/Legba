# Legba — Platform Reference

*Autonomous intelligence analysis platform.*
*Last updated: 2026-03-22 | Cycle redesign (SURVEY/SYNTHESIZE), hypothesis engine (ACH), 22+ UI panels, worker mode, hybrid LLM routing*

---

## 1. What It Is

Legba is a continuously operating AI intelligence analyst. It does not collect data — an automated ingestion pipeline handles that. The AI runs structured analytical cycles: building a knowledge graph, tracking situations, testing competing hypotheses against incoming evidence, and producing named intelligence products.

**Three layers, each doing what it's best at:**

- **Collection layer** (deterministic, no LLM): Fetches 100+ RSS/API sources, normalizes signals, 4-tier dedup, spaCy NER, embeds to Qdrant, clusters signals into events. Runs continuously and independently.
- **Analytical layer** (LLM-driven): 7 cycle types with restricted tool sets. SURVEY cycles review events, build graph relationships, and stress-test hypotheses. SYNTHESIZE cycles deep-dive into situations and produce named briefs. ANALYSIS detects patterns. RESEARCH enriches entities. INTROSPECTION produces world assessment reports. EVOLVE audits the agent itself.
- **Operator layer**: 22+ panel intelligence workstation, consultation engine with 15+ tools, inbox for directives, real-time SSE feed.

**Current deployment:** Continuous Global Situational Awareness — monitoring geopolitical, conflict, health, environmental, and economic developments. Same codebase supports privacy/overreach monitoring and attack surface management via configuration (seed goal + source portfolio).

**Two-tier data model:** Raw **signals** (RSS items, API responses, alerts) are ingested and deterministically clustered into derived **events** (real-world occurrences). Signals are evidence; events are the analytical unit. Reports, situations, hypotheses, and graph analysis operate on events.

**Key numbers:** 100+ Python source files, 200+ tests, **66 built-in tools** across 19 builtin modules, ~30,500 signals, ~1,100 events, ~13,400 active facts, ~598 entities, 138 active sources, 14 Docker containers.

---

## 2. Architecture

### Container Topology

```
Host VM (Debian 12, 8 vCPU, 16GB RAM)
|
+-- Docker Compose (project: legba, 14 containers)
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
|   |   - Cycle: WAKE > ORIENT > [cycle type routing] > REFLECT > NARRATE > PERSIST
|   |   - cycle.py orchestrator + 15 phase mixins (phases/ directory)
|   |   - 66 built-in tools (incl. cycle_complete pseudo-tool)
|   |
|   +-- Ingestion Service Container
|   |   - Background signal fetching, normalization, deterministic clustering
|   |   - spaCy NER, 4-tier dedup, no LLM required
|   |
|   +-- Platform Services (long-lived)
|   |   - Redis :6379         -- Transient state (counters, flags, registers)
|   |   - Postgres+AGE :5432  -- Structured data, entity graph (Cypher)
|   |   - Qdrant :6333        -- Semantic search (episodes, fact embeddings)
|   |   - NATS :4222          -- Event bus, messaging, data ingestion
|   |   - OpenSearch :9200    -- Bulk data, full-text search, aggregations
|   |   - OpenSearch Audit :9201 -- Audit logs (agent cannot access)
|   |   - Airflow :8080       -- Scheduled pipelines, 4 DAGs (metrics rollup, source health, decision surfacing, eval rubrics)
|   |   - TimescaleDB :5433   -- Time-series metrics (cycle, ingestion, source health, HDX conflict baselines)
|   |   - Grafana :3000       -- Operational dashboards (auto-provisioned TimescaleDB datasource)
|   |
|   +-- Operator Console v1 :8501  -- Web UI + consultation (FastAPI + htmx)
|   +-- Operator Console v2 :8503  -- Multi-panel workstation (React + Dockview + Sigma.js + MapLibre)
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
- Reasoning mode expects exactly 2 output messages (reasoning + final) — `reasoning: high` must appear exactly once per combined message to avoid multi-segment 400 errors
- `reasoning` levels: `high`, `medium`, `low` only (no `off`)
- Phase-specific templates (EVOLVE, RESEARCH, etc.) do NOT include `reasoning: high` — it is only in the system prompt to prevent duplication when combined into a single user message

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

The cycle is implemented as a mixin-based architecture: `cycle.py` (~195 lines) is a thin orchestrator that inherits from 15 phase mixins in the `phases/` directory. Each mixin owns one phase and its helper methods.

```
1. WAKE      -- Read challenge, load seed goal + world briefing, connect services, register 66 tools, drain inbox
2. ORIENT    -- Retrieve memories (episodic + semantic), load goals, graph summary, source health, ingestion gap tracking, journal leads
3. Route to cycle type (3-tier):
   Tier 1 — Scheduled outputs (fixed intervals):
     a. EVOLVE (every 30)        -- self-improvement, source discovery, operational scorecard
     b. INTROSPECTION (every 15) -- mission review, deep audit, journal consolidation, analysis report
     c. SYNTHESIZE (every 10)    -- deep-dive investigation, situation briefs, predictions
   Tier 2 — Guaranteed work (coprime modulo intervals):
     d. ANALYSIS (every 4)       -- pattern detection, graph mining, anomaly detection, trend analysis
     e. RESEARCH (every 7)       -- entity enrichment via Wikipedia/reference, gap-filling
     f. CURATE (every 9)         -- event curation from clustered signals
   Tier 3 — Dynamic fill (state-scored):
     g. CURATE or SURVEY         -- scored by uncurated backlog vs default analytical desk work
4. REFLECT   -- LLM evaluates: significance (calibrated 0-1 scale), facts learned, entities, goal progress
5. NARRATE   -- LLM writes 1-3 journal entries + extracts investigation leads
6. PERSIST   -- Store episode, track ingestion, auto-complete goals, promote memories, heartbeat, exit
```

```python
# 3-tier cycle type selection:
# Tier 1: scheduled outputs
if cn % 30 == 0: EVOLVE
elif cn % 15 == 0: INTROSPECTION
elif cn % 10 == 0: SYNTHESIZE
# Tier 2: guaranteed work (coprime intervals)
elif cn % 4 == 0: ANALYSIS
elif cn % 7 == 0: RESEARCH
elif cn % 9 == 0: CURATE
# Tier 3: dynamic fill
else: score(CURATE vs SURVEY) based on uncurated backlog
```

**Cycle type distribution (per 90 cycles):** 3 evolve (3%), 3 introspection (3%), 6 synthesize (7%), 18 analysis (20%), 8 research (9%), 5 curate (6% guaranteed + dynamic), 47 survey/dynamic (52%). Tier 2 intervals are coprime (4, 7, 9) to minimize masking. ANALYSIS is intentionally frequent — pattern detection benefits from running often on fresh data. Each cycle type uses a filtered tool set. SURVEY replaces the old NORMAL cycle — no collection tools, explicitly analytical.

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

Research cycles use a restricted tool set (no feed ingestion): `http_request`, graph tools, memory tools, entity tools, `os_search`, signal/event query tools, `cycle_complete`.

### Curate Cycles (every 3 cycles)

Every 3 cycles (when not research/analysis/introspection), the agent runs a dedicated event curation phase. With the ingestion service active, raw signals are already being collected continuously. CURATE cycles focus on reviewing candidate events produced by deterministic clustering and promoting them into the events table.

```
WAKE → ORIENT → CURATE (REASON+ACT with filtered tools) → REFLECT → NARRATE → PERSIST
```

The curate prompt includes candidate events (signal clusters) and the agent:
1. Reviews candidate events from deterministic clustering
2. Creates or updates derived events (`event_create`, `event_update`)
3. Links supporting signals to events (`event_link_signal`)
4. Resolves entities from newly curated events
5. Adjusts severity, category, and event type as needed

Curate cycles use a restricted tool set (curation focus): signal tools (`signal_query`, `signal_search`), event tools (`event_create`, `event_update`, `event_query`, `event_link_signal`), `entity_resolve`, `entity_profile`, source tools, `graph_store`, watchlist/situation query tools.

**Ingestion gap tracking:** Redis tracks `last_ingestion_cycle`. When no signals have been stored for >5 cycles, a warning is injected into the PLAN context via ORIENT.

### Analysis Cycles (every 10 cycles)

Every 10 cycles (when not introspection), the agent runs a dedicated analysis phase:

```
WAKE → ORIENT → ANALYZE (REASON+ACT with filtered tools) → REFLECT → NARRATE → PERSIST
```

The analysis prompt includes **data context**: event distribution by category, top entities by event involvement, data thresholds for analytical tools (30+ events for anomaly_detect, 20+ relationships for graph_analyze), active situations, watchlist trigger summary, and **differential reporting** (changes since last analysis cycle).

The agent:
1. Runs graph analysis (centrality, clustering, paths) for structural insights
2. Detects event patterns (temporal, categorical, geographic)
3. Uses anomaly detection when enough data exists
4. Queries temporal trends with `temporal_query` (day/week/month buckets)
5. Identifies gaps and under-covered areas
6. Stores analytical insights as high-significance memories

**Differential reporting**: Each analysis cycle stores a snapshot (event/entity/relationship counts, category distribution, top entities, situation states) to Redis. The next analysis cycle compares against the previous snapshot and highlights changes: new events/entities, growing categories, new top entities, situation status changes.

Analysis cycles use: graph tools, memory tools, entity tools, signal/event query tools, analytics tools (`anomaly_detect`, `temporal_query`), watchlist/situation tools (can create new watches/situations based on findings).

### Evolve Cycles (every 30 cycles)

The highest-priority cycle type. Every 30 cycles, the agent runs a structured self-improvement and source discovery phase:

```
WAKE → ORIENT → EVOLVE (REASON+ACT with filtered tools) → REFLECT → NARRATE → PERSIST
```

The evolve prompt builds an **operational context** from:
- Recent analysis reports (coverage and watch sections)
- Reflection data from recent cycles
- Source utilization stats (fetched vs total, staleness)
- Entity freshness stats (stale profiles not verified in 100+ cycles)
- Coverage breadth (distinct locations in recent events)
- Previous evolve log (what was changed last time, did it help?)

The agent then works through 5 structured phases:
1. **Operational Scorecard** — assess source coverage, entity freshness, reporting quality, tool usage patterns
2. **Source Discovery** — identify coverage gaps, find new sources, register via `source_add`
3. **Prompt & Tool Evaluation** — read own prompt templates, check for consistently unfollowed instructions, unused tools, tool call failure patterns
4. **Implement Improvements** — modify prompts, add normalization rules, adjust parameters, create goals for structural fixes
5. **Track Changes** — log what changed and why to `evolve_log` Redis key, compare with previous evolve cycle results

Evolve cycles use: filesystem tools (`fs_read`, `fs_write`, `fs_list`, `code_test`), graph tools, memory tools, entity tools, event/signal query tools, `os_search`, source tools (`source_list`, `source_add`, `source_update`), goal tools, inline tools (`note_to_self`, `explain_tool`, `cycle_complete`).

Changes are stored in Redis `evolve_log` as a structured record with cycle number, timestamp, changes list, and assessment summary.

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

**Analyst Hypotheses:** Reports include a separate "Analyst Hypotheses" section, clearly delineated from factual content. Hypotheses are labeled inferences with explicit confirm/refute criteria (e.g., "If X happens within Y timeframe, this hypothesis is confirmed"). This section is the only place inferential reasoning is permitted — fact sections are restricted to data-grounded statements only (anti-inferential-leakage rules ban words like "implicit", "implied", "inferred", "suggests", "appears to be").

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
8. **Signals vs Events** (section 6): Two-tier data model explanation
9. **Guidance addons**: Memory management, efficiency, analytics, orchestration, SA guidance, entity guidance
10. **Self-Assessment Discipline**: Anti-catastrophizing rules — don't conclude infra is down from single tool errors, journal carries weight, AGE Cypher limitations
11. **Bootstrap addon** (cycles 1-5 only): Early orientation referencing world briefing

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
| Narrate | `NARRATE_PROMPT` | NARRATE phase |
| Journal consolidation | `JOURNAL_CONSOLIDATION_PROMPT` | Introspection |
| Analysis report | `ANALYSIS_REPORT_PROMPT` | Introspection |
| Survey | `SURVEY_PROMPT` | Default cycle (analytical desk work, replaces NORMAL) |
| Synthesize | `SYNTHESIZE_PROMPT` | Every 10 cycles (deep-dive investigation, situation briefs) |
| Curate | `CURATE_PROMPT` | Every 9 cycles (event curation from clustered signals) |
| Evolve | `EVOLVE_PROMPT` | Every 30 cycles (self-improvement, source discovery, operational scorecard) |
| Liveness | `LIVENESS_PROMPT` | PERSIST phase |

---

## 4. Memory Architecture

| Layer | Store | What it holds | Access pattern |
|-------|-------|--------------|----------------|
| **Registers** | Redis | Cycle state, counters, flags, journal, reports | Sync per-cycle |
| **Short-term episodic** | Qdrant | Recent actions/observations (1 per cycle) | Embedding similarity |
| **Long-term episodic** | Qdrant | Significant past events, lessons (auto-promoted at significance ≥ 0.6) | Embedding similarity (decayed) |
| **Structured knowledge** | Postgres | Facts, goals, modifications, sources, signals, events, entity profiles | SQL queries |
| **Entity graph** | Apache AGE | Entities + relationships (Cypher topology) | Cypher queries |
| **Entity profiles** | Postgres (JSONB) | Rich profiles with versioned assertions | SQL + JSONB |
| **Bulk data** | OpenSearch | Documents, event indices, aggregations | Full-text + structured search |
| **Time-series metrics** | TimescaleDB | Cycle metrics, ingestion metrics, source health, HDX conflict baselines (242 countries, 2018-2025) | SQL + hypertables |

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

## 5. Tool System (66 Tools)

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

### Analytical Toolkit (6 tools)
| Tool | Backend |
|------|---------|
| `anomaly_detect` | PyOD (IForest, LOF, KNN) |
| `forecast` | statsforecast (AutoARIMA) |
| `nlp_extract` | spaCy (entities, noun chunks, sentences) |
| `graph_analyze` | NetworkX (centrality, PageRank, community, paths) |
| `correlate` | scikit-learn (correlation, clustering, PCA) |
| `temporal_query` | Postgres date_trunc (event trends, category breakdown, trend detection) |

### Orchestration (5 tools)
`workflow_define`, `workflow_trigger`, `workflow_status`, `workflow_list`, `workflow_pause`

### SA: Source & Feed Tools (5 tools)
`feed_parse`, `source_add`, `source_list`, `source_update`, `source_remove`

### SA: Signal Tools (3 tools)
`signal_store` (aliased as `event_store` for backward compat; stores raw signals to Postgres + OpenSearch with auto geo-resolution), `signal_query`, `signal_search`

**Post-store hooks** (best-effort, appended to signal_store response):
- **Watchlist auto-matching**: checks new signals against active watch patterns, creates triggers automatically
- **Situation suggestions**: scores signal relevance against active situations by entity/region/category overlap
- **Novelty scoring**: rates how unexpected the signal is based on actor familiarity and category rarity

### SA: Event Tools (4 tools)
`event_create` (derive event from signals, assign severity/type/category), `event_update` (modify event metadata, severity, status), `event_query` (filter by severity, type, category, time range), `event_link_signal` (associate additional signals with an event)

### SA: Entity Intelligence Tools (3 tools)
`entity_profile` (with tags), `entity_inspect`, `entity_resolve`

### SA: Watchlist Tools (3 tools)
`watchlist_add`, `watchlist_list`, `watchlist_remove` — persistent alerting patterns that trigger on matching events (entities, keywords, categories, regions). Watch triggers stored in `watch_triggers` table with event references.

### SA: Situation Tracking Tools (4 tools)
`situation_create`, `situation_update`, `situation_list`, `situation_link_event` — persistent tracked narratives (e.g., "Iran Nuclear Crisis") that accumulate events, track status (active/escalating/de_escalating/dormant/resolved), and measure intensity over time.

### SA: Prediction Tracking Tools (3 tools)
`prediction_create`, `prediction_update`, `prediction_list` — falsifiable hypotheses for future verification. Create predictions when analysis reveals developing patterns, add evidence for/against, adjust confidence, and resolve as confirmed/refuted/expired.

### Inline Cycle Tools (3 tools)
| Tool | Purpose |
|------|---------|
| `note_to_self` | Write observations to working memory |
| `explain_tool` | On-demand full parameter lookup for tools outside the planned set |
| `cycle_complete` | Signal early exit from tool loop (pseudo-tool) |

### Planned-Tool Filtering

The PLAN phase outputs a `Tools:` line listing which tools the agent expects to use. During REASON, only those tools get full parameter definitions in the system prompt; all others are listed as name + description only, with `explain_tool` available for on-demand lookup. This significantly reduces context usage (~5-10k tokens saved per cycle).

### Tool Utilization

**Core working set (~15 tools used most cycles):** entity_resolve, signal_store, event_create, event_query, http_request, feed_parse, graph_query, entity_profile, graph_store, source_add, source_update, signal_search, source_list, entity_inspect, memory_query, memory_store, goal_list

**Cycle-type-specific tools:** Analytics tools (anomaly_detect, temporal_query, graph_analyze, correlate) used during ANALYSIS cycles. Filesystem tools (fs_read, fs_write, code_test) used during EVOLVE cycles. Orchestration tools available but rarely invoked autonomously.

**Observed pattern:** The agent converges on its core working set organically. Specialized tools see use primarily during their designated cycle types.

---

## 6. Situational Awareness Mission

### Two-Tier Data Model

Legba separates raw ingested material from derived real-world occurrences:

- **Signals** (`signals` table) — raw data items from sources: RSS items, API responses, alerts. Each signal has a source, timestamp, title, content, and metadata. Signals are the ground-truth input layer, created by the ingestion service without LLM involvement.
- **Events** (`events` table) — derived real-world occurrences. Each event represents something that happened in the world, potentially supported by multiple signals. Events have: title, summary, category, event_type (incident/development/shift/threshold), severity (critical/high/medium/low/routine), time_start, time_end, signal_count, source_method (auto/agent/manual).
- **Signal-Event Links** (`signal_event_links` table) — many-to-many relationship connecting signals to the events they support.

### Signal + Event Pipeline

```
Source → Ingestion Service → Signal (raw) → Deterministic Clustering → Candidate Event
                                                                            ↓
                                              CURATE cycle → Agent reviews → Event (derived)
                                                                            ↓
                                                          entity_resolve → graph_store
```

1. **Ingestion**: Sources are fetched continuously by the ingestion service. Each item becomes a signal in the `signals` table.
2. **Clustering**: The ingestion service groups related signals into candidate events using deterministic clustering (entity overlap + title similarity + temporal proximity + category matching). No LLM required.
3. **Curation**: During CURATE cycles, the agent reviews candidate events, creates/updates events, links signals, assigns severity and event_type, and resolves entities.
4. **Enrichment**: Entity resolution links actors and locations. Graph edges capture relationship topology.

### Signal Deduplication (3-tier)
1. RSS GUID fast-path (exact match on `guid` column)
2. Source URL exact match (same article from same source)
3. Title similarity — adaptive threshold (40% for short titles ≤5 words, 50% otherwise) within ±1 day window (200 signals) or last 300 signals when no timestamp

### Deterministic Clustering

The ingestion service clusters signals into candidate events without LLM involvement:
- **Entity overlap** — signals mentioning the same actors/locations are grouped
- **Title similarity** — Jaccard similarity on title tokens
- **Temporal proximity** — signals within a configurable time window
- **Category matching** — signals in the same category are preferred for grouping

Candidate events are stored for agent review during CURATE cycles.

Time-partitioned OpenSearch indices: `legba-signals-YYYY.MM`.

### Source Management
Sources have multi-dimensional trust metadata: reliability (0-1), bias_label, ownership_type, geo_origin, timeliness (0-1), coverage_scope.

**Source lifecycle:** Prompt guidance instructs the agent to retry 403/405 responses with a browser User-Agent before giving up, then disable the source via `source_update(status=disabled)` if it still fails. Duplicate detection in `source_add` checks up to 500 existing sources by normalized URL and name.

**Source health stats:** During ORIENT, the cycle queries total sources, sources with events, and total events from Postgres. These stats are injected into the planning context. When utilization is below 50%, a warning is shown and the planning prompt deprioritizes adding new sources.

**HTTP User-Agent fallback:** Both `feed_parse` and `http_request` default to the `Legba-SA/1.0` bot User-Agent. On 403 or 405 responses, they automatically retry once with a Chrome browser User-Agent string before returning the error.

### Entity Resolution Flow
```
signal_store("Russia launches missile at Ukraine")  → signal record
event_create(title="Russian missile strike on Ukraine", signals=[signal_id])  → event record
  > entity_resolve(name="Russia", event_id=..., role="actor")
      > resolves to EntityProfile(canonical_name="Russia", type=country)
  > entity_resolve(name="Ukraine", event_id=..., role="target")
      > creates EventEntityLink(event_id, entity_id, role="target")
```

### Ingestion Service

Autonomous background service that continuously fetches, normalizes, and stores **signals** from registered sources without consuming agent cycles. Runs as a separate container alongside the agent. The service also performs **deterministic clustering** — grouping related signals into candidate events based on entity overlap, title similarity, temporal proximity, and category matching.

When active (`INGESTION_SERVICE_ACTIVE=true`), CURATE cycles replace the old ACQUIRE cycle — the agent curates events from clustered signals rather than fetching sources directly. Source discovery moves to EVOLVE cycles.

**Source type normalizers** — specialized parsers for structured APIs (dispatched by source name prefix). Generic RSS/Atom feeds use the default normalizer.

| Source Type | API/Format | Normalizer |
|------------|-----------|------------|
| GDELT | REST JSON (DOC API) | `normalize_gdelt()` |
| USGS Earthquakes | GeoJSON | `normalize_usgs_earthquake()` |
| NASA EONET | REST JSON | `normalize_eonet()` |
| NWS Alerts | GeoJSON | `normalize_nws_alert()` |
| ACLED | REST JSON (OAuth) | `normalize_acled()` |
| CISA KEV | Static JSON | `normalize_cisa_kev()` |
| ReliefWeb | REST JSON | `normalize_reliefweb()` |
| IFRC | REST JSON | `normalize_ifrc()` |
| NVD CVE | REST JSON | `normalize_nvd()` |
| NASA FIRMS | CSV | `normalize_firms()` |
| UCDP | REST JSON | `normalize_ucdp()` |
| Frankfurter | REST JSON (FX rates) | `normalize_frankfurter()` |
| CDC | RSS | (default) |
| WHO | OData JSON | (default) |
| Event Registry | REST JSON | (default) |
| RSS feeds | RSS/Atom | (default) |

See `src/legba/ingestion/source_normalizers.py` for normalizer implementations.

---

## 7. Operator Console UI

**V1** (FastAPI + htmx on `:8501`) is the legacy console. **V2** (React + Dockview on `:8503`) is the recommended operator interface. V1 is feature-frozen; all new development targets V2.

### Operator Console v1 — Legacy

Server-rendered web interface for inspecting, managing, and consulting Legba.

**Stack:** FastAPI + Jinja2 + htmx + Tailwind CSS (CDN). No npm, no JS build step.

**Access:** `ssh -L 8501:localhost:8501 user@<your-host>` then `http://localhost:8501`

| Page | URL | Data Source | CRUD |
|------|-----|-------------|------|
| Consult | `/consult` | LLM + all stores (tool-calling loop). Interactive operator chat. | Read + Write |
| Dashboard | `/` | Redis, Postgres, response.json. Auto-refreshes 30s. | Read |
| Entity Explorer | `/entities`, `/entities/{id}` | Postgres entity profiles with search + type filter | Read, add/remove assertions |
| Signal Explorer | `/signals`, `/signals/{id}` | Postgres + OpenSearch full-text search | Read, edit metadata, delete |
| Event Explorer | `/events`, `/events/{id}` | Derived events with severity/type filtering | Read, edit metadata, delete |
| Source Registry | `/sources`, `/sources/{id}` | Postgres source table with status/type filters | Read, full edit |
| Facts | `/facts` | Postgres structured facts | Read, inline edit, delete |
| Goals | `/goals` | Postgres goal tree with status/progress | Read |
| Watchlist | `/watchlist` | Postgres watchlist + watch_triggers tables | Read, create, delete |
| Situations | `/situations`, `/situations/{id}` | Postgres situations + situation_events tables | Read, create, update status, delete |
| Cycle Monitor | `/cycles` | Audit OpenSearch (cycle aggregation, tool calls, LLM calls) | Read |
| Messages | `/messages` | NATS outbound (status reports, alerts, analysis reports). Markdown rendered. | Read |
| Memory | `/memory` | Qdrant episodic + long-term vectors | Read, delete |
| Journal | `/journal` | Redis — latest journal consolidation (Legba's inner voice) | Read |
| Journal API | `/api/journal` | Redis — raw journal JSON (entries + consolidation) | Read (JSON) |
| Reports | `/reports`, `/reports/{n}` | Redis — analysis report list + detail view, full markdown rendering | Read |
| Reports API | `/api/reports` | Redis — all reports as JSON array | Read (JSON) |
| Graph | `/graph` | Apache AGE — interactive knowledge graph visualization | Read, add/remove edges |

### CRUD Operations (htmx)

All mutations use htmx for inline updates without full page reloads:

| Operation | Endpoint | Method | UI Pattern |
|-----------|----------|--------|------------|
| Delete fact | `/api/facts/{id}` | DELETE | Confirm → row fade out |
| Edit fact | `/api/facts/{id}` | PUT | Inline edit → row replace |
| Delete memory | `/api/memory/{collection}/{id}` | DELETE | Confirm → card fade out |
| Add entity assertion | `/api/entities/{id}/assertions` | POST | Form → row append |
| Remove entity assertion | `/api/entities/{id}/assertions` | DELETE | Confirm → row remove |
| Delete event | `/api/events/{id}` | DELETE | Confirm → cascade delete (entity links + OpenSearch) |
| Edit event metadata | `/api/events/{id}` | PUT | Collapsible form → category, tags, confidence |
| Add graph edge | `/api/graph/edges` | POST | Form with entity datalist + relationship type dropdown |
| Remove graph edge | `/api/graph/edges` | DELETE | Confirm → edge remove + graph refresh |
| Edit source | `/api/sources/{id}` | PUT | Collapsible form → name, url, type, reliability, bias, tags, description |
| Create watch | `/api/watchlist` | POST | Inline form → name, entities, keywords, categories, regions, priority |
| Delete watch | `/api/watchlist/{id}` | DELETE | Confirm → row remove (cascades triggers) |
| Create situation | `/api/situations` | POST | Inline form → name, description, category, entities, regions, tags |
| Update situation status | `/api/situations/{id}` | PUT | Status dropdown → badge update |
| Delete situation | `/api/situations/{id}` | DELETE | Confirm → cascade delete (event links) |

### Consultation ("The Working")

Interactive agentic chat interface at `/consult`. The operator converses directly with Legba, which has tool access to the same data stores the UI provides.

**How it works:**
- Lightweight LLM tool-calling loop (up to 10 tool steps per exchange)
- Own LLM config via `CONSULT_*` env vars — defaults to Anthropic (Claude Sonnet), independent of the agent's vLLM/GPT-OSS provider
- Does NOT reuse `LLMClient` (too coupled to cycles) — uses providers directly
- System prompt includes Legba's identity, live data context (entity/event/fact counts, latest journal, active situations)
- Redis-backed session management (1-hour TTL per session)
- `respond` tool pattern: LLM calls `respond` to emit its final answer, ending the tool loop
- Empty response recovery (re-prompts on empty content) and 400 retry for GPT-OSS Harmony multi-message errors
- Separate from the main agent cycle — does not interfere with autonomous operations

**15 consultation tools:**

| Tool | Purpose | Access |
|------|---------|--------|
| `search_signals` | Full-text signal search (OpenSearch) | Read |
| `query_signals` | Structured signal query (Postgres) | Read |
| `query_events` | Derived event query (Postgres, filter by severity/type) | Read |
| `inspect_entity` | Entity profile + assertions + recent events | Read |
| `search_facts` | Fact search by subject/predicate/value | Read |
| `query_graph` | Cypher graph queries (Apache AGE) | Read |
| `list_situations` | Active situations with event counts | Read |
| `list_watchlist` | Active watches with trigger counts | Read |
| `list_sources` | Source registry with health status | Read |
| `list_goals` | Goals with status and progress | Read |
| `search_memory` | Semantic search over episodic memory (Qdrant) | Read |
| `update_situation` | Update situation status | Write |
| `update_goal` | Update goal status/progress | Write |
| `send_message` | Send message to agent via NATS | Write |

**Provider handling:** vLLM gets a single combined user message; Anthropic gets proper system field + multi-turn messages — same branching logic as the agent but using providers directly.

### Operator Console v2 — "The Crossroads"

Multi-panel intelligence workstation built with React, running as a separate container on port **8503**. Designed for interactive analysis with draggable, resizable, tabbed panels.

**Stack:** React 18 + TypeScript + Vite, Tailwind CSS + shadcn/ui, Dockview (multi-panel layout), Sigma.js + Graphology (WebGL graph), MapLibre GL JS (geospatial map), vis-timeline (temporal events), TanStack Query (server state), Zustand (client state).

**Access:** `ssh -L 8503:localhost:8503 user@<your-host>` then `http://localhost:8503`

**22+ panels across 7 groups:**

| Group | Panels |
|-------|--------|
| Overview | Dashboard (KPIs, recent events, sparklines) |
| Intelligence | Signals (raw feed, search, filter), Events (derived, severity badges, type/category filter), Entities (search, type filter), Sources (CRUD), Goals (tree, status edit), Facts (search, delete) |
| Visualization | Knowledge Graph (Sigma.js, ForceAtlas2, ego graph, search highlight), Geospatial Map (MapLibre, dark tiles, fly-to), Timeline (vis-timeline, category colors) |
| Real-Time | Live Feed (SSE stream), Consult (AI chat, markdown rendered) |
| Tracking | Situations (status filter), Watchlist (CRUD, entity/keyword tracking), Proposed Edges (review queue) |
| Analysis | Hypotheses (ACH — competing thesis/counter-thesis pairs, evidence balance, diagnostic checklists), Situation Briefs (SYNTHESIZE deliverables, expandable markdown) |
| System | Analytics, Cycle Monitor (type detection), Journal (consolidation + entries), Reports (markdown, download), Scorecard |

**Cross-panel interactions:** Clicking an entity in Graph/Map/table selects it globally. Other panels react — Graph highlights, Map flies to location, Entity Detail opens. Selection history with back/forward.

**API layer:** v2 JSON endpoints at `/api/v2/*` (CRUD for all resources) plus proxied legacy endpoints (`/api/graph`, `/api/journal`, `/api/reports`, `/consult/*`, `/sse/*`).

Full details: [UI_V2.md](UI_V2.md)

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

**Consultation engine** (separate LLM config for `/consult`, falls back to main LLM config):

| Key | Default | Description |
|-----|---------|-------------|
| CONSULT_LLM_PROVIDER | (LLM_PROVIDER) | Provider for consult: `vllm` or `anthropic` |
| CONSULT_API_BASE | (OPENAI_BASE_URL) | Consultation LLM API endpoint |
| CONSULT_API_KEY | (OPENAI_API_KEY) | Consultation LLM API key |
| CONSULT_MODEL | (OPENAI_MODEL) | Consultation model name |
| CONSULT_MAX_TOKENS | (LLM_MAX_TOKENS) | Max output tokens for consultation |
| CONSULT_TEMPERATURE | (LLM_TEMPERATURE) | Sampling temperature for consultation |
| CONSULT_TIMEOUT | (LLM timeout) | Request timeout (seconds) |

**Ingestion service** (background event fetcher):

| Key | Default | Description |
|-----|---------|-------------|
| INGESTION_SERVICE_ACTIVE | false | When `true`, CURATE cycles replace ACQUIRE (agent curates events from signals) |
| INGESTION_CHECK_INTERVAL | 30 | Seconds between scheduler ticks |
| INGESTION_MAX_WORKERS | 4 | Concurrent source fetches |
| INGESTION_HTTP_TIMEOUT | 30 | Per-source fetch timeout (seconds) |
| INGESTION_DEDUP_CACHE_SIZE | 500 | Recent signals kept in memory for Jaccard dedup |
| INGESTION_BATCH_SIZE | 50 | Max signals per source fetch before store |
| INGESTION_HEALTH_PORT | 8600 | Health/metrics HTTP port |
| INGESTION_AUTO_PAUSE_THRESHOLD | 10 | Consecutive failures before auto-pause |
| INGESTION_LOG_LEVEL | INFO | Log level |

**Ingestion Redis keys:** `ingestion:activity` (recent fetch log), `ingestion:status` (service health), `last_ingestion_cycle` (gap tracking).

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
| UI CRUD | Operator console CRUD: fact delete/edit, memory delete, entity assertion add/remove, event delete/edit, graph edge add/remove, source full edit (htmx inline) |
| Cycle Decomposition | cycle.py split from 2005 lines to 192-line orchestrator + 10 phase mixin modules (phases/ directory) |
| V2 Cycle Architecture | 7 cycle types via 3-tier routing: Tier 1 scheduled (EVOLVE/30, INTROSPECTION/15, SYNTHESIZE/10), Tier 2 guaranteed modulo (ANALYSIS/4, RESEARCH/7, CURATE/9), Tier 3 dynamic fill (CURATE vs SURVEY scored by backlog). 15 phase mixins. Coprime Tier 2 intervals (4,7,9) minimize masking. SURVEY replaces NORMAL (analytical, no collection). SYNTHESIZE produces situation briefs. |
| Data Pipeline Hardening | 3-tier event dedup (GUID → source_url → adaptive Jaccard), source domain dedup relaxed (path-prefix instead of domain-level), entity completeness depth-weighted (assertions/3 per section), graph fuzzy match limit 100→500 |
| Watchlists & Situations | Persistent watch patterns (entities, keywords, categories, regions) with trigger tracking. Situation tracking (persistent narratives with status, event accumulation, intensity scoring). Both with full agent tools + operator UI CRUD. |
| EVOLVE Cycle | Self-improvement + source discovery cycle (every 30, highest priority). Operational scorecard, source discovery, prompt/tool evaluation, implement improvements, change tracking via `evolve_log`. 18-tool filtered set including filesystem + code_test for self-modification. |
| Report Grounding | Anti-inferential-leakage rules in analysis reports — banned "implicit"/"implied"/"inferred"/"suggests"/"appears to be" in fact sections. Separate "Analyst Hypotheses" section for labeled inference. |
| Source Rotation | Mandatory stale source fetching in ACQUIRE cycles — must fetch ≥2 sources not fetched in last 30 cycles. Coverage diversity directive (≥2 regions/types per acquire). |
| Entity Freshness | Leader freshness priority in RESEARCH cycles — re-verify leader assertions >100 cycles old. Stale entity stats in EVOLVE context. |
| JSON API Endpoints | `/api/reports` and `/api/journal` — raw JSON endpoints for programmatic access to reports and journal data. |
| Signals/Events Refactor | Two-tier data model: `signals` (raw ingested material) and `events` (derived real-world occurrences) with many-to-many `signal_event_links`. Events have severity, event_type, time range, signal count. Deterministic clustering in ingestion service. ACQUIRE → CURATE cycle, source discovery to EVOLVE. New agent tools: `event_create`, `event_update`, `event_query`, `event_link_signal`, `signal_store`, `signal_query`, `signal_search`. UI: Signals panel + Events panel with severity badges. |

### Planned

See `docs/WORKLOG.md` for the current work queue.

### Production Metrics

As of cycle ~2070: ~30,500 signals, ~1,100 events, ~13,400 active facts, ~598 entities, ~16,500 signal-level entity links, ~1,280 event-level entity links, 138 active sources (all categorized). Sub-1% error rate across 2,000+ autonomous cycles.

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
|   |   +-- schemas/                 -- Pydantic models (cycle, goals, memory, tools, signals, events, entities, sources, comms, modifications)
|   |   +-- config.py               -- LegbaConfig (temperature default 1.0)
|   +-- agent/
|   |   +-- main.py                  -- Entry point
|   |   +-- cycle.py                 -- Orchestrator (~195 lines), inherits 15 phase mixins
|   |   +-- phases/                  -- Phase mixin modules
|   |   |   +-- wake.py             -- WakeMixin: service init, tool registration (63 tools)
|   |   |   +-- orient.py           -- OrientMixin: memory/context + live infra health check + ingestion gap tracking + journal leads
|   |   |   +-- plan.py             -- PlanMixin: LLM planning + tool selection
|   |   |   +-- act.py              -- ActMixin: tool loop execution
|   |   |   +-- reflect.py          -- ReflectMixin: significance, facts, graph
|   |   |   +-- narrate.py          -- NarrateMixin: journal + consolidation + lead extraction
|   |   |   +-- persist.py          -- PersistMixin: storage, goals, ingestion tracking, heartbeat
|   |   |   +-- introspect.py       -- IntrospectMixin: mission review, reports
|   |   |   +-- research.py         -- ResearchMixin: entity enrichment
|   |   |   +-- curate.py           -- CurateMixin: event curation from clustered signals
|   |   |   +-- survey.py           -- SurveyMixin: analytical desk work (replaces NORMAL)
|   |   |   +-- synthesize.py       -- SynthesizeMixin: deep-dive investigation, situation briefs
|   |   |   +-- analyze.py          -- AnalyzeMixin: pattern detection, graph mining, anomaly detection
|   |   |   +-- evolve.py           -- EvolveMixin: self-improvement, operational scorecard, change tracking
|   |   +-- log.py                   -- CycleLogger (JSONL structured logging)
|   |   +-- llm/                     -- format.py, provider.py, client.py, tool_parser.py
|   |   +-- memory/                  -- manager.py, registers.py, episodic.py, structured.py, graph.py, opensearch.py
|   |   +-- goals/                   -- Goal CRUD + decomposition
|   |   +-- tools/
|   |   |   +-- registry.py
|   |   |   +-- executor.py
|   |   |   +-- builtins/            -- 19 modules (66 tools) + geo.py utility
|   |   +-- selfmod/                 -- Self-modification engine + rollback
|   |   +-- comms/                   -- NATS client, Airflow client
|   |   +-- prompt/
|   |       +-- templates.py         -- All prompt templates (includes information layers framing)
|   |       +-- assembler.py         -- Context assembly + token budget + world briefing injection
|   +-- supervisor/
|   |   +-- main.py, lifecycle.py, heartbeat.py, comms.py, cli.py, audit.py, drain.py
|   +-- ui/
|       +-- app.py, stores.py, messages.py, static/
|       +-- routes/                    -- dashboard, entities, events, sources, goals, cycles, messages, journal, reports, graph, facts, memory, watchlist, situations, consult, analytics
|       +-- templates/                 -- Jinja2 (base.html + per-page dirs)
+-- tests/                           -- 200+ tests (unit + integration + graph)
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
| Time-series | TimescaleDB (hypertables, HDX conflict baselines) |
| Dashboards | Grafana (auto-provisioned TimescaleDB datasource) |
| Orchestration | Apache Airflow (4 DAGs: metrics rollup, source health, decision surfacing, eval rubrics) |
| Analytics | PyOD, statsforecast, spaCy, NetworkX, scikit-learn |
| RSS/Feed | feedparser, trafilatura |
| Operator Console v1 | FastAPI, Jinja2, htmx, Tailwind CSS |
| Operator Console v2 | React 18, TypeScript, Vite, Dockview, Sigma.js, Graphology, MapLibre GL JS, vis-timeline, TanStack Query, Zustand, Tailwind CSS |
| Data models | Pydantic v2 |
