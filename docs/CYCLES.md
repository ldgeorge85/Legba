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

## Cycle Type Routing

Every cycle gets a **type** based on its cycle number. Types are evaluated in
strict priority order; when intervals overlap, the highest-priority type wins.

| Priority | Type | Interval | Trigger | Purpose |
|----------|------|----------|---------|---------|
| 1 | **EVOLVE** | 30 | `N % 30 == 0` | Self-improvement: audit prompts, tools, workflows |
| 2 | **INTROSPECTION** | 15 | `N % 15 == 0` | Deep knowledge audit, world assessment, journal consolidation |
| 3 | **SYNTHESIZE** | 10 | `N % 10 == 0` | Deep-dive investigation into a single situation |
| 4 | **ANALYSIS** | 5 | `N % 5 == 0` | Pattern detection, anomaly detection, graph mining |
| 5 | **RESEARCH** | 7 | `N % 7 == 0` | Entity enrichment via external sources |
| 6 | **CURATE** | 9 | `N % 9 == 0` | Signal triage, event creation, entity linking |
| 7 | **SURVEY** | -- | fallback | Analytical desk work (default) |

### Coprime Design Rationale

The intervals (30, 15, 10, 5, 7, 9) are chosen so that higher-priority types
only rarely collide with each other, distributing analytical work evenly across
cycles. 7 and 9 are coprime to each other and to 5 and 10, so RESEARCH and
CURATE never land on the same cycle. The only overlap is at the LCM boundaries
(e.g., cycle 30 triggers EVOLVE + INTROSPECTION + SYNTHESIZE + ANALYSIS
simultaneously -- EVOLVE wins by priority, but also runs the INTROSPECTION
report and consolidation to avoid skipping that work).

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
| 2 | **ORIENT** | `orient.py` | Semantic memory retrieval, load active goals + facts, infra health check, graph inventory, source health, journal context + investigation leads |
| 3 | **PLAN** | `plan.py` | LLM produces a prose plan + expected tool list from context. SURVEY-only (other types have hardcoded plans) |
| 4 | **ACT** | `act.py` | Tool-calling loop (up to 20 steps, max 4 concurrent). Sliding-window history. SURVEY-only |
| 5 | **SURVEY** | `survey.py` | Analytical desk work: situation updates, graph building, hypothesis evaluation, lead-following. Rate-limited external access (max 2 HTTP requests) |
| 6 | **CURATE** | `curate.py` | Editorial judgment: promote signals to events, refine auto-events, link entities to situations |
| 7 | **RESEARCH** | `research.py` | Entity enrichment: fetch external data for low-completeness entities, fill profile gaps |
| 8 | **ANALYZE** | `analyze.py` | Analytics: centrality, community detection, anomaly detection, co-occurrence correlation, differential reporting |
| 9 | **SYNTHESIZE** | `synthesize.py` | Deep-dive: pick one situation, build narrative, generate falsifiable predictions. Produces a Situation Brief |
| 10 | **INTROSPECT** | `introspect.py` | Mission review: graph gap analysis, isolated node discovery, operator scorecard, world assessment report |
| 11 | **EVOLVE** | `evolve.py` | Self-improvement: audit own prompts, tools, source utilization, coverage gaps. Can read/modify its own code |
| 12 | **REFLECT** | `reflect.py` | Structured extraction: LLM produces JSON with facts, entities, relationships, goal progress |
| 13 | **NARRATE** | `narrate.py` | Journal entries (1-3 per cycle) + investigation leads extraction. Archival to OpenSearch |
| 14 | **CONSOLIDATION** | `introspect.py` | Journal consolidation: compress recent journal into summary. INTROSPECTION and EVOLVE cycles only |
| 15 | **PERSIST** | `persist.py` | Save episode to Qdrant, track ingestion, auto-complete goals, publish outbox, liveness check, emit heartbeat |

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

Full routing table for cycles 1-30. Priority column shows which intervals
matched and which type won.

| Cycle | Type | Why |
|------:|------|-----|
| 1 | SURVEY | No interval matches |
| 2 | SURVEY | |
| 3 | SURVEY | |
| 4 | SURVEY | |
| 5 | **ANALYSIS** | 5%5=0 |
| 6 | SURVEY | |
| 7 | **RESEARCH** | 7%7=0 |
| 8 | SURVEY | |
| 9 | **CURATE** | 9%9=0 |
| 10 | **SYNTHESIZE** | 10%10=0 (beats ANALYSIS at 10%5=0) |
| 11 | SURVEY | |
| 12 | SURVEY | |
| 13 | SURVEY | |
| 14 | **RESEARCH** | 14%7=0 |
| 15 | **INTROSPECTION** | 15%15=0 (beats ANALYSIS at 15%5=0) |
| 16 | SURVEY | |
| 17 | SURVEY | |
| 18 | **CURATE** | 18%9=0 |
| 19 | SURVEY | |
| 20 | **SYNTHESIZE** | 20%10=0 (beats ANALYSIS at 20%5=0) |
| 21 | **RESEARCH** | 21%7=0 |
| 22 | SURVEY | |
| 23 | SURVEY | |
| 24 | SURVEY | |
| 25 | **ANALYSIS** | 25%5=0 |
| 26 | SURVEY | |
| 27 | **CURATE** | 27%9=0 |
| 28 | **RESEARCH** | 28%7=0 |
| 29 | SURVEY | |
| 30 | **EVOLVE** | 30%30=0 (top priority; also runs consolidation + report) |

---

## Distribution Summary

Counts and time allocation per 30-cycle window:

| Type | Cycles | % | Approx. Time |
|------|-------:|--:|-------------|
| SURVEY | 17 | 57% | ~3.1 hours |
| RESEARCH | 4 | 13% | ~44 min |
| CURATE | 3 | 10% | ~33 min |
| SYNTHESIZE | 2 | 7% | ~22 min |
| ANALYSIS | 2 | 7% | ~22 min |
| INTROSPECTION | 1 | 3% | ~11 min |
| EVOLVE | 1 | 3% | ~11 min |
| **Total** | **30** | **100%** | **~5.6 hours** |

SURVEY is the default analytical cycle -- it replaced the old NORMAL type
when the ingestion service took over data collection. The agent now spends
57% of its time on analytical desk work rather than feed parsing.

---

## Key Mechanisms

### Dynamic CURATE Promotion

When the uncurated signal backlog exceeds a threshold (default: 100 signals),
the next SURVEY cycle is automatically promoted to CURATE. This prevents
backlog growth during high-volume news periods without requiring interval
changes.

```python
# phases/__init__.py
CURATE_BACKLOG_THRESHOLD = 100

# cycle.py — inside the SURVEY branch
if self._should_promote_to_curate():
    await self._curate()
else:
    await self._survey()
```

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
| Average cycle duration | ~11 minutes |
| Cycles per hour | ~5.3 |
| 30-cycle window | ~5.6 hours |
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
| **SURVEY** | Situation updates, new graph edges, hypothesis evaluations, watchlist triggers, opportunistic event curation |
| **CURATE** | Promoted events (signal -> event), refined auto-events, entity-situation links |
| **RESEARCH** | Enriched entity profiles, new graph edges from external data, filled knowledge gaps |
| **ANALYSIS** | Centrality rankings, community clusters, anomaly alerts, temporal correlations, differential reports |
| **SYNTHESIZE** | Situation Briefs (named deliverable), falsifiable predictions, narrative timelines. Tracks recently investigated threads to prevent rabbit-holing |
| **INTROSPECTION** | World Assessment report (scorecard, novelty, source quality, proposed edges), journal consolidation, graph gap analysis |
| **EVOLVE** | Self-modification proposals, prompt edits, tool evaluations, operational scorecard, also runs consolidation + report |

All cycle types also produce:
- **Facts** (from REFLECT): structured extractions stored in Postgres
- **Journal entries** (from NARRATE): 1-3 per cycle, archived to OpenSearch
- **Episode** (from PERSIST): full cycle record stored in Qdrant
- **Heartbeat** (from PERSIST): liveness signal to the supervisor

---

## Source Files

| File | Role |
|------|------|
| `src/legba/agent/cycle.py` | Orchestrator: routing, worker mode, lifecycle |
| `src/legba/agent/phases/__init__.py` | Interval constants, backlog threshold |
| `src/legba/agent/phases/*.py` | Phase mixin implementations |
| `src/legba/agent/prompt/templates.py` | Tool allowlists and prompts per cycle type |
| `src/legba/shared/config.py` | `mission_review_interval` and other tunables |
