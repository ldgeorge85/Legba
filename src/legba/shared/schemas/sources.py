"""
Source registry schemas.

Sources are feeds, APIs, or scraped endpoints that provide event data.
Each source carries multi-dimensional trust metadata (reliability, bias,
ownership, geographic origin, language, timeliness, coverage scope) that
feeds into cross-source corroboration analysis in later phases.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, Field


class SourceType(str, Enum):
    RSS = "rss"
    API = "api"
    SCRAPE = "scrape"
    MANUAL = "manual"


class BiasLabel(str, Enum):
    FAR_LEFT = "far_left"
    LEFT = "left"
    CENTER_LEFT = "center_left"
    CENTER = "center"
    CENTER_RIGHT = "center_right"
    RIGHT = "right"
    FAR_RIGHT = "far_right"


class OwnershipType(str, Enum):
    STATE = "state"
    CORPORATE = "corporate"
    NONPROFIT = "nonprofit"
    PUBLIC_BROADCAST = "public_broadcast"
    INDEPENDENT = "independent"


class CoverageScope(str, Enum):
    GLOBAL = "global"
    REGIONAL = "regional"
    NATIONAL = "national"
    LOCAL = "local"


class SourceStatus(str, Enum):
    ACTIVE = "active"
    PAUSED = "paused"
    ERROR = "error"
    RETIRED = "retired"


class Source(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    name: str
    url: str
    source_type: SourceType = SourceType.RSS
    description: str = ""

    # Trust dimensions
    reliability: float = Field(default=0.5, ge=0.0, le=1.0)
    bias_label: BiasLabel = BiasLabel.CENTER
    ownership_type: OwnershipType = OwnershipType.INDEPENDENT
    geo_origin: str = ""  # ISO 3166-1 alpha-2 (e.g. "US", "GB", "QA")
    language: str = "en"  # ISO 639-1
    timeliness: float = Field(default=0.5, ge=0.0, le=1.0)
    coverage_scope: CoverageScope = CoverageScope.GLOBAL

    # Operational
    status: SourceStatus = SourceStatus.ACTIVE
    last_fetched_at: datetime | None = None
    last_successful_fetch_at: datetime | None = None
    last_error: str | None = None
    fetch_interval_minutes: int = 60

    # Reliability tracking (auto-updated by feed_parse / event_store)
    fetch_success_count: int = 0
    fetch_failure_count: int = 0
    events_produced_count: int = 0
    consecutive_failures: int = 0

    # Flexible metadata
    tags: list[str] = Field(default_factory=list)
    config: dict[str, Any] = Field(default_factory=dict)

    # Timestamps
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def create_source(
    name: str,
    url: str,
    source_type: SourceType = SourceType.RSS,
    reliability: float = 0.5,
    bias_label: BiasLabel = BiasLabel.CENTER,
    ownership_type: OwnershipType = OwnershipType.INDEPENDENT,
    geo_origin: str = "",
    language: str = "en",
    timeliness: float = 0.5,
    coverage_scope: CoverageScope = CoverageScope.GLOBAL,
    description: str = "",
    tags: list[str] | None = None,
    fetch_interval_minutes: int = 60,
) -> Source:
    return Source(
        name=name,
        url=url,
        source_type=source_type,
        reliability=reliability,
        bias_label=bias_label,
        ownership_type=ownership_type,
        geo_origin=geo_origin,
        language=language,
        timeliness=timeliness,
        coverage_scope=coverage_scope,
        description=description,
        tags=tags or [],
        fetch_interval_minutes=fetch_interval_minutes,
    )
