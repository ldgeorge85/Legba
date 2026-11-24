# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The liveness watchdog's honest-quiet evidence rules (split out of
``liveness_watchdog`` in roll 86; that module re-exports every name here),
and the pure empty-streak decision that applies them (moved with its roll-86
weekend floor).

Pure functions over one source's freshest poll evidence: is a cadence-stale or
empty-streaking source simply QUIET upstream (no alert), or did the cursor or
filter eat what upstream published?
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .source_first_poll_age import DEFAULT_FIRST_POLL_MAX_AGE
from .source_staleness import WEEKEND_S

__all__ = [
    "QUIET_PAGE_MIN_ELAPSED_S",
    "_DEFAULT_HONEST_QUIET_STREAK",
    "_evaluate_empty_streaks",
    "_as_aware_utc",
    "_classify_streak",
    "_source_stall_is_honest_quiet",
    "quiet_long_enough_to_page",
]

#: The prolonged honest-quiet poll count (the 2026-08-27 sweep; the reasoning
#: and its env knob are in ``liveness_watchdog``).
_DEFAULT_HONEST_QUIET_STREAK = 36

#: Roll 86 — the honest-quiet page's WEEKEND FLOOR. The prolonged-quiet rule
#: counts polls (36 by default), and 36 polls of an hourly source is 36 h:
#: shorter than a weekend, so a weekday-only feed (source.ansa.english) paged
#: every Saturday. A streak now also has to have lasted this long, measured
#: from the source's last ingested signal: the weekend ``source_staleness``
#: leaves out of business time (Saturday 00:00 - Monday 00:00 UTC, 48 h) plus
#: one day for its edges (a Friday-midday last item to a Monday-morning first
#: is ~69 h) = 72 h. Polls alone still decide on a slow source (36 polls of a
#: 6-hourly feed is 9 days). A source descriptor's
#: ``cadence.quiet_page_after_hours`` replaces the floor (0 = polls alone).
QUIET_PAGE_MIN_ELAPSED_S = WEEKEND_S + 86_400.0


def quiet_long_enough_to_page(
    quiet_since: datetime | None, newest_empty_at: datetime | None, declared_hours: Any = None,
) -> bool:
    """Has an honest-quiet run lasted past the floor (see the constant)?

    ``quiet_since``: when the quiet began — the source's newest ingested signal
    (or, never ingested, the feed's newest observed entry). ``newest_empty_at``:
    the run's newest empty poll. ``declared_hours``: the descriptor's
    ``cadence.quiet_page_after_hours`` as read (text from the body, an int, or
    None); unreadable or negative falls back to the floor. With no start to
    measure from, the poll count alone decides (the pre-floor rule).
    """
    floor_s = QUIET_PAGE_MIN_ELAPSED_S
    try:
        declared = int(str(declared_hours)) if declared_hours is not None else None
    except ValueError:
        declared = None
    if declared is not None and declared >= 0:
        floor_s = declared * 3600.0
    if floor_s <= 0 or not isinstance(quiet_since, datetime) or not isinstance(newest_empty_at, datetime):
        return True
    elapsed = (_as_aware_utc(newest_empty_at) - _as_aware_utc(quiet_since)).total_seconds()
    return elapsed >= floor_s


def _as_aware_utc(dt: datetime) -> datetime:
    """Normalize a possibly-naive datetime to aware UTC for comparison."""
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _classify_streak(
    newest_entry_ts: datetime | None, last_signal: datetime | None, polled_at: datetime | None = None,
) -> str:
    """B0-12 empty-streak precision discriminator — the truth table.

    ``newest_entry_ts``: the newest upstream-entry timestamp the handler
    OBSERVED across the streak's polls (pre-since-filter, future-skew-clamped
    at the handler). ``last_signal``: the source's newest INGESTED signal.

      * no observation             → 'unknown'      (keep the old degraded path)
      * never ingested + observed  → 'cursor_fault' (feed has entries, we
                                                     yielded none — ever), unless the
        newest was older than the first-poll age cap at ``polled_at`` → 'honest_quiet'
      * observed >  last ingest    → 'cursor_fault' (upstream published, the
                                                     cursor/filter ate it)
      * observed <= last ingest    → 'honest_quiet' (feed is simply quiet —
                                                     NO alert)
    """
    if newest_entry_ts is None:
        return "unknown"
    if last_signal is None:  # W11-F265: an entry older than the first-poll age cap when polled was skipped by design
        stale = polled_at is not None and _as_aware_utc(newest_entry_ts) < _as_aware_utc(polled_at) - DEFAULT_FIRST_POLL_MAX_AGE
        return "honest_quiet" if stale else "cursor_fault"
    if _as_aware_utc(newest_entry_ts) > _as_aware_utc(last_signal):
        return "cursor_fault"
    return "honest_quiet"


def _source_stall_is_honest_quiet(row: dict[str, Any]) -> bool:
    """B0-12 cadence-precision gate: is this cadence-stale source just QUIET?

    True when the source's freshest poll evidence shows a clean empty poll
    whose newest OBSERVED upstream entry is <= the source's newest ingested
    signal — the feed itself has produced nothing new since we last ingested
    (weekly/monthly feeds, slow news day), so a staleness alert would be the
    B0-11 false-positive class. Any error outcome, missing evidence, or a
    never-ingested source keeps the alert.

    W4b-F2: a last poll with outcome ``'gated'`` (migration 0226 — the feed
    delivered, the keyword gate dropped every entry) is honest-quiet too: the
    source is alive and answering, it simply has nothing the gate admits, so
    the CADENCE alert stays silent. (The empty-streak check already treats
    'gated' as productive.)
    """
    outcome = row.get("last_poll_outcome") or ""
    if outcome == "gated":
        return True
    # Roll 86: a 'success' that wrote NOTHING is the same evidence as an empty
    # poll. The geojson safety valve re-yields every feature on its 13th empty
    # poll (geojson.py force_unconditional); the unchanged features collapse
    # at the sink's duplicate check (reserve_unchanged) and the poll records
    # 'success' with signals_written 0. Read as a stall, it paged a quiet
    # feed every 6.5 h (source.usgs.earthquakes_m45, 5 pages in ~26 h).
    zero_write_success = (
        outcome == "success" and row.get("last_poll_signals_written") == 0
    )
    if outcome != "empty" and not zero_write_success:
        return False
    newest = row.get("last_poll_newest_entry_ts")
    last_signal = row.get("last_signal")
    if not isinstance(newest, datetime) or not isinstance(last_signal, datetime):
        return False
    return _as_aware_utc(newest) <= _as_aware_utc(last_signal)


def _evaluate_empty_streaks(
    rows: Any,
    *,
    threshold: int,
    honest_quiet_threshold: int = _DEFAULT_HONEST_QUIET_STREAK,
) -> list[tuple[str, int, Any, str]]:
    """Pure empty-streak decision over poll-outcome rows — no DB, unit-testable.

    ``rows``: iterable of mappings with ``source_id``, ``outcome``
    ('empty'|'error'|'success'), ``occurred_at`` (tz-aware datetime), and
    (optionally) ``last_signal`` (tz-aware datetime — the source's newest
    produced signal) and ``newest_entry_ts`` (the handler's
    newest-observed-upstream-entry evidence for that poll — B0-12), already
    grouped per source and ordered NEWEST-FIRST (the SQL guarantees this). For
    each source, count the leading run of consecutive 'empty' rows; the run
    breaks on the first non-'empty' row — an 'error', since migration 0114 a
    'success', or since 0226 a 'gated' (the feed delivered, the keyword gate
    dropped it all: productive, never a stall). Returns ``(source_id, streak_len, newest_empty_at,
    fault_class)`` for every source whose leading empty run is >= ``threshold``
    and is NOT producing again — INCLUDING a run classified honest-quiet, once
    it separately clears ``honest_quiet_threshold``.

    B0-12 discriminator: the run's newest non-null ``newest_entry_ts`` is
    compared against ``last_signal`` by :func:`_classify_streak` —
    'honest_quiet' runs (upstream's newest observed entry <= our last ingest:
    the feed is simply quiet) stay SILENT below ``honest_quiet_threshold`` (7/8
    of the B0-11 "stalled cluster" were this class) but escalate as
    ``'honest_quiet_prolonged'`` once the streak reaches it — the 2026-08-27
    sweep's source.wto.news gap: 110 consecutive empty/healthy polls, every one
    honestly evidenced quiet, that the pre-fix rule would have silenced
    forever regardless of length. 'cursor_fault' runs (upstream carries NEWER
    entries yet 0 yielded) and 'unknown' runs (no evidence — handlers that
    don't record the observation, pre-B0-12 rows) are returned at the lower
    ``threshold`` exactly as before.

    Roll 86 WEEKEND FLOOR: a prolonged honest-quiet run also has to have
    lasted :data:`QUIET_PAGE_MIN_ELAPSED_S` since the source's last ingested
    signal (:func:`quiet_long_enough_to_page`; a row's optional
    ``quiet_page_after_hours`` is the descriptor's own floor). Only that class
    waits: a cursor fault is evidence of a fault, and an unevidenced run is the
    fast dead-feed check (xinhua/aljazeera) the floor must not slow down.

    Signal BOUND (supersedes the older newest-empty-only reset): before
    migration 0114 a PRODUCTIVE poll wrote NO outcome row, so this table alone
    could not see successes at all. It can now — a 'success' row ends the run
    directly — but the bound is RETAINED because every pre-0114 row on disk
    still lacks it. The streak counts ONLY the leading 'empty' rows that
    occurred SINCE ``last_signal`` — an empty poll at
    or before the newest produced signal is stale evidence and stops the count.
    Without the bound, a source that produces daily but polls more often than
    it publishes accumulates an unbounded interleaved 'empty' run and is
    false-flagged (the foreignaffairs 2026-07-21 case: 2 signals ingested that
    morning, "empty_streak=20" alarm the same day). When ``last_signal`` is
    absent (older callers / a source that has never produced), the contiguous
    'empty' run alone decides the streak, which is exactly the
    xinhua/aljazeera dead case.
    """
    if threshold <= 0:
        return []
    degraded: list[tuple[str, int, Any, str]] = []
    by_source: dict[str, list[dict[str, Any]]] = {}
    order: list[str] = []
    for r in rows:
        sid = r.get("source_id")
        if not sid:
            continue
        if sid not in by_source:
            by_source[sid] = []
            order.append(sid)
        by_source[sid].append(r)
    for sid in order:
        outcomes = by_source[sid]
        # All rows carry the same per-source last_signal; read it off the first.
        last_signal = outcomes[0].get("last_signal") if outcomes else None
        # Rows already newest-first; count the leading 'empty' run, bounded to
        # the rows SINCE the newest produced signal (see docstring — an empty
        # poll at/before the signal is stale evidence, not part of the run).
        streak = 0
        newest_empty_at: Any = None
        newest_observed: datetime | None = None
        for r in outcomes:
            if (r.get("outcome") or "") != "empty":
                break
            occurred = r.get("occurred_at")
            if (
                last_signal is not None
                and occurred is not None
                and occurred <= last_signal
            ):
                break
            if newest_empty_at is None:
                newest_empty_at = occurred
            # B0-12: aggregate the run's upstream-observation evidence.
            observed = r.get("newest_entry_ts")
            if isinstance(observed, datetime):
                observed = _as_aware_utc(observed)
                if newest_observed is None or observed > newest_observed:
                    newest_observed = observed
            streak += 1
        if streak >= threshold:
            fault_class = _classify_streak(newest_observed, last_signal, newest_empty_at)
            if fault_class == "honest_quiet":
                if honest_quiet_threshold <= 0 or streak < honest_quiet_threshold:
                    continue  # still just quiet — stay silent (B0-12)
                # Roll 86: and long in TIME, past a weekend (the weekday-only feed)
                if not quiet_long_enough_to_page(
                    last_signal or newest_observed, newest_empty_at,
                    outcomes[0].get("quiet_page_after_hours"),
                ):
                    continue
                # 2026-08-27 sweep: quiet for THIS long, even honestly, is no
                # longer distinguishable from dead — escalate, but tagged
                # separately from cursor_fault/unknown so the alert body can
                # say plainly that this is not a cursor/filter fault.
                fault_class = "honest_quiet_prolonged"
            degraded.append((sid, streak, newest_empty_at, fault_class))
    return degraded
