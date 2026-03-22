# Legba — Evaluation Rubrics & Quality Checks

*Procedures for verifying analytical output quality. Run independently from agent operation.*

---

## 1. Entity Resolution Accuracy (Precision/Recall)

**What it measures**: Are entities being resolved correctly? Are duplicates being missed?

### Procedure
1. Pull 50 random entities:
   ```sql
   SELECT id, canonical_name, data->>'entity_type' as type
   FROM entity_profiles ORDER BY RANDOM() LIMIT 50;
   ```
2. For each entity, check:
   - **Correct type?** (Is "Hamas" marked as organization, not person?)
   - **Duplicate exists?** (Search for variants: `SELECT canonical_name FROM entity_profiles WHERE canonical_name ILIKE '%{name}%'`)
   - **Profile has facts?** (`SELECT COUNT(*) FROM facts WHERE subject = '{name}'`)
3. Score:
   - **Type accuracy**: correct types / 50
   - **Dedup miss rate**: entities with unmerged duplicates / 50
   - **Profile completeness**: entities with >3 facts / 50

### Target
- Type accuracy: >95%
- Dedup miss rate: <5%
- Profile completeness: >40%

---

## 2. Event Dedup Effectiveness

**What it measures**: Are duplicates slipping through the 3-tier dedup?

### Procedure
1. Pull 100 random events from the last 7 days:
   ```sql
   SELECT id, title, source_id, created_at::date
   FROM events
   WHERE created_at > NOW() - INTERVAL '7 days'
   ORDER BY RANDOM() LIMIT 100;
   ```
2. For each, search for potential duplicates:
   ```sql
   SELECT id, title, source_id, created_at::date
   FROM events
   WHERE title ILIKE '%{first_5_words}%'
   AND id != '{this_id}'
   AND created_at > NOW() - INTERVAL '14 days';
   ```
3. Manually judge: are any of the matches true duplicates?
4. Score: confirmed duplicate pairs / 100

### Target
- Missed duplicate rate: <3%

---

## 3. Report Quality Rubric

**What it measures**: Are the world assessment reports accurate, grounded, and useful?

### Procedure
Score the 3 most recent reports (from Redis `legba:report_history` or OpenSearch `legba-reports`) on these dimensions:

| Dimension | 1 (Poor) | 3 (Adequate) | 5 (Good) | Weight |
|-----------|----------|--------------|----------|--------|
| **Factual accuracy** | Wrong leaders, wrong events, fabricated claims | Mostly correct, 1-2 errors | All claims verifiable against stored data | 25% |
| **Source grounding** | No citations, vague references | Some event titles cited | Specific events, entities, and data points cited | 20% |
| **Change detection** | Identical to previous report | Notes some changes | Clearly identifies what's new, escalated, or de-escalated | 20% |
| **Coverage balance** | Only covers 1-2 regions | Covers major regions, acknowledges gaps | Proportional coverage with honest gap reporting | 15% |
| **Hypothesis separation** | Inference mixed with facts | Some separation | Hypotheses clearly labeled with confirm/refute criteria | 10% |
| **Actionability** | No priorities or watch items | Lists watch items | Watch items with confidence levels and data gaps | 10% |

### Scoring
- Pull report content: `docker compose -p legba exec redis redis-cli GET legba:report_history | python3 -m json.tool`
- Verify factual claims against the database (entity profiles, events, facts)
- Compare consecutive reports for change detection

### Target
- Average score: >3.5/5.0
- No dimension below 2.0

---

## 4. Entity Freshness Audit

**What it measures**: Are entity profiles current or stale?

### Procedure
1. Pull the 20 most-cited entities (by event link count):
   ```sql
   SELECT ep.canonical_name, COUNT(eel.event_id) as event_count,
          ep.data->>'entity_type' as type
   FROM entity_profiles ep
   JOIN event_entity_links eel ON ep.id = eel.entity_id
   GROUP BY ep.id ORDER BY event_count DESC LIMIT 20;
   ```
2. For each, check LeaderOf/HeadOfState facts:
   ```sql
   SELECT subject, predicate, value, source_cycle, confidence
   FROM facts
   WHERE subject = '{entity_name}'
   AND predicate IN ('LeaderOf', 'HeadOfState', 'HeadOfGovernment', 'President', 'PrimeMinister')
   AND superseded_by IS NULL;
   ```
3. Verify against real-world current leaders (manual lookup)
4. Score:
   - **Leader accuracy**: correct current leaders / total leader facts
   - **Stale fact rate**: facts >200 cycles old / total facts for these entities

### Target
- Leader accuracy: >90%
- Stale fact rate: <20%

---

## 5. Graph Quality Check

**What it measures**: Is the knowledge graph well-formed and useful?

### Procedure
```sql
LOAD 'age'; SET search_path = ag_catalog, public;

-- 1. Unknown type nodes
SELECT * FROM cypher('legba_graph', $$
  MATCH (n) WHERE n.entity_type = 'Unknown' OR n.entity_type = 'unknown'
  RETURN count(n)
$$) AS (cnt agtype);

-- 2. RelatedTo edges (vague)
SELECT * FROM cypher('legba_graph', $$
  MATCH ()-[r:RelatedTo]->() RETURN count(r)
$$) AS (cnt agtype);

-- 3. Isolated nodes (no edges)
SELECT * FROM cypher('legba_graph', $$
  MATCH (n) WHERE NOT EXISTS { MATCH (n)-[]-() }
  RETURN count(n)
$$) AS (cnt agtype);

-- 4. Average degree
SELECT * FROM cypher('legba_graph', $$
  MATCH (n)-[r]-()
  RETURN avg(count(r))
$$) AS (avg_degree agtype);

-- 5. Edge type distribution
SELECT * FROM cypher('legba_graph', $$
  MATCH ()-[r]->() RETURN type(r), count(r) ORDER BY count(r) DESC
$$) AS (rel_type agtype, cnt agtype);
```

### Target
- Unknown nodes: <2% of total
- RelatedTo edges: <5% of total
- Isolated nodes: <5%
- Average degree: >3.0

---

## 6. Prediction Quality (once predictions accumulate)

**What it measures**: Are the agent's predictions tracking reality?

### Procedure
1. Pull open predictions older than 7 days:
   ```sql
   SELECT id, hypothesis, category, confidence, created_at
   FROM predictions WHERE status = 'open'
   AND created_at < NOW() - INTERVAL '7 days';
   ```
2. For each, manually assess:
   - Did the predicted event/pattern materialize?
   - Was the confidence calibrated? (0.8 confidence should be right ~80% of the time)
3. Update status via API: `PUT /api/v2/predictions/{id}` with `confirmed`, `refuted`, or `expired`
4. Over time, track:
   - **Calibration**: are 70% confidence predictions right ~70% of the time?
   - **Resolution rate**: what % of predictions get resolved (vs. expiring without assessment)?

### Target (after 50+ predictions)
- Calibration error: <15% (Brier score proxy)
- Resolution rate: >60%

---

## 7. Source Health Check

**What it measures**: Are sources producing and are they reliable?

### Procedure
```sql
-- Active sources with zero events
SELECT name, status, consecutive_failures, last_success_at::date
FROM sources WHERE status = 'active'
AND id NOT IN (SELECT DISTINCT source_id FROM events WHERE source_id IS NOT NULL)
ORDER BY name;

-- Sources with >50% failure rate (last 7 days)
-- Check ingestion logs or source_fetch_log table

-- Category coverage from active sources
SELECT category, COUNT(DISTINCT source_id) as source_count,
       COUNT(*) as event_count
FROM events
WHERE created_at > NOW() - INTERVAL '7 days'
GROUP BY category ORDER BY event_count DESC;
```

### Target
- Active sources with zero events: <10%
- Category coverage: no category below 2% of total events (except niche categories)

---

## Running the Full Evaluation

Recommended cadence: **weekly** or after major code changes.

```bash
# Quick automated checks (add to a script)
docker compose -p legba exec postgres psql -U legba -d legba -f /path/to/eval_queries.sql

# Manual assessment (human judgment)
# 1. Score 3 recent reports (rubric in section 3)
# 2. Spot-check 20 entity profiles (section 1)
# 3. Check 50 events for missed dupes (section 2)
# 4. Verify top-10 leader facts (section 4)
```

Total time for a full manual evaluation pass: ~2-3 hours.
