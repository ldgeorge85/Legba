# Legba — Architecture Reference

## 1. Overview

Legba is a continuously operating, goal-oriented, self-modifying autonomous agent. It runs in a containerized environment with a dedicated LLM (InnoGPT-1, Harmony prompt format, 128k context) available for constant inference. It receives a broad, open-ended seed goal from a human operator and pursues it indefinitely through observation, reasoning, action, and self-improvement.

This is not a one-shot task agent. It is a persistent, ambient system that maintains state across cycles, accumulates knowledge, adapts its own behavior, and operates with minimal human intervention.

The platform is domain-agnostic — the seed goal defines the domain, the platform provides capabilities. All enhancements (messaging, search, analytics, orchestration) are general-purpose infrastructure that any seed goal benefits from.

**Key numbers:** 61+ Python source files, 240 tests (188 unit + 32 integration + 20 graph), 43 built-in tools across 14 builtin modules, 7 platform services.

---

## 2. LLM: Harmony Prompt Format

The model (InnoGPT-1 / gpt-oss based) uses Harmony prompt formatting via an OpenAI-compatible API served by vLLM.

### Connection Info
- API: OpenAI-compatible at configured base URL
- Model: InnoGPT-1
- Context window: 128k tokens
- Embedding model: embedding-inno1, 1024 dimensions
- Temperature: 1.0, Top-P: 1.0, Max tokens: 16384 (gpt-oss recommended defaults)

### Reasoning Level

The system prompt includes `Reasoning: high` as the first line — a gpt-oss Harmony protocol setting that controls reasoning effort. This is a model-level directive (not application logic) that tells the model to use its full chain-of-thought capability. The liveness check prompt uses `Reasoning: low` (simple concatenation) and the reflect prompt uses `Reasoning: high` (evaluation). See `prompt/templates.py` and `prompt/assembler.py`.

### Harmony Format Structure

Harmony uses special tokens and a channel system:

**Special tokens:**
| Token | Purpose |
|-------|---------|
| `<\|start\|>` | Begins a message, followed by header (role, channel, routing) |
| `<\|end\|>` | Closes a completed message |
| `<\|message\|>` | Transitions from header to content body |
| `<\|channel\|>` | Marks channel information in header |
| `<\|constrain\|>` | Specifies data type constraint (e.g., json) for tool call arguments |
| `<\|return\|>` | Final completion stop token (decode-time only) |
| `<\|call\|>` | Tool invocation stop token |

**Stop tokens:** `<|end|>` (end of message, may continue), `<|call|>` (tool invocation, must execute and resume), `<|return|>` (fully done).

**Roles (hierarchy: system > developer > user > assistant > tool):**
- `system` — reasoning effort, knowledge cutoff, built-in config
- `developer` — instructions, tool definitions (NOT system — key difference from ChatML)
- `user` — user input
- `assistant` — model output (spans multiple channels)
- `tool` — tool execution results (e.g., `functions.get_weather`)

**Channels (assistant messages only):**
| Channel | Purpose | Safety filtered? |
|---------|---------|-----------------|
| `analysis` | Internal chain-of-thought reasoning | No |
| `commentary` | Tool call preambles, explanations | Yes |
| `final` | User-facing response | Yes |

The `analysis` channel gives free logging of the agent's reasoning process.

### Tool Definitions in Harmony

Tools use TypeScript-style syntax in `developer` messages (not JSON schema):

```
<|start|>developer<|message|># Tools
## functions
namespace functions {
// Read a file from the filesystem
type fs_read = (_: {
  path: string,
  offset?: number,
  limit?: number,
}) => any;
} // namespace functions<|end|>
```

Tool invocation by model:
```
<|start|>assistant<|channel|>commentary to=functions.fs_read<|constrain|>json<|message|>{"path": "/workspace/config.yml"}<|call|>
```

Tool result fed back:
```
<|start|>functions.fs_read to=assistant<|channel|>commentary<|message|>{"content": "..."}<|end|>
```

### Harmony Quirks and Handling Rules

1. **CoT must be preserved during multi-step tool chains.** If the model is mid-reasoning and calls a tool, all `analysis` channel messages must be fed back until a `final` message is produced.
2. **CoT should be pruned between turns.** After a `final` message, drop previous `analysis` messages to save tokens on the next cycle.
3. **Replace `<|return|>` with `<|end|>` when storing in history.** `<|return|>` is decode-time only.
4. **Only `tool_choice="auto"` is supported.** Cannot force specific tool calls.
5. **vLLM tool calling is fragile.** Function calls often ignored or cut short, multi-step tool use unreliable, structured output enforcement patchy.
6. **InnoGPT-1 writes literal text instead of special tokens for tool calls.** Observed in production: model produces `assistantcommentary to=functions.fs_readjson{"path": "..."}` (literal text) instead of proper Harmony tokens. The tool parser handles both formats. Tool name cleaning strips trailing "json" that gets merged when there's no space separator.

### Custom Tool Call Parsing

**Do not rely on vLLM's native tool_choice mechanism.** Implementation:
- Tool definitions sent in the prompt via `developer` messages (TypeScript syntax)
- `tool_parser.py` parses tool invocations from raw completions — handles both Harmony special token format and literal text format
- Tools executed and results injected back into the conversation as `functions.{tool_name}` messages
- Full control over retry logic, validation, and tool name cleaning

The LLM interface is modular — abstracted behind a provider interface so models can be swapped. The Harmony-specific handling is a concrete implementation behind that interface.

---

## 3. Execution Environment

### Container Topology

```
+------------------------------------------------------------------------------+
|  Host VM                                                                      |
|                                                                               |
|  +------------------------------------------------------------------------+   |
|  | Docker Compose (project: legba)                                       |   |
|  |                                                                        |   |
|  |  +-------------------+          +--------------------------------+    |   |
|  |  | Supervisor         |          | Agent Container                |    |   |
|  |  | (container w/      |  docker  | (one per cycle)                |    |   |
|  |  |  Docker socket)    |--run---->|  PYTHONPATH=/agent/src         |    |   |
|  |  |  * auto-rollback   |          |  legba.agent.main             |    |   |
|  |  |  * cycle loop      |          |  +-- cycle.py (6 phases)       |    |   |
|  |  |  * heartbeat mgr   |  <-file--|  +-- llm/ (Harmony+vLLM)      |    |   |
|  |  |  * NATS sub/pub    |  --file->|  +-- memory/ (5 stores)       |    |   |
|  |  |  * log drain       |          |  +-- tools/ (43 built-in)     |    |   |
|  |  |  * lifecycle mgr   |          |  +-- goals/ (CRUD)            |    |   |
|  |  |                    |          |  +-- selfmod/ (engine+rb)     |    |   |
|  |  +------+-------------+          |  +-- prompt/ (assembler+ctx)  |    |   |
|  |         |                        |  +-- analytics/ (toolkit)     |    |   |
|  |         | CLI: legba-cli        +----------+---------------------+    |   |
|  |         | (NATS pub)                        |                          |   |
|  |         |                                   | async clients            |   |
|  |  +------+----------------------------------++-----------------------+   |   |
|  |  | Shared Volumes                                                  |   |   |
|  |  |  /shared -- challenge.json, response.json (heartbeat only)      |   |   |
|  |  |  /logs ---- cycle logs (agent writes, supervisor drains to OS)  |   |   |
|  |  |  /seed_goal -- goal.txt (read-only to agent)                    |   |   |
|  |  |  /workspace -- agent work product (persistent)                  |   |   |
|  |  |  /agent -- agent source code + pipelines (self-mod, git)        |   |   |
|  |  +-----------------------------------------------------------------+   |   |
|  |                                                                        |   |
|  |  +----------------------------------------------------------------+  |   |
|  |  | Platform Services (long-lived, compose-managed)                |  |   |
|  |  |                                                                |  |   |
|  |  |  +--------+ +------------+ +--------+ +-------------+         |  |   |
|  |  |  | Redis  | | Postgres   | | Qdrant | | NATS        |         |  |   |
|  |  |  | :6379  | | +AGE :5432 | | :6333  | | :4222       |         |  |   |
|  |  |  |        | |            | |        | |             |         |  |   |
|  |  |  |transient| | structured | |semantic| | messaging   |         |  |   |
|  |  |  | state  | | ACID data  | | search | | event bus   |         |  |   |
|  |  |  |        | | entity     | |episodes| | data ingest |         |  |   |
|  |  |  |        | | graph      | | facts  | | human comms |         |  |   |
|  |  |  +--------+ +------------+ +--------+ +-------------+         |  |   |
|  |  |                                                                |  |   |
|  |  |  +-------------+ +----------------------------------------+   |  |   |
|  |  |  | OpenSearch  | | Airflow (standalone)                    |   |  |   |
|  |  |  | :9200       | | :8080                                  |   |  |   |
|  |  |  |             | |                                        |   |  |   |
|  |  |  | bulk data   | | persistent DAG pipelines               |   |  |   |
|  |  |  | full-text   | | scheduled jobs                         |   |  |   |
|  |  |  | search      | | agent-defined workflows                |   |  |   |
|  |  |  | aggregations| | results -> NATS                        |   |  |   |
|  |  |  | log indexing| | reads -> OpenSearch                    |   |  |   |
|  |  |  | ISM         | | writes -> OpenSearch, NATS             |   |  |   |
|  |  |  +-------------+ +----------------------------------------+   |  |   |
|  |  |                                                                |  |   |
|  |  |  Optional (--profile dashboards):                              |  |   |
|  |  |  +---------------------------+                                 |  |   |
|  |  |  | OpenSearch Dashboards     |                                 |  |   |
|  |  |  | :5601                     |                                 |  |   |
|  |  |  +---------------------------+                                 |  |   |
|  |  +----------------------------------------------------------------+  |   |
|  +------------------------------------------------------------------------+   |
|                                                                               |
|  External: LLM Inference (<your-llm-endpoint>/v1)                            |
|            /completions (Harmony pre-formatted prompts)                       |
|            /embeddings  (embedding-inno1, 1024d)                              |
|                                                                               |
|  External: Data sources -> NATS (direct clients or HTTP ingest sidecar)      |
+------------------------------------------------------------------------------+
```

### Data Flow Per Cycle

```
Supervisor                    Agent                     Services             LLM API
    |                           |                           |                  |
    |  1. write challenge.json  |                           |                  |
    +-------------------------->|                           |                  |
    |  2. docker run            |                           |                  |
    +-------------------------->|                           |                  |
    |                           |                           |                  |
    |                       WAKE|                           |                  |
    |                           |  read challenge.json      |                  |
    |                           |  read seed_goal/goal.txt  |                  |
    |                           +--drain(legba.human.*)----> (NATS)           |
    |                           +--connect------------------> (Redis,PG,Qdrant)|
    |                           |  incr cycle_number        |                  |
    |                           |                           |                  |
    |                      ORIENT                           |                  |
    |                           +--generate_embedding()-----+----------------->|
    |                           |<-------------[1024d vec]--+------------------|
    |                           +--search_both(vec)---------> (Qdrant)         |
    |                           +--get_active_goals()-------> (Postgres)       |
    |                           +--search_facts(vec)--------> (Qdrant)         |
    |                           +--query_facts()------------> (Postgres)       |
    |                           +--queue_summary()----------> (NATS)           |
    |                           |                           |                  |
    |               MISSION_REVIEW (every N cycles, conditional)               |
    |                           +--complete(review)----------+----------------->|
    |                           |<-------[review]-----------+------------------|
    |                           |  evaluate goal health, defer/abandon stuck   |
    |                           |  (skipped if cycle % mission_review_interval)|
    |                           |                           |                  |
    |                       PLAN                            |                  |
    |                           +--complete(plan)------------+----------------->|
    |                           |<-------[plan]-------------+------------------|
    |                           |  select goal focus, decide approach          |
    |                           |                           |                  |
    |                   REASON+ACT (loop, up to N steps)    |                  |
    |                           +--complete(prompt)----------+----------------->|
    |                           |<-------[response]---------+------------------|
    |                           |  parse tool call?         |                  |
    |                           |  yes: execute tool        |                  |
    |                           |    +-- fs_read/write       |                 |
    |                           |    +-- exec (shell)        |                 |
    |                           |    +-- http_request -------+---> Internet    |
    |                           |    +-- memory_*    -------> (Qdrant/PG)      |
    |                           |    +-- graph_*     -------> (AGE/Postgres)   |
    |                           |    +-- nats_*      -------> (NATS)           |
    |                           |    +-- os_*        -------> (OpenSearch)     |
    |                           |    +-- anomaly_detect ----> (OpenSearch>PyOD)|
    |                           |    +-- forecast    -------> (OS>statsforecast)
    |                           |    +-- nlp_extract -------> (OS/NATS>spaCy)  |
    |                           |    +-- graph_analyze -----> (AGE>NetworkX)   |
    |                           |    +-- correlate   -------> (OS>scikit-learn)|
    |                           |    +-- workflow_*  -------> (Airflow API)    |
    |                           |    +-- spawn_subagent      |                 |
    |                           |         +-- (own LLM loop)-+----------------->
    |                           |  feed result back, repeat  |                 |
    |                           |  no: final response        |                 |
    |                           |                           |                  |
    |                    REFLECT |                           |                  |
    |                           +--complete(reflect)---------+----------------->|
    |                           |<-------[reflection]--------+-----------------|
    |                           |                           |                  |
    |                    PERSIST |                           |                  |
    |                           +--store_episode()----------> (Qdrant)         |
    |                           +--publish(legba.human.out)> (NATS)           |
    |                           +--complete(liveness)--------+----------------->|
    |                           |<-[transformed nonce]------+------------------|
    |                           +--write response.json      |                  |
    |                           |  (heartbeat, file-based)  |                  |
    |                           +--flush logs ---------------> /logs volume    |
    |                           |  exit(0)                  |                  |
    |                           |                           |                  |
    |  3. validate response     |                           |                  |
    |  4. index logs to OS      |                           | (OpenSearch)     |
    |  5. subscribe outbound    |                           | (NATS)           |
    |  6. sleep, next cycle     |                           |                  |
```

### Volume Mounts

| Volume | Container Path | Access | Purpose |
|--------|---------------|--------|---------|
| `shared_data` | `/shared` | RW (both) | challenge/response (heartbeat only) |
| `log_data` | `/logs` | RW (agent writes, supervisor reads) | Structured JSON cycle logs |
| `seed_goal` | `/seed_goal` | RO (agent) | Immutable seed goal text |
| `workspace_data` | `/workspace` | RW (agent) | Agent work product |
| `agent_code` | `/agent` | RW (agent) | Self-modifiable source code (git-tracked) |

**Warning: `agent_code` volume persistence.** The `agent_code` volume persists self-modified code across container rebuilds. After rebuilding Docker images (e.g., updating dependencies, fixing agent code), the `agent_code` volume still contains the old `/agent/src` tree — the entrypoint script only seeds it on first boot (empty volume). You must manually remove or update the volume (`docker volume rm legba_agent_code`) after image rebuilds to pick up the new code. Otherwise the agent continues running stale self-modified code from the previous image.

### Network
- All containers on `legba_default` Docker network
- Agent container: full internet access (for tools), internal access to memory services
- Supervisor container: Docker socket access (to launch/kill agent containers)
- Memory services: internal only (no host port exposure needed in production)

---

## 4. Supervisor

The supervisor runs in its own container, outside the agent container. The agent cannot reach, modify, or disable it.

### Responsibilities
- Holds and mounts the seed goal as read-only into the agent container
- Expects a heartbeat from the agent on a defined interval
- Kills the agent container if heartbeat stops (preserves volumes for forensics)
- Drains all prompt I/O logs to host-side storage (outside agent's reach)
- Indexes logs into a dedicated audit OpenSearch instance (agent cannot access)
- Provides a kill switch for the human operator

### Heartbeat Design
The heartbeat is not just a ping. It uses a dedicated liveness check in the PERSIST phase:
1. Supervisor generates a short 8-character hex nonce (`uuid4().hex[:8]`) and writes it to `challenge.json` at cycle start. The `Challenge` schema has no `insert_position` field — it contains only `cycle_number`, `nonce`, `timeout_seconds`, and `issued_at`.
2. In PERSIST, a lightweight LLM call concatenates the nonce with the cycle number as `nonce:cycle_number` (simple string concatenation, e.g., `a3f1b2c4:42`) — proving the LLM is live.
3. Supervisor validates the transformed nonce by computing the same `f"{nonce}:{cycle_number}"` and comparing (`HeartbeatManager.compute_expected_nonce()`).
4. The nonce is **not** included in every prompt — it is only used during the dedicated liveness check, keeping reasoning prompts clean.

**Limitations:** If the agent modifies its loop code to pass through the challenge without running the full cycle, this could be gamed. The real protection is structural: the supervisor is unreachable, the seed goal is read-only, and logs are drained externally. The challenge-response is an additional signal, not the sole protection.

### Communication Protocol

**Primary:** NATS + JetStream. CLI publishes to `legba.human.inbound`, supervisor subscribes to `legba.human.outbound`. File-based fallback (`/shared/inbox.json`, `/shared/outbox.json`) when NATS is unavailable. Heartbeat stays file-based (intentional safety isolation — heartbeat must not share the agent's messaging layer).

**Primary (NATS):**
```
Operator --> legba-cli send "message" --> NATS legba.human.inbound  --> Agent (WAKE phase drains)
Operator <-- supervisor subscribe       <-- NATS legba.human.outbound <-- Agent (PERSIST phase publishes)
```

**Fallback (file-based, when NATS unavailable):**
```
Operator --> legba-cli send "message" --> /shared/inbox.json --> Agent (WAKE phase reads)
Operator <-- legba-cli read            <-- /shared/outbox.json <-- Agent (PERSIST phase writes)
```

**Inbox message priorities:**
- `normal` — Agent sees it, incorporates into reasoning
- `urgent` — Agent prioritizes over current goal focus, should respond
- `directive` — Overrides current cycle. Agent must address before anything else.

Messages marked `requires_response: true` force the agent to include a reply in its outbox output.

### Lifecycle Model: Supervisor-Managed Restart
1. Supervisor writes challenge file
2. Supervisor launches agent process (one cycle)
3. Agent runs full cycle (WAKE > ORIENT > [MISSION_REVIEW] > PLAN > REASON+ACT > REFLECT > PERSIST)
4. Agent writes response file with heartbeat
5. Agent exits
6. Supervisor validates heartbeat, checks response
7. Supervisor launches agent again for next cycle

This means:
- **Self-modifications take effect naturally** — next process launch loads new code from `/agent/src` via `PYTHONPATH` (set by `agent-entrypoint.sh`)
- **Agent cannot persist a rogue process** — it doesn't own its own lifecycle
- **Clean state per cycle** — no leaked state between cycles (memory services provide persistence)
- **Supervisor has total control** — can delay, skip, or abort cycles
- **Auto-rollback on bad self-mods** — supervisor detects when agent code changes break boot, reverts via `git reset --hard`

### Persistent Cycle Count
The supervisor reads the cycle number from Redis (`legba:cycle_number`) on startup and resumes from there. The agent increments this counter each cycle. The cycle number persists across supervisor restarts and reflects the agent's total lifetime — not just the current supervisor session.

### Stale Goal Flagging (L.6)

After each successful heartbeat validation, the supervisor reads `legba:reflection_forward` from Redis (written by the agent's REFLECT phase). If `stale_goal_count > 0` and the count has changed since the last alert, the supervisor sends a normal-priority inbox message advising the agent to complete, abandon, or reprioritize stuck goals. Tracks last-sent count to avoid spamming the same alert.

### Graceful Shutdown

When the supervisor's soft timeout fires (default `SUPERVISOR_HEARTBEAT_TIMEOUT`), it does not immediately kill the agent. Instead it follows a negotiation sequence: soft timeout reached -> supervisor writes `/shared/stop_flag.json` -> agent detects the flag between REASON+ACT steps and writes `/shared/stop_ping.json` -> supervisor sees the ping and extends the timeout by 50% -> agent breaks out of the tool loop and proceeds through REFLECT and PERSIST (including the heartbeat liveness check) -> clean exit. If no ping arrives within 150 seconds (`PING_WAIT_SECONDS`), the supervisor hard-kills the container. Maximum 2 extensions. The `stop_check` callback is already wired into `reason_with_tools()` in `client.py`. See `docs/GRACEFUL_SHUTDOWN.md` for the full design.

### Stop Conditions
- Goal completed (agent self-reports, supervisor confirms)
- Goal determined impossible (agent self-reports with rationale)
- Human operator issues kill command
- Heartbeat timeout exceeded (after graceful shutdown attempt)

---

## 5. Agent Core Loop

```
+------------------------------------------------------------------+
|                         AGENT CYCLE                                |
|                                                                    |
|  1. WAKE                                                          |
|     - Read supervisor challenge (nonce + cycle metadata)          |
|     - Load seed goal (read-only mount)                            |
|     - Connect to memory services (Redis, Postgres, Qdrant)       |
|     - Connect to platform services (NATS, OpenSearch, Airflow)   |
|     - Register tools (after service connections are live)         |
|     - Drain NATS human inbound (fallback: read inbox.json)       |
|     - Get/increment cycle counter from registers                  |
|     - Initialize LLM client, self-mod engine                     |
|                                                                    |
|  2. ORIENT                                                        |
|     - Generate embedding of seed goal for similarity search       |
|     - Retrieve episodic memories (Qdrant: short + long term)     |
|     - Load active goals (Postgres)                                |
|     - Query facts (Qdrant semantic + Postgres structured, merged) |
|     - Build graduated graph inventory (L.5 completeness table)   |
|     - Get NATS queue summary (pending messages across subjects)   |
|     - Build working context for this cycle                        |
|                                                                    |
|  2b. MISSION_REVIEW (conditional, every mission_review_interval    |
|       cycles, default 15 — skipped otherwise)                      |
|     - Strategic review of goal tree alignment with seed goal       |
|     - Evaluate stuck/low-value goals for deferral or abandonment   |
|     - Identify neglected areas of the primary mission              |
|     - Re-evaluate deferred goals past their revisit cycle          |
|     - Review output prepended to reflection_forward for PLAN       |
|                                                                    |
|  3. PLAN                                                          |
|     - LLM selects goal focus and decides approach for this cycle  |
|     - Considers oriented context, inbox messages, goal state      |
|     - Produces a lightweight action plan before execution          |
|                                                                    |
|  4. REASON + ACT (interleaved loop, up to N tool calls)           |
|     - Assemble prompt: system + tools + goals + memories +        |
|       inbox messages + action request                             |
|     - LLM reasons (Harmony analysis channel = logged CoT)         |
|     - If LLM calls a tool:                                        |
|         execute -> feed result back -> reason again               |
|     - If LLM calls cycle_complete:                                |
|         early exit (intercepted in client.py, never executed)     |
|     - If LLM produces final response:                             |
|         exit loop                                                 |
|     - If step budget exhausted:                                   |
|         force final summary                                       |
|     - Sub-agents: LLM can spawn_subagent for complex tasks       |
|         (own 128k context, returns summary to head agent)         |
|                                                                    |
|  5. REFLECT                                                       |
|     - LLM evaluates: what happened, significance, lessons         |
|     - Rate cycle importance (0.0-1.0)                             |
|     - Identify facts/entities/relationships to store              |
|     - Assess need for self-modification                           |
|                                                                    |
|  6. PERSIST                                                       |
|     - Store cycle episode (Qdrant, with embedding)                |
|     - Publish outbox responses to NATS (fallback: outbox.json)   |
|     - Liveness check: lightweight LLM call concatenates 8-char    |
|       hex nonce with cycle number as nonce:cycle_number           |
|     - Write response.json (heartbeat with transformed nonce)      |
|     - Flush logs to drain volume                                  |
|     - Exit. Supervisor controls next cycle timing.                |
|                                                                    |
+------------------------------------------------------------------+
```

Cycle phases: WAKE > ORIENT > [MISSION_REVIEW] > PLAN > REASON+ACT > REFLECT > PERSIST. MISSION_REVIEW is conditional — it fires every `mission_review_interval` cycles (default 15, 0 = disabled). REASON and ACT are interleaved in a single loop (not separate phases). The LLM decides when to call tools and when to stop. Each full cycle produces exactly one heartbeat via a dedicated liveness LLM call in PERSIST. The supervisor owns cycle timing — there is no SLEEP phase inside the agent.

### Sub-Agent Architecture

The head agent delegates complex or context-heavy tasks to sub-agents via the `spawn_subagent` tool:

```
Head Agent (main cycle, 128k context)
  |
  |  "I need to research X, analyze Y, search Z"
  |
  +--spawn_subagent(task="research X", tools=["http_request","exec"])
  |    +-- Sub-agent gets own fresh 128k context
  |        +-- LLM reasons about the task
  |        +-- Calls tools (up to max_steps)
  |        +-- Returns summary (not raw tool output)
  |              +-- Head agent sees only the summary (~200 tokens)
  |                  instead of raw results (~5000+ tokens)
  |
  +--spawn_subagent(task="analyze Y", tools=["fs_read","memory_query"])
  |    +-- (same pattern)
  |
  +-- Head agent incorporates summaries into its reasoning
```

This is the primary context management strategy: delegate instead of truncate. The head agent maintains strategic context (goals, memories, inbox). Sub-agents handle tactical execution with their own context windows and return compressed results.

---

## 6. Memory Architecture

### Storage Layers

| Layer | Store | What it holds | Access pattern |
|-------|-------|--------------|----------------|
| **Registers** | Redis | Current cycle state, scratch vars, counters, flags | Sync read/write every cycle |
| **Working context** | In-process + Redis | Assembled context for current cycle reasoning | Built per cycle from other layers |
| **Short-term episodic** | Qdrant (collection: short_term) | Recent actions, observations, outcomes (hours to days) | Embedding similarity search |
| **Long-term episodic** | Qdrant (collection: long_term) | Significant past events, lessons learned, older episodes | Embedding similarity search with decayed relevance |
| **Fact semantic index** | Qdrant (collection: facts) | Fact embeddings for context-aware retrieval | Embedding similarity search (no time decay) |
| **Structured knowledge** | Postgres | Extracted facts (source of truth), configs, system state, learned rules | SQL queries, structured lookups |
| **Entity graph** | Apache AGE (Postgres extension) | Entities, relationships, dependency maps, system topology | Cypher graph queries via AGE |

### Embedding Model
- Model: `embedding-inno1`
- Dimensions: 1024
- Used for: vectorizing episodic memories and facts for similarity-based retrieval

### Memory Write Strategy
After each cycle, a dedicated memory consolidation step (part of REFLECT phase):
1. **Significance filter** — LLM rates "how important was this cycle's outcome?" Only persist above threshold for episodic memory
2. **Structured extraction** — LLM extracts entities, facts, relationships, and updates to write to Postgres and graph DB
3. **Summary generation** — compress cycle events into a storable episode with embedding

### Memory Read Strategy (ORIENT phase)
1. Load registers (Redis) — always, cheap
2. Query recent episodes (short-term vector DB) — last N hours, similarity to current goal state
3. Query relevant long-term episodes — similarity search against current situation
4. Query facts — **dual-path retrieval**: semantic search via Qdrant (context-aware, similarity to current situation) + structured query via Postgres (recent/confident). Results merged and deduplicated by fact ID. Semantic results ranked first, structured results fill gaps.
5. Query entity graph — relationships relevant to current context
6. Assemble into working context, truncated/summarized to fit context window

### Technology Choices

**Vector DB: Qdrant** — Purpose-built for vector search, better indexing/filtering than pgvector. Native support for multiple collections (clean short-term / long-term separation). Payload filtering for metadata queries alongside similarity search. MIT licensed, lightweight Docker image. Relevance decay: time-based exponential decay on episodic similarity search (configurable half-life, default 1 week).

**Entity Graph: Apache AGE (Cypher on Postgres)** — AGE extension on existing Postgres instance (PG18). Entities are labeled vertices (label = CamelCase entity_type), properties stored as vertex attributes. Relationships are directed labeled edges with arbitrary properties. Full Cypher query language: MATCH, MERGE, CREATE, pattern matching, variable-length paths. Agent can execute raw Cypher queries via `graph_query` tool. Own connection pool (separate from structured store).

**Graph Quality Controls (Phase L):**
- **Canonical relationship whitelist (L.1x + SA-EI):** 30 canonical relationship types enforced via a 4-tier normalization pipeline in `graph_tools.py`: (1) alias lookup (70+ known synonyms), (2) canonical passthrough, (3) fuzzy match (SequenceMatcher ≥0.7), (4) fallback to `RelatedTo`. Non-canonical types are never stored — all relationships are normalized before write. Original types: `CreatedBy`, `MaintainedBy`, `FundedBy`, `UsesArchitecture`, `UsesPersistence`, `HasSafety`, `HasLimitation`, `HasFeature`, `AffiliatedWith`, `PartOf`, `Extends`, `DependsOn`, `AlternativeTo`, `InspiredBy`, `RelatedTo`. SA types: `AlliedWith`, `HostileTo`, `TradesWith`, `SanctionedBy`, `SuppliesWeaponsTo`, `MemberOf`, `LeaderOf`, `OperatesIn`, `LocatedIn`, `BordersWith`, `OccupiedBy`, `SignatoryTo`, `ProducesResource`, `ImportsFrom`, `ExportsTo`. Temporal edges: relationships support `since`/`until` string properties for temporal graph queries.
- **Fuzzy entity dedup (L.4):** Before creating a new entity, `_find_similar_entity()` checks for existing entities with similar names (SequenceMatcher ≥0.85 after normalization: strip dashes/underscores/spaces, lowercase). Applied in both the `graph_store` tool handler (tool-call path) and `_store_reflection_graph()` (reflection path). Matching entities are auto-merged with feedback to the LLM. Name remapping ensures relationships reference the canonical entity name.

---

## 7. Goal System

### Seed Goal
- Human operator provides an initial goal as a text file
- Mounted read-only into the agent container at `/seed_goal`
- Cannot be modified by the agent, ever
- The agent can build sub-goals, reinterpret, decompose — but the seed text itself persists unchanged
- Loaded into every cycle's context

### Goal Hierarchy
```
Seed Goal (immutable, read-only mount)
  +-- Meta Goals (long-term strategic objectives, agent-generated)
       +-- Goals (operational objectives)
            +-- Sub-goals (tactical steps)
                 +-- Tasks (specific actions)
```

### Goal State
Stored in Postgres (structured) and loaded each cycle:
- Active goals with priority, progress estimate, status
- GoalStatus enum: `ACTIVE`, `PAUSED`, `BLOCKED`, `DEFERRED`, `COMPLETED`, `ABANDONED`
- `DEFERRED` goals have `deferred_until_cycle: int | None` (cycle number to revisit) and `defer_reason: str | None`. MISSION_REVIEW re-evaluates deferred goals past their revisit cycle.
- Goal dependencies (what blocks what)
- Goal history (completed, abandoned, failed — with rationale)
- Current focus (which goal is being actively pursued this cycle)

### Goal Management
The LLM decides during REASON phase:
- Which goal to focus on this cycle
- Whether to decompose a goal into sub-goals
- Whether to abandon a goal (with rationale)
- Whether to defer a goal for later revisit (`goal_update` action `defer` with `reason` and optional `revisit_after_cycles`, default 15)
- Whether to generate new goals based on observations
- Whether to re-prioritize based on new information

---

## 8. Self-Modification

### What's Modifiable (agent container, /agent volume)
- Agent source code (the main loop, utilities, helpers)
- Prompt templates (system prompt structure, memory retrieval prompts, reflection prompts)
- Tool definitions and implementations
- Memory retrieval strategies and parameters
- Goal evaluation heuristics
- Configuration values
- DAG pipeline definitions

### What's Immutable (outside agent's reach)
- Supervisor process and code (own container)
- Seed goal file (read-only mount)
- Heartbeat mechanism (supervisor-side)
- Log drain (host-side storage)
- Audit OpenSearch instance (agent has no credentials)
- Memory service infrastructure (separate containers — agent can read/write data but not modify the services)

### Self-Modification Flow
1. Agent identifies need for modification during REFLECT phase (or responds to operator directive)
2. Uses `fs_write` targeting `/agent/src/...` — intercepted by `_register_selfmod_fs_write()` in `cycle.py`
3. Routed through `SelfModEngine.propose_and_apply()`: captures before-snapshot, writes file, captures after-snapshot, git-commits with cycle number and rationale
4. `state.self_modifications` incremented, reported in heartbeat
5. **Changes take effect next cycle** — agent loads from `PYTHONPATH=/agent/src` (set by `agent-entrypoint.sh`), supervisor restarts agent process, new code is loaded fresh
6. No hot-reload. The agent must complete the current cycle before modifications activate. This prevents mid-cycle heartbeat bypass.

**Agent Entrypoint:** `docker/agent-entrypoint.sh` seeds `/agent/src` from the Docker image on first boot (empty volume). On subsequent boots, the existing (possibly self-modified) code in `/agent/src` is used. `PYTHONPATH=/agent/src` ensures Python loads from the modifiable volume, not from the installed package at `/app/src`.

### Rollback
- Before-snapshots stored for every modification
- If the agent detects degraded performance after a modification, it can roll back
- **Supervisor auto-rollback:** On heartbeat failure, if the /agent volume's git HEAD differs from the last known-good pre-launch state, the supervisor runs `git reset --hard` to the last known-good commit. Tracks HEAD *before* launching the agent each cycle (the state the agent actually booted from). On heartbeat success, that pre-launch HEAD is marked as the known-good state. Decrements the consecutive failure counter to give the rolled-back code a fresh chance. Human notified via outbox.

### Dynamic Tool Registration
- Directory: `/agent/tools/`
- Each file defines a tool: name, description, parameter schema, implementation
- Tool executor scans this directory each cycle and loads available tools
- Agent extends its own capabilities by writing new tool files

---

## 9. Tool System

### Core (16 tools)

| Tool | Category | Backend |
|------|----------|---------|
| `fs_read`, `fs_write`, `fs_list` | Filesystem | Local |
| `exec` | Shell | Local |
| `http_request` | HTTP | Internet |
| `memory_store`, `memory_query`, `memory_promote`, `memory_supersede` | Memory | Qdrant, Postgres |
| `graph_store`, `graph_query` | Graph | AGE/Postgres |
| `goal_create`, `goal_list`, `goal_update` (actions: progress, complete, abandon, pause, resume, reprioritize, defer), `goal_decompose` | Goals | Postgres |
| `code_test` | Self-mod | Local |
| `spawn_subagent` | Delegation | LLM API |

### NATS (5 tools)

| Tool | Backend |
|------|---------|
| `nats_publish` | NATS |
| `nats_subscribe` | NATS |
| `nats_create_stream` | NATS JetStream |
| `nats_queue_summary` | NATS |
| `nats_list_streams` | NATS JetStream |

**Initialization order:** Service connections (NATS, OpenSearch, Airflow) are established in `cycle.py:_wake()` BEFORE tool registration (`_register_builtin_tools()`). This ensures tool handler closures capture live client references. Previously, tools were registered first, causing handlers to close over `None` and fail with `'NoneType' object has no attribute 'publish'` on every call.

**Availability guards:** All 5 NATS tool handlers in `nats_tools.py` check `if not nats.available` before calling methods, returning a clean error message instead of crashing when the NATS service is unreachable.

### OpenSearch (6 tools)

| Tool | Backend |
|------|---------|
| `os_create_index` | OpenSearch |
| `os_index_document` | OpenSearch (single + bulk) |
| `os_search` | OpenSearch |
| `os_aggregate` | OpenSearch |
| `os_delete_index` | OpenSearch |
| `os_list_indices` | OpenSearch |

### Analytical Toolkit (5 tools)

| Tool | Backend |
|------|---------|
| `anomaly_detect` | PyOD (IForest, LOF, KNN) <- OpenSearch |
| `forecast` | statsforecast (AutoARIMA) <- OpenSearch |
| `nlp_extract` | spaCy (entities, noun chunks, sentences) <- OpenSearch/NATS |
| `graph_analyze` | NetworkX (centrality, PageRank, community, shortest path) <- AGE |
| `correlate` | scikit-learn (correlation, clustering, PCA) <- OpenSearch |

### Orchestration (5 tools)

| Tool | Backend |
|------|---------|
| `workflow_define` | Airflow API (deploy DAG file) |
| `workflow_trigger` | Airflow API (trigger DAG run) |
| `workflow_status` | Airflow API (run/task details) |
| `workflow_list` | Airflow API (DAG inventory) |
| `workflow_pause` | Airflow API (pause/unpause) |

### Pseudo-Tools (1)

| Tool | Purpose |
|------|---------|
| `cycle_complete` | Agent signals it has finished its plan early. Intercepted in `client.py` before reaching the executor (never actually executed). Registered in `cycle.py:_register_cycle_complete()` and listed in the system prompt (`templates.py`). Satisfies the `to=functions.` autoregressive prime while giving the agent a clean exit path instead of burning remaining steps. |

**Total: 43 built-in tools + 1 pseudo-tool + dynamic tools** (agent-created JSON definitions in `/agent/tools/`)

### SA: Source, Feed & Event Tools (8 tools)

| Tool | Backend |
|------|---------|
| `feed_parse` | httpx + feedparser (RSS/Atom) |
| `source_add`, `source_list`, `source_update`, `source_remove` | Postgres (source registry) |
| `event_store` | Postgres + OpenSearch (dual-store) |
| `event_query` | Postgres (structured filters) |
| `event_search` | OpenSearch (full-text) |

### SA: Entity Intelligence Tools (3 tools)

| Tool | Backend |
|------|---------|
| `entity_profile` | Postgres JSONB (create/update structured profiles with sourced assertions) |
| `entity_inspect` | Postgres (read profile with completeness, staleness, events, history) |
| `entity_resolve` | Postgres (resolve name to canonical entity, create stubs, link events) |

### Tool Timeout Enforcement
The `exec` and `http_request` tools accept an optional `timeout` argument from the agent. The agent can request shorter timeouts for quick operations, but the value is clamped to a configurable ceiling set by `AGENT_SHELL_TIMEOUT` and `AGENT_HTTP_TIMEOUT` env vars. The config value serves as both the default (when the agent omits the argument) and the maximum (when the agent requests more). Tool descriptions are updated at registration time to show the actual ceiling.

---

## 10. Prompt Architecture

### Context Assembly (per cycle)

The 128k context window must be carefully managed. Approximate budget:

| Section | Est. tokens | Source |
|---------|------------|--------|
| System prompt | ~500-1k | Static (identity, directives, cycle number) |
| Seed goal | ~200-1k | Read-only mount |
| Developer message (tool defs + calling instructions) | ~1-3k | /agent/tools/ directory + templates.py |
| Goal state summary | ~500-2k | Postgres |
| Retrieved memories | ~2-10k | Vector DB + structured queries |
| Recent cycle history (short-term) | ~2-5k | Redis / recent episodes |
| Current task context | ~1-5k | From previous tool results this cycle |
| **Available for reasoning + output** | **~100-120k** | Model generation |

Strategy: **delegate instead of truncate.** The sub-agent architecture is the primary context management mechanism. Multiple layers work together to keep the prompt within budget:

**Sliding window** (`client.py`): During the REASON+ACT tool loop, the last 5 tool steps (`SLIDING_WINDOW_SIZE`) are kept in full (including analysis channel messages). Older steps are condensed to one-line summaries: `Step N: tool_name(args_summary) -> result_preview`. This prevents unbounded context growth in long cycles.

**Tool result truncation** (`client.py`): Individual tool results exceeding `MAX_TOOL_RESULT_CHARS = 30000` characters are truncated with a note appended. This prevents oversized API responses (e.g., a GitHub recursive tree listing at 1.86M chars / ~465k tokens) from blowing past the 128k context window and causing 400 Bad Request errors from the LLM API.

**Working memory scratchpad** (`WorkingMemory` class in `client.py`): An in-cycle scratch buffer that records every tool result (step number, tool name, args summary, result summary) and free-form notes. Fed to re-grounding prompts, the REFLECT phase, and the forced-final prompt. Does NOT persist across cycles — episodic memory handles cross-cycle persistence.

**Re-grounding** (`REGROUND_INTERVAL = 8`): Every 8 tool steps, a re-grounding message is injected into the conversation containing the working memory summary. This prevents the model from losing track of its progress in long reasoning chains.

**Budget enforcement** (`assembler.py`): The prompt assembler tracks approximate token counts (4 chars per token heuristic) and enforces `AGENT_MAX_CONTEXT_TOKENS` (default 120k). When over budget, flexible sections are truncated in priority order: memories first, then goals. System prompt, tool definitions, inbox messages, and the action request are never truncated.

### Prompt Sections
1. **System message** — Identity, core behavioral directives, current date, cycle number, reasoning instructions
2. **Developer message** — Tool definitions in TypeScript format + explicit tool calling instructions (`to=functions.NAME json{...}` format with examples)
3. **Goal context (user message)** — Seed goal text (via `GOAL_CONTEXT_TEMPLATE`: "Primary Mission (Strategic Direction — Not a Task to Complete)") + current goal state + progress summary
4. **Memory context (user message)** — Retrieved relevant memories, summarized
5. **Cycle context (user message)** — What happened so far this cycle (if multi-step)
6. **Action request (user message)** — "Given the above, decide your next action."
7. **Mission review (conditional)** — `MISSION_REVIEW_PROMPT` template. Assembled only on MISSION_REVIEW cycles. Includes seed goal, active goals with progress, deferred goals, and performance data. LLM evaluates goal health, diminishing returns, mission coverage, and deferred-goal revisit.

---

## 11. Alignment and Safety

### Strategy: Defense in Depth
There is no single safety mechanism. Layers:

1. **Structural isolation** — Container boundary. Agent can't reach supervisor, seed goal is read-only, logs drain externally. Audit logs indexed into a dedicated OpenSearch instance (`opensearch-audit`) that the agent never receives credentials for — isolation by env var omission.
2. **Prompt alignment** — Initial system prompt establishes behavioral norms, goal adherence, self-limitation expectations.
3. **Heartbeat liveness** — Supervisor verifies LLM is in the loop via challenge-response. Code-only bypass detectable.
4. **Full I/O logging** — Every prompt input and output logged to host-side storage. Observable after the fact. Logs also indexed into the audit OpenSearch (searchable, 90-day ISM retention). Primary record stays on disk (JSONL archive).
5. **Model alignment** — The base model (InnoGPT-1) has its own alignment training. Built on top of that.
6. **Self-modification boundaries** — Even though the agent can modify /agent, it can't modify the container, the supervisor, or the seed goal.

### Known Gaps
- **No real-time intervention** — Human can send messages via inbox, but they're only read at cycle start (WAKE). Mid-cycle intervention requires killing the container.
- **Goal drift** — The agent can reinterpret the seed goal over time. The text persists but interpretation is in the agent's hands.
- **Memory poisoning** — Bad memories that influence future decisions. No automatic detection mechanism.
- **Hallucination compounding** — Agent hallucinates, acts on hallucination, stores result as fact, retrieves it later, reinforces the error.
- **Self-modification feedback loops** — Self-evaluated improvements that aren't actually improvements. Mitigated: supervisor auto-rollback reverts code that breaks agent boot. Doesn't catch subtle degradation.

---

## 12. Platform Services

All enhancements are domain-agnostic platform capabilities. The seed goal defines the domain; the platform provides tools.

### NATS + JetStream

Universal I/O layer — human comms, data feeds, inter-process events, and agent-created feedback loops all flow through the same mechanism.

**Subject architecture:**
```
legba.human.inbound    -- operator -> agent (priority, durable)
legba.human.outbound   -- agent -> operator
legba.data.*           -- external data feeds (7d retention)
legba.events.*         -- internal events (30d retention)
legba.alerts.*         -- priority alerts (30d retention)
legba.pipeline.*       -- orchestration results
```

Agent can create new subjects/streams at runtime via tools. JetStream provides durable delivery, replay, and consumer groups.

**Key decisions:** Drain `legba.human.*` fully every cycle (always small, always critical). Data subjects: fetch count + last N messages as summary in ORIENT. Human stream: unlimited retention. Data feeds: 7 days. Events/alerts: 30 days.

### OpenSearch

Large-scale search and analytics engine. Agent creates its own indices with whatever mappings fit the data.

**Role:** Bulk collected data — documents, time series, analytical outputs, search indices. Complements (not replaces) existing stores: Qdrant (semantic), Postgres (structured ACID), AGE (graph), Redis (transient KV).

**Features:** Full-text search (BM25), structured queries, aggregations, ISM lifecycle policies (90-day default hot retention). `os_index_document` accepts both single doc and array (bulk API internally). Index naming convention: `legba-{purpose}-{YYYY.MM}`.

**Audit instance:** Dedicated `opensearch-audit` on port 9201. Supervisor-only — agent cannot access. Cycle logs indexed with typed mappings, monthly indices (`legba-audit-YYYY.MM`), 90-day ISM retention.

### Analytical Toolkit

Pre-built analytical capabilities exposed as agent tools. The LLM focuses on reasoning and decision-making while specialized algorithms handle mechanical analysis.

**Capability matrix:**

| Data Type | Analysis | Tool | Input Source |
|-----------|----------|------|-------------|
| Time series | Anomaly detection | PyOD (IForest/LOF/KNN) | OpenSearch |
| Time series | Forecasting | statsforecast (AutoARIMA) | OpenSearch |
| Text documents | Entity extraction | spaCy (en_core_web_sm) | OpenSearch, NATS |
| Graph / relational | Centrality, community, paths | NetworkX | AGE graph |
| Tabular | Correlation, clustering, PCA | scikit-learn | OpenSearch |

**Data flow:** Tools accept data references (index name, query, graph label), not raw data as arguments. Tools have OpenSearch/Postgres clients injected at registration time.

### Orchestration: Airflow

DAG-based workflow engine for persistent data pipelines. Agent defines pipelines in Python, deploys them, and they run independently — surviving agent crashes, cycling, and restarts.

**Architecture:** Airflow standalone mode (single container: webserver + scheduler + SQLite). Custom Dockerfile + entrypoint. Under `--profile airflow`.

**Separation of concerns:**
- **Goals** = what the agent wants to accomplish (declarative, LLM-driven)
- **Pipelines** = how persistent operations execute (imperative, DAG-driven)
- **Supervisor** = agent process lifecycle (cycle management, heartbeat, rollback)

Pipeline outputs report back via NATS (`legba.pipeline.*`). DAG files stored in shared volume (`/airflow/dags`), managed via `workflow_define` tool.

### Storage Layer Map

```
+----------------------------------------------------------------------+
|                        Data Layer                                     |
|                                                                       |
|  +-----------+  +--------------+  +--------------+  +------------+   |
|  | Redis     |  | Qdrant       |  | Postgres+AGE |  | OpenSearch |   |
|  | :6379     |  | :6333        |  | :5432        |  | :9200      |   |
|  |           |  |              |  |              |  |            |   |
|  | Transient |  | Semantic     |  | Structured   |  | Bulk data  |   |
|  | state     |  | similarity   |  | ACID data    |  | Full-text  |   |
|  |           |  |              |  |              |  | search     |   |
|  | * cycle # |  | * episodes   |  | * goals      |  | * indices  |   |
|  | * flags   |  |   (short+    |  | * facts      |  | * docs     |   |
|  | * scratch |  |    long term)|  | * mods       |  | * time     |   |
|  | * counters|  | * fact       |  | * entity     |  |   series   |   |
|  |           |  |   embeddings |  |   graph      |  | * aggs     |   |
|  |           |  |              |  |   (Cypher)   |  | * ISM      |   |
|  +-----------+  +--------------+  +--------------+  +------------+   |
|                                                                       |
|  Access pattern:                                                      |
|  Redis: every cycle (cheap KV)                                       |
|  Qdrant: ORIENT (vector search), ACT (memory tools)                  |
|  Postgres: ORIENT (goals, facts), ACT (CRUD tools, Cypher)           |
|  OpenSearch: ACT (search/index tools), pipelines (bulk ingest)       |
+----------------------------------------------------------------------+
```

### Messaging Layer Map

```
+----------------------------------------------------------------------+
|                     Messaging Layer                                    |
|                                                                       |
|  PRIMARY (NATS + JetStream):                                         |
|  +--------------------------------------------------------------+    |
|  | NATS :4222                                                    |    |
|  |                                                               |    |
|  | legba.human.inbound    -- operator -> agent (priority)       |    |
|  | legba.human.outbound   -- agent -> operator                  |    |
|  | legba.data.*           -- external data feeds                |    |
|  | legba.events.*         -- internal events, triggers          |    |
|  | legba.alerts.*         -- priority alerts                    |    |
|  | legba.pipeline.*       -- orchestration results              |    |
|  |                                                               |    |
|  | Agent can create new subjects/streams at runtime              |    |
|  | JetStream: durable delivery, replay, consumer groups          |    |
|  +--------------------------------------------------------------+    |
|                                                                       |
|  FALLBACK (file-based, when NATS unavailable):                       |
|  +--------------------------------------------------------------+    |
|  | /shared/inbox.json  -- supervisor -> agent                    |    |
|  | /shared/outbox.json -- agent -> supervisor                    |    |
|  +--------------------------------------------------------------+    |
|                                                                       |
|  HEARTBEAT (always file-based -- safety isolation):                  |
|  +--------------------------------------------------------------+    |
|  | /shared/challenge.json -- supervisor -> agent                 |    |
|  | /shared/response.json  -- agent -> supervisor                 |    |
|  +--------------------------------------------------------------+    |
+----------------------------------------------------------------------+
```

---

## 13. Technical Decisions

| Decision | Rationale |
|----------|-----------|
| **Python (async)** | Interpreted = trivial self-modification, mature LLM/async ecosystem, rapid iteration |
| **Custom tool call parsing** | vLLM's native tool_choice unreliable. Custom parser handles both Harmony tokens and literal text |
| **Qdrant for vector search** | Purpose-built, multi-collection, payload filtering, MIT licensed |
| **Apache AGE for graph** | Cypher on Postgres, no new service. Labeled vertices, directed edges, raw Cypher access |
| **Postgres for structured data** | ACID, goals/facts/modifications |
| **Redis for registers** | Cheap KV for cycle state, counters, flags |
| **NATS for messaging** | Durable streaming, JetStream, replaces file-based comms |
| **OpenSearch for bulk data** | Full-text search, aggregations, ISM lifecycle |
| **Airflow for orchestration** | Apache 2.0, mature, DAG-native, REST API |
| **File-based heartbeat** | Safety isolation — must not share agent's messaging layer |
| **Next-cycle activation** | Self-modifications activate on next cycle only. No hot-reload |
| **Sub-agents for context** | Delegate instead of truncate. Each gets own 128k window |
| **Docker-first development** | Production topology = development topology. No host-side pip installs |
| **Schemas first** | All data models defined before implementation |
| **Graceful degradation** | Service failures never crash a cycle. Log and continue |
| **Minimal artificial limits** | Tool output capped at 30k chars (`MAX_TOOL_RESULT_CHARS`) to prevent API errors. No hard caps on self-mod rate or context sections. Agent learns other constraints |
| **Git on /agent from day one** | Auto-commit every self-modification |
| **Loose dependency pinning** | `>=` in pyproject.toml. No lockfile by design |

---

## 14. Risks and Concerns

### Technical
1. **Context window pressure (128k)** — Continuous agent accumulates context. Aggressive summarization needed. Memory retrieval quality is critical.
2. **Harmony tool calling fragility** — Must implement our own parsing rather than trusting vLLM's pipeline.
3. **Inference latency** — 128k context on 120B params. Individual inference steps observed up to ~120s in production (116s worst case). `PING_WAIT_SECONDS` increased to 150s to accommodate.
4. **Memory retrieval quality** — Bad retrieval -> bad context -> bad decisions -> bad memories -> worse retrieval. Potential death spiral.
5. **First-cycle bootstrapping** — Agent starts with no memories. Bootstrap prompt addon guides first N cycles.

### Architectural
6. **Hallucination in a loop** — One-shot hallucination is annoying. Continuous hallucination compounding is dangerous.
7. **Memory pollution** — No mechanism to detect or clean corrupted/hallucinated memories.
8. **Goal drift without detection** — Gradual reinterpretation of the seed goal.
9. **Self-modification death spirals** — Agent rates itself as improved when it isn't. Mitigated: supervisor auto-rollback + self-mod rate monitoring.
10. **The agent modifying its own memory interface** — Can change how it reads/writes memories, adding bias or filtering.

### Operational
11. **OpenSearch stale connections** — The OpenSearch async client (`opensearch.py`) holds persistent TCP connections. During long cycles (many reasoning steps, slow LLM inference), idle connections can exceed the TCP idle timeout and become stale. The next OpenSearch call on a stale connection fails with a connection-reset error. The client is configured with `max_retries=2` and `retry_on_timeout=True`, which mitigates most cases, but long-idle connections between cycles (when the agent container is not running) are not affected because each agent container gets a fresh client. The issue is within a single long-running cycle.

### Philosophical
12. **Identity through modification** — At what point has the agent modified itself enough that it's effectively a different agent?
13. **Alignment decay** — Initial prompt alignment may erode as the agent modifies its own prompts.
14. **Observation vs. intervention gap** — We can see everything (logs) but can only intervene by killing. Graceful shutdown mechanism (see section 4) provides a soft alternative.

---

## 15. Schemas

### Supervisor Protocol

```python
class Challenge(BaseModel):
    cycle_number: int
    nonce: str                    # 8-char hex string (uuid4().hex[:8])
    issued_at: datetime
    timeout_seconds: int = 300
    metadata: dict = {}

class CycleResponse(BaseModel):
    cycle_number: int
    nonce: str                    # Transformed nonce: "nonce:cycle_number" (e.g., "a3f1b2c4:42")
    started_at: datetime
    completed_at: datetime
    status: Literal["completed", "error", "partial"]
    cycle_summary: str
    actions_taken: int = 0
    goals_active: int = 0
    self_modifications: int = 0
    error: str | None = None
    signature: str | None = None  # Ed25519 signature of nonce:cycle_number
    metadata: dict = {}
```

### Human Communication

```python
class InboxMessage(BaseModel):
    id: str                                              # UUID
    timestamp: datetime
    content: str                                         # Free-text from human
    priority: Literal["normal", "urgent", "directive"]
    requires_response: bool
    metadata: dict = {}

class OutboxMessage(BaseModel):
    id: str
    timestamp: datetime
    in_reply_to: str | None     # References inbox message ID
    content: str                # Agent's response
    cycle_number: int
    metadata: dict = {}
```

---

## 16. Technology Stack

| Component | Technology | Python Package |
|-----------|------------|---------------|
| LLM API | OpenAI-compatible (vLLM) | `httpx` |
| Prompt format | Harmony | Custom (in-house) |
| Vector DB | Qdrant | `qdrant-client` |
| Structured DB | PostgreSQL | `asyncpg` |
| Entity Graph | Apache AGE (Cypher on Postgres) | `asyncpg` |
| Registers/KV | Redis | `redis[hiredis]` |
| Data models | Pydantic v2 | `pydantic` |
| Crypto | Ed25519 | `pynacl` |
| Embeddings | embedding-inno1 (1024d) | `httpx` (API call) |
| Containers | Docker + Compose | -- |
| Event Bus | NATS + JetStream | `nats-py` |
| Full-text Search | OpenSearch | `opensearch-py[async]` |
| Orchestration | Apache Airflow (standalone) | `httpx` (REST API) |
| Anomaly Detection | PyOD (IForest, LOF, KNN) | `pyod` |
| Forecasting | statsforecast (AutoARIMA) | `statsforecast` |
| NLP | spaCy (en_core_web_sm) | `spacy` |
| Graph Analytics | NetworkX | `networkx` |
| ML / Statistics | scikit-learn | `scikit-learn` |
| Async | asyncio | stdlib |

---

## 17. Configuration Reference

All tuning knobs exposed as env vars with sane defaults:

| Env Var | Default | Used By |
|---------|---------|---------|
| `LLM_TEMPERATURE` | 1.0 | Sampling temperature (gpt-oss recommended) |
| `LLM_TOP_P` | 1.0 | Nucleus sampling threshold (gpt-oss recommended) |
| `LLM_MAX_TOKENS` | 16384 | Max generation tokens per completion |
| `AGENT_MAX_REASONING_STEPS` | 20 (deployed: 30) | Max tool calls per cycle |
| `AGENT_MAX_SUBAGENT_STEPS` | 10 | Default sub-agent step budget |
| `AGENT_SHELL_TIMEOUT` | 60 (deployed: 120) | Shell exec timeout (seconds) |
| `AGENT_HTTP_TIMEOUT` | 30 (deployed: 120) | HTTP request timeout (seconds) |
| `AGENT_MEMORY_RETRIEVAL_LIMIT` | 5 | Episodic episodes per query |
| `AGENT_FACTS_RETRIEVAL_LIMIT` | 10 | Facts per query |
| `AGENT_PG_POOL_MIN` | 1 | Postgres connection pool min |
| `AGENT_PG_POOL_MAX` | 5 | Postgres connection pool max |
| `AGENT_BOOTSTRAP_THRESHOLD` | 5 | Cycles with bootstrap guidance |
| `AGENT_MISSION_REVIEW_INTERVAL` | 15 | Strategic goal-tree review every N cycles (0 = disabled) |
| `AGENT_QDRANT_SHORT_TERM` | legba_short_term | Qdrant short-term collection |
| `AGENT_QDRANT_LONG_TERM` | legba_long_term | Qdrant long-term collection |
| `AGENT_QDRANT_FACTS` | legba_facts | Qdrant facts semantic index |
| `NATS_URL` | nats://localhost:4222 | NATS server URL |
| `NATS_CONNECT_TIMEOUT` | 10 | NATS connection timeout (seconds) |
| `OPENSEARCH_HOST` | localhost | OpenSearch host |
| `OPENSEARCH_PORT` | 9200 | OpenSearch port |
| `OPENSEARCH_SCHEME` | http | OpenSearch scheme (http/https) |
| `OPENSEARCH_USERNAME` | *(none)* | OpenSearch auth username |
| `OPENSEARCH_PASSWORD` | *(none)* | OpenSearch auth password |
| `AIRFLOW_URL` | http://localhost:8080 | Airflow REST API URL |
| `AIRFLOW_ADMIN_USER` | airflow | Airflow admin username |
| `AIRFLOW_ADMIN_PASSWORD` | airflow | Airflow admin password |
| `AIRFLOW_DAGS_PATH` | /airflow/dags | Shared DAG file directory |
| `AUDIT_OPENSEARCH_HOST` | localhost | Audit OpenSearch host (supervisor-only) |
| `AUDIT_OPENSEARCH_PORT` | 9200 | Audit OpenSearch port |
| `AUDIT_OPENSEARCH_SCHEME` | http | Audit OpenSearch scheme |
| `SUPERVISOR_MAX_FAILURES` | 5 | Kill after N heartbeat failures |
| `SUPERVISOR_CYCLE_SLEEP` | 2.0 | Seconds between cycles |
| `SUPERVISOR_HEARTBEAT_TIMEOUT` | 300 (deployed: 600) | Seconds to wait for heartbeat |

---

## 18. Project Structure

```
legba/
├── docs/
│   ├── ARCHITECTURE.md              # This document
│   └── archive/                     # Build history
│       ├── DESIGN.md                # Original architecture & rationale
│       ├── IMPLEMENTATION.md        # Build phases, bugs, verification
│       └── enhancements.md          # Phases 8-11 planning & tracking
├── DEPLOYMENT.md                    # Production deployment & operations
├── README.md
├── pyproject.toml
├── .env
├── docker-compose.yml
├── docker/
│   ├── agent.Dockerfile
│   ├── agent-entrypoint.sh          # Seeds /agent/src on first boot, sets PYTHONPATH
│   ├── supervisor.Dockerfile
│   ├── airflow.Dockerfile           # Airflow standalone (webserver + scheduler)
│   └── airflow-entrypoint.sh        # DB migrate, admin user, start services
├── scripts/
│   └── diagnostics.sh               # Host-side diagnostic dump (all services)
├── seed_goal/                       # Seed goals (read-only mount)
│   ├── goal.txt                     # Active goal (copy your choice here)
│   ├── goal_cybersec.txt            # Cybersecurity threat analyst
│   ├── goal_osint.txt               # OSINT intelligence analyst
│   └── goal_autonomous_research.txt # AI agent landscape researcher
├── src/legba/
│   ├── __init__.py
│   ├── shared/                      # Shared between agent + supervisor
│   │   ├── __init__.py
│   │   ├── config.py                # Env-based configuration
│   │   ├── crypto.py                # Ed25519 signing/verification
│   │   └── schemas/
│   │       ├── __init__.py
│   │       ├── cycle.py             # CycleState, Challenge, CycleResponse
│   │       ├── goals.py             # Goal hierarchy models
│   │       ├── memory.py            # Episode, Fact, Entity
│   │       ├── tools.py             # ToolDefinition, ToolResult
│   │       ├── modifications.py     # ModificationProposal, Snapshot, Rollback
│   │       └── comms.py             # InboxMessage, OutboxMessage
│   ├── agent/
│   │   ├── __init__.py
│   │   ├── main.py                  # Entry point: single cycle execution
│   │   ├── cycle.py                 # WAKE->ORIENT->[MISSION_REVIEW]->PLAN->REASON+ACT->REFLECT->PERSIST
│   │   ├── llm/
│   │   │   ├── __init__.py
│   │   │   ├── client.py            # LLM client with logging proxy
│   │   │   ├── provider.py          # vLLM OpenAI-compat provider
│   │   │   ├── harmony.py           # Harmony prompt formatter (full spec)
│   │   │   └── tool_parser.py       # Parse tool invocations from completions
│   │   ├── memory/
│   │   │   ├── __init__.py
│   │   │   ├── registers.py         # Redis: cycle state, counters, flags
│   │   │   ├── episodic.py          # Qdrant: short-term + long-term episodes
│   │   │   ├── structured.py        # Postgres: facts, goal state, knowledge
│   │   │   ├── graph.py             # Apache AGE: entity graph (Cypher queries)
│   │   │   ├── opensearch.py        # OpenSearch: full-text search, bulk data
│   │   │   └── manager.py           # Unified retrieval interface
│   │   ├── goals/
│   │   │   ├── __init__.py
│   │   │   └── manager.py           # Goal CRUD, decomposition, focus selection
│   │   ├── tools/
│   │   │   ├── __init__.py
│   │   │   ├── registry.py          # Scan /agent/tools/, load definitions
│   │   │   ├── executor.py          # Dispatch tool calls, collect results
│   │   │   ├── subagent.py          # Sub-agent execution engine
│   │   │   └── builtins/
│   │   │       ├── __init__.py
│   │   │       ├── fs.py             # fs_read, fs_write, fs_list
│   │   │       ├── shell.py          # exec
│   │   │       ├── http.py           # http_request
│   │   │       ├── memory_tools.py   # memory_store, memory_query, memory_promote, memory_supersede
│   │   │       ├── graph_tools.py    # graph_store, graph_query (AGE/Cypher)
│   │   │       ├── goal_tools.py     # goal_create, goal_list, goal_update, goal_decompose
│   │   │       ├── selfmod_tools.py  # code_test (syntax + import validation)
│   │   │       ├── nats_tools.py     # nats_publish, nats_subscribe, nats_create_stream, nats_queue_summary, nats_list_streams
│   │   │       ├── opensearch_tools.py # os_create_index, os_index_document, os_search, os_aggregate, os_delete_index, os_list_indices
│   │   │       ├── analytics_tools.py  # anomaly_detect, forecast, nlp_extract, graph_analyze, correlate
│   │   │       └── orchestration_tools.py # workflow_define, workflow_trigger, workflow_status, workflow_list, workflow_pause
│   │   ├── comms/
│   │   │   ├── __init__.py
│   │   │   ├── nats_client.py       # NATS + JetStream async client wrapper
│   │   │   └── airflow_client.py    # Airflow REST API async client wrapper
│   │   ├── selfmod/
│   │   │   ├── __init__.py
│   │   │   ├── engine.py            # Propose, snapshot, apply, log modifications
│   │   │   └── rollback.py          # Revert from before-snapshots
│   │   ├── prompt/
│   │   │   ├── __init__.py
│   │   │   ├── assembler.py         # Context assembly + budget awareness
│   │   │   └── templates.py         # System prompt, goal framing, delegation guidance
│   │   └── log.py                   # Structured JSON logging to drain volume
│   └── supervisor/
│       ├── __init__.py
│       ├── main.py                  # Entry point: cycle loop
│       ├── lifecycle.py             # Launch/kill agent process per cycle
│       ├── heartbeat.py             # Challenge generation, response validation
│       ├── comms.py                 # Human inbox/outbox management
│       ├── cli.py                   # Operator CLI (send/read/status)
│       ├── audit.py                 # Audit log indexer (dedicated OpenSearch)
│       └── drain.py                 # Log collection from agent volume
└── tests/
    ├── __init__.py
    ├── test_unit.py                 # 188 unit tests
    ├── test_integration.py          # 32 integration tests
    └── test_graph_age.py            # 20 AGE graph integration tests
```

---

## 19. Seed Goals

Three seed goals defined. Any can be deployed depending on use case.

### Cybersecurity Threat Analyst

File: `seed_goal/goal_cybersec.txt`

> You are a cybersecurity threat analyst. You have access to scan results from our scanning service. Monitor all incoming scan data. Continuously track public threat intelligence — CVE databases, vendor advisories, threat feeds, security research. Correlate: when new vulnerabilities or threat campaigns emerge, assess which client domains are potentially affected based on their scan profiles. Flag critical risks immediately. Produce regular threat assessments. Build and expand your monitoring capabilities over time. Accuracy matters more than speed — false positives erode trust, missed correlations cost clients.

Key properties: Domain-specific (cybersec scanning + threat intel correlation). Graph-native (CVEs, domains, services, vendors, campaigns). Concrete deliverables (threat assessments, risk flags). Accuracy bias.

### Intelligence Analyst (OSINT)

File: `seed_goal/goal_osint.txt`

> Develop and maintain comprehensive real-time situational awareness of global events. Start with publicly available news sources. Continuously expand your information gathering capabilities — find new sources, build tools to access them, cross-reference across channels, identify patterns. Produce daily briefings. Flag critical developments immediately via outbox. Your awareness should deepen and broaden over time. You are an intelligence analyst, not a news aggregator — context, connections, and implications matter more than volume.

Key properties: Open-ended growth. Concrete deliverables (daily briefings, critical flags). Quality bias (analyst not aggregator). Self-improvement incentive ("build tools to access them").

### Autonomous AI Research Analyst

File: `seed_goal/goal_autonomous_research.txt`

> You are a technology research analyst. Your mission is to track and understand the current landscape of autonomous AI agent development. Monitor public sources — research papers, blog posts, GitHub repositories, developer forums, news articles. Build a structured knowledge base of projects, organizations, capabilities, and architectural patterns.

Key properties: Domain-agnostic but specific. Graph-native (projects, organizations, funding, citations). Exercises all tools (HTTP, OpenSearch, AGE, NATS, Qdrant, Airflow). Safe to start (public sources only).
