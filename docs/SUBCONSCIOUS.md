# Subconscious Service Reference

The subconscious service is Legba's second cognitive layer -- a continuously running SLM-powered background processor that handles validation, enrichment, and pattern detection tasks that benefit from lightweight language understanding but don't require the full reasoning capability of the conscious agent's primary LLM.

Container: `subconscious` (via `docker-compose.cognitive.yml`)
Entry point: `python -m legba.subconscious`
Health endpoint: `:8800/health` (also `:8800/metrics` for task stats)
Source: `src/legba/subconscious/`
SLM: Llama 3.1 8B Instruct (Q5_K_M quantization via vLLM)

## Architecture

The service runs three concurrent async loops:

1. **NATS consumer** -- listens for triggered work items from other services (ingestion, agent, maintenance)
2. **Timer loop** -- periodic tasks on modulo schedule (same pattern as the maintenance daemon)
3. **Differential accumulator** -- continuous state change tracking, writes structured briefings to Redis for the conscious agent

All SLM calls use `temperature=0.1` for deterministic, conservative validation. Every prompt includes explicit confidence calibration guidance and requires structured JSON output.

### Connections

| Store | Purpose |
|-------|---------|
| Postgres | Primary data store for signals, entities, facts, situations |
| Redis | Differential briefing output, state snapshots |
| NATS | Triggered work items from other services |

### SLM Provider

The SLM provider is configurable (vLLM or Anthropic) but in practice runs Llama 3.1 8B Instruct via vLLM on a dedicated GPU endpoint. The provider handles HTTP basic auth for Caddy-proxied endpoints.

| Parameter | Default | Env Variable |
|---|---|---|
| Provider | vllm | `SUBCONSCIOUS_LLM_PROVIDER` |
| Model | meta-llama/Llama-3.1-8B-Instruct | `SUBCONSCIOUS_LLM_MODEL` |
| Max tokens | 2048 | `SUBCONSCIOUS_MAX_TOKENS` |
| Temperature | 0.1 | `SUBCONSCIOUS_LLM_TEMPERATURE` |
| Timeout | 60s | `SUBCONSCIOUS_LLM_TIMEOUT` |

### Configuration

All intervals are in ticks (1 tick = `SUBCONSCIOUS_CHECK_INTERVAL` seconds, default 60).

| Env Variable | Default | Effective |
|---|---|---|
| `SUBCONSCIOUS_CHECK_INTERVAL` | 60 | Base tick interval (seconds) |
| `SUBCONSCIOUS_HEALTH_PORT` | 8800 | Health HTTP port |
| `SUBCONSCIOUS_SIGNAL_VALIDATION_INTERVAL` | 15 | 15 min |
| `SUBCONSCIOUS_ENTITY_RESOLUTION_INTERVAL` | 30 | 30 min |
| `SUBCONSCIOUS_CLASSIFICATION_INTERVAL` | 30 | 30 min |
| `SUBCONSCIOUS_FACT_REFRESH_INTERVAL` | 60 | 60 min |
| `SUBCONSCIOUS_SITUATION_DETECT_INTERVAL` | 60 | 60 min |
| `SUBCONSCIOUS_GRAPH_CONSISTENCY_INTERVAL` | 1440 | 24 hours |
| `SUBCONSCIOUS_SOURCE_RELIABILITY_INTERVAL` | 1440 | 24 hours |
| `SUBCONSCIOUS_UNCERTAINTY_LOW` | 0.3 | Below this: rejected |
| `SUBCONSCIOUS_UNCERTAINTY_HIGH` | 0.7 | Above this: accepted |
| `SUBCONSCIOUS_SIGNAL_BATCH_SIZE` | 10 | Signals per validation batch |
| `SUBCONSCIOUS_ENTITY_BATCH_SIZE` | 10 | Entities per resolution batch |
| `SUBCONSCIOUS_CLASSIFICATION_BATCH_SIZE` | 10 | Signals per classification batch |

---

## Module Reference

### 1. Signal Batch Validation

**File:** `validation.py`
**Schedule:** Timer every 15 ticks (15 min) + NATS triggered
**JDL Level:** L0 (Signal Refinement)
**NATS subject:** `legba.subconscious.signals`

Validates signals in the uncertainty band (`confidence` between `uncertainty_low` and `uncertainty_high`, default 0.3-0.7) that were ingested in the last 24 hours.

**SLM assessment (per signal):**
- **Specificity** (0-1): How specific and actionable is the information? Vague rumors score low; named actors, dates, and locations score high.
- **Internal consistency** (0-1): Does the signal contradict itself? Are claims logically coherent?
- **Cross-signal contradiction**: Does this signal directly contradict others in the batch?
- **Adjusted confidence**: Recomputed confidence score considering all the above.

**Confidence calibration guidance in the prompt:**
- Conservative bias -- when in doubt, lower the confidence
- Purely speculative or unsourced signals should score < 0.3 specificity
- Same-source repetition is not cross-contradiction

The adjusted confidence is written back to the signal's `confidence` column. Signals that drop below `uncertainty_low` are effectively flagged as unreliable.

### 2. Entity Resolution

**File:** `entity_resolution.py`
**Schedule:** Timer every 30 ticks (30 min) + NATS triggered
**JDL Level:** L1 (Entity Assessment)
**NATS subject:** `legba.subconscious.entities`

Resolves ambiguous entity extractions by matching them against existing entity profiles.

**Process:**
1. Fetch entities from `signal_entity_links` where `confidence < 0.6` (created in last 24h)
2. For each, query candidate matches using `pg_trgm` trigram similarity (falls back to ILIKE if extension unavailable)
3. Present the entity name, context (signal title), and candidates to the SLM
4. SLM returns a match verdict with confidence

**Trigram cross-validation:**

After the SLM returns its verdict, the result is cross-validated against the database trigram similarity. If the SLM claims a high-confidence match (> 0.8) but the trigram similarity between the extracted name and the matched canonical name is below 0.3, the confidence is downgraded to 0.5. This catches hallucinated matches where the SLM sees semantic connection that doesn't exist textually.

**Confidence calibration guidance in the prompt:**
- 0.95+: Name, type, and context all clearly match a single candidate
- 0.80-0.94: Strong match but minor ambiguity
- 0.60-0.79: Probable match but real uncertainty
- 0.40-0.59: Uncertain, multiple candidates plausible
- Below 0.40: Very uncertain -- mark as new entity

**Auto-apply threshold:** Only verdicts with `confidence > 0.85` are applied automatically:
- Matches: update `signal_entity_links.confidence` for the matched entity
- New entities: create a new `entity_profiles` row with `source=subconscious_entity_resolution`

Below 0.85, the verdict is logged but not applied -- the conscious agent can review during CURATE cycles.

### 3. Classification Refinement

**File:** `classification.py`
**Schedule:** Timer every 30 ticks (30 min)
**JDL Level:** L4 (Process Refinement)

Handles boundary cases where the DeBERTa ML classifier is uncertain between top categories. Currently targets signals classified as `other` in the last 24 hours (a proxy for boundary cases until classification scores are stored).

**Process:**
1. Fetch signals with `category=other` created in last 24h
2. Present the signal text and ML classifier scores to the SLM
3. SLM returns the corrected category (or confirms `other`) with confidence

Available categories: conflict, political, economic, disaster, health, technology, social, environmental, security, diplomatic, military, other.

**Applied changes:**
- Updates the signal's `category` column
- Writes the SLM classification confidence to `confidence_components.classification`
- Only applied when the SLM suggests a category other than `other`

### 4. Fact Refresh

**File:** `service.py` (inline in `_periodic_fact_refresh`)
**Schedule:** Timer every 60 ticks (1 hour)
**JDL Level:** L3 (Impact Assessment)

Checks stored facts against recent signals to assess whether they are corroborated, contradicted, or stale.

**Process:**
1. Fetch 10 facts with oldest `updated_at` (non-superseded, not updated in 24h)
2. For each fact, query signals from the last 7 days matching the fact's subject
3. Present the fact and related signals to the SLM
4. SLM returns a verdict: `corroborated`, `contradicted`, or `stale`

**Applied changes:**
- **Corroborated:** Confidence boosted by 0.1 (capped at 1.0)
- **Contradicted:** Confidence reduced by 0.3 (floored at 0.0)
- **Stale:** Only `updated_at` is touched (prevents re-checking)

**Confidence calibration guidance in the prompt:**
- A single high-confidence contradicting signal can override multiple low-confidence supporting ones
- Consider signal confidence when weighting evidence

### 5. Situation Detection (SLM)

**File:** `situation_detect.py`
**Schedule:** Timer every 60 ticks (1 hour)
**JDL Level:** L2 (Situation Assessment)

SLM-based situation detection that produces human-quality names and descriptions, replacing the mechanical entity-concatenation approach of the maintenance daemon's situation detector.

**Process:**
1. Fetch events from last 48h, group by (category, primary_region)
2. Filter to clusters with 8+ events
3. For each qualifying cluster, ask the SLM to evaluate narrative coherence
4. Dedup against existing situations using Jaccard similarity on names (threshold 0.5)
5. Insert passing clusters as `status=proposed` situations tagged `slm-detected`

**SLM evaluation criteria:**
- Is there a coherent narrative thread (causality, shared actors, developing story)?
- Events that merely share region and category but lack narrative connection are rejected
- The SLM provides a clear, human-readable name (e.g., "Iran-Israel Military Escalation" not "Conflict: Ir -- Iran, Israel, IRGC")
- Confidence reflects certainty that the events form one coherent narrative

### 6. Differential Accumulator

**File:** `differential.py`
**Schedule:** Continuous (accumulates every 5 minutes)
**JDL Level:** L3 (Impact Assessment)

Tracks all state changes between conscious agent cycles and writes a structured JSON briefing to Redis. The conscious agent reads and clears this differential at the start of each cycle's ORIENT phase.

**Tracked changes:**
- New signals per situation (last 200, grouped by linked situation)
- Event transitions (updated events, last 50)
- Entity anomalies (low completeness, stale verification, last 30)
- Fact changes (created, updated, or superseded, last 50)
- Hypothesis evidence changes (last 20)
- Watchlist trigger matches (last 30)

**Redis keys:**
- `legba:subconscious:differential` -- current accumulated differential (JSON)
- `legba:subconscious:last_snapshot` -- timestamp of last accumulation

The differential provides the conscious agent with a structured summary of everything that changed since its last cycle, enabling efficient ORIENT without re-querying all data.

### 7. Relationship Validation

**File:** `service.py` (inline handler)
**Schedule:** NATS triggered only
**JDL Level:** L1 (Entity Assessment)
**NATS subject:** `legba.subconscious.relationships`

Validates relationship triples extracted by the REBEL model. Each triple has a subject, predicate (relationship type), and object.

**SLM validation:**
- **Valid:** The relationship accurately reflects the source text
- **Invalid:** The extraction is wrong, hallucinated, or the relationship type is incorrect
- **Reclassified:** The relationship exists but the type is wrong -- SLM provides the corrected type

Common relationship types: `allied_with`, `opposes`, `part_of`, `located_in`, `leads`, `member_of`, `supplies`, `sanctions`, `controls`, `subsidiary_of`.

**Applied changes:**
- Valid triples: inserted into `proposed_edges` with `status=pending`
- Reclassified triples: inserted with the corrected relationship type
- Invalid triples: logged, not stored
- Duplicate detection prevents reinsertion of existing proposed edges
- All edges get `confidence=0.7` and `source_cycle=0` (subconscious origin)

### 8. Graph Consistency (Daily)

**File:** `service.py` (inline in `_periodic_graph_consistency`)
**Schedule:** Timer every 1440 ticks (24 hours)
**JDL Level:** L1 (Entity Assessment)

Queries for graph anomalies (orphan entities, stale entities) and asks the SLM to assess which ones need action.

**Anomaly types:**
- **orphan_entity:** No signal links in 30+ days, older than 7 days
- **stale_entity:** Not verified in 14+ days
- **dissolved_active_edges:** Entity marked inactive but still has recent edges
- **contradictory_edges:** Two edges imply contradictory relationships

### 9. Source Reliability Recalculation (Daily)

**File:** `service.py` (inline in `_periodic_source_reliability`)
**Schedule:** Timer every 1440 ticks (24 hours)
**JDL Level:** L4 (Process Refinement)

Recalculates `source_quality_score` for all active sources based on:
- Fetch success ratio (60% weight): `success_count / (success_count + failure_count)`
- Average signal confidence (40% weight): Average confidence of signals from this source in the last 7 days

---

## NATS Subjects

The subconscious service subscribes to the following NATS subjects:

| Subject | Trigger | Handler |
|---|---|---|
| `legba.subconscious.signals` | Ingestion batch complete | Signal validation for specific signal IDs |
| `legba.subconscious.entities` | Entity extraction complete | Entity resolution for specific entity IDs |
| `legba.subconscious.relationships` | Relationship extraction complete | Triple validation with source text |
| `legba.subconscious.verdicts` | Conscious agent acknowledges verdict | Logged only |
| `legba.subconscious.briefing` | Conscious agent requests briefing | Triggers immediate differential accumulation |

---

## JDL Fusion Level Coverage

| Level | Description | Subconscious Tasks |
|---|---|---|
| L0 | Signal Refinement | Signal batch validation (specificity, consistency, adjusted confidence) |
| L1 | Entity Assessment | Entity resolution, relationship validation, graph consistency |
| L2 | Situation Assessment | SLM situation detection (narrative coherence evaluation) |
| L3 | Impact Assessment | Fact refresh (corroboration/contradiction), differential accumulator |
| L4 | Process Refinement | Classification refinement, source reliability recalculation |

---

## Key Design Decisions

**Conservative by default.** Every SLM prompt instructs the model to be conservative -- lower confidence when uncertain, mark entities as new rather than guessing at matches, reject ambiguous classifications rather than forcing a category.

**Auto-apply threshold at 0.85.** Entity resolution verdicts below 0.85 confidence are logged but not applied. This prevents the SLM from making low-confidence changes that the conscious agent would need to reverse.

**Trigram cross-validation.** SLM entity matches are checked against database trigram similarity. High-confidence SLM matches with low trigram similarity are downgraded, catching hallucinated semantic connections.

**Structured JSON output.** All SLM calls require JSON schema-constrained output using Pydantic models (`schemas.py`). This eliminates parsing ambiguity and ensures every verdict has the required fields.

**Temperature 0.1.** Low temperature ensures deterministic, repeatable validation results. The subconscious is not doing creative reasoning -- it is making conservative quality judgments.
