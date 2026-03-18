"""
Hypothesis schemas for Analysis of Competing Hypotheses (ACH).

A hypothesis is a structured analytical object that tracks competing
explanations for an observed pattern. Unlike simple predictions (which
are standalone claims), hypotheses come in pairs (thesis + counter-thesis)
and accumulate evidence across cycles.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from uuid import UUID, uuid4

from pydantic import BaseModel, Field


class HypothesisStatus(str, Enum):
    ACTIVE = "active"
    CONFIRMED = "confirmed"
    REFUTED = "refuted"
    SUPERSEDED = "superseded"
    STALE = "stale"


class DiagnosticEvidence(BaseModel):
    """A specific data point that would prove or disprove a hypothesis."""
    description: str
    proves: str = "thesis"  # "thesis" or "counter"
    observed: bool = False
    observed_cycle: int | None = None
    signal_id: UUID | None = None


class Hypothesis(BaseModel):
    """A competing hypothesis pair for ACH analysis."""
    id: UUID = Field(default_factory=uuid4)
    situation_id: UUID | None = None

    thesis: str               # "Iran is preparing for naval exercise"
    counter_thesis: str       # "Iran is bluffing to mask land repositioning"

    diagnostic_evidence: list[DiagnosticEvidence] = Field(default_factory=list)

    supporting_signals: list[UUID] = Field(default_factory=list)
    refuting_signals: list[UUID] = Field(default_factory=list)
    evidence_balance: int = 0  # +N supporting, -N refuting

    status: HypothesisStatus = HypothesisStatus.ACTIVE
    created_cycle: int = 0
    last_evaluated_cycle: int = 0

    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
