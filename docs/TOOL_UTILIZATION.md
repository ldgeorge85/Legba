# Tool Utilization Report

*Snapshot: Cycle 117 | ~16 hours runtime | 2026-03-08*

---

## Overview

**50 registered tools** across 15 modules. **22 used** (44%), **28 unused**.

Total tool calls: 3,389 across 117 cycles (avg ~29/cycle).

---

## Usage by Tool (All Cycles)

| Tool | Calls | Category | Notes |
|------|------:|----------|-------|
| `graph_store` | 672 | Graph | Most-used. Heavy graph building |
| `entity_resolve` | 446 | Entity | Core pipeline — links events to entities |
| `graph_query` | 440 | Graph | Audits, relationship lookups, Cypher |
| `event_store` | 371 | Event | Dual Postgres + OpenSearch |
| `feed_parse` | 287 | Feed | RSS parsing from registered sources |
| `source_add` | 221 | Source | Source registration |
| `entity_profile` | 174 | Entity | Profile enrichment with assertions |
| `http_request` | 158 | HTTP | Web fetching (Wikipedia, news sites) |
| `source_list` | 156 | Source | Source inventory checks |
| `source_update` | 138 | Source | Status/metadata updates |
| `note_to_self` | 73 | Inline | Working memory observations |
| `memory_query` | 61 | Memory | Episodic recall |
| `entity_inspect` | 53 | Entity | Profile completeness checks |
| `goal_list` | 36 | Goals | Goal tree review |
| `explain_tool` | 26 | Inline | On-demand tool parameter lookup |
| `goal_update` | 17 | Goals | Progress updates |
| `event_search` | 17 | Event | Full-text search |
| `memory_store` | 11 | Memory | Manual memory storage |
| `nats_publish` | 11 | NATS | Status reports to outbound |
| `goal_create` | 10 | Goals | New sub-goals |
| `nlp_extract` | 10 | Analytics | NER on news text (adopted organically) |
| `spawn_subagent` | 1 | Subagent | Tried once |

---

## Unused Tools (28)

### Analytics (4 remaining)

| Tool | Backend | Why unused | Worth pushing? |
|------|---------|-----------|----------------|
| `anomaly_detect` | PyOD (IForest, LOF, KNN) | Agent hasn't been guided to look for statistical anomalies in event patterns | **Yes** — 72 events with timestamps and categories are enough for spike detection |
| `forecast` | statsforecast (AutoARIMA) | No time series data assembled yet | Not yet — needs structured time series first |
| `graph_analyze` | NetworkX (centrality, PageRank, community, paths) | Agent does manual graph audits instead | **Yes** — 155 nodes, 255 edges is enough for meaningful analysis. Natural fit for introspection cycles |
| `correlate` | scikit-learn (correlation, clustering, PCA) | No structured datasets assembled | Later — needs more event volume |

*Note: `nlp_extract` (spaCy) was adopted organically around cycle 7. Used for extracting named entities from news text before entity resolution.*

### Orchestration (5)

| Tool | Why unused | Worth pushing? |
|------|-----------|----------------|
| `workflow_define` | No recurring pipeline patterns established yet | Not yet |
| `workflow_trigger` | — | Not yet |
| `workflow_status` | — | Not yet |
| `workflow_list` | — | Not yet |
| `workflow_pause` | — | Not yet |

*These become relevant when the agent identifies stable recurring tasks (e.g., "parse these 5 feeds every hour"). Premature to push until the agent has settled into repeatable patterns.*

### Raw OpenSearch (6)

| Tool | Why unused | Worth pushing? |
|------|-----------|----------------|
| `os_create_index` | Events auto-create time-partitioned indices | No |
| `os_index_document` | `event_store` handles this | No |
| `os_search` | `event_search` wraps this with SA-specific fields | No |
| `os_aggregate` | No aggregation queries attempted | Maybe later |
| `os_delete_index` | No index management needed | No |
| `os_list_indices` | No index management needed | No |

*The SA event tools (`event_store`, `event_search`, `event_query`) wrap OpenSearch with domain-specific logic. The raw tools are available for custom use cases but redundant for the current mission.*

### NATS (4 remaining)

| Tool | Why unused | Worth pushing? |
|------|-----------|----------------|
| `nats_subscribe` | Inbox is drained automatically in WAKE | No |
| `nats_create_stream` | Streams created by infrastructure | No |
| `nats_queue_summary` | Injected automatically in ORIENT | No |
| `nats_list_streams` | — | No |

*`nats_publish` is used (11 calls) for status reports. The remaining NATS tools are infrastructure-level and handled by the cycle framework.*

### Memory (2)

| Tool | Why unused | Worth pushing? |
|------|-----------|----------------|
| `memory_promote` | Auto-promotion at significance >= 0.6 handles this | No |
| `memory_supersede` | Agent hasn't needed to manually invalidate memories | No |

*Auto-promotion in PERSIST phase covers the common case. Manual tools remain available for edge cases.*

### Other (7)

| Tool | Why unused | Worth pushing? |
|------|-----------|----------------|
| `event_query` | Agent always uses `event_search` (full-text) instead | **Yes** — structured filters by category/time/source would be more precise |
| `goal_decompose` | Agent uses `goal_create` directly | No |
| `source_remove` | No sources retired yet | No |
| `fs_read` | No file reading needed yet | No |
| `fs_write` | No file writing needed yet | No |
| `fs_list` | No directory listing needed | No |
| `exec` | No shell commands needed | No |
| `code_test` | No self-modification attempted | No |

---

## Recommended Prompt Changes

### Priority 1: Introspection Cycle — Graph Analysis

The introspection prompt (`MISSION_REVIEW_PROMPT`) already asks for graph audit and pattern analysis, but doesn't mention `graph_analyze`. The agent does manual Cypher queries instead. Adding explicit guidance to use `graph_analyze` during introspection would surface:
- **PageRank** — which entities are most connected/influential
- **Community detection** — natural clusters in the knowledge graph
- **Centrality** — key nodes that bridge different clusters

**Where to add:** `MISSION_REVIEW_PROMPT` in templates.py, in the graph audit section.

### Priority 2: Anomaly Detection on Events

With 72+ events categorized and timestamped, `anomaly_detect` could detect spikes in event frequency by region or category. The SA_GUIDANCE already mentions pattern detection but doesn't connect it to the anomaly_detect tool.

**Where to add:** `SA_GUIDANCE` in templates.py, in a new "Analysis" subsection.

### Priority 3: event_query for Structured Filters

The agent exclusively uses `event_search` (full-text) and never `event_query` (structured Postgres). For questions like "all conflict events in the last 24 hours" or "events from source X", structured queries are more precise.

**Where to add:** `SA_GUIDANCE` in templates.py, in the event querying section. Clarify when to use `event_query` (filters) vs `event_search` (text search).

---

## Data Growth Snapshot

| Metric | Value |
|--------|-------|
| Events stored | 72 |
| Distinct sources used | 13 |
| Entity profiles | 120 |
| — Countries | 37 |
| — Organizations | 31 |
| — Persons | 22 |
| — Locations | 22 |
| — Other | 8 |
| Profiles with completeness > 0.5 | 2 (1.7%) |
| Graph nodes | ~155 |
| Graph edges | ~255 |
| Goals completed | 9 |
| Goals active | 1 |

### Observations

1. **Graph-heavy pattern**: The agent spends most cycles building and enriching the knowledge graph (672 graph_store calls, 440 graph_query). This is good — the graph is the core intelligence product.

2. **Entity completeness gap**: Only 2 of 120 profiles exceed 0.5 completeness. The agent stores assertions via `entity_profile` (174 calls) but either isn't filling enough sections per entity type, or the completeness scoring heuristic is too strict. Worth investigating.

3. **Modest event volume**: 72 events across 117 cycles (~0.6 events/cycle). Many cycles focus on graph enrichment rather than ingestion. The single active goal is now focused on increasing feed ingestion.

4. **Dud cycle rate**: ~2-3 cycles per 20 produce 0 actions due to LLM errors (multi-message 400) or the LLM reasoning without producing tool call JSON. The forced-final mechanism recovers gracefully but the cycle is effectively wasted.

5. **nlp_extract adoption**: The agent discovered `nlp_extract` on its own and uses it to pre-extract entities from news text before `entity_resolve`. This is the intended analytical pipeline emerging organically.

---

## Audit Fix Verification

The audit mapping fix (prompt field: `text` → `object`) was deployed at cycle 57. The old index was deleted after cycle 59.

- **Cycles 57-59**: Still showed ~20% error rate (old index mapping)
- **Cycles 60-119**: **Zero indexing errors**. 4,995 docs indexed cleanly.
- **Current index mapping**: `prompt: {type: object, enabled: false}` — correct.
