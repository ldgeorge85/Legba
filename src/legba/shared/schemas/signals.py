"""
Signal schemas.

A signal is raw ingested material from an external source — an RSS item,
an API response, a feed entry. Signals are the atomic unit of collection.
Real-world occurrences (events) are derived from signals.

`event_timestamp` is when the source material was published; `created_at`
is when it was ingested. `raw_content` preserves the original text for
future translation pipelines; `full_content` is the processed/cleaned version.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from uuid import UUID, uuid4

from pydantic import BaseModel, Field


class SignalCategory(str, Enum):
    CONFLICT = "conflict"
    POLITICAL = "political"
    ECONOMIC = "economic"
    TECHNOLOGY = "technology"
    HEALTH = "health"
    ENVIRONMENT = "environment"
    SOCIAL = "social"
    DISASTER = "disaster"
    OTHER = "other"


# Backward-compat alias — used across the codebase during transition
EventCategory = SignalCategory


class Signal(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    title: str
    summary: str = ""
    full_content: str = ""
    raw_content: str = ""

    # Temporal — field name kept as event_timestamp for DB/code compat
    event_timestamp: datetime | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    # Provenance
    source_id: UUID | None = None
    source_url: str = ""

    # Classification
    category: SignalCategory = SignalCategory.OTHER
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)

    # Entities
    actors: list[str] = Field(default_factory=list)
    locations: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)

    # Geo-resolved location data
    geo_countries: list[str] = Field(default_factory=list)
    geo_regions: list[str] = Field(default_factory=list)
    geo_coordinates: list[dict] = Field(default_factory=list)

    # Feed dedup
    guid: str = ""

    # Metadata
    language: str = "en"


# Backward-compat aliases
Event = Signal


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def create_signal(
    title: str,
    summary: str = "",
    full_content: str = "",
    raw_content: str = "",
    event_timestamp: datetime | None = None,
    source_id: UUID | None = None,
    source_url: str = "",
    category: SignalCategory = SignalCategory.OTHER,
    confidence: float = 0.5,
    actors: list[str] | None = None,
    locations: list[str] | None = None,
    tags: list[str] | None = None,
    language: str = "en",
    guid: str = "",
) -> Signal:
    return Signal(
        title=title,
        summary=summary,
        full_content=full_content,
        raw_content=raw_content,
        event_timestamp=event_timestamp,
        source_id=source_id,
        source_url=source_url,
        category=category,
        confidence=confidence,
        actors=actors or [],
        locations=locations or [],
        tags=tags or [],
        language=language,
        guid=guid,
    )


# Backward-compat alias
create_event = create_signal
