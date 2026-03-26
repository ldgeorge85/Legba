# Legba — Nexus Relationships & Temporal Knowledge

*How Legba models complex relationships and tracks knowledge over time.*

---

## 1. The Problem with Flat Edges

Legba's knowledge graph connects entities with typed, directed edges — `AlliedWith`, `HostileTo`, `SuppliesWeaponsTo`, and 27 other canonical predicates. For many relationships this works well: `France AlliedWith Germany` is unambiguous.

But some relationships resist flattening:

- **`Iran SuppliesWeaponsTo Israel`** — this edge is technically wrong. Iran supplies weapons to Hamas, and Hamas operates against Israel. The proxy chain (Iran -> Hamas -> Israel) is the intelligence; the flat edge erases it.
- **`Iran SuppliesWeaponsTo Saudi Arabia`** — same problem. The real chain runs through the Houthis.
- **`US SuppliesWeaponsTo Iran`** — not directly. The US funds Kurdish separatists who operate inside Iran. Without the intermediary, this looks like allied support when it is hostile action through a covert proxy.

The predicate alone cannot distinguish hostile supply from allied support. Structural balance analysis (`structural_balance.py`) misreads these edges because a hostile weapons transfer through a proxy looks identical to a direct allied transfer — both are `SuppliesWeaponsTo` with the same sign.

Temporal relationships create a second problem. An edge like `Russia AlliedWith Syria` has no time dimension. If the alliance ends, the edge persists as current truth. Without temporal bounds, the graph accumulates stale assertions that pollute analytical queries and context injection.

Nexus nodes and the temporal fact system solve these two problems independently but composably.

---

## 2. Nexus Nodes — Reified Relationships

### What a Nexus Is

A Nexus is a relationship promoted to a first-class graph node with its own properties. Instead of a simple directed edge `(A)-[:SuppliesWeaponsTo]->(B)`, the relationship becomes a node that can carry metadata (channel, intent, confidence, temporal bounds) and connect to multiple entities with different roles (actor, target, intermediary).

The term "reified" means treating a relationship as a thing — something you can describe, track over time, and link to evidence.

### Schema

Nexuses live in two places: a Postgres `nexuses` table for efficient querying, and an AGE graph node (label `Nexus`) for traversal.

**Postgres table (`nexuses`)**:

| Column | Type | Description |
|---|---|---|
| `id` | UUID | Primary key |
| `nexus_type` | TEXT | Canonical predicate (`SuppliesWeaponsTo`, `FundedBy`, etc.) |
| `channel` | TEXT | `direct`, `proxy`, `covert`, `institutional` |
| `intent` | TEXT | `supportive`, `hostile`, `dual-use`, `neutral` |
| `description` | TEXT | Human-readable summary |
| `actor_entity` | TEXT | Who initiates or conducts |
| `target_entity` | TEXT | Who is affected |
| `via_entity` | TEXT | Intermediary (nullable) |
| `confidence` | REAL | 0.0–1.0 |
| `evidence_count` | INT | Number of corroborating signals |
| `valid_from` | TIMESTAMPTZ | When the relationship became active |
| `valid_until` | TIMESTAMPTZ | When it ended (NULL = ongoing) |
| `source_cycle` | INT | Cycle that created this nexus |
| `created_at` | TIMESTAMPTZ | Row creation time |

The Postgres table exists because AGE's Cypher property filtering is limited. The `nexuses` table is the queryable store; the AGE node is for graph traversal.

**AGE graph node**:

```
(op:Nexus {
    op_id: '<uuid>',
    type: '<nexus_type>',
    channel: '<channel>',
    intent: '<intent>',
    description: '<text>',
    confidence: <float>
})
```

### Connecting Edges

Four edge types connect a Nexus to other graph entities:

| Edge | Direction | Meaning |
|---|---|---|
| `PartyTo` | Actor entity -> Nexus | Who initiates/conducts. Carries `role: 'actor'`. |
| `Targets` | Nexus -> Target entity | Who is affected by the relationship. |
| `ConductedVia` | Nexus -> Intermediary entity | The proxy, cutout, or channel. |
| `EvidencedBy` | Nexus -> Signal/Event | Provenance chain (design-spec'd, evidence_id stored in Postgres). |

### Example: Iran -> Hamas -> Israel

The canonical example. Iran supplies weapons to Hamas, which uses them in operations against Israel.

```cypher
-- Create the Nexus node
CREATE (op:Nexus {
    op_id: '<uuid>',
    type: 'SuppliesWeaponsTo',
    channel: 'proxy',
    intent: 'hostile',
    description: 'Iranian weapons supply to Hamas for operations against Israel',
    confidence: 0.85
})

-- Connect the actors
MATCH (iran {name: 'Iran'}), (op:Nexus {op_id: '<uuid>'})
CREATE (iran)-[:PartyTo {role: 'actor'}]->(op)

MATCH (op:Nexus {op_id: '<uuid>'}), (israel {name: 'Israel'})
CREATE (op)-[:Targets]->(israel)

MATCH (op:Nexus {op_id: '<uuid>'}), (hamas {name: 'Hamas'})
CREATE (op)-[:ConductedVia]->(hamas)
```

Via the `graph_store_nexus` tool, the agent issues a single call:

```
graph_store_nexus(
    nexus_type="SuppliesWeaponsTo",
    channel="proxy",
    intent="hostile",
    description="Iranian weapons supply to Hamas for operations against Israel",
    actor="Iran",
    target="Israel",
    via="Hamas",
    confidence=0.85,
    evidence_id="<signal-uuid>"
)
```

The handler creates both the Postgres row and the AGE graph structure in one operation. If the AGE graph creation partially fails (e.g., an entity node does not exist yet), the Postgres row is still written and the agent is told to create the missing entity first via `graph_store`.

### Coexistence with Flat Edges

Flat edges and Nexus nodes coexist in the same graph. They serve different purposes:

- **Simple view** (flat edges): `(Iran)-[:HostileTo]->(Israel)` — fast, glanceable, good for structural overview queries like centrality and clustering.
- **Deep view** (Nexus nodes): `(Iran)-[:PartyTo]->(op:Nexus {type: 'SuppliesWeaponsTo', channel: 'proxy'})-[:ConductedVia]->(Hamas)` — full nuance for analytical work.

**When to use which** (from prompt guidance in `templates.py`):

- Use `graph_store_nexus` when the relationship goes through an intermediary, the channel is not direct (proxy, covert, institutional), or the intent differs from what the predicate implies.
- Use regular `graph_store` for simple, direct relationships (`US AlliedWith UK`, `France MemberOf NATO`).

The agent makes this decision based on relationship complexity. The prompts for SURVEY, ANALYSIS, and SYNTHESIZE cycle types all include `graph_store_nexus` in their tool sets and provide explicit guidance on the criteria.

### How It's Used Across Layers

**Conscious layer (agent)**:
- `graph_store_nexus` tool for creating new nexuses with full metadata.
- `graph_query` with three dedicated modes: `proxy_chains` (all proxy nexuses), `hostile_nexuses` (all hostile-intent nexuses), `entity_nexuses` (all nexuses involving a specific entity as actor, target, or intermediary).
- All six analytical cycle types (SURVEY, ANALYSIS, SYNTHESIZE, CURATE, RESEARCH, EVOLVE) have `graph_store_nexus` in their tool sets.

**Subconscious layer (SLM validation)**:
- The subconscious service (`subconscious/service.py`) runs a reification heuristic on every proposed edge. When a new `SuppliesWeaponsTo` or `FundedBy` edge involves entities that already have a `HostileTo` relationship (checked in both `proposed_edges` and the `nexuses` table), it logs a `REIFICATION RECOMMENDED` warning. This flags potential proxy relationships for the agent to reify.
- The heuristic runs automatically — the subconscious does not create nexuses itself, it flags candidates.

**Maintenance layer**:
- `NexusDecayManager` (`maintenance/nexus_decay.py`) mirrors the fact decay pattern: nexuses not updated in 30 days get confidence decremented by 0.05 per maintenance cycle, floored at 0.1. Only nexuses that are still temporally active (`valid_until IS NULL OR valid_until > NOW()`) are decayed.
- `MetricsCollector` (`maintenance/metrics.py`) tracks `nexuses_total`, `nexuses_by_channel`, and `nexuses_by_intent` in TimescaleDB for Grafana dashboards.
- `IntegrityVerifier` (`maintenance/integrity.py`) checks for orphaned nexuses — nexuses whose `actor_entity` or `target_entity` no longer exists in `entity_profiles`.

**Structural balance**:
- `structural_balance.py` queries the `nexuses` table alongside flat `AlliedWith` and `HostileTo` edges. The `intent` property maps to edge sign: `supportive` = +1, `hostile` = -1, `dual-use` and `neutral` are excluded (sign 0). This means a hostile `SuppliesWeaponsTo` through a proxy correctly counts as a negative edge — no more false alliance detection from ambiguous predicates.

**Priority stack**:
- `priority.py` boosts situation scores for entities involved in covert or dual-use nexuses. It queries `nexuses WHERE (channel = 'covert' OR intent = 'dual-use')` created in the last 48 hours, then adds 0.05 per matching entity (capped at 0.15) to the situation's composite score. Covert and dual-use nexuses are inherently analytically interesting — they represent hidden dynamics worth tracking.

---

## 3. Temporal Fact System

Facts in Legba are structured triples — `(subject, predicate, value)` — stored in Postgres with temporal bounds and confidence scores.

### Fact Lifecycle

Every fact has four lifecycle states:

1. **Active**: `superseded_by IS NULL` and (`valid_until IS NULL` or `valid_until > NOW()`). Injected into agent context.
2. **Expired**: `valid_until` is in the past. Automatically marked by `FactDecayManager`. Excluded from context injection.
3. **Superseded**: `superseded_by` points to the replacement fact's UUID. The old fact's `valid_until` is set to `NOW()` at supersede time. Excluded from all queries.
4. **Auto-closed**: Open-ended facts with no supporting signals in 30 days get `valid_until = NOW()` set by maintenance. Recorded with `auto_closed_reason: "no_supporting_signals_30d"` in the JSONB `data` field.

The key columns:

| Column | Purpose |
|---|---|
| `valid_from` | When the fact became true (defaults to `NOW()` in DB if not set) |
| `valid_until` | When it stops being true (NULL = open-ended, ongoing) |
| `superseded_by` | UUID of the fact that replaced this one |
| `confidence` | 0.0–1.0, subject to decay |
| `evidence_set` | JSONB array of signal UUIDs that support this fact |
| `confidence_components` | JSONB tracking decay history |

### Single-Value Predicates

Some predicates are inherently single-valued per subject — a country can only have one capital, one GDP figure, one population. These are defined in `structured.py`:

```python
_SINGLE_VALUE_PREDICATES = frozenset({
    "LeaderOf", "HeadOfState", "HeadOfGovernment", "President",
    "PrimeMinister", "SupremeLeader", "Monarch",
    "Capital", "Population", "GDP", "Area", "Currency",
    "GovernmentType", "OfficialLanguage", "SignatoryTo",
})
```

When the agent stores a new fact with a single-value predicate and the same subject but a different value, the old fact is automatically superseded:

- `France Capital Paris` is stored.
- Later, `France Capital Lyon` is stored (hypothetically).
- The system finds the existing active fact `France Capital Paris`, sets `superseded_by = <new_fact_id>` and `valid_until = NOW()`, and inserts the new fact.
- No manual `memory_supersede` call needed.

This works for same-subject, same-predicate, different-value triples only.

### Volatile Predicates (Leadership)

Leadership predicates are a strict subset of single-value predicates with an additional cross-subject supersede rule:

```python
_VOLATILE_PREDICATES = frozenset({
    "LeaderOf", "HeadOfState", "HeadOfGovernment", "President",
    "PrimeMinister", "SupremeLeader", "Monarch",
})
```

The difference: volatile predicates supersede across subjects, not just within the same subject.

When `Donald Trump LeaderOf United States` is stored:
- Any existing `X LeaderOf United States` where X != `Donald Trump` is superseded (the previous leader is replaced).
- Any existing `United States LeaderOf X` (wrong direction) is also superseded (cleans up reversed facts).

The old facts get `valid_until = NOW()` and `superseded_by = <new_fact_id>`. Corresponding graph edges (`LeaderOf`) are also removed from AGE via `graph.remove_relationship()` as a best-effort cleanup.

### Context Injection Filtering

Facts are injected into the agent's prompt via two query paths, both of which enforce temporal filtering:

**`query_facts()`** — general-purpose fact retrieval:
```sql
WHERE superseded_by IS NULL
  AND (valid_until IS NULL OR valid_until > NOW())
ORDER BY confidence DESC, created_at DESC
```

**`query_facts_recent()`** — recent-cycle facts (ensures the agent sees its own recent work):
```sql
WHERE superseded_by IS NULL
  AND (valid_until IS NULL OR valid_until > NOW())
  AND source_cycle >= <current_cycle - lookback>
ORDER BY source_cycle DESC, created_at DESC
```

Both paths exclude superseded facts and facts with `valid_until` in the past. The agent never sees expired or replaced facts in its context window.

When temporal bounds are present, the assembler (`assembler.py`) appends them to the fact line in the injected context:
```
- Donald Trump LeaderOf United States [from 2025-01-20, until 2029-01-20]
- Joe Biden LeaderOf United States [from 2021-01-20, until 2025-01-20]  ← excluded (valid_until past)
```

Qdrant stores fact embeddings for semantic retrieval. When a fact is superseded, `remove_fact_embedding()` deletes its vector from Qdrant so stale facts do not appear in similarity searches either.

### The `store_fact` Tool

The agent stores facts via the `store_fact` tool with these parameters:

| Parameter | Required | Description |
|---|---|---|
| `subject` | Yes | Entity the fact is about |
| `predicate` | Yes | Must be a canonical predicate (normalized, non-canonical rejected with suggestions) |
| `value` | Yes | The value or target |
| `confidence` | No | 0.0–1.0, default 0.7. Calibration: 0.3–0.4 single source, 0.5–0.6 multiple, 0.7–0.8 strong, 0.9+ verified. |
| `evidence_id` | No | Signal/event UUID for provenance |
| `valid_from` | No | ISO 8601 timestamp. Defaults to now. |
| `valid_until` | No | ISO 8601 timestamp. NULL = open-ended. |

Prompt guidance tells the agent to always set `valid_from` and `valid_until` for time-bounded assertions (leadership positions, treaty terms, economic figures). The example from the SURVEY prompt:

```
store_fact(
    subject="Donald Trump",
    predicate="LeaderOf",
    value="United States",
    valid_from="2025-01-20T00:00:00Z",
    confidence=0.95
)
```

The storage path in `structured.py` performs several checks before insertion:
1. Predicate normalization (aliases resolved to canonical forms).
2. Rejection of non-canonical predicates (with fuzzy-matched suggestions).
3. Dedup check — if the exact triple already exists as an active fact, skip insert (update confidence if higher).
4. Contradiction detection — checks existing facts for predicate contradictions (`AlliedWith` vs `HostileTo`) and value contradictions (single-valued predicates). Auto-creates hypotheses when contradictions are detected and signal references meet a threshold.
5. Volatile predicate auto-supersede (cross-subject leadership replacement).
6. Single-value predicate auto-supersede (same-subject value replacement).
7. Embedding generation and storage in Qdrant for semantic retrieval.

### Fact Decay

`FactDecayManager` (`maintenance/fact_decay.py`) runs two maintenance operations:

**1. Expiration of explicitly bounded facts:**
Facts with `valid_until` in the past are marked expired (`data.expired = "true"`). These were already excluded from context injection by the temporal filter; the expiry flag is an audit trail.

**2. Auto-closure of stale open-ended facts:**
Facts with `valid_until IS NULL` (open-ended), older than 30 days, and with no supporting signals in that period get `valid_until = NOW()`. The system checks evidence two ways:
- If the fact has an `evidence_set` (JSONB array of signal UUIDs), it checks whether any of those signals were ingested recently.
- If no `evidence_set`, it falls back to a subject-matching heuristic against recent signals.

**3. Confidence decay:**
Facts with no corroboration in 30 days get confidence decremented by 0.05 per maintenance cycle, floored at 0.1. This affects facts that are:
- Not superseded
- Not expired
- Still temporally active (`valid_until IS NULL OR valid_until > NOW()`)
- Have `confidence > 0.1`
- Have not been updated in 30 days

The decay is recorded in `confidence_components.decay` and `data.last_confidence_decay` for full audit trail.

---

## 4. How They Work Together

Nexus relationships carry the same temporal bounds as facts. A proxy chain that was active in 2023 but ended in 2024 has `valid_until` set. The structural balance computation filters on `(valid_until IS NULL OR valid_until > NOW())`, so expired nexuses do not distort the current balance score. The priority stack similarly filters covert/dual-use nexuses to the last 48 hours.

The confidence decay mechanisms run in parallel: `NexusDecayManager` for nexuses, `FactDecayManager` for facts. Both use the same 30-day window and 0.05 decrement. Both floor at 0.1 to prevent complete erasure — a decayed fact or nexus is still visible to the agent as low-confidence knowledge, not silently deleted.

The evidence chain traces from analytical products through the graph:

```
Situation Brief
  └── Situation (linked events)
        └── Event (clustered signals)
              └── Signal (raw data from source)

Nexus (reified relationship)
  ├── PartyTo ← Actor entity
  ├── Targets → Target entity
  ├── ConductedVia → Intermediary entity
  └── evidence_id → Signal/Event UUID

Fact (structured triple)
  ├── evidence_set → [Signal UUIDs]
  └── superseded_by → newer Fact UUID (temporal chain)
```

When the agent reconstructs a situation for a time window — say, "what was the Iran-Israel dynamic in Q1 2024?" — it can:
1. Query nexuses with `valid_from <= '2024-03-31' AND (valid_until IS NULL OR valid_until >= '2024-01-01')` to find active proxy chains in that period.
2. Query facts with the same temporal filter to find leadership positions, alliances, and economic data that were current at the time.
3. Trace evidence from nexuses and facts back to source signals for provenance.

The temporal system ensures the graph reconstructs correctly for any time window. Facts and nexuses that have ended are not erased — they retain their `valid_until` timestamp and `superseded_by` chain, making the full history reconstructable.

---

*See also: ANALYTICAL_FRAMEWORK.md for the broader analytical methodology, REIFIED_RELATIONSHIPS.md for the original design spec.*
