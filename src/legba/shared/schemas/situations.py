"""Situation schema — persistent tracked narratives."""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID, uuid4

from pydantic import BaseModel, Field


class Situation(BaseModel):
    """A tracked narrative/situation that accumulates events."""
    id: UUID = Field(default_factory=uuid4)
    name: str
    description: str = ""
    status: str = "active"  # active, escalating, de_escalating, dormant, resolved
    category: str = ""
    key_entities: list[str] = Field(default_factory=list)  # entity names
    regions: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    last_event_at: datetime | None = None
    event_count: int = 0
    intensity_score: float = 0.0  # computed from event velocity + entity involvement
