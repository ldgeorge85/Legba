# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Source STALENESS — "no new items for N× the feed's expected interval".

A feed can poll cleanly (HTTP 200, outcome='empty' or 'gated', health 'healthy') and
still have delivered NO NEW ITEMS for days: it looks healthy and is useless.
``get_source_health`` already carried the raw signals (``silent_hours``,
``feed_has_no_new_items``); nothing turned them into a visible, alertable
state. This leaf is that state, and it is the ONE place staleness is decided:

* :func:`staleness_state` — the pure decision. ``fresh`` < 3× < ``quiet`` < 8×
  < ``stale`` < 24× <= ``dead`` (multiples of the feed's EXPECTED interval);
  ``unknown`` when there is nothing to measure (never delivered, no cadence) —
  absence is absence, never a 0.
* :func:`feed_has_no_new_items` — the B.9 honest-quiet discriminator, defined
  here once; ``get_source_health`` and the watchdog both read it from here.
* :func:`should_alert` — the trip policy over a staleness dict.
* :func:`fetch_source_profiles` — the per-source publishing profile (observed
  interval, weekday-only?) derived from ``signals`` — NO new column.

Leaf: imports nothing from the substrate port, the watchdog, or the source
health read (all of which import THIS), so it cannot cycle.

THRESHOLDS (module constants; multiples of the expected interval)::

    factor <  QUIET_FACTOR  (3)   fresh
    factor <  STALE_FACTOR  (8)   quiet   -- a longer-than-usual gap, nothing more
    factor < DEAD_FACTOR   (24)   stale   -- trips an alert (see should_alert)
    factor >= DEAD_FACTOR         dead    -- trips an alert, always

EXPECTED INTERVAL. The descriptor's ``body.cadence.schedule.raw`` is how often
we ASK, not how often the feed PUBLISHES: 8× a 15-minute poll is two hours,
which would mark every thin feed stale every night. So the caller's expected
interval is ``max(poll period, observed publishing interval)``
(:func:`expected_interval_s`), where the observed interval is the feed's own
rhythm over the last 28 days: the span from its first to its last item over
the gaps between the distinct hours that carried an item (a burst publisher
that posts twenty items at 06:00 daily reads 24 h, not 1.2 h; a reference
feed with two items a month reads two weeks, not its 6-hour poll). One item
in the whole window reads the window (28 d). A feed with NO item in the
window, or whose items all landed inside one hour (a backlog burst), has no
observed rhythm and falls back to the poll period alone.

WEEKEND. ~30 weekday publishers go quiet Saturday and Sunday; flagging them on
a Monday morning is the false positive this tripwire must not produce. A feed
is ``weekday_only`` when at least 12 of its last-28-day items exist and at most
10% of them landed on a Saturday or Sunday (UTC) — a 24/7 feed sits near 28%.
For a ``weekday_only`` feed the age is BUSINESS time: the wall-clock gap minus
the time that fell inside Saturday 00:00 - Monday 00:00 UTC, i.e. measured
against the last weekday window, so a Friday-evening item is ~1 h old on
Monday 01:00. A 24/7 feed is measured on the wall clock, weekends included.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

__all__ = [
    "QUIET_FACTOR",
    "STALE_FACTOR",
    "DEAD_FACTOR",
    "FEED_EMPTY_SLACK",
    "STATES",
    "staleness_state",
    "feed_has_no_new_items",
    "should_alert",
    "business_elapsed_s",
    "poll_period_s",
    "expected_interval_s",
    "staleness_for",
    "fetch_source_profiles",
    "fetch_poll_evidence",
    "reset_profile_cache",
]

#: Multiples of the expected interval at which the state steps up.
QUIET_FACTOR = 3.0
STALE_FACTOR = 8.0
DEAD_FACTOR = 24.0

#: Clock-skew slack for the honest-quiet discriminator (the feed's own entry
#: timestamps vs our fetch time can disagree by seconds). Mirrors the value
#: ``get_source_health`` carried before this module existed.
FEED_EMPTY_SLACK = timedelta(minutes=5)

STATES = ("fresh", "quiet", "stale", "dead", "unknown")

#: Profile window and the minimums below which a profile claims nothing.
PROFILE_WINDOW_DAYS = 28
_MIN_ITEMS_FOR_WEEKDAY = 12
_WEEKEND_SHARE_MAX = 0.10
_PROFILE_TTL_S = 1800.0

_SECONDS_PER_DAY = 86400.0
#: The weekend :func:`business_elapsed_s` leaves out: Saturday 00:00 - Monday
#: 00:00 UTC. The empty-streak page's quiet floor is built on it
#: (``source_quiet_evidence.QUIET_PAGE_MIN_ELAPSED_S``).
WEEKEND_S = 2 * _SECONDS_PER_DAY


def _utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def _iso(dt: datetime | None) -> str | None:
    return _utc(dt).isoformat(timespec="seconds") if isinstance(dt, datetime) else None


def business_elapsed_s(start: datetime, end: datetime) -> float:
    """Seconds from ``start`` to ``end`` NOT inside a UTC weekend
    (Saturday 00:00 - Monday 00:00). Never negative."""
    start, end = _utc(start), _utc(end)
    if end <= start:
        return 0.0
    total = (end - start).total_seconds()
    day = start.replace(hour=0, minute=0, second=0, microsecond=0)
    weekend = 0.0
    while day < end:
        if day.weekday() >= 5:  # Sat=5, Sun=6
            nxt = day + timedelta(days=1)
            overlap = (min(end, nxt) - max(start, day)).total_seconds()
            if overlap > 0:
                weekend += overlap
        day += timedelta(days=1)
    return max(0.0, total - weekend)


def feed_has_no_new_items(
    last_poll_outcome: str | None,
    newest_entry_ts: datetime | None,
    last_signal_at: datetime | None,
) -> bool:
    """B.9 honest-quiet: the last poll came back 'empty' AND the newest
    OBSERVED upstream entry is no newer than our newest ingested signal (plus
    :data:`FEED_EMPTY_SLACK`). The upstream simply has nothing newer — the
    feed is quiet, not broken."""
    return (
        last_poll_outcome == "empty"
        and isinstance(newest_entry_ts, datetime)
        and isinstance(last_signal_at, datetime)
        and _utc(newest_entry_ts) <= _utc(last_signal_at) + FEED_EMPTY_SLACK
    )


def _fmt_interval(seconds: float) -> str:
    if seconds >= 2 * 3600:
        return f"{seconds / 3600.0:.1f}h"
    return f"{seconds / 60.0:.0f}min"


def staleness_state(
    last_new_item_at: datetime | None,
    last_poll_ok_at: datetime | None,
    expected_interval_s: float | None,
    now: datetime,
    weekday_only: bool = False,
    upstream_quiet: bool | None = None,
    last_poll_gated: bool = False,
) -> dict[str, Any]:
    """The staleness of one source: ``{state, factor, reason, ...}``.

    ``last_new_item_at`` — when the newest NEW item was ingested (``None``:
    never delivered). ``last_poll_ok_at`` — the newest poll that did not error
    (``None``: none on record); it never moves the state, it explains it.
    ``expected_interval_s`` — see the module docstring (``None`` or <= 0: no
    cadence to measure against). ``upstream_quiet`` — the
    :func:`feed_has_no_new_items` verdict, carried so :func:`should_alert` can
    tell a quiet feed from an ingestion fault. ``last_poll_gated`` — the newest
    poll came back ``outcome='gated'`` (migration 0226: the feed delivered, the
    keyword gate dropped every entry). It is a CLEAN poll (``last_poll_ok_at``
    already counts it) but NOT a new item: ``last_new_item_at`` and the state
    are untouched; it only changes what the ``reason`` says is wrong.

    ``factor`` is the age over the expected interval (``None`` when unknown),
    ``age_s`` the age measured (business time for ``weekday_only``).
    """
    now = _utc(now)
    out: dict[str, Any] = {
        "state": "unknown",
        "factor": None,
        "reason": "",
        "age_s": None,
        "expected_interval_s": (
            round(float(expected_interval_s), 1)
            if isinstance(expected_interval_s, (int, float)) and expected_interval_s > 0
            else None
        ),
        "weekday_only": bool(weekday_only),
        "upstream_quiet": upstream_quiet,
        "last_poll_gated": bool(last_poll_gated),
        "last_new_item_at": _iso(last_new_item_at),
        "last_poll_ok_at": _iso(last_poll_ok_at),
    }
    if not isinstance(last_new_item_at, datetime):
        out["reason"] = "no new item has ever been recorded for this source"
        return out
    if out["expected_interval_s"] is None:
        out["reason"] = "no cadence to measure against (no poll schedule on the descriptor)"
        return out
    expected = float(out["expected_interval_s"])
    age = (
        business_elapsed_s(last_new_item_at, now)
        if weekday_only
        else max(0.0, (now - _utc(last_new_item_at)).total_seconds())
    )
    factor = age / expected
    if factor >= DEAD_FACTOR:
        state = "dead"
    elif factor >= STALE_FACTOR:
        state = "stale"
    elif factor >= QUIET_FACTOR:
        state = "quiet"
    else:
        state = "fresh"
    out.update(state=state, factor=round(factor, 1), age_s=round(age, 1))
    basis = "weekday time" if weekday_only else "wall clock"
    reason = (
        f"{state} · {factor:.1f}× its {_fmt_interval(expected)} expected interval "
        f"({basis}); last new item {out['last_new_item_at']}"
    )
    if state in ("quiet", "stale", "dead"):
        if upstream_quiet is True:
            reason += "; polls clean and upstream carries nothing newer"
        elif not isinstance(last_poll_ok_at, datetime):
            reason += "; no clean poll on record"
        elif (now - _utc(last_poll_ok_at)).total_seconds() > STALE_FACTOR * expected:
            reason += f"; last clean poll {out['last_poll_ok_at']} — the poller itself is behind"
        elif last_poll_gated:
            reason += (
                "; polling is clean and the feed delivered, but the keyword "
                "gate dropped every entry (gated) — nothing on-topic arrived"
            )
        else:
            reason += "; polling is clean — the upstream is not delivering"
    out["reason"] = reason
    return out


def should_alert(staleness: Mapping[str, Any]) -> str | None:
    """The trip policy. ``'dead'`` always trips; ``'stale'`` trips unless the
    feed is honestly quiet (clean empty poll, nothing newer upstream — the
    weekly/monthly-feed class the watchdog already exempts, which escalates
    only once it is DEAD). Returns the tier to alert on, or ``None``."""
    state = staleness.get("state")
    if state == "dead":
        return "dead"
    if state == "stale" and staleness.get("upstream_quiet") is not True:
        return "stale"
    return None


def poll_period_s(cron: str | None, now: datetime) -> float | None:
    """The poll schedule's period in seconds (``None``: absent or invalid)."""
    if not cron or not cron.strip():
        return None
    from .dapr_cron import cron_to_reminder_timing  # croniter: import on use

    try:
        _due, period = cron_to_reminder_timing(cron.strip(), base_time=_utc(now))
    except ValueError:
        return None
    seconds = period.total_seconds()
    return seconds if seconds > 0 else None


def expected_interval_s(
    cron: str | None, profile: Mapping[str, Any] | None, now: datetime
) -> float | None:
    """``max(poll period, observed publishing interval)`` — see the module
    docstring. ``None`` when neither is known."""
    candidates = [
        v
        for v in (
            poll_period_s(cron, now),
            (profile or {}).get("observed_interval_s"),
        )
        if isinstance(v, (int, float)) and v > 0
    ]
    return float(max(candidates)) if candidates else None


def staleness_for(
    *,
    last_signal_at: datetime | None,
    cadence_raw: str | None,
    profile: Mapping[str, Any] | None,
    last_poll_outcome: str | None,
    newest_entry_ts: datetime | None,
    last_ok_poll_at: datetime | None,
    now: datetime,
) -> dict[str, Any]:
    """The whole composition for one source — cadence + publishing profile +
    poll evidence -> the staleness dict. Every reader (``get_source_health``,
    the ``/v3/source-quality`` route, the watchdog via the former) goes
    through THIS, so the verdicts cannot drift."""
    return staleness_state(
        last_signal_at if isinstance(last_signal_at, datetime) else None,
        last_ok_poll_at,
        expected_interval_s(cadence_raw, profile, now),
        now,
        weekday_only=bool((profile or {}).get("weekday_only")),
        upstream_quiet=feed_has_no_new_items(
            last_poll_outcome, newest_entry_ts, last_signal_at
        ),
        last_poll_gated=last_poll_outcome == "gated",
    )


# -- per-source publishing profile (derived from ``signals``) ----------------

_PROFILE_SQL = f"""
SELECT source_id,
       count(*)                                           AS n_items,
       min(fetched_at)                                    AS first_at,
       max(fetched_at)                                    AS last_at,
       count(DISTINCT date_trunc('hour', fetched_at))     AS active_hours,
       count(*) FILTER (
         WHERE extract(isodow FROM fetched_at AT TIME ZONE 'UTC') IN (6, 7)
       )                                                  AS weekend_items
  FROM signals
 WHERE fetched_at > now() - interval '{PROFILE_WINDOW_DAYS} days'
 GROUP BY source_id
"""

_profile_cache: dict[str, Any] = {"at": 0.0, "profiles": {}}


def reset_profile_cache() -> None:
    """Drop the in-process profile cache (tests; and a manual refresh)."""
    _profile_cache.update(at=0.0, profiles={})


def profile_from_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """One source's publishing profile from its aggregate row (pure)."""
    n = int(row.get("n_items") or 0)
    hours = int(row.get("active_hours") or 0)
    first, last = row.get("first_at"), row.get("last_at")
    weekend = int(row.get("weekend_items") or 0)
    observed: float | None = None
    if hours >= 2 and isinstance(first, datetime) and isinstance(last, datetime):
        # Mean gap between active hours. Measured first-to-LAST item, so a
        # feed that has since died does not inflate its own rhythm.
        span = max((_utc(last) - _utc(first)).total_seconds(), 3600.0)
        observed = span / (hours - 1)
    elif hours == 1 and n == 1:
        # One item in the whole window: its rhythm is at least the window.
        # (Several items inside ONE hour is a backlog burst, which says nothing
        # about a rhythm: no observed interval, the poll period alone.)
        observed = PROFILE_WINDOW_DAYS * _SECONDS_PER_DAY
    weekday_only = n >= _MIN_ITEMS_FOR_WEEKDAY and (weekend / n) <= _WEEKEND_SHARE_MAX
    return {
        "observed_interval_s": observed,
        "weekday_only": weekday_only,
        "n_items": n,
        "weekend_items": weekend,
    }


async def fetch_source_profiles(
    pool: Any, *, ttl_s: float = _PROFILE_TTL_S
) -> dict[str, dict[str, Any]]:
    """``{source_id: profile}`` over the last 28 days of ``signals``, cached
    in-process for ``ttl_s`` (the profile moves over weeks, not minutes; one
    grouped scan of ``signals_fetched_at_idx``, ~150 ms live). A failed read
    returns the previous profiles (stale beats missing) and is never raised."""
    now_m = time.monotonic()
    if _profile_cache["profiles"] and (now_m - _profile_cache["at"]) < ttl_s:
        return _profile_cache["profiles"]
    try:
        async with pool.acquire() as conn:
            records = await conn.fetch(_PROFILE_SQL)
    except Exception:  # pragma: no cover — degrade to the last known profiles
        return _profile_cache["profiles"]
    profiles = {str(r["source_id"]): profile_from_row(r) for r in records}
    _profile_cache.update(at=now_m, profiles=profiles)
    return profiles


#: Per-source poll evidence for a set of ids: the newest poll's outcome and
#: newest observed upstream entry, and the newest poll that did not error.
#: Both sub-selects ride ``source_poll_outcomes_source_time_idx``
#: (source_id, occurred_at DESC): one index descent per id.
_POLL_EVIDENCE_SQL = """
SELECT u.sid AS source_id,
       po.outcome AS last_poll_outcome,
       po.newest_entry_ts,
       ok.last_ok_poll_at
  FROM unnest($1::text[]) AS u(sid)
  LEFT JOIN LATERAL (
        SELECT outcome, newest_entry_ts
          FROM source_poll_outcomes
         WHERE source_id = u.sid
         ORDER BY occurred_at DESC
         LIMIT 1
       ) po ON TRUE
  LEFT JOIN LATERAL (
        SELECT max(occurred_at) AS last_ok_poll_at
          FROM source_poll_outcomes
         WHERE source_id = u.sid AND outcome <> 'error'
       ) ok ON TRUE
"""


async def fetch_poll_evidence(
    pool: Any, source_ids: list[str]
) -> dict[str, dict[str, Any]]:
    """``{source_id: {last_poll_outcome, newest_entry_ts, last_ok_poll_at}}``
    for the ``/v3/source-quality`` route (``get_source_health`` carries the
    same three columns in its own query). Raises on a DB error; the caller
    decides what an unreadable ledger means."""
    if not source_ids:
        return {}
    async with pool.acquire() as conn:
        records = await conn.fetch(_POLL_EVIDENCE_SQL, list(source_ids))
    return {
        str(r["source_id"]): {
            "last_poll_outcome": r["last_poll_outcome"],
            "newest_entry_ts": r["newest_entry_ts"],
            "last_ok_poll_at": r["last_ok_poll_at"],
        }
        for r in records
    }
