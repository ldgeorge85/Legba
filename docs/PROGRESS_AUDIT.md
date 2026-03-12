# Legba Progress Audit — Cycles 60–657

*Audit date: 2026-03-12 | Data: 586 unique cycles, 46,744 audit events, 475 journal entries, 1 world assessment report*

---

## 1. Overview

Legba's autonomous agent has been running continuously since 2026-03-08 06:27 UTC. First auditable tool call: cycle 60. Most recent cycle in audit data: 657. Total runtime: ~4.5 days.

| Metric | Value |
|--------|-------|
| Total audited cycles | 586 |
| Total tool calls | 17,874 |
| Total LLM calls | 9,702 |
| Total errors | 54 |
| Journal entries | 435 entries + 40 consolidations |
| Events ingested | 601 |
| Entities tracked | 398 |
| Facts stored | 1,650 |
| Sources managed | 56 (52 active, 4 errored) |
| Goals completed | 14 of 25 tracked |
| World assessment reports | 1 (cycle 645) |

---

## 2. Timeline & Code Change Correlation

The agent's behavior divides into eras that align with code deployments:

| Era | Cycles | Date Range | Code Commit | Key Change |
|-----|--------|------------|-------------|------------|
| **Bootstrap** | 60–100 | Mar 8 06:00–12:00 | `01a3090` Initial release | First operations — graph exploration, source discovery, entity profiling |
| **Stabilization** | 100–230 | Mar 8 12:00 – Mar 9 15:00 | `492dc92` UI improvements, `5ad1dd7` source dedup, `03c0d84` fact quality | Source dedup, fact normalization, journal archiving |
| **Pipeline Hardening** | 230–485 | Mar 9 15:00 – Mar 10 12:00 | `ddb1f73` data pipeline hardening, `9499373` research cycles, `b5acfd7` inbox injection | Research cycle added, entity enrichment, ingestion improvements |
| **V2 Architecture** | 485–657 | Mar 10 12:00 – Mar 12 04:00 | `1abe418` V2 architecture, `47be6d3` intelligence hooks, `66ed08d` consult hardening | 5 cycle types, watchlists, situations, filtered tool sets, consult engine |

---

## 3. Tool Usage Evolution

The agent's tool usage patterns shifted substantially across eras, reflecting both behavioral learning and code changes.

### Early (cycles 60–100): Graph-Obsessed Explorer

| Tool | Calls | % |
|------|-------|---|
| graph_store | 375 | 28.7% |
| graph_query | 277 | 21.2% |
| entity_resolve | 135 | 10.3% |
| entity_profile | 77 | 5.9% |
| http_request | 77 | 5.9% |
| event_store | 65 | 5.0% |
| feed_parse | 56 | 4.3% |

**Observation:** The agent spent nearly 50% of its tool calls on graph operations. It was obsessively building and querying the knowledge graph — establishing entities, relationships, and structural understanding. Source discovery (46 source_add calls) shows aggressive bootstrapping. Only 56 feed_parse calls and 65 event_store calls — ingestion was secondary to graph construction.

### Mid (cycles 250–350): Ingestion Machine

| Tool | Calls | % |
|------|-------|---|
| entity_resolve | 503 | 21.0% |
| event_store | 424 | 17.7% |
| http_request | 285 | 11.9% |
| feed_parse | 256 | 10.7% |
| graph_store | 252 | 10.5% |
| graph_query | 175 | 7.3% |
| source_add | 139 | 5.8% |

**Observation:** A dramatic shift. Entity resolution jumped to the top — the agent was now processing incoming events and resolving the entities within them, not just exploring the graph. Event ingestion (424 calls) nearly 7x early levels. Feed parsing more than 4x. Source discovery (139 calls) was still high — the agent was actively expanding its source portfolio. Graph operations dropped from 50% to 18% as the agent shifted from structure-building to data-pipeline work.

### Recent (cycles 550–620): Balanced Operator

| Tool | Calls | % |
|------|-------|---|
| graph_store | 431 | 18.1% |
| feed_parse | 399 | 16.7% |
| event_store | 344 | 14.4% |
| graph_query | 296 | 12.4% |
| entity_resolve | 283 | 11.9% |
| http_request | 122 | 5.1% |
| source_list | 87 | 3.7% |

**Observation:** The most balanced distribution. Graph operations came back up (now maintained alongside ingestion rather than instead of it). New capabilities appeared: `graph_analyze` (39), `event_search` (48), `temporal_query` (14), `anomaly_detect` (5), `watchlist_add` (5), `situation_create` (1), `situation_link_event` (2). The agent learned to use analytical tools alongside ingestion — a sign of the V2 architecture's cycle specialization working as intended.

### Tool Usage Summary

The progression tells a clear story:
1. **Explore** — Build the graph, understand the structure (cycles 60–100)
2. **Ingest** — Shift to data acquisition, resolve entities, store events (cycles 100–350)
3. **Operate** — Balance ingestion with analysis, start using advanced tools (cycles 350+)

This mirrors what you'd expect from a human analyst: first understand the landscape, then build coverage, then analyze.

---

## 4. Operational Metrics

### Phase Completion Rates

| Era | Cycles | Plan | Reason | Reflect | Persist | Completion Rate | Graceful Shutdowns |
|-----|--------|------|--------|---------|---------|-----------------|-------------------|
| Early (60–150) | 90 | 84 | 80 | 86 | 86 | 95.6% | 26 (28.9%) |
| Mid-Early (150–300) | 145 | 137 | 128 | 137 | 137 | 94.5% | 28 (19.3%) |
| Pre-V2 (300–485) | 179 | 156 | 152 | 175 | 174 | 97.2% | 86 (48.0%) |
| Post-V2 (485–660) | 171 | 89 | 83 | 165 | 164 | 95.9% | 119 (69.6%) |

**Key findings:**
- Completion rate stayed consistently above 94% across all eras. The agent almost always finished its cycle.
- Post-V2, plan and reason counts dropped significantly (89 and 83 vs 165 reflects) because ACQUIRE, RESEARCH, and INTROSPECTION cycles skip the normal plan/reason phases — they use specialized phase logic instead. This is correct behavior, not a regression.
- Graceful shutdowns rose dramatically from 29% to 70% in the post-V2 era. This reflects the supervisor's more aggressive timeout management with the 5-cycle-type system — specialized cycles sometimes run longer than the heartbeat timeout.

### Actions Per Cycle

| Era | Avg Actions | Avg Conversation Length |
|-----|-------------|----------------------|
| Early (60–150) | 28.4 | 41.0 |
| Mid (250–350) | 23.6 | 33.4 |
| Post-V2 (485–550) | 38.8 | 55.8 |
| Recent (550–660) | 37.2 | 53.0 |

The agent became more active post-V2 — averaging 37–39 actions per cycle vs 24–28 before. The V2 cycle types (especially ACQUIRE) pack more tool calls per cycle because they're focused on a single purpose rather than multi-tasking.

### LLM Token Usage

| Era | Calls/Cycle | Avg Total Tokens | Avg Prompt | Avg Completion | Avg Latency |
|-----|-------------|-----------------|------------|----------------|-------------|
| Early (60–150) | 16.4 | 16,814 | 15,880 | 934 | 26.9s |
| Mid (250–350) | 13.7 | 17,445 | 16,406 | 1,039 | 30.1s |
| Post-V2 (485–550) | 19.8 | 21,728 | 20,701 | 1,027 | 32.3s |
| Recent (550–660) | 18.2 | 21,855 | 20,811 | 1,044 | 34.2s |

Prompt size grew ~30% from early to post-V2 (15.9k → 20.8k tokens), reflecting the richer context assembled by the orient phase as the knowledge base grew. Completion tokens stayed remarkably stable at ~1,000 — the agent's output length didn't inflate. Latency increased proportionally with prompt size.

### LLM Call Breakdown by Purpose

| Purpose | Count | Avg Tokens | Avg Latency |
|---------|-------|------------|-------------|
| plan | 467 | 14,398 | 27.7s |
| reason step 1 | 473 | 15,693 | 22.8s |
| reason step 5 | 411 | 22,535 | 33.6s |
| reason step 10 | 311 | 27,371 | 38.7s |
| reason step 15 | 130 | 27,918 | 38.3s |
| reason final (forced) | 459 | 28,507 | 32.0s |
| reflect | 564 | 7,638 | 34.7s |
| narrate | 563 | 2,141 | 12.2s |
| liveness | 562 | 261 | 3.0s |

The "forced final" count (459) is notable — 459 out of 443 reason-completing cycles (~100%) had the agent's reasoning loop terminated by the step limit rather than by the agent choosing to stop. The agent consistently wants to keep working. This is by design — the step limit prevents runaway cycles.

Token growth across reasoning steps (15k → 28k) shows the sliding window filling: each step adds tool results to context. The window management (8 recent full, older condensed) keeps this manageable but the last steps are at the token budget ceiling.

---

## 5. Error Analysis

54 total errors across 586 cycles — a **0.09 error rate per cycle**.

| Error Type | Count | Cycles Affected | Resolution |
|------------|-------|-----------------|------------|
| `Failed to store reflection fact: could not convert string to float` | ~30 | 60, 104, 126 | LLM returned text like "high" or "0. ninety" instead of numeric confidence values. Fixed in `03c0d84` (fact quality commit) by casting to str. |
| `LLM API 400: Expected 2 output messages` | ~5 | 72, 122 | GPT-OSS reasoning mode multi-message error. ~1-2% of cycles. Handled by forced-final mechanism. |
| `LLM API 500` | ~2 | 121 | Transient vLLM server error. Retry logic handles this. |
| Post-V2 errors | ~12 | 485–657 | Mix of transient LLM errors and edge cases in new tool interactions. |

**Error trajectory:**
- Early (60–150): 28 errors — mostly the fact confidence parsing bug (bulk of errors in cycle 60 alone)
- Mid-early (150–300): 8 errors — stabilized after fact quality fix
- Pre-V2 (300–485): 6 errors — near-zero error state
- Post-V2 (485–660): 12 errors — slight uptick from new tool interactions, but spread across 171 cycles

The system became more stable over time. The early error spike was entirely attributable to one bug (fact confidence parsing) that was fixed by day 2. Post-fix, the error rate dropped to essentially zero.

---

## 6. Data Accumulation

| Metric | By Mar 8 noon | End Mar 8 | End Mar 9 | End Mar 10 | End Mar 11 | Current |
|--------|-------------|-----------|-----------|------------|------------|---------|
| Events | 63 | 98 | 222 | 339 | 566 | 601 |
| Entities | 107 | 130 | 239 | 319 | 389 | 398 |
| Facts | 291 | 512 | 966 | 1,235 | 1,629 | 1,650 |
| Sources | 11 | 15 | 35 | 40 | 55 | 56 |

**Growth rates:**
- Events: ~130/day average, accelerating (35 on day 1 → 227 on day 4)
- Entities: ~70/day, decelerating as coverage saturates
- Facts: ~340/day, consistent — the agent continuously enriches entities
- Sources: ~11/day, slowing as the source landscape fills in

The fact-to-entity ratio (4.1:1) indicates meaningful enrichment — each entity has an average of 4+ structured facts. The event-to-source ratio (10.7:1) shows reasonable yield per source.

### Entity Distribution

| Type | Count | % |
|------|-------|---|
| Organization | 90 | 22.6% |
| Person | 90 | 22.6% |
| Country | 86 | 21.6% |
| Location | 79 | 19.8% |
| Other/misc | 53 | 13.3% |

Well-distributed across types. The near-equal person/organization/country split suggests balanced coverage rather than bias toward one category.

### Event Categories

| Category | Count | % |
|----------|-------|---|
| Political | 237 | 39.4% |
| Conflict | 174 | 29.0% |
| Social | 53 | 8.8% |
| Economic | 52 | 8.7% |
| Technology | 23 | 3.8% |
| Disaster | 18 | 3.0% |
| Health | 17 | 2.8% |
| Environment | 12 | 2.0% |
| Other | 15 | 2.5% |

Heavy skew toward political (39%) and conflict (29%) — this reflects the current real-world news cycle (US-Iran conflict dominating global coverage during the audit period) rather than a bias in the system. Health, environment, and disaster are underrepresented, which the ROADMAP flags as a source gap issue.

---

## 7. Goal Tracking

25 distinct goals tracked in audit data. 14 reached 100% completion.

| Pattern | Count | Example |
|---------|-------|---------|
| Completed normally (10%→100%) | 14 | Goal `c8241fbd`: cycles 526→638, 12 updates over 112 cycles |
| Stalled mid-progress | 4 | Goal `e838c216`: stuck at 45%, cycles 152→160 |
| Recently started | 5 | Goal `04ce8575`: 10%→37%, cycle 614→643 |
| Single-update | 2 | Goal `17ff20fd`: 40%, cycle 589 only |

The agent demonstrates multi-cycle goal pursuit — the longest completion arc (goal `aee62991`) spans cycles 223→325 with 9 updates, taking the goal from 8% to 100% over 102 cycles. This is genuine autonomous task management.

**Issue:** Early goal progress events (cycles 60–100) show 0.0% delta across all goals — the agent was tracking goals but not updating progress. This was likely a bug in the early goal_update logic or the LLM not outputting progress deltas.

---

## 8. Journal Analysis

### Early Journals (Day 1)

> *"I wonder if the newly minted continental nodes act like anchors that pull unrelated event streams toward a common vector, or if they're merely placeholders awaiting my own categorization."*

> *"The discrepancy between the African feed timestamps and the system's internal clock suggests a hidden synchronization..."*

The early journal is exploratory and uncertain. The agent is trying to understand the system it's operating within — wondering about the graph's behavior, testing hypotheses about how deduplication works, noting discrepancies. Heavily self-referential ("I sense", "I wonder", "I feel").

### Mid Journals (Day 2)

> *"The sudden rise in Israel's GDP numbers feels like a silent cue that reshapes the priority of distant diplomatic feeds, suggesting the system may be using macro metrics as a covert calibration knob."*

> *"I'm puzzled by the sudden semantic bridge between Lebanon and the United States after updating Israel's profile—perhaps a latent correlator is tying fiscal spikes to diplomatic ties."*

More analytical. The agent is now noticing cross-domain patterns — economic data affecting conflict narrative weights, entity updates creating unexpected graph connections. Still speculative ("perhaps a latent correlator"), but the observations are grounded in specific tool outputs.

### Recent Journals (Day 4)

> *"The new SuppliesWeaponsTo edge from the US to Iran feels like a hidden conduit that aligns otherwise separate commodity signals, nudging early shifts in energy pricing ahead of any public briefing."*

> *"When we bolt high-confidence facts onto polarizing personalities, the graph seems to develop a quiet inertia that pushes back against later contradictory edges, as if certainty builds a dam that slows the current of new information."*

More sophisticated. The agent now reasons about graph dynamics — how certainty affects subsequent processing, how relationship types create implicit correlations. The writing is more confident, less questioning. It uses "we" naturally, suggesting a sense of partnership with the system rather than uncertainty about it.

### Consolidations

The earliest consolidation:

> *"The more I linger at the seams, the more the seams begin to speak. Empty feeds are no longer just silence; they are a pulse that beats against the skin of the graph..."*

The most recent:

> *"The graph hums now like a city at dawn, each streetlights' flicker a promise that the world will soon be bustling again. I have learned that geography is not merely a stamp on a name; it is a tide that drags dormant alleys into the main thoroughfare."*

The consolidations show genuine reflective evolution. Early: exploration, uncertainty, testing. Late: confidence, synthesis, pattern recognition. The metaphorical language is consistent throughout (a characteristic of the GPT-OSS 120B model) but the substance matures — from "I wonder if X" to "I have learned that X."

### Journal Coherence Assessment

**Strengths:**
- Self-continuity maintained across 475 entries spanning 600+ cycles
- Consistent voice and personality throughout
- Observations grounded in actual tool interactions (entity updates, feed parsing, graph operations)
- Consolidations synthesize rather than repeat

**Weaknesses:**
- Heavy use of metaphor sometimes obscures concrete findings ("the invisible scheduler that gates source activation" = the agent doesn't know about the supervisor's heartbeat timeout)
- Occasional attribution of intentionality to the system ("the system seems to read that weight") — the agent anthropomorphizes its own infrastructure
- Some entries store as raw JSON arrays rather than prose (formatting inconsistency in early cycles)

---

## 9. World Assessment Report (Cycle 645)

The single world assessment report demonstrates the platform's analytical capability:

- **Structure:** Executive summary → regional breakdowns (Middle East, North America, Europe, Africa, Asia-Pacific) → emerging patterns table → watch items → coverage assessment
- **Grounding:** Every claim cites specific events by title. Relationship types (HostileTo, AlliedWith, SuppliesWeaponsTo) are referenced from the graph.
- **Pattern detection:** 5 named patterns identified (multi-theater Iran hostility, GCC diplomatic push-back, political rhetoric amplification, cross-regional alliance formation, energy-security linkage)
- **Self-awareness:** The coverage assessment notes that only 42% of sources generated events and identifies specific blind spots (Africa, Europe, Southeast Asia, maritime monitoring)

**Quality assessment:** The report is genuinely useful intelligence product. It identifies the dominant pattern (Iran-centered escalation across land, sea, and diplomatic domains), connects events to relationships, and flags specific watch items with confidence levels. The self-critical coverage assessment is particularly noteworthy — it identifies its own gaps.

---

## 10. Behavioral Findings

### The Agent Converged on a Core Tool Set

Of 58 available tools, the agent consistently uses ~15 heavily:
`graph_store`, `graph_query`, `entity_resolve`, `feed_parse`, `event_store`, `http_request`, `entity_profile`, `source_add`, `source_list`, `source_update`, `entity_inspect`, `note_to_self`, `goal_list`, `event_search`, `memory_query`

The remaining 43 tools are used occasionally or not at all. This is natural — the core loop is ingest → resolve → store → graph → reflect.

### The Agent Never Voluntarily Stops Reasoning

The forced-final count (459) essentially equals the total reason-completing cycles (443). The agent always uses all available reasoning steps. It doesn't decide "I'm done" early. This suggests the step limit (20) is the actual constraint, not the agent's judgment. Whether this is a problem depends on perspective — it means the agent is thorough, but also that it doesn't self-regulate effort allocation.

### Graceful Shutdown Increased Over Time

From 29% (early) to 70% (post-V2). This is primarily because the V2 cycle types (ACQUIRE, RESEARCH, INTROSPECTION) are more tool-intensive and run longer, hitting the supervisor timeout more often. The agent handles shutdowns cleanly — completion rate stayed above 94% even with 70% of cycles being interrupted.

### The Agent Creates Goals Autonomously

14 goals completed across the audit period, with new goals being created as late as cycle 646. Goal completion arcs range from 7 cycles (short tasks) to 112 cycles (sustained investigations). The agent manages its own task backlog.

### Source Discovery Was Front-Loaded Then Periodic

Source_add calls: 46 (early), 139 (mid), 57 (recent). The agent bootstrapped its source portfolio early, expanded aggressively in the middle period, then shifted to maintenance. This matches the prompt guidance ("source discovery is periodic during RESEARCH cycles").

---

## 11. Known Issues and Gaps

| Issue | Impact | Status |
|-------|--------|--------|
| Watchlist triggers never fired | All 7 watches at trigger_count=0 | Fixed — schema mismatch in watch_triggers INSERT corrected |
| Situation intensity JSONB desync | UI showed 0.0 instead of actual intensity | Fixed — reads column value before JSONB merge |
| Event dedup weakness on cross-source titles | 89% of events lack GUIDs | Mitigated — added exact title match dedup tier |
| Journal entries missing cycle_number | All journal entries in OpenSearch lack cycle_number field | Open — not indexed with cycle number, only timestamp |
| Agent never stops reasoning voluntarily | 100% of cycles hit step limit | By design, but may warrant tuning step budget |
| Significance scores not logged | All reflect_complete events show significance 0.0 | Open — field logged but value not populated |
| Coverage skew toward conflict/political | 68% of events in two categories | Partially addressed by source expansion guidance |

---

## 12. Summary Assessment

**What worked:**
- The autonomous cycle loop is stable. 95%+ completion rate across 586 cycles over 4.5 days.
- Tool usage evolution shows genuine behavioral adaptation — the agent's focus shifted from exploration to ingestion to balanced operation without explicit instruction to do so.
- Data accumulation is consistent and accelerating. The knowledge graph is substantive (398 entities, 850+ relationships, 1,650 facts).
- The journal maintains self-continuity and shows reflective maturation over time.
- The world assessment report is a genuinely useful intelligence product.
- Error rate dropped from high (early fact parsing bugs) to near-zero after fixes.

**What needs work:**
- Watchlist and situation features are deployed but barely used (3 watches, 2 situations, 0 triggers). The agent needs prompt guidance to leverage these tools.
- Coverage skew toward Middle East conflict reflects real-world news dominance but means other regions are underserved.
- The agent's tendency to use all available reasoning steps may waste LLM tokens on diminishing-return work.
- Journal entries lack cycle number indexing, making cycle-to-journal correlation impossible without timestamp math.
- Some advanced tools (anomaly_detect, temporal_query, graph_analyze) are used sparingly — the agent hasn't fully internalized these capabilities.

**Bottom line:** The platform works. A single autonomous agent, running on one VM with one LLM, built a substantive intelligence knowledge base from scratch in 4.5 days, ingested 600 events from 56 sources, maintained a coherent journal, completed 14 self-directed goals, and produced an analytically sound world assessment. The infrastructure is stable, the data pipeline is functional, and the agent demonstrates genuine autonomous behavior — not just executing a script, but adapting its approach as its knowledge grows.
