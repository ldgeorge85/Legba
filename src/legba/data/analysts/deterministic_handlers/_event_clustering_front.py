# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``event_clustering``'s FRONT: the signal slice's paging, catch-up and lag.

Split out of ``event_clustering.py`` (N-Z, 2026-11-14), which sat at 1,499 of
the 1,500-line gate. The handler keeps the lifecycle and tower legs, the
receipt and every substrate seam (``fetch_signal_page``, ``_semantic_cosines``,
``_open_events``, ``_match_event``, ``_write_candidate``, ``_cluster_page``);
this module drives one pass's pages through those seams. It reads them off
the handler module ``h`` at call time, so a test that monkeypatches
``event_clustering.<seam>`` still reaches the code below.

THE DEFECT (live, 2026-11-13/14). The slice cursor sat ~29 h and 5,788 rows
behind real time (≈5,000 canonical text signals arrive a day) and, from 23:14
UTC, stopped moving at all: the lifecycle phase that runs first took 59-80 s
(its open-event scan fanned signal links × entity links — fixed in
``_event_lifecycle``), the front's share of the 120 s budget is 60 s, so the
pass was "spent" before it clustered a row; a zero-progress tick halves the
page down to 25 rows, and the next tick spent its budget the same way. Before
the roll the front processed ~40 rows a pass against ~70 arriving, so the
cursor had been losing ground for weeks.

What a pass now does, in order:

* **progress floor.** The first candidate of the first page is processed even
  when the budget is already spent (on a ``min_signals`` page): every pass
  advances the cursor, or records exactly which candidate it skipped and why.
  A page can no longer be read and left unprocessed tick after tick;
* **catch-up split (newest first).** When the cursor is more than
  ``catch_up_lag_minutes`` behind (default 120; ``0`` disables), the head
  cursor jumps to ``now - catch_up_head_minutes`` (default 60) and the gap
  behind it becomes a BACKFILL segment ``[old cursor, head start)`` stored on
  the watermark. The head is processed first every pass (an analyst a day
  behind is useless for "what is happening now"); the backfill drains
  oldest-first, in full pages, with whatever front budget is left. Nothing is
  skipped: the two segments partition the slice exactly (``until`` is an
  exclusive bound on ``fetched_at``, the head cursor ``(until, NIL)`` is
  inclusive of ``fetched_at == until``). One split at a time: while a backfill
  is open, a lagging head drains with multiple pages instead of splitting
  again;
* **several pages per pass.** After a full, fully processed page the pass
  reads the next one while the front budget lasts (``max_pages_per_pass``,
  default 8);
* **lag.** ``lag_minutes`` (oldest unprocessed position: the backfill cursor
  when one is open, else the head) and ``head_lag_minutes`` ride every receipt,
  the watermark state and one ``event_clustering.lag`` log line; the liveness
  watchdog alerts on the stored value (``runtime/analyst_cursor_lag``). A head
  whose last page came back short (it read everything there was) is caught up
  and reports 0.

What can differ from strictly oldest-first order, and why it is acceptable:
during a split, backfill rows are matched against an open-event set that
already holds the events minted from NEWER signals, so an old signal can
reattach to an event first minted by a later report instead of minting its
own. Reattachment is the same four-feature match either way, and the event's
``time_start`` takes the minimum over its members, so the event row ends with
the same span. Without a split (lag under the threshold) the order and the
output are unchanged.
"""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any, Mapping
from uuid import UUID

from . import _event_slice_progress as _sp
from ._event_candidates import EventCandidate, entity_links_for
from ._event_matcher import MAX_CLUSTER_MEMBERS, prepare_open_events
from ._open_event_read import MINTED_EVENT_SQL as _MINTED_EVENT_SQL

logger = logging.getLogger(__name__)

DEFAULT_CATCH_UP_LAG_MINUTES = 120
DEFAULT_CATCH_UP_HEAD_MINUTES = 60
DEFAULT_MAX_PAGES_PER_PASS = 8
_NIL_UUID = UUID(int=0)


def catch_up_knobs(options: Mapping[str, Any]) -> tuple[float, float, int]:
    """``(catch_up_lag_minutes, catch_up_head_minutes, max_pages_per_pass)``;
    a malformed value reads as the default, never as zero."""
    def _num(raw: Any, default: float) -> float:
        try:
            return max(0.0, float(raw))
        except (TypeError, ValueError):
            return float(default)
    # Literal reads (the X-1 catalog sweep greps for ``options.get("<knob>"``).
    return (
        _num(options.get("catch_up_lag_minutes", DEFAULT_CATCH_UP_LAG_MINUTES),
             DEFAULT_CATCH_UP_LAG_MINUTES),
        _num(options.get("catch_up_head_minutes", DEFAULT_CATCH_UP_HEAD_MINUTES),
             DEFAULT_CATCH_UP_HEAD_MINUTES),
        max(1, int(_num(options.get("max_pages_per_pass", DEFAULT_MAX_PAGES_PER_PASS),
                        DEFAULT_MAX_PAGES_PER_PASS))),
    )


def open_split(
    progress: _sp.SliceProgress, *, now: datetime,
    lag_minutes: float, head_minutes: float,
) -> None:
    """Open the newest-first catch-up split when the cursor lags too far."""
    before = progress.cursor_before
    if lag_minutes <= 0 or before is None or progress.backfill is not None:
        return
    if (now - before[0]) <= timedelta(minutes=lag_minutes):
        return
    until = now - timedelta(minutes=max(1.0, head_minutes))
    if until <= before[0]:
        return
    progress.backfill = {
        "cursor": before, "from": before[0].isoformat(), "until": until,
        "opened_at": now.isoformat(),
    }
    progress.split_opened = {
        "from": before[0].isoformat(), "until": until.isoformat(),
        "lag_minutes": round((now - before[0]).total_seconds() / 60.0, 1),
    }
    progress.cursor_before = (until, _NIL_UUID)
    logger.warning(
        "event_clustering.catch_up_split from=%s until=%s lag_minutes=%.0f — "
        "the head resumes at now-%sm; the gap behind it drains oldest-first",
        before[0].isoformat(), until.isoformat(),
        progress.split_opened["lag_minutes"], head_minutes,
    )


def record_lag(progress: _sp.SliceProgress, *, now: datetime) -> None:
    """Fill ``lag_minutes`` / ``head_lag_minutes`` and log the lag line."""
    head = progress.cursor_after or progress.cursor_before
    if progress.head_caught_up:
        progress.head_lag_minutes = 0.0
    elif head is not None:
        progress.head_lag_minutes = max(0.0, (now - head[0]).total_seconds() / 60.0)
    if progress.backfill is not None:
        pos = progress.backfill.get("cursor")
        oldest = pos[0] if pos is not None else _sp._parse_ts(
            progress.backfill.get("from"))
        progress.lag_minutes = (
            max(0.0, (now - oldest).total_seconds() / 60.0)
            if oldest is not None else progress.head_lag_minutes
        )
    else:
        progress.lag_minutes = progress.head_lag_minutes
    logger.info(
        "event_clustering.lag lag_minutes=%s head_lag_minutes=%s backfill_open=%s"
        " pages=%d head_caught_up=%s",
        None if progress.lag_minutes is None else round(progress.lag_minutes),
        None if progress.head_lag_minutes is None
        else round(progress.head_lag_minutes),
        progress.backfill is not None, progress.pages, progress.head_caught_up,
    )


@dataclass
class FrontContext:
    """Everything one pass's pages share."""

    h: Any                         # the handler module (its seams)
    conn: Any
    ctx: Any
    budget: Any
    reserve: float
    since: datetime
    now: datetime
    match_threshold: float
    embedding_threshold: float
    block_window_hours: float
    max_events: int
    cache_limit: int
    qdrant: Any
    qdrant_collection: str
    funnel: Counter
    phase_seconds: dict[str, float]
    progress: _sp.SliceProgress
    newly_linked: dict[UUID, set[UUID]]
    new_context: dict[UUID, bool]
    open_events: list[dict[str, Any]] | None = None
    prepared: list[Any] | None = None
    page_totals: Counter = field(default_factory=Counter)


async def _page(
    fc: FrontContext, *, cursor: Any, size: int, until: datetime | None,
    floor: bool,
) -> tuple[list[tuple[datetime, UUID]], set[UUID], str | None, int]:
    """Read, cluster, match and write ONE page. Returns its positions, the
    signal ids still owed a write, the phase a budget cut stopped in, and
    the number of unnormalizable rows. ``floor``: the pass has processed
    nothing yet, so the page's first candidate runs whatever the budget."""
    h, conn, funnel, ps = fc.h, fc.conn, fc.funnel, fc.phase_seconds
    spent = lambda: h._spent(fc.budget, fc.reserve)  # noqa: E731
    kw: dict[str, Any] = {"since": fc.since, "max_signals": size, "cursor": cursor}
    if until is not None:
        kw["until"] = until
    with h._phase(ps, "slice"):
        evidence, positions = await h.fetch_signal_page(conn, **kw)
        cosines: dict = {}
        if evidence and not spent():
            cosines, semantic_counts = await h._semantic_cosines(
                conn, evidence, qdrant=fc.qdrant,
                collection=fc.qdrant_collection,
                threshold=fc.embedding_threshold,
            )
            funnel.update(semantic_counts)
    funnel["examined"] += len(evidence)
    unnormalizable = max(0, len(positions) - len(evidence))
    if not evidence:
        return positions, set(), None, unnormalizable
    # ``floor_cut``: the phase where a floor page found the budget spent. The
    # receipt still names it (``budget_exceeded``), but the floor carries the
    # page's first candidate through regardless.
    floor_cut: str | None = None
    if spent():
        if not floor:
            return positions, {e.id for e in evidence}, "cluster", unnormalizable
        floor_cut = "cluster"
    with h._phase(ps, "cluster"):
        candidates, match_counts = await asyncio.to_thread(
            h._cluster_page, evidence, sort=True,
            threshold=fc.match_threshold, embedding_cosines=cosines,
            embedding_threshold=fc.embedding_threshold,
            max_members=MAX_CLUSTER_MEMBERS,
            block_window_hours=fc.block_window_hours,
        )
    funnel.update(match_counts)
    # These four are ASSIGNED per pass (``match_counts`` may carry a key of
    # the same name, which the assignment overrides, as it always did).
    fc.page_totals["clustered"] += len(candidates)
    fc.page_totals["oversized"] += sum(c.oversized for c in candidates)
    fc.page_totals["declined_single_source"] += sum(
        c.decline_reason == "single_source" for c in candidates)
    fc.page_totals["signature_unmintable"] += sum(
        c.decline_reason == "signature_unmintable" for c in candidates)
    for key, value in fc.page_totals.items():
        funnel[key] = value
    if spent():
        if not floor:
            return positions, {s for c in candidates for s in c.signal_ids}, \
                "match", unnormalizable
        floor_cut = floor_cut or "match"
    if candidates and fc.open_events is None:
        with h._phase(ps, "match"):
            fc.open_events = await h._open_events(
                conn, analyst_id=fc.ctx.analyst_id, limit=fc.max_events)
            fc.prepared = await asyncio.to_thread(
                prepare_open_events, fc.open_events, cache_limit=fc.cache_limit)
    unprocessed: list[EventCandidate] = list(candidates)
    stopped: str | None = None
    for idx, candidate in enumerate(candidates):
        if spent():
            if not (floor and idx == 0):
                stopped = "write"
                break
            floor_cut = floor_cut or "write"
        unprocessed = candidates[idx + 1:]
        try:
            await _match_and_write(fc, candidate)
        except Exception as exc:  # noqa: BLE001 — isolate, record, move on
            funnel["candidates_skipped"] += 1
            fc.progress.skip(candidate.signal_ids,
                             f"{type(exc).__name__}: {exc}", now=fc.now)
    owed = {s for c in unprocessed for s in c.signal_ids} if stopped else set()
    if floor_cut is not None:
        fc.progress.floor_ran = True
    return positions, owed, floor_cut or stopped, unnormalizable


async def _match_and_write(fc: FrontContext, candidate: EventCandidate) -> None:
    """One candidate: match against the open set, write, refresh the set."""
    h, funnel = fc.h, fc.funnel
    if candidate.oversized:
        return
    with h._phase(fc.phase_seconds, "match"):
        matched = (await asyncio.to_thread(
            h._match_event, candidate, fc.open_events,
            threshold=fc.match_threshold, prepared_events=fc.prepared,
        )).matched
    if not candidate.promoted and matched is None:
        return
    if not candidate.promoted:
        # The corroboration bar decides whether a NEW event may open; it is
        # not a refusal to attach to an occurrence that already exists.
        candidate = replace(candidate, promoted=True,
                            decline_reason=f"reattached_{candidate.decline_reason}")
    with h._phase(fc.phase_seconds, "write"):
        event_id, _new, sit_linked = await h._write_candidate(
            fc.conn, candidate, matched=matched, ctx=fc.ctx,
            newly_linked=fc.newly_linked, new_context=fc.new_context,
        )
    if event_id is None:
        funnel["write_refusals"] += 1
        return
    funnel["promoted"] += 1
    funnel["situations_linked"] += sit_linked
    if matched is not None:
        funnel["reattached"] += 1
        # Refresh the local row so later candidates see the new members.
        row = dict(matched)
        row["signal_ids"] = list(set(row.get("signal_ids") or ())
                                 | set(candidate.signal_ids))
        row["entity_ids"] = list(set(row.get("entity_ids") or ())
                                 | {lk.entity_id for lk in entity_links_for(candidate)})
        row["entity_names"] = sorted(
            set(row.get("entity_names") or ())
            | {n for m in candidate.members for n in m.entity_names})
        row["geo"] = sorted(set(row.get("geo") or ()) | set(candidate.geo))
        if candidate.time_end and (
            row.get("latest_linked_at") is None
            or candidate.time_end > row["latest_linked_at"]
        ):
            row["latest_linked_at"] = candidate.time_end
        fc.open_events = [row if h._uuid(r.get("id")) == event_id else r
                          for r in fc.open_events]
        refreshed = (await asyncio.to_thread(
            prepare_open_events, [row], cache_limit=fc.cache_limit))[0]
        fc.prepared = [refreshed if h._uuid(i.row.get("id")) == event_id else i
                       for i in fc.prepared]
    else:
        # The same projection ``_open_event_read`` returns.
        row = dict((await fc.conn.fetchrow(_MINTED_EVENT_SQL, event_id)) or {})
        row["signal_ids"] = candidate.signal_ids
        row["source_ids"] = [m.source_id for m in candidate.members if m.source_id]
        row["entity_ids"] = [lk.entity_id for lk in entity_links_for(candidate)]
        row["entity_names"] = list(
            {n for m in candidate.members for n in m.entity_names})
        row["latest_linked_at"] = candidate.time_end
        fc.open_events.append(row)
        fc.prepared.append((await asyncio.to_thread(
            prepare_open_events, [row], cache_limit=fc.cache_limit))[0])


async def run_front(fc: FrontContext, *, lifecycle_cut: bool, max_pages: int) -> str | None:
    """One pass's slice: head pages first, then backfill pages, under the
    front budget, with the progress floor. Returns the first phase a budget
    cut stopped in (``None`` when every page it started finished)."""
    p, h = fc.progress, fc.h
    stopped: str | None = None
    # -- head ------------------------------------------------------------
    first = True
    while p.pages < max_pages:
        floor = first
        if not first and h._spent(fc.budget, fc.reserve):
            break
        # A floor page after a lifecycle cut is the smallest page; otherwise
        # the adaptive page (no extra budget probe before the first page).
        size = p.min_signals if (first and lifecycle_cut) else p.page
        cursor = p.cursor_after or p.cursor_before
        positions, owed, cut, unnorm = await _page(
            fc, cursor=cursor, size=size, until=None, floor=floor)
        p.pages += 1
        p.unnormalizable += unnorm
        if first:
            saved_page, p.page = p.page, size
            _sp.finish(p, page_positions=positions,
                       unprocessed_signal_ids=owed, slice_ran=True)
            p.page = saved_page
            if size != saved_page:          # a floor page never resizes
                p.next_page = (saved_page if p.processed else p.next_page)
        else:
            last, done = _sp.processed_prefix(positions, owed)
            p.rows_read += len(positions)
            p.processed += done
            if last is not None:
                p.cursor_after = last
        stopped = stopped or cut
        first = False
        if cut:
            break
        if len(positions) < size and not owed:
            p.head_caught_up = True
            break
        if owed or not positions:
            break
    # -- backfill ----------------------------------------------------------
    seg = p.backfill
    while seg is not None and p.pages < max_pages and not stopped:
        if h._spent(fc.budget, fc.reserve):
            break
        positions, owed, cut, unnorm = await _page(
            fc, cursor=seg.get("cursor") or (
                _sp._parse_ts(seg.get("from")), _NIL_UUID),
            size=p.max_signals, until=seg["until"], floor=False)
        p.pages += 1
        p.unnormalizable += unnorm
        last, done = _sp.processed_prefix(positions, owed)
        p.backfill_rows += len(positions)
        p.backfill_processed += done
        if last is not None:
            seg["cursor"] = last
        stopped = stopped or cut
        if len(positions) < p.max_signals and not owed:
            p.backfill_closed = {
                "from": seg.get("from"), "until": seg["until"].isoformat(),
                "closed_at": fc.now.isoformat(),
            }
            logger.info("event_clustering.backfill_closed from=%s until=%s",
                        seg.get("from"), seg["until"].isoformat())
            p.backfill = seg = None
            break
        if owed or not positions:
            break
    return stopped


__all__ = [
    "DEFAULT_CATCH_UP_HEAD_MINUTES",
    "DEFAULT_CATCH_UP_LAG_MINUTES",
    "DEFAULT_MAX_PAGES_PER_PASS",
    "FrontContext",
    "catch_up_knobs",
    "open_split",
    "record_lag",
    "run_front",
]
