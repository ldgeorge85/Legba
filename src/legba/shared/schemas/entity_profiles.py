"""
Entity Intelligence Layer — Versioned, sourced entity profiles.

Profiles are the rich structured data about an entity. They live in
Postgres (entity_profiles table, JSONB data column) while AGE holds
only the topology (vertices + edges). Profiles accumulate understanding
over time through sourced assertions, and preserve full version history.

This is the "Persistent World Model" layer: the event stream is what's
happening; entity profiles are the system's understanding of who and what
exists and how they relate. Events update the world model. The world
model provides context for interpreting events.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, Field


class EntityType(str, Enum):
    """Constrained entity types for SA world model."""
    COUNTRY = "country"
    ORGANIZATION = "organization"
    PERSON = "person"
    LOCATION = "location"
    MILITARY_UNIT = "military_unit"
    POLITICAL_PARTY = "political_party"
    ARMED_GROUP = "armed_group"
    INTERNATIONAL_ORG = "international_org"
    CORPORATION = "corporation"
    MEDIA_OUTLET = "media_outlet"
    EVENT_SERIES = "event_series"
    CONCEPT = "concept"
    COMMODITY = "commodity"
    INFRASTRUCTURE = "infrastructure"
    OTHER = "other"


class Assertion(BaseModel):
    """A single sourced claim about an entity."""
    key: str
    value: str
    confidence: float = Field(default=0.7, ge=0.0, le=1.0)
    source_event_id: UUID | None = None
    source_url: str = ""
    observed_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    superseded: bool = False


# Suggested section names per entity type
COUNTRY_SECTIONS = [
    "identity", "government", "military", "economy",
    "alliances", "conflicts", "demographics",
]
PERSON_SECTIONS = [
    "identity", "role", "affiliations", "background",
]
ORGANIZATION_SECTIONS = [
    "identity", "leadership", "operations", "membership",
]


def _expected_sections(etype: EntityType) -> list[str]:
    mapping = {
        EntityType.COUNTRY: COUNTRY_SECTIONS,
        EntityType.PERSON: PERSON_SECTIONS,
        EntityType.ORGANIZATION: ORGANIZATION_SECTIONS,
        EntityType.INTERNATIONAL_ORG: ORGANIZATION_SECTIONS,
        EntityType.CORPORATION: ORGANIZATION_SECTIONS,
        EntityType.ARMED_GROUP: ORGANIZATION_SECTIONS,
    }
    return mapping.get(etype, [])


class EntityProfile(BaseModel):
    """A versioned, structured entity profile."""
    id: UUID = Field(default_factory=uuid4)
    canonical_name: str
    entity_type: EntityType = EntityType.OTHER
    aliases: list[str] = Field(default_factory=list)
    summary: str = ""

    # Assertions grouped by section
    # Key "" or "general" holds uncategorized assertions
    sections: dict[str, list[Assertion]] = Field(default_factory=dict)

    # Tags — freeform metadata for filtering and context
    tags: list[str] = Field(default_factory=list)

    # Metadata
    completeness_score: float = Field(default=0.0, ge=0.0, le=1.0)
    event_link_count: int = 0
    last_event_link_at: datetime | None = None

    version: int = 1
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )

    def all_assertions(self) -> list[Assertion]:
        """Flatten all assertions across sections."""
        result = []
        for section_assertions in self.sections.values():
            result.extend(section_assertions)
        return result

    def active_assertions(self) -> list[Assertion]:
        """Non-superseded assertions only."""
        return [a for a in self.all_assertions() if not a.superseded]

    def compute_completeness(self) -> float:
        """Heuristic completeness based on entity type, filled sections, and depth.

        For typed entities: each expected section contributes proportionally,
        weighted by assertion depth (min(active_assertions / 3, 1.0) per section).
        For other types: based on summary + assertion count.
        """
        expected = _expected_sections(self.entity_type)
        if not expected:
            has_summary = 1.0 if self.summary else 0.0
            has_assertions = min(len(self.active_assertions()) / 3.0, 1.0)
            return round((has_summary + has_assertions) / 2.0, 2)

        section_scores = []
        for s in expected:
            if s in self.sections:
                active = [a for a in self.sections[s] if not a.superseded]
                # Depth: 3+ assertions = fully complete for this section
                section_scores.append(min(len(active) / 3.0, 1.0))
            else:
                section_scores.append(0.0)
        return round(sum(section_scores) / len(expected), 2)


class EventEntityLink(BaseModel):
    """Junction: an event mentions/involves an entity."""
    event_id: UUID
    entity_id: UUID
    role: str = "mentioned"  # "actor", "location", "target", "mentioned"
    confidence: float = Field(default=0.8, ge=0.0, le=1.0)
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
