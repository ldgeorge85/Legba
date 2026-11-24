# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The consult turn's COVERAGE BY DAY (lane Q-1, rule A) — computed, never paraphrased.

Every consult turn under the contract runs this deterministic read over the
window in force BEFORE the model drills, hands the result to the model as data,
and attaches it to the answer as data; the answer's "coverage by day" block is
rendered HERE from the same result, so the model cannot restate the record's
density in its own words.

WHAT IT COUNTS, per UTC day of the window (all reads are bounded index scans —
live EXPLAIN 2026-11-13 over a 7-day window: signals 0.55 s on
``signals_fetched_at``-family indexes, findings 0.11 s on
``idx_analyst_outputs_kind``, events 6 ms on ``idx_events_time``, poll errors
83 ms on ``source_poll_outcomes_source_time_idx``):

* ``signals`` — rows by INGEST day (``fetched_at``) and the distinct sources
  contributing that day;
* ``findings`` — ``analyst_outputs`` rows of kind ``finding`` by
  ``produced_at`` (narrowed to the pinned target ids when the turn has any);
* ``events`` — ``events`` rows by ``time_start`` (same target narrowing).

And the window's KNOWN COLLECTION GAPS: head source descriptors in state
``paused``, and sources whose polls returned ``error`` inside the window (with
counts). Sparse days are named: a day whose signal count is below a quarter of
the window's median day.

The subject filter is the turn's pinned TARGET ids only. Free-text subjects are
not filtered (a keyword guess would make the counts a second opinion about the
question); the block says "whole record" when no target is pinned.

Never raises: a failed read is recorded as ``unavailable`` with its reason.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from statistics import median
from typing import Any, Mapping, Sequence

from .consult_window import TurnWindow

logger = logging.getLogger(__name__)

__all__ = [
    "COVERAGE_SCHEMA",
    "MAX_COVERAGE_DAYS",
    "cited_row_dates",
    "coverage_by_day",
    "render_coverage_block",
    "render_coverage_for_prompt",
    "rows_by_day",
]

COVERAGE_SCHEMA = "consult_coverage/1"
#: A window longer than this is counted over its LAST this-many days (and says so).
MAX_COVERAGE_DAYS = 92
#: Below this share of the window's median signal day, a day is named sparse.
SPARSE_SHARE = 0.25
_GAP_NAMES = 12

_SIGNALS_SQL = (
    "SELECT (fetched_at AT TIME ZONE 'UTC')::date AS d, count(*) AS n, "
    "count(DISTINCT source_id) AS s FROM signals "
    "WHERE fetched_at >= $1 AND fetched_at < $2 GROUP BY 1"
)
_FINDINGS_SQL = (
    "SELECT (produced_at AT TIME ZONE 'UTC')::date AS d, count(*) AS n FROM analyst_outputs "
    "WHERE kind = 'finding' AND produced_at >= $1 AND produced_at < $2 "
    "AND ($3::text[] IS NULL OR target_id = ANY($3::text[])) GROUP BY 1"
)
_EVENTS_SQL = (
    "SELECT (time_start AT TIME ZONE 'UTC')::date AS d, count(*) AS n FROM events "
    "WHERE time_start >= $1 AND time_start < $2 "
    "AND ($3::text[] IS NULL OR target_id = ANY($3::text[])) GROUP BY 1"
)
_PAUSED_SQL = (
    "SELECT descriptor_id, name FROM source_descriptors "
    "WHERE is_head AND state = 'paused' ORDER BY descriptor_id"
)
_ERRORS_SQL = (
    "SELECT source_id, count(*) AS n, max(occurred_at) AS last FROM source_poll_outcomes "
    "WHERE outcome = 'error' AND occurred_at >= $1 AND occurred_at < $2 "
    "GROUP BY 1 ORDER BY 2 DESC, 1"
)


async def _fetch(conn: Any, sql: str, *args: Any) -> list[Mapping[str, Any]]:
    return list(await conn.fetch(sql, *args))


def _day_key(value: Any) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)[:10]


async def coverage_by_day(
    pool: Any,
    window: TurnWindow,
    *,
    target_ids: Sequence[str] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """The ``consult_coverage/1`` result for ``window`` (never raises)."""
    now = now or datetime.now(timezone.utc)
    end = min(window.end, now)
    start = window.start
    clipped = False
    if (end - start).days > MAX_COVERAGE_DAYS:
        start, clipped = end - timedelta(days=MAX_COVERAGE_DAYS), True
    days = [d for d in window.days(now=now)
            if datetime(d.year, d.month, d.day, tzinfo=timezone.utc) >= start.replace(
                hour=0, minute=0, second=0, microsecond=0)]
    targets = [str(t) for t in (target_ids or []) if t] or None
    base: dict[str, Any] = {
        "schema": COVERAGE_SCHEMA,
        "window": {"start": start.isoformat(), "end": end.isoformat()},
        "clipped_to_days": MAX_COVERAGE_DAYS if clipped else None,
        "subject_filter": {"target_ids": targets} if targets else None,
        "counted_by": {"signals": "fetched_at (ingest day)",
                       "findings": "produced_at", "events": "time_start"},
    }
    if pool is None:
        return {**base, "state": "unavailable", "reason": "no_read_pool"}
    try:
        async with pool.acquire() as conn:
            sig = await _fetch(conn, _SIGNALS_SQL, start, end)
            fnd = await _fetch(conn, _FINDINGS_SQL, start, end, targets)
            evt = await _fetch(conn, _EVENTS_SQL, start, end, targets)
            paused = await _fetch(conn, _PAUSED_SQL)
            errors = await _fetch(conn, _ERRORS_SQL, start, end)
    except Exception as exc:  # noqa: BLE001 — coverage is reported, never fatal
        logger.warning("consult.coverage.failed err=%s", exc)
        return {**base, "state": "unavailable", "reason": f"read_failed: {type(exc).__name__}"}
    by_sig = {_day_key(r["d"]): (int(r["n"]), int(r["s"])) for r in sig}
    by_fnd = {_day_key(r["d"]): int(r["n"]) for r in fnd}
    by_evt = {_day_key(r["d"]): int(r["n"]) for r in evt}
    rows = []
    for d in days:
        key = d.isoformat()
        n_sig, n_src = by_sig.get(key, (0, 0))
        rows.append({
            "day": key, "signals": n_sig, "sources": n_src,
            "findings": by_fnd.get(key, 0), "events": by_evt.get(key, 0),
        })
    sig_counts = [r["signals"] for r in rows]
    med = median(sig_counts) if sig_counts else 0
    zero = [r["day"] for r in rows if not (r["signals"] or r["findings"] or r["events"])]
    sparse = [
        r["day"] for r in rows
        if r["day"] not in zero and med > 0 and r["signals"] < SPARSE_SHARE * med
    ]
    return {
        **base,
        "state": "measured",
        "days": rows,
        "zero_days": zero,
        "sparse_days": sparse,
        "sparse_rule": f"signals below {SPARSE_SHARE:.0%} of the window's median day ({med:g})",
        "gaps": {
            "paused_sources": [
                {"id": str(r["descriptor_id"]), "name": str(r["name"] or "")} for r in paused
            ],
            "error_sources": [
                {"id": str(r["source_id"]), "errors": int(r["n"]),
                 "last_error_at": r["last"].isoformat() if r["last"] else None}
                for r in errors
            ],
        },
    }


def rows_by_day(coverage: Mapping[str, Any] | None) -> dict[str, int] | None:
    """ISO day → all rows held that day, or ``None`` when coverage was not measured."""
    if not coverage or coverage.get("state") != "measured":
        return None
    return {
        r["day"]: int(r["signals"]) + int(r["findings"]) + int(r["events"])
        for r in coverage.get("days") or []
    }


def _names(items: Sequence[Mapping[str, Any]], key: str = "id") -> str:
    shown = [str(i.get(key)) for i in items[:_GAP_NAMES]]
    more = len(items) - len(shown)
    return ", ".join(shown) + (f" and {more} more" if more > 0 else "")


def render_coverage_block(coverage: Mapping[str, Any] | None) -> str:
    """The answer's code-built ``Coverage by day`` block."""
    head = "## Coverage by day (computed from the record, not written by the model)"
    if not coverage or coverage.get("state") != "measured":
        why = (coverage or {}).get("reason") or "not measured"
        return f"{head}\n\nCoverage could not be measured for this turn ({why})."
    subject = coverage.get("subject_filter")
    scope = (
        f"pinned targets {', '.join(subject['target_ids'])} (findings and events); signals: whole record"
        if subject else "whole record (no subject filter)"
    )
    lines = [
        head, "",
        f"Scope: {scope}. Signals are counted by ingest day.",
        "",
        "| Day (UTC) | Signals | Sources | Findings | Events |",
        "|---|---:|---:|---:|---:|",
    ]
    for r in coverage.get("days") or []:
        lines.append(
            f"| {r['day']} | {r['signals']:,} | {r['sources']:,} | {r['findings']:,} | {r['events']:,} |"
        )
    zero = coverage.get("zero_days") or []
    sparse = coverage.get("sparse_days") or []
    lines.append("")
    lines.append(
        "Days with no rows: " + (", ".join(zero) if zero else "none") + ". "
        "Sparse days (" + str(coverage.get("sparse_rule")) + "): "
        + (", ".join(sparse) if sparse else "none") + "."
    )
    gaps = coverage.get("gaps") or {}
    paused = gaps.get("paused_sources") or []
    errs = gaps.get("error_sources") or []
    err_named = [{"id": "%s ×%s" % (e["id"], e["errors"])} for e in errs]
    lines.append(
        f"Known collection gaps: {len(paused)} source(s) paused"
        + (f" ({_names(paused)})" if paused else "")
        + f"; {len(errs)} source(s) returned poll errors in the window"
        + (f" ({_names(err_named)})" if errs else "")
        + "."
    )
    if coverage.get("clipped_to_days"):
        lines.append(f"(Counted over the last {coverage['clipped_to_days']} days of the window.)")
    return "\n".join(lines)


def render_coverage_for_prompt(coverage: Mapping[str, Any] | None) -> str:
    """A compact copy of the same result for the model's first turn."""
    if not coverage or coverage.get("state") != "measured":
        return "coverage: not measured (" + str((coverage or {}).get("reason")) + ")"
    days = "; ".join(
        f"{r['day']}: signals {r['signals']} from {r['sources']} sources, "
        f"findings {r['findings']}, events {r['events']}"
        for r in coverage.get("days") or []
    )
    gaps = coverage.get("gaps") or {}
    return (
        f"coverage by day — {days}. zero days: {', '.join(coverage.get('zero_days') or []) or 'none'}; "
        f"sparse days: {', '.join(coverage.get('sparse_days') or []) or 'none'}; "
        f"paused sources: {len(gaps.get('paused_sources') or [])}; "
        f"sources with poll errors in the window: {len(gaps.get('error_sources') or [])}."
    )


# ---------------------------------------------------------------------------
# The cited rows' own dates (for the narrowing check)
# ---------------------------------------------------------------------------

_DATE_SQL: tuple[tuple[str, str], ...] = (
    ("signals", "SELECT id::text AS id, fetched_at AS at, payload->>'published_at' AS pub "
                "FROM signals WHERE id = ANY($1::uuid[])"),
    ("analyst_outputs", "SELECT id::text AS id, produced_at AS at, NULL AS pub "
                        "FROM analyst_outputs WHERE id = ANY($1::uuid[])"),
    ("events", "SELECT id::text AS id, coalesce(time_start, created_at) AS at, NULL AS pub "
               "FROM events WHERE id = ANY($1::uuid[])"),
    ("situations", "SELECT id::text AS id, created_at AS at, NULL AS pub "
                   "FROM situations WHERE id = ANY($1::uuid[])"),
    ("hypotheses", "SELECT id::text AS id, created_at AS at, NULL AS pub "
                   "FROM hypotheses WHERE id = ANY($1::uuid[])"),
)


def _published(raw: Any) -> datetime | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        at = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return at.replace(tzinfo=timezone.utc) if at.tzinfo is None else at


async def cited_row_dates(pool: Any, refs: Sequence[str]) -> dict[str, datetime] | None:
    """ref → the row's own date (a signal's published time when it carries one,
    else its ingest time). ``None`` when nothing could be read."""
    from uuid import UUID

    ids: list[UUID] = []
    for r in refs:
        try:
            ids.append(UUID(str(r)))
        except (ValueError, AttributeError):
            continue
    if pool is None:
        return None
    if not ids:
        return {}
    out: dict[str, datetime] = {}
    failures = 0
    async with pool.acquire() as conn:
        for _table, sql in _DATE_SQL:
            remaining = [u for u in ids if str(u) not in out]
            if not remaining:
                break
            try:
                rows = await conn.fetch(sql, remaining)
            except Exception as exc:  # noqa: BLE001
                failures += 1
                logger.debug("consult.coverage.dates_failed table=%s err=%s", _table, exc)
                continue
            for r in rows:
                at = _published(r["pub"]) or r["at"]
                if isinstance(at, datetime):
                    out[str(r["id"]).lower()] = (
                        at.replace(tzinfo=timezone.utc) if at.tzinfo is None else at
                    )
    if failures == len(_DATE_SQL):
        return None
    return out
