# Reified Relationships — Design Spec

*Modeling proxy chains, intent, and intermediaries as first-class graph objects.*
*Created 2026-03-25. For implementation after burn-in 7 cycle 60 audit.*

---

## Problem

The current 30 canonical edge predicates flatten complex multi-actor dynamics into simple pairwise relationships. This loses critical analytical nuance:

- `Iran SuppliesWeaponsTo Israel` — actually Iran → Hamas → (operates in) Israel
- `Iran SuppliesWeaponsTo Saudi Arabia` — actually Iran → Houthis → (attacks) Saudi Arabia
- `US SuppliesWeaponsTo Iran` — actually US → Kurdish separatists → (operates in) Iran

The proxy intermediary, the channel (direct/covert/proxy), and the intent (supportive/hostile) are erased. The structural balance analysis misreads the graph because a hostile weapons supply through a proxy looks identical to a direct allied transfer.

## Solution: Reified Relationships

The relationship itself becomes a first-class node in the AGE graph.

### Schema

```
Nexus Node:
  type: str            — the canonical predicate (SuppliesWeaponsTo, AlliedWith, etc.)
  channel: str         — direct | proxy | covert | institutional
  intent: str          — supportive | hostile | dual-use | neutral
  description: str     — human-readable summary
  confidence: float    — 0-1
  evidence_count: int  — independent corroborating signals
  valid_from: str      — ISO timestamp
  valid_until: str     — ISO timestamp or null
  source_cycle: int    — cycle that created this

Connecting Edges:
  PARTY_TO         — actor entity → Nexus (who initiates/conducts)
  CONDUCTED_VIA    — Nexus → intermediary entity (the proxy)
  TARGETS          — Nexus → target entity (who is affected)
  EVIDENCED_BY     — Nexus → signal/event (provenance)
```

### Example: Iran → Hamas → Israel

```cypher
CREATE (op:Nexus {
  type: 'SuppliesWeaponsTo',
  channel: 'proxy',
  intent: 'hostile',
  description: 'Iranian weapons supply to Hamas for operations against Israel',
  confidence: 0.85,
  valid_from: '2023-01-01'
})

MATCH (iran:Entity {name: 'Iran'}), (hamas:Entity {name: 'Hamas'}), (israel:Entity {name: 'Israel'})
CREATE (iran)-[:PARTY_TO {role: 'supplier'}]->(op)
CREATE (op)-[:CONDUCTED_VIA]->(hamas)
CREATE (op)-[:TARGETS]->(israel)
```

### What This Enables

**Proxy chain queries:**
```cypher
MATCH (a:Entity)-[:PARTY_TO]->(op:Nexus)-[:CONDUCTED_VIA]->(proxy)-[:TARGETS]->(target)
WHERE op.channel = 'proxy' AND op.intent = 'hostile'
RETURN a.name, proxy.name, target.name, op.type
```
→ "Iran supplies weapons via Hamas targeting Israel"

**Intent-aware structural balance:**
The structural balance analysis uses `op.intent` to determine edge sign — a hostile SuppliesWeaponsTo counts as negative even though the predicate sounds positive. No more false alliance detection.

**Temporal reconstruction:**
Nexuses have `valid_from`/`valid_until`. Timeline scrubbing shows which proxy relationships were active when.

**Evidence provenance:**
Every Nexus links to the signals/events that support it. Click the relationship → see the evidence chain.

### Coexistence with Flat Edges

Existing flat edges (AlliedWith, HostileTo, etc.) remain for simple queries and backward compatibility. Reified Nexuses are the "deep view" for analytical work. The graph has two levels:
- **Simple**: `(Iran)-[:HostileTo]->(Israel)` — fast, glanceable
- **Deep**: `(Iran)-[:PARTY_TO]->(op:Nexus {type: 'SuppliesWeaponsTo', channel: 'proxy'})-[:CONDUCTED_VIA]->(Hamas)-[:TARGETS]->(Israel)` — full nuance

The agent can query either level. The UI can toggle between simple and deep views.

### Implementation Plan

**1. Schema extension (~2h)**
- New node label `Nexus` in AGE
- Three new edge types: PARTY_TO, CONDUCTED_VIA, TARGETS, EVIDENCED_BY
- Schema bootstrap in structured.py

**2. Agent tooling (~4h)**
- New tool: `graph_store_nexus(type, channel, intent, description, actor, target, via, evidence_id)`
- Or extend existing `graph_store` with a `nexus` mode
- Named query modes for Nexus nodes in graph_query

**3. Prompt updates (~2h)**
- Entity guidance: when storing relationships, assess channel/intent/intermediary
- SURVEY/ANALYSIS prompts: use nexuses for complex relationships, flat edges for simple ones
- Criteria: if a relationship involves a proxy or covert channel, use a Nexus node

**4. Subconscious validation (~2h)**
- Relationship validation checks for implausible combinations (hostile SuppliesWeaponsTo between allies)
- Auto-detect proxy chains from existing flat edges that should be reified

**5. UI integration (~1d)**
- Graph viewer: Nexus nodes rendered as diamonds or hexagons (distinct from entity circles)
- Nexus detail modal: shows full chain with evidence
- Toggle between simple/deep graph view

**6. Structural balance update (~2h)**
- Use intent property for edge signing instead of just predicate type
- Hostile nexuses count as negative edges regardless of predicate

### Estimated Effort
~3 days total. No infrastructure changes — AGE, TimescaleDB, and the existing graph tooling handle it.

### Dependencies
- Burn-in 7 cycle 60 audit (verify current graph quality)
- Seed data cleanup (remove stale/reversed facts)
- Clustering quality investigation (understand signal-event mislinks)

---

*This spec becomes implementation work after the burn-in audit. The agent is already reasoning about proxy chains and intent — this gives it a place to store that nuance structurally.*
