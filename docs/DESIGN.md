# Legba — Implementation Design

*Key design decisions, data flows, and component interactions.*
*Last updated: 2026-03-25*

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
| Structured | Postgres | Indefinite | Facts, goals, sources, signals, events, entity profiles |
| Graph | Apache AGE | Indefinite | Entity relationships (Cypher queries) |
| Bulk | OpenSearch | Indefinite | Full-text search, signal/event indices, aggregations |
| Journal archive | OpenSearch | Indefinite | Permanent record of all journal entries and consolidations (`legba-journal` index) |
| Config versions | Postgres | Indefinite | Versioned prompt templates, guidance, mission config (`config_versions` table) |
| Temporal graph | AGE + TimescaleDB | Indefinite | Weighted edges, structural balance triads, graph entropy, relationship history |

### Why Separate Stores

Each store serves a different access pattern:
- **Qdrant** for "what's relevant to what I'm doing now?" (embedding similarity with time decay)
- **Postgres** for "what do I know about entity X?" (structured queries, JSONB profiles)
- **AGE** for "how are these entities connected?" (Cypher pattern matching)
- **OpenSearch** for "find all signals/events mentioning this term" (full-text search + aggregations)
- **Redis** for "what happened last cycle?" (fast key-value, no persistence guarantees needed)

### Fact Evidence Tracking

Each fact accumulates an `evidence_set` — a list of evidence items linking the fact to the signals, events, or tool results that support it. Evidence items record:

- **signal_id / event_id** — the backing evidence
- **relationship** — how the evidence relates to the fact (`supports`, `corroborates`, `challenges`)
- **confidence** — the evidence item's individual strength
- **observed_at** — when the evidence was recorded

This allows traceability: any stored fact can answer "what signals support this claim?" The maintenance daemon and subconscious service both write evidence items as they detect corroboration or contradiction.

### Contradiction Detection

When a new fact is stored, the structured store checks for contradictions against existing facts sharing the same subject. Contradictions are detected via two mechanisms:

1. **Predicate-level incompatibility** — A table of semantically contradictory predicate pairs (e.g., `AlliedWith` contradicts `HostileTo`; `MemberOf` contradicts `WithdrewFrom`). If the new fact's predicate contradicts an existing active fact on the same subject, the existing fact is flagged.

2. **Value-level conflict** — For predicates like `LeaderOf` or `CapitalOf` where only one value is valid at a time, a new fact with a different value automatically supersedes the old one (volatile auto-supersede).

Contradicted facts are not deleted — they are lowered in confidence (typically -0.3) and retain a `contradiction_of` back-reference. This preserves the full evidence chain and lets the agent or operator review conflicting information rather than silently discarding it.

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

### Nexus Nodes (Reified Relationships)

Simple edges work for direct relationships (US AlliedWith UK), but proxy chains, covert channels, and intermediary-mediated interactions need richer structure. A **Nexus node** is a reified relationship — an AGE vertex of label `Nexus` that sits between the actor and target, connected by three typed edges:

- **PARTY_TO** (actor -> Nexus) — who initiates
- **TARGETS** (Nexus -> target) — who is acted upon
- **CONDUCTED_VIA** (Nexus -> intermediary) — optional proxy or channel entity

Each Nexus carries `channel` (proxy, covert, diplomatic, financial, military, etc.), `intent` (hostile, supportive, neutral), and a free-text `description`. A corresponding row in the `nexus_operations` Postgres table holds the full metadata and links to evidence.

Nexus nodes coexist with flat edges in the same graph. The `graph_store_nexus` tool creates them; `graph_store` continues to handle direct relationships. Structural balance analysis reads `intent` from Nexus nodes for edge signing, so proxy warfare and covert operations factor into triadic stability calculations alongside explicit AlliedWith/HostileTo edges.

### Temporal Fact Enforcement

Facts carry `valid_from` and `valid_until` timestamps. Two auto-supersession mechanisms keep the fact base current:

1. **Volatile predicates** (LeaderOf, HeadOfState, HeadOfGovernment, etc.) — a new value for a different subject auto-supersedes the old one across subjects (e.g., storing "Country X LeaderOf PersonB" supersedes "Country X LeaderOf PersonA").
2. **Single-value predicates** (broader set including CapitalOf, President, PrimeMinister, etc.) — a new value for the same subject auto-supersedes the old one (e.g., "France CapitalOf Paris" is protected; "France CapitalOf Lyon" would supersede it).

Superseded facts receive a `valid_until = NOW()` and a `superseded_by` foreign key. Default queries exclude expired and superseded facts, but the full history remains available for temporal analysis.

---

## 4. Cycle Flow

### Module Structure

`cycle.py` is a thin orchestrator (~435 lines) that inherits from 13 phase mixins in the `phases/` directory (plan/act logic merged into cycle.py). Each mixin owns one phase of the cycle:

| Mixin | File | Phase |
|-------|------|-------|
| `WakeMixin` | `phases/wake.py` | Service init, tool registration |
| `OrientMixin` | `phases/orient.py` | Memory/context gathering, live infra health check |
| *(plan logic)* | *(merged into cycle.py)* | LLM planning + tool selection |
| *(act logic)* | *(merged into cycle.py)* | Tool loop execution |
| `ReflectMixin` | `phases/reflect.py` | Significance, facts, graph extraction |
| `NarrateMixin` | `phases/narrate.py` | Journal entries + consolidation |
| `PersistMixin` | `phases/persist.py` | Memory storage, goal completion, heartbeat |
| `IntrospectMixin` | `phases/introspect.py` | Mission review, analysis reports |
| `ResearchMixin` | `phases/research.py` | Entity enrichment cycles |
| `AcquireMixin` | `phases/acquire.py` | Dedicated source fetching + signal ingestion (legacy; only when ingestion service not active) |
| `CurateMixin` | `phases/curate.py` | Signal review, event creation, editorial judgment (replaces ACQUIRE when ingestion active) |
| `SurveyMixin` | `phases/survey.py` | Analytical desk work — situations, graph building, hypothesis evaluation (replaces NORMAL) |
| `SynthesizeMixin` | `phases/synthesize.py` | Deep-dive investigation, situation briefs, hypothesis creation |
| `AnalyzeMixin` | `phases/analyze.py` | Pattern detection, graph mining, anomaly detection |
| `EvolveMixin` | `phases/evolve.py` | Self-improvement, operational scorecard, change tracking |

Class attributes (e.g. `_JOURNAL_KEY`, `_JOURNAL_MAX_ENTRIES`) are defined on their owning mixin and accessed via MRO since `AgentCycle` inherits all mixins. Phase modules use `TYPE_CHECKING` guards for `AgentCycle` type hints to avoid circular imports.

### Normal Cycle

```
main.py:main()
  └── AgentCycle.run()
        ├── _wake()                     [phases/wake.py]
        │     ├── Load challenge.json, seed goal, world briefing
        │     ├── Connect: Redis, Postgres/AGE, Qdrant, OpenSearch, NATS, Airflow
        │     ├── _register_builtin_tools() → 67 tools from 19 modules + 2 config tools
        │     └── Drain NATS inbox
        │
        ├── _orient()                   [phases/orient.py]
        │     ├── Retrieve episodic memories (Qdrant similarity search)
        │     ├── Load active goals + goal work tracker (Postgres + Redis)
        │     ├── Query known facts (Postgres)
        │     ├── Live infrastructure health check (Postgres, AGE, Redis, Qdrant)
        │     ├── Build graph inventory (entity counts by type, top relationships)
        │     ├── Query source health stats (total sources, utilization %)
        │     ├── Get NATS queue summary
        │     ├── Load previous reflection forward
        │     ├── Load journal context (consolidation + recent entries)
        │     ├── Compute priority stack (situation ranking by velocity/goals/watches/recency)
        │     └── Inject differential briefing from subconscious accumulator (Redis)
        │
        ├── _plan()                     [phases/plan.py]
        │     ├── assemble_plan_prompt() → [system, user]
        │     ├── LLM completion → prose plan + "Tools: tool1, tool2, ..."
        │     └── Parse planned_tools set
        │
        ├── _act()                      [phases/act.py]
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
        ├── _reflect()                  [phases/reflect.py]
        │     ├── assemble_reflect_prompt() → [system, user]
        │     └── LLM → JSON: {summary, significance, facts, entities, goal_progress, ...}
        │
        ├── _narrate()                  [phases/narrate.py]
        │     ├── assemble_narrate_prompt() → [system, user]
        │     ├── LLM → JSON array of 1-3 journal entries
        │     └── Archive entries to OpenSearch (legba-journal index)
        │
        └── _persist()                  [phases/persist.py]
              ├── Store episodic memory (Qdrant)
              ├── Auto-complete goals at 100% progress
              ├── Auto-promote memories (significance >= 0.6 → long-term)
              ├── Update goal work tracker (Redis)
              ├── Store facts from reflection (Postgres, normalized predicates, triple-deduped)
              ├── Publish outbox messages (NATS)
              ├── Store journal entries (Redis)
              └── Liveness check (nonce echo — dedicated LLM call, reasoning: low)
```

### Survey Cycle (default — replaces NORMAL)

The default cycle type when no specialized cycle fires. Analytical desk work with a restricted tool set — no feed_parse, no source fetching, no code modification. Rate-limited http_request (max 2 calls/cycle, verification only).

```
_wake() → _orient() →
  _survey()
    ├── _build_survey_context()
    │   ├── Recent events (last 24h)
    │   ├── Active situations with intensity scores
    │   ├── Events not linked to any situation
    │   ├── Active predictions needing evidence check
    │   ├── Recent watch triggers
    │   ├── Journal investigation leads
    │   └── Uncurated signal count (cached for dynamic CURATE promotion)
    ├── assemble_survey_prompt() with SURVEY_TOOLS
    ├── reason_with_tools() → full tool loop with rate-limited executor
    ├── _reflect()
    ├── _narrate()
    └── _persist()
```

Tier 3 dynamic fill: when no Tier 1 or Tier 2 cycle fires, CURATE and SURVEY compete on score. CURATE scores by uncurated signal backlog (capped at 0.6), SURVEY is fixed at 0.4. Cooldown halves the previous dynamic type's score to prevent repetition.

### Synthesize Cycle (every 10, non-evolve/introspection)

Deep-dive investigation into a single situation or emerging pattern. Produces a named deliverable: a **Situation Brief** stored in Redis and archived to OpenSearch.

```
_wake() → _orient() →
  _synthesize()
    ├── _build_synthesize_context()
    │   ├── Recently investigated threads (anti-rabbit-holing from Redis)
    │   ├── Candidate situations ranked by novelty and intensity
    │   ├── Active predictions for evaluation
    │   ├── High-activity entities (last 48h)
    │   └── Journal investigation leads
    ├── assemble_synthesize_prompt() with SYNTHESIZE_TOOLS
    ├── reason_with_tools() → full tool loop with http_request
    ├── _store_situation_brief() → Redis + OpenSearch archive
    ├── _update_synth_history() → track thread rotation
    ├── _reflect()
    ├── _narrate()
    └── _persist()
```

### Research Cycle (every 7, non-introspection/analysis/synthesize)

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

### Acquire Cycle (legacy fallback)

Only used when the ingestion service is not active. The agent fetches sources directly and stores signals. Dormant when ingestion service is running.

### Curate Cycle (every 9, when ingestion active)

Replaces ACQUIRE when the ingestion service handles source fetching. The agent applies editorial judgment to raw signals and auto-created events:

```
_wake() → _orient() →
  _curate()
    ├── _build_curate_context()
    │   ├── Unclustered signals (no linked event, top 20 by confidence)
    │   ├── Auto-created events with signal_count <= 2 (top 15, need review)
    │   ├── Trending events with signal_count > 2 (top 5)
    │   └── Data overview (total signals, events, unlinked count)
    ├── assemble_curate_prompt() with restricted tools:
    │   signal_query, signal_search, event_create, event_update,
    │   event_query, event_link_signal, entity_profile, entity_inspect,
    │   entity_resolve, graph_store, graph_query, memory_query
    ├── reason_with_tools() → full tool loop with curate tools
    ├── _reflect()
    ├── _narrate()
    └── _persist()
```

The agent can: link unclustered signals to existing events (`event_link_signal`), create new events from signals the clusterer missed (`event_create`, confidence 0.7), refine auto-created events by improving titles, adjusting severity, or adding summaries (`event_update`), and enrich entity profiles.

---

## 5. Data Flow: Signal-to-Event Pipeline

Legba uses a two-tier information model: **signals** (raw ingested material) and **events** (derived real-world occurrences). Signals are atomic collection units; events are the analytical units that reports, situations, and graph analysis operate on.

### Signal Ingestion

```
Source → Fetch → Normalize → Dedup → Signal (Postgres + OpenSearch)
                                         │
                                         ├── signal_entity_links (spaCy NER)
                                         └── Increment source signal count

      entity_resolve(name, signal_id, role)
            ├── Resolution cascade (exact → alias → fuzzy → stub)
            ├── Create/update EntityProfile in Postgres
            └── Create SignalEntityLink junction

      graph_store(entity, relate_to, relation_type, since)
            ├── Normalize relationship type (70+ aliases → 30 canonical)
            ├── Fuzzy entity matching (prevent duplicates)
            └── Upsert vertex + MERGE edge in AGE

      graph_store_nexus(type, channel, intent, actor, target, via)
            ├── Create Nexus node + PARTY_TO/TARGETS/CONDUCTED_VIA edges
            └── For proxy chains, covert channels, intermediary relationships
```

### Composite Confidence Scoring

Each signal carries a composite confidence score derived from a gatekeeper formula:

```
Gate     = source_reliability * classification_confidence
Modifier = 0.4 * temporal_freshness + 0.35 * corroboration + 0.25 * specificity
Confidence = Gate * Modifier
```

The gate ensures unreliable sources or poorly classified signals can never produce high confidence regardless of other factors. The modifier captures freshness, corroboration, and specificity:

| Component | Source | Range |
|-----------|--------|-------|
| `source_reliability` | `sources.reliability` column | 0.0-1.0 |
| `classification_confidence` | Classifier self-reported score | 0.0-1.0 |
| `temporal_freshness` | Linear decay: 1.0 at 0h, 0.5 at 24h, 0.0 at 168h | 0.0-1.0 |
| `corroboration` | Independent source count on the same event (maintenance daemon) | 0.0-1.0 |
| `specificity` | SLM-assessed signal specificity (subconscious service) | 0.0-1.0 |

Individual components are stored as `confidence_components` JSONB on the signal row so the composite can be recomputed when any input changes. Weights are configurable via environment variables.

### Signal Provenance

Every signal carries a `provenance` JSONB column recording its full processing trace through the ingestion pipeline. This is an append-only log of each processing step:

- **normalizer** — source, fetch timestamp, title/body extraction
- **dedup** — which dedup tier resolved (GUID, source_url, Jaccard), or `new` if no match
- **confidence** — the individual component values and final composite score
- **clusterer** — event assignment (event_id, method: new_cluster / reinforced / singleton_promoted / unclustered)

Provenance is immutable after creation. It provides an end-to-end audit trail answering "how did this signal get here and why does it have this confidence?"

### URL Discovery

The ingestion pipeline extracts URLs from signal content and stores unique base domains in a `discovered_urls` table, deduplicating against existing active sources. High-frequency domains surface to EVOLVE cycles for operator/agent review as potential new data sources.

### 4-Tier Signal Dedup

1. **GUID fast-path** — Exact match on RSS guid / Atom id. Instant rejection.
2. **Source URL dedup** — Exact match on source_url after normalization.
3. **Vector cosine similarity** — Embedding-based semantic dedup via Qdrant.
4. **Jaccard similarity** — Title words with source suffix/prefix stripping (e.g., " - Reuters", "BBC News: "). 50% word overlap within +/-1 day, or last 100 signals if no timestamp.

### Deterministic Clustering (every 20 min)

```
Unclustered Signals (no signal_event_links entry)
      │
      ├── Extract features: actors, locations, title words, timestamp, category
      │
      ├── Score pairwise similarity (composite):
      │     entity overlap    0.3
      │     title Jaccard     0.3
      │     temporal proximity 0.2  (linear decay over 48h)
      │     category match    0.2
      │
      ├── Single-linkage clustering (threshold: 0.4)
      │
      ├── Multi-signal clusters → Create or merge-into Event
      │     ├── Title: highest-confidence signal's title
      │     ├── Time window: min(timestamps) → max(timestamps)
      │     ├── Category: modal across signals
      │     ├── Confidence: mean, capped at 0.6 (auto-created)
      │     └── Link all signals via signal_event_links
      │
      └── Singletons:
            ├── Structured sources (NWS, USGS, GDACS, etc.) → auto-promote to 1:1 Event
            └── RSS singletons → wait for agent or next clustering window
```

### Event Reinforcement

When new signals cluster into an existing event:
- `signal_count` is bumped
- Confidence increases: `min(0.8, 0.4 + 0.05 * signal_count)`
- `time_end` is extended to `max(timestamps)`
- Actors/locations sets are merged
- Threshold alerts logged at 3, 5, 10, 20 signals

### Event Lifecycle State Machine

Every derived event has a `lifecycle_status` that transitions deterministically based on signal activity and temporal rules. The maintenance daemon evaluates transitions on a 5-minute tick:

```
                ┌──────────┐  signal_count >= 3   ┌────────────┐
                │ EMERGING ├─────────────────────>│ DEVELOPING │
                │          │                       │            │
                │          │  no signals 48h       │            │  signal_count >= 5
                │          ├──────────┐            │            │  AND confidence >= 0.6
                └──────────┘          │            │            ├──────────┐
                                      v            └─────┬──────┘          v
                                ┌──────────┐             │          ┌──────────┐
                                │ RESOLVED │<────────────┘          │  ACTIVE  │
                                │          │  no signals 72h        │          │
                                │          │                        │          │  velocity > 2.0
                                │          │<───────────────────────┤          ├──────────┐
                                │          │  no signals 7d         └─────┬────┘          v
                                └─────┬────┘                              ^        ┌──────────┐
                                      │                                   │        │ EVOLVING │
                                      │  new signal linked                │        │          │
                                      v                                   │        └─────┬────┘
                                ┌──────────────┐   immediate              │              │
                                │ REACTIVATED  ├──────────────>───────────┘  velocity    │
                                └──────────────┘   (→ DEVELOPING)            < 1.5      │
                                                                             ───────────┘
```

| Status | Meaning | Entry condition |
|--------|---------|-----------------|
| `EMERGING` | New event, few signals | Default on creation |
| `DEVELOPING` | Gaining corroboration | signal_count >= 3, or reactivated |
| `ACTIVE` | Confirmed, well-sourced | signal_count >= 5 AND confidence >= 0.6 |
| `EVOLVING` | Rapid development | signal velocity > 2.0 signals/hour |
| `RESOLVED` | No longer developing | No new signals within the decay window |
| `REACTIVATED` | Resolved event re-emerges | New signal linked to a resolved event |

Velocity is measured as signals per hour over a 6-hour trailing window. All transitions are deterministic — no LLM involved.

### Agent Curation (CURATE cycle)

```
Clustered Events + Unclustered Signals
      │
      └── Agent CURATE cycle (editorial judgment)
            ├── Review unclustered signals → event_create (conf 0.7) or event_link_signal
            ├── Refine auto-events → event_update (title, severity, event_type, summary)
            ├── Enrich entity profiles from signal/event context
            └── Triage low-confidence events
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

### Event Confidence Caps
- Auto-created events (clustering): capped at confidence 0.6
- Agent-created events (CURATE tools): capped at confidence 0.7
- Reinforced events (signal_count growth): capped at 0.8
- Only the operator (via UI or manual DB update) can set confidence above 0.8

### Full Audit Trail
- Every prompt, response, tool call, and error logged to JSONL
- Archived per-cycle in `/logs/archive/cycle_NNNNNN/`
- Indexed in audit OpenSearch (isolated, agent cannot access)
- 90-day ISM retention policy

---

## 8. Airflow DAGs

Four Airflow DAGs run on fixed schedules, independent of the agent cycle. They handle background maintenance, quality assurance, and decision surfacing.

| DAG | Schedule | Purpose |
|-----|----------|---------|
| `metrics_rollup` | Hourly | Roll raw TimescaleDB metrics into hourly/daily aggregates for Grafana dashboards and baseline comparisons |
| `source_health` | Every 6h | Auto-pause sources with >20 consecutive failures; report source utilization stats |
| `decision_surfacing` | Every 12h | Identify stale goals (>7 days), dormant situations, merge candidates — surfaces items needing human or agent attention |
| `eval_rubrics` | Every 8h | Automated quality evaluation: event dedup rate (<3%), graph quality (RelatedTo edges <5%, isolated nodes <5%), zero-signal source rate (<10%), entity link density |

DAGs are deployed via the `workflow_define` orchestration tool (writes Python to the shared dags volume) or manually placed in `dags/`. Results from `eval_rubrics` are written to TimescaleDB as eval metrics and visualized in Grafana.

### Quality Assurance / Eval Rubrics

The `eval_rubrics` DAG implements the quantitative checks from `EVALUATION_RUBRICS.md`:
- **Event dedup rate** — duplicate event titles within 7 days, target <3%
- **Graph quality** — RelatedTo edge ratio (lazy relationship typing) and isolated node ratio, target <5% each
- **Source health** — active sources that have never produced a signal, target <10%
- **Entity link density** — average entity links per event (tracks enrichment coverage)

Each check writes its result to TimescaleDB (`metrics` table, dimension `eval`) so trends are visible in Grafana alongside operational metrics.

---

## 9. Dedup Strategies

Deduplication is enforced at multiple layers — ingestion, tool-level, and background evaluation — to prevent data sprawl without constraining the LLM's flexibility.

### Signal Dedup (ingestion)

Three-tier check in `ingestion/dedup.py` before any signal is stored:
1. **GUID fast-path** — exact match on RSS guid / Atom id
2. **Source URL** — exact match after URL normalization
3. **Jaccard title similarity** — word-set overlap after source suffix/prefix stripping (e.g., " - Reuters"), threshold 50%, within +/-1 day window

### Event Dedup (agent tools)

`derived_event_tools.py:event_create` checks before creating a new event:
- Exact title match against recent events (7 days)
- Jaccard title-word similarity against recent events, threshold configurable (default ~0.5). Returns `duplicate_detected` with the existing event ID so the agent can use `event_update` or `event_link_signal` instead.

### Hypothesis Dedup (agent tools)

`hypothesis_tools.py:hypothesis_create` checks before creating:
- Jaccard similarity of thesis words against existing hypotheses for the same situation, threshold 0.45. Returns `duplicate_detected` with the existing hypothesis ID.

### Situation Dedup (agent tools)

`situation_tools.py:situation_create` checks before creating:
- Exact name match
- Fuzzy word-overlap (Jaccard on name words with stop-word removal), threshold 0.50. Returns `duplicate_detected` with overlap words.

### Watchlist Dedup (agent tools)

`watchlist_tools.py:watchlist_add` checks before creating:
- Exact name match
- Term overlap — Jaccard on the union of entities + keywords against existing active watches, threshold 0.50. Returns `duplicate_detected` with overlapping terms.

### Background Dedup Audit

The `eval_rubrics` Airflow DAG checks event dedup rate every 8 hours, flagging when duplicate titles exceed 3% of recent events.

---

## 10. Graph Database (Apache AGE)

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

The `graph_query` tool does **not** expose raw Cypher. Instead it provides named operations (`top_connected`, `shared_connections`, `path`, `triangles`, `by_type`, `edge_types`, `isolated`, `recent_edges`) that map to pre-built AGE-compatible queries. This prevents the LLM from generating Neo4j-style Cypher that AGE rejects.

### Graph Schema

Vertices: labeled by entity type (CamelCase), or `Nexus` for reified relationships. Entity vertices carry `name`, `entity_id`, `created_at`, `updated_at`, plus arbitrary key-value pairs. Nexus vertices carry `op_id`, `type`, `channel`, `intent`, `description`.

Edges: labeled by relationship type (30 canonical, CamelCase) for flat edges. Nexus-specific edge types: `PARTY_TO`, `TARGETS`, `CONDUCTED_VIA`. All edges support `since`, `until` (temporal) properties plus arbitrary key-value pairs.

Entity deduplication: `upsert_entity` first checks for any existing vertex with the same name (regardless of label) before creating. This prevents duplicates when the entity type changes between calls.

---

## 11. Known Limitations

### LLM Behavioral Issues
- **Multi-message errors**: GPT-OSS occasionally generates multiple Harmony message blocks → 400 error. Mitigated by `{"actions": [...]}` wrapper, but still happens (~1-2% of steps).
- **Repetitive enrichment**: Agent sometimes re-does work it already completed. Partially addressed by goal work tracker and stall detection, but the LLM doesn't always check entity_inspect before re-enriching.
- **Dud cycles**: Occasionally the LLM generates a brief instead of executing tools (0 actions). The forced-final mechanism catches this but the cycle is wasted.

### Tool Utilization Gap
The agent has converged on a core working set of ~15-20 tools (entity resolution, event curation, graph building, hypothesis/situation management). Orchestration tools (5) and some raw OpenSearch tools see limited use. Analytics tools are now used during ANALYSIS cycles. Config tools (`config_read`, `config_update`) are available in EVOLVE cycles for prompt self-modification via the versioned config store.

### Context Pressure
The full system prompt with all guidance addons is ~20k tokens. With tool definitions, goals, memories, and world briefing, REASON calls regularly hit 40-60k tokens — half the budget used before the LLM generates anything. The planned-tool filtering helps, but long-running tool loops with many results still approach the 120k budget.

---

## 12. Consultation Engine

### Why Not Reuse LLMClient

`LLMClient` is deeply coupled to the agent cycle — it manages sliding windows, working memory, phase-aware prompt assembly, forced-final fallback, and step budgets. The consultation engine needs none of this. It uses providers (`VLLMProvider` / `AnthropicProvider`) directly with its own lightweight tool-calling loop.

### Architecture

The engine (`src/legba/ui/consult.py`) runs inside the UI container, not the agent container. It has:

- **Own LLM config** via `CONSULT_*` env vars, defaulting to Anthropic (Claude Sonnet). This keeps the operator's interactive queries on a fast, reliable model even when the agent runs on vLLM/GPT-OSS.
- **13 read-mostly tools** wired to the same Postgres, OpenSearch, Qdrant, Redis, and AGE stores that the agent writes to. Three write tools (`update_situation`, `update_goal`, `send_message`) allow lightweight operator actions.
- **`respond` tool pattern**: The LLM signals it is done by calling `respond(answer=...)`. The loop terminates and the answer is returned. If the LLM never calls `respond` within 10 steps, the last assistant content is used as a fallback.

### Session Management

Multi-turn conversation state is stored in Redis under `legba:consult:session:{id}` with a 1-hour TTL. Each exchange appends the user message, any tool call/result pairs, and the final assistant response. The session key is tracked via a browser cookie.

### Provider Branching

Same branching logic as the agent, applied locally:
- **Anthropic**: proper `system` field + multi-turn `messages` array with role alternation.
- **vLLM**: single combined user message (system + history concatenated), matching GPT-OSS's expected format.

### Error Recovery

- **Empty responses**: If the provider returns empty content, the engine re-prompts once ("You returned an empty response — please try again").
- **400 retry**: For vLLM, 400 errors (typically GPT-OSS Harmony multi-message issues) trigger a single retry.
- **Timeout / 5xx**: Surfaced to the operator as an error message in the chat UI.

---

## 13. Priority Stack

### Design

The priority stack (`shared/priority.py`) ranks active situations by composite score to guide agent focus. Computed during ORIENT and injected into PLAN context as a differential briefing.

**Scoring formula:**
```
score = (event_velocity * 0.3) + (goal_overlap * 0.25)
      + (watchlist_trigger_density * 0.25) + (recency_penalty * 0.2)
      + structural_instability_boost  (capped at 0.10)
```

**Components:**
- **Event velocity** — new events linked to the situation per unit time
- **Goal overlap** — how many active goals reference this situation
- **Watchlist trigger density** — recent watch triggers matching situation entities/regions
- **Recency penalty** — cycles since last agent attention, with adaptive staleness thresholds by severity (critical: 5 cycles, high: 10, medium: 20, low: 30)
- **Structural instability boost** — derived from structural balance analysis; situations whose entities appear in unbalanced triads (friend-of-friend-is-enemy) receive a scoring boost

The stack is advisory — it informs the agent's planning but does not override cycle type routing.

---

## 14. Config Store Architecture

Versioned configuration store (`shared/config_store.py`) backed by a Postgres `config_versions` table. Replaces filesystem-based prompt self-modification with tracked, rollback-capable versioned storage.

**Schema:** `config_versions(id SERIAL, key TEXT, value TEXT, version INT, created_at TIMESTAMPTZ, created_by TEXT, notes TEXT, active BOOL)`

**Operations:**
- `get(key)` — returns the active version's value
- `set(key, value, created_by, notes)` — creates a new version, deactivates the previous
- `history(key, limit)` — returns version history for a key
- `rollback(key, version)` — reactivates a specific version, deactivates the current

**Seeding:** On first boot, `templates.py` contents are loaded as version 1. Subsequent changes via `config_update` tool or UI create new versions.

**Access paths:**
- Agent: `config_read` / `config_update` tools (EVOLVE cycle tool set)
- Operator: UI config panel or `/api/v2/config` REST endpoints
- Audit: every version records `created_by` (agent cycle number or operator username) and `notes`

---

## 15. Hybrid LLM Routing

The `PromptRouter` (`agent/llm/router.py`) routes individual prompts to different LLM providers:

1. **Static overrides** — config-driven map of prompt name to provider (e.g., always route `analysis_report` to Claude)
2. **Escalation flags** — the agent can request escalation mid-cycle for complex reasoning, or deterministic rules can trigger it
3. **Default provider** — all unmatched prompts go to the default (typically GPT-OSS 120B)

**Token budget:** A rolling 24h budget (`shared/token_budget.py`) tracks escalation provider usage in a Redis sorted set. When the budget is exceeded, escalation is disabled and all prompts fall back to the default provider. Daily totals are archived to TimescaleDB.

**Integration point:** The router sits between the prompt assembler and `LLMClient`. `LLMClient.complete()` consults the router to select the provider for each call.

---

## 16. Auth Middleware

JWT authentication (`ui/auth.py`, `ui/middleware.py`) with 3 roles:

| Role | Permissions |
|------|-------------|
| admin | read, write, delete, admin (user management) |
| analyst | read, write |
| viewer | read only |

**Architecture:**
- `auth.py` — HMAC-SHA256 JWT implementation (no external dependency), user CRUD against Postgres `users` table, password hashing with PBKDF2
- `middleware.py` — Starlette middleware that intercepts `/api/` routes, validates JWT from HttpOnly cookie, injects user context into request state
- `routes/auth.py` — login/logout/me endpoints at `/api/v2/auth/*`
- `responses.py` — standardized API error envelope

**Backward compatibility:** Auth is disabled by default (`AUTH_ENABLED=false`). When disabled, the middleware passes all requests through. Enabling auth requires setting `AUTH_ENABLED=true` and optionally `AUTH_SECRET_KEY` and `AUTH_DEFAULT_PASSWORD`.

---

## 17. Cognitive Architecture

### Three-Layer Model

Legba's processing is organized into three layers, analogous to levels of cognitive awareness:

| Layer | Service | LLM | Purpose |
|-------|---------|-----|---------|
| **Unconscious** | Maintenance daemon | None | Deterministic housekeeping: lifecycle decay, entity GC, fact expiration, corroboration scoring, integrity verification, adversarial detection, calibration tracking. Runs on a tick-based scheduler (default 60s). |
| **Subconscious** | Subconscious service | SLM (Llama 3.1 8B) | Continuous validation and enrichment: signal quality assessment, entity resolution, classification refinement, fact corroboration, graph consistency, relationship validation. Runs three concurrent async loops (NATS consumer, timer, differential accumulator). |
| **Conscious** | Agent cycle | Primary LLM (GPT-OSS 120B / Claude) | Deliberate analytical work: planning, reasoning, tool use, reflection, situation briefs, hypothesis evaluation. Runs discrete cycles with full context assembly and tool loops. |

The layers operate independently and concurrently. The unconscious layer requires no LLM at all — it is purely rule-based SQL and heuristics. The subconscious layer uses a small, fast, cheap model for tasks that benefit from language understanding but do not require the full reasoning capability of the primary LLM. The conscious layer uses the full primary model for complex analytical work.

### Information Flow Between Layers

The three layers communicate through shared data stores, not direct API calls:

- **Unconscious -> Conscious**: The maintenance daemon writes lifecycle transitions, corroboration scores, and integrity metrics to Postgres and TimescaleDB. The agent reads these during ORIENT.
- **Subconscious -> Conscious**: The subconscious service writes a differential summary to Redis (`legba:subconscious:differential`) capturing state changes between conscious cycles. The agent reads and clears this at the start of each cycle.
- **Conscious -> Subconscious**: The agent's actions (creating events, storing facts, resolving entities) naturally create work for the subconscious via NATS triggers.
- **Unconscious -> Subconscious**: The maintenance daemon flags adversarial signals and integrity issues that the subconscious service can pick up for SLM-powered validation.

### Maintenance Daemon (Unconscious Layer)

Nine scheduled tasks, each on its own modulo interval:

| Task | Default Interval | Purpose |
|------|-----------------|---------|
| Lifecycle decay | 5 min | Event lifecycle state machine transitions, situation dormancy |
| Corroboration scoring | 10 min | Count independent sources per event, update corroboration scores |
| Metric collection | 5 min | Extended operational metrics to TimescaleDB for Grafana |
| Situation detection | 30 min | Propose new situations from event clusters (3+ events, shared region/category/entities) |
| Adversarial detection | 30 min | Source velocity spikes, semantic echo detection, provenance grouping |
| Entity GC | 60 min | Mark dormant entities, detect duplicates, clean orphan graph edges, source health |
| Fact decay | 60 min | Expire facts past `valid_until`, apply confidence decay to stale facts |
| Calibration tracking | 60 min | Record claimed confidence vs actual outcomes for systematic bias detection |
| Integrity verification | 12 hr | Evidence chain verification, eval rubrics (event dedup rate, graph quality, source health) |

### Subconscious Service (Subconscious Layer)

Three concurrent async loops:

1. **NATS consumer** — Triggered work items from other services (signal validation, entity resolution, relationship validation). Listens on `legba.subconscious.*` subjects.
2. **Timer loop** — Periodic tasks on modulo schedule: signal validation (15 min), entity resolution (30 min), classification refinement (30 min), fact refresh (60 min), graph consistency (daily), source reliability recalc (daily).
3. **Differential accumulator** — Tracks state changes between conscious agent cycles. Writes a JSON summary to Redis every 5 minutes capturing new signals per situation, event lifecycle transitions, entity anomalies, fact changes, hypothesis evidence changes, and watchlist matches.

The SLM provider supports both vLLM (OpenAI-compatible, with `guided_json` for constrained decoding) and Anthropic (with `tool_use` for structured output). Default model: Llama 3.1 8B Instruct at temperature 0.1 for deterministic validation.

---

## 18. Planning Layer

Goals, situations, watchlists, hypotheses, and predictions existed as individual features but operated as islands. The planning layer ties them into a coherent loop:

```
DETECT → ESCALATE → DEDUPLICATE → PLAN → EXECUTE → EVALUATE → ADJUST
  │                                                               │
  └───────────────────────────────────────────────────────────────┘
```

### Goal Types

| Type | Persistence | Created By | Purpose |
|------|-------------|-----------|---------|
| **Standing** | Persistent until retired | Human / seed / EVOLVE | Weights analytical priority. "Maintain SA on Iran energy infrastructure" — doesn't decompose into tasks, but an unlinked Iran energy event scores higher in SURVEY task selection. |
| **Investigative** | Time-bound, attached to hypothesis/situation | Agent (SURVEY/ANALYSIS escalation) | Decomposes into concrete tasks. "Investigate whether Iran is deliberately curtailing oil exports" — creates research tasks, watchlists, hypothesis evaluations. Completes when the hypothesis resolves. |

### Escalation Scoring

A pure function (`shared/escalation.py`) that scores novel event clusters for portfolio promotion. Inputs: event count, entity overlap with existing portfolio, severity distribution, region novelty, domain coverage gap. Returns a score (0-1) and a recommendation: `ignore`, `monitor`, `situation`, `situation_and_watchlist`, or `full_portfolio` (goal + watchlist + hypothesis + research tasks).

Runs in the maintenance daemon when automated situation detection finds a candidate. Also callable by the agent during SURVEY/ANALYSIS when it notices a novel pattern.

### Task Backlog

A persistent priority queue (Redis sorted set, `shared/task_backlog.py`) that bridges goals to cycle execution. Nine task types:

| Task Type | Target Cycle |
|-----------|-------------|
| `research_entity` | RESEARCH |
| `evaluate_hypothesis` | SURVEY / ANALYSIS |
| `deep_dive_situation` | SYNTHESIZE |
| `create_watchlist` | SURVEY |
| `link_events` | CURATE |
| `resolve_contradiction` | SURVEY / ANALYSIS |
| `review_proposed_edges` | SURVEY |
| `coverage_gap` | SURVEY / RESEARCH |
| `stale_goal_review` | EVOLVE |

Each cycle type checks the backlog for matching tasks. If a matching task exists, it's injected into the cycle's context as a focused directive. If no tasks match, the cycle runs its normal heuristic selection. Goal alignment amplifies task priority but never constrains it — the agent can always pivot.

### Reactive State Propagation

Five rules in the maintenance daemon (`maintenance/propagation.py`) that cascade state changes across the portfolio:

| Trigger | Effect |
|---------|--------|
| Watchlist fires (new trigger) | Link event to watchlist's parent situation. Update goal progress if linked. |
| Hypothesis evidence shifts | Update parent situation severity. If balance crosses threshold, flag for SYNTHESIZE. |
| Situation severity escalates | If no goal covers it, create escalation candidate. |
| Event reaches ACTIVE lifecycle | If linked to a situation with an investigative goal, add research tasks for event entities. |
| Goal stale (no progress in 50 cycles) | Flag for EVOLVE review. |

### Portfolio Review (EVOLVE cycle)

EVOLVE receives a structured portfolio view (`shared/portfolio.py`) with seven sections: active goals + progress, situations ranked by goal linkage, hypothesis health (evidence accumulation rate), watchlist effectiveness (trigger rate), prediction track record, coverage gaps (regions/domains with events but no analytical coverage), and task backlog summary. EVOLVE can retire goals, promote situations to goals, adjust priority, and flag imbalances.

### Goal-Weighted Cycle Routing

Tier 3 dynamic scoring is extended with goal alignment:

```
base_score  = normal_heuristic_score  (CURATE backlog, SURVEY baseline, etc.)
goal_weight = sum(goal.priority for aligned goals) / 10
final_score = base_score * (1 + goal_weight)
```

Goals amplify, not constrain. A RESEARCH cycle for an entity linked to a high-priority goal scores higher than one for a random low-completeness entity.
