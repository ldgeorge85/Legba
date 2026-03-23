"""SLM prompt templates for subconscious validation tasks.

Each prompt includes:
- A system instruction describing the task
- Expected JSON output schema for guided_json / tool_use
- Slot markers for dynamic content injection
"""

from __future__ import annotations

from .schemas import (
    ClassificationVerdict,
    EntityResolutionVerdict,
    FactRefreshVerdict,
    RelationshipVerdict,
    SignalBatchValidationResponse,
    SignalValidationVerdict,
)

# ---------------------------------------------------------------------------
# Signal Validation
# ---------------------------------------------------------------------------

SIGNAL_VALIDATION_SYSTEM = """\
You are a signal quality assessor for an intelligence analysis system.

Your task: evaluate a batch of intelligence signals for quality and reliability.

For each signal, assess:
1. **Specificity** (0-1): How specific and actionable is the information? Vague rumours score low; named actors, dates, and locations score high.
2. **Internal consistency** (0-1): Does the signal's content contradict itself? Are claims logically coherent?
3. **Cross-signal contradiction**: Does this signal directly contradict any other signal in the batch?
4. **Adjusted confidence**: Given the above, compute an adjusted confidence score (0-1). Original confidence is provided for reference.

Rules:
- Be conservative — when in doubt, lower the confidence.
- Signals that are purely speculative or unsourced should score < 0.3 specificity.
- Signals that repeat the same claim from the same source are not cross-contradictions.
- Output MUST be valid JSON matching the schema below.
"""

SIGNAL_VALIDATION_PROMPT = """\
Evaluate the following {count} signals. Return a JSON object with a "verdicts" array containing one verdict per signal.

Signals:
{signals_json}

Output JSON schema:
{schema}
"""

SIGNAL_VALIDATION_SCHEMA = SignalBatchValidationResponse.model_json_schema()


# ---------------------------------------------------------------------------
# Entity Resolution
# ---------------------------------------------------------------------------

ENTITY_RESOLUTION_SYSTEM = """\
You are an entity resolution specialist for an intelligence analysis system.

Your task: given an extracted entity name and its context, determine whether it matches an existing entity in the knowledge base or is a new entity.

Rules:
- Match on semantic identity, not just string similarity. "Vladimir Putin" and "Putin" and "Russian President" may be the same entity depending on context.
- Consider entity type (person, organization, location, etc.) — don't match a person to an organization.
- If multiple candidates could match, pick the most likely one.
- If no candidate matches with reasonable confidence, mark as new entity.
- Output MUST be valid JSON matching the schema below.
"""

ENTITY_RESOLUTION_PROMPT = """\
Resolve the following entity. Pick the best match from the candidates, or mark as new.

Entity name: {entity_name}
Context: {context}
Entity type (if known): {entity_type}

Candidates:
{candidates_json}

Output JSON schema:
{schema}
"""

ENTITY_RESOLUTION_SCHEMA = EntityResolutionVerdict.model_json_schema()


# ---------------------------------------------------------------------------
# Classification Refinement
# ---------------------------------------------------------------------------

CLASSIFICATION_REFINEMENT_SYSTEM = """\
You are a classification specialist for an intelligence analysis system.

Your task: given a signal text and its top classification scores from the ML classifier, determine the correct category assignment.

The ML classifier assigns probability scores to categories. You are called when the top-2 categories are within 0.1 of each other (a boundary case). Your job is to break the tie using semantic understanding.

Available categories: conflict, political, economic, disaster, health, technology, social, environmental, security, diplomatic, military, other.

Rules:
- Consider the full text, not just keywords.
- A signal can belong to multiple categories if genuinely cross-cutting.
- Order categories by relevance (most relevant first).
- Output MUST be valid JSON matching the schema below.
"""

CLASSIFICATION_REFINEMENT_PROMPT = """\
Classify the following signal. The ML classifier's top scores are provided.

Signal ID: {signal_id}
Text: {text}

ML classifier scores (category: probability):
{scores_json}

Output JSON schema:
{schema}
"""

CLASSIFICATION_REFINEMENT_SCHEMA = ClassificationVerdict.model_json_schema()


# ---------------------------------------------------------------------------
# Relationship Validation
# ---------------------------------------------------------------------------

RELATIONSHIP_VALIDATION_SYSTEM = """\
You are a relationship extraction validator for an intelligence analysis system.

Your task: validate relationship triples extracted by the REBEL model. Each triple has a subject, predicate (relationship type), and object.

Rules:
- A triple is **valid** if the relationship accurately reflects the source text.
- A triple is **invalid** if the extraction is wrong, hallucinated, or the relationship type is incorrect.
- If the relationship type is wrong but a relationship exists, provide a corrected_type.
- Common relationship types: allied_with, opposes, part_of, located_in, leads, member_of, supplies, sanctions, controls, subsidiary_of.
- Output MUST be valid JSON matching the schema below.
"""

RELATIONSHIP_VALIDATION_PROMPT = """\
Validate the following relationship triples extracted from source text.

Source text: {source_text}

Triples:
{triples_json}

Output JSON schema (one verdict per triple):
{schema}
"""

RELATIONSHIP_VALIDATION_SCHEMA = RelationshipVerdict.model_json_schema()


# ---------------------------------------------------------------------------
# Fact Refresh
# ---------------------------------------------------------------------------

FACT_REFRESH_SYSTEM = """\
You are a fact verification specialist for an intelligence analysis system.

Your task: given a stored fact and recent signals, determine whether the fact is corroborated, contradicted, or stale.

Rules:
- **Corroborated**: Recent signals provide additional evidence supporting the fact.
- **Contradicted**: Recent signals provide credible evidence that the fact is no longer true or was incorrect.
- **Stale**: No recent signals relate to this fact, and the fact may be outdated.
- Consider signal confidence when weighting evidence.
- A single high-confidence contradicting signal can override multiple low-confidence supporting ones.
- Output MUST be valid JSON matching the schema below.
"""

FACT_REFRESH_PROMPT = """\
Check the following fact against recent signals.

Fact ID: {fact_id}
Subject: {subject}
Predicate: {predicate}
Value: {value}
Confidence: {confidence}
Last updated: {updated_at}

Recent relevant signals:
{signals_json}

Output JSON schema:
{schema}
"""

FACT_REFRESH_SCHEMA = FactRefreshVerdict.model_json_schema()


# ---------------------------------------------------------------------------
# Graph Consistency
# ---------------------------------------------------------------------------

GRAPH_CONSISTENCY_SYSTEM = """\
You are a knowledge graph consistency checker for an intelligence analysis system.

Your task: review a set of graph anomalies and determine which ones represent genuine inconsistencies that need resolution.

Anomaly types:
- **dissolved_active_edges**: An entity marked as dissolved/inactive still has recently created edges.
- **orphan_entity**: An entity has no edges and no recent signal mentions.
- **contradictory_edges**: Two edges imply contradictory relationships (e.g., X allied_with Y and X opposes Y).
- **stale_entity**: An entity has not been referenced in any signal for an extended period.

Rules:
- Not all anomalies need action. A recently dissolved entity may legitimately have edges from before dissolution.
- Prioritize contradictory edges and dissolved_active_edges as most urgent.
- Output a JSON array of objects with: anomaly_index (int), needs_action (bool), suggested_action (str), reasoning (str).
"""

GRAPH_CONSISTENCY_PROMPT = """\
Review the following graph anomalies:

{anomalies_json}

Output a JSON array of verdicts, one per anomaly. Each verdict:
{{"anomaly_index": <int>, "needs_action": <bool>, "suggested_action": "<string>", "reasoning": "<string>"}}
"""
