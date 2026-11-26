# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``GET /signals`` answers its light weight by default (the list-weight rule).

A signal's ``data`` is the stored payload — the item as received: raw and
archived bodies, extracted entities and relations, GeoJSON. A live walk
measured ``/signals?limit=500`` at 2.21 MB, 48% of the Watch page's 4.57 MB,
read by the time band, which draws only a title, a category and two clocks per
row. Cutting ``data`` alone left ~1 KB of columns per row (0.54 MB at 500), so
the light weight is its own row model, like ``FindingSummaryRow``.

``fields=summary`` (the default) is :class:`SignalSummaryRow`: what a list
draws — id, title, source, category, language, geo, tags, the two clocks — and
``data`` cut to :data:`LIGHT_DATA_KEYS` (the title, the publisher's
instant and the point the map places it at). ``fields=full`` is the whole
``SignalRow`` with the payload, as every reader that opens a signal asks by
name.

Registry-slim: pydantic only.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

__all__ = [
    "LIGHT_DATA_KEYS",
    "LIGHT_GEO_KEYS",
    "SIGNALS_DEFAULT_FIELDS",
    "SignalSummaryRow",
    "SignalsFields",
    "SignalsSummaryPage",
    "light_data",
    "summary_row",
]

#: The weights ``GET /signals`` answers at.
SignalsFields = Literal["summary", "full"]
#: What an omitted ``fields`` means — the light list weight.
SIGNALS_DEFAULT_FIELDS: SignalsFields = "summary"

#: The payload keys a light row keeps: what a list draws besides the columns.
#: ``geo`` is kept cut to :data:`LIGHT_GEO_KEYS` — the world map places a
#: signal at ``data.geo.lat/lon`` (without it every point fell back to a
#: country centroid, or off the map).
LIGHT_DATA_KEYS: tuple[str, ...] = ("title", "title_en", "published_at", "geo")
#: What a light row's ``data.geo`` keeps: the point, nothing else (~40 B of
#: the ~250 B gazetteer object).
LIGHT_GEO_KEYS: tuple[str, ...] = ("lat", "lon")


class SignalSummaryRow(BaseModel):
    """One ``signals`` row at ``fields=summary`` weight."""

    id: str
    title: str
    source_id: str | None = None
    category: str = ""
    language: str = ""
    geo: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    #: Both are the row's ``fetched_at`` (as on the full row).
    event_timestamp: datetime | None = None
    produced_at: datetime | None = None
    data: dict[str, Any] = Field(default_factory=dict)


class SignalsSummaryPage(BaseModel):
    data: list[SignalSummaryRow]
    next_cursor: str | None = None


def light_data(payload: dict[str, Any]) -> dict[str, Any]:
    """The payload cut to :data:`LIGHT_DATA_KEYS` (absent keys stay absent).

    ``geo`` keeps only :data:`LIGHT_GEO_KEYS`, and only when it is an object
    carrying at least one of them.
    """
    out: dict[str, Any] = {}
    for k in LIGHT_DATA_KEYS:
        if k not in payload:
            continue
        if k == "geo":
            g = payload[k]
            point = (
                {gk: g[gk] for gk in LIGHT_GEO_KEYS if gk in g}
                if isinstance(g, dict) else {}
            )
            if point:
                out[k] = point
            continue
        out[k] = payload[k]
    return out


def summary_row(full: Any) -> SignalSummaryRow:
    """A hydrated full ``SignalRow`` at the light weight."""
    return SignalSummaryRow(
        id=full.id,
        title=full.title,
        source_id=full.source_id,
        category=full.category,
        language=full.language,
        geo=list(full.geo or []),
        tags=list(full.tags or []),
        event_timestamp=full.event_timestamp,
        produced_at=full.produced_at,
        data=light_data(full.data or {}),
    )
