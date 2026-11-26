# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``GET /v3/events`` answers a light row when asked for it by name.

On a live walk the Watch page weighed 2.01 MB against its 2.00 MB budget
and its largest response was ``/v3/events?limit=500`` at 0.42 MB. Two readers
share that one read — the world map's events layer (``v4/world/mapData.ts``:
a point, its countries, severity, lifecycle, subject, one clock and the title)
and the time band's events lane (``lib/useTemporalRows.ts``: the title,
severity, lifecycle and both clocks). Neither draws the summary text, the
locations, the counts, the confidence or the producer columns, which are
nearly half the row.

``fields=summary`` is :class:`EventSummaryRow`: the union of what those two
readers draw, nothing more (live, 500 rows: 419 KB -> 239 KB). ``fields=full``
— still what an omitted ``fields`` answers — is the whole ``EventRow``; every
other reader (a subject's timeline, a situation's tracked events, the export
basket, the inspector by id) keeps reading it unchanged. The column list is
held, case for case, against the walk's fixture mirror
(``legba-ui-v3/tools/walk/fixtures/weights_golden.json``), so the fixture walk
answers a light read with the light row it gets live.

Registry-slim: pydantic only.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

__all__ = [
    "EVENTS_DEFAULT_FIELDS",
    "EventSummaryRow",
    "EventsFields",
    "EventsSummaryPage",
    "summary_row",
]

#: The weights ``GET /v3/events`` answers at.
EventsFields = Literal["summary", "full"]
#: What an omitted ``fields`` means — the whole row (the light row is asked
#: for by name, as the descriptor rosters ask for theirs).
EVENTS_DEFAULT_FIELDS: EventsFields = "full"


class EventSummaryRow(BaseModel):
    """One ``events`` row at ``fields=summary`` weight: what a map point and a
    time-band row draw."""

    id: str
    title: str
    severity: str = "medium"
    lifecycle_state: str = "emerging"
    category: str = ""
    target_id: str | None = None
    #: ISO2 codes — the countries a point is listed under.
    geo: list[str] = Field(default_factory=list)
    #: The point the map places the event at (no point: not placed).
    geo_lat: float | None = None
    geo_lon: float | None = None
    #: The occurrence clocks and the record clock.
    time_start: datetime | None = None
    time_end: datetime | None = None
    valid_from: datetime | None = None
    produced_at: datetime | None = None


class EventsSummaryPage(BaseModel):
    """The paged envelope at the light weight (the same keys as ``EventsPage``)."""

    data: list[EventSummaryRow] = Field(default_factory=list)
    next_cursor: str | None = None
    as_of: datetime | None = None
    unbounded_start: int = 0


def summary_row(full: Any) -> EventSummaryRow:
    """A full ``EventRow`` (or anything carrying its attributes) at the light weight."""
    return EventSummaryRow(**{k: getattr(full, k) for k in EventSummaryRow.model_fields})
