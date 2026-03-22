# Legba — Cycle Routing Design

*3-tier routing: scheduled outputs + guaranteed work + dynamic fill.*

---

## Design Evolution

1. **Original**: Pure modulo routing — every type had a fixed interval, priority cascade resolved collisions. Simple but wasteful (ANALYSIS on empty diffs, CURATE when nothing to curate).
2. **v2 (0610f65)**: Scheduled outputs + fully dynamic scoring — CURATE/RESEARCH/ANALYSIS/SURVEY all competed on state scores. Flexible but ANALYSIS and RESEARCH could be starved.
3. **v3 (ab69732, current)**: 3-tier hybrid — scheduled outputs, guaranteed modulo floor for work types, dynamic fill for the rest. Best of both: predictable floor + responsive fill.

---

## Current Design: 3-Tier Routing

### Tier 1 — Scheduled Outputs (fixed intervals, highest priority)

Non-negotiable deliverables the operator expects on a predictable cadence.

| Type | Interval | Output |
|------|----------|--------|
| EVOLVE | Every 30 cycles | Self-assessment, operational scorecard, source discovery |
| INTROSPECTION | Every 15 cycles | World assessment report, journal consolidation |
| SYNTHESIZE | Every 10 cycles | Named situation brief, hypotheses, predictions |

### Tier 2 — Guaranteed Work (modulo floor, coprime intervals)

These fire on their interval unless a Tier 1 type already claimed the slot. Coprime intervals minimize masking between types.

| Type | Interval | Purpose | Coprime with |
|------|----------|---------|--------------|
| ANALYSIS | Every 4 cycles | Pattern detection, graph mining, anomaly detection | 7, 9, 10 |
| RESEARCH | Every 7 cycles | Entity enrichment via Wikipedia/reference sources | 4, 9, 10 |
| CURATE | Every 9 cycles | Event curation from clustered signals | 4, 7, 10 |

When CURATE fires and `INGESTION_SERVICE_ACTIVE=true` (or Redis heartbeat detected), it runs the curation path. Otherwise falls back to legacy ACQUIRE (source fetching).

### Tier 3 — Dynamic Fill (state-scored, remaining cycles)

Cycles not claimed by Tier 1 or Tier 2 are filled dynamically. Only two candidates compete:

| Type | Score | Condition |
|------|-------|-----------|
| CURATE | `min(uncurated / 80, 0.6)` | Only if uncurated > 30 signals |
| SURVEY | 0.4 (fixed) | Always available — analytical desk work |

- CURATE capped at 0.6 to prevent monopolization
- Cooldown: last dynamic type gets score halved (no back-to-back repeats)
- `_uncurated_count` computed in ORIENT from 24h window

### Distribution (over 90 cycles)

```
SURVEY/dynamic : 47 (52%)   — analytical desk work, graph building, hypotheses
ANALYSIS       : 18 (20%)   — pattern detection, anomaly detection
RESEARCH       :  8  (9%)   — entity enrichment
SYNTHESIZE     :  6  (7%)   — deep-dive briefs
CURATE         :  5  (6%)   — guaranteed slots (+ dynamic tier 3 promotions)
INTROSPECTION  :  3  (3%)   — world assessment reports
EVOLVE         :  3  (3%)   — self-improvement
```

ANALYSIS at 20% is intentional — it's the most frequent analytical cycle, reflecting that pattern detection and graph mining benefit from running often on fresh data.

### Masking (Tier 1 overrides Tier 2)

Some Tier 2 slots collide with Tier 1. Over 60 cycles:

```
Cycle 20: SYNTHESIZE masks ANALYSIS
Cycle 28: ANALYSIS masks RESEARCH
Cycle 30: EVOLVE masks INTROSPECTION, SYNTHESIZE
Cycle 36: ANALYSIS masks CURATE
Cycle 40: SYNTHESIZE masks ANALYSIS
Cycle 45: INTROSPECTION masks CURATE
Cycle 56: ANALYSIS masks RESEARCH
Cycle 60: EVOLVE masks INTROSPECTION, SYNTHESIZE, ANALYSIS
```

Coprime intervals keep masking to ~5 slots per 90 cycles for each type — acceptable.

---

## Implementation

### Code Path

```python
# cycle.py — _select_cycle_type()
# Tier 1: fixed schedule
if cn % 30 == 0: return "EVOLVE"
if cn % 15 == 0: return "INTROSPECTION"
if cn % 10 == 0: return "SYNTHESIZE"

# Tier 2: guaranteed modulo
if cn % 4 == 0: return "ANALYSIS"
if cn % 7 == 0: return "RESEARCH"
if cn % 9 == 0: return "CURATE"

# Tier 3: dynamic scoring
scores = {
    "CURATE": min(uncurated / 80, 0.6) if uncurated > 30 else 0.0,
    "SURVEY": 0.4,
}
# Cooldown halves last type's score
return max(scores, key=scores.get)
```

### State Variables (computed in ORIENT)

| Variable | Source | Used By |
|----------|--------|---------|
| `_uncurated_count` | SQL: 24h signals with no event link | Tier 3 CURATE score |
| `_ingestion_heartbeat_detected` | Redis: `legba:ingest:heartbeat` | CURATE vs ACQUIRE fallback |

### Worker Mode

When `CYCLE_TYPE` env var is set, routing is bypassed — the agent runs only that cycle type. Enables parallel workers (e.g., a dedicated CURATE worker for heavy backlog). Worker mode also has a hard CURATE promotion: if `CYCLE_TYPE=survey` and uncurated > 100, promotes to CURATE.

### Logging

Every cycle logs `cycle_scores` to audit OpenSearch:
```json
{
    "event": "cycle_scores",
    "scores": {"CURATE": 0.6, "SURVEY": 0.4},
    "selected": "CURATE",
    "uncurated": 1024
}
```

Tier 1/2 selections log `cycle_type_selected` with the type and cycle number.
