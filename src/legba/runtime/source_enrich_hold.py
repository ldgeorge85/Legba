# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The per-poll enrichment HOLD: a transient skip never advances the cursor
past itself (W11-F103), in a stdlib-only leaf beside ``source_actor``.

INCIDENT 2026-10-23: the hosted NLP service died and every per-entry
enrichment call hung until ``_ENRICH_TIMEOUT_S``. ``SourceCore._process_one``
skipped the timed-out entries ("the next poll re-pulls it"), but the pull loop
had already counted them into ``last_processed_ts`` and the cursor advanced to
it, so the next poll's ``since`` sat PAST them: a morning's intake was dropped,
not deferred (recovered by a manual cursor rewind). Three rules close it:

  * **Cursor hold.** A TRANSIENT skip (the enrichment timeout, or an
    ``NlpServiceUnavailable`` escaping the chain) records the entry's logical
    timestamp. At cursor-advance the actor writes
    ``max(since, min(last_processed_ts, earliest_skipped_ts - CURSOR_EPSILON))``.
    ``since`` is a strict-after bound on the next pull (rss drops
    ``published_at <= since``; telegram / json_api are inclusive, which ε also
    satisfies), so ``ts - 1µs`` re-pulls the skipped entry and everything
    sharing or following its timestamp; the content-hash dedup absorbs the
    re-pulled entries that were written. It is the EARLIEST skipped timestamp,
    not the first in iteration order, because feeds iterate newest-first as
    often as oldest-first. The ``max(since, ...)`` clamp means the hold never
    rewinds below the prior cursor (a handler that ignores ``since`` re-yields
    the entry anyway). PERMANENT drops (keyword gate, baseline filter, S-4
    collapse, conflict no-op) stay processed, exactly as before.
  * **Bounded hold.** The skipped entry's key and a consecutive-skip count ride
    the cursor row (``enrich_hold``). Once the same entry has been skipped on
    :data:`ENRICH_HOLD_MAX_SKIPS` consecutive polls it is written UNENRICHED
    (the baseline without the enrichment chain — the same signal the chain's
    own error fallback produces) with ``payload.enrichment_pending`` naming why,
    so a dead NLP plane can never stall a source forever.
  * **Degraded for the rest of the poll.** The first transient skip marks the
    NLP plane degraded for the remainder of THAT pull: every later entry runs
    the baseline without the enrichment chain and is written unenriched
    (``enrichment_pending``) instead of paying the timeout again — one 12 s
    stall per poll, not one per entry. The next poll tries the plane afresh.

A bulk (high-water-offset) source holds its offset at the first skipped row's
position in the walk and never reports end-of-stream on a held poll.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from typing import Any

#: Consecutive polls the SAME entry may be skipped for a transient enrichment
#: failure before it is written unenriched. 3 polls is ~15 min at the modal
#: */5 cadence: long enough to ride out a models-host restart, short enough
#: that a dead plane cannot hold a source's intake for a morning again.
ENRICH_HOLD_MAX_SKIPS = 3

#: The step back from a skipped entry's timestamp: the smallest datetime step,
#: so the next pull's strict-after ``since`` filter re-yields that entry.
CURSOR_EPSILON = timedelta(microseconds=1)

#: ``payload.enrichment_pending`` values (why the NLP stages were bypassed).
PENDING_DEGRADED = "nlp_degraded_this_poll"
PENDING_HOLD_EXHAUSTED = "enrich_hold_exhausted"

_CURSOR_KEY = "enrich_hold"


def _utc(ts: datetime) -> datetime:
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def entry_key(raw: Any, ts: datetime) -> str:
    """A stable identity for one yielded entry, read BEFORE the baseline runs
    (tier 1 may stamp ``content_hash`` in place, so reading after would differ
    between the skipping poll and the next one)."""
    payload = getattr(raw, "payload", None) or {}
    ident = (
        getattr(raw, "canonical_url", None)
        or getattr(raw, "content_hash", None)
        or f"{payload.get('title') or ''}|{_utc(ts).isoformat()}"
    )
    return hashlib.sha256(str(ident).encode("utf-8")).hexdigest()[:32]


class EnrichHold:
    """One poll's transient-skip ledger. Built per ``pull_once``."""

    def __init__(self, prior_cursor: Any) -> None:
        prior = prior_cursor if isinstance(prior_cursor, dict) else {}
        held = prior.get(_CURSOR_KEY)
        self._prior: dict[str, Any] | None = held if isinstance(held, dict) else None
        self.degraded: str | None = None   # why NLP is bypassed for the rest of the poll
        self.skipped = 0                   # transient skips this poll
        self.bypassed = 0                  # entries that went through unenriched
        self._cur: tuple[str, datetime, int] | None = None
        self._min_skip: tuple[datetime, str] | None = None
        self._first_skip_index: int | None = None

    # -- per entry ----------------------------------------------------------

    def at(self, raw: Any, ts: datetime, index: int) -> None:
        """The pull loop announces the entry about to be processed."""
        self._cur = (entry_key(raw, ts), _utc(ts), index)

    def bypass_reason(self) -> str | None:
        """Why the current entry must skip the enrichment chain, or None."""
        if self.degraded is not None:
            return PENDING_DEGRADED
        prior = self._prior
        if self._cur is not None and prior is not None:
            try:
                skips = int(prior.get("skips") or 0)
            except (TypeError, ValueError):
                skips = 0
            if prior.get("key") == self._cur[0] and skips >= ENRICH_HOLD_MAX_SKIPS:
                return PENDING_HOLD_EXHAUSTED
        return None

    def skip(self, reason: str) -> None:
        """Record a transient skip of the current entry; degrade the poll."""
        self.skipped += 1
        if self.degraded is None:
            self.degraded = reason
        if self._cur is None:
            return
        key, ts, index = self._cur
        if self._min_skip is None or ts < self._min_skip[0]:
            self._min_skip = (ts, key)
        if self._first_skip_index is None:
            self._first_skip_index = index

    def mark_unenriched(self, signal: Any, reason: str) -> None:
        signal.payload["enrichment_pending"] = reason
        self.bypassed += 1

    # -- cursor advance -----------------------------------------------------

    def cursor_ts(self, last_processed_ts: datetime, since: datetime | None) -> datetime:
        """The ``since`` to persist for a pull that consumed >=1 entry."""
        if self._min_skip is None:
            return last_processed_ts
        held = min(_utc(last_processed_ts), self._min_skip[0] - CURSOR_EPSILON)
        return held if since is None else max(_utc(since), held)

    def bulk_rows(self, rows: int, reached_end: bool) -> tuple[int, bool]:
        """(rows to advance the bulk offset by, reached_end) under the hold."""
        if self._first_skip_index is None:
            return rows, reached_end
        return min(rows, self._first_skip_index), False

    def cursor_fields(self, *, errored: bool) -> dict[str, Any]:
        """The ``enrich_hold`` cursor field (+ this poll's counters).

        Hard error -> the prior hold rides forward unchanged; a skip -> the
        earliest skipped entry with its consecutive count; else cleared.
        """
        if errored:
            held = self._prior
        elif self._min_skip is not None:
            ts, key = self._min_skip
            prior = self._prior or {}
            try:
                prior_skips = int(prior.get("skips") or 0)
            except (TypeError, ValueError):
                prior_skips = 0
            skips = prior_skips + 1 if prior.get("key") == key else 1
            held = {"key": key, "ts": ts.isoformat(), "skips": skips}
        else:
            held = None
        return {
            _CURSOR_KEY: held,
            "enrichment_skipped_last_poll": self.skipped,
            "enrichment_bypassed_last_poll": self.bypassed,
        }


__all__ = [
    "CURSOR_EPSILON",
    "ENRICH_HOLD_MAX_SKIPS",
    "EnrichHold",
    "PENDING_DEGRADED",
    "PENDING_HOLD_EXHAUSTED",
    "entry_key",
]
