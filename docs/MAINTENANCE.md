# Maintenance Daemon Reference

The maintenance daemon is Legba's unconscious processing layer -- a continuously running, deterministic background service that handles all housekeeping tasks without any LLM involvement. It keeps the data store healthy, computes derived metrics, detects anomalies, and propagates state changes between analytical objects.

Container: `maintenance` (via `docker-compose.cognitive.yml`)
Entry point: `python -m legba.maintenance`
Health endpoint: `:8700/health` (also `:8700/metrics` for task stats)
Source: `src/legba/maintenance/`

## Architecture

The daemon runs a single-threaded async tick loop. Each tick is `check_interval` seconds (default 60s). Tasks are scheduled by modulo arithmetic on the tick counter -- when `tick % interval == 0`, the task runs. This provides deterministic, non-overlapping execution without cron or external schedulers.

All tasks are isolated, idempotent, and non-fatal -- a failure in one task is logged and does not affect others. Each task gets its own error counter and timing stats, exposed via the `/metrics` endpoint.

### Connections

The daemon connects to all five backing stores on startup:

| Store | Purpose |
|-------|---------|
| Postgres/AGE | Primary data store, graph queries |
| Redis | Heartbeat, state snapshots, task backlog |
| OpenSearch | Document index (optional) |
| Qdrant | Vector store for adversarial detection (optional) |
| NATS | Event notifications (optional) |
| TimescaleDB | Metrics time-series (via shared MetricsClient) |

### Configuration

All intervals are configurable via environment variables. Intervals are expressed in ticks (1 tick = `MAINTENANCE_CHECK_INTERVAL` seconds, default 60).

| Env Variable | Default | Effective |
|---|---|---|
| `MAINTENANCE_CHECK_INTERVAL` | 60 | Base tick interval (seconds) |
| `MAINTENANCE_HEALTH_PORT` | 8700 | Health HTTP port |
| `MAINTENANCE_LIFECYCLE_DECAY_INTERVAL` | 5 | 5 min |
| `MAINTENANCE_CORROBORATION_INTERVAL` | 10 | 10 min |
| `MAINTENANCE_METRICS_INTERVAL` | 5 | 5 min |
| `MAINTENANCE_PROPAGATION_INTERVAL` | 5 | 5 min |
| `MAINTENANCE_SITUATION_DETECT_INTERVAL` | 30 | 30 min |
| `MAINTENANCE_ADVERSARIAL_DETECT_INTERVAL` | 30 | 30 min |
| `MAINTENANCE_ENTITY_GC_INTERVAL` | 60 | 1 hour |
| `MAINTENANCE_FACT_DECAY_INTERVAL` | 60 | 1 hour |
| `MAINTENANCE_CALIBRATION_TRACK_INTERVAL` | 60 | 1 hour |
| `MAINTENANCE_INTEGRITY_INTERVAL` | 720 | 12 hours |

---

## Module Reference

### 1. Event Lifecycle Decay

**File:** `lifecycle.py`
**Schedule:** Every 5 ticks (5 min)
**JDL Level:** L2 (Situation Assessment)

Applies deterministic state machine transitions to events based on signal activity and elapsed time. No LLM judgment -- purely rule-based.

**Transition rules:**

| From | To | Condition |
|---|---|---|
| EMERGING | DEVELOPING | 3+ linked signals, most recent signal within 48h |
| EMERGING | RESOLVED | No new signals in 48h |
| DEVELOPING | ACTIVE | 8+ linked signals, most recent within 72h |
| DEVELOPING | RESOLVED | No new signals in 72h |
| ACTIVE | EVOLVING | Signal velocity doubled (24h vs prior 24h), 3+ recent signals |
| ACTIVE | RESOLVED | No new signals in 7 days |
| EVOLVING | ACTIVE | Velocity stabilized |
| EVOLVING | RESOLVED | No new signals in 7 days |
| RESOLVED | DEVELOPING | New signal linked after resolution timestamp |

Transition history is stored in the event's `data` JSONB (last 20 transitions). Both the dedicated `lifecycle_status` column and the JSONB field are updated.

**Situation decay:** Active situations with no linked event activity in 10 days are marked `dormant`.

### 2. Entity Garbage Collection

**File:** `entity_gc.py`
**Schedule:** Every 60 ticks (1 hour)
**JDL Level:** L1 (Entity Assessment)

Four sub-tasks:

- **Dormancy marking:** Entities with no `signal_entity_links` created in 30 days are marked `gc_status=dormant` in their JSONB. Not deleted -- excluded from active queries.
- **Duplicate detection:** Uses `pg_trgm` trigram similarity (threshold 0.6) on `canonical_name` between entities of the same type. Co-occurrence in shared signals strengthens the match. Duplicates are flagged as `duplicate_candidate` with a reference to the stronger entity.
- **Orphan edge cleanup:** Removes `signal_entity_links` and `event_entity_links` pointing to non-existent or merged entities.
- **Source health:** Auto-pauses sources with >20 consecutive fetch failures. Records the pause reason and timestamp in the source's JSONB.

### 3. Fact Decay / Temporal Validity

**File:** `fact_decay.py`
**Schedule:** Every 60 ticks (1 hour)
**JDL Level:** L3 (Impact Assessment)

Two operations:

- **Expiration:** Facts with `valid_until` in the past are marked `expired=true` in JSONB. Open-ended facts (no `valid_until`) with no supporting signals in 30 days have `valid_until` set to `NOW()` and reason recorded as `no_supporting_signals_30d`. For facts with an `evidence_set`, the specific signal UUIDs are checked; for facts without, a subject-matching heuristic is used.
- **Confidence decay:** Facts with no update in 30 days get confidence reduced by 0.05 per cycle (floor: 0.1). The decay amount is tracked in `confidence_components.decay` for auditability. Only affects non-superseded, non-expired facts with `confidence > 0.1`.

### 4. Corroboration Scoring

**File:** `corroboration.py`
**Schedule:** Every 10 ticks (10 min)
**JDL Level:** L2 (Situation Assessment)

For events that received new signals in the last 15 minutes, counts distinct `source_id` values across linked signals.

**Scoring table:**

| Independent Sources | Corroboration Score |
|---|---|
| 1 | 0.0 (uncorroborated) |
| 2 | 0.3 |
| 3 | 0.5 |
| 4 | 0.7 |
| 5+ | 0.9 |

The score is written to:
- The event's `data` JSONB (`corroboration_score`, `corroboration_sources`, `corroboration_updated_at`)
- Each linked signal's `confidence_components` JSONB column (corroboration object with score, independent source count, and event ID)

### 5. Metric Collection

**File:** `metrics.py`
**Schedule:** Every 5 ticks (5 min)
**JDL Level:** L0-L5 (all levels)

Collects 30+ metrics from Postgres and writes them to TimescaleDB in a single batch. These power the Grafana dashboards.

**Metrics collected:**

| Category | Metrics |
|---|---|
| Signal velocity | Per-source signals/hour (24h), per-category signals/hour |
| Events | Total, created in 24h, lifecycle distribution |
| Entities | Total, by type, signal links total, event links total, versions in 24h |
| Hypotheses | Count by status, avg evidence balance, evaluated in 24h |
| Facts | Confidence distribution (4 buckets), active total |
| Situations | Count by status, avg events, avg intensity |
| Signals | Total, 24h count |
| Sources | Count by status, diversity concentration |
| Graph | Balance score, balanced/unbalanced triads, signed edges, Shannon entropy |
| Fusion quality | L0 signal quality, L1 entity completeness, L2 situation coverage, L3 hypothesis resolution, L3 prediction accuracy, L4 calibration score, L5 consult sessions |

**Structural balance** (computed during metrics tick): Queries AlliedWith and HostileTo edges from the AGE graph, builds a signed adjacency matrix, enumerates all triads, and classifies each as balanced or unbalanced. The balance score, triad counts, and signed edge count are written to TimescaleDB.

**Graph entropy** (computed during metrics tick): Computes Shannon entropy over the relationship type distribution from the AGE graph. Higher entropy = more diverse relationship landscape. Entropy spikes indicate relationship reorganization.

### 6. Integrity Verification

**File:** `integrity.py`
**Schedule:** Every 720 ticks (12 hours)
**JDL Level:** L4 (Process Refinement)

Verifies data consistency across the relational schema. Detects and auto-fixes certain categories of drift.

**Checks (detect and fix):**
- Events with phantom `signal_count` (claims signals but has no links)
- Events with `signal_count` mismatch vs actual link count (auto-corrected)
- Orphan `signal_event_links` pointing to deleted signals (auto-cleaned)
- Orphan `signal_entity_links` (signal or entity side)
- Facts with broken `superseded_by` references (cleared)
- Situations with `event_count` drift vs actual junction table count (auto-corrected)
- Active facts with empty `evidence_set`

**Evaluation rubrics** (computed alongside integrity): Six quality metrics normalized to 0.0-1.0:
- Event clustering rate (signal-to-event link coverage, 7-day window)
- Graph quality (avg entity links per event, normalized at 3+)
- Source health (active sources with <5 consecutive failures)
- Entity link coverage (signals with at least one entity link)
- Fact freshness (non-expired active facts)
- Hypothesis balance (lower evidence imbalance = higher score)

All metrics and rubrics are written to TimescaleDB.

### 7. Adversarial Detection

**File:** `adversarial.py`
**Schedule:** Every 30 ticks (30 min, offset by 15 ticks)
**JDL Level:** L4 (Process Refinement)

Three heuristic detections for coordinated inauthentic behavior. No ML or SLM -- purely SQL and lightweight Python.

**7a. Velocity Spike Detection**

Within a 6-hour window, finds entities mentioned by 3+ low-quality sources (`source_quality_score < 0.4`) where no high-quality sources (`> 0.7`) are reporting the same entity. Flags as medium severity (<5 sources) or high severity (5+ sources).

**7b. Semantic Echo Detection**

Within 4-hour time buckets, finds clusters of 3+ signals from different sources with title Jaccard similarity > 0.6. Clusters where all sources share the same ownership type or geographic origin are filtered out (legitimate wire service republishing). Remaining clusters are flagged as potential echo campaigns.

**7c. Source Provenance Grouping**

Finds source groups sharing `ownership_type` and `geo_origin`, then checks if the group converges on the same entity within 12 hours. When 50%+ of a provenance group covers the same entity, flags as coordinated publishing.

All flags are persisted to the signal's `data` JSONB under `adversarial_flags` (appended array). Flag counts are written to TimescaleDB.

### 8. Calibration Tracking

**File:** `calibration.py`
**Schedule:** Every 60 ticks (1 hour, offset by 45 ticks)
**JDL Level:** L4 (Process Refinement)

Measures whether confidence scores are actually predictive of outcomes.

**Hypothesis resolution tracking:** When hypotheses reach `confirmed` or `refuted` status, records the evidence balance at resolution, thesis text, support/refute signal counts, and time to resolution. Stored to Redis (`legba:calibration:data` list) and marked in the hypothesis JSONB to avoid double-counting.

**Confidence distribution analysis:** Compares average signal confidence across event lifecycle states:
- Signals in ACTIVE events (signal_count > 5): should have higher avg confidence
- Signals in RESOLVED events (signal_count < 3): should have lower avg confidence
- **Discrimination score** = active avg - dead avg (higher is better -- means confidence is predictive)
- Also tracks avg evidence balance for confirmed vs refuted hypotheses (confirmed should be positive, refuted should be negative)

Results are written to TimescaleDB as calibration metrics.

### 9. Situation Detection

**File:** `situation_detect.py`
**Schedule:** Every 30 ticks (30 min)
**JDL Level:** L2 (Situation Assessment)

Mechanical (non-LLM) situation candidate detection. Finds event clusters that should be proposed as new situations.

**Criteria:**
- 8+ events in the same region and category within 7 days
- Sharing 2+ entities (actors or locations)
- No existing active/proposed situation already covers the same entity set and region

Proposed situations are created with `status=proposed` and tagged `auto-detected`. The conscious agent reviews and promotes or dismisses them during CURATE cycles.

### 10. State Propagation

**File:** `propagation.py`
**Schedule:** Every 5 ticks (5 min)
**JDL Level:** L3 (Impact Assessment)

Five reactive propagation rules that tie analytical objects together:

**10a. Watch Trigger Propagation**

When a watchlist trigger fires, finds the matching situation (by entity/keyword overlap) and links the triggering event to that situation. Updates the situation's event count.

**10b. Hypothesis Shift Propagation**

Snapshots hypothesis evidence balances to Redis. When the balance shifts by >= 3 between snapshots, adds a `deep_dive_situation` task to the backlog for the SYNTHESIZE cycle, prioritized by delta magnitude.

**10c. Situation Escalation**

Compares current situation severity against the previous snapshot. When severity increases or event count jumps by 3+, and no existing goal covers the situation, computes an escalation score (using the shared `compute_escalation_score` function). High-scoring escalations generate `create_investigative_goal` tasks for SURVEY cycles.

**10d. Event Lifecycle Propagation**

When events reach ACTIVE status and are linked to a situation with an investigative goal, creates `research_entity` tasks for the event's key actors (up to 2 per event) on the RESEARCH cycle backlog.

**10e. Stale Goal Flagging**

Goals with no progress in 10+ hours (approximately 50 cycles) are flagged with `review_goal` tasks for the EVOLVE cycle. Idempotent -- tracks which goals have already been flagged in Redis.

All propagation rules also expire stale tasks from the backlog (max age 24 hours).

### 11. Backfill

**File:** `backfill.py`
**Schedule:** Startup only (one-time, idempotent)
**JDL Level:** L1-L2

Three backfill tasks that populate new data structures from existing data:

- **Event graph vertices:** Creates Event vertices in the AGE graph for all events that don't have one yet. Also creates INVOLVED_IN edges from event actors.
- **Situation graph edges:** Creates TRACKED_BY edges from events to situations based on the `situation_events` junction table.
- **Edge properties:** Sets default temporal properties (`weight=0.5`, `confidence=0.5`, `evidence_count=1`, `volatility=0.0`) on any graph edge where `evidence_count IS NULL`. Handles edges from the seed CSV import that predate the temporal edge model.

### 12. Nexus Confidence Decay

Nexus operations not evidenced in 30 days get confidence decremented by 0.05 (floor 0.1), matching the fact decay pattern. This prevents stale proxy/covert channel assessments from persisting at high confidence indefinitely.

---

## JDL Fusion Level Coverage

| Level | Description | Maintenance Tasks |
|---|---|---|
| L0 | Signal Refinement | Corroboration scoring (signal confidence_components) |
| L1 | Entity Assessment | Entity GC, duplicate detection, orphan cleanup, backfill |
| L2 | Situation Assessment | Event lifecycle, situation detection, situation decay, backfill |
| L3 | Impact Assessment | Fact decay, state propagation (escalation, hypothesis shifts) |
| L4 | Process Refinement | Integrity verification, adversarial detection, calibration tracking, eval rubrics |
| L5 | User Refinement | (Metric collection surfaces all levels to Grafana for operator review) |
