# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The desk slice's WATERMARK WINDOW and its declared coverage gap (lane N-B).

THE DEFECT. A desk's cadence slice was ``geo && [code] ORDER BY fetched_at
DESC LIMIT 120`` with no salience ranking. The US desk sees ~1,275 signals in
72 h, so 120 rows covered only the newest ~6-7 h of an ~11 h cycle: signals
fetched 11-10 19:13-23:44Z and 11-11 06:21-09:17Z were in NO US desk slice
(the Jim Jordan election, Sanders on Schumer). The cut was silent — nothing
on the finding said that a third of the cycle had never been read.

THE RULE (opt-in per descriptor, ``method.options.slice_since_watermark``):

  * The slice covers everything fetched since the desk's LAST SUCCESSFUL RUN
    of this (analyst, target) — its ``analyst_traces.run_started_at``, which is
    the instant that run's slice was cut — less :data:`OVERLAP_MINUTES` for
    ingest lag (a row fetched just before the cut but committed after it). Rows
    that previous run was actually handed (``input_row_refs``) are excluded
    from the overlap, so nothing is read twice and nothing falls between.
  * A first run (no successful trace) looks back
    ``slice_watermark_first_run_hours`` (default 24). Every window covers at
    least ``slice_watermark_min_hours`` (default 6) so a re-run minutes after
    the last one is not handed a near-empty slice, and no window reaches past
    the descriptor's own ``subscription.targets.time_window``.
  * When the window holds more than the prompt can carry (the reader's row
    cap — the INPUT-token budget's proxy; never a max_tokens), rows are chosen
    by SALIENCE with SOURCE DIVERSITY (:func:`select_by_salience`), not by
    recency, and the finding RECORDS how many in-window signals it did not read
    (``data.slice_coverage``) — a declared coverage gap, not a silent cut.

Absent / false knob ⇒ the reader's statement and rows are exactly what they
were. A leaf: stdlib only (the option catalog is imported lazily).
"""

from __future__ import annotations

import heapq
import json
import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping

logger = logging.getLogger(__name__)

#: The X-1 kind-catalog knobs (``ANALYST_KIND_OPTIONS["inline_target"]``).
SINCE_WATERMARK_OPTION = "slice_since_watermark"
MIN_HOURS_OPTION = "slice_watermark_min_hours"
FIRST_RUN_HOURS_OPTION = "slice_watermark_first_run_hours"
DEFAULT_MIN_HOURS = 6
DEFAULT_FIRST_RUN_HOURS = 24
#: Ingest-lag overlap behind the watermark (fetched_at is stamped at fetch;
#: the row commits after enrichment).
OVERLAP_MINUTES = 30
#: Hard bound on rows one watermark window may fetch (a desk down for days
#: is clamped to its descriptor window anyway; this bounds the scan).
FETCH_HARD_CAP = 3000
#: A row whose geo came from the publisher-origin FALLBACK (no in-body place
#: resolved; ``payload.geo_fallback``) weighs this much of its salience.
FALLBACK_GEO_WEIGHT = 0.5
#: Each further row from the same source is worth this factor less.
SOURCE_DECAY = 0.7
#: Ids of the highest-salience unselected rows recorded on the coverage stamp.
UNREAD_TOP_N = 5

#: The finding / options key the coverage rides on.
COVERAGE_KEY = "slice_coverage"

_WATERMARK_SQL = (
    "SELECT run_id, run_started_at, input_row_refs FROM analyst_traces "
    "WHERE analyst_id = $1 AND target_id = $2 AND status = 'success' "
    "ORDER BY run_started_at DESC LIMIT 1"
)


@dataclass(frozen=True)
class WatermarkConfig:
    min_hours: int = DEFAULT_MIN_HOURS
    first_run_hours: int = DEFAULT_FIRST_RUN_HOURS


@dataclass
class SliceWindow:
    """One run's resolved window."""

    start: datetime
    end: datetime
    basis: str
    watermark_run_id: str | None = None
    watermark_at: datetime | None = None
    seen_ids: list[str] = field(default_factory=list)
    clamped_to_descriptor_window: bool = False

    def hours(self) -> int:
        """The window length in whole hours, rounded UP (the slice header)."""
        return max(1, math.ceil((self.end - self.start).total_seconds() / 3600.0))


class SliceRows(list):
    """A slice reader's rows plus the coverage stamp the actor threads into
    the run options. A plain ``list`` to every consumer."""

    slice_coverage: dict[str, Any] | None = None


def watermark_config(descriptor: Any) -> WatermarkConfig | None:
    """The descriptor's resolved watermark knobs, or ``None`` when the knob is
    absent / false / invalid (validated through the X-1 kind catalog, so a bad
    value degrades LOUDLY to today's recency slice)."""
    identity = getattr(descriptor, "identity", None)
    kind_raw = getattr(identity, "kind", None)
    kind = str(getattr(kind_raw, "value", kind_raw) or "")
    raw = getattr(getattr(descriptor, "method", None), "options", None)
    if not isinstance(raw, Mapping) or not raw.get(SINCE_WATERMARK_OPTION):
        return None
    from .option_catalog.handler_options import resolve_kind_options

    opts = resolve_kind_options(
        kind, dict(raw), log_context=f"{getattr(identity, 'id', '?')}@slice_watermark",
    ).accepted
    if opts.get(SINCE_WATERMARK_OPTION) is not True:
        return None
    return WatermarkConfig(
        min_hours=int(opts.get(MIN_HOURS_OPTION, DEFAULT_MIN_HOURS)),
        first_run_hours=int(opts.get(FIRST_RUN_HOURS_OPTION, DEFAULT_FIRST_RUN_HOURS)),
    )


def plan_window(
    *,
    now: datetime,
    window_hours: int,
    cfg: WatermarkConfig,
    last_run_id: str | None,
    last_run_at: datetime | None,
    last_input_ids: Iterable[Any] | None,
) -> SliceWindow:
    """Pure: the window from the last successful run (or the first-run floor)."""
    if last_run_at is not None:
        start = last_run_at - timedelta(minutes=OVERLAP_MINUTES)
        basis = "last_success_run"
        seen = [str(i) for i in (last_input_ids or ()) if i]
    else:
        start = now - timedelta(hours=cfg.first_run_hours)
        basis = "first_run"
        seen = []
    min_start = now - timedelta(hours=cfg.min_hours)
    if start > min_start:
        # Widened BACK past the watermark: the rows it re-admits are wanted,
        # so the read-once exclusion does not apply.
        start, basis, seen = min_start, basis + "+min_hours", []
    clamped = False
    floor = now - timedelta(hours=window_hours)
    if start < floor:
        start, clamped = floor, True
    return SliceWindow(
        start=start, end=now, basis=basis,
        watermark_run_id=str(last_run_id) if last_run_id else None,
        watermark_at=last_run_at, seen_ids=seen,
        clamped_to_descriptor_window=clamped,
    )


async def resolve_window(
    conn: Any,
    *,
    analyst_id: str | None,
    target_id: str | None,
    window_hours: int,
    cfg: WatermarkConfig,
    now: datetime | None = None,
) -> SliceWindow:
    """Read the desk's last successful run and plan its window. A failed read
    degrades to the first-run floor (logged) — a desk that cannot find its
    watermark still reads a bounded, declared window."""
    now = now or datetime.now(tz=timezone.utc)
    row = None
    if analyst_id and target_id:
        try:
            row = await conn.fetchrow(_WATERMARK_SQL, analyst_id, target_id)
        except Exception as exc:                            # pragma: no cover
            logger.warning(
                "slice_watermark.read_failed analyst=%s target=%s err=%s",
                analyst_id, target_id, exc,
            )
    return plan_window(
        now=now, window_hours=window_hours, cfg=cfg,
        last_run_id=row["run_id"] if row else None,
        last_run_at=row["run_started_at"] if row else None,
        last_input_ids=row["input_row_refs"] if row else None,
    )


def window_clauses(window: SliceWindow, params: list[Any]) -> list[str]:
    """The window's SQL clauses (params appended): the lower bound, and the
    read-once exclusion of the previous run's rows inside the overlap."""
    params.append(window.start)
    out = [f"fetched_at > ${len(params)}::timestamptz"]
    if window.seen_ids:
        params.append(list(window.seen_ids))
        out.append(f"NOT (id = ANY(${len(params)}::uuid[]))")
    return out


# ---------------------------------------------------------------------------
# Selection — salience with source diversity
# ---------------------------------------------------------------------------


def _get(row: Any, key: str) -> Any:
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return None


def _as_dict(raw: Any) -> Mapping[str, Any] | None:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            return None
    return raw if isinstance(raw, Mapping) else None


def is_fallback_geo(row: Any) -> bool:
    """Did this row's geo come from the publisher-origin fallback?"""
    payload = _as_dict(_get(row, "payload")) or _as_dict(_get(row, "data")) or {}
    return bool(payload.get("geo_fallback"))


def row_score(row: Any) -> float:
    """Salience magnitude (0 when unscored, so an unscored row never outranks a
    scored one), discounted for a fallback geo tag."""
    sal = _as_dict(_get(row, "salience"))
    try:
        mag = float(sal.get("magnitude")) if sal is not None else 0.0
    except (TypeError, ValueError):
        mag = 0.0
    mag = max(0.0, mag)
    return mag * FALLBACK_GEO_WEIGHT if is_fallback_geo(row) else mag


def _ts(row: Any) -> float:
    v = _get(row, "fetched_at")
    try:
        return v.timestamp() if v is not None else 0.0
    except AttributeError:
        return 0.0


def select_by_salience(
    rows: list[Any], *, limit: int, per_source_cap: int | None = None,
) -> list[Any]:
    """At most ``limit`` rows, chosen greedily by salience with a per-source
    decay (each further row from one ``source_id`` is worth
    :data:`SOURCE_DECAY` less), ties to the newer row. ``per_source_cap`` (the
    declared ``slice_per_source_cap``) is strict. Returned newest-first."""
    if len(rows) <= limit:
        return list(rows)
    buckets: dict[Any, list[Any]] = {}
    for r in rows:
        buckets.setdefault(_get(r, "source_id"), []).append(r)
    for b in buckets.values():
        b.sort(key=lambda r: (row_score(r), _ts(r)), reverse=True)
    taken: dict[Any, int] = {s: 0 for s in buckets}
    heap: list[tuple[float, float, int, Any]] = []
    order = {s: i for i, s in enumerate(buckets)}
    for s, b in buckets.items():
        heapq.heappush(heap, (-row_score(b[0]), -_ts(b[0]), order[s], s))
    out: list[Any] = []
    while heap and len(out) < limit:
        _neg, _t, _o, s = heapq.heappop(heap)
        out.append(buckets[s][taken[s]])
        taken[s] += 1
        if taken[s] >= len(buckets[s]):
            continue
        if per_source_cap is not None and taken[s] >= per_source_cap:
            continue
        nxt = buckets[s][taken[s]]
        heapq.heappush(
            heap, (-row_score(nxt) * SOURCE_DECAY ** taken[s], -_ts(nxt), order[s], s),
        )
    out.sort(key=_ts, reverse=True)
    return out


def coverage_stamp(
    window: SliceWindow,
    *,
    in_window: int,
    selected: list[Any],
    candidates: list[Any],
    fetch_capped: bool,
) -> dict[str, Any]:
    """The slice-level coverage record (finalized with read counts by the
    unit's ORIENT, :func:`finalize_coverage`)."""
    chosen = {str(_get(r, "id")) for r in selected}
    unread = sorted(
        (r for r in candidates if str(_get(r, "id")) not in chosen),
        key=lambda r: (row_score(r), _ts(r)), reverse=True,
    )
    return {
        "mode": "since_watermark",
        "window_start": window.start.isoformat(),
        "window_end": window.end.isoformat(),
        "window_hours": window.hours(),
        "basis": window.basis,
        "watermark_run_id": window.watermark_run_id,
        "watermark_at": window.watermark_at.isoformat() if window.watermark_at else None,
        "clamped_to_descriptor_window": window.clamped_to_descriptor_window,
        "previously_read_excluded": len(window.seen_ids),
        "in_window": int(in_window),
        "selected": len(selected),
        "not_selected": max(0, int(in_window) - len(selected)),
        "selection": "salience_diversity" if len(candidates) > len(selected) else "all",
        "fetch_capped": bool(fetch_capped),
        "unselected_top": [
            {"id": str(_get(r, "id")), "magnitude": round(row_score(r), 3)}
            for r in unread[:UNREAD_TOP_N]
        ],
    }


def coverage_options(inputs: Any) -> dict[str, Any]:
    """The run-options keys a watermark slice contributes: the coverage stamp
    and the window the slice was REALLY cut with (the slice header's line).
    ``{}`` for every other slice, so the options dict is unchanged."""
    cov = getattr(inputs, COVERAGE_KEY, None)
    if not isinstance(cov, dict):
        return {}
    return {COVERAGE_KEY: dict(cov), "slice_window_hours": int(cov["window_hours"])}


def finalize_coverage(coverage: Any, *, read: int) -> dict[str, Any] | None:
    """Add what the unit actually READ (its ``derived_from``, after the
    ORIENT budget pack): ``not_read`` = in-window signals this finding never
    saw. ``None`` when the run carried no watermark coverage."""
    if not isinstance(coverage, Mapping):
        return None
    out = dict(coverage)
    out["read"] = int(read)
    out["not_read"] = max(0, int(out.get("in_window") or 0) - int(read))
    return out


__all__ = [
    "COVERAGE_KEY",
    "FIRST_RUN_HOURS_OPTION",
    "MIN_HOURS_OPTION",
    "SINCE_WATERMARK_OPTION",
    "SliceRows",
    "SliceWindow",
    "WatermarkConfig",
    "coverage_options",
    "coverage_stamp",
    "finalize_coverage",
    "is_fallback_geo",
    "plan_window",
    "resolve_window",
    "row_score",
    "select_by_salience",
    "watermark_config",
    "window_clauses",
]
