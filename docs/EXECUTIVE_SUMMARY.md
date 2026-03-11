# Legba — Executive Summary

## What It Is

Legba is a continuously operating autonomous AI agent. It runs 24/7, independently deciding what to do each cycle — ingesting data from global sources, building and maintaining a knowledge graph, producing analytical products, and refining its own approach over time. It is not a chatbot, not a pipeline, and not a task runner. It is a persistent intelligence system that operates with minimal human intervention.

The name comes from Papa Legba, the Vodou loa of crossroads and communication — the intermediary between worlds.

## What It Does

**Current mission: Continuous Global Situational Awareness.**

Legba ingests events from 50+ global data sources (RSS feeds, APIs, wire services), resolves entities, builds a knowledge graph of actors and relationships, detects patterns and anomalies, tracks evolving situations, and produces structured briefings. It covers conflict, political, economic, health, environmental, technology, and disaster domains across every region.

In concrete terms, it:

- **Ingests** — Pulls from Al Jazeera, NHK, Crisis Group, GDELT, USGS, WHO, GDACS, and dozens more. 3-tier deduplication (GUID, URL, title, fuzzy) prevents noise accumulation.
- **Resolves** — Maps entity mentions ("Islamic Republic of Iran", "Tehran government", "Iran") to canonical nodes via a 5-stage resolution cascade with alias matching and fuzzy fallback.
- **Graphs** — Maintains a knowledge graph (Apache AGE/Cypher) with 30 canonical relationship types, 70+ normalized aliases. 380+ entities, 850+ relationships, 1,600+ structured facts.
- **Analyzes** — Dedicated analysis cycles run graph mining, pattern detection, and anomaly scoring. Research cycles enrich entity profiles via Wikipedia and reference APIs.
- **Tracks** — Watchlist patterns trigger on matching events. Situations aggregate related events with intensity scoring. Goals drive multi-cycle investigation arcs.
- **Reports** — Produces structured intelligence briefings grounded in tool output. Anti-fabrication rules enforce that every claim traces to data, not LLM confabulation.
- **Journals** — Maintains a personal journal for self-continuity across cycles, with periodic consolidation and lead extraction feeding future investigations.

## Architecture at a Glance

One host VM (Debian 12, 8 vCPU, 16GB RAM) running 10 Docker containers:

| Component | Role |
|-----------|------|
| **Supervisor** | Agent lifecycle — launches one ephemeral container per cycle, heartbeat validation, audit logging, auto-rollback on bad self-modifications |
| **Agent** (ephemeral) | The brain — runs one cycle then exits. 58 tools, 5 cycle types, self-modifiable code volume |
| **Operator UI** | Web console (FastAPI + htmx) — full CRUD on all data, interactive LLM-backed consultation engine |
| **Postgres + AGE** | Structured data + entity graph (Cypher queries) |
| **Redis** | Transient state, counters, journal, reports |
| **Qdrant** | Semantic vector search — episodic memory with time-decay |
| **OpenSearch x2** | Full-text search + isolated audit logs |
| **NATS** | Event bus, operator messaging |
| **Airflow** | Scheduled pipeline orchestration |

**LLM:** Modular provider — runs on GPT-OSS 120B (via vLLM, OpenAI-compatible) or Claude Sonnet (Anthropic API). Same prompts, same tool format, swap with one env var.

## The Cycle

Every 2–10 minutes, the supervisor launches a fresh agent container. The agent runs one of five cycle types selected by priority:

| Cycle | Frequency | Purpose |
|-------|-----------|---------|
| **INTROSPECTION** | Every 15 cycles | Deep self-audit, journal consolidation, world assessment |
| **ANALYSIS** | Every 10 cycles | Pattern detection, graph mining, anomaly scoring |
| **RESEARCH** | Every 5 cycles | Entity enrichment from reference sources and APIs |
| **ACQUIRE** | Every 3 cycles | Dedicated source fetching and event ingestion |
| **NORMAL** | Default | Goal-directed planning and execution |

Each cycle follows: **WAKE** (connect services, register tools) → **ORIENT** (load memories, goals, context) → **[cycle-specific work]** → **REFLECT** (evaluate what was learned) → **NARRATE** (journal) → **PERSIST** (store everything, heartbeat).

The agent has a 20-step tool loop with sliding context window, working memory, and format retry logic. Each specialized cycle type gets a filtered tool set — only relevant tools are available, preventing drift.

## What Makes It Interesting

**It actually runs continuously.** This isn't a demo or a proof-of-concept agent that processes one query. It's been running for 600+ cycles, accumulating knowledge, refining its graph, and improving its coverage. The system is designed around persistence — graceful degradation when services are unavailable, automatic memory promotion, goal auto-completion, and journal-based self-continuity.

**Autonomy, not orchestration.** The agent decides what to investigate, which sources to add, which entities to enrich, and how to structure its knowledge. The supervisor manages lifecycle; the agent manages intelligence. You give it a seed goal and it figures out the rest.

**Grounded intelligence.** Every LLM call is single-turn with explicit context control — no conversation drift, no accumulated hallucination. The REFLECT phase extracts structured facts that go into Postgres, not prose summaries that decay. Anti-fabrication rules in every prompt enforce tool-output grounding.

**Six-layer memory.** Redis for fast transient state, Qdrant for semantic episodic recall, Postgres for structured facts and profiles, Apache AGE for graph relationships, OpenSearch for full-text search, plus a journal archive. Each store serves a different access pattern — "what's relevant now?" vs "what do I know about X?" vs "how are these connected?"

**Self-modifiable code.** The agent's source lives in a Docker volume it can write to. The supervisor validates changes and auto-rolls back on failure. The agent can modify its own prompts, tool implementations, and cycle logic.

**Operator consultation.** The web UI includes an interactive chat backed by its own LLM instance with 31 tools — read and write access to every data layer. Ask it questions, task it with cleanup, or direct investigations through natural language.

**Provider-agnostic.** The same codebase runs on a self-hosted 120B parameter model (GPT-OSS via vLLM) or Anthropic's Claude API. One env var swap. Both instances can run in parallel on shifted ports with separate data stores.

## Tech Stack

Python 3.12 (async), Docker Compose, PostgreSQL 18 + Apache AGE, Qdrant, OpenSearch 2.x, Redis, NATS + JetStream, Apache Airflow, FastAPI + htmx, Pydantic v2, spaCy, NetworkX, feedparser, pycountry, Leaflet.js

## Current Scale

| Metric | Value |
|--------|-------|
| Cycles completed | 600+ |
| Entities tracked | 383 |
| Events stored | 540 |
| Structured facts | 1,595 |
| Graph relationships | 850+ |
| Active sources | 53 |
| Built-in tools | 58 across 17 modules |
| Canonical relationship types | 30 (70+ aliases) |
| LLM context budget | 120k tokens |
| Python source files | 100+ |
| Tests | 241 |
