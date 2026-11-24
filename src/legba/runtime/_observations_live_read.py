# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The LIVE series read — ``live_series`` (lane E2).

WHAT IT READS. Since migration 0246 a source's ObservationSink writes live
numeric series (chokepoint transits, central-bank policy rates) into
``observations`` as ``origin_class='live'`` with ``collection_id =
'live:<source_id>'``, append-only (a revision is a new row). The history reads
in :mod:`legba.runtime._observations_read` deliberately never select those rows
(only collections whose descriptor is ``loaded``; "history must not look like a
live surge", SEAMS #57/#62). That rule stands. This is a SEPARATE reader with
its own name, and every row it returns says ``live`` and names its source.

TWO MODES, one tool.

* CATALOGUE — no ``series_id``, or a ``series_id`` without a ``subject``: one
  line per live series (source, series id, subject, unit, period) with the
  newest period held, the newest record time, the newest load time and the row
  count. Capped at :data:`MAX_CATALOGUE_LINES`.
* WINDOW — ``series_id`` and ``subject``: every row of that series whose valid
  period lies in ``[from, to]``, oldest period first, ALL revisions (a revised
  period is several rows; ``is_latest_revision`` marks the newest one returned
  for its period) with both timestamps — ``record_time`` (the provider's
  revision stamp, or the first fetch that saw the value: ``record_time_basis``)
  and ``loaded_at`` (when the platform wrote it). Bounded: the window may not
  exceed :data:`MAX_WINDOW_DAYS` (a wider one refuses, named), rows are capped
  at :data:`MAX_LIVE_ROWS` (``truncated`` says when the cap bit). With no
  window the read is the last :data:`DEFAULT_WINDOW_DAYS` days.

``known_at`` (E2, evaluation harness only): every leg keeps rows with
``loaded_at < K`` and ``record_time <= K`` and the default window ends at K
(classified in :mod:`legba.data.raw_reader_known_at`).

Read-only, set-based, no NATS. The window read filters on ``origin_class =
'live'`` so it uses the partial ``observations_live_vintage_idx`` once the
source is known, and the period predicate carries the redundant
``valid_from <= to`` for partition pruning, as the history reads do.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from ._observations_read import observation_ref

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_ROWS",
    "DEFAULT_WINDOW_DAYS",
    "LIVE_COLLECTION_PREFIX",
    "MAX_CATALOGUE_LINES",
    "MAX_LIVE_ROWS",
    "MAX_WINDOW_DAYS",
    "read_live_series",
]

#: Rows a window read returns when the caller names no ``limit``.
DEFAULT_ROWS = 200
#: Hard cap on rows one window read returns (a year of a daily series with a
#: revision or so per period); ``truncated`` is set when it bites.
MAX_LIVE_ROWS = 500
#: Hard cap on the valid window one read may span, in days.
MAX_WINDOW_DAYS = 366
#: The window when the caller names none: the last 90 days.
DEFAULT_WINDOW_DAYS = 90
#: Hard cap on catalogue lines.
MAX_CATALOGUE_LINES = 400
#: The ``collection_id`` spelling a live row carries (migration 0246).
LIVE_COLLECTION_PREFIX = "live:"

_LIVE_NOTE = (
    "LIVE series rows, written by a source's poller (origin_class='live'), "
    "not a curated history holding. Each row is about its own valid period; "
    "record_time is when the provider published (or the platform first saw) "
    "that value, loaded_at when the platform wrote it. A revised period has "
    "several rows; cite the row you use with its source_id and both times."
)


def _cut_clause(alias: str, k_param: int) -> str:
    # Imported here: the cut is evaluation-only and this leaf stays slim.
    from .substrate_known_at import recorded_before_sql

    return (f"AND {recorded_before_sql(f'{alias}.loaded_at', k_param)} "
            f"AND {alias}.record_time <= ${k_param} ")


def _catalogue_sql(*, by_source: bool, by_series: bool, k_param: int | None) -> tuple[str, int]:
    """The catalogue statement and the index of its LIMIT parameter."""
    n = 0
    where = "WHERE o.origin_class = 'live' "
    if by_source:
        n += 1
        where += f"AND o.source_id = ${n} "
    if by_series:
        n += 1
        where += f"AND o.series_id = ${n} "
    if k_param is not None:
        n += 1
        where += _cut_clause("o", n)
    n += 1
    sql = (
        "SELECT o.source_id, o.series_id, o.subject, o.subject_kind, o.unit, "
        "       o.period, max(o.valid_to) AS newest_period_end, "
        "       max(o.record_time) AS newest_record_time, "
        "       max(o.loaded_at) AS newest_loaded_at, count(*) AS rows_held "
        "FROM observations o "
        f"{where}"
        "GROUP BY o.source_id, o.series_id, o.subject, o.subject_kind, o.unit, o.period "
        "ORDER BY o.series_id, o.subject, o.source_id "
        f"LIMIT ${n}"
    )
    return sql, n


def _window_sql(*, by_source: bool, cut: bool) -> str:
    """$1 series, $2 subject, $3 from, $4 to, $5 limit, [$6 source], [$6/$7 K]."""
    extra = ""
    n = 5
    if by_source:
        n += 1
        extra += f"AND o.source_id = ${n} "
    if cut:
        n += 1
        extra += _cut_clause("o", n)
    return (
        "SELECT o.id, o.collection_id, o.source_id, o.series_id, o.subject, "
        "       o.subject_kind, o.period, o.valid_from, o.valid_to, "
        "       o.record_time, o.record_time_basis, o.loaded_at, o.value, "
        "       o.value_text, o.unit, o.source_url, o.fetch_sha256, "
        "       o.methodology_version, o.origin_class "
        "FROM observations o "
        "WHERE o.origin_class = 'live' "
        "AND o.series_id = $1 AND o.subject = $2 "
        "AND o.valid_from >= $3 AND o.valid_from <= $4 AND o.valid_to <= $4 "
        f"{extra}"
        "ORDER BY o.valid_from, o.valid_to, o.record_time, o.source_id "
        "LIMIT $5"
    )


def _refusal(why: str) -> dict[str, Any]:
    logger.info("observations_live_read.refused why=%s", why)
    return {"rows": [], "refs": [], "count": 0, "error": why, "origin_class": "live"}


def _iso(v: Any) -> str | None:
    return v.isoformat() if isinstance(v, (date, datetime)) else None


def _as_date(value: Any, label: str) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        raise ValueError(f"{label}={text!r} is not a date (YYYY-MM-DD)") from None


def _clamp_rows(limit: Any) -> int:
    try:
        n = int(limit)
    except (TypeError, ValueError):
        return DEFAULT_ROWS
    return max(1, min(n, MAX_LIVE_ROWS))


def _window(since: Any, until: Any, known_at: datetime | None) -> tuple[date, date]:
    """The valid window, defaulted and checked (ValueError names the problem)."""
    end_default = (known_at or datetime.now(timezone.utc)).date()
    window_to = _as_date(until, "to") or end_default
    window_from = _as_date(since, "from") or (window_to - timedelta(days=DEFAULT_WINDOW_DAYS))
    if window_from > window_to:
        raise ValueError(f"from {window_from.isoformat()} is after to {window_to.isoformat()}")
    if (window_to - window_from).days > MAX_WINDOW_DAYS:
        raise ValueError(
            f"the window {window_from.isoformat()}..{window_to.isoformat()} spans "
            f"{(window_to - window_from).days} days; a live_series read spans at most "
            f"{MAX_WINDOW_DAYS} — narrow from/to")
    return window_from, window_to


def _row(r: Any) -> dict[str, Any]:
    value = r["value"]
    return {
        "observation_id": str(r["id"]),
        "ref": observation_ref(r["id"]),
        "origin_class": "live",
        "label": "LIVE",
        "source_id": r["source_id"],
        "collection_id": r["collection_id"],
        "series_id": r["series_id"],
        "subject": r["subject"],
        "subject_kind": r["subject_kind"],
        "period": r["period"],
        "valid_from": _iso(r["valid_from"]),
        "valid_to": _iso(r["valid_to"]),
        "record_time": _iso(r["record_time"]),
        "record_time_basis": r["record_time_basis"],
        "loaded_at": _iso(r["loaded_at"]),
        "value": float(value) if isinstance(value, (Decimal, int, float)) else None,
        "value_display": format(value, "f") if isinstance(value, Decimal) else (
            str(value) if value is not None else None),
        "value_text": r["value_text"],
        "unit": r["unit"],
        "source_url": r["source_url"],
        "fetch_sha256": r["fetch_sha256"],
        "methodology_version": r["methodology_version"],
    }


async def read_live_series(  # type: ignore[no-untyped-def]
    pool,
    *,
    series_id: Any = None,
    subject: Any = None,
    source_id: Any = None,
    since: Any = None,
    until: Any = None,
    limit: Any = DEFAULT_ROWS,
    known_at: datetime | None = None,
) -> dict[str, Any]:
    """``live_series`` — the catalogue, or one series over a bounded window."""
    sid = str(series_id or "").strip() or None
    subj = str(subject or "").strip() or None
    src = str(source_id or "").strip() or None
    kp: tuple[Any, ...] = (known_at,) if known_at is not None else ()
    base: dict[str, Any] = {"origin_class": "live", "live_note": _LIVE_NOTE}
    if known_at is not None:
        from .substrate_known_at import cut_stamp

        base["known_at_cut"] = cut_stamp(
            known_at, dropped=0, how="observations.loaded_at < K and record_time <= K")

    if sid is None or subj is None:
        sql, lim_param = _catalogue_sql(by_source=src is not None, by_series=sid is not None,
                                        k_param=0 if kp else None)
        args = [a for a in (src, sid) if a is not None] + list(kp) + [MAX_CATALOGUE_LINES]
        async with pool.acquire() as conn:
            recs = await conn.fetch(sql, *args)
        series = [{
            "source_id": r["source_id"],
            "series_id": r["series_id"],
            "subject": r["subject"],
            "subject_kind": r["subject_kind"],
            "unit": r["unit"],
            "period": r["period"],
            "newest_period_end": _iso(r["newest_period_end"]),
            "newest_record_time": _iso(r["newest_record_time"]),
            "newest_loaded_at": _iso(r["newest_loaded_at"]),
            "rows_held": int(r["rows_held"]),
        } for r in recs]
        return {
            **base,
            "mode": "catalogue",
            "series": series,
            "series_count": len(series),
            "truncated": len(series) >= MAX_CATALOGUE_LINES,
            "rows": [], "refs": [], "count": 0,
            "hint": ("read one with live_series(series_id=..., subject=..., from=..., to=...)"
                     if series else "no live series is held"
                     + (" before known_at" if kp else "")),
        }

    try:
        window_from, window_to = _window(since, until, known_at)
    except ValueError as exc:
        return {**base, **_refusal(str(exc))}
    cap = _clamp_rows(limit)
    sql = _window_sql(by_source=src is not None, cut=bool(kp))
    args: list[Any] = [sid, subj, window_from, window_to, cap + 1]
    if src is not None:
        args.append(src)
    args.extend(kp)
    async with pool.acquire() as conn:
        recs = await conn.fetch(sql, *args)
    truncated = len(recs) > cap
    rows = [_row(r) for r in recs[:cap]]
    newest: dict[tuple[Any, Any], str] = {}
    for row in rows:
        key = (row["source_id"], row["valid_from"], row["valid_to"])
        if row["record_time"] and row["record_time"] >= newest.get(key, ""):
            newest[key] = row["record_time"]
    revisions: dict[tuple[Any, Any], int] = {}
    for row in rows:
        key = (row["source_id"], row["valid_from"], row["valid_to"])
        revisions[key] = revisions.get(key, 0) + 1
        row["revision"] = revisions[key]
        row["is_latest_revision"] = row["record_time"] == newest.get(key)
    return {
        **base,
        "mode": "window",
        "series_id": sid,
        "subject": subj,
        "source_id": src,
        "valid_from": window_from.isoformat(),
        "valid_to": window_to.isoformat(),
        "rows": rows,
        "refs": [r["observation_id"] for r in rows],
        "count": len(rows),
        "truncated": truncated,
        "periods": len(newest),
        "revised_periods": sum(1 for n in revisions.values() if n > 1),
    }
