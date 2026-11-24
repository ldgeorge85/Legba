# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Why a quiet source is quiet — the class ``/v3/system/source-firing`` carries
per row as ``quiet_class`` (U-9, the console's Watch surface).

A source that produced nothing in the firing route's 48 h floor is quiet for
one of a few reasons, and the reader must be able to tell "we looked and there
was nothing" from "we could not look". The liveness watchdog already makes
this call when it decides whether to raise a stall
(``runtime/liveness_watchdog.py::_source_stall_is_honest_quiet`` and
``_classify_streak``, B0-12 / W4b-F2); this is the same truth table over the
same evidence — the source's newest INGESTED signal and its latest poll's
outcome and newest OBSERVED upstream entry — so the console and the watchdog
never disagree about a source.

The closed list (``QUIET_CLASSES``):

* ``polling_fails``        — the source's polls are erroring (the route's
  ``status = 'error'``, or the latest poll was an error). Could not look.
* ``filtered_to_nothing``  — the latest poll was ``gated``: the feed answered
  and the keyword gate admitted none of it. Looked; nothing relevant.
* ``never_produced``       — the source has never landed a signal. Nothing
  to compare against, so it cannot yet be told apart from a fault.
* ``published_nothing``    — the latest poll was empty and the newest entry
  the feed showed is no newer than what is already ingested: the feed itself
  has published nothing new. Looked; nothing there.
* ``published_not_kept``   — the latest poll was empty but the feed showed an
  entry NEWER than anything ingested: it published and nothing was kept (the
  watchdog's ``cursor_fault``). Could not see what was there.
* ``unrecorded``           — quiet, but the latest poll left no evidence to
  decide by (no poll yet, a success with nothing landed, an empty poll
  without the upstream stamp).

``None`` for a source that is firing or paused (not quiet in this sense).

Registry-slim: stdlib only.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

QUIET_CLASSES: tuple[str, ...] = (
    "polling_fails",
    "filtered_to_nothing",
    "never_produced",
    "published_nothing",
    "published_not_kept",
    "unrecorded",
)


def _aware(v: Any) -> Optional[datetime]:
    if not isinstance(v, datetime):
        return None
    return v if v.tzinfo else v.replace(tzinfo=timezone.utc)


def quiet_class(
    *,
    status: str,
    last_seen_at: Any,
    last_poll_outcome: Optional[str],
    newest_entry_at: Any,
) -> Optional[str]:
    """The quiet class of one source row (see the module docstring)."""
    if status not in ("silent", "error"):
        return None
    outcome = (last_poll_outcome or "").strip()
    if status == "error" or outcome == "error":
        return "polling_fails"
    if outcome == "gated":
        return "filtered_to_nothing"
    last_signal = _aware(last_seen_at)
    if last_signal is None:
        return "never_produced"
    if outcome == "empty":
        newest = _aware(newest_entry_at)
        if newest is None:
            return "unrecorded"
        return "published_nothing" if newest <= last_signal else "published_not_kept"
    return "unrecorded"


__all__ = ["QUIET_CLASSES", "quiet_class"]
