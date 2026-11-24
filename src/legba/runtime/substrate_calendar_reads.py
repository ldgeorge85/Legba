# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The ``list_scheduled_events`` read (W11-F141), split out of the substrate
query port (roll 59: the port sat past its module-size ceiling once survey
mode and the calendar both landed). The port delegates; behaviour is the
method's, moved verbatim.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from . import substrate_temporal as _temporal
from ..data import scheduled_events as _sched

#: The default read window when no bound is given: now → +14 days.
DEFAULT_WINDOW = timedelta(days=14)


async def list_scheduled_events(
    pool: Any,
    *,
    window_start: str | None = None,
    window_end: str | None = None,
    scope: str | None = None,
    subject_keys: list[str] | None = None,
    limit: int = 50,
    max_row_limit: int,
) -> dict[str, Any]:
    """The scheduled-events calendar (migration 0240) — the register of
    FUTURE dated things (summits, votes, deadlines, strikes) the platform
    knows about.

    Operator- and seed-entered ground truth, NOT analysis-derived: a row
    is what a person (or a curated file) declared will happen, with its
    own ``confidence`` (confirmed / reported / tentative) and
    ``provenance``. ``window_start`` / ``window_end`` (ISO-8601) bound
    the read by overlap — an event counts when it starts before
    ``window_end`` and has not ended before ``window_start``; absent,
    they default to now → +14 days. ``scope`` is an exact match on the
    row's scope tag (a country/target code or ``global``);
    ``subject_keys`` matches on any shared key.

    The ``coverage`` block is the point of the tool: an empty answer
    says ``n: 0`` over a named window, and ``total_rows`` tells "no
    events held for this window" apart from "the calendar is empty" —
    so a consult never reads a bare empty list as "nothing is
    scheduled". A malformed bound refuses loud; an inverted window
    refuses rather than answering a different question.
    """
    now = datetime.now(timezone.utc)
    try:
        w_start = (
            _temporal.parse_instant(window_start, name="window_start")
            if window_start is not None else now
        )
        w_end = (
            _temporal.parse_instant(window_end, name="window_end")
            if window_end is not None else now + DEFAULT_WINDOW
        )
    except _temporal.TemporalParameterError as exc:
        return {"rows": [], "refs": [], "count": 0, "error": str(exc)}
    if w_end <= w_start:
        return {
            "rows": [], "refs": [], "count": 0,
            "error": "window_end must be after window_start — "
                     "refusing rather than reading an empty window",
        }
    clamped_limit = max(1, min(int(limit), max_row_limit))
    async with pool.acquire() as conn:
        return await _sched.list_events(
            conn,
            window_start=w_start,
            window_end=w_end,
            scope=scope,
            subject_keys=subject_keys,
            limit=clamped_limit,
            now=now,
        )
