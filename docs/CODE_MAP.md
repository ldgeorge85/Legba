# Legba Code Map

**Generated:** 2026-03-16
**Total Python files:** 97
**Total lines of Python:** ~19,500

---

## 1. Complete File Listing

```
src/legba/
  __init__.py

  shared/
    __init__.py
    config.py                        (322 lines) — All configuration from env vars
    crypto.py                        (94 lines)  — Ed25519 signing for heartbeat
    schemas/
      __init__.py                    (37 lines)  — Re-exports all schema types
      comms.py                       (89 lines)  — Inbox/Outbox/NATS message schemas
      cycle.py                       (58 lines)  — Challenge/CycleResponse/CycleState
      entity_profiles.py             (147 lines) — EntityProfile, Assertion, EntityType
      signals.py                     (118 lines) — Signal, SignalCategory, create_signal (was events.py)
      derived_events.py              (85 lines)  — DerivedEvent, EventType, EventSeverity, SignalEventLink
      goals.py                       (141 lines) — Goal hierarchy: Goal, Milestone, GoalType
      memory.py                      (93 lines)  — Episode, Fact, Entity, Relationship
      modifications.py               (115 lines) — Self-modification tracking schemas
      sources.py                     (131 lines) — Source registry with trust metadata
      tools.py                       (74 lines)  — ToolDefinition, ToolCall, ToolResult

  agent/
    __init__.py
    main.py                          (49 lines)  — Entry point: asyncio.run(run_cycle())
    log.py                           (149 lines) — Structured JSON logging (CycleLogger)
    cycle.py                         (192 lines) — Orchestrator: AgentCycle inherits phase mixins

    phases/
      __init__.py                    (12 lines)  — Constants (REPORT_INTERVAL, RESEARCH_INTERVAL)
      wake.py                        (387 lines) — WakeMixin: init, connections, tool registration
      orient.py                      (212 lines) — OrientMixin: context from all memory layers
      plan.py                        (75 lines)  — PlanMixin: LLM plan + tool filtering
      act.py                         (65 lines)  — ActMixin: REASON+ACT tool loop
      reflect.py                     (181 lines) — ReflectMixin: structured extraction, fact/graph storage
      narrate.py                     (178 lines) — NarrateMixin: journal entries, consolidation, archival
      persist.py                     (362 lines) — PersistMixin: save state, liveness check, heartbeat
      introspect.py                  (319 lines) — IntrospectMixin: deep review, analysis reports
      research.py                    (157 lines) — ResearchMixin: entity enrichment, health summary
      curate.py                      (166 lines) — CurateMixin: signal review, event creation, editorial judgment

    llm/
      __init__.py
      format.py                      (137 lines) — Message formatting, Harmony stripping
      provider.py                    (184 lines) — VLLMProvider: HTTP to vLLM
      client.py                      (460 lines) — LLMClient + WorkingMemory + reason_with_tools
      tool_parser.py                 (167 lines) — Parse {"actions":[...]} from LLM output
      harmony_legacy.py              — Legacy harmony token handling (unused)

    memory/
      __init__.py
      manager.py                     (217 lines) — MemoryManager: unified interface
      registers.py                   (143 lines) — RegisterStore: Redis key-value
      episodic.py                    (302 lines) — EpisodicStore: Qdrant vector memory
      structured.py                  (~600 lines)— StructuredStore: Postgres CRUD
      graph.py                       (602 lines) — GraphStore: Apache AGE Cypher
      opensearch.py                  (339 lines) — OpenSearchStore: full-text search

    goals/
      __init__.py
      manager.py                     (182 lines) — GoalManager: CRUD + progress tracking

    tools/
      __init__.py
      registry.py                    (174 lines) — ToolRegistry: definition + handler store
      executor.py                    (74 lines)  — ToolExecutor: dispatch + logging
      subagent.py                    (129 lines) — Sub-agent execution engine
      builtins/
        __init__.py
        fs.py                        (174 lines) — fs_read, fs_write, fs_list
        shell.py                     (81 lines)  — exec (shell command)
        http.py                      (142 lines) — http_request (with trafilatura)
        memory_tools.py              (268 lines) — memory_store, memory_query, memory_promote, memory_supersede
        graph_tools.py               (437 lines) — graph_store, graph_query, graph_analyze
        goal_tools.py                (288 lines) — goal_create, goal_list, goal_update, goal_decompose
        nats_tools.py                (196 lines) — nats_publish, nats_subscribe, nats_create_stream, nats_queue_summary
        opensearch_tools.py          (252 lines) — os_create_index, os_index, os_search, os_delete_index, os_list_indices
        analytics_tools.py           (750 lines) — anomaly_detect, nlp_extract, forecast, graph_centrality
        orchestration_tools.py       (189 lines) — workflow_define, workflow_trigger, workflow_status, workflow_list
        feed_tools.py                (167 lines) — feed_parse (RSS/Atom with UA retry)
        source_tools.py              (356 lines) — source_register, source_list, source_update, source_get
        event_tools.py               (483 lines) — signal_store, signal_query, signal_search (was event_store/query/search)
        derived_event_tools.py       (435 lines) — event_create, event_update, event_query, event_link_signal
        entity_tools.py              (473 lines) — entity_profile, entity_inspect, entity_resolve
        selfmod_tools.py             (110 lines) — code_test (syntax + import validation)
        geo.py                       (177 lines) — Location normalization (pycountry + GeoNames)

    prompt/
      __init__.py
      templates.py                   (795 lines) — All prompt templates (system, plan, reflect, narrate, etc.)
      assembler.py                   (649 lines) — PromptAssembler: builds [system, user] messages

    selfmod/
      __init__.py
      engine.py                      (224 lines) — SelfModEngine: propose, apply, git commit
      rollback.py                    (85 lines)  — RollbackManager: restore from snapshots

    comms/
      __init__.py
      nats_client.py                 (405 lines) — LegbaNatsClient: NATS + JetStream
      airflow_client.py              (321 lines) — AirflowClient: Airflow REST API

  supervisor/
    __init__.py
    main.py                          (490 lines) — Supervisor: orchestrates agent lifecycle
    comms.py                         (155 lines) — CommsManager: human <-> agent messaging
    lifecycle.py                     (393 lines) — LifecycleManager: Docker container management
    heartbeat.py                     (109 lines) — HeartbeatManager: challenge-response protocol
    drain.py                         (71 lines)  — LogDrain: collect agent logs
    audit.py                         (242 lines) — AuditIndexer: index logs to OpenSearch
    cli.py                           (187 lines) — Operator CLI: send/read/status

  ui/
    __init__.py
    app.py                           (188 lines) — FastAPI app: Jinja2 + htmx + Tailwind
    messages.py                      (226 lines) — MessageStore (Redis) + UINatsClient
    stores.py                        (294 lines) — StoreHolder: read-only store connections + Qdrant helpers
    routes/
      __init__.py
      dashboard.py                   (71 lines)  — GET / stats dashboard
      messages.py                    (103 lines) — GET/POST /messages
      cycles.py                      (187 lines) — GET /cycles/{n}
      events.py                      (176 lines) — CRUD /events: list, detail, delete, metadata edit
      entities.py                    (195 lines) — CRUD /entities: list, detail, add/remove assertions
      sources.py                     (198 lines) — CRUD /sources: list, detail, create, edit, delete, status
      goals.py                       (83 lines)  — CRUD /goals: list, create, status, delete
      graph.py                       (221 lines) — GET /graph (Cytoscape.js) + edge add/remove
      journal.py                     (30 lines)  — GET /journal
      reports.py                     (47 lines)  — GET /reports
      facts.py                       (161 lines) — CRUD /facts: list, paginated rows, delete, inline edit
      memory.py                      (87 lines)  — GET /memory + DELETE episodes from Qdrant

  ingestion/
    __init__.py
    __main__.py                      (5 lines)   — Entry point
    config.py                        (50 lines)  — Ingestion-specific config
    service.py                       (540 lines) — IngestionService: tick loop, batch entity linking
    scheduler.py                     (130 lines) — Source fetch scheduling
    fetcher.py                       (588 lines) — HTTP/RSS fetching with retry
    normalizer.py                    (401 lines) — Content normalization pipeline
    source_normalizers.py            (922 lines) — Per-source format normalizers
    dedup.py                         (329 lines) — 3-tier signal dedup (GUID → source_url → Jaccard)
    storage.py                       (498 lines) — Signal storage to Postgres + OpenSearch
    cluster.py                       (531 lines) — SignalClusterer: deterministic signal-to-event clustering

scripts/
  migrate_signals_events.sql         (131 lines) — DDL migration: events→signals, events_derived→events
```

---

## 2. Module-by-Module Documentation

### 2.1 `src/legba/shared/` — Shared Configuration and Schemas

#### `shared/config.py`
**Purpose:** All configuration loaded from environment variables. Frozen dataclasses with `from_env()` class methods.

**Key classes:**
- `LLMConfig` — LLM endpoint, model name (default: `InnoGPT-1`), max_tokens, temperature, embedding model
- `RedisConfig` — Redis connection (host, port, db, password)
- `PostgresConfig` — Postgres connection with `.dsn` property
- `QdrantConfig` — Qdrant vector DB connection
- `NatsConfig` — NATS + JetStream URL and timeout
- `OpenSearchConfig` — OpenSearch connection; also `from_audit_env()` for supervisor's isolated audit instance
- `AirflowConfig` — Airflow REST API URL, credentials, DAGs path
- `PathConfig` — Filesystem paths: seed_goal, workspace, agent_code, shared, logs; properties for inbox/outbox/challenge/response
- `AgentConfig` — Tuning knobs: max_reasoning_steps (20), max_subagent_steps (10), memory_retrieval_limit (12), facts_retrieval_limit (20), max_context_tokens (120000), mission_review_interval (15), Qdrant collection names
- `SupervisorConfig` — max_consecutive_failures (5), cycle_sleep (2s), heartbeat_timeout (300s)
- `LegbaConfig` — Top-level aggregator of all sub-configs

**External deps:** `python-dotenv`

#### `shared/crypto.py`
**Purpose:** Ed25519 signing/verification for supervisor-agent challenge-response and self-modification accountability.

**Key functions:**
- `hash_payload(payload)` — SHA-256 canonical JSON hash
- `generate_keypair(private_path, public_path)` — Generate Ed25519 keypair
- `load_signing_key(path)` / `load_verify_key(path)` — Load keys from files
- `sign_message(key, message)` / `verify_message(key, sig, message)` — Sign/verify strings
- `sign_challenge_response(key, nonce, cycle)` — Sign `nonce:cycle_number`
- `verify_challenge_response(key, sig, nonce, cycle)` — Verify challenge response

**External deps:** `PyNaCl` (nacl.signing, nacl.encoding)

#### `shared/schemas/cycle.py`
**Purpose:** Supervisor-agent protocol schemas.

**Key classes:**
- `Challenge` — Supervisor issues: cycle_number, nonce (UUID), timeout_seconds
- `CycleResponse` — Agent returns: cycle_number, nonce, status (completed|error|partial), cycle_summary, actions_taken, signature
- `CycleState` — In-process tracking: phase (wake|orient|reason|act|reflect|persist|idle), nonce, seed_goal, inbox_messages, tool_results, reasoning_steps

**External deps:** `pydantic`

#### `shared/schemas/goals.py`
**Purpose:** Goal hierarchy: Seed Goal -> Meta Goals -> Goals -> Sub-goals -> Tasks.

**Key classes:**
- `GoalType` enum — meta_goal, goal, subgoal, task
- `GoalStatus` enum — active, paused, blocked, deferred, completed, abandoned
- `GoalSource` enum — seed, agent, human, subgoal
- `Milestone` — Weighted completion milestones
- `Goal` — Full goal model: hierarchy (parent_id, child_ids), progress (progress_pct, milestones), dependencies (blocked_by, blocks), deferral (deferred_until_cycle, defer_reason)
- `GoalUpdate` — Partial update model
- Factory functions: `create_goal()`, `create_subgoal()`, `create_task()`

#### `shared/schemas/memory.py`
**Purpose:** Memory data structures stored across layers.

**Key classes:**
- `EpisodeType` enum — action, observation, reasoning, cycle_summary, lesson, interaction
- `Episode` — Episodic memory (Qdrant): content, significance (0-1), embedding (1024-dim), goal_id, tool_name, tags
- `Fact` — Structured fact (Postgres): subject, predicate, value, confidence, superseded_by
- `Entity` — Graph entity (AGE): name, entity_type, properties
- `Relationship` — Directed graph edge: source_id, target_id, relation_type, properties

#### `shared/schemas/signals.py` (was `events.py`)
**Purpose:** Signal schemas — raw ingested material from external sources (RSS items, API responses, feed entries). Signals are the atomic unit of collection; events are derived from them.

**Key classes:**
- `SignalCategory` enum — conflict, political, economic, technology, health, environment, social, disaster, other
- `EventCategory` — Backward-compat alias for `SignalCategory`
- `Signal` — Full signal: title, summary, full_content, raw_content, event_timestamp, source_id/source_url, category, confidence, actors[], locations[], tags[], geo_countries[] (ISO alpha-2), geo_regions[], geo_coordinates[{name, lat, lon}], guid, language
- `Event` — Backward-compat alias for `Signal`
- `create_signal()` — Factory function
- `create_event()` — Backward-compat alias for `create_signal()`

#### `shared/schemas/derived_events.py`
**Purpose:** Derived event schemas — real-world occurrences derived from one or more signals. Events are the primary analytical unit; reports, situations, and graph analysis operate on events, not raw signals.

**Key classes:**
- `EventType` enum — incident (discrete), development (ongoing), shift (state change), threshold (metric crossing)
- `EventSeverity` enum — critical, high, medium, low, routine
- `DerivedEvent` — Full event: title, summary, category (SignalCategory), event_type, severity, time_start/time_end (temporal window), locations[], geo_countries[], geo_coordinates[], actors[], tags[], confidence, signal_count, source_method ("auto"/"agent"/"manual"), source_cycle
- `SignalEventLink` — Many-to-many junction: signal_id, event_id, relevance (0.0-1.0)

#### `shared/schemas/entity_profiles.py`
**Purpose:** Versioned, sourced entity profiles forming the "Persistent World Model."

**Key classes:**
- `EntityType` enum — 15 types: country, organization, person, location, military_unit, political_party, armed_group, international_org, corporation, media_outlet, event_series, concept, commodity, infrastructure, other
- `Assertion` — Sourced claim: key, value, confidence, source_event_id, source_url, observed_at, superseded flag
- `EntityProfile` — Versioned profile: canonical_name, entity_type, aliases, summary, sections (dict of Assertion lists), tags, completeness_score, event_link_count, version
- `SignalEntityLink` — Junction table (was `EventEntityLink`): signal_id, entity_id, role (actor|location|target|mentioned), confidence

**Key functions:**
- `EntityProfile.compute_completeness()` — Heuristic score based on expected sections for entity type

#### `shared/schemas/sources.py`
**Purpose:** Source registry with multi-dimensional trust metadata.

**Key classes:**
- `SourceType` enum — rss, api, scrape, manual
- `BiasLabel` enum — far_left through far_right
- `OwnershipType` enum — state, corporate, nonprofit, public_broadcast, independent
- `CoverageScope` enum — global, regional, national, local
- `SourceStatus` enum — active, paused, error, retired
- `Source` — Full model: trust dimensions (reliability, bias_label, ownership_type, geo_origin, language, timeliness, coverage_scope), operational state, reliability tracking (fetch_success_count, fetch_failure_count, events_produced_count, consecutive_failures)

#### `shared/schemas/comms.py`
**Purpose:** Human communication channel + NATS message schemas.

**Key classes:**
- `MessagePriority` enum — normal, urgent, directive
- `InboxMessage` — Supervisor -> agent: content, priority, requires_response
- `OutboxMessage` — Agent -> supervisor: content, in_reply_to, cycle_number
- `Inbox` / `Outbox` — Container models (serialized to JSON files)
- `NatsMessage` — Generic NATS data message: subject, payload, headers, sequence
- `StreamInfo` — JetStream stream summary
- `QueueSummary` — ORIENT context: human_pending, data_streams, total_data_messages

#### `shared/schemas/tools.py`
**Purpose:** Tool system schemas.

**Key classes:**
- `ToolParameter` — Parameter definition: name, type, description, required, default
- `ToolDefinition` — Tool definition: name, description, parameters, return_type, builtin flag, source_file; `to_typescript()` renderer
- `ToolCall` — Parsed invocation: tool_name, arguments, raw_text
- `ToolResult` — Execution result: success, result, error, duration_ms

#### `shared/schemas/modifications.py`
**Purpose:** Self-modification tracking.

**Key classes:**
- `ModificationType` enum — code, prompt, tool, config
- `ModificationStatus` enum — proposed, applied, failed, rolled_back
- `CodeSnapshot` — Before/after file state: file_path, content, content_hash, line_count; `capture()` class method
- `ModificationProposal` — Intent: file_path, rationale, expected_outcome, new_content, goal_id
- `ModificationRecord` — Full audit record: before/after snapshots, applied_at, error, rolled_back_at
- `RollbackResult` — success, rolled_back_records[], error

---

### 2.2 `src/legba/agent/main.py` — Agent Entry Point

**Purpose:** Single-cycle entry point. The supervisor launches this for each cycle.

**Key functions:**
- `run_cycle()` — Creates `LegbaConfig.from_env()`, instantiates `AgentCycle(config)`, calls `cycle.run()`, returns exit code 0/1
- `main()` — Entry point for `python -m legba.agent.main`. Wraps `run_cycle()` in `asyncio.run()`.

**Internal deps:** `shared.config.LegbaConfig`, `agent.cycle.AgentCycle`

---

### 2.3 `src/legba/agent/log.py` — Structured JSON Logging

**Purpose:** Per-cycle structured JSON logging. Writes JSONL files to the log drain volume.

**Key class: `CycleLogger`**
- `__init__(log_dir, cycle_number)` — Creates cycle-specific log file
- `update_cycle_number(n)` — Renames log file once real cycle number is known
- `log(event, **data)` — General structured log entry
- `log_llm_call(purpose, prompt, response, ...)` — Full LLM call with prompt/response
- `log_tool_call(tool_name, arguments, result, ...)` — Tool execution
- `log_phase(phase)` — Cycle phase transition
- `log_error(error)` — Error with stderr output
- `log_memory(operation, store)` — Memory operation
- `log_self_mod(action, file_path)` — Self-modification event

---

### 2.4 `src/legba/agent/cycle.py` — Core Agent Cycle (1827 lines)

**Purpose:** The heart of Legba. Executes one complete cycle through all phases.

**Key class: `AgentCycle`**

**Constructor** wires together all subsystems:
- `LLMClient`, `MemoryManager`, `GoalManager`, `ToolRegistry`, `ToolExecutor`
- `SelfModEngine`, `PromptAssembler`, `LegbaNatsClient`, `OpenSearchStore`, `AirflowClient`

**Constants:**
- `REPORT_INTERVAL = 5` — Status report every 5 cycles
- `INTROSPECTION_TOOLS` — frozenset of tools allowed during introspection

**Phase methods (detailed below in Section 3):**
- `run()` — Main entry: calls all phases, handles errors, runs cleanup
- `_wake()` — Initialize all connections and services
- `_orient()` — Gather context from all memory layers
- `_plan()` — LLM decides what to do this cycle
- `_reason_and_act()` — LLM reasoning loop with tool execution
- `_reflect()` — Extract facts, entities, relationships from results
- `_narrate()` — Write 1-3 journal entries
- `_persist()` — Save everything, emit heartbeat
- `_mission_review()` — Deep introspection with restricted tools
- `_journal_consolidation()` — Consolidate journal entries into narrative
- `_generate_analysis_report()` — Full "Current World Assessment"
- `_validate_liveness()` — LLM echoes nonce:cycle_number
- `_cleanup()` — Close all connections

**Helper methods:**
- `_is_introspection_cycle()` — cycle_number % mission_review_interval == 0
- `_parse_reflection(text)` — Extract JSON with "cycle_summary" key from LLM output
- `_store_reflection_facts()` — Store facts from reflection data
- `_store_reflection_graph()` — Store entities/relationships with fuzzy dedup
- `_parse_planned_tools(plan_text)` — Extract "Tools: a, b, c" from plan
- `_check_stop_flag()` / `_send_ping()` / `_make_stop_checker()` — Graceful shutdown
- `_register_builtin_tools()` — Wire all 14 builtin tool modules
- `_register_note_to_self()` — Working memory notes
- `_register_cycle_complete()` — Clean exit from tool loop
- `_register_explain_tool()` — On-demand tool definition lookup
- `_register_subagent()` — Spawn sub-agent tool

**Internal deps:** Every agent module. This is the central orchestrator.

---

### 2.5 `src/legba/agent/llm/` — LLM Subsystem

#### `llm/format.py`
**Purpose:** Message formatting and Harmony token stripping.

**Key types/functions:**
- `Message` — Dataclass: role (system|user|assistant), content
- `strip_harmony_response(text)` — Remove GPT-OSS Harmony channel markers (`<|channel|>final<|message|>...`, `assistantfinal`, stray `<|...|>` tokens)
- `to_chat_messages(messages)` — Combine all Messages into a single `{"role": "user", "content": ...}` dict (GPT-OSS doesn't handle system role reliably)
- `format_tool_result(tool_name, result)` — Format as `[Tool Result: name]\nresult`
- `format_tool_definitions(tools, only)` — JSON block with full params for `only` tools, name+description for rest
- `format_tool_summary(tools)` — Compact name+description list for PLAN phase

#### `llm/provider.py`
**Purpose:** HTTP client for vLLM's OpenAI-compatible API.

**Key class: `VLLMProvider`**
- `__init__(api_base, api_key, model, timeout, temperature, top_p)` — Configures httpx client
- `chat_complete(messages, max_tokens, temperature, ...)` — POST to `/chat/completions`
  - Always sends `temperature: 1.0` (GPT-OSS requirement)
  - Retries on 429/500/502/503 with exponential backoff (up to 3 retries)
  - Strips Harmony markers from response via `strip_harmony_response()`
- `close()` — Close httpx client

**Key type: `LLMResponse`** — content, finish_reason, usage dict, raw_response

**Key type: `LLMApiError`** — Non-retryable error with status_code, body, msg_count, total_chars

**External deps:** `httpx`

#### `llm/client.py`
**Purpose:** High-level LLM client with tool call loop and context management.

**Key class: `WorkingMemory`**
- In-cycle scratchpad for observations, tool results, notes
- `add_tool_result(step, tool_name, args_summary, result_summary)`
- `add_note(note)`
- `summary()` — Condensed text for re-grounding prompts
- `full_text()` — Detailed text for REFLECT phase
- Does NOT persist across cycles

**Key class: `LLMClient`**
- `__init__(config, logger, provider)` — Creates VLLMProvider + WorkingMemory
- `complete(messages, purpose, max_tokens, temperature)` — Single completion with logging
- `reason_with_tools(messages, tool_executor, purpose, max_steps, stop_check)` — The REASON-ACT loop:
  1. Extracts system message (stays constant, contains tool defs + calling instructions)
  2. Splits user message at `--- END CONTEXT ---` separator
  3. Each step: sends [system, user] to LLM
  4. Parses tool calls from response via `parse_tool_calls()`
  5. Executes tools concurrently (max 4 concurrent, deduplicates identical calls)
  6. Builds next step's user message via `_build_step_message()`
  7. Sliding window: last 8 tool steps in full detail, older condensed to one-liners
  8. On `cycle_complete` tool call: breaks out of loop
  9. On step budget exhaustion: forces final response with `BUDGET_EXHAUSTED_PROMPT`
  10. Returns (final_response, history_messages)
- `generate_embedding(text)` — POST to `/embeddings` endpoint
- `_build_step_message(base_context, tool_history, final_prompt)` — Constructs user message with tool history + working memory
- `_format_tool_history(tool_history)` — Sliding window condensation

**Constants:** `MAX_CONCURRENT_TOOLS = 4`, `SLIDING_WINDOW_SIZE = 8`, `CONDENSED_RESULT_MAX_CHARS = 2000`, `MAX_TOOL_RESULT_CHARS = 30000`

**Internal deps:** `format.py`, `provider.py`, `tool_parser.py`, `prompt.templates`

#### `llm/tool_parser.py`
**Purpose:** Parse tool invocations from LLM output.

**Key function: `parse_tool_calls(text) -> list[ToolCall]`**
Three parsing strategies (tried in order):
1. **Primary:** `{"actions": [{"tool": "name", "args": {...}}, ...]}` — single JSON wrapper
2. **Fallback:** Bare `{"tool": "name", "args": {...}}` objects
3. **Legacy:** `to=functions.NAME json{...}` format

**Helper functions:**
- `has_tool_call(text)` — Quick check for `"actions"`, `"tool"`, or `to=functions.`
- `_extract_balanced_braces(text)` — Parse balanced `{...}` with string awareness
- `_parse_json_safe(text)` — JSON parse with cleanup of `<|end|>` etc.; falls back to `ast.literal_eval` for Python dict literals
- `_clean_tool_name(name)` — Strip merged "json" suffix
- `_extract_tool_call(parsed, raw)` — Convert dict to ToolCall

---

### 2.6 `src/legba/agent/memory/` — Memory Subsystem

#### `memory/manager.py`
**Purpose:** Unified interface across all memory layers.

**Key class: `MemoryManager`**
- **Owns:** `RegisterStore` (Redis), `EpisodicStore` (Qdrant), `StructuredStore` (Postgres), `GraphStore` (AGE)
- `connect()` — Connect to all backends; each degrades gracefully
- `close()` — Close all connections
- `get_cycle_number()` / `increment_cycle()` — Cycle counter from Redis
- `retrieve_context(query_embedding, limit, current_cycle)` — ORIENT phase retrieval:
  - Registers (all keys from Redis)
  - Episodes (semantic search across short-term + long-term with time decay)
  - Goals (active goals from Postgres)
  - Facts (merged: semantic Qdrant search + structured Postgres query + recent-cycle facts, deduped by subject, max 2 per subject)
- `store_episode(episode)` — PERSIST: store to Qdrant short-term
- `store_fact(fact, embedding)` — PERSIST: store to both Postgres + Qdrant semantic index
- `save_goal(goal)` — Store goal to Postgres

#### `memory/registers.py`
**Purpose:** Redis-backed key-value store with in-memory fallback.

**Key class: `RegisterStore`**
- All keys prefixed with `legba:`
- Falls back to in-memory dict if Redis unavailable
- Operations: `get/set` (scalar), `incr/get_int` (counter), `set_flag/get_flag` (boolean), `set_json/get_json` (JSON), `get_all_registers` (bulk scan)

**External deps:** `redis.asyncio`

#### `memory/episodic.py`
**Purpose:** Vector-based episodic memory using Qdrant.

**Key class: `EpisodicStore`**
- Three collections: `SHORT_TERM`, `LONG_TERM`, `FACTS` (all 1024-dim cosine)
- `connect()` — Creates collections if they don't exist
- `store_episode(episode, collection)` — Upsert point with payload (cycle_number, episode_type, content, significance, tags, metadata)
- `search_similar(query_vector, collection, limit, min_score, filters)` — Vector search with optional payload filters
- `search_both(query_vector, limit, decay_hours)` — Search across both collections with time-based relevance decay (exponential, half-life 168h = 1 week)
- `promote_to_long_term(episode_id, vector, payload)` — Move from short-term to long-term (upsert + delete)
- `store_fact_embedding(fact_id, text, embedding, ...)` — Store fact in FACTS collection
- `search_facts(query_vector, limit)` — Semantic fact search (no time decay)
- `remove_fact_embedding(fact_id)` — Delete superseded fact from index

**External deps:** `qdrant-client`

#### `memory/structured.py` (~600 lines)
**Purpose:** PostgreSQL-backed store for goals, facts, sources, signals, events, entity profiles.

**Key class: `StructuredStore`**
- `connect()` — Creates asyncpg pool + runs `_ensure_tables()`
- **Tables created:** goals, facts, modifications, sources, signals, events, signal_event_links, entity_profiles, entity_profile_versions, signal_entity_links, event_entity_links, situation_signals, situation_events
- **Additive migrations:** Source reliability tracking columns (safe to re-run)

**Goal operations:** `save_goal`, `get_goal`, `get_active_goals`, `get_all_goals`, `get_deferred_goals`
**Fact operations:** `store_fact`, `query_facts(subject, limit)`, `query_facts_recent(current_cycle, lookback, limit)`, `supersede_fact(old_id, new_fact)`
**Source operations:** `save_source`, `get_source`, `get_sources(status, source_type, limit)`, `find_source_by_url(url)`, `record_source_fetch(source_id, success, error, events_count)`, `increment_source_event_count(source_id)`
**Signal operations:** `save_signal`, `get_signal`, `get_signals(limit, category, source_id)`, `check_signal_guid(guid)`, `query_signals(limit, category, source_id)`, `find_duplicate_signal(title, event_timestamp)`
**Derived event operations:** `save_derived_event`, `get_derived_event`, `query_derived_events(category, event_type, severity, since, until, min_signal_count, source_method, limit)`, `link_signal_to_event(signal_id, event_id, relevance)`
**Entity profile operations:** `save_entity_profile`, `get_entity_profile(id)`, `get_entity_profile_by_name(name)`, `search_entity_profiles(query, entity_type, limit)`, `save_signal_entity_link`, `get_signal_entity_links(signal_id)`

**External deps:** `asyncpg`

#### `memory/graph.py`
**Purpose:** Apache AGE (graph extension for Postgres) with Cypher query support.

**Key class: `GraphStore`**
- Graph name: `legba_graph`
- `connect()` — Creates AGE extension, pool with per-connection codec registration (LOAD 'age' + search_path), creates graph
- `_cypher(conn, query, cols)` — Execute Cypher via `SELECT * FROM cypher(...)`
- `_parse_agtype(val)` — Parse AGE text values (vertex, edge, path, string, number, etc.)
- `_sanitize_label(raw)` — Convert to CamelCase Cypher label
- `_escape(val)` — Escape for Cypher single-quoted literals

**Entity operations:**
- `upsert_entity(entity)` — Match-first, create-if-absent to prevent label-change duplicates
- `find_entity(name)` — Case-insensitive exact match
- `search_entities(query, entity_type, limit)` — Fuzzy name search with optional type filter

**Relationship operations:**
- `add_relationship(source_name, target_name, relation_type, properties, since, until)` — MERGE edge between named entities
- `get_relationships(entity_name, direction, relation_type, limit)` — Get outgoing/incoming/both edges

**Graph queries:**
- `find_path(source, target, max_depth)` — Shortest path between entities
- `query_subgraph(entity_name, depth, limit)` — N-hop neighborhood with edges
- `execute_cypher(query)` — Raw Cypher execution with automatic column inference from RETURN clause

**External deps:** `asyncpg`

#### `memory/opensearch.py`
**Purpose:** Async OpenSearch client for full-text search and aggregations.

**Key class: `OpenSearchStore`**
- `connect()` — Verifies connection, clears create-index blocks
- **Index management:** `create_index`, `delete_index`, `list_indices`
- **Document CRUD:** `index_document`, `bulk_index`, `get_document`, `delete_document`
- **Search:** `search(index, query, size, sort, source)` — Returns `{hits, total, took_ms}`
- **Aggregations:** `aggregate(index, aggs, query, size)` — Returns `{aggregations, took_ms}`

**External deps:** `opensearch-py`

---

### 2.7 `src/legba/agent/goals/manager.py` — Goal Management

**Purpose:** CRUD operations for the goal hierarchy.

**Key class: `GoalManager`**
- `get_active_goals()` / `get_all_goals()` / `get_goal(id)`
- `select_focus(goals)` — Highest priority (lowest number) active goal
- `create_goal(description, goal_type, priority, source, parent_id, success_criteria)`
- `decompose(parent, subtask_descriptions)` — Create sub-goals and update parent's child_ids
- `update_progress(goal_id, progress_pct, summary)` — Update with timestamp
- `complete_goal(goal_id, reason, summary)` — Set completed status
- `abandon_goal(goal_id, reason)` — Set abandoned status
- `defer_goal(goal_id, reason, revisit_after_cycles, current_cycle)` — Set deferred with revisit cycle
- `get_deferred_goals(current_cycle)` — Get goals whose deferred_until_cycle has passed

**Internal deps:** `StructuredStore`, `CycleLogger`

---

### 2.8 `src/legba/agent/tools/` — Tool System

#### `tools/registry.py`
**Purpose:** Manages tool definitions and handlers (both builtin and dynamic).

**Key class: `ToolRegistry`**
- `register(definition, handler)` — Register a tool
- `get_definition(name)` / `get_handler(name)` / `list_tools()`
- `to_tool_data()` — Raw dicts for prompt rendering
- `to_tool_definitions(only)` — Formatted block for LLM context (with optional filtering)
- `to_tool_summary()` — Compact name+description for PLAN phase
- `load_dynamic_tools()` — Scan `/agent/tools/*.json` for dynamic tool definitions; supports `shell` and `python` implementations

#### `tools/executor.py`
**Purpose:** Dispatches tool calls to handlers with logging.

**Key class: `ToolExecutor`**
- `execute(tool_name, arguments)` — Look up handler in registry, execute, log result/error. This is the callable passed to `LLMClient.reason_with_tools()`.

#### `tools/subagent.py`
**Purpose:** Sub-agent execution engine for the `spawn_subagent` tool.

**Key function: `run_subagent(task, context, allowed_tools, max_steps, llm_client, registry, logger)`**
- Creates fresh context with `SUBAGENT_SYSTEM_PROMPT` (reasoning: high, focused rules)
- Builds [system, user] messages with filtered tool definitions
- Runs `llm_client.reason_with_tools()` with a `filtered_executor` that restricts to allowed tools
- Returns the sub-agent's final response text

---

### 2.9 `src/legba/agent/tools/builtins/` — Built-in Tool Modules

Each module exports a `register(registry, **deps)` function called by `cycle.py._register_builtin_tools()`.

| Module | Tools | Purpose |
|---|---|---|
| `fs.py` | `fs_read`, `fs_write`, `fs_list` | Filesystem operations. `/agent` writes routed through SelfModEngine |
| `shell.py` | `exec` | Shell command execution with timeout ceiling |
| `http.py` | `http_request` | HTTP with trafilatura HTML extraction, within-cycle GET cache, browser UA retry on 403/405 |
| `memory_tools.py` | `memory_store`, `memory_query`, `memory_promote`, `memory_supersede` | Explicit memory CRUD: store episodes/facts, semantic search, promote to long-term, supersede facts |
| `graph_tools.py` | `graph_store`, `graph_query`, `graph_analyze` | Entity/relationship CRUD in AGE graph. Includes `RELATIONSHIP_ALIASES` (30+ canonical types with synonyms), `normalize_relationship_type()`, `_find_similar_entity()` for fuzzy dedup |
| `goal_tools.py` | `goal_create`, `goal_list`, `goal_update`, `goal_decompose` | Goal hierarchy CRUD via GoalManager |
| `nats_tools.py` | `nats_publish`, `nats_subscribe`, `nats_create_stream`, `nats_queue_summary` | NATS event bus operations |
| `opensearch_tools.py` | `os_create_index`, `os_index`, `os_search`, `os_delete_index`, `os_list_indices` | OpenSearch document management and search |
| `analytics_tools.py` | `anomaly_detect`, `nlp_extract`, `forecast`, `graph_centrality` | Statistical analysis (Isolation Forest, LOF), NLP (keyword extraction via YAKE), time-series forecasting, graph centrality (PageRank, betweenness, degree) |
| `orchestration_tools.py` | `workflow_define`, `workflow_trigger`, `workflow_status`, `workflow_list` | Airflow DAG deployment, triggering, monitoring |
| `feed_tools.py` | `feed_parse` | RSS/Atom feed parsing with feedparser, browser UA retry on 403/405, source reliability tracking via `record_source_fetch()` |
| `source_tools.py` | `source_register`, `source_list`, `source_update`, `source_get` | Source registry CRUD with dedup (checks existing URL, limit 500), auto-pause at 5 consecutive failures |
| `event_tools.py` | `signal_store`, `signal_query`, `signal_search` | Signal storage to Postgres + OpenSearch (was event_store/query/search). Auto geo-resolution via `geo.py`. 3-tier dedup. `increment_source_event_count` on store |
| `derived_event_tools.py` | `event_create`, `event_update`, `event_query`, `event_link_signal` | Derived event CRUD. Agent-created events start at confidence 0.7. Link signals as evidence |
| `entity_tools.py` | `entity_profile`, `entity_inspect`, `entity_resolve` | Entity profile CRUD in Postgres + AGE sync. Profile versioning. Event-entity linking |
| `selfmod_tools.py` | `code_test` | Syntax check + import validation before self-modifications |
| `geo.py` | (internal, not a tool) | Location normalization: `resolve_locations(locations)` using pycountry + GeoNames cities15000 gazetteer. Returns `{countries, regions, coordinates}` |

**Additionally registered in `cycle.py` (not in builtin modules):**
- `note_to_self` — Write to WorkingMemory within this cycle
- `cycle_complete` — Signal clean exit from tool loop (intercepted in client.py)
- `explain_tool` — Get full parameter details for any tool on demand
- `spawn_subagent` — Delegate work to a sub-agent with its own context window

**Total registered tools: 47+**

---

### 2.10 `src/legba/agent/prompt/` — Prompt System

#### `prompt/templates.py` (795 lines)
**Purpose:** All prompt templates. The agent can modify these via self-modification.

**Key templates:**
- `CONTEXT_DATA_SEPARATOR` / `CONTEXT_END_SEPARATOR` — Bracket data sections in user message
- `SYSTEM_PROMPT` — Identity ("You ARE the loa"), cycle number, behavioral rules, output format (`{"actions": [...]}`)
- `TOOL_CALLING_INSTRUCTIONS` — JSON format spec, concurrent calls, cycle_complete usage
- `BOOTSTRAP_PROMPT_ADDON` — Extra guidance for first 5 cycles
- `MEMORY_MANAGEMENT_GUIDANCE` — significance >= 0.6 promotes, use memory_supersede
- `EFFICIENCY_GUIDANCE` — Avoid redundant tool calls, use spawn_subagent
- `ANALYTICS_GUIDANCE` — Use analytics tools on collected data
- `ORCHESTRATION_GUIDANCE` — Airflow DAG patterns (conditional on airflow.available)
- `SA_GUIDANCE` — Source attribution, event extraction, feed_parse with source_id, 30 canonical relationship types, entity tagging categories
- `ENTITY_GUIDANCE` — Entity profile management, sections, completeness
- `GOAL_CONTEXT_TEMPLATE` — Format seed goal + active goals
- `MEMORY_CONTEXT_TEMPLATE` — Format episodes + facts
- `INBOX_TEMPLATE` — Format operator messages
- `PLAN_PROMPT` — Planning instructions: focus, tool selection, efficiency
- `CYCLE_REQUEST` — Reason phase task with plan + working memory
- `REPORTING_REMINDER` — Periodic status report prompt
- `REFLECT_PROMPT` — JSON extraction: cycle_summary, facts_learned, entities_discovered, relationships, goal_progress, self_assessment, next_cycle_suggestion, significance, memories_to_promote
- `LIVENESS_PROMPT` — Echo `nonce:cycle_number`
- `BUDGET_EXHAUSTED_PROMPT` — Force final response after max steps
- `MISSION_REVIEW_PROMPT` — Deep introspection task
- `NARRATE_PROMPT` — Journal entry generation (1-3 entries, anti-repetition)
- `JOURNAL_CONSOLIDATION_PROMPT` — Merge entries into narrative
- `ANALYSIS_REPORT_PROMPT` — Data-grounded "Current World Assessment" with anti-fabrication rules

#### `prompt/assembler.py` (649 lines)
**Purpose:** Builds [system, user] message lists for each cycle phase.

**Key class: `PromptAssembler`**
- `__init__(tool_data, tool_summary, bootstrap_threshold, max_context_tokens, report_interval, world_briefing, airflow_available)`

**Assembly methods:**
- `assemble_plan_prompt(...)` — System (identity + tool summary) + User (world briefing, goals, memories, graph, inbox, queue, journal, reflection_forward, plan request)
- `assemble_reason_prompt(...)` — Instructions-first pattern:
  - System = identity + rules + guidance + tool defs (filtered by planned_tools) + calling format
  - User = `--- CONTEXT DATA ---` / goals / memories / graph / inbox / queue / reflection / `--- END CONTEXT ---` / task
  - Budget enforcement: truncates memories and goals if total exceeds max_context_tokens
- `assemble_introspection_prompt(...)` — Like reason but with restricted tool set
- `assemble_reflect_prompt(...)` — Plan + working memory + results -> JSON extraction
- `assemble_narrate_prompt(...)` — Cycle summary + journal context -> journal entries
- `assemble_journal_consolidation_prompt(...)` — Entries + previous consolidation -> narrative
- `assemble_analysis_report_prompt(...)` — Graph, relationships, profiles, events -> assessment
- `assemble_liveness_prompt(...)` — Simple echo service
- `assemble_mission_review_prompt(...)` — Strategic review (legacy, replaced by introspection)

**Helper methods:**
- `_build_system_text(cycle_number, context_tokens, include_tools, planned_tools)` — Concatenates: SYSTEM_PROMPT + BOOTSTRAP + MEMORY_MANAGEMENT + EFFICIENCY + ANALYTICS + [ORCHESTRATION] + SA + ENTITY + [tool defs + calling instructions]
- `_format_goals(seed_goal, active_goals, tracker, cycle)` — Goals with per-goal work tracking and stall detection
- `_format_memories(context)` — Episodes + facts
- `_format_queue_summary(summary)` — NATS stream info
- `_format_inbox(messages)` — Priority-tagged operator messages

---

### 2.11 `src/legba/agent/selfmod/` — Self-Modification

#### `selfmod/engine.py`
**Purpose:** Propose, apply, and git-track modifications to agent code.

**Key class: `SelfModEngine`**
- `initialize()` — Set up git repo on `/agent` if not exists
- `propose_and_apply(file_path, new_content, rationale, expected_outcome, ...)` — Captures before-snapshot, writes file, captures after-snapshot, git commits
- `rollback_last()` — Restore most recent modification from before-snapshot
- `modifications_this_cycle` — List of ModificationRecord for this cycle

#### `selfmod/rollback.py`
**Purpose:** Restore files from stored before-snapshots.

**Key class: `RollbackManager`**
- `rollback(record)` — Single modification rollback
- `rollback_all(records)` — Cascade rollback in reverse order

---

### 2.12 `src/legba/agent/comms/` — External Communications

#### `comms/nats_client.py`
**Purpose:** NATS + JetStream client for event bus and human communication.

**Key class: `LegbaNatsClient`**
- **Human comms:** `publish_human_inbound/outbound`, `drain_human_inbound/outbound` — Replace file-based inbox/outbox
- **Data pub/sub:** `publish(subject, payload, headers)`, `subscribe_recent(subject, limit, stream)` — JetStream with core NATS fallback
- **Stream management:** `create_stream(name, subjects, max_msgs, max_bytes, max_age)`, `list_streams()`
- **ORIENT context:** `queue_summary()` — Returns QueueSummary with human_pending and data_streams

**Constants:** `HUMAN_STREAM = "LEGBA_HUMAN"`, `HUMAN_INBOUND = "legba.human.inbound"`, `HUMAN_OUTBOUND = "legba.human.outbound"`

**External deps:** `nats-py`

#### `comms/airflow_client.py`
**Purpose:** Async Airflow REST API client.

**Key class: `AirflowClient`**
- `connect()` — Verify health endpoint
- **DAG file deployment:** `deploy_dag(dag_id, dag_code)` — Write Python file to shared dags volume; `remove_dag_file(dag_id)`
- **DAG queries:** `list_dags(limit)`, `get_dag(dag_id)`
- **DAG control:** `trigger_dag(dag_id, conf, logical_date)`, `pause_dag(dag_id, paused)`
- **Run queries:** `list_dag_runs(dag_id, limit)`, `get_dag_run(dag_id, dag_run_id)`
- **Task queries:** `list_task_instances(dag_id, dag_run_id)`

**External deps:** `httpx`

---

### 2.13 `src/legba/supervisor/` — Supervisor

#### `supervisor/main.py` (490 lines)
**Purpose:** Main supervisor process. Orchestrates the agent lifecycle loop.

**Key class: `Supervisor`**
- Manages: `HeartbeatManager`, `CommsManager`, `LifecycleManager`, `AuditIndexer`, `LogDrain`, `LegbaNatsClient`
- Main loop: issue challenge -> launch agent container -> wait for completion -> validate heartbeat -> collect logs -> handle comms -> repeat
- Connects to Redis for cycle counting, NATS for messaging, audit OpenSearch for log indexing

#### `supervisor/lifecycle.py` (393 lines)
**Purpose:** Manages the agent Docker container.

**Key class: `LifecycleManager`**
- `launch_cycle(challenge, timeout)` — docker compose run with timeout
- **Graceful shutdown:** writes `stop_flag.json` to shared volume at soft timeout; agent pings back, gets extension (up to 2 extensions, EXTENSION_FACTOR 0.5)
- **Monitoring:** polls container status at POLL_INTERVAL (2s)

**Key type: `CycleResult`** — success, duration, exit_code, error

#### `supervisor/heartbeat.py`
**Purpose:** Challenge-response protocol for LLM liveness verification.

**Key class: `HeartbeatManager`**
- `issue_challenge(cycle_number, timeout)` — Generate 8-char hex nonce, write to `/shared/challenge.json`
- `validate_response()` — Read `/shared/response.json`, verify nonce matches
- Tracks consecutive failures

#### `supervisor/comms.py`
**Purpose:** Human-agent messaging.

**Key class: `CommsManager`**
- Primary transport: NATS JetStream (durable)
- Fallback: file-based inbox.json/outbox.json
- `send_message(content, priority, requires_response)` — Publish to inbound
- `read_responses()` — Drain outbound

#### `supervisor/drain.py`
**Purpose:** Collect and archive agent logs from the shared volume.

**Key class: `LogDrain`**
- `get_cycle_logs(cycle_number)` — Get JSONL files for a cycle
- `get_recent_logs(limit)` — Most recent log files
- `archive_cycle(cycle_number)` — Move to archive directory

#### `supervisor/audit.py` (242 lines)
**Purpose:** Index cycle logs into a dedicated (agent-inaccessible) OpenSearch instance.

**Key class: `AuditIndexer`**
- Uses httpx directly (not opensearch-py) for lightweight deps
- Monthly indices: `legba-audit-YYYY.MM`
- `index_cycle_logs(cycle_number, logs)` — Bulk index log entries
- Separate from agent's data OpenSearch (isolation by env var omission)

**External deps:** `httpx`

#### `supervisor/cli.py`
**Purpose:** Operator CLI for sending messages and reading responses.

**Commands:**
- `send "message"` — Send normal/urgent/directive message
- `read` — Read agent responses
- `status` — Show current cycle status

---

### 2.14 `src/legba/ui/` — Operator Console

#### `ui/app.py`
**Purpose:** FastAPI application with Jinja2 + htmx + Tailwind CSS.

- Lifespan: connects StoreHolder, MessageStore, UINatsClient
- Registers all route modules
- Template filters: markdown rendering, time formatting

#### `ui/messages.py`
**Purpose:** Message infrastructure.

**Key classes:**
- `MessageStore` — Redis sorted-set wrapper for conversation history (ZSET by timestamp)
- `UINatsClient` — Lightweight NATS wrapper for UI publish/pull with durable consumer

#### `ui/stores.py`
**Purpose:** Read-only store connections for the UI.

**Key class: `StoreHolder`**
- Owns: `StructuredStore`, `GraphStore`, `RegisterStore`, `OpenSearchStore`
- Count helpers: `count_entities()`, `count_events()`, `count_sources()`, `count_goals()`, `count_relationships()`

#### UI Routes:
| Route | Path | Purpose |
|---|---|---|
| `dashboard.py` | `GET /` | Stats dashboard (cycle, entities, events, sources, goals, relationships) |
| `messages.py` | `GET/POST /messages` | Bidirectional operator-agent messaging with NATS polling |
| `cycles.py` | `GET /cycles` | Cycle monitor with log viewer |
| `events.py` | `GET /events` | Event explorer with OpenSearch full-text search + category filter |
| `entities.py` | `GET /entities` | Entity browser with type filter |
| `sources.py` | `GET /sources` | Source registry browser with status/type filter |
| `goals.py` | `GET /goals` | Goal tree view |
| `graph.py` | `GET /graph` | Graph visualization using vis.js (nodes colored by type) |
| `journal.py` | `GET /journal` | Legba's consolidated journal narrative |
| `reports.py` | `GET /reports` | Analysis report history |

---

### 2.15 `src/legba/ingestion/` — Ingestion Service

Deterministic (no LLM) service that runs independently of the agent cycle. Fetches sources on schedule, normalizes content, deduplicates, stores signals, and clusters them into events.

#### `ingestion/service.py` (540 lines)
**Purpose:** Main ingestion tick loop. Runs every ~60s: fetch due sources, normalize, deduplicate, store signals, batch entity linking via spaCy NER, run clustering every 20 minutes.

#### `ingestion/dedup.py` (329 lines)
**Purpose:** 3-tier signal deduplication. GUID fast-path, source_url match, Jaccard title similarity with source suffix/prefix stripping.

**Key functions:**
- `check_duplicate(signal, pool)` — Returns True if duplicate
- `_title_words(title)` — Tokenize + lowercase + strip stop words
- `_jaccard(a, b)` — Jaccard similarity between two sets
- `_strip_source_suffixes(title)` — Remove " - Reuters", "BBC News: " etc. before comparison

#### `ingestion/cluster.py` (531 lines)
**Purpose:** Deterministic signal-to-event clustering engine. Groups related signals into derived events using entity overlap, title similarity, temporal proximity, and category matching.

**Key class: `SignalClusterer`**
- `__init__(pool)` — Takes asyncpg pool
- `cluster(window_hours=6, max_signals=500)` — One clustering pass: fetch unclustered signals, extract features, score pairwise similarity, single-linkage clustering (threshold 0.4), create/merge events
- `_fetch_unclustered(window_hours, limit)` — SQL: signals with no signal_event_links entry, excluding 'other' category
- `_handle_cluster(feats)` — Multi-signal cluster: find merge target or create new event
- `_find_merge_target(actors, locations, time_start, time_end, category)` — Entity overlap >= 0.3 against existing events
- `_reinforce_event(existing, feats, ...)` — Bump signal_count, extend time_end, increase confidence (cap 0.8)
- `_create_event_from_cluster(feats, ...)` — New event: title from highest-confidence signal, modal category, mean confidence capped at 0.6
- `_create_singleton_event(feat)` — 1:1 event for structured sources (NWS, USGS, GDACS, etc.)

**Key functions:**
- `_similarity(a_entities, b_entities, a_words, b_words, a_ts, b_ts, a_cat, b_cat)` — Composite: entity overlap 0.3 + title Jaccard 0.3 + temporal proximity 0.2 + category match 0.2
- `_single_linkage_cluster(n, sim_fn, threshold)` — Union-Find based single-linkage

**Constants:** `_CLUSTER_THRESHOLD = 0.4`, `_AUTO_CONFIDENCE_CAP = 0.6`, `_REINFORCED_CONFIDENCE_CAP = 0.8`, `_STRUCTURED_SOURCES` (NWS, USGS, GDACS, NASA EONET, EMSC, IFRC, ACLED)

#### `ingestion/storage.py` (498 lines)
**Purpose:** Signal storage to Postgres + OpenSearch. Handles geo-resolution, entity extraction.

#### `ingestion/fetcher.py` (588 lines)
**Purpose:** HTTP/RSS fetching with retry, browser UA fallback, timeout handling.

#### `ingestion/normalizer.py` (401 lines)
**Purpose:** Content normalization pipeline: HTML stripping, encoding fix, truncation.

#### `ingestion/source_normalizers.py` (922 lines)
**Purpose:** Per-source format normalizers for structured APIs (USGS, GDACS, NASA EONET, etc.).

---

### 2.16 `src/legba/agent/phases/curate.py` — CURATE Phase

**Purpose:** Intelligence curation phase that replaces ACQUIRE when the ingestion service is active. The agent reviews unclustered signals, refines auto-created events, and enriches entity profiles with editorial judgment.

**Key class: `CurateMixin`**
- `_curate()` — Main curate phase: build context (unclustered signals + low-confidence events + trending events + data overview), assemble prompt with CURATE_TOOLS, run tool loop with filtered executor
- `_build_curate_context()` — SQL queries for: unclustered signals (top 20 by confidence), auto-created events with signal_count <= 2 (top 15), trending events (signal_count > 2, top 5), total counts

**CURATE_TOOLS available:** `signal_query`, `signal_search`, `event_create`, `event_update`, `event_query`, `event_link_signal`, `entity_profile`, `entity_inspect`, `entity_resolve`, `graph_store`, `graph_query`, `memory_query`, `note_to_self`, `explain_tool`, `cycle_complete`

---

### 2.17 `scripts/migrate_signals_events.sql` — Database Migration

**Purpose:** DDL migration for the signals/events refactor. Renames `events` -> `signals`, `events_derived` -> `events`, renames junction tables and foreign keys accordingly.

**Key operations:**
1. `events` table renamed to `signals` (raw ingested material)
2. `events_derived` table renamed to `events` (derived real-world occurrences)
3. `event_entity_links` renamed to `signal_entity_links` (column `event_id` -> `signal_id`)
4. New `event_entity_links` table created for derived events
5. `situation_events` renamed to `situation_signals` (column `event_id` -> `signal_id`)
6. New `situation_events` table created for derived events
7. `watch_triggers.event_id` renamed to `signal_id`, new `event_id` column added
8. All indexes renamed for clarity

---

## 3. Normal Cycle Function Call Flow

```
main.py:main()
  asyncio.run(run_cycle())
    config = LegbaConfig.from_env()
    cycle = AgentCycle(config)
    cycle.run()

AgentCycle.run():
  _wake()
    Read /shared/challenge.json -> cycle_number, nonce
    Load seed_goal from /seed_goal/goal.txt
    Load world_briefing.txt (bootstrap only)
    MemoryManager.connect() -> Redis, Qdrant, Postgres, AGE
    LLMClient(config.llm, logger)
    GoalManager(structured, logger)
    SelfModEngine(agent_code_path, logger).initialize()
    LegbaNatsClient.connect()
    OpenSearchStore.connect()
    AirflowClient(config)
    ToolRegistry + _register_builtin_tools() + load_dynamic_tools()
    ToolExecutor(registry, logger)
    PromptAssembler(tool_data, ...)
    AirflowClient.connect()
    Drain inbox (NATS first, file fallback)

  _orient()
    Load active goals from GoalManager
    Generate query embedding from seed_goal + top goal
    MemoryManager.retrieve_context() -> episodes, facts, goals, registers
    LegbaNatsClient.queue_summary()
    Build graph inventory (entity type counts, relationship counts, unknowns warning)
    Source health stats (utilization %)
    Load reflection_forward from previous cycle
    Load goal_work_tracker
    Load journal context (consolidation + recent entries)

  _plan()
    PromptAssembler.assemble_plan_prompt(all context)
    LLMClient.complete(plan_messages, purpose="plan")
    Parse "Tools: a, b, c" line -> _planned_tools

  _reason_and_act()
    PromptAssembler.assemble_reason_prompt(context + plan + planned_tools)
    LLMClient.reason_with_tools(messages, executor.execute, max_steps=20)
      Loop up to 20 steps:
        LLMClient.complete([system, user]) -> raw response
        tool_parser.parse_tool_calls(raw) -> list[ToolCall]
        If no tool calls -> final response, break
        If cycle_complete -> break
        Execute tools concurrently (max 4)
        Add to tool_history + working_memory
        Build next step's user message (sliding window)
      If budget exhausted -> force final response
    Count actions_taken from tool result messages

  _reflect()
    PromptAssembler.assemble_reflect_prompt(plan, working_memory, results)
    LLMClient.complete(reflect_messages, purpose="reflect")
    _parse_reflection() -> extract JSON with cycle_summary
    _store_reflection_facts() -> Fact objects to Postgres + Qdrant
    _store_reflection_graph() -> entities/relationships to AGE with fuzzy dedup

  _narrate()
    PromptAssembler.assemble_narrate_prompt(cycle_summary, journal_context)
    LLMClient.complete(narrate_messages, purpose="narrate")
    Parse JSON array of strings -> 1-3 journal entries
    _store_journal_entries() -> append to Redis journal data

  _persist()
    Update goal progress from reflection_data
    Auto-complete goals at 100% progress_pct
    Update goal_work_tracker
    Compute work pattern (collecting/deepening/analyzing/mixed)
    Count stale goals (no progress >1hr)
    Store reflection_forward for next cycle
    Auto-promote reflection-flagged memories
    Auto-promote high-significance (>=0.6) short-term memories
    Store cycle episode to Qdrant
    Generate outbox responses for inbox messages
    Add status report on reporting cycles (every 5)
    Write outbox (NATS first, file fallback)
    _validate_liveness() -> LLM echoes nonce:cycle_number
    Build CycleResponse
    Write /shared/response.json

  _cleanup()
    Close: Airflow, OpenSearch, NATS, LLM, Memory
```

---

## 4. Introspection Cycle Function Call Flow (Every 15 Cycles)

```
AgentCycle.run():
  _wake()          (same as normal)
  _orient()        (same as normal)

  --- Introspection branch (_is_introspection_cycle() == True) ---

  _mission_review()
    Load reflection_forward + deferred_goals
    PromptAssembler.assemble_introspection_prompt(
      allowed_tools=INTROSPECTION_TOOLS  # graph_query, memory_query, entity_inspect, etc.
    )
    Create filtered executor (blocks non-introspection tools)
    LLMClient.reason_with_tools(review_messages, introspection_executor)
    Extract findings from working memory
    Prepend to reflection_forward

  _reflect()       (same as normal)

  _narrate()       (same as normal)

  _journal_consolidation()
    Load all raw journal entries from Redis
    PromptAssembler.assemble_journal_consolidation_prompt(entries, previous_consolidation)
    LLMClient.complete() -> new consolidated narrative
    Store consolidation, clear raw entries

  _generate_analysis_report()
    Query actual data from all stores:
      - Key relationships from AGE (LeaderOf, HostileTo, AlliedWith, etc.)
      - Entity profiles with summaries from Postgres
      - Recent events from OpenSearch (fallback: Postgres)
      - Coverage regions from graph
      - Current journal narrative
    PromptAssembler.assemble_analysis_report_prompt(all data)
    LLMClient.complete() -> report content
    Store to Redis (latest_report + report_history)
    Add to outbox as ANALYSIS REPORT message

  _persist()       (same as normal)
```

**INTROSPECTION_TOOLS allowed set:**
`graph_query`, `graph_store`, `graph_analyze`, `memory_query`, `memory_store`, `memory_promote`, `memory_supersede`, `entity_inspect`, `entity_profile`, `os_search`, `note_to_self`, `explain_tool`, `goal_update`, `goal_create`, `cycle_complete`

---

## 4b. CURATE Cycle Function Call Flow (Every 3 Cycles, When Ingestion Active)

Replaces ACQUIRE when the ingestion service is running. The agent applies editorial judgment to raw signals and auto-created events rather than doing its own source fetching.

```
AgentCycle.run():
  _wake()          (same as normal)
  _orient()        (same as normal)

  --- Curate branch (ingestion active, cycle_number % 3 == 0) ---

  _curate()
    _build_curate_context()
      ├── SQL: Unclustered signals (no event link, not junk, top 20 by confidence)
      ├── SQL: Auto-created events with signal_count <= 2 (top 15)
      ├── SQL: Trending events with signal_count > 2 (top 5)
      └── SQL: Total signals, events, unlinked count

    PromptAssembler.assemble_curate_prompt(
      curate_context=...,
      allowed_tools=CURATE_TOOLS
    )
    Create filtered executor (blocks non-curate tools)
    LLMClient.reason_with_tools(curate_messages, curate_executor)

  _reflect()       (same as normal)
  _narrate()       (same as normal)
  _persist()       (same as normal)
```

**CURATE_TOOLS allowed set:**
`signal_query`, `signal_search`, `event_create`, `event_update`, `event_query`, `event_link_signal`, `entity_profile`, `entity_inspect`, `entity_resolve`, `graph_store`, `graph_query`, `memory_query`, `note_to_self`, `explain_tool`, `cycle_complete`

---

## 5. LLM Call Flow

```
cycle.py (any phase)
  |
  v
assembler.py:assemble_*_prompt(...)
  Build [Message(role="system", ...), Message(role="user", ...)]
  System = identity + rules + guidance + tool defs + calling format
  User = context data bracketed by separators + task request
  |
  v
client.py:LLMClient.complete(messages, purpose)
  |
  v
format.py:to_chat_messages(messages)
  Combine all messages into single {"role": "user", "content": combined}
  (GPT-OSS doesn't handle system role reliably)
  |
  v
provider.py:VLLMProvider.chat_complete(chat_msgs)
  POST /v1/chat/completions
  {
    "model": "InnoGPT-1",
    "messages": [{"role": "user", "content": combined}],
    "temperature": 1.0,
    "stream": false
  }
  Retry on 429/500/502/503 with exponential backoff (max 3)
  |
  v
vLLM server (GPT-OSS 120B MoE, 128k context)
  |
  v
provider.py: parse response
  format.py:strip_harmony_response(raw_content) -> clean content
  Return LLMResponse(content, finish_reason, usage)
  |
  v
client.py: log full call via CycleLogger
  Return LLMResponse to caller
```

**For REASON+ACT loops (reason_with_tools):**
```
client.py:reason_with_tools(messages, tool_executor, max_steps=20)
  Extract system_msg (constant across steps)
  Split user content at "--- END CONTEXT ---"
  |
  v
  Loop (step 1..20):
    Build [system_msg, user_msg] for this step
      Step 1: original messages as-is
      Step 2+: base_context + tool_history (sliding window) + working_memory + act instruction
    |
    LLMClient.complete([system, user]) -> response
    |
    tool_parser.py:parse_tool_calls(response.content) -> list[ToolCall]
      Strategy 1: {"actions": [...]} wrapper
      Strategy 2: bare {"tool": ...} objects
      Strategy 3: legacy to=functions.NAME format
    |
    If no tool calls -> return (response, history)
    If cycle_complete -> break
    |
    Execute tools concurrently (max 4):
      asyncio.gather(*[tool_executor(tc.tool_name, tc.arguments) for tc in tool_calls])
    |
    Add to tool_history, working_memory, history_messages
    |
    Continue loop
  |
  If budget exhausted:
    Force final response with BUDGET_EXHAUSTED_PROMPT
    Return (forced_response, history)
```

---

## 6. External Dependencies

| Category | Package | Used By |
|---|---|---|
| HTTP | `httpx` | provider.py, http.py, airflow_client.py, audit.py |
| Async Redis | `redis[asyncio]` | registers.py, messages.py |
| Postgres | `asyncpg` | structured.py, graph.py |
| Vector DB | `qdrant-client` | episodic.py |
| NATS | `nats-py` | nats_client.py |
| OpenSearch | `opensearch-py` | opensearch.py |
| RSS | `feedparser` | feed_tools.py |
| HTML extract | `trafilatura` | http.py, feed_tools.py |
| Crypto | `PyNaCl` | crypto.py |
| Data | `pydantic` | all schemas |
| Country lookup | `pycountry` | geo.py |
| NLP | `yake` | analytics_tools.py |
| NLP | `spacy` | ingestion/service.py (NER for entity extraction) |
| ML | `scikit-learn`, `numpy` | analytics_tools.py |
| Config | `python-dotenv` | config.py |
| Web framework | `FastAPI`, `uvicorn`, `jinja2` | ui/ |

---

## 7. Data Flow Summary

```
                    +---------+
                    |  vLLM   |
                    |GPT-OSS  |
                    +----+----+
                         ^
                         | HTTP /v1/chat/completions
                    +----+----+
        +---------->| Agent   |<----------+
        |           | Cycle   |           |
        |           +----+----+           |
        |                |                |
   +----+----+    +------+------+   +-----+-----+
   | Qdrant  |    |  Postgres   |   | OpenSearch |
   | (vector)|    | (structured)|   | (fulltext) |
   |         |    |   + AGE     |   |            |
   +---------+    | (graph ext) |   +-----------+
                  +------+------+
                         |
               +---------+---------+
               |  Signal/Event     |
               |  Two-Tier Model   |
               |                   |
               | signals (raw)     |
               |   ↓ clustering    |
               | events (derived)  |
               |   ↓ signal_event  |
               |     _links (M:N)  |
               +-------------------+

        |                |                |
   +----+----+    +------+------+   +-----+-----+
   |  Redis  |    |    NATS     |   |  Airflow  |
   |(registers|   | (event bus) |   |(workflows)|
   | +journal)|   +-------------+   +-----------+
   +---------+

   +-----------+        +-----------+
   |Supervisor |------->|   Agent   |
   |           |<-------| Container |
   +-----------+        +-----------+
   (challenge/          (response.json
    response)            logs, outbox)

   +-----------+        +-----------+
   |    UI     |        | Ingestion |
   | (read-only|        | Service   |
   |  console) |        | (no LLM)  |
   +-----------+        +-----------+
   FastAPI + htmx       Fetch → Dedup → Signal
   Reads all stores     Cluster → Event (every 20m)
```

### Database Tables (Post-Migration)

| Table | Purpose |
|-------|---------|
| `signals` | Raw ingested material (was `events`) |
| `events` | Derived real-world occurrences (was `events_derived`) |
| `signal_event_links` | Many-to-many: signals evidencing events |
| `signal_entity_links` | Signal-entity junction (was `event_entity_links`) |
| `event_entity_links` | Event-entity junction (new, for derived events) |
| `entity_profiles` | Versioned entity profiles |
| `entity_profile_versions` | Profile version history |
| `situations` | Tracked situations |
| `situation_signals` | Situation-signal junction (was `situation_events`) |
| `situation_events` | Situation-event junction (new, for derived events) |
| `watch_triggers` | Alert triggers: `signal_id` + `event_id` columns |
| `goals` | Goal hierarchy |
| `facts` | Structured facts |
| `modifications` | Self-modification audit trail |
| `sources` | Source registry with trust metadata |
