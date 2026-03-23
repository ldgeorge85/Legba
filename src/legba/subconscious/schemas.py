"""Pydantic models for SLM structured responses.

These schemas define the expected output format from the SLM for each
validation task. They are used both for response parsing and for
guided_json constrained decoding on the vLLM provider.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


# --- Signal Validation ---

class SignalValidationVerdict(BaseModel):
    """Verdict for a single signal in a validation batch."""

    signal_id: str = Field(description="UUID of the signal being validated")
    specificity: float = Field(
        ge=0.0, le=1.0,
        description="How specific/actionable is this signal (0=vague, 1=precise)",
    )
    internal_consistency: float = Field(
        ge=0.0, le=1.0,
        description="Internal logical consistency of the signal (0=contradictory, 1=coherent)",
    )
    cross_signal_contradiction: bool = Field(
        default=False,
        description="True if this signal contradicts other signals in the batch",
    )
    adjusted_confidence: float = Field(
        ge=0.0, le=1.0,
        description="Adjusted confidence score after validation",
    )
    reasoning: str = Field(description="Brief explanation for the verdict")


class SignalBatchValidationResponse(BaseModel):
    """Response containing verdicts for a batch of signals."""

    verdicts: list[SignalValidationVerdict] = Field(
        description="One verdict per signal in the input batch",
    )


# --- Entity Resolution ---

class EntityResolutionVerdict(BaseModel):
    """Verdict for an ambiguous entity resolution."""

    entity_name: str = Field(description="The entity name being resolved")
    matched_entity_id: str | None = Field(
        default=None,
        description="UUID of the matched existing entity, or null if new",
    )
    is_new_entity: bool = Field(
        description="True if this entity does not match any existing entity",
    )
    confidence: float = Field(
        ge=0.0, le=1.0,
        description="Confidence in the resolution decision",
    )
    reasoning: str = Field(description="Brief explanation for the match/non-match")


# --- Classification Refinement ---

class ClassificationVerdict(BaseModel):
    """Verdict for a boundary classification case."""

    signal_id: str = Field(description="UUID of the signal being reclassified")
    corrected_categories: list[str] = Field(
        description="Corrected category list (most likely first)",
    )
    reasoning: str = Field(description="Brief explanation for the reclassification")


# --- Relationship Validation ---

class RelationshipVerdict(BaseModel):
    """Verdict for a REBEL-extracted relationship triple."""

    triple_index: int = Field(description="Index of the triple in the input batch")
    valid: bool = Field(description="Whether the extracted relationship is valid")
    corrected_type: str | None = Field(
        default=None,
        description="Corrected relationship type, or null if valid/invalid",
    )
    reasoning: str = Field(description="Brief explanation")


# --- Fact Refresh ---

class FactRefreshVerdict(BaseModel):
    """Verdict for a fact corroboration check."""

    fact_id: str = Field(description="UUID of the fact being checked")
    status: Literal["corroborated", "contradicted", "stale"] = Field(
        description="Whether recent evidence supports, refutes, or has no bearing on this fact",
    )
    reasoning: str = Field(description="Brief explanation of the evidence assessment")
