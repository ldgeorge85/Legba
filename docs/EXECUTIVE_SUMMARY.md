# Legba -- Executive Summary

*Last updated: 2026-03-24*

---

## What It Is

Legba is a continuously operating autonomous AI agent platform for situational awareness. It runs 24/7 with no human in the loop -- an automated ingestion pipeline collects and clusters open-source intelligence from 138 active sources, while an AI analyst runs structured analytical cycles to build a knowledge graph, track evolving situations, stress-test competing hypotheses (ACH), and produce named intelligence products (world assessments, situation briefs, predictions). The operator provides a seed goal and data sources. The agent does everything else.

Legba is not a chatbot or task runner. Collection is deterministic (no LLM), analysis is LLM-driven, and every cycle type has a specific purpose with a restricted tool set. The same codebase supports multiple deployment targets via configuration: geopolitical situational awareness (current mission), privacy/government overreach monitoring, and attack surface management (cybersecurity).

## Architecture

17 Docker containers on a single Debian 12 VM (8 vCPU, 16 GB RAM). Processing is organized into a three-layer cognitive architecture that runs concurrently:

- **Unconscious** (maintenance daemon) -- Deterministic, no LLM. Lifecycle decay, entity garbage collection, corroboration scoring, adversarial detection (velocity spikes, semantic echoes, provenance clusters), confidence calibration, integrity verification. Tick-based scheduler with reactive state propagation.
- **Subconscious** (SLM service) -- Llama 3.1 8B. Signal quality validation, entity resolution, classification refinement, fact corroboration, graph consistency checks. Three concurrent async loops on timed intervals.
- **Conscious** (agent cycle) -- GPT-OSS 120B via vLLM (~42 tps). Planning, reasoning, tool use, reflection, situation briefs, hypothesis evaluation. Discrete cycles with full context assembly and a 128k-token window.

A planning layer ties goals, situations, watchlists, and hypotheses into a detect-escalate-plan-execute loop. Standing goals weight analytical priority; investigative goals decompose into typed tasks that feed the cycle router. Reactive propagation ensures state changes cascade across the portfolio.

**Storage:** Postgres/AGE (structured + graph), Qdrant (semantic/episodic vector search), OpenSearch x2 (full-text + audit), Redis (transient state), TimescaleDB (time-series metrics + HDX conflict baselines). **Orchestration:** NATS (event bus), Airflow (4 DAGs: metrics rollup, source health, decision surfacing, eval rubrics), Grafana (operational dashboards).

## Fusion Architecture

Processing maps onto a multi-level data fusion model (adapted from the JDL framework), with each cognitive layer contributing at specific levels:

| Fusion Level | Purpose | Cognitive Layer |
|---|---|---|
| L0 -- Source | Signal ingestion, normalization, dedup | Ingestion (deterministic) |
| L1 -- Object | Entity resolution, profiling, enrichment | Subconscious (SLM) + Conscious (RESEARCH) |
| L2 -- Situation | Event clustering, situation tracking, pattern detection | Unconscious (maintenance) + Conscious (CURATE, ANALYSIS) |
| L3 -- Impact | Hypothesis evaluation, predictions, threat assessment | Conscious (SYNTHESIZE, ANALYSIS) |
| L4 -- Process | Calibration, self-improvement, source health | Unconscious (calibration) + Conscious (EVOLVE) |
| L5 -- Operator | Consultation engine, briefings, world assessments | Conscious (INTROSPECTION) + UI |

The three cognitive layers (unconscious, subconscious, conscious) and the six fusion levels form a matrix where each cell represents a specific class of analytical work. This structure ensures that raw signals are systematically refined into actionable intelligence, with each level building on the outputs below it and feeding back corrections downward.

## Agent Cycle

Every cycle (~2-10 minutes), the agent runs one of 7 cycle types selected by 3-tier priority routing:

| Tier | Cycle Type | Frequency | Purpose |
|------|------------|-----------|---------|
| 1 (scheduled) | EVOLVE | Every 30 cycles | Self-improvement, source discovery, operational scorecard, portfolio review |
| 1 (scheduled) | INTROSPECTION | Every 15 cycles | Deep audit, journal consolidation, world assessment reports |
| 1 (scheduled) | SYNTHESIZE | Every 10 cycles | Deep-dive investigation, named situation briefs, predictions |
| 2 (modulo) | ANALYSIS | Every 4 cycles | Pattern detection, graph mining, anomaly detection, hypothesis evaluation |
| 2 (modulo) | RESEARCH | Every 7 cycles | Entity enrichment from Wikipedia and reference sources |
| 2 (modulo) | CURATE | Every 9 cycles | Event curation from clustered signals, severity assignment, entity linking |
| 3 (dynamic) | CURATE or SURVEY | Fill cycles | Scored by uncurated backlog vs analytical desk work |

Each cycle follows a fixed phase sequence: WAKE (connect, register tools, drain inbox) -> ORIENT (retrieve memories, goals, health checks, journal leads) -> PLAN -> REASON+ACT (tool loop, up to 20 steps) -> REFLECT -> NARRATE (journal entries + investigation leads) -> PERSIST (store episode, track metrics, heartbeat).

## Key Numbers

| Metric | Value |
|--------|-------|
| Signals ingested | ~30,500 |
| Derived events | ~1,100 |
| Active facts | ~13,400 |
| Entities | ~598 |
| Active sources | 138 (all categorized) |
| Built-in tools | 66 across 19 modules |
| Containers | 17 |
| Cognitive layers | 3 (unconscious, subconscious, conscious) |
| Memory layers | 6 (registers, short-term episodic, long-term episodic, structured, graph, bulk) |
| Canonical relationship types | 30 |
| Python source files | 176 |
| Tests | 200+ |
| UI panels | 22+ |

## Data Pipeline

```
Sources (138 feeds: RSS, APIs, weather alerts, conflict data)
    |
Signal Ingestion (deterministic, no LLM)
  Fetch -> Normalize -> Classify -> NER -> Embed -> 4-tier Dedup -> Store
  (Postgres + OpenSearch + Qdrant)
    |
Clustering (every 20 min)
  Entity overlap + title similarity + temporal proximity + category match
  Signals -> Events (many-to-many), auto-links to situations
    |
Subconscious Validation (SLM, continuous)
  Signal QA, entity resolution, classification refinement, fact corroboration
    |
Unconscious Maintenance (daemon, continuous)
  Lifecycle decay, entity GC, corroboration, adversarial detection, calibration
    |
Agent Curation (CURATE cycles)
  Promote singletons, refine auto-events, set severity, link entities, create situations
    |
Analysis (ANALYSIS + RESEARCH + SURVEY cycles)
  Pattern detection, anomaly flagging, temporal trends, graph mining, entity enrichment
    |
Intelligence Products (SYNTHESIZE + INTROSPECTION cycles)
  Situation briefs, world assessments, hypothesis evaluations, predictions
```

## Current Deployment

**Mission:** Continuous Global Situational Awareness -- monitoring geopolitical, conflict, health, environmental, and economic developments worldwide.

**Primary LLM:** GPT-OSS 120B via vLLM (self-hosted, ~42 tps, ~5.3 cycles/hour). **SLM:** Llama 3.1 8B Q5_K_M via llama.cpp (~40 tps). **Alternative:** Claude Sonnet via Anthropic API (tested, viable for enterprise budgets).

**Operator interface:** 22+ panel React intelligence workstation (Dockview layout) with knowledge graph explorer, geospatial map, timeline, live signal feed, derived events, AI consultation, world assessment reports, hypothesis tracker (ACH), situation briefs, and analytics dashboards.

## Documentation

| Document | Description |
|----------|-------------|
| [LEGBA.md](LEGBA.md) | Full platform reference -- architecture, prompts, memory, tools, config |
| [DESIGN.md](DESIGN.md) | Implementation design -- decisions, data flows, component interactions |
| [CODE_MAP.md](CODE_MAP.md) | Complete code map -- every file, function flows, dependencies |
| [OPERATIONS.md](OPERATIONS.md) | Ops runbook -- deployment, resets, monitoring, debugging, backups |
| [UI_V2.md](UI_V2.md) | UI v2 operator console -- panels, API, deployment |
| [DATA_SOURCES.md](DATA_SOURCES.md) | Global data source catalog |
| [PROMPT_DUMP.md](PROMPT_DUMP.md) | Full assembled prompts for each cycle phase |
