"""
Cognitive architecture Pydantic models.

Extended schemas for confidence tracking, evidence provenance, event
lifecycle, and signal processing lineage.  These are additive models
that will be wired into existing schemas (Signal, Fact, DerivedEvent)
during the cognitive architecture integration phase.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Confidence components (stored as JSONB on signals and facts)
# ---------------------------------------------------------------------------

class ConfidenceComponents(BaseModel):
    """Individual components that feed the composite confidence formula.

    Stored as ``confidence_components`` JSONB on signals and facts.
    See :mod:`legba.shared.confidence` for the computation.
    """
    source_reliability: float = Field(
        default=0.5, ge=0.0, le=1.0,
        description="Source reliability rating (from sources.reliability)",
    )
    classification_confidence: float = Field(
        default=0.5, ge=0.0, le=1.0,
        description="Classifier's self-reported confidence in category assignment",
    )
    temporal_freshness: float = Field(
        default=1.0, ge=0.0, le=1.0,
        description="Freshness decay (1.0 = just happened, 0.0 = stale)",
    )
    corroboration: float = Field(
        default=0.0, ge=0.0, le=1.0,
        description="Cross-source corroboration score",
    )
    specificity: float = Field(
        default=0.5, ge=0.0, le=1.0,
        description="How specific/actionable the information is",
    )


# ---------------------------------------------------------------------------
# Evidence items (stored as JSONB array on facts)
# ---------------------------------------------------------------------------

class EvidenceItem(BaseModel):
    """A single piece of evidence supporting or relating to a fact.

    Facts accumulate an ``evidence_set`` (list of EvidenceItem) over time
    as signals and events corroborate or challenge them.
    """
    signal_id: UUID | None = None
    event_id: UUID | None = None
    url: str | None = None
    type: str = Field(
        default="direct",
        description="Evidence type: 'direct' (primary source), "
                    "'derived' (analytical product), 'external' (URL reference)",
    )
    confidence: float = Field(
        default=0.5, ge=0.0, le=1.0,
        description="Confidence that this evidence supports the fact",
    )
    added_cycle: int | None = Field(
        default=None,
        description="Agent cycle number when this evidence was added",
    )


# ---------------------------------------------------------------------------
# Event lifecycle (stored as columns on events table)
# ---------------------------------------------------------------------------

class EventLifecycle(BaseModel):
    """Lifecycle metadata for a derived event.

    See :mod:`legba.shared.lifecycle` for the state machine and
    transition rules.
    """
    status: str = Field(
        default="emerging",
        description="Current lifecycle status (emerging, developing, active, "
                    "evolving, resolved, reactivated)",
    )
    changed_at: datetime | None = Field(
        default=None,
        description="Timestamp of last lifecycle status change",
    )


# ---------------------------------------------------------------------------
# Signal provenance (tracks the full processing lineage of a signal)
# ---------------------------------------------------------------------------

class SignalProvenance(BaseModel):
    """Full processing lineage for a signal.

    Records every step the signal went through from raw fetch to final
    storage, enabling auditability and confidence calibration.
    """
    # Ingestion
    raw_source: str = Field(
        default="",
        description="Original source identifier (feed URL, API endpoint)",
    )
    fetched_at: datetime | None = Field(
        default=None,
        description="When the raw content was fetched",
    )

    # Classification
    normalized_by: str = Field(
        default="",
        description="Module/version that normalized the content",
    )
    classified_by: str = Field(
        default="",
        description="Module/version that classified the signal category",
    )
    classification_confidence: float = Field(
        default=0.0, ge=0.0, le=1.0,
        description="Classifier's self-reported confidence",
    )

    # NER
    ner_by: str = Field(
        default="",
        description="NER model/version (e.g. 'spacy:en_core_web_lg:3.7')",
    )
    entities_extracted: list[str] = Field(
        default_factory=list,
        description="Entity names extracted by NER",
    )

    # Deduplication
    dedup_checked: dict = Field(
        default_factory=dict,
        description="Dedup tiers checked and results (e.g. {'guid': false, "
                    "'url': false, 'vector': true, 'jaccard': 0.42})",
    )
    dedup_nearest: dict = Field(
        default_factory=dict,
        description="Nearest existing signal if dedup was close "
                    "(e.g. {'signal_id': '...', 'similarity': 0.87})",
    )

    # Embedding
    embedded_by: str = Field(
        default="",
        description="Embedding model/version",
    )

    # Clustering
    clustered_into: str | None = Field(
        default=None,
        description="Event ID this signal was clustered into (if any)",
    )
    cluster_similarity: float = Field(
        default=0.0, ge=0.0, le=1.0,
        description="Cosine similarity to the cluster centroid",
    )

    # Validation (agent review)
    validated_by: str | None = Field(
        default=None,
        description="Cycle type/number that validated this signal (e.g. 'CURATE:1234')",
    )
    validation_verdict: dict = Field(
        default_factory=dict,
        description="Agent validation output (e.g. {'keep': true, 'reason': '...'})",
    )
