"""
Event schemas.

Events are discrete occurrences extracted from news sources.
`event_timestamp` is when the event happened; `created_at` is when
it was ingested. `raw_content` preserves the original text for future
translation pipelines; `full_content` is the processed/cleaned version.

NOTE: actors[] and locations[] are string arrays in SA-1. In the entity
profile layer (SA-2+), these will resolve to canonical graph entity UUIDs.
"Iran", "Islamic Republic of Iran", "Tehran government" must collapse to
the same node.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, Field


class EventCategory(str, Enum):
    CONFLICT = "conflict"
    POLITICAL = "political"
    ECONOMIC = "economic"
    TECHNOLOGY = "technology"
    HEALTH = "health"
    ENVIRONMENT = "environment"
    SOCIAL = "social"
    DISASTER = "disaster"
    OTHER = "other"


class Event(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    title: str
    summary: str = ""
    full_content: str = ""
    raw_content: str = ""  # Original text, preserved for translation pipeline

    # Temporal
    event_timestamp: datetime | None = None  # When the event happened
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    # Provenance
    source_id: UUID | None = None
    source_url: str = ""

    # Classification
    category: EventCategory = EventCategory.OTHER
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)

    # Entities (string arrays — will evolve to graph entity UUIDs in SA-2+)
    actors: list[str] = Field(default_factory=list)
    locations: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)

    # Geo-resolved location data (auto-populated at store time)
    geo_countries: list[str] = Field(default_factory=list)  # ISO 3166-1 alpha-2 codes
    geo_regions: list[str] = Field(default_factory=list)    # Resolved region/state names
    geo_coordinates: list[dict] = Field(default_factory=list)  # [{"name": ..., "lat": ..., "lon": ...}]

    # Metadata
    language: str = "en"


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def create_event(
    title: str,
    summary: str = "",
    full_content: str = "",
    raw_content: str = "",
    event_timestamp: datetime | None = None,
    source_id: UUID | None = None,
    source_url: str = "",
    category: EventCategory = EventCategory.OTHER,
    confidence: float = 0.5,
    actors: list[str] | None = None,
    locations: list[str] | None = None,
    tags: list[str] | None = None,
    language: str = "en",
) -> Event:
    return Event(
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
    )
