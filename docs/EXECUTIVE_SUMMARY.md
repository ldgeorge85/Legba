# Legba — Executive Summary

*Last updated: 2026-03-17*

---

## What It Is

Legba is a continuously operating autonomous AI agent for situational awareness. It runs 24/7 with no human in the loop — ingesting open-source intelligence, building a knowledge graph, producing analytical reports, and improving its own capabilities over time.

The operator provides a seed goal. The agent does everything else.

## How It Works

**Two-tier data model.** Raw source material (RSS feeds, API responses, weather alerts, conflict data) is ingested as **signals**. Signals are automatically deduplicated, embedded, and clustered into **events** — real-world occurrences that multiple signals describe. The agent curates events, sets severity, links entities, and produces intelligence products.

**Six cycle types.** The agent runs continuously in cycles (~5/hour), each cycle one of:

| Cycle | Frequency | Purpose |
|-------|-----------|---------|
| CURATE | 27% | Turn raw signals into curated events. Editorial judgment. |
| NORMAL | 60% | Goal-directed work — research, relationship building, analysis |
| ANALYSIS | 7% | Pattern detection, anomaly flagging, graph mining |
| RESEARCH | 3% | Entity enrichment from external sources |
| INTROSPECTION | 3% | World assessment reports, knowledge audit, scorecard |
| EVOLVE | 3% | Self-improvement, source discovery, operational review |

**63 tools** across memory, graph, search, HTTP, analytics, entity management, situation tracking, and self-modification.

## Key Numbers

| Metric | Value |
|--------|-------|
| Signals ingested | ~17,000+ |
| Derived events | ~470+ |
| Entity profiles | 507 |
| Graph relationships | 1,360 |
| Active facts | ~1,700 |
| Active sources | 116 |
| Tracked situations | 7 |
| Watchlist patterns | 5 |
| Docker containers | 12 |
| Built-in tools | 63 |
| Passing tests | 122 |

## Infrastructure

12 Docker containers on a single Debian 12 VM (8 vCPU, 16GB RAM):

- **LLM**: GPT-OSS 120B via vLLM (self-hosted, ~42 tps) or Claude via Anthropic API
- **Embeddings**: embedding-inno1 (1024 dims) on vLLM — signals embedded at ingestion
- **Storage**: Postgres/AGE (structured + graph), Qdrant (vector search), OpenSearch (full-text), Redis (transient state)
- **Ingestion**: Independent service — fetches 116 sources, 4-tier dedup (GUID → URL → vector cosine → Jaccard), deterministic clustering, NWS alert normalization
- **UI**: React multi-panel workstation with graph explorer, geo map, timeline, event/signal browsers, dashboard

## Signal-to-Intelligence Pipeline

```
Sources (116 feeds)
    ↓
Signal Ingestion (deterministic, no LLM)
  - Fetch → Normalize → Embed → Dedup → Store (Postgres + OpenSearch + Qdrant)
    ↓
Clustering (every 20 min)
  - Entity overlap + title similarity + temporal proximity + category match
  - Signals → Events (many-to-many), auto-links to situations
    ↓
Agent Curation (CURATE cycles, 27% of runtime)
  - Promote singletons, refine auto-events, set severity, link entities
    ↓
Analysis (ANALYSIS + INTROSPECTION cycles)
  - Pattern detection, anomaly flagging, temporal trends, graph mining
    ↓
Intelligence Products
  - World assessment reports (every 15 cycles)
  - Knowledge graph (entities + typed relationships)
  - Situation tracking (7 active narratives)
  - Watchlist alerting (5 active patterns)
```

## What Makes It Different

- **Autonomous.** No human prompts. The agent sets its own priorities, discovers sources, and decides what to investigate.
- **Continuous.** Memory persists across cycles. The agent builds on what it learned yesterday.
- **Two-tier data.** Signals (raw noise) are separated from events (curated intelligence). The agent operates on events, not raw feeds.
- **Self-improving.** Can modify its own prompts, tools, and normalization rules. Changes are git-tracked with auto-rollback on failure.
- **Reasoning over knowledge.** The LLM is used for judgment and synthesis, not as a knowledge base. All facts come from live data, not training memory.

## Deployment Targets

Same codebase, different configurations:

1. **Geopolitical situational awareness** (current mission)
2. **Privacy and government overreach monitoring**
3. **Attack surface management** (cybersecurity)

## Documentation

| Document | Purpose |
|----------|---------|
| [LEGBA.md](LEGBA.md) | Full platform reference |
| [DESIGN.md](DESIGN.md) | Architecture and design decisions |
| [CODE_MAP.md](CODE_MAP.md) | Source code structure and file guide |
| [OPERATIONS.md](OPERATIONS.md) | Deployment, monitoring, maintenance |
| [DATA_SOURCES.md](DATA_SOURCES.md) | Source catalog and API reference |
| [UI_V2.md](UI_V2.md) | React UI panel documentation |
