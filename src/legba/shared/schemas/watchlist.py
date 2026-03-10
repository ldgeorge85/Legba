"""Watchlist schema — persistent alerting patterns."""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID, uuid4

from pydantic import BaseModel, Field


class WatchPattern(BaseModel):
    """A watchlist entry — triggers when matching events are stored."""
    id: UUID = Field(default_factory=uuid4)
    name: str
    description: str = ""
    entities: list[str] = Field(default_factory=list)   # entity names to match
    keywords: list[str] = Field(default_factory=list)    # keywords in title/summary
    categories: list[str] = Field(default_factory=list)  # event categories
    regions: list[str] = Field(default_factory=list)     # location names
    priority: str = "normal"  # normal, high, critical
    active: bool = True
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    last_triggered_at: datetime | None = None
    trigger_count: int = 0


class WatchTrigger(BaseModel):
    """Record of a watch being triggered."""
    watch_id: UUID
    watch_name: str
    event_id: UUID
    event_title: str
    match_reasons: list[str]
    priority: str
    triggered_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
