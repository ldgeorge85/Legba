"""
Derived event schemas.

An event is a real-world occurrence derived from one or more signals.
Multiple signals can evidence the same event (many-to-many via
signal_event_links). Events are the primary analytical unit — reports,
situations, and graph analysis operate on events, not raw signals.

During Phase 0 this lives alongside the existing events.py. After
the Phase 1 migration (events table rename), this becomes the
canonical events.py.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from uuid import UUID, uuid4

from pydantic import BaseModel, Field

from .signals import SignalCategory


class EventType(str, Enum):
    INCIDENT = "incident"        # Discrete event (attack, disaster, announcement)
    DEVELOPMENT = "development"  # Ongoing process (negotiations, campaign, crisis)
    SHIFT = "shift"              # State change (policy change, leadership change)
    THRESHOLD = "threshold"      # Metric crossing (inflation hits X%, casualties pass N)


class EventSeverity(str, Enum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    ROUTINE = "routine"


class DerivedEvent(BaseModel):
    """A real-world occurrence derived from one or more signals.

    Named DerivedEvent during Phase 0 to avoid collision with the
    existing Event class. Will be renamed to Event after Phase 1 migration.
    """
    id: UUID = Field(default_factory=uuid4)
    title: str
    summary: str = ""

    # Classification
    category: SignalCategory = SignalCategory.OTHER
    event_type: EventType = EventType.INCIDENT
    severity: EventSeverity = EventSeverity.MEDIUM

    # Temporal window (events span time, signals are point-in-time)
    time_start: datetime | None = None
    time_end: datetime | None = None

    # Geography
    locations: list[str] = Field(default_factory=list)
    geo_countries: list[str] = Field(default_factory=list)
    geo_coordinates: list[dict] = Field(default_factory=list)

    # Actors
    actors: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)

    # Quality
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    signal_count: int = 0  # Number of supporting signals

    # Provenance
    source_method: str = "auto"  # "auto" (clustering), "agent" (LLM), "manual"
    source_cycle: int | None = None  # Cycle that created/refined this

    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class SignalEventLink(BaseModel):
    """Many-to-many link between a signal and a derived event."""
    signal_id: UUID
    event_id: UUID
    relevance: float = Field(default=1.0, ge=0.0, le=1.0)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
