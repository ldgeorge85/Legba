# Legba -- Cycle Architecture

*How the agent thinks, one cycle at a time.*

---

## Overview

A **cycle** is one complete execution of the agent. The supervisor launches a
fresh container for each cycle, the agent runs through a deterministic phase
sequence, emits a heartbeat, and exits. State persists across cycles in
external stores (Postgres, Redis, Qdrant, OpenSearch) -- the agent process
itself is stateless and ephemeral.

```
Supervisor
  |
  +-- launches container (cycle N)
  |     |
  |     +-- WAKE -> ORIENT -> [type-specific phases] -> REFLECT -> NARRATE -> PERSIST
  |     |
  |     +-- heartbeat + exit
  |
  +-- launches container (cycle N+1)
  |     ...
```

Changes to prompts, tools, or configuration take effect on the next cycle
automatically -- there is no reload mechanism because there is nothing to
reload.

---

## Cycle Type Routing (3-Tier)

Every cycle gets a **type** determined by 3-tier routing. Higher tiers take
priority; within a tier, types are evaluated top to bottom.

### Tier 1 -- Scheduled Outputs (fixed intervals)

Non-negotiable deliverables on a predictable cadence.

| Priority | Type | Interval | Trigger | Purpose |
|----------|------|----------|---------|---------|
| 1 | **EVOLVE** | 30 | `N % 30 == 0` | Self-improvement: audit prompts, tools, workflows |
| 2 | **INTROSPECTION** | 15 | `N % 15 == 0` | Deep knowledge audit, world assessment, journal consolidation |
| 3 | **SYNTHESIZE** | 10 | `N % 10 == 0` | Deep-dive investigation into a single situation |

### Tier 2 -- Guaranteed Work (coprime modulo intervals)

Fire on their interval unless a Tier 1 type already claimed the slot.

| Priority | Type | Interval | Trigger | Purpose |
|----------|------|----------|---------|---------|
| 4 | **ANALYSIS** | 4 | `N % 4 == 0` | Pattern detection, anomaly detection, graph mining |
| 5 | **RESEARCH** | 7 | `N % 7 == 0` | Entity enrichment via external sources |
| 6 | **CURATE** | 9 | `N % 9 == 0` | Signal triage, event creation, entity linking |

Intervals 4, 7, 9 are coprime -- they rarely mask each other.

### Tier 3 -- Dynamic Fill (state-scored)

Cycles not claimed by Tier 1 or Tier 2. Two candidates compete:

| Type | Score | Condition |
|------|-------|-----------|
| CURATE | `min(uncurated / 80, 0.6)` | Only if uncurated > 30 signals |
| SURVEY | 0.4 (fixed) | Always available -- analytical desk work |

Cooldown: the previous dynamic type gets its score halved (no back-to-back repeats).

### CURATE vs ACQUIRE Fallback

When a CURATE slot fires, it checks `_ingestion_service_active()`:
- If true (env `INGESTION_SERVICE_ACTIVE=true` or Redis heartbeat detected): runs `_curate()` -- editorial judgment on clustered signals
- If false: runs `_acquire()` -- legacy source fetching (pre-ingestion-service path)

### INTROSPECTION Interval

INTROSPECTION uses `config.agent.mission_review_interval` (default 15,
env `AGENT_MISSION_REVIEW_INTERVAL`). All other intervals are constants in
`phases/__init__.py`.

---

## Phase Descriptions

Every cycle executes a subset of these 15 phases:

| # | Phase | Module | What It Does |
|---|-------|--------|-------------|
| 1 | **WAKE** | `wake.py` | Connect all services (Postgres, Redis, Qdrant, OpenSearch, NATS), register tools, load seed goal, increment cycle counter, drain inbox |
| 2 | **ORIENT** | `orient.py` | Semantic memory retrieval, load active goals + facts, infra health check, graph inventory, source health, journal context + investigation leads, uncurated count |
| 3 | **PLAN** | `plan.py` | LLM produces a prose plan + expected tool list from context. SURVEY-only (other types have hardcoded plans) |
| 4 | **ACT** | `act.py` | Tool-calling loop (up to 20 steps, max 4 concurrent). Sliding-window history. SURVEY-only |
| 5 | **SURVEY** | `survey.py` | Analytical desk work: situation updates, graph building, hypothesis evaluation, lead-following. Rate-limited external access (max 2 HTTP requests) |
| 6 | **CURATE** | `curate.py` | Editorial judgment: promote signals to events, refine auto-events, link entities to situations |
| 7 | **RESEARCH** | `research.py` | Entity enrichment: fetch external data for low-completeness entities, fill profile gaps |
| 8 | **ANALYZE** | `analyze.py` | Analytics: centrality, community detection, anomaly detection, co-occurrence correlation, differential reporting |
| 9 | **SYNTHESIZE** | `synthesize.py` | Deep-dive: pick one situation, build narrative, generate falsifiable predictions. Produces a Situation Brief |
| 10 | **INTROSPECT** | `introspect.py` | Mission review: graph gap analysis, isolated node discovery, operator scorecard, world assessment report |
| 11 | **EVOLVE** | `evolve.py` | Self-improvement: audit own prompts, tools, source utilization, coverage gaps. Can read/modify its own code |
| 12 | **REFLECT** | `reflect.py` | Structured extraction: LLM produces JSON with significance, entities, goal progress (fact extraction disabled -- facts come from ingestion pipeline) |
| 13 | **NARRATE** | `narrate.py` | Journal entries (1-3 per cycle) + investigation leads extraction. Archival to OpenSearch |
| 14 | **CONSOLIDATION** | `introspect.py` | Journal consolidation: compress recent journal into summary. INTROSPECTION and EVOLVE cycles only |
| 15 | **PERSIST** | `persist.py` | Save episode to Qdrant, track ingestion, auto-complete goals, publish outbox, sanity checks, liveness check, emit heartbeat, write metrics to TimescaleDB |

---

## Phase Sequence Per Cycle Type

```
ALL cycles start:   WAKE --> ORIENT --> ...

SURVEY:        ... --> SURVEY ---------> REFLECT --> NARRATE --> PERSIST
CURATE:        ... --> CURATE ---------> REFLECT --> NARRATE --> PERSIST
RESEARCH:      ... --> RESEARCH -------> REFLECT --> NARRATE --> PERSIST
ANALYSIS:      ... --> ANALYZE --------> REFLECT --> NARRATE --> PERSIST
SYNTHESIZE:    ... --> SYNTHESIZE -----> REFLECT --> NARRATE --> PERSIST
INTROSPECTION: ... --> MISSION_REVIEW -> REFLECT --> NARRATE --> CONSOLIDATION --> REPORT --> PERSIST
EVOLVE:        ... --> EVOLVE ---------> REFLECT --> NARRATE --> CONSOLIDATION --> REPORT --> PERSIST
```

INTROSPECTION and EVOLVE add two extra phases at the end: journal
consolidation (compressing recent journal entries) and an analysis report
(world assessment with scorecard, novelty, source quality).

When EVOLVE fires at cycle 30, it overlaps with INTROSPECTION (30 % 15 == 0).
EVOLVE wins by priority but still runs the consolidation and report phases
so the INTROSPECTION work is not lost.

---

## 30-Cycle Layout

Full routing table for cycles 1-30 under the 3-tier design.

| Cycle | Type | Why |
|------:|------|-----|
| 1 | SURVEY/dynamic | No tier match |
| 2 | SURVEY/dynamic | |
| 3 | SURVEY/dynamic | |
| 4 | **ANALYSIS** | 4%4=0 (Tier 2) |
| 5 | SURVEY/dynamic | |
| 6 | SURVEY/dynamic | |
| 7 | **RESEARCH** | 7%7=0 (Tier 2) |
| 8 | **ANALYSIS** | 8%4=0 (Tier 2) |
| 9 | **CURATE** | 9%9=0 (Tier 2) |
| 10 | **SYNTHESIZE** | 10%10=0 (Tier 1, masks ANALYSIS) |
| 11 | SURVEY/dynamic | |
| 12 | **ANALYSIS** | 12%4=0 (Tier 2) |
| 13 | SURVEY/dynamic | |
| 14 | **RESEARCH** | 14%7=0 (Tier 2) |
| 15 | **INTROSPECTION** | 15%15=0 (Tier 1) |
| 16 | **ANALYSIS** | 16%4=0 (Tier 2) |
| 17 | SURVEY/dynamic | |
| 18 | **CURATE** | 18%9=0 (Tier 2) |
| 19 | SURVEY/dynamic | |
| 20 | **SYNTHESIZE** | 20%10=0 (Tier 1, masks ANALYSIS) |
| 21 | **RESEARCH** | 21%7=0 (Tier 2) |
| 22 | SURVEY/dynamic | |
| 23 | SURVEY/dynamic | |
| 24 | **ANALYSIS** | 24%4=0 (Tier 2) |
| 25 | SURVEY/dynamic | |
| 26 | SURVEY/dynamic | |
| 27 | **CURATE** | 27%9=0 (Tier 2) |
| 28 | **ANALYSIS** | 28%4=0 (Tier 2, masks RESEARCH at 28%7=0) |
| 29 | SURVEY/dynamic | |
| 30 | **EVOLVE** | 30%30=0 (Tier 1, top priority; also runs consolidation + report) |

---

## Distribution Summary

Counts per 90-cycle window (LCM of 4,7,9,10,15,30):

| Type | Cycles | % | Role |
|------|-------:|--:|------|
| SURVEY/dynamic | 47 | 52% | Analytical desk work (+ dynamic CURATE when backlog builds) |
| ANALYSIS | 18 | 20% | Pattern detection, anomaly detection |
| RESEARCH | 8 | 9% | Entity enrichment |
| SYNTHESIZE | 6 | 7% | Deep-dive briefs |
| CURATE | 5 | 6% | Guaranteed curation slots (+ Tier 3 promotions) |
| INTROSPECTION | 3 | 3% | World assessment reports |
| EVOLVE | 3 | 3% | Self-improvement |
| **Total** | **90** | **100%** | |

ANALYSIS at 20% is intentional -- pattern detection and graph mining benefit
from running often on fresh data. SURVEY is the default analytical cycle,
replacing the old NORMAL type when the ingestion service took over data
collection.

---

## Key Mechanisms

### CYCLE_TYPE Worker Mode

Setting the `CYCLE_TYPE` environment variable bypasses interval routing
entirely and forces a specific cycle type. This enables running parallel
specialist workers:

```
CYCLE_TYPE=research   # Only runs research cycles
CYCLE_TYPE=curate     # Only runs curate cycles
CYCLE_TYPE=analysis   # Only runs analysis cycles
```

Valid values: `evolve`, `introspection`, `synthesize`, `analysis`, `research`,
`curate`, `survey`. The worker still executes the full phase sequence for that
type (WAKE -> ORIENT -> [type] -> REFLECT -> NARRATE -> PERSIST).

Worker mode includes a hard CURATE promotion: if `CYCLE_TYPE=survey` and
uncurated backlog > 100, the cycle promotes to CURATE automatically.

### Hybrid LLM Routing (Dormant)

The architecture supports routing different cycle types to different LLM
providers. The intended design:

- **GPT-OSS 120B** (self-hosted vLLM): SURVEY, CURATE, RESEARCH -- bulk
  analytical work at ~42 tokens/sec, zero marginal cost
- **Claude** (Anthropic API): INTROSPECTION, EVOLVE, ANALYSIS -- higher-value
  reasoning at ~$900/day (not viable for personal use, suitable for enterprise)

This is wired in `LLMClient` but currently dormant -- all cycles use the
self-hosted model.

### Graceful Shutdown

The supervisor signals the agent to stop by writing `stop_flag.json` to the
shared volume. The agent checks for this flag at phase boundaries and, if
found, acknowledges with `stop_ping.json` and completes the current phase
before exiting.

```
Supervisor                    Agent
    |                           |
    +-- write stop_flag.json -->|
    |                           +-- detect flag
    |                           +-- write stop_ping.json
    |<-- ping acknowledgment ---+
    |                           +-- finish current phase
    |                           +-- exit cleanly
    +-- wait (extended timeout)
```

### Tool Restriction Per Cycle Type

Each cycle type has its own tool allowlist. Tools not on the list are rejected
at execution time with a descriptive error. This prevents drift -- e.g.,
SURVEY cycles cannot parse feeds, INTROSPECTION cycles cannot make HTTP
requests, EVOLVE cycles cannot create events.

| Type | Tool Count | Key Capabilities |
|------|-----------|-----------------|
| SURVEY | 30 | Graph, memory, events, situations, hypotheses, limited HTTP (2/cycle) |
| CURATE | 17 | Signals, events, entity linking, situations |
| RESEARCH | 15 | HTTP, graph, memory, entities, events |
| ANALYSIS | 23 | Graph analysis, anomalies, temporal queries, situations, hypotheses |
| SYNTHESIZE | 29 | Full analytics, situations, predictions, hypotheses, HTTP |
| INTROSPECTION | 18 | Internal queries only: graph, memory, entities, predictions |
| EVOLVE | 14 | Filesystem read/write, code test, graph, memory, sources |

---

## Timing

| Metric | Value |
|--------|-------|
| Average cycle duration | ~5-12 minutes |
| Cycles per hour | ~5.3 |
| 90-cycle window | ~17 hours |
| LLM throughput (GPT-OSS 120B) | ~42 tokens/sec |
| Max reasoning steps per cycle | 20 |
| Max concurrent tool calls | 4 |

The bottleneck is LLM inference speed. The architecture itself -- Postgres,
Redis, Qdrant, OpenSearch -- adds negligible latency.

---

## Outputs Per Cycle Type

What each cycle type produces:

| Type | Primary Outputs |
|------|----------------|
| **SURVEY** | Situation updates, new graph edges, hypothesis evaluations, watchlist triggers |
| **CURATE** | Promoted events (signal -> event), refined auto-events, entity-situation links |
| **RESEARCH** | Enriched entity profiles, new graph edges from external data, filled knowledge gaps |
| **ANALYSIS** | Centrality rankings, community clusters, anomaly alerts, temporal correlations, differential reports |
| **SYNTHESIZE** | Situation Briefs (named deliverable), falsifiable predictions, narrative timelines. Tracks recently investigated threads to prevent rabbit-holing |
| **INTROSPECTION** | World Assessment report (scorecard, novelty, source quality, proposed edges), journal consolidation, graph gap analysis |
| **EVOLVE** | Self-modification proposals, prompt edits, tool evaluations, operational scorecard, also runs consolidation + report |

All cycle types also produce:
- **Journal entries** (from NARRATE): 1-3 per cycle, archived to OpenSearch
- **Episode** (from PERSIST): full cycle record stored in Qdrant
- **Heartbeat** (from PERSIST): liveness signal to the supervisor
- **Metrics** (from PERSIST): cycle duration, action count, success flag to TimescaleDB

---

## Source Files

| File | Role |
|------|------|
| `src/legba/agent/cycle.py` | Orchestrator: 3-tier routing, worker mode, lifecycle |
| `src/legba/agent/phases/__init__.py` | Interval constants (Tier 1: 10,15,30; Tier 2: 4,7,9) |
| `src/legba/agent/phases/*.py` | Phase mixin implementations |
| `src/legba/agent/prompt/templates.py` | Tool allowlists and prompts per cycle type |
| `src/legba/shared/config.py` | `mission_review_interval` and other tunables |
