# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The raw-signal slice's progress contract for ``event_clustering``.

THE DEFECT (2026-11-09, CORPUS_DATA_LAYER review). The ``signal_slice``
watermark sat at 2026-09-23T19:37:14Z from 09-24 07:56Z for seven weeks, and
no raw signal was clustered into an event in that time. Two cuts produced the
same fixed point:

* a cut in ``write`` resumed INCLUSIVE at the earliest member of ANY
  unwritten candidate. Candidates were walked in cluster order, not slice
  order, and a candidate holding the page's first row was always among the
  unwritten ones, so the resume bound was the page head every tick: the same
  300 rows, the same cut and the same bound, 3,000+ times;
* from late October the lifecycle phase grew (5 s to 35 s), and a 300-row
  cluster phase (about 29 s) no longer fitted in the 60 s the tower reserve
  leaves to the front. The pass stopped in ``match`` with nothing written,
  and a slice that never reached the write loop kept its prior watermark.

The contract this module holds the handler to:

* **progress is the processed PREFIX.** Candidates are walked in slice order
  (by their earliest member), and the watermark advances, exclusively, to the
  last row of the page whose candidate has been processed (written, refused,
  declined or skipped). A cut never re-reads rows already processed, and it
  never skips rows owed a write;
* **a failing candidate is skipped, not retried forever.** Its signals count
  as processed, the reason rides the receipt and a bounded dead-letter list on
  the watermark state, and the pass moves on. A row the normalizer cannot read
  is skipped the same way;
* **the page adapts.** A tick that made no progress halves the next page (down
  to ``min_signals_per_run``). A tick that consumed its page grows it by a
  quarter (up to ``max_signals``). So a fixed cost that outgrows the front's
  share of the budget shrinks the slice until it fits again, rather than
  freezing it;
* **a stall is loud.** Every tick that read rows and did not move the
  watermark increments ``stall_ticks`` and logs
  ``event_clustering.watermark_stalled``;
* **a one-time catch-up floor.** On the first tick under this contract, a
  watermark older than ``now - catch_up_floor_hours`` jumps to that floor.
  The skipped span (from, to, signal count) is logged and recorded on the
  watermark state and the receipt as a coverage note. ``0`` disables the
  floor, which leaves the backlog to drain oldest-first at the adaptive page.
  The floor re-arms only when the knob's value changes.

Everything persisted lives on the existing ``signal_slice`` row of
``alert_trigger_watermarks``. ``ts`` and ``id`` keep their meaning, so
``_decode_cursor`` reads the row as before; the new keys ride beside them.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Sequence
from uuid import UUID

from ...signal_slice_scope import canonical_only_sql_for

logger = logging.getLogger(__name__)

#: The adaptive page's floor. Twenty-five rows is about 12 minutes of the
#: measured live arrival rate (about 2,900 a day), so even a floor-sized page
#: keeps pace with a 20-minute cadence.
DEFAULT_MIN_SIGNALS_PER_RUN = 25

#: The one-time catch-up floor, in hours. ``24`` is the slice's own no-cursor
#: bound (``lookback_hours``), the window a fresh install starts from. The
#: handler docstring and the lane report give the numbers behind the choice.
DEFAULT_CATCH_UP_FLOOR_HOURS = 24

#: The page grows by this factor after a fully consumed page.
_GROWTH = 1.25

#: Bounded receipt and dead-letter sizes, so a poison-heavy page cannot bloat
#: a trace or the watermark row.
MAX_DEAD_LETTER = 50
MAX_SIGNAL_IDS_PER_SKIP = 10

_NIL_UUID = UUID(int=0)

#: The skipped span's size, counted once when the floor applies. It uses the
#: slice's own predicate (text modality, canonical only), so the count is the
#: number of rows the slice would have offered.
_SKIPPED_SPAN_COUNT_SQL = f"""
    SELECT count(*)::bigint
      FROM signals s
     WHERE s.modality = 'text'
       AND {canonical_only_sql_for("s")}
       AND (s.fetched_at, s.id) > ($1::timestamptz, $2::uuid)
       AND s.fetched_at < $3::timestamptz
"""


Position = tuple[datetime, UUID]


def _iso(pos: Position | None) -> dict[str, str] | None:
    if pos is None:
        return None
    return {"ts": pos[0].isoformat(), "id": str(pos[1])}


def _coerce_page(value: Any) -> int | None:
    try:
        page = int(value)
    except (TypeError, ValueError):
        return None
    return page if page >= 1 else None


@dataclass
class SliceProgress:
    """One tick's slice bookkeeping: what it read, what it processed, where
    the watermark lands and what the next tick's page is."""

    cursor_before: Position | None
    page: int
    max_signals: int
    min_signals: int
    stall_ticks: int = 0
    dead_letter: list[dict[str, Any]] = field(default_factory=list)
    catch_up: dict[str, Any] | None = None
    catch_up_applied_now: bool = False
    # filled during the tick
    rows_read: int = 0
    processed: int = 0
    skipped: list[dict[str, Any]] = field(default_factory=list)
    unnormalizable: int = 0
    cursor_after: Position | None = None
    next_page: int | None = None
    stalled: bool = False
    # N-Z — the catch-up split, multi-page passes and lag (see
    # ``_event_slice_catchup``). ``backfill`` is the open gap BEHIND the head:
    # ``{"cursor": Position | None, "from": iso, "until": datetime, ...}``.
    backfill: dict[str, Any] | None = None
    backfill_rows: int = 0
    backfill_processed: int = 0
    backfill_closed: dict[str, Any] | None = None
    split_opened: dict[str, Any] | None = None
    pages: int = 0
    head_caught_up: bool = False
    floor_ran: bool = False
    lag_minutes: float | None = None
    head_lag_minutes: float | None = None

    @property
    def cursor(self) -> Position | None:
        """The resume bound this tick's slice fetch should use."""
        return self.cursor_before

    def skip(self, signal_ids: Iterable[UUID], reason: str, *, now: datetime) -> None:
        """Record a candidate the pass skipped rather than let it block."""
        ids = [str(s) for s in signal_ids]
        entry = {
            "at": now.isoformat(),
            "reason": str(reason)[:500],
            "signal_count": len(ids),
            "signal_ids": ids[:MAX_SIGNAL_IDS_PER_SKIP],
        }
        self.skipped.append(entry)
        self.dead_letter = (self.dead_letter + [entry])[-MAX_DEAD_LETTER:]
        logger.warning(
            "event_clustering.candidate_skipped reason=%s signals=%d first=%s",
            entry["reason"], len(ids), ids[:1],
        )

    def state(self) -> dict[str, Any]:
        """The ``signal_slice`` watermark row's full state."""
        pos = self.cursor_after or self.cursor_before
        out: dict[str, Any] = {}
        if pos is not None:
            out.update(_iso(pos) or {})
        out["page"] = int(self.next_page or self.page)
        out["stall_ticks"] = int(self.stall_ticks)
        if self.catch_up is not None:
            out["catch_up"] = dict(self.catch_up)
        if self.dead_letter:
            out["dead_letter"] = list(self.dead_letter)
        if self.backfill is not None:
            out["backfill"] = _backfill_state(self.backfill)
        if self.lag_minutes is not None:
            out["lag_minutes"] = round(self.lag_minutes, 1)
        if self.head_lag_minutes is not None:
            out["head_lag_minutes"] = round(self.head_lag_minutes, 1)
        return out

    def receipt(self) -> dict[str, Any]:
        """The ``slice`` block of the run receipt."""
        return {
            "rows_read": self.rows_read,
            "processed": self.processed,
            "skipped": len(self.skipped) + self.unnormalizable,
            "skipped_candidates": self.skipped[:MAX_DEAD_LETTER],
            "unnormalizable": self.unnormalizable,
            "watermark_before": _iso(self.cursor_before),
            "watermark_after": _iso(self.cursor_after or self.cursor_before),
            "advanced": self.cursor_after is not None
            and self.cursor_after != self.cursor_before,
            "page": self.page,
            "next_page": self.next_page or self.page,
            "max_signals": self.max_signals,
            "stall_ticks": self.stall_ticks,
            "stalled": self.stalled,
            "catch_up": dict(self.catch_up) if self.catch_up_applied_now else None,
            "pages": self.pages,
            "lag_minutes": None if self.lag_minutes is None
            else round(self.lag_minutes, 1),
            "head_lag_minutes": None if self.head_lag_minutes is None
            else round(self.head_lag_minutes, 1),
            "head_caught_up": self.head_caught_up,
            "floor_ran": self.floor_ran,
            "backfill": None if self.backfill is None
            else _backfill_state(self.backfill),
            "backfill_rows_read": self.backfill_rows,
            "backfill_processed": self.backfill_processed,
            "backfill_opened": self.split_opened,
            "backfill_closed": self.backfill_closed,
        }


def begin(
    state: Mapping[str, Any] | None,
    cursor: Position | None,
    *,
    max_signals: int,
    min_signals: int,
) -> SliceProgress:
    """Open one tick's bookkeeping from the stored ``signal_slice`` state."""
    state = state or {}
    min_signals = max(1, min(int(min_signals), int(max_signals)))
    stored = _coerce_page(state.get("page"))
    page = max(min_signals, min(int(max_signals), stored or int(max_signals)))
    dead = state.get("dead_letter")
    catch_up = state.get("catch_up")
    progress = SliceProgress(
        cursor_before=cursor,
        page=page,
        max_signals=int(max_signals),
        min_signals=min_signals,
        stall_ticks=_coerce_page(state.get("stall_ticks")) or 0,
        dead_letter=list(dead)[-MAX_DEAD_LETTER:] if isinstance(dead, list) else [],
        catch_up=dict(catch_up) if isinstance(catch_up, Mapping) else None,
    )
    progress.backfill = _decode_backfill(state.get("backfill"))
    return progress


def _parse_ts(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        ts = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def _decode_backfill(raw: Any) -> dict[str, Any] | None:
    """The stored backfill segment, or ``None`` (absent or unreadable — an
    unreadable one is logged, never silently dropped)."""
    if not isinstance(raw, Mapping):
        return None
    until = _parse_ts(raw.get("until"))
    if until is None:
        logger.warning("event_clustering.backfill_unreadable state=%r", raw)
        return None
    cursor = None
    ts, rid = _parse_ts(raw.get("ts")), raw.get("id")
    if ts is not None and rid:
        try:
            cursor = (ts, UUID(str(rid)))
        except ValueError:
            cursor = None
    out = {k: v for k, v in raw.items() if k not in ("ts", "id", "until")}
    out.update({"cursor": cursor, "until": until})
    return out


def _backfill_state(seg: Mapping[str, Any]) -> dict[str, Any]:
    out = {k: v for k, v in seg.items() if k not in ("cursor", "until")}
    out["until"] = seg["until"].isoformat()
    if seg.get("cursor") is not None:
        out.update(_iso(seg["cursor"]) or {})
    return out


async def apply_catch_up_floor(
    conn: Any,
    progress: SliceProgress,
    *,
    floor_hours: float,
    now: datetime,
) -> None:
    """Apply the one-time catch-up floor, recording the skipped span.

    Runs once per configured floor value: the record it leaves on the state
    (``catch_up.floor_hours``) is what stops a second application. A
    watermark already inside the floor, or no watermark at all (the slice
    then starts from ``lookback_hours``), records the floor as considered
    with ``applied: false``, so a later lag never triggers a silent jump.
    """
    if floor_hours <= 0:
        return
    prior = progress.catch_up or {}
    if prior and float(prior.get("floor_hours") or -1) == float(floor_hours):
        return
    floor_ts = now - timedelta(hours=float(floor_hours))
    record: dict[str, Any] = {
        "floor_hours": float(floor_hours),
        "considered_at": now.isoformat(),
        "applied": False,
    }
    before = progress.cursor_before
    if before is not None and before[0] < floor_ts:
        skipped = await conn.fetchval(
            _SKIPPED_SPAN_COUNT_SQL, before[0], before[1], floor_ts
        )
        record.update({
            "applied": True,
            "from": before[0].isoformat(),
            "to": floor_ts.isoformat(),
            "signals_skipped": int(skipped or 0),
            "note": (
                "one-time catch-up floor: this span of the canonical text "
                "slice was never clustered into events"
            ),
        })
        progress.cursor_before = (floor_ts, _NIL_UUID)
        progress.catch_up_applied_now = True
        logger.warning(
            "event_clustering.catch_up_floor applied floor_hours=%s from=%s"
            " to=%s signals_skipped=%s",
            floor_hours, record["from"], record["to"],
            record["signals_skipped"],
        )
    else:
        logger.info(
            "event_clustering.catch_up_floor considered floor_hours=%s"
            " watermark=%s (inside the floor; nothing skipped)",
            floor_hours, _iso(before),
        )
    progress.catch_up = record


def knobs(options: Mapping[str, Any]) -> tuple[int, float]:
    """``(min_signals_per_run, catch_up_floor_hours)`` from the descriptor
    options; a malformed value reads as the default, never as zero."""
    try:
        min_signals = max(1, int(options.get(
            "min_signals_per_run", DEFAULT_MIN_SIGNALS_PER_RUN)))
    except (TypeError, ValueError):
        min_signals = DEFAULT_MIN_SIGNALS_PER_RUN
    try:
        floor = max(0.0, float(options.get(
            "catch_up_floor_hours", DEFAULT_CATCH_UP_FLOOR_HOURS)))
    except (TypeError, ValueError):
        floor = float(DEFAULT_CATCH_UP_FLOOR_HOURS)
    return min_signals, floor


def owed_signal_ids(
    stopped_in_phase: str | None,
    candidates: Sequence[Any],
    unprocessed: Sequence[Any],
    evidence: Sequence[Any],
) -> set[UUID]:
    """The page rows still owed a write: none after a clean front, every row
    when the cut came before clustering produced candidates, else every
    member of a candidate the write loop did not reach."""
    if stopped_in_phase is None:
        return set()
    if not candidates:
        return {e.id for e in evidence}
    return {sid for c in unprocessed for sid in c.signal_ids}


def candidate_order_key(position: Position | None) -> tuple[int, datetime, str]:
    """Sort key walking candidates in slice order (earliest member first);
    a candidate with no position goes last."""
    if position is None:
        return (1, datetime.max.replace(tzinfo=timezone.utc), "")
    return (0, position[0], str(position[1]))


def processed_prefix(
    page_positions: Sequence[Position], unprocessed_signal_ids: set[UUID]
) -> tuple[Position | None, int]:
    """The last row of the page's processed prefix and the prefix length."""
    last_done: Position | None = None
    processed = 0
    for pos in page_positions:
        if pos[1] in unprocessed_signal_ids:
            break
        last_done = pos
        processed += 1
    return last_done, processed


def finish(
    progress: SliceProgress,
    *,
    page_positions: Sequence[Position],
    unprocessed_signal_ids: set[UUID],
    slice_ran: bool,
) -> None:
    """Close the tick: the prefix watermark, the next page, the stall guard.

    ``page_positions`` is every row the slice SQL returned, in its
    ``(fetched_at, id)`` order (unnormalizable rows included).
    ``unprocessed_signal_ids`` is every member of a candidate the pass did
    not get to. The watermark lands on the last row before the first owed
    one.
    """
    progress.rows_read = len(page_positions)
    if not slice_ran or not page_positions:
        progress.next_page = progress.page
        return
    last_done, processed = processed_prefix(page_positions, unprocessed_signal_ids)
    progress.processed = processed
    if last_done is not None:
        progress.cursor_after = last_done
    whole = processed == len(page_positions)
    if processed == 0:
        progress.stalled = True
        progress.stall_ticks += 1
        progress.next_page = max(progress.min_signals, progress.page // 2)
        logger.warning(
            "event_clustering.watermark_stalled stall_ticks=%d page=%d"
            " next_page=%d watermark=%s",
            progress.stall_ticks, progress.page, progress.next_page,
            _iso(progress.cursor_before),
        )
    else:
        progress.stall_ticks = 0
        progress.next_page = (
            min(progress.max_signals, math.ceil(progress.page * _GROWTH))
            if whole and len(page_positions) >= progress.page
            else progress.page
        )


def log_line(progress: SliceProgress) -> None:
    """The one INFO line per tick the operator greps for."""
    r = progress.receipt()
    logger.info(
        "event_clustering.slice rows_read=%d processed=%d skipped=%d"
        " watermark_before=%s watermark_after=%s page=%d next_page=%d"
        " stall_ticks=%d",
        r["rows_read"], r["processed"], r["skipped"],
        (r["watermark_before"] or {}).get("ts"),
        (r["watermark_after"] or {}).get("ts"),
        r["page"], r["next_page"], r["stall_ticks"],
    )


__all__ = [
    "DEFAULT_CATCH_UP_FLOOR_HOURS",
    "DEFAULT_MIN_SIGNALS_PER_RUN",
    "SliceProgress",
    "apply_catch_up_floor",
    "begin",
    "candidate_order_key",
    "finish",
    "knobs",
    "owed_signal_ids",
    "log_line",
]
