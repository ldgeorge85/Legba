# Analytical Framework

This document describes the analytical methodology behind Legba -- the theories, models, and formulas that drive its intelligence analysis. It is written for analysts and architects who need to understand *why* the system makes the decisions it does, without reading any code.

---

## JDL Data Fusion Model

Legba's architecture follows the Joint Directors of Laboratories (JDL) data fusion model, adapted for open-source intelligence analysis. The JDL model defines six levels of information processing, each building on the output of the level below.

### The Six Levels in Legba's Context

| Level | JDL Name | Legba Equivalent | What It Does |
|---|---|---|---|
| L0 | Signal Refinement | Ingestion + signal validation | Raw data acquisition, normalization, deduplication, quality scoring. Transforms raw RSS/API feeds into standardized signals with confidence scores. |
| L1 | Entity Assessment | Entity extraction + resolution | Identifies and disambiguates entities (people, organizations, locations) mentioned in signals. Links them to canonical profiles. Builds the knowledge graph vertices. |
| L2 | Situation Assessment | Event clustering + situation detection | Groups related signals into events, detects lifecycle transitions, identifies higher-order situations from event clusters. Builds graph edges between entities and events. |
| L3 | Impact Assessment | Hypothesis engine + priority stack | Generates and evaluates competing explanations for observed patterns. Ranks situations by composite scoring. Assesses impact on goals and watchlists. |
| L4 | Process Refinement | Calibration + adversarial detection | Monitors the system's own performance -- confidence calibration, classification accuracy, adversarial signal detection. Feeds corrections back into lower levels. |
| L5 | User Refinement | Situation briefs + priority advisories | Produces human-consumable analytical products (situation briefs, priority stacks, watchlist alerts) that support decision-making. |

### Cognitive Layer x Fusion Level Matrix

Each of Legba's three cognitive layers contributes to specific fusion levels. This matrix shows the coverage:

|  | L0 Signal | L1 Entity | L2 Situation | L3 Impact | L4 Process | L5 User |
|---|---|---|---|---|---|---|
| **Unconscious** (maintenance daemon) | Corroboration scoring | Entity GC, duplicate detection | Event lifecycle, mechanical situation detection | Fact decay, state propagation | Integrity checks, adversarial detection, calibration | Metric collection for Grafana |
| **Subconscious** (SLM service) | Signal validation (specificity, consistency) | Entity resolution, relationship validation | SLM situation detection (narrative coherence) | Fact refresh (corroboration/contradiction) | Classification refinement, source reliability | Differential briefing for agent |
| **Conscious** (agent cycles) | SURVEY signal collection | RESEARCH entity enrichment | CURATE event curation | ANALYSIS hypothesis evaluation, SYNTHESIZE situation briefs | INTROSPECTION self-assessment, EVOLVE goal management | Situation briefs, priority advisories |

### Slotting New Capabilities

When adding a new analytical capability, identify its JDL level and assign it to the appropriate cognitive layer:

- **Deterministic, no LLM needed** -- unconscious (maintenance daemon)
- **Needs lightweight language understanding** -- subconscious (SLM service)
- **Needs full reasoning, judgment, or creativity** -- conscious (agent cycle)

For example: A new capability to detect geopolitical alliance shifts would operate at L2/L3, need graph analysis (unconscious) to detect structural changes, SLM validation (subconscious) to assess whether the shift is genuine, and full LLM reasoning (conscious) to produce an analytical brief explaining the implications.

---

## Temporal Graph Intelligence

Legba maintains a knowledge graph in Apache AGE (a Postgres extension) with enriched temporal edges that track relationship evolution over time.

### Edge Enrichment Model

Every relationship edge in the graph carries five temporal properties:

| Property | Type | Description |
|---|---|---|
| `weight` | float 0-1 | Strength of the relationship. Increases when corroborated, decays without evidence. |
| `confidence` | float 0-1 | How certain we are that the relationship exists. Derived from evidence quality. |
| `evidence_count` | int | Number of independent signals supporting this edge. |
| `last_evidenced` | date | Most recent signal that provided evidence for this relationship. |
| `volatility` | float 0-1 | How much the relationship has changed recently. High volatility = contested or shifting. |

Edges without these properties (e.g., from seed data imports) receive default values (`weight=0.5`, `confidence=0.5`, `evidence_count=1`, `volatility=0.0`) via the maintenance daemon's backfill process.

### Event-Sourcing Relationship Changes

When a signal provides evidence for a relationship, the edge properties are updated and the change is event-sourced to TimescaleDB. This creates a time-series of relationship evolution that can answer questions like:

- When did Entity A's relationship with Entity B shift from allied to hostile?
- Which relationships are most volatile right now?
- How quickly did this alliance network form?

The TimescaleDB time-series also feeds the structural balance and graph entropy metrics, enabling detection of relationship landscape reorganization.

### Structural Balance Theory

Legba applies Structural Balance Theory from signed network analysis to the knowledge graph. The theory classifies every triad (three-node subgraph where all three pairs are connected) as balanced or unbalanced based on the signs of the edges.

**Signed edges:**
- AlliedWith = positive (+1)
- HostileTo = negative (-1)

**Triad classification (product of all three edge signs):**

| Triad Pattern | Product | Balanced? | Interpretation |
|---|---|---|---|
| +++ (friend of friend is friend) | +1 | Yes | Stable alliance bloc |
| ++- (friend of friend is enemy) | -1 | No | Structurally unstable -- predicts realignment |
| +-- (enemy of enemy is friend) | +1 | Yes | Classic balance -- shared adversary creates alliance |
| --- (all enemies) | -1 | No | Rare and unstable -- one pair will likely reconcile |

**Why unbalanced triads matter:** An unbalanced triad represents a structurally unstable configuration. In international relations, if Country A is allied with both Country B and Country C, but B and C are hostile to each other, something will eventually give -- either A will distance from one of them, or B and C will reconcile. Detecting these configurations early provides analytical lead time.

**How Legba uses it:**
- The balance score (ratio of balanced to total triads) is tracked over time in TimescaleDB
- Situations whose key entities appear in unbalanced triads receive a scoring boost in the priority stack (up to +0.10)
- Unbalanced triads are surfaced in metrics for analyst review
- A declining balance score across the graph signals broader relationship landscape reorganization

### Graph Entropy

Legba computes Shannon entropy over the distribution of relationship types in the knowledge graph:

    H = -SUM(p(type) * log2(p(type)))

where `p(type)` is the fraction of edges with a given relationship type (AlliedWith, HostileTo, PartOf, etc.) out of all edges.

**What entropy values mean:**
- **Low entropy:** The graph is dominated by a few relationship types (e.g., mostly PartOf and LocatedIn). The relationship landscape is settled.
- **High entropy:** Many different relationship types are roughly equally represented. The relationship landscape is diverse.
- **Entropy spike:** A sudden increase means new relationship types are appearing or previously dominant types are losing share. This indicates the relationship landscape is actively reorganizing -- new alliances forming, old ones dissolving, new organizational structures emerging.

Graph entropy is tracked over time in TimescaleDB and surfaced in Grafana dashboards.

### Future: Advanced Graph Analytics

The temporal graph architecture is designed to support more advanced analytics as they become practical:

- **Tensor decomposition:** Decompose the multi-relational graph into latent factors to discover hidden groupings of entities by relationship pattern.
- **Knowledge graph embeddings:** Learn vector representations of entities that encode their structural position in the graph. Similar embeddings predict similar roles.
- **Hawkes processes:** Model the temporal dynamics of relationship changes as self-exciting point processes -- when one relationship changes, it increases the probability of related changes nearby.
- **Temporal motifs:** Detect recurring patterns of relationship change sequences (e.g., "sanction followed by alliance shift followed by military escalation").

These require more compute and graph scale than the current deployment supports, but the edge enrichment model and TimescaleDB event-sourcing provide the data foundation.

---

## Priority Stack

The priority stack ranks active situations by a composite score to advise the agent (and the operator) on where to focus analytical attention. It operates in advisory mode -- it informs but does not override the agent's autonomy.

### Composite Scoring Formula

    score = (event_velocity x 0.30)
          + (goal_overlap x 0.25)
          + (watchlist_trigger_density x 0.25)
          + (recency_penalty x 0.20)
          + structural_instability_boost

**Components:**

| Component | Weight | Source | Range |
|---|---|---|---|
| Event velocity | 0.30 | Events linked to the situation in the last 48h, normalized across all situations | 0.0 - 1.0 |
| Goal overlap | 0.25 | Whether an active goal references this situation directly, by name, or by entity overlap | 0.0, 0.5, or 1.0 |
| Watchlist trigger density | 0.25 | Watchlist triggers in the last 48h for signals linked to this situation's events, normalized | 0.0 - 1.0 |
| Recency | 0.20 | Inverse of cycles since this situation was last analyzed (via SYNTHESIZE or ANALYSIS history) | 0.0 - 1.0 |
| Structural instability | additive | Number of situation key entities appearing in unbalanced triads | 0.0 - 0.10 |

Event velocity and watchlist trigger density are normalized by the maximum value across all active situations, so the highest-velocity situation always scores 1.0 on that component.

Goal overlap scores are discrete: 1.0 if a goal directly references the situation (or for operator-priority goals, any entity overlap counts); 0.5 for entity overlap with non-operator goals; 0.0 for no overlap.

### Adaptive Staleness Thresholds

Recency decay is not uniform -- critical situations become stale faster than routine ones. The recency score starts at 1.0 and decays linearly to 0.0 over a range that varies by severity:

| Severity | Staleness Start (cycles) | Full Decay (cycles) | Interpretation |
|---|---|---|---|
| Critical | 5 | 15 | Must be re-analyzed within ~1 hour |
| High | 10 | 30 | Must be re-analyzed within ~2 hours |
| Medium | 20 | 60 | Must be re-analyzed within ~4 hours |
| Low | 30 | 90 | Can go ~6 hours between analyses |

This ensures that critical situations bubble to the top of the priority stack quickly when they haven't been recently analyzed, while low-severity situations don't waste cycles with unnecessary re-analysis.

### Structural Instability Boost

Situations whose key entities appear in unbalanced triads (see Structural Balance Theory above) receive an additive scoring boost. Each entity in an unbalanced triad contributes +0.033, capped at +0.10 (approximately 3 entities). This is a small but meaningful boost that can break ties between otherwise equally-scored situations, directing attention toward areas where the relationship landscape is actively shifting.

### Advisory Mode vs Active Mode

The priority stack is currently advisory -- it surfaces in the agent's prompt context and influences cycle type selection (SYNTHESIZE targets the top-ranked situation), but does not force specific actions. In a future active mode, the priority stack could directly drive cycle scheduling, automatically assigning RESEARCH cycles to entity-enrichment needs and SYNTHESIZE cycles to the highest-priority situation.

---

## Hypothesis Engine (ACH)

Legba implements Analysis of Competing Hypotheses (ACH), a structured analytical technique developed by Richards Heuer for the CIA. The implementation enforces competing explanations for observed patterns, preventing confirmation bias.

### Thesis/Counter-Thesis Pairs

Every hypothesis is a pair:
- **Thesis:** A proposed explanation for an observed pattern (e.g., "Iran is preparing for a naval exercise")
- **Counter-thesis:** A competing explanation for the same observations (e.g., "Iran is conducting a bluff to mask land repositioning")

The system will not accept a hypothesis without a counter-thesis. This forces the analyst (or the agent) to consider alternative explanations from the moment a hypothesis is created.

### Diagnostic Evidence

Each hypothesis can include diagnostic evidence items -- specific observations that would prove one hypothesis and disprove the other. For example:

- "Satellite imagery of port loading activity" -- proves thesis (naval exercise preparation)
- "Increased ground vehicle traffic on eastern border" -- proves counter (land repositioning)

Diagnostic evidence items are stored with the hypothesis and tracked through SURVEY and ANALYSIS cycles. When a diagnostic observation is confirmed by a signal, its impact on the evidence balance is significant.

### Evidence Balance Tracking

As signals are linked to hypotheses (supporting the thesis or refuting it, which equivalently supports the counter-thesis), the `evidence_balance` counter tracks the net weight:

- Positive balance: evidence favors the thesis
- Negative balance: evidence favors the counter-thesis
- Zero balance: evidence is ambiguous

The evidence balance is a simple count (not weighted by signal confidence), keeping the metric interpretable. The absolute count of supporting and refuting signals is also tracked separately.

When the evidence balance shifts by 3 or more between maintenance daemon snapshots, the parent situation is automatically flagged for SYNTHESIZE re-assessment via the state propagation system.

### Embedding Dedup

Before creating a new hypothesis, the system checks for duplicates against all active hypotheses. Two deduplication methods are used:

1. **Cosine similarity** (preferred): If an embedding function is available, computes cosine similarity between the thesis texts. Threshold: 0.80. Catches semantic duplicates where the wording differs but the meaning is the same.
2. **Jaccard similarity** (fallback): Word-level Jaccard similarity on the thesis text after stopword removal. Threshold: 0.45. Catches obvious textual duplicates.

If a duplicate is detected, the create request returns the existing hypothesis ID instead of creating a new one, directing the analyst to add evidence to the existing hypothesis.

### Auto-Hypothesis from Fact Contradictions

When the subconscious service's fact refresh module detects a contradiction between a stored fact and recent signals, and no existing hypothesis covers the contradiction, the system can propose a new hypothesis pair:
- Thesis: the original fact is still true (contradiction is noise)
- Counter-thesis: the fact has changed (contradiction reflects reality)

This ensures that contradictions are not silently ignored but are tracked through the structured ACH framework.

---

## Confidence Architecture

Legba uses a multi-level confidence scoring system where each analytical object has its own confidence semantics.

### Signal Composite Confidence

Signal confidence uses a hybrid gatekeeper formula:

    Confidence = Gate x Modifier

    Gate = source_reliability x classification_confidence
    Modifier = (0.40 x temporal_freshness) + (0.35 x corroboration) + (0.25 x specificity)

**Gate components** (multiplicative -- either can zero out the result):

| Component | Source | Semantics |
|---|---|---|
| Source reliability | Source profile (`reliability` column) | Historical track record of this source. Recalculated daily by the subconscious service based on fetch success ratio (60%) and average signal confidence (40%). |
| Classification confidence | DeBERTa classifier output | How confidently the ML classifier assigned this signal's category. Low scores indicate ambiguous or cross-cutting topics. |

**Modifier components** (weighted sum):

| Component | Weight | Source | Semantics |
|---|---|---|---|
| Temporal freshness | 0.40 | Time since signal creation | Linear decay: 1.0 at 0h, 0.5 at 24h, 0.1 at 72h, 0.0 at 168h (1 week) |
| Corroboration | 0.35 | Independent source count on linked event | 0 sources = 0.0, 1 = 0.3, 2 = 0.6, 3 = 0.8, 4 = 0.9, 5+ = 1.0 |
| Specificity | 0.25 | SLM assessment (subconscious) or heuristic | How specific and actionable the information is. Vague rumors score low; named actors, dates, locations score high. |

The gate ensures that unreliable sources or poorly classified signals can never produce high confidence regardless of how fresh or corroborated they are. The modifier captures the operational quality of the specific signal.

Individual components are stored in the signal's `confidence_components` JSONB column for full auditability. The corroboration component is continuously updated by the maintenance daemon as new signals are linked to events.

### Fact Confidence Decay

Fact confidence decays over time when a fact receives no new corroboration:

- **Active decay:** After 30 days without update, confidence is reduced by 0.05 per maintenance cycle (floor: 0.1). The cumulative decay is tracked in `confidence_components.decay`.
- **Temporal expiration:** Facts with an explicit `valid_until` date that has passed are marked as expired. Open-ended facts with no supporting signals in 30 days have `valid_until` set to the current time.
- **Contradiction override:** When the subconscious detects a contradicting signal, fact confidence is reduced by 0.3 in a single step (floor: 0.0).
- **Corroboration boost:** When corroborated by the subconscious, fact confidence is increased by 0.1 (cap: 1.0).
- **Supersession:** When a new fact supersedes an old one (same subject/predicate), the old fact's `superseded_by` is set to the new fact's ID.

### Entity Completeness Scoring

Entity profiles have a `completeness_score` (0.0-1.0) that reflects how well-characterized the entity is. The score considers:

- Whether a description exists
- Whether key attributes are populated (for the entity type)
- Number of linked signals and events
- Recency of the last verification

Entities below a completeness threshold are flagged by the subconscious's graph consistency check and may be targeted by RESEARCH cycles for enrichment.

### Calibration Tracking

The calibration system closes the loop by measuring whether confidence scores are actually predictive of outcomes:

**Discrimination score:** The difference between average signal confidence in ACTIVE events (signals that proved significant -- event reached signal_count > 5) and RESOLVED events (signals that went nowhere -- signal_count < 3). A positive discrimination score means confidence is predictive: high-confidence signals are more likely to be significant.

**Hypothesis calibration:** For hypotheses that reach confirmed or refuted status, the system records the evidence balance at resolution time. Confirmed hypotheses should have positive evidence balance (thesis was supported); refuted hypotheses should have negative balance (counter-thesis was supported). Divergence from this pattern indicates that the evidence evaluation process is miscalibrated.

Both calibration metrics are tracked over time in TimescaleDB and surfaced in Grafana.

---

## Evidence Chains

Every analytical product in Legba traces back to raw signals through a complete evidence chain. This is not just good practice -- it is the foundation of analytical accountability.

### The Evidence Chain

    Signal --> Event --> Fact --> Hypothesis --> Situation --> Brief

| Step | Mechanism | Traceability |
|---|---|---|
| Signal | Raw data from ingestion (RSS, API, scrape) | `provenance` JSONB on every signal: source URL, fetch timestamp, ingestion pipeline, source ID |
| Signal --> Event | Event clustering by the conscious agent | `signal_event_links` junction table with timestamp |
| Event --> Fact | Fact extraction by the conscious agent | `evidence_set` JSONB on every fact: array of signal UUIDs that support this fact |
| Fact --> Hypothesis | Hypothesis evaluation (supporting/refuting) | `supporting_signals` and `refuting_signals` arrays on every hypothesis |
| Event --> Situation | Situation detection (mechanical or SLM) | `situation_events` junction table |
| Situation --> Brief | Situation brief generation (SYNTHESIZE cycle) | Brief metadata references the situation ID and cycle number |

### Provenance

Every signal carries a `provenance` JSONB field recording:
- Source URL (where the data came from)
- Source ID (which configured source produced it)
- Fetch timestamp (when it was retrieved)
- Ingestion pipeline identifier
- Any transformation or normalization applied

This provenance is immutable -- once a signal is ingested, its provenance never changes.

### Evidence Sets

Every fact carries an `evidence_set` JSONB field: an array of signal UUIDs that support the fact. When the maintenance daemon's fact decay module checks whether a fact is still supported, it queries these specific signals rather than relying on heuristic matching. When a new signal corroborates an existing fact, its UUID is added to the evidence set.

### Full Traceability

Given any analytical product (a sentence in a situation brief, a hypothesis verdict, a priority recommendation), an analyst can trace it back through the chain:

1. The brief references a situation
2. The situation is linked to events via `situation_events`
3. Each event is linked to signals via `signal_event_links`
4. Each signal has a `provenance` field pointing to the original source
5. Any facts cited in the brief have `evidence_set` linking to the supporting signals
6. Any hypotheses have `supporting_signals` and `refuting_signals` linking to the evidence

This end-to-end traceability means the system can answer "why do we believe this?" at every level of the analytical chain.
