# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The poll-path ``since`` cursor policy (W11-F151), a leaf beside ``source_actor``.

INCIDENT 2026-11-05 (consult dig 01): seven live feeds were polling "empty"
while their documents carried items we never ingested — ISW lost 17 of 20
posts in a week, State Dept the 13 Middle-East advisories re-issued 10-23, ICG
every dated English item since 10-12, Kremlin events posted after their event
time, and Maritime Executive / Taipei Times / HRW whatever arrived while one
future-dated entry held the cursor hours ahead of the clock. Fetching was fine
every time; the cursor discarded the items. Three rules close the class:

  * **Never in the future.** A feed-provided logical timestamp is clamped to
    NOW before it can advance the cursor (it was clamped only beyond now+26h,
    so an embargoed HRW item 11.6 h ahead, a Maritime Executive item 20 h ahead
    and every date-only Taipei Times item rode straight in), and a STORED
    cursor later than now + :data:`CURSOR_FUTURE_TOLERANCE` is poison, healed
    on read. The persisted signal keeps the feed's own date — only the cursor
    is clamped.
  * **Look back.** A handler for a DATED feed (rss, json_api) advertises
    ``cursor_lookback_seconds``; it is handed ``since - lookback`` instead of
    ``since``, so an item that appears in the feed AFTER the cursor passed its
    date (a date-only pubDate, a CDN edge serving a stale document, an item
    posted after its event, a mislabeled timezone) is still considered. The
    cursor itself only ever moves forward.
  * **Judged once, by identity.** What the look-back re-yields must not be
    re-enriched, re-gated or re-counted every poll, so the cursor row carries a
    small ledger of ``(entry id, logical ts)`` keys already judged inside the
    window; a re-yielded entry whose key is in the ledger is skipped before the
    enrichment chain and is not counted toward the poll's entry cap or its
    outcome. A re-DATED entry (a re-issued advisory keeps its guid and gets a
    new pubDate) has a new key and is judged again. An entry the poll skipped
    for a TRANSIENT reason (the W11-F103 enrichment hold) is not recorded, so
    the next poll retries it.

**A capped poll never passes an item it did not reach** (2026-11-06). ISW's
WordPress endpoint lists newest first; with NER at ~5 s per item the 30 s
budget capped every poll at 2 items, and the cursor moved to the NEWEST of the
two, past the older items the poll never reached. Once the cursor sat more than
the look-back ahead of them they were gone (11 of the last 20 posts). So a
dated handler's capped poll whose walk was not in ascending feed-time order
(:class:`WalkOrder`) holds the cursor where it was; the ledger carries the
progress (judged entries cost no cap slot), so each poll takes the next items
down, and the first poll that completes moves the cursor to the head. An
ascending walk (oldest first) keeps advancing to its last processed item, which
is exact for it. The alternative, sorting the drain oldest-first inside each
handler, would have to be repeated in every dated handler (rss, json_api, ...)
and still needs the cursor rule for a feed that is not sorted at all; the rule
here is one place and needs no handler to change. Handlers without a look-back
keep the historical anti-trap advance (they have no ledger, so holding would
re-grind the same capped items every poll).

For a dated handler an entry WITHOUT a feed date (an unparseable or missing
pubDate) no longer advances the cursor: it used to stand in as ``fetched_at``
(= now), which moved ICG's cursor to the poll time on every poll and dropped
every dated item that surfaced later. Handlers that stamp no logical timestamp
at all (telegram, gdelt, geojson, ...) advertise no look-back and keep the
historical policy exactly, except the clamp.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)

#: How far past NOW a STORED cursor may sit before it is treated as poison
#: (clock skew between writers). A feed timestamp advancing the cursor is
#: clamped to NOW itself, so a healthy cursor never exceeds now.
CURSOR_FUTURE_TOLERANCE = timedelta(minutes=5)

#: Default look-back for dated-feed handlers. 72 h covers every delay measured
#: in the 11-05 dig: ICG's stale-edge 304 pin forces a refetch only after 12
#: polls x 6 h = 72 h, Kremlin posts items up to ~71 h after their event time,
#: date-only pubDates (State Dept, Taipei Times) need >= 24 h, ISW's ET-as-UTC
#: offset 5 h. It stays well inside the 168 h S-4 intra-source dedupe window.
DEFAULT_LOOKBACK = timedelta(hours=72)

#: No handler or descriptor may ask for more: the ledger must cover the window.
LOOKBACK_CEILING = timedelta(hours=168)

#: Handler attribute (seconds) a dated-feed handler advertises.
LOOKBACK_ATTR = "cursor_lookback_seconds"

#: Cursor-row field carrying the judged-entry ledger ({key: epoch seconds}).
SEEN_FIELD = "lookback_seen"

#: Hard bound on the ledger (a feed lists tens to a few hundred items; the
#: bound only matters for a pathological feed and keeps the row small).
SEEN_MAX = 2000

#: Payload keys a handler stamps as the entry's own logical timestamp, in
#: precedence order. ``_cursor_dt`` is the rss ``cursor_timestamp: latest``
#: knob (max of published / updated); the rest are the historical keys.
_TS_KEYS = ("_cursor_dt", "_published_at_dt", "_last_seen_dt", "_event_dt")


def utcnow() -> datetime:
    """The clock the cursor policy reads (monkeypatchable in tests)."""
    return datetime.now(timezone.utc)


def _utc(ts: datetime) -> datetime:
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def _clamp(ts: datetime, now: datetime, source: Any) -> datetime:
    if ts <= now:
        return ts
    if ts > now + CURSOR_FUTURE_TOLERANCE:
        logger.warning(
            "source_actor.cursor.future_ts_clamped source=%s ts=%s now=%s "
            "(feed-provided timestamp in the future; cursor uses now, the "
            "payload keeps the feed's own date)",
            source, ts.isoformat(), now.isoformat(),
        )
    return now


def raw_feed_ts(sig: Any) -> datetime | None:
    """The entry's FEED-provided logical timestamp as the feed states it (not
    clamped); None when the handler stamped none (an undated entry)."""
    payload = getattr(sig, "payload", None) or {}
    for key in _TS_KEYS:
        val = payload.get(key)
        if isinstance(val, datetime):
            return _utc(val)
    return None


def feed_ts(sig: Any, *, now: datetime | None = None) -> datetime | None:
    """The entry's feed timestamp clamped to now — what may advance the cursor."""
    ts = raw_feed_ts(sig)
    if ts is None:
        return None
    payload = getattr(sig, "payload", None) or {}
    source = payload.get("source_id") or getattr(sig, "source_id", "?")
    return _clamp(ts, now or utcnow(), source)


def entry_logical_ts(sig: Any, *, now: datetime | None = None) -> datetime:
    """Historical policy: the feed timestamp (clamped), else ``fetched_at``."""
    now = now or utcnow()
    ts = feed_ts(sig, now=now)
    if ts is not None:
        return ts
    return min(_utc(sig.fetched_at), now)


def lookback_of(handler: Any) -> timedelta:
    """The look-back a handler advertises (0 = historical policy).

    Garbage, bools and non-positive values mean none; more than
    :data:`LOOKBACK_CEILING` is clamped to it.
    """
    try:
        raw = getattr(handler, LOOKBACK_ATTR, None)
    except Exception:  # pragma: no cover — a handler property that raises
        raw = None
    if raw is None or isinstance(raw, bool):
        return timedelta(0)
    try:
        seconds = float(raw)
    except (TypeError, ValueError):
        return timedelta(0)
    if seconds != seconds or seconds <= 0:
        return timedelta(0)
    return min(timedelta(seconds=seconds), LOOKBACK_CEILING)


def read_since(
    cursor_raw: Any, *, source: str, dated: bool, now: datetime | None = None,
) -> datetime | None:
    """The stored cursor, healed: a value later than now + tolerance is poison.

    Historical handlers get None for poison (a full re-scan, dedupe absorbs
    it); a dated handler gets NOW, so its look-back bounds the re-scan.
    """
    now = now or utcnow()
    iso = cursor_raw.get("last_pulled_at") if isinstance(cursor_raw, dict) else None
    if not isinstance(iso, str) or not iso:
        return None
    try:
        since = _utc(datetime.fromisoformat(iso))
    except ValueError:
        return None
    if since > now + CURSOR_FUTURE_TOLERANCE:
        logger.warning(
            "source_actor.cursor.poisoned_future source=%s stored=%s — %s "
            "(self-heal; dedupe absorbs the re-scan)",
            source, iso, "clamped to now" if dated else "discarding cursor",
        )
        return now if dated else None
    return since


def entry_identity(raw: Any) -> str:
    """The entry's own id (guid / external id), else its link, else its hash."""
    payload = getattr(raw, "payload", None) or {}
    return str(
        payload.get("external_id")
        or getattr(raw, "canonical_url", None)
        or payload.get("link")
        or getattr(raw, "content_hash", None)
        or payload.get("title")
        or ""
    )


class LookbackLedger:
    """One poll's look-back window + judged-entry ledger. Built per poll."""

    def __init__(self, cursor_raw: Any, lookback: timedelta) -> None:
        self.lookback = lookback
        self.skipped = 0
        prior = cursor_raw.get(SEEN_FIELD) if isinstance(cursor_raw, dict) else None
        self._seen: dict[str, int] = {}
        if isinstance(prior, dict):
            for k, v in prior.items():
                if isinstance(k, str) and isinstance(v, (int, float)) and not isinstance(v, bool):
                    self._seen[k] = int(v)

    @property
    def dated(self) -> bool:
        return self.lookback > timedelta(0)

    def handler_since(self, since: datetime | None) -> datetime | None:
        if since is None or not self.dated:
            return since
        return since - self.lookback

    @staticmethod
    def _key(raw: Any) -> tuple[str, datetime] | None:
        """``(key, ts)`` on the entry's id and its date AS THE FEED STATES IT
        (unclamped: an item dated ahead keeps one key while the clock passes
        its date); None for an undated entry (historical handling)."""
        ts = raw_feed_ts(raw)
        if ts is None:
            return None
        ident = f"{entry_identity(raw)}|{ts.isoformat()}"
        return hashlib.sha256(ident.encode("utf-8")).hexdigest()[:20], ts

    def judged(self, raw: Any) -> bool:
        """True when this dated entry was already judged inside the window."""
        if not self.dated:
            return False
        key = self._key(raw)
        if key is not None and key[0] in self._seen:
            self.skipped += 1
            return True
        return False

    def mark(self, raw: Any) -> None:
        if self.dated:
            key = self._key(raw)
            if key is not None:
                self._seen[key[0]] = int(key[1].timestamp())

    def cursor_fields(self, cursor_ts: datetime) -> dict[str, Any]:
        """The pruned ledger for the next cursor row (dated handlers only)."""
        if not self.dated:
            return {}
        floor = int((_utc(cursor_ts) - self.lookback - timedelta(hours=1)).timestamp())
        kept = sorted(
            ((k, v) for k, v in self._seen.items() if v >= floor),
            key=lambda kv: kv[1], reverse=True,
        )[:SEEN_MAX]
        return {SEEN_FIELD: dict(kept), "lookback_skipped_last_poll": self.skipped}


class WalkOrder:
    """Whether one poll's walk reached entries in ascending feed-time order.

    The actor calls :meth:`see` with the feed timestamp of every entry it
    REACHES that the ledger has not already judged, including the entry it
    stops on when it caps. Undated entries are ignored.
    """

    def __init__(self) -> None:
        self.ascending = True
        self._last: datetime | None = None

    def see(self, ts: datetime | None) -> None:
        if ts is None:
            return
        ts = _utc(ts)
        if self._last is not None and ts < self._last:
            self.ascending = False
        if self._last is None or ts > self._last:
            self._last = ts


def advance(
    *,
    errored: bool,
    since: datetime | None,
    last_processed_ts: datetime | None,
    hold: Any,
    dated: bool,
    now: datetime | None = None,
    capped: bool = False,
    walk_ascending: bool = True,
) -> datetime:
    """The cursor to persist after one poll — never later than ``now``
    (or the stored, already-tolerated ``since``).

      * hard error -> keep the prior position;
      * a dated handler's CAPPED poll whose walk was not ascending -> keep
        the prior position (entries older than what it processed were not
        reached; the ledger carries the progress — see the module docstring);
      * >= 1 dated entry processed -> its logical ts (the W11-F103 hold applied);
        for a dated handler never below the prior cursor (the look-back
        re-yields entries older than it);
      * nothing processed -> keep the prior position, or seed now on a
        first-ever pull.
    """
    now = now or utcnow()
    if errored or last_processed_ts is None:
        ts = since if since is not None else now
    elif dated and capped and not walk_ascending and since is not None:
        ts = since
    else:
        ts = hold.cursor_ts(last_processed_ts, since)
        if dated and since is not None:
            ts = max(_utc(ts), since)
    ts = _utc(ts)
    ceiling = max(now, since) if since is not None else now
    return min(ts, ceiling)


__all__ = [
    "CURSOR_FUTURE_TOLERANCE",
    "DEFAULT_LOOKBACK",
    "LOOKBACK_ATTR",
    "LOOKBACK_CEILING",
    "LookbackLedger",
    "SEEN_FIELD",
    "WalkOrder",
    "advance",
    "entry_identity",
    "entry_logical_ts",
    "feed_ts",
    "lookback_of",
    "raw_feed_ts",
    "read_since",
    "utcnow",
]
