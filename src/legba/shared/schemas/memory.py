"""
Memory schemas.

Defines the data structures stored across the memory layers:
- Episodes (Qdrant) — short-term and long-term episodic memory
- Facts (Postgres) — structured knowledge
- Entities (Postgres + AGE) — entity graph nodes/edges
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, Field


class EpisodeType(str, Enum):
    """What kind of episode this represents."""

    ACTION = "action"          # Tool call + result
    OBSERVATION = "observation" # Something the agent noticed
    REASONING = "reasoning"    # A significant reasoning step
    CYCLE_SUMMARY = "cycle_summary"  # Compressed summary of a full cycle
    LESSON = "lesson"          # Extracted lesson/insight
    INTERACTION = "interaction" # Human communication


class Episode(BaseModel):
    """An episodic memory stored in Qdrant."""

    id: UUID = Field(default_factory=uuid4)
    cycle_number: int
    episode_type: EpisodeType
    content: str  # Human-readable summary
    significance: float = Field(default=0.5, ge=0.0, le=1.0)  # How important (LLM-rated)
    embedding: list[float] | None = None  # 1024-dim vector (set after embedding call)

    # Context
    goal_id: UUID | None = None  # Which goal this relates to
    tool_name: str | None = None  # If action, which tool
    tags: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    # Timestamps
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    # Retrieval tracking
    access_count: int = 0
    last_accessed_at: datetime | None = None


class Fact(BaseModel):
    """A structured fact stored in Postgres."""

    id: UUID = Field(default_factory=uuid4)
    subject: str  # What it's about
    predicate: str  # The relationship/property
    value: str  # The value
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    source_cycle: int | None = None  # Which cycle produced this
    source_episode_id: UUID | None = None

    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    superseded_by: UUID | None = None  # If a newer fact replaces this one

    # Temporal bounds — when is this fact true?
    valid_from: datetime | None = None   # When the fact became true (default: NOW() in DB)
    valid_until: datetime | None = None  # When the fact stops being true (NULL = open-ended)


class Entity(BaseModel):
    """An entity in the knowledge graph (Postgres + AGE)."""

    id: UUID = Field(default_factory=uuid4)
    name: str
    entity_type: str  # "server", "service", "person", "concept", etc.
    properties: dict[str, Any] = Field(default_factory=dict)

    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class Relationship(BaseModel):
    """A directed relationship between two entities."""

    id: UUID = Field(default_factory=uuid4)
    source_id: UUID
    target_id: UUID
    relation_type: str  # "depends_on", "runs_on", "owns", etc.
    properties: dict[str, Any] = Field(default_factory=dict)

    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
