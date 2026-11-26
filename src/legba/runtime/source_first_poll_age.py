# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The first-poll entry-age cap, a leaf beside ``source_actor``.

INCIDENT 2026-11-14: sources registered that morning loaded every entry their
feed documents carried on the first poll, whatever its age. A status feed
landed entries from March; a national CERT's alert feed reached back to 2015.
A feed handler with no cursor has no ``since`` to filter on, so the first poll
walked the whole document. The cursor policy (:mod:`.source_cursor`) only
bounds polls that HAVE a cursor; the very first one was unbounded. The same
holds after a descriptor edit: the actor id embeds the descriptor's content
hash, so a new head starts with an empty cursor and re-walks the document.

The rule: on a poll with no cursor (``since is None``), a handler that
advertises :data:`FIRST_POLL_AGE_ATTR` (seconds) has every entry whose OWN
feed date is older than ``now - max_age`` skipped before enrichment. Skipped
entries are not written, not enriched, do not take an entry-cap slot and do
not move the cursor; they are COUNTED, and the count lands on the poll's
provenance row (``source_poll_outcomes.age_skipped``, migration 0254) and in
the pull summary, never silently dropped. Undated entries are never skipped
(there is no evidence of their age).

The handler owns the number because the descriptor's config is parsed by the
handler's own schema (``extra='forbid'``): the rss kind reads
``first_poll_max_entry_age_days`` (default :data:`DEFAULT_FIRST_POLL_MAX_AGE`,
``0`` = no cap, for an archive that is wanted whole). A handler that
advertises nothing (or ``0`` / a non-positive value) is not capped, so every
kind that does not opt in keeps its historical first poll exactly — the
json_api kind already bounds its first run with its own ``lookback_minutes``
URL window, and series/portwatch kinds with ``backfill_days``.

P-5: the json_api kind ALSO drops, inside its own pull, items its API returns
dated at or before that window's start — silently, until now. It counts them
(:data:`HANDLER_AGE_SKIPPED_ATTR`) and the actor adds the count to the same
``age_skipped`` / ``first_poll_age_skipped`` / ``pull.done`` path. What is
ingested does not change. Drops at or before a CURSOR window's start (an item
already seen) are not "too old" and are not counted.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from .source_cursor import raw_feed_ts, utcnow

#: Handler attribute (seconds) a feed handler advertises; absent/<=0 = no cap.
FIRST_POLL_AGE_ATTR = "first_poll_max_entry_age_seconds"

#: Handler attribute (int) a handler that drops too-old items ITSELF sets
#: during a pull: how many it dropped (P-5). The json_api kind drops, on a pull
#: whose window is its ``lookback_minutes`` window (no cursor yet), every item
#: dated at or before the window start; the actor adds that count to this
#: poll's :class:`FirstPollAgeCap` (``absorb_handler_skips``). The handler is
#: built per poll, so the attribute is this poll's count.
HANDLER_AGE_SKIPPED_ATTR = "age_skipped_this_pull"

#: The default a feed kind applies when its descriptor says nothing.
DEFAULT_FIRST_POLL_MAX_AGE = timedelta(days=30)


def first_poll_age_cap_of(handler: Any) -> timedelta | None:
    """The first-poll max entry age a handler advertises, or None (no cap).

    Garbage, bools, NaN and non-positive values mean no cap — a bad value must
    degrade to the historical behaviour, never take a source offline.
    """
    try:
        raw = getattr(handler, FIRST_POLL_AGE_ATTR, None)
    except Exception:  # pragma: no cover — a handler property that raises
        raw = None
    if raw is None or isinstance(raw, bool):
        return None
    try:
        seconds = float(raw)
    except (TypeError, ValueError):
        return None
    if seconds != seconds or seconds <= 0 or seconds == float("inf"):
        return None
    return timedelta(seconds=seconds)


class FirstPollAgeCap:
    """One poll's age cap. Inactive unless this is a first poll (no cursor)
    and the handler advertises a cap. Built per poll."""

    def __init__(
        self, handler: Any, *, first_poll: bool, now: datetime | None = None,
    ) -> None:
        self.max_age = first_poll_age_cap_of(handler) if first_poll else None
        self.floor = (now or utcnow()) - self.max_age if self.max_age else None
        self.skipped = 0
        self.oldest_skipped: datetime | None = None

    @property
    def active(self) -> bool:
        return self.floor is not None

    def too_old(self, raw: Any) -> bool:
        """True (and counted) when the entry's own feed date is older than
        the cap. Undated entries and inactive caps never skip."""
        if self.floor is None:
            return False
        ts = raw_feed_ts(raw)
        if ts is None or ts >= self.floor:
            return False
        self.skipped += 1
        if self.oldest_skipped is None or ts < self.oldest_skipped:
            self.oldest_skipped = ts
        return True

    def absorb_handler_skips(self, handler: Any) -> int:
        """Add the entries the HANDLER skipped as too old for this poll
        (:data:`HANDLER_AGE_SKIPPED_ATTR`, P-5) to the count, so they reach the
        same poll row, summary key and log line. A handler that reports
        nothing (or garbage) adds 0. Returns what was added."""
        try:
            raw = getattr(handler, HANDLER_AGE_SKIPPED_ATTR, 0)
        except Exception:  # pragma: no cover — a handler property that raises
            raw = 0
        if isinstance(raw, bool) or not isinstance(raw, int) or raw <= 0:
            return 0
        self.skipped += raw
        return raw

    def outcome_count(self) -> int | None:
        """The ``age_skipped`` value for the poll row and the pull summary:
        the count when this poll skipped >= 1 entry, else None (the column is
        left out of the statement and the key out of the summary, so every
        poll that skipped nothing is recorded exactly as before)."""
        return self.skipped if self.skipped > 0 else None


__all__ = [
    "DEFAULT_FIRST_POLL_MAX_AGE",
    "FIRST_POLL_AGE_ATTR",
    "FirstPollAgeCap",
    "HANDLER_AGE_SKIPPED_ATTR",
    "first_poll_age_cap_of",
]
