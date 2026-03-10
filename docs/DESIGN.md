# Legba — Implementation Design

*Key design decisions, data flows, and component interactions.*
*Last updated: 2026-03-10*

---

## 1. Design Philosophy

Legba is designed around three principles:

1. **Autonomy over orchestration** — The agent decides what to do, not a predefined pipeline. The supervisor manages lifecycle; the agent manages intelligence.
2. **Grounded over generative** — Every claim must trace to tool output, not LLM confabulation. Information layers (identity vs facts vs tools) are explicitly separated in prompts.
3. **Persistence over performance** — The agent runs continuously. Every design choice prioritizes reliability, graceful degradation, and data preservation over speed.

---

## 2. LLM Integration

### Why Single-Turn

Every LLM call is a fresh `[system, user]` message pair — no multi-turn conversation. This is deliberate:

- **Context control**: The assembler controls exactly what the LLM sees. No accumulated conversation drift.
- **Token budget enforcement**: Each call is budgeted independently (120k max). Flexible sections (memories, goals) are truncated first; system, inbox, and task request are never truncated.
- **GPT-OSS compatibility**: The model doesn't reliably handle the system role via `/v1/chat/completions`. Both messages are combined into a single `{"role": "user"}` message by `format.py:to_chat_messages()`.

### Why `{"actions": [...]}` Wrapper

GPT-OSS's reasoning mode expects exactly 2 output messages (reasoning + final). When tool calls were bare JSON objects, the model sometimes wrapped each in a separate Harmony message block, triggering `"Expected 2 output messages, but got N"` errors. The single-object wrapper ensures all tool calls live in one final message.

### Sliding Window

The tool loop (REASON+ACT) can run up to 20 steps. Each step rebuilds the full prompt. To prevent context exhaustion:
- The 8 most recent tool results are included in full
- Older results are condensed to one-line summaries
- A working memory (key observations noted by the LLM via `note_to_self`) persists across all steps

### Tool Loop Resilience

Two retry mechanisms prevent premature loop exits:
- **API error retry**: On transient LLM API errors (timeouts, 500s, rate limits), retries up to 2 times with exponential backoff. Failed attempts don't count against the step budget.
- **Format retry**: When the LLM returns an unparseable response (no valid `{"actions": [...]}` found), re-prompts up to 2 times with explicit format instructions before falling through to forced-final.

### Planned-Tool Filtering

After PLAN, the cycle parses a `Tools:` line from the plan output. During REASON:
- Listed tools get full parameter definitions in the system prompt
- All other tools get name + one-line description only
- `explain_tool` is registered for on-demand full-definition lookup

This saves ~5-10k tokens per cycle without limiting the agent's capabilities.

---

## 3. Memory Architecture

### Six Layers, Each with a Purpose

| Layer | Store | TTL | Purpose |
|-------|-------|-----|---------|
| Registers | Redis | Per-cycle | Counters, flags, cycle state, journal, reports |
| Short-term episodic | Qdrant | ~50 cycles | Recent actions/observations (1 per cycle) |
| Long-term episodic | Qdrant | Indefinite | Significant events auto-promoted at significance >= 0.6 |
| Structured | Postgres | Indefinite | Facts, goals, sources, events, entity profiles |
| Graph | Apache AGE | Indefinite | Entity relationships (Cypher queries) |
| Bulk | OpenSearch | Indefinite | Full-text search, event indices, aggregations |
| Journal archive | OpenSearch | Indefinite | Permanent record of all journal entries and consolidations (`legba-journal` index) |

### Why Separate Stores

Each store serves a different access pattern:
- **Qdrant** for "what's relevant to what I'm doing now?" (embedding similarity with time decay)
- **Postgres** for "what do I know about entity X?" (structured queries, JSONB profiles)
- **AGE** for "how are these entities connected?" (Cypher pattern matching)
- **OpenSearch** for "find all events mentioning this term" (full-text search + aggregations)
- **Redis** for "what happened last cycle?" (fast key-value, no persistence guarantees needed)

### Entity Resolution Cascade

When the agent encounters an entity name:
1. Exact canonical name match
2. Alias match (e.g., "Russian Federation" → "Russia")
3. Case-insensitive match
4. Fuzzy match (SequenceMatcher > 85%)
5. Create stub (completeness 0.0, to be filled later)

This prevents entity fragmentation — "Iran", "Islamic Republic of Iran", and "Tehran government" collapse to one node.

### Relationship Normalization

30 canonical relationship types with 70+ aliases normalized at the storage layer. The agent can say `"PresidentOf"` and it becomes `LeaderOf`. Unrecognized types fuzzy-match or fall back to `RelatedTo`. This keeps the graph schema consistent without constraining the LLM's natural language.

---

## 4. Cycle Flow

### Normal Cycle

```
main.py:main()
  └── AgentCycle.run()
        ├── _wake()
        │     ├── Load challenge.json, seed goal, world briefing
        │     ├── Connect: Redis, Postgres/AGE, Qdrant, OpenSearch, NATS, Airflow
        │     ├── _register_builtin_tools() → 50 tools from 15 modules
        │     └── Drain NATS inbox
        │
        ├── _orient()
        │     ├── Retrieve episodic memories (Qdrant similarity search)
        │     ├── Load active goals + goal work tracker (Postgres + Redis)
        │     ├── Query known facts (Postgres)
        │     ├── Build graph inventory (entity counts by type, top relationships)
        │     ├── Query source health stats (total sources, utilization %)
        │     ├── Get NATS queue summary
        │     ├── Load previous reflection forward
        │     └── Load journal context (consolidation + recent entries)
        │
        ├── _plan()
        │     ├── assemble_plan_prompt() → [system, user]
        │     ├── LLM completion → prose plan + "Tools: tool1, tool2, ..."
        │     └── Parse planned_tools set
        │
        ├── _act()
        │     ├── assemble_reason_prompt(planned_tools=...) → [system, user]
        │     └── llm_client.reason_with_tools()
        │           ├── For each step (up to 20):
        │           │     ├── LLM generates {"actions": [...]}
        │           │     ├── tool_parser extracts tool calls
        │           │     ├── executor runs tools concurrently
        │           │     ├── Results added to working memory
        │           │     ├── Sliding window: keep last 8 full, condense older
        │           │     ├── Re-grounding prompt every 8 steps
        │           │     └── Check graceful shutdown flag
        │           └── Returns results summary
        │
        ├── _reflect()
        │     ├── assemble_reflect_prompt() → [system, user]
        │     └── LLM → JSON: {summary, significance, facts, entities, goal_progress, ...}
        │
        ├── _narrate()
        │     ├── assemble_narrate_prompt() → [system, user]
        │     ├── LLM → JSON array of 1-3 journal entries
        │     └── Archive entries to OpenSearch (legba-journal index)
        │
        └── _persist()
              ├── Store episodic memory (Qdrant)
              ├── Auto-complete goals at 100% progress
              ├── Auto-promote memories (significance >= 0.6 → long-term)
              ├── Update goal work tracker (Redis)
              ├── Store facts from reflection (Postgres, normalized predicates, triple-deduped)
              ├── Publish outbox messages (NATS)
              ├── Store journal entries (Redis)
              └── Liveness check (nonce echo — dedicated LLM call, reasoning: low)
```

### Research Cycle (every 5, non-introspection)

Replaces PLAN → ACT with entity enrichment using a restricted tool set:

```
_wake() → _orient() →
  _research()
    ├── _build_entity_health_summary()
    │   └── SQL: entity completeness scores, event counts, assertion counts
    ├── assemble_research_prompt(entity_health=..., allowed_tools=RESEARCH_TOOLS)
    ├── reason_with_tools() → full tool loop (http_request, entity/graph/memory tools, os_search)
    ├── _reflect()
    ├── _narrate()
    └── _persist()
```

Research targets: entities with low completeness but high event involvement. Primary sources: Wikipedia API, official references, cross-referencing existing data.

### Introspection Cycle (every 15)

Replaces PLAN → ACT with a deep self-assessment using internal-only tools:

```
_wake() → _orient() →
  _run_introspection()
    ├── assemble_introspection_prompt() with restricted tools:
    │   graph_query, graph_store, memory_query, entity_inspect,
    │   goal_update, goal_list, note_to_self, explain_tool
    ├── reason_with_tools() → full tool loop with internal tools
    ├── _reflect()
    ├── _narrate()
    ├── _journal_consolidation()
    │   ├── LLM weaves all journal entries since last consolidation
    │   │   into a single narrative (Legba's inner voice)
    │   └── Archive consolidation to OpenSearch before clearing entries
    ├── _generate_analysis_report()
    │   ├── Query graph relationships, entity profiles, recent events
    │   └── LLM produces "Current World Assessment" (1000-3000 words)
    │       with strict anti-hallucination rules
    └── _persist()
```

---

## 5. Data Flow: Event Pipeline

```
RSS Feed → feed_parse(source_id) → structured entries
                │
                ├── Record source fetch (success/failure tracking)
                │
                └── event_store(title, summary, actors, locations, ...)
                      ├── GUID fast-path dedup (exact match on RSS guid/Atom id)
                      ├── Title dedup (50% word overlap within ±1 day, or last 100 events if no timestamp)
                      ├── Geo resolution (pycountry + GeoNames → ISO codes + coordinates)
                      ├── Store in Postgres (structured queries)
                      ├── Store in OpenSearch (full-text, time-partitioned: legba-events-YYYY.MM)
                      └── Increment source event count

      entity_resolve(name, event_id, role)
            ├── Resolution cascade (exact → alias → fuzzy → stub)
            ├── Create/update EntityProfile in Postgres
            └── Create EventEntityLink junction

      graph_store(entity, relate_to, relation_type, since)
            ├── Normalize relationship type (70+ aliases → 30 canonical)
            ├── Fuzzy entity matching (prevent duplicates)
            └── Upsert vertex + MERGE edge in AGE
```

---

## 6. Prompt Architecture

### Instructions-First, Data-Last

Every prompt follows this pattern:
```
SYSTEM: identity → rules → guidance addons → tool definitions → calling format
USER:   --- CONTEXT DATA --- → data sections → --- END CONTEXT --- → task request
```

The task request is always last — closest to where the LLM generates. Tool definitions are at the end of the system message for the same reason.

### Three Information Layers

The system prompt explicitly separates:
1. **Identity** — persona, analytical framework (how to think)
2. **Factual content** — world briefing, context injections, tool results (supersede training priors)
3. **Tools** — interface to the real world (tool results are ground truth)

This prevents the model from treating factual briefings as fiction.

### Guidance Addons

Six guidance modules are appended to the system prompt in PLAN, REASON, and INTROSPECTION phases:

| Addon | Content |
|-------|---------|
| MEMORY_MANAGEMENT | How to use memory tools, avoid duplicates, check before creating |
| EFFICIENCY | Batching, cost awareness, parallel tool calls |
| ANALYTICS | When to use statistical/NLP/graph analysis tools |
| ORCHESTRATION | Airflow workflow tools (conditional — only when Airflow is available) |
| SA_GUIDANCE | Source management, feed parsing, event quality, HTTP behavior, tagging |
| ENTITY_GUIDANCE | Entity profiles, resolution, temporal relationships, 30 canonical types |

### Phase-Specific Prompts

| Phase | System | User |
|-------|--------|------|
| PLAN | Full identity + all guidance + tool summary | Goals + memories + graph + journal + inbox + plan request |
| REASON | Full identity + all guidance + filtered tool defs + calling instructions | Context data (bracketed) + plan + working memory + act instruction |
| REFLECT | Lightweight identity | Plan + working memory + results summary |
| NARRATE | Minimal ("Write your journal entries") | Cycle summary + prior journal |
| Research | Full identity + all guidance + research tool defs | Entity health summary + research tasks |
| Introspection | Full identity + all guidance + internal tool defs | Mission review task + goals + work pattern |
| Journal Consolidation | Narrative voice identity | All entries since last consolidation |
| Analysis Report | Analyst identity | Queried data: graph, entities, events, coverage |
| Liveness | "You are a simple echo service" | Nonce + cycle number |

---

## 7. Safety Model

### Structural Isolation
- Agent cannot reach supervisor code or process
- Seed goal is read-only (mounted volume)
- Audit OpenSearch hostname is never given to the agent

### Heartbeat Liveness
- Dedicated LLM call in PERSIST: echo `nonce:cycle_number`
- Uses `reasoning: low` for efficiency
- Moved to last step so data is saved even if liveness fails
- Supervisor validates the response before marking cycle complete

### Auto-Rollback
- Self-modifications tracked in git inside the agent container
- Supervisor reverts modifications that break the agent (container exit != 0)

### Graceful Shutdown
- Supervisor writes `stop_flag.json` on soft timeout (300s default)
- Agent checks between tool steps → breaks to REFLECT → PERSIST
- Agent can write `stop_ping.json` to extend deadline (up to 2 extensions of 300s each)

### Full Audit Trail
- Every prompt, response, tool call, and error logged to JSONL
- Archived per-cycle in `/logs/archive/cycle_NNNNNN/`
- Indexed in audit OpenSearch (isolated, agent cannot access)
- 90-day ISM retention policy

---

## 8. Graph Database (Apache AGE)

### Why AGE, Not Neo4j

AGE runs as a PostgreSQL extension — no additional service to manage. Shares the same Postgres instance as structured data. Supports Cypher queries for pattern matching.

### AGE vs Neo4j Cypher Differences

Key syntax differences that affect LLM-generated queries:

| Pattern | Neo4j | AGE |
|---------|-------|-----|
| Find isolated nodes | `WHERE NOT (n)-[]-()` | `OPTIONAL MATCH (n)-[r]-() ... WHERE r IS NULL` |
| Check existence | `WHERE EXISTS { (n)-[:REL]->() }` | `OPTIONAL MATCH (n)-[r:REL]->() ... WHERE r IS NOT NULL` |
| Get vertex label | `labels(n)` | `label(n)` |
| Return aliases | Optional | Required for clean output (`AS name`) |

These differences are documented in the `graph_query` tool description so the LLM sees them when generating Cypher.

### Graph Schema

Vertices: labeled by entity type (CamelCase). Properties: `name`, `entity_id`, `created_at`, `updated_at`, plus arbitrary key-value pairs.

Edges: labeled by relationship type (30 canonical, CamelCase). Properties: `since`, `until` (temporal), plus arbitrary key-value pairs.

Entity deduplication: `upsert_entity` first checks for any existing vertex with the same name (regardless of label) before creating. This prevents duplicates when the entity type changes between calls.

---

## 9. Known Limitations

### LLM Behavioral Issues
- **Multi-message errors**: GPT-OSS occasionally generates multiple Harmony message blocks → 400 error. Mitigated by `{"actions": [...]}` wrapper, but still happens (~1-2% of steps).
- **Repetitive enrichment**: Agent sometimes re-does work it already completed. Partially addressed by goal work tracker and stall detection, but the LLM doesn't always check entity_inspect before re-enriching.
- **Dud cycles**: Occasionally the LLM generates a brief instead of executing tools (0 actions). The forced-final mechanism catches this but the cycle is wasted.

### Tool Utilization Gap
Only ~40% of registered tools have been used (20 of 50). Entire modules untouched: analytics (5 tools), orchestration (5), raw OpenSearch (6). The agent has converged on a core loop of ~15 tools focused on RSS ingestion → event storage → entity resolution → graph building. The analytical and orchestration tools represent future capability that the agent hasn't been guided to explore yet.

### Context Pressure
The full system prompt with all guidance addons is ~20k tokens. With tool definitions, goals, memories, and world briefing, REASON calls regularly hit 40-60k tokens — half the budget used before the LLM generates anything. The planned-tool filtering helps, but long-running tool loops with many results still approach the 120k budget.
