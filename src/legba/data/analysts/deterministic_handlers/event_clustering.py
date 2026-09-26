# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``event_clustering`` — DATA MODEL V3 / P1's one event writer.

Per tick the handler (P1c order — lifecycle FIRST):

1. recomputes lifecycle over every open event, plus any resolved event that
   received a link since its last ledger row, and writes the transition ledger;
2. reads a bounded canonical signal slice and clusters it with the four measured
   v1 features (entity fold Jaccard, normalized title distance, 24-hour temporal
   proximity, category overlap), scoring only pairs that survive BLOCKING;
3. adds the measured-but-separate embedding and shared-fact-subject gates;
4. applies the cross-source / official promotion bar and the B1 mega-bucket
   refusal;
5. matches promoted clusters against existing event identities — the event scan
   is bounded by the number of events, never by a signal lookback — and upserts
   through ``provenance.write_event``;
6. repeats the same signature-keyed upsert for the two bounded tower candidate
   paths, under a reserved budget share and a per-candidate wall.

P1g (2026-09-24) retired the leg's FIXED cost — the ~12 s every tower tick paid
before it compared a single candidate, out of a 60 s reserve that five of the
last six ticks exhausted mid-walk. The open-event read is now set-based (see
``_open_event_read.py``: 7.3 s -> 1.3 s live for ``tower_backfill``, output
byte-identical), and ``prepare_open_events`` carries its fold ACROSS ticks in a
bounded LRU keyed by every column the fold reads, so an open set that barely
moved is re-folded only where it moved (was 4.1-4.7 s per tick for ~1,000
events). ``tower.prepare_cache_hits`` / ``tower.prepare_cache_misses`` partition
``tower.events_prepared`` on the receipt. Nothing persisted about an event or a
link changes, so ``EVENT_CLUSTERING_VERSION`` is untouched.

P1e (2026-09-24) closed the cost P1d's wall left standing: with the aggregate
prepared once, the member cap engaged and the pre-filter trimming the walk, a
live profile of the three candidates past the cursor (125/119/76 members)
still measured ``_match_event`` at 90.5 s / 106.4 s / 13.8 s — because
``match_prepared_to_event`` re-``_prepare``d the EVENT side (folding its
entities, normalizing its title, direction-scanning it) fresh on every one of
up to 2,000 open events, for EVERY candidate, and the open-event set does not
change within a tick. ``prepare_open_events`` folds that set ONCE, right after
the tick's ``_open_events`` fetch (both the tower leg and the front match
phase); every candidate then scores against the same prepared list through
``match_prepared_pair`` — pure arithmetic over two already-folded sides, no
``_prepare`` call left on the hot path. The receipt's ``tower.events_prepared``
/ ``tower.prepare_seconds`` name the one-time fold's size and cost.
``prepare_open_events`` also memoizes the entity fold/junk lookups BY SURFACE
across that one call — a synthetic 1,000-event / 50-300-mention profile (the
realistic shape, heavy on repeated real-world entities) folded in ~9-10 s
un-memoized and ~0.2-0.3 s memoized; the per-candidate walk that follows lands
well under a second either way, since it is now arithmetic, not folding.

P1d (2026-09-24) made the tower leg's per-candidate wall real. The reserve fix's
first live tick ran ONE candidate 254.3 s against a 10 s ``tower_candidate_max_seconds``
and held the actor turn 381 s: ``_write_tower_candidate`` calls ``_match_event``
SYNCHRONOUSLY before its first await, and ``asyncio.wait_for`` cannot cancel
CPU work — matching a 120-member candidate against up to 2,000 open events
re-derived that fixed candidate's folded/normalized/direction-scanned features
on every row it walked. Four changes, none touching the reserve gate: the
aggregate side is ``prepare_aggregate``d ONCE and scored with
``match_prepared_to_event`` instead of re-``_prepare``d per row; the COMPARE
(never the write) is bounded to ``max_tower_members`` representative members
(newest first, then distinct source); the open-event walk is pre-filtered to
the candidate's own category/geo first; and ``_match_event`` itself now
accepts a cooperative ``deadline_monotonic``, checked every handful of rows,
so a candidate is matched or cut UNDER the wall from inside the loop that does
the work — ``asyncio.wait_for`` stays the outer guard for the write's awaits.

P1c (2026-09-23) moved the run's time to where the product is. The first
budgeted tick spent 83 s clustering 300 signals (44,850 all-pairs scores) plus
35 s matching, and the write, tower and lifecycle phases never ran; with the
tower leg on, ten candidates took 284 s and the pass overshot to 345 s. Three
changes: pairs are BLOCKED before scoring (``pair_block_window_hours``), the
lifecycle phase runs FIRST so the transitions are unconditional, and the tower
leg gets a reserved share of the budget (``tower_budget_share``) plus a
per-candidate wall (``tower_candidate_max_seconds``) that a candidate is never
started without. The tower page's lineage resolves in ONE batched round rather
than four queries per candidate.

P1b (2026-09-23) bounds the whole pass: P1's first live run held its Dapr actor
turn ~60 minutes because one pass had no budget, no cursor and no partial
receipt. The run now holds ``LEGBA_EVENT_CLUSTERING_PASS_BUDGET_SECONDS``
(default 120 s — under the 180 s actor invoke timeout), checked between phases
and between candidates; on exhaustion it writes the receipt with the partial
funnel, ``budget_exceeded``, the phase it stopped in and the resume cursor, and
returns. The signal slice resumes from a ``(fetched_at, id)`` watermark and the
two tower legs from ``(produced_at | occurred_at, id)`` watermarks, all on
``alert_trigger_watermarks`` keyed by this handler — a completed pass advances
them and an exhausted pass advances them to where it stopped.

``LEGBA_EVENTS`` remains the structural gate: with it off this handler returns an
audited no-op before it reads or writes the event plane. The descriptor ships
``state: draft``; turning the descriptor active without the flag still writes no
events.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator, Mapping
from uuid import UUID, uuid4

from ...events import _writes as _event_writes
from ...provenance import AnalystContext, EventPayload, events_enabled, write_event
from ...provenance.models import FindingPayload
from ....runtime.analyst_method import AnalystMethodResult
from ._event_candidates import (
    EventCandidate,
    cluster_candidate,
    entity_links_for,
    fetch_signal_evidence,
    fetch_tower_candidates,
    normalize_signal,
    signal_links_for,
    tower_producer_id,
)
from ._event_lifecycle import maintain_event_lifecycle
from ._event_matcher import (
    DEFAULT_EMBEDDING_THRESHOLD,
    DEFAULT_MATCH_THRESHOLD,
    DEFAULT_PAIR_BLOCK_WINDOW_HOURS,
    EVENT_CLUSTERING_VERSION,
    MAX_CLUSTER_MEMBERS,
    PreparedEvent,
    cluster_aggregate,
    cluster_evidence,
    match_prepared_pair,
    prepare_aggregate,
    prepare_open_events,
    prepared_cache_stats,
)
from ._open_event_read import fetch_open_events as _open_events
from .cross_source_dedup import (
    _linkable_neighbours,
    _qdrant_neighbours_batch,
)
from .fact_contention_pass import PassBudget

logger = logging.getLogger(__name__)

SUB_HANDLER_NAME = "event_clustering"
DEFAULT_LOOKBACK_HOURS = 24
DEFAULT_MAX_SIGNALS = 1000
DEFAULT_MAX_EVENT_CANDIDATES = 5000
DEFAULT_TOWER_WINDOW_DAYS = 30
DEFAULT_TOWER_FLOOR = 0.50
DEFAULT_MAX_TOWER_CANDIDATES = 500
DEFAULT_MAX_LIFECYCLE_EVENTS = 5000
DEFAULT_QDRANT_COLLECTION = "legba_signals"

#: P1c — the share of the turn budget held back for the tower leg while the
#: slice/cluster/match/write phases run. Before this, clustering could spend
#: the whole 120 s and the tower leg never started; the reserve is what makes
#: "the run spends its budget where the product is" enforceable rather than
#: aspirational.
DEFAULT_TOWER_BUDGET_SHARE = 0.25

#: P1c — the per-candidate wall on the tower leg. Measured 2026-09-23: ten
#: tower candidates took 284 s (~28 s each) and the leg's between-candidate
#: budget check let one pass finish at 345 s against a 120 s budget, because a
#: check that passes with 2 s left still admits a 28 s candidate. Two rules
#: close that: a candidate is never STARTED without this much budget left, and
#: one that runs past the wall is abandoned, counted and named on the receipt.
DEFAULT_TOWER_CANDIDATE_MAX_SECONDS = 10.0

#: P1d — the first live tick after the reserve fix ran ONE candidate 254.3 s
#: against a 10 s wall: ``_write_tower_candidate`` calls ``_match_event``
#: SYNCHRONOUSLY before its first await, and ``asyncio.wait_for`` cannot
#: cancel CPU work. Bounds the tower COMPARE, never the write: at most this
#: many representative members (newest first, then distinct source) feed
#: ``cluster_aggregate`` for the fuzzy event walk — the full member list still
#: reaches ``signal_links_for``/``entity_links_for``, so every derived signal
#: keeps its link on the written event regardless of the cap.
DEFAULT_MAX_TOWER_MEMBERS = 20

#: P1d — check the cooperative deadline every this many open events walked, so
#: the monotonic-clock read costs nothing next to the score itself but a
#: deadline that has already passed is still caught within a handful of rows
#: rather than after the whole event scan.
_DEADLINE_CHECK_EVERY = 25

#: The receipt is a finding body, not a profiler dump: the tower phase's
#: per-candidate timings are kept only for the first page-sized run of
#: candidates, which is every candidate at the shipped cap of 50.
_MAX_RECEIPT_TIMINGS = 50

#: THE TURN BUDGET (2026-09-23, P1b). P1's first live run held one actor turn
#: for ~60 minutes — the reconcile storm that queued behind it (148
#: ``reconcile.failed``, 84 ``ERR_ACTOR_INVOKE_METHOD``) is the incident this
#: constant exists to prevent. Bounds the wall clock one tick may hold; the
#: descriptor option ``pass_budget_seconds`` mirrors it per-descriptor;
#: ``<= 0`` disables (pre-P1b behavior). The marker name is what the deploy
#: verifies.
EVENT_CLUSTERING_PASS_BUDGET = "LEGBA_EVENT_CLUSTERING_PASS_BUDGET_SECONDS"
DEFAULT_PASS_BUDGET_SECONDS = 120.0

#: The watermark plane — the generic per-class table, one row per cursor key.
WATERMARK_CLASS = "event_clustering"
SIGNAL_CURSOR_KEY = "signal_slice"
TOWER_FINDINGS_CURSOR_KEY = "tower_findings"
TOWER_SITUATIONS_CURSOR_KEY = "tower_situations"

#: An all-zero id as the cursor's id half means "this timestamp INCLUSIVE":
#: ``(fetched_at, id) > (ts, NIL)`` re-reads every row at ``ts``, which is the
#: honest resume bound when a truncated pass stopped mid-candidate — the tail
#: re-clusters and the idempotent upserts absorb the overlap.
_NIL_UUID = UUID(int=0)


def _uuid(value: Any) -> UUID | None:
    """Coerce a UUID-ish option value, returning ``None`` on malformed input."""
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return None


def _coerce_int(value: Any, default: int) -> int:
    """Coerce an integer option with a positive floor."""
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return default


def _coerce_float(value: Any, default: float) -> float:
    """Coerce a numeric option with a [0, 1] clamp."""
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return default


def _coerce_seconds(value: Any, default: float) -> float:
    """Coerce an unclamped non-negative duration option (hours or seconds)."""
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return default


def _spent(budget: Any, reserve: float = 0.0) -> bool:
    """Must the pass yield now, holding ``reserve`` seconds back?

    ``reserve`` is the tower leg's share, claimed by the phases that run
    before it. A budget stand-in without ``remaining`` (the tests' probe
    counter) degrades to plain exhaustion, and either way this costs exactly
    ONE ``exhausted()`` probe so probe-counted tests stay deterministic.
    """
    if reserve > 0.0:
        remaining = getattr(budget, "remaining", None)
        if remaining is not None and remaining <= reserve:
            return True
    return bool(budget.exhausted())


def _affords(budget: Any, cost_seconds: float) -> bool:
    """Is there room to START work that may take ``cost_seconds``?

    The tower leg's overshoot to 345 s came from asking only "is the budget
    spent?" before a candidate that could run for 28 s. A budget stand-in
    without ``allows`` answers yes, leaving the plain exhaustion check as the
    only bound — the pre-P1c behaviour, never a tighter one.
    """
    allows = getattr(budget, "allows", None)
    return True if allows is None else bool(allows(cost_seconds))


def _coerce_bool(value: Any, default: bool) -> bool:
    """Coerce a descriptor boolean without treating ``"false"`` as truthy."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    return bool(value)


def _pass_budget_seconds(options: Mapping[str, Any]) -> float:
    """Per-tick wall-clock ceiling: the descriptor option wins when set, else
    ``LEGBA_EVENT_CLUSTERING_PASS_BUDGET_SECONDS``, else 120 s. ``<= 0`` means
    unbounded (the pre-P1b pass shape); a malformed value reads as the
    default, never as zero."""
    raw: Any = options.get("pass_budget_seconds")
    if raw is None:
        raw = os.getenv(EVENT_CLUSTERING_PASS_BUDGET, "").strip()
        if not raw:
            return DEFAULT_PASS_BUDGET_SECONDS
    try:
        return float(raw)
    except (TypeError, ValueError):
        logger.warning(
            "event_clustering.bad_pass_budget %r; using %s",
            raw, DEFAULT_PASS_BUDGET_SECONDS,
        )
        return DEFAULT_PASS_BUDGET_SECONDS


@contextmanager
def _phase(seconds: dict[str, float], name: str) -> Iterator[None]:
    """Accumulate one phase's monotonic seconds onto the receipt map."""
    t0 = time.monotonic()
    try:
        yield
    finally:
        seconds[name] = seconds.get(name, 0.0) + (time.monotonic() - t0)


def _decode_cursor(state: Any) -> tuple[datetime, UUID] | None:
    """Read a ``{"ts": iso, "id": uuid}`` watermark state into a bound tuple."""
    if not isinstance(state, Mapping):
        return None
    raw_ts = state.get("ts")
    ts: datetime | None
    if isinstance(raw_ts, datetime):
        ts = raw_ts if raw_ts.tzinfo else raw_ts.replace(tzinfo=timezone.utc)
    elif raw_ts:
        try:
            ts = datetime.fromisoformat(str(raw_ts).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    else:
        return None
    uid = _uuid(state.get("id"))
    if ts is None or uid is None:
        return None
    return (ts, uid)


async def _load_cursors(conn: Any) -> dict[str, tuple[datetime, UUID] | None]:
    """This handler's resume cursors off the shared watermark plane.

    Call-time import of the alert plane's watermark helpers — the
    situation_tracker precedent that keeps the deterministic-handler import
    graph acyclic.
    """
    from .alert_trigger_scan import _load_class_watermarks

    _seeded, rows = await _load_class_watermarks(conn, WATERMARK_CLASS)
    return {key: _decode_cursor(state) for key, state in rows.items()}


async def _save_cursor(
    conn: Any,
    key: str,
    pos: tuple[datetime, UUID] | None,
    *,
    prior: tuple[datetime, UUID] | None,
) -> None:
    """Persist one cursor's advance; a ``None`` position writes nothing."""
    if pos is None or pos == prior:
        return
    from .alert_trigger_scan import _upsert_watermark

    await _upsert_watermark(
        conn,
        WATERMARK_CLASS,
        key,
        {"ts": pos[0].isoformat(), "id": str(pos[1])},
        fired=False,
    )


def _fmt_cursor(pos: tuple[datetime, UUID] | None) -> dict[str, str] | None:
    """JSON-shape one cursor position for the receipt."""
    if pos is None:
        return None
    return {"ts": pos[0].isoformat(), "id": str(pos[1])}


def _receipt_data(
    funnel: Counter,
    phase_seconds: Mapping[str, float],
    cursors: Mapping[str, tuple[datetime, UUID] | None],
    stopped_in_phase: str | None,
    *,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The uniform run receipt — funnel, six phase timings, budget state and
    the cursor positions the pass ended at."""
    data = {
        "funnel": dict(funnel),
        "phase_seconds": {
            k: round(float(v), 3) for k, v in phase_seconds.items()
        },
        "budget_exceeded": stopped_in_phase is not None,
        "stopped_in_phase": stopped_in_phase,
        "cursor": {k: _fmt_cursor(v) for k, v in cursors.items()},
    }
    if extra:
        data.update(extra)
    return data


def _analyst_ctx(options: Mapping[str, Any], *, analyst_id: str | None = None) -> AnalystContext:
    """Build the provenance context for one side-write."""
    run_id = _uuid(options.get("run_id")) or uuid4()
    return AnalystContext(
        analyst_id=str(analyst_id or options.get("analyst_id") or SUB_HANDLER_NAME),
        analyst_version=str(options.get("analyst_version") or EVENT_CLUSTERING_VERSION),
        run_id=run_id,
        target_id=options.get("target_id"),
        target_version=options.get("target_version"),
    )


def _finding(reason: str, data: Mapping[str, Any], *, changed: bool = False) -> AnalystMethodResult:
    """The run receipt — a FindingPayload summary plus the funnel counters."""
    headline = "changed" if changed else "no-op"
    return AnalystMethodResult(
        finding=FindingPayload(
            title=f"Event clustering ({headline})"[:2048],
            body=(
                f"event_clustering {EVENT_CLUSTERING_VERSION}: {reason}\n"
                f"funnel={dict(data.get('funnel') or {})}"
            )[:65536],
            confidence=1.0,
            evidence=[],
            tags=["deterministic", SUB_HANDLER_NAME, "v3_events"],
            data={"sub_handler": SUB_HANDLER_NAME,
                  "pipeline_version": EVENT_CLUSTERING_VERSION,
                  **dict(data)},
        ),
        usage={"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0},
        derived_from=[],
        force_trace_only=not changed,
    )


async def _semantic_cosines(
    conn: Any,
    evidence: list[Any],
    *,
    qdrant: Any,
    collection: str,
    threshold: float,
) -> tuple[dict[tuple[UUID, UUID], float], dict[str, int]]:
    """Qdrant neighbours inside the candidate slice, under the structural gate."""
    counters = Counter()
    if qdrant is None:
        counters["semantic_unavailable"] = 1
        return {}, dict(counters)
    candidates = [e for e in evidence if _uuid(e.embedding_ref) is not None]
    if not candidates:
        return {}, dict(counters)
    counters["semantic_examined"] = len(candidates)
    neighbours, failed = await _qdrant_neighbours_batch(
        qdrant,
        collection,
        [e.embedding_ref for e in candidates],
        threshold,
    )
    counters["qdrant_errors"] = failed
    candidate_ids = {e.id for e in evidence}
    proposed: dict[tuple[UUID, UUID], float] = {}
    every_neighbour: set[UUID] = set()
    for item, hits in zip(candidates, neighbours):
        for raw_id, score in hits:
            nid = _uuid(raw_id)
            if nid is None or nid == item.id or nid not in candidate_ids:
                continue
            pair = (item.id, nid) if str(item.id) <= str(nid) else (nid, item.id)
            proposed[pair] = max(float(score), proposed.get(pair, 0.0))
            every_neighbour.add(nid)
    linkable = set(await _linkable_neighbours(conn, sorted(every_neighbour)))
    gated = {k: v for k, v in proposed.items() if k[0] in linkable and k[1] in linkable}
    counters["semantic_gated"] = len(proposed) - len(gated)
    counters["embedding_pairs"] = len(gated)
    return gated, dict(counters)


@dataclass(frozen=True)
class _MatchOutcome:
    """One candidate's result against the open-event walk."""

    matched: Mapping[str, Any] | None
    #: True when the members feeding the aggregate were reduced to the cap —
    #: the COMPARE was bounded, never the candidate's full provenance.
    capped: bool = False
    #: True when a ``deadline_monotonic`` passed mid-walk and the loop
    #: returned the best match found so far rather than finishing the scan.
    cut: bool = False


def _representative_members(
    members: tuple[Any, ...], cap: int
) -> tuple[Any, ...]:
    """At most ``cap`` members for the COMPARE — newest first, then filled
    out by distinct ``source_id`` before falling back to plain recency, so a
    capped aggregate still reflects the candidate's source diversity rather
    than ``cap`` copies of the newest wire's phrasing. Never touches the
    candidate's own ``members`` tuple — the write path keeps every one."""
    if cap <= 0 or len(members) <= cap:
        return members
    by_recency = sorted(
        members,
        key=lambda m: (
            _utc_evidence_time(m) or datetime.min.replace(tzinfo=timezone.utc),
            str(m.id),
        ),
        reverse=True,
    )
    chosen: list[Any] = []
    seen_sources: set[str] = set()
    for member in by_recency:
        if len(chosen) >= cap:
            break
        if member.source_id and member.source_id in seen_sources:
            continue
        chosen.append(member)
        if member.source_id:
            seen_sources.add(member.source_id)
    if len(chosen) < cap:
        chosen_ids = {m.id for m in chosen}
        for member in by_recency:
            if len(chosen) >= cap:
                break
            if member.id not in chosen_ids:
                chosen.append(member)
                chosen_ids.add(member.id)
    return tuple(chosen)


def _utc_evidence_time(member: Any) -> datetime | None:
    """A member's ``fetched_at``, tolerating a naive datetime (test rows)."""
    value = member.fetched_at
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _prefilter_predicate(
    candidate: EventCandidate,
) -> tuple[str, frozenset[str]]:
    """The category/geo terms a plausible-match row must share one of."""
    return (
        candidate.category.strip().lower(),
        frozenset(g.lower() for g in candidate.geo if g),
    )


def _plausibly_matches(
    row: Mapping[str, Any], *, category: str, geos: frozenset[str]
) -> bool:
    """One event row's plausible-match test — the shared predicate behind
    both :func:`_prefilter_open_events` and :func:`_prefilter_prepared_events`."""
    return (
        str(row.get("category") or "").strip().lower() == category
        or bool(geos & {str(g).lower() for g in (row.get("geo") or ())})
    )


def _prefilter_open_events(
    candidate: EventCandidate, events: list[Mapping[str, Any]]
) -> list[Mapping[str, Any]]:
    """Events that could plausibly match: sharing the candidate's own
    category or one of its own geo tokens — the signature's own fields,
    cheap to read off both sides before the aggregate matcher scores a
    single pair. A pass that would drop EVERY row falls back to the full
    set — the heuristic only trims the walk, it never refuses a
    reattachment the category/geo shortcut cannot see (e.g. a same-entity,
    same-hour candidate whose category drifted from the event's)."""
    category, geos = _prefilter_predicate(candidate)
    if not category and not geos:
        return events
    filtered = [
        row for row in events
        if _plausibly_matches(row, category=category, geos=geos)
    ]
    return filtered or events


def _prefilter_prepared_events(
    candidate: EventCandidate, items: list[PreparedEvent]
) -> list[PreparedEvent]:
    """:func:`_prefilter_open_events`'s counterpart over an already-prepared
    list (P1e) — same predicate, read off ``item.row`` so the hot tower walk
    never rebuilds a raw-dict view just to filter it."""
    category, geos = _prefilter_predicate(candidate)
    if not category and not geos:
        return items
    filtered = [
        item for item in items
        if _plausibly_matches(item.row, category=category, geos=geos)
    ]
    return filtered or items


def _match_event(
    candidate: EventCandidate,
    events: list[Mapping[str, Any]],
    *,
    threshold: float,
    max_members: int | None = None,
    prefilter: bool = False,
    deadline_monotonic: float | None = None,
    prepared_events: list[PreparedEvent] | None = None,
) -> _MatchOutcome:
    """Exact signature first, then the four-feature aggregate matcher.

    P1c built the candidate's aggregate ONCE and carried it through the event
    walk (was: re-derived per row, the measured 35 s match phase). P1d closed
    the cost that left on the AGGREGATE side: even with the aggregate
    supplied, the old ``match_cluster_to_event`` still re-``_prepare``d that
    FIXED aggregate — folding its entities, normalizing its title,
    direction-scanning it — on every one of up to 2,000 open events.
    ``prepare_aggregate`` does that ONCE here.

    P1e closes the cost P1d left on the EVENT side: even with the aggregate
    prepared once, the event side was still ``_event_like`` + ``_prepare``d
    fresh on every row, on every call — and a caller matching many candidates
    against the SAME open-event set (the tower leg) paid that fold again for
    every candidate. ``prepared_events`` lets a caller that has already run
    :func:`prepare_open_events` ONCE PER TICK hand the folded list straight
    in; every candidate then scores via :func:`match_prepared_pair`, pure
    arithmetic over two already-prepared sides. A caller with no prepared
    list (direct tests, one-shot callers) is unchanged: ``events`` is
    prefiltered/prepared inline, exactly as before.

    ``max_members`` bounds the aggregate to a representative subset (never
    the candidate's write-time member list); ``prefilter`` trims the walk to
    the events sharing the candidate's own category/geo first;
    ``deadline_monotonic``, checked every :data:`_DEADLINE_CHECK_EVERY` rows,
    makes the walk itself yield the best match found so far once its budget
    is gone — the loop that does the CPU work enforces the wall, rather than
    leaving it to ``asyncio.wait_for``, which cannot cancel a synchronous
    walk that never awaits.
    """
    for row in events:
        if row.get("event_signature") == candidate.event_signature:
            return _MatchOutcome(matched=row)
    members = candidate.members
    capped = bool(max_members) and len(members) > max_members
    if capped:
        members = _representative_members(members, max_members)
    aggregate = cluster_aggregate(members)
    if aggregate is None:
        return _MatchOutcome(matched=None, capped=capped)
    prepared = prepare_aggregate(aggregate)
    if prepared_events is not None:
        walk = (
            _prefilter_prepared_events(candidate, prepared_events)
            if prefilter else prepared_events
        )
    else:
        walk_raw = (
            _prefilter_open_events(candidate, events) if prefilter else events
        )
        walk = prepare_open_events(walk_raw)
    best: tuple[float, Mapping[str, Any] | None] = (0.0, None)
    cut = False
    for idx, item in enumerate(walk):
        if (
            deadline_monotonic is not None
            and idx % _DEADLINE_CHECK_EVERY == 0
            and time.monotonic() >= deadline_monotonic
        ):
            cut = True
            break
        match = match_prepared_pair(prepared, item.prepared, threshold=threshold)
        if match.linked and match.score > best[0]:
            best = (match.score, item.row)
    return _MatchOutcome(matched=best[1], capped=capped, cut=cut)


def _event_payload(
    candidate: EventCandidate,
    *,
    matched: Mapping[str, Any] | None,
) -> EventPayload:
    """Project one promoted candidate into the writer's payload shape."""
    prior_signals = {
        _uuid(v) for v in ((matched or {}).get("signal_ids") or ())
    }
    prior_sources = {
        str(v) for v in ((matched or {}).get("source_ids") or ()) if str(v)
    }
    sources = prior_sources | {m.source_id for m in candidate.members if m.source_id}
    prior_geo = {str(v).lower() for v in ((matched or {}).get("geo") or ())}
    geo = prior_geo | set(candidate.geo)
    starts = [x for x in (candidate.time_start, (matched or {}).get("time_start")) if x]
    ends = [x for x in (candidate.time_end, (matched or {}).get("time_end")) if x]
    return EventPayload(
        event_signature=str((matched or {}).get("event_signature") or candidate.event_signature),
        title=str((matched or {}).get("title") or candidate.title)[:2048],
        summary=candidate.title[:8192],
        category=candidate.category,
        event_type=candidate.event_type,  # type: ignore[arg-type]
        severity=candidate.severity,  # type: ignore[arg-type]
        time_start=min(starts) if starts else None,
        time_end=max(ends) if ends else None,
        geo=sorted(geo),
        locations=[],
        confidence=candidate.confidence,
        signal_count=len(prior_signals | set(candidate.signal_ids)),
        distinct_source_count=len(sources),
        oversized=candidate.oversized,
        valid_from=min(starts) if starts else None,
        valid_until=None,
        source_method=candidate.source_method,  # type: ignore[arg-type]
        signals=signal_links_for(candidate),
        entities=entity_links_for(candidate),
        data={
            "event_candidate_kind": candidate.kind,
            "member_count": len(candidate.members),
            "decline_reason": candidate.decline_reason,
            **candidate.data,
        },
    )


async def _write_candidate(
    conn: Any,
    candidate: EventCandidate,
    *,
    matched: Mapping[str, Any] | None,
    ctx: AnalystContext,
    newly_linked: dict[UUID, set[UUID]],
    new_context: dict[UUID, bool],
) -> tuple[UUID | None, int, int]:
    """Write one promoted event candidate.

    Returns ``(event_id, new_link_count, situations_linked)`` —
    ``situations_linked`` is the count of NEW ``situation_event_links`` rows
    this write created (V3/P1, the event/situation bridge: every mint or
    relink tries to attach the event to its matching open situations).
    """
    payload = _event_payload(candidate, matched=matched)
    prior_signals = {
        _uuid(v) for v in ((matched or {}).get("signal_ids") or ())
        if _uuid(v) is not None
    }
    prior_entities = {
        _uuid(v) for v in ((matched or {}).get("entity_ids") or ())
        if _uuid(v) is not None
    }
    prior_geo = {
        str(v).lower() for v in ((matched or {}).get("geo") or ()) if str(v)
    }
    async with conn.transaction():
        out, dlq = await write_event(
            conn,
            analyst_ctx=ctx,
            payload=payload,
            derived_from=candidate.derived_from,
        )
    if dlq is not None or out is None:
        return None, 0, 0
    # The write upsert can land on the matched row's id rather than the freshly
    # minted attempt id; resolve the durable id from the row, not OutputRow.id.
    event_id = _uuid((matched or {}).get("id")) or await conn.fetchval(
        "SELECT id FROM events WHERE event_signature = $1 AND analyst_id = $2",
        payload.event_signature,
        ctx.analyst_id,
    )
    if event_id is None:
        return None, 0, 0
    new_ids = set(candidate.signal_ids) - prior_signals
    newly_linked.setdefault(event_id, set()).update(new_ids)
    new_entities = {
        link.entity_id for link in payload.entities
    } - prior_entities
    new_geo = set(candidate.geo) - prior_geo
    if new_ids or new_entities or new_geo:
        new_context[event_id] = bool(new_entities or new_geo)
    # V3/P1 — every mint or relink tries to attach the event to its matching
    # OPEN situations (spec §2.4/§6.3): idempotent, so a re-run that finds
    # nothing new is a silent no-op. window_at anchors the situation-window
    # check on the event's own validity start, falling back to wall-clock
    # only when the candidate carries no time at all (a title/entity-only
    # cluster).
    situations_linked = await _event_writes.link_event_to_situations(
        conn,
        event_id,
        payload.event_signature,
        derived_from=candidate.derived_from,
        window_at=payload.valid_from or datetime.now(timezone.utc),
    )
    return event_id, len(new_ids), situations_linked


async def _write_tower_candidate(
    conn: Any,
    candidate: EventCandidate,
    *,
    events: list[dict[str, Any]],
    ctx: AnalystContext,
    threshold: float,
    funnel: Counter,
    newly_linked: dict[UUID, set[UUID]],
    new_context: dict[UUID, bool],
    max_members: int | None = None,
    deadline_monotonic: float | None = None,
    prepared_events: list[PreparedEvent] | None = None,
) -> None:
    """Match and write ONE tower candidate — the unit the P1c wall bounds.

    Extracted so ``asyncio.wait_for`` has a single awaitable to bound. The
    wall can only interrupt at an await point, and the measured 254.3 s
    overrun (2026-09-24) proved ``_match_event`` has none: it runs to
    completion as pure CPU before this coroutine's first await, so
    ``wait_for`` alone recorded the overrun rather than preventing it. P1d
    makes the wall real from the inside — ``max_members`` bounds the compare,
    and ``deadline_monotonic`` lets ``_match_event``'s own walk cut itself —
    so this coroutine reaches its first await well inside ``asyncio.wait_for``
    instead of relying on it. P1e — ``prepared_events`` is this tick's open
    events, folded ONCE by the caller (:func:`prepare_open_events`) before
    the candidate loop starts, so this call never re-derives the event side's
    features; ``events`` stays the raw list for the exact-signature check.
    """
    outcome = _match_event(
        candidate,
        events,
        threshold=threshold,
        max_members=max_members,
        prefilter=True,
        deadline_monotonic=deadline_monotonic,
        prepared_events=prepared_events,
    )
    if outcome.capped:
        funnel["tower_candidates_capped"] += 1
    if outcome.cut:
        funnel["tower_match_cut"] += 1
    matched = outcome.matched
    event_id, _new, sit_linked = await _write_candidate(
        conn,
        candidate,
        matched=matched,
        ctx=ctx,
        newly_linked=newly_linked,
        new_context=new_context,
    )
    if event_id is None:
        funnel["write_refusals"] += 1
        return
    funnel["tower_promoted"] += 1
    funnel["situations_linked"] += sit_linked  # P6-D1: links minted by this write
    if matched is not None:
        funnel["reattached"] += 1


async def handle(
    inputs: list[dict[str, Any]],
    options: Mapping[str, Any],
    deps: Any | None = None,
) -> AnalystMethodResult:
    """Run one bounded, budgeted, resumable event-clustering tick.

    Every receipt — flag-off, synthetic, clean or truncated — carries
    ``phase_seconds`` (slice/cluster/match/write/tower/lifecycle),
    ``budget_exceeded``, ``stopped_in_phase`` and the three cursor positions
    the pass ended at.
    """
    funnel = Counter({
        "examined": 0,
        "clustered": 0,
        "oversized": 0,
        "promoted": 0,
        "declined_single_source": 0,
        "reattached": 0,
        "tower_promoted": 0,
        "write_refusals": 0,
        "situations_linked": 0,
        # P1c blocking: scored + blocked partition the slice's pairs exactly.
        "pairs_examined": 0,
        "pairs_blocked": 0,
        "tower_candidates_over_wall": 0,
        # P1d — the member cap (aggregate reduced) and the cooperative
        # deadline (walk cut mid-scan) that make the per-candidate wall real.
        "tower_candidates_capped": 0,
        "tower_match_cut": 0,
    })
    funnel["transitions_by_kind"] = {}
    phase_seconds = {
        "slice": 0.0, "cluster": 0.0, "match": 0.0,
        "write": 0.0, "tower": 0.0, "lifecycle": 0.0,
    }
    cursors_out: dict[str, tuple[datetime, UUID] | None] = {
        SIGNAL_CURSOR_KEY: None,
        TOWER_FINDINGS_CURSOR_KEY: None,
        TOWER_SITUATIONS_CURSOR_KEY: None,
    }
    stopped_in_phase: str | None = None
    if not events_enabled():
        return _finding(
            "LEGBA_EVENTS is off; the event plane stayed untouched",
            _receipt_data(funnel, phase_seconds, cursors_out, stopped_in_phase,
                          extra={"events_enabled": False}),
        )

    pool = getattr(deps, "pg_pool", None) if deps is not None else None
    lookback_hours = _coerce_int(
        options.get("lookback_hours", DEFAULT_LOOKBACK_HOURS),
        DEFAULT_LOOKBACK_HOURS,
    )
    max_signals = _coerce_int(
        options.get("max_signals", DEFAULT_MAX_SIGNALS), DEFAULT_MAX_SIGNALS
    )
    max_events = _coerce_int(
        options.get("max_open_events", DEFAULT_MAX_EVENT_CANDIDATES),
        DEFAULT_MAX_EVENT_CANDIDATES,
    )
    match_threshold = _coerce_float(
        options.get("match_threshold", DEFAULT_MATCH_THRESHOLD),
        DEFAULT_MATCH_THRESHOLD,
    )
    embedding_threshold = _coerce_float(
        options.get("embedding_cosine_min", DEFAULT_EMBEDDING_THRESHOLD),
        DEFAULT_EMBEDDING_THRESHOLD,
    )
    qdrant_collection = str(
        options.get("qdrant_collection") or DEFAULT_QDRANT_COLLECTION
    )
    tower_days = _coerce_int(
        options.get("tower_window_days", DEFAULT_TOWER_WINDOW_DAYS),
        DEFAULT_TOWER_WINDOW_DAYS,
    )
    tower_floor = _coerce_float(
        options.get("tower_floor", DEFAULT_TOWER_FLOOR), DEFAULT_TOWER_FLOOR
    )
    max_tower = _coerce_int(
        options.get("max_tower_candidates", DEFAULT_MAX_TOWER_CANDIDATES),
        DEFAULT_MAX_TOWER_CANDIDATES,
    )
    include_tower = _coerce_bool(options.get("include_tower", True), True)
    max_lifecycle = _coerce_int(
        options.get("max_lifecycle_events", DEFAULT_MAX_LIFECYCLE_EVENTS),
        DEFAULT_MAX_LIFECYCLE_EVENTS,
    )
    block_window_hours = _coerce_seconds(
        options.get("pair_block_window_hours", DEFAULT_PAIR_BLOCK_WINDOW_HOURS),
        DEFAULT_PAIR_BLOCK_WINDOW_HOURS,
    )
    tower_share = _coerce_float(
        options.get("tower_budget_share", DEFAULT_TOWER_BUDGET_SHARE),
        DEFAULT_TOWER_BUDGET_SHARE,
    )
    tower_wall = _coerce_seconds(
        options.get(
            "tower_candidate_max_seconds", DEFAULT_TOWER_CANDIDATE_MAX_SECONDS
        ),
        DEFAULT_TOWER_CANDIDATE_MAX_SECONDS,
    )
    max_tower_members = _coerce_int(
        options.get("max_tower_members", DEFAULT_MAX_TOWER_MEMBERS),
        DEFAULT_MAX_TOWER_MEMBERS,
    )
    ctx = _analyst_ctx(options)
    now = datetime.now(timezone.utc)
    budget_seconds = _pass_budget_seconds(options)
    budget = PassBudget(seconds=budget_seconds)
    # The reserve only exists while there is a tower leg to protect and a
    # finite budget to divide; an unbounded pass reserves nothing.
    tower_reserve = (
        budget_seconds * tower_share
        if include_tower and budget_seconds > 0
        else 0.0
    )

    if pool is None:
        evidence = [e for e in (normalize_signal(i) for i in inputs) if e]
        funnel["examined"] = len(evidence)
        with _phase(phase_seconds, "cluster"):
            clusters, match_counts = cluster_evidence(
                evidence,
                threshold=match_threshold,
                embedding_threshold=embedding_threshold,
                max_members=MAX_CLUSTER_MEMBERS,
                block_window_hours=block_window_hours,
            )
            candidates = [cluster_candidate(c) for c in clusters]
        funnel.update(match_counts)
        funnel["clustered"] = len(candidates)
        funnel["oversized"] = sum(c.oversized for c in candidates)
        funnel["promoted"] = sum(c.promoted for c in candidates)
        funnel["declined_single_source"] = sum(
            c.decline_reason == "single_source" for c in candidates
        )
        return _finding(
            "no pg_pool injected; synthetic clustering only, no event writes",
            _receipt_data(funnel, phase_seconds, cursors_out, None,
                          extra={"synthetic": True}),
            changed=False,
        )

    qdrant = (getattr(deps, "extras", {}) or {}).get("qdrant")
    async with pool.acquire() as conn:
        cursors = await _load_cursors(conn)
        since = now - timedelta(hours=lookback_hours)

        # -- phase: lifecycle (FIRST, P1c) ------------------------------------
        # The transitions ARE the product, and before P1c they ran last: the
        # 2026-09-23 tick spent all 120 s on clustering and never reached them.
        # Running them first makes them unconditional.
        #
        # The cost is one tick of latency on links this pass is about to
        # write, and the P1b machinery already absorbs it: the open-event scan
        # measures LIVE ``signal_event_links``, and a resolved event re-enters
        # the scan through ``link_since_ledger`` whenever a link landed after
        # its last ledger row. So a link written below is transitioned by the
        # next tick rather than lost — the same guarantee that made a
        # budget-truncated pass safe.
        async with conn.transaction():
            with _phase(phase_seconds, "lifecycle"):
                lifecycle = await maintain_event_lifecycle(
                    conn,
                    now=now,
                    analyst_ctx=ctx,
                    max_events=max_lifecycle,
                    budget=budget,
                )
        funnel["transitions_by_kind"] = dict(lifecycle.get("transitions") or {})
        funnel["orphan_events"] = int(lifecycle.get("orphans") or 0)
        funnel["lifecycle_deferred"] = int(lifecycle.get("deferred") or 0)
        if lifecycle.get("deferred"):
            stopped_in_phase = "lifecycle"

        # -- phase: slice --------------------------------------------------
        # The fetch IS the examination — every row returned is counted, so a
        # truncated pass resumes the slice from the last candidate it failed
        # to finish writing rather than from the page boundary.
        evidence: list[Any] = []
        cosines: dict[tuple[UUID, UUID], float] = {}
        semantic_counts: dict[str, int] = {}
        if stopped_in_phase is None:
            with _phase(phase_seconds, "slice"):
                evidence = await fetch_signal_evidence(
                    conn,
                    since=since,
                    max_signals=max_signals,
                    cursor=cursors.get(SIGNAL_CURSOR_KEY),
                )
                if evidence and not _spent(budget, tower_reserve):
                    cosines, semantic_counts = await _semantic_cosines(
                        conn,
                        evidence,
                        qdrant=qdrant,
                        collection=qdrant_collection,
                        threshold=embedding_threshold,
                    )
        funnel["examined"] = len(evidence)
        funnel.update(semantic_counts)
        slice_tail = (
            (evidence[-1].fetched_at, evidence[-1].id)
            if evidence and evidence[-1].fetched_at is not None
            else None
        )

        # -- phase: cluster -------------------------------------------------
        candidates: list[EventCandidate] = []
        if evidence and _spent(budget, tower_reserve):
            stopped_in_phase = "cluster"
        elif evidence:
            with _phase(phase_seconds, "cluster"):
                clusters, match_counts = cluster_evidence(
                    evidence,
                    threshold=match_threshold,
                    embedding_cosines=cosines,
                    embedding_threshold=embedding_threshold,
                    max_members=MAX_CLUSTER_MEMBERS,
                    block_window_hours=block_window_hours,
                )
                candidates = [cluster_candidate(c) for c in clusters]
            funnel.update(match_counts)
            funnel["clustered"] = len(candidates)
            funnel["oversized"] = sum(c.oversized for c in candidates)
            funnel["declined_single_source"] = sum(
                c.decline_reason == "single_source" for c in candidates
            )
            funnel["signature_unmintable"] = sum(
                c.decline_reason == "signature_unmintable" for c in candidates
            )
            if _spent(budget, tower_reserve):
                stopped_in_phase = "match"

        # -- phases: match + write ------------------------------------------
        # ``unprocessed`` starts as every candidate and shrinks as the write
        # loop lands each one; the slice watermark resumes at the earliest
        # member position still owed a write.
        newly_linked: dict[UUID, set[UUID]] = {}
        new_context: dict[UUID, bool] = {}
        unprocessed = list(candidates)
        if candidates and stopped_in_phase is None:
            with _phase(phase_seconds, "match"):
                open_events = await _open_events(
                    conn, analyst_id=ctx.analyst_id, limit=max_events
                )
                # P1e — folded ONCE for the whole candidate loop below, not
                # once per candidate; kept in step with ``open_events`` by the
                # same reattach/mint refresh that keeps that list current.
                # P1g — and across ticks: an event unchanged in every column
                # the fold reads reuses the bundle it was folded into last
                # tick, so a settled open set costs no fold at all.
                prepared_open_events = prepare_open_events(
                    open_events, cache_limit=max_events
                )
            for idx, candidate in enumerate(candidates):
                if _spent(budget, tower_reserve):
                    stopped_in_phase = "write"
                    break
                unprocessed = candidates[idx + 1:]
                if candidate.oversized:
                    continue
                with _phase(phase_seconds, "match"):
                    # No cap, no prefilter, no deadline: this leg's candidates
                    # are already bounded by MAX_CLUSTER_MEMBERS, and this call
                    # keeps the exact pre-P1d behaviour.
                    matched = _match_event(
                        candidate, open_events, threshold=match_threshold,
                        prepared_events=prepared_open_events,
                    ).matched
                if not candidate.promoted and matched is None:
                    continue
                if not candidate.promoted:
                    # A corroboration bar decides whether a NEW event may open;
                    # it is not a refusal to attach a signal to an occurrence
                    # that already exists. This is what lets a resolved event
                    # reactivate on one late official/reporting link.
                    candidate = replace(
                        candidate,
                        promoted=True,
                        decline_reason=f"reattached_{candidate.decline_reason}",
                    )
                with _phase(phase_seconds, "write"):
                    event_id, _new, _sit_linked = await _write_candidate(
                        conn,
                        candidate,
                        matched=matched,
                        ctx=ctx,
                        newly_linked=newly_linked,
                        new_context=new_context,
                    )
                if event_id is None:
                    funnel["write_refusals"] += 1
                    continue
                funnel["promoted"] += 1
                funnel["situations_linked"] += _sit_linked
                if matched is not None:
                    funnel["reattached"] += 1
                    # Refresh the local row enough for subsequent candidates in
                    # the same tick to see the newly attached member ids / geo.
                    row = dict(matched)
                    row["signal_ids"] = list(
                        set(row.get("signal_ids") or ()) | set(candidate.signal_ids)
                    )
                    row["entity_ids"] = list(
                        set(row.get("entity_ids") or ())
                        | {l.entity_id for l in entity_links_for(candidate)}
                    )
                    row["entity_names"] = sorted(
                        set(row.get("entity_names") or ())
                        | {n for m in candidate.members for n in m.entity_names}
                    )
                    row["geo"] = sorted(set(row.get("geo") or ()) | set(candidate.geo))
                    if candidate.time_end and (
                        row.get("latest_linked_at") is None
                        or candidate.time_end > row["latest_linked_at"]
                    ):
                        row["latest_linked_at"] = candidate.time_end
                    open_events = [
                        row if _uuid(r.get("id")) == event_id else r
                        for r in open_events
                    ]
                    # P1e — the reattached row's features changed (entities,
                    # geo, latest link time), so its prepared bundle is
                    # re-folded here, ONCE, rather than left stale for the
                    # rest of this candidate loop.
                    # P1g — through the same cache: the rewritten entity /
                    # geo / link-time values are cache-key columns, so this is
                    # a miss that folds once and stores under the new stamps.
                    refreshed = prepare_open_events(
                        [row], cache_limit=max_events
                    )[0]
                    prepared_open_events = [
                        refreshed if _uuid(item.row.get("id")) == event_id else item
                        for item in prepared_open_events
                    ]
                else:
                    # The same projection ``_open_event_read`` returns, so a
                    # minted row and a fetched one are one shape in this list.
                    row = dict((await conn.fetchrow(
                        "SELECT id, event_signature, title, category, geo,"
                        " time_start, time_end, updated_at"
                        " FROM events WHERE id = $1", event_id
                    )) or {})
                    row["signal_ids"] = candidate.signal_ids
                    row["source_ids"] = [m.source_id for m in candidate.members if m.source_id]
                    row["entity_ids"] = [l.entity_id for l in entity_links_for(candidate)]
                    row["entity_names"] = list(
                        {n for m in candidate.members for n in m.entity_names}
                    )
                    row["latest_linked_at"] = candidate.time_end
                    open_events.append(row)
                    # P1e — a freshly minted event joins the prepared list too,
                    # so a later candidate in this same loop can reattach to it
                    # without falling back to an unprepared scan.
                    # P1g — a new id is a new cache key: folded once here and
                    # already warm for the next tick that reads it back.
                    prepared_open_events.append(
                        prepare_open_events([row], cache_limit=max_events)[0]
                    )

        # A slice whose write loop finished advances to the page tail; a slice
        # cut mid-write resumes INCLUSIVE at the earliest member position still
        # owed a write (the NIL-id bound re-reads every row at that timestamp —
        # idempotent upserts absorb the overlap); a slice that never reached
        # the write loop keeps its prior watermark, so the page is re-offered
        # rather than dropped.
        # P1c: lifecycle runs before the slice, so stopping there means the
        # slice never ran and ``evidence`` is empty — only ``tower`` can now
        # follow a fully consumed slice.
        slice_done = stopped_in_phase in (None, "tower")
        if evidence:
            if slice_done:
                if slice_tail is not None:
                    cursors_out[SIGNAL_CURSOR_KEY] = slice_tail
            else:
                floors = [
                    p for p in (c.cursor_position for c in unprocessed)
                    if p is not None
                ]
                if floors:
                    cursors_out[SIGNAL_CURSOR_KEY] = (
                        min(floors)[0], _NIL_UUID,
                    )

        # -- phase: tower ----------------------------------------------------
        unprocessed_tower: list[EventCandidate] = []
        tower_tails: dict[str, Any] = {}
        per_candidate_seconds: list[float] = []
        over_wall: list[str] = []
        # P1e — the open-event set this tick's tower candidates are matched
        # against, folded ONCE below (``events_prepared``/``prepare_seconds``
        # ride the ``tower`` receipt block) rather than once per candidate.
        events_prepared = 0
        prepare_seconds = 0.0
        # P1g — how much of that fold the cross-tick cache answered. The two
        # partition ``events_prepared``: a hit reused a bundle folded under
        # the same change stamps, a miss folded.
        prepare_cache_hits = 0
        prepare_cache_misses = 0
        # P1c review (2026-09-24): the front phases stop AT the reserve
        # boundary (``_spent(budget, tower_reserve)``), and that sets
        # ``stopped_in_phase`` — so a gate of "nothing stopped" meant the
        # reserve was honoured by the front and then never used (three live
        # ticks: cut in write at 90 s of 120, tower 0.0 s). A front cut is a
        # front cut, not a spent budget: the tower runs whenever its reserve
        # is still there (the whole budget is not exhausted).
        tower_ran = False
        tower_cut = False
        tower_may_run = include_tower and (
            stopped_in_phase is None
            or (tower_reserve > 0.0 and not _spent(budget))
        )
        if tower_may_run:
            tower_ran = True
            with _phase(phase_seconds, "tower"):
                tower_since = now - timedelta(days=tower_days)
                tower_candidates, tower_counts, tower_tails = (
                    await fetch_tower_candidates(
                        conn,
                        since=tower_since,
                        floor=tower_floor,
                        max_candidates=max_tower,
                        findings_cursor=cursors.get(TOWER_FINDINGS_CURSOR_KEY),
                        situations_cursor=cursors.get(
                            TOWER_SITUATIONS_CURSOR_KEY
                        ),
                    )
                )
                funnel.update(tower_counts)
                tower_ctx = _analyst_ctx(
                    options, analyst_id=tower_producer_id()
                )
                tower_events = await _open_events(
                    conn,
                    analyst_id=tower_ctx.analyst_id,
                    limit=max_events,
                )
                # P1e — the 90.5 s/106.4 s/13.8 s per-candidate cost profiled
                # 2026-09-24 was ``_match_event`` re-``_prepare``ing this SAME
                # open-event set fresh for every candidate; fold it once here,
                # right after the fetch, and hand the same list to every
                # candidate below.
                _prepare_started = time.monotonic()
                _cache_before = prepared_cache_stats()
                tower_prepared_events = prepare_open_events(
                    tower_events, cache_limit=max_events
                )
                prepare_seconds = time.monotonic() - _prepare_started
                events_prepared = len(tower_prepared_events)
                _cache_after = prepared_cache_stats()
                prepare_cache_hits = _cache_after["hits"] - _cache_before["hits"]
                prepare_cache_misses = (
                    _cache_after["misses"] - _cache_before["misses"]
                )
                unprocessed_tower = list(tower_candidates)
                for idx, candidate in enumerate(tower_candidates):
                    # The budget check runs BEFORE each candidate and asks
                    # whether a WHOLE candidate fits. Asking only "is the
                    # budget spent?" is what let a pass with 2 s left start a
                    # 28 s candidate and finish at 345 s against a 120 s
                    # budget.
                    if _spent(budget) or not _affords(budget, tower_wall):
                        # The receipt names the FIRST phase that was cut; the
                        # tower's own cut rides ``tower.cut`` beside it.
                        tower_cut = True
                        if stopped_in_phase is None:
                            stopped_in_phase = "tower"
                        break
                    started = time.monotonic()
                    # P1d — the deadline the candidate's OWN CPU-bound match
                    # walk self-enforces; ``asyncio.wait_for`` below stays the
                    # outer guard for the await-bound write, which is the only
                    # part of this coroutine it can actually cancel.
                    deadline = (
                        started + tower_wall if tower_wall > 0.0 else None
                    )
                    try:
                        await asyncio.wait_for(
                            _write_tower_candidate(
                                conn,
                                candidate,
                                events=tower_events,
                                ctx=tower_ctx,
                                threshold=match_threshold,
                                funnel=funnel,
                                newly_linked=newly_linked,
                                new_context=new_context,
                                max_members=max_tower_members,
                                deadline_monotonic=deadline,
                                prepared_events=tower_prepared_events,
                            ),
                            timeout=tower_wall or None,
                        )
                    except (asyncio.TimeoutError, TimeoutError):
                        funnel["tower_candidates_over_wall"] += 1
                        over_wall.append(str(candidate.source_ref_id))
                        logger.warning(
                            "event_clustering.tower_candidate_over_wall"
                            " ref=%s wall=%.1fs",
                            candidate.source_ref_id, tower_wall,
                        )
                    if len(per_candidate_seconds) < _MAX_RECEIPT_TIMINGS:
                        per_candidate_seconds.append(
                            round(time.monotonic() - started, 3)
                        )
                    unprocessed_tower = tower_candidates[idx + 1:]
                # P1d — ``tower.cut`` already means "the tower LOOP stopped
                # before every candidate was attempted" (insufficient budget
                # for another whole one); a candidate whose own match walk hit
                # its deadline mid-scan is the same promise broken a different
                # way, so it rides the same receipt flag.
                if funnel.get("tower_match_cut"):
                    tower_cut = True

        # Per-leg watermarks: a leg that ran to completion advances to the
        # last row it examined; a leg left with unprocessed candidates resumes
        # inclusive at the earliest of them; a leg that never ran keeps its
        # prior watermark so its backlog is not skipped.
        for kind, leg_key in (
            ("finding", TOWER_FINDINGS_CURSOR_KEY),
            ("situation_event", TOWER_SITUATIONS_CURSOR_KEY),
        ):
            leg = "findings" if kind == "finding" else "situations"
            if not tower_tails.get(f"{leg}_ran"):
                continue
            floors = [
                p for p in (
                    c.cursor_position for c in unprocessed_tower
                    if c.kind == kind
                )
                if p is not None
            ]
            if floors:
                cursors_out[leg_key] = (min(floors)[0], _NIL_UUID)
            else:
                tail = tower_tails.get(leg)
                if tail is not None and tail[0] is not None:
                    cursors_out[leg_key] = tail

        # The events this pass attached evidence to. Their transitions are the
        # NEXT tick's lifecycle phase (see the lifecycle-first banner above);
        # the count rides the receipt so the lag is visible rather than silent.
        funnel["events_touched"] = len(newly_linked)

        # Cursor persistence is unconditional: a completed pass advances and
        # an exhausted pass advances to where it stopped.
        async with conn.transaction():
            for key, pos in cursors_out.items():
                await _save_cursor(
                    conn, key, pos, prior=cursors.get(key)
                )

    changed = bool(
        funnel["promoted"]
        or funnel["tower_promoted"]
        or funnel["transitions_by_kind"]
    )
    reason = "clustered signals, promoted eligible candidates, and maintained lifecycle"
    if stopped_in_phase is not None:
        reason = (
            f"{EVENT_CLUSTERING_PASS_BUDGET} spent; stopped in "
            f"{stopped_in_phase} with a partial funnel and a saved cursor"
        )
    return _finding(
        reason,
        _receipt_data(
            funnel, phase_seconds, cursors_out, stopped_in_phase,
            extra={"tower": {
                "ran": tower_ran,
                "cut": tower_cut,
                "per_candidate_seconds": per_candidate_seconds,
                "candidates_over_wall": over_wall,
                "candidate_max_seconds": tower_wall,
                "budget_share": tower_share,
                # P1e — the open-event set folded ONCE for the whole tower
                # candidate loop, and how long that one fold took; the fix
                # for the 90.5 s/106.4 s/13.8 s per-candidate cost this
                # receipt used to hide inside ``per_candidate_seconds``.
                "events_prepared": events_prepared,
                "prepare_seconds": round(prepare_seconds, 3),
                "prepare_cache_hits": prepare_cache_hits,
                "prepare_cache_misses": prepare_cache_misses,
            }},
        ),
        changed=changed,
    )


__all__ = [
    "DEFAULT_LOOKBACK_HOURS",
    "DEFAULT_MAX_SIGNALS",
    "DEFAULT_MAX_TOWER_MEMBERS",
    "DEFAULT_PAIR_BLOCK_WINDOW_HOURS",
    "DEFAULT_PASS_BUDGET_SECONDS",
    "DEFAULT_TOWER_BUDGET_SHARE",
    "DEFAULT_TOWER_CANDIDATE_MAX_SECONDS",
    "EVENT_CLUSTERING_PASS_BUDGET",
    "EVENT_CLUSTERING_VERSION",
    "MAX_CLUSTER_MEMBERS",
    "SUB_HANDLER_NAME",
    "handle",
]
