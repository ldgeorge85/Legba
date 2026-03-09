# Data Quality: Findings & Improvement Plan

*Snapshot: Cycle ~370 | 2026-03-09*

## What We Found

Manual audit of data stores after ~370 cycles revealed systemic quality issues
across all data types. The root causes are a mix of missing code-level enforcement,
inconsistent LLM output, and prompt gaps.

### Facts (worst offender)

**Before cleanup**: 1,927 rows, only 1,314 unique (32% bloat).

Problems:
1. **No dedup at any layer** — `store_fact()` uses `ON CONFLICT (id)`, so only
   catches UUID collisions. Same triple (subject, predicate, value) is stored
   repeatedly across cycles. "Isaac Herzog LeaderOf Israel" appeared 19 times.
2. **Predicate drift** — The LLM outputs predicates inconsistently across cycles:
   `HostileTo`, `hostile_to`, `hostile to`, `hostileTo`, `is hostile to`,
   `are hostile to`, `relation_HostileTo_United States`. Eight variant forms
   for a single relationship type. No normalization on insert.
3. **Value pollution** — Date suffixes appended to values: `Iran (since 2026-03-08)`,
   `Iran since 2020-01`, plain `Iran`. These are treated as different facts.
4. **Factual errors** — "Israel HostileTo United States" (conf 0.6-1.0),
   "Iran HostileTo Hezbollah" (Iran *supports* Hezbollah). LLM hallucination
   stored as high-confidence facts with no validation layer.
5. **Facts created in REFLECT** — `cycle.py:838-854` creates facts from the
   reflection JSON, which the LLM generates freeform. No normalization,
   no dedup check, no predicate validation. Just raw insert.

### Events

**Before cleanup**: 334 events, 119 were title-duplicates (36% bloat).

Problems:
1. **Dedup only on timestamp match** — `event_store` checks ±1 day window with
   50% Jaccard title similarity, but only if `event_timestamp` is provided.
   If omitted, event goes straight through.
2. **Same headline ingested across cycles** — "Dozens killed as Israeli special
   forces raid Lebanese village" appeared 38 times. The agent re-parses the same
   RSS feed items that haven't rotated out yet.
3. **No URL/guid dedup** — RSS items have unique GUIDs. We don't track or dedup on them.

### Entities

**Before cleanup**: 249 entities, 12 problematic.

Problems:
1. **Event titles as entities** — `Event_Israel_Beirut_2026-03-08`, full headline
   strings stored as entity type `other`. Created during reflection entity storage.
2. **Name variants** — "Gaza" vs "Gaza Strip", "Netanyahu" vs "Benjamin Netanyahu",
   "Russian Federation" vs "Russia". The 0.85 fuzzy threshold catches close matches
   but not semantic equivalents with different string forms.
3. **Vague entities** — "Gulf", "Gulf nations", "Gulf countries", "Gulf region",
   "migrants", "German football fans". Too vague to be useful graph nodes.
4. **Entity type inconsistency** — Same entity created as different types across
   cycles (e.g., `organization` vs `armed_group` for "Israeli forces").

### Sources

**Before cleanup**: 149 sources, 68 duplicates.

Root cause identified and fixed: `get_sources()` silently returned `[]` on any
DB error, bypassing the dedup check entirely. Fixed with direct DB queries for
name, normalized URL, and domain matching.

### Goals

**Before cleanup**: 22 goals, 6 duplicate completed goals.

Problems:
1. **Duplicate goals** — Three "connect isolated nodes" variants, two "enrich Iran",
   two "enrich Sudan". Agent creates new goals instead of finding existing ones.
2. **Progress >100%** — User reported goals at 1000%+. The `progress_pct` field
   is set by the LLM in reflection output with no clamping.
3. **Stale completed goals** — Completed goals accumulate indefinitely. No archival.

---

## Root Cause Analysis

The problems trace to three layers:

### Layer 1: No Code Enforcement on Facts

Graph relationships go through `normalize_relationship_type()` — 30 canonical types
with 70+ aliases and fuzzy matching. **Facts have nothing.** The predicate is whatever
string the LLM outputs, stored verbatim. This is the single biggest gap.

- Graph: `HostileTo` → normalized via alias table → canonical form
- Facts: `hostile_to` → stored as-is → duplicate of `HostileTo` → stored again

### Layer 2: Prompt Guidance Without Teeth

The prompts say "check before creating" but the agent frequently skips the check step,
especially under token pressure in long REASON chains. Prompts can't enforce behavior —
they're suggestions the LLM may or may not follow.

Key prompt gaps:
- No predicate vocabulary list for facts (the agent invents forms each cycle)
- No guidance on value format (don't append dates, don't include qualifiers)
- No instruction to use consistent casing
- REFLECT phase prompt doesn't constrain fact output schema

### Layer 3: No Post-Hoc Cleanup

There's no automated mechanism to catch what slips through. Introspection cycles
review knowledge gaps but don't audit data quality. No dedupe pass, no normalization
sweep, no consistency check.

---

## Improvement Plan

### A. Fact Predicate Normalization (code — high impact)

Add a `normalize_fact_predicate()` function analogous to `normalize_relationship_type()`.
Reuse the same canonical types + alias table from graph_tools.py where applicable.

**Where to apply:**
1. `cycle.py:838` — Before `Fact()` creation in `_store_reflection_facts()`
2. `structured.py:store_fact()` — As a safety net on every insert

**Implementation:**
```python
# Map all known variants to canonical PascalCase forms
FACT_PREDICATE_ALIASES = {
    "hostile_to": "HostileTo", "hostile to": "HostileTo", "hostileTo": "HostileTo",
    "is hostile to": "HostileTo", "are hostile to": "HostileTo",
    "allied_with": "AlliedWith", "allied with": "AlliedWith",
    "leader_of": "LeaderOf", "leader of": "LeaderOf", "leaderOf": "LeaderOf",
    "is leader of": "LeaderOf", "is supreme leader of": "LeaderOf",
    "located_in": "LocatedIn", "located in": "LocatedIn", "locatedIn": "LocatedIn",
    "operates_in": "OperatesIn", "operates in": "OperatesIn", "operatesIn": "OperatesIn",
    "part_of": "PartOf", "part of": "PartOf", "partOf": "PartOf",
    "related_to": "RelatedTo", "relatedTo": "RelatedTo",
    "supplies_weapons_to": "SuppliesWeaponsTo",
    # ... extend from RELATIONSHIP_ALIASES in graph_tools.py
}

def normalize_fact_predicate(predicate: str) -> str:
    """Normalize fact predicates to canonical PascalCase."""
    stripped = predicate.strip()
    # Direct alias lookup
    if stripped in FACT_PREDICATE_ALIASES:
        return FACT_PREDICATE_ALIASES[stripped]
    # Case-insensitive alias lookup
    lower = stripped.lower()
    for alias, canonical in FACT_PREDICATE_ALIASES.items():
        if alias.lower() == lower:
            return canonical
    # Already canonical — pass through
    return stripped
```

### B. Fact Dedup on Insert (code — high impact)

Change `store_fact()` to use `ON CONFLICT` on a **functional unique index**
instead of just the UUID.

```sql
CREATE UNIQUE INDEX IF NOT EXISTS idx_facts_triple
ON facts (lower(subject), lower(predicate), lower(value));
```

Then change the INSERT to:
```sql
INSERT INTO facts (id, subject, predicate, value, confidence, source_cycle, data, created_at)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
ON CONFLICT (lower(subject), lower(predicate), lower(value)) DO UPDATE SET
    confidence = GREATEST(facts.confidence, EXCLUDED.confidence),
    data = EXCLUDED.data,
    updated_at = NOW()
WHERE EXCLUDED.confidence > facts.confidence
```

This ensures: one row per unique triple, highest confidence wins, no duplicates.

### C. Fact Value Sanitization (code — medium impact)

Strip date suffixes and qualifiers from values before storage:
```python
def normalize_fact_value(value: str) -> str:
    """Strip temporal qualifiers from fact values."""
    import re
    # Remove "(since YYYY-MM-DD)" and "since YYYY-MM" suffixes
    cleaned = re.sub(r'\s*\(since [0-9]{4}[-/][0-9]{2}([-/][0-9]{2})?\)', '', value)
    cleaned = re.sub(r'\s+since [0-9]{4}[-/][0-9]{2}', '', cleaned)
    return cleaned.strip()
```

Temporal data belongs in `since`/`until` properties, not baked into the value string.

### D. Event Dedup Hardening (code — medium impact)

1. **Always check title similarity**, even without `event_timestamp`. Fall back
   to ±7 day window or just check last 100 events.
2. **Track RSS GUIDs** — Add a `guid` column to the events table. Dedup on GUID
   before title similarity (fast path).
3. **Raise threshold** — 50% Jaccard is low. Consider 40% for flagging + 60% for
   hard reject, or add a secondary check on actors/locations overlap.

### E. REFLECT Prompt Tightening (prompt — high impact)

The REFLECT phase prompt should constrain fact output format. Add to the reflection
instructions:

```
When outputting facts_learned:
- Use PascalCase predicates from this vocabulary: LeaderOf, HostileTo, AlliedWith,
  LocatedIn, OperatesIn, PartOf, SuppliesWeaponsTo, MemberOf, BordersWith, Capital,
  Population, GDP, TradesWith, SanctionedBy, OccupiedBy, DisplacedFrom, MediatesBetween
- Do NOT append dates to values (use a separate "since" field if temporal)
- Do NOT use variant forms like "hostile_to" or "is hostile to" — use "HostileTo"
- Values should be entity names only, not sentences or qualifiers
- Check: would this fact duplicate one you already stored? If yes, skip it.
```

### F. Introspection Dedupe Phase (code+prompt — medium impact)

Add a dedicated data quality audit step to the introspection cycle. During
introspection, the agent already has access to `graph_query`, `entity_inspect`,
and `memory_query`. Add a structured prompt section:

```
DATA QUALITY AUDIT:
Review your knowledge base for quality issues:
1. Query facts for your top entities — look for duplicate or contradictory assertions
2. Use memory_supersede to replace outdated facts with current ones
3. Check entity profiles for incomplete or conflicting data
4. Report any data quality issues found in your journal
```

This leverages the agent's existing tools rather than adding new code. The agent
can use `memory_supersede` (already implemented) to clean up facts it identifies
as outdated.

**Alternatively** (code-only, no agent involvement): Add an automated cleanup
function that runs at the start of each introspection cycle:
- Dedupe facts by (subject, predicate, value), keep highest confidence
- Normalize all predicates to canonical forms
- Flag low-confidence facts (< 0.3) for review
- Archive completed goals older than N cycles

### G. Goal Dedup (code — low impact)

Add a name-similarity check to `goal_create` in the tools, similar to source_add:
query existing active goals, check description similarity (Jaccard on words),
return `duplicate_detected` if > 60% overlap.

### H. Entity Type Consistency (prompt — low impact)

Add a canonical entity type vocabulary to the prompts:
```
Entity types: country, person, organization, armed_group, political_party, location, event
Do NOT use: other, unknown, region (use "location" for regions)
```

---

## Priority Order

| # | Fix | Impact | Effort | Layer |
|---|-----|--------|--------|-------|
| 1 | Fact predicate normalization | High | Low | Code |
| 2 | Fact triple unique index | High | Low | DB |
| 3 | Fact value sanitization | Medium | Low | Code |
| 4 | REFLECT prompt predicate vocabulary | High | Low | Prompt |
| 5 | Event dedup without timestamp | Medium | Medium | Code |
| 6 | Introspection data audit prompt | Medium | Low | Prompt |
| 7 | Goal dedup | Low | Low | Code |
| 8 | Entity type vocabulary in prompts | Low | Low | Prompt |
| 9 | RSS GUID tracking | Medium | Medium | Code+DB |

Items 1-4 are the highest-value fixes and address the bulk of the issues found.
They can all be implemented in one pass.
