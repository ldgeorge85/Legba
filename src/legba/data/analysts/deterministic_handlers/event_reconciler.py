# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``event_reconciler`` — deterministic event-edge writes (V3/P1).

Two relation classes are safe to infer from existing evidence:

* ``correlated_with`` — two producer ids minted the same ``event_signature``.
  That is the deliberate two-readings rule from spec §2.4: clustering and tower
  remain separate rows and the edge carries the correspondence.
* ``evolves_from`` — a later event's first evidence time follows an earlier
  event's end inside the configured adjacency window and the two share at least
  ``evolves_min_actors`` resolved actors.

``caused_by`` and ``contradicts`` remain unproduced by declaration. The schema
admits them, but no deterministic rule has earned them; asking this writer for
one raises with **SEAMS #56** rather than minting an attribution edge.

P1b (2026-09-23) gives the sweep the same turn discipline event_clustering got:
``LEGBA_EVENT_RECONCILER_PASS_BUDGET_SECONDS`` (default 120 s, under the 180 s
actor invoke timeout) bounds one pass, checked between phases and between edge
writes; the receipt always carries ``phase_seconds``, ``budget_exceeded`` and
``stopped_in_phase``. Edge upserts are idempotent, so a truncated pass resumes
for free on the next tick — the pair computation iterates newest-first so the
fresh frontier is always covered before the stable backlog.
"""

from __future__ import annotations

import logging
import os
import time
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
from itertools import combinations
from typing import Any, Callable, Iterator, Iterable, Mapping
from uuid import UUID, uuid4

from ...provenance import AnalystContext, events_enabled
from ...provenance.models import FindingPayload
from ....runtime.analyst_method import AnalystMethodResult
from .fact_contention_pass import PassBudget

logger = logging.getLogger(__name__)

SUB_HANDLER_NAME = "event_reconciler"
DEFAULT_MAX_EVENTS = 5000
DEFAULT_EVOLVES_MIN_ACTORS = 2
DEFAULT_EVOLVES_MAX_GAP_HOURS = 24 * 7
EDGE_WRITER_VERSION = "2026-09/p1b"

#: THE TURN BUDGET (2026-09-23, P1b) — same discipline as event_clustering's
#: ``EVENT_CLUSTERING_PASS_BUDGET``: one bounded pass, ``<= 0`` disables, the
#: ``pass_budget_seconds`` descriptor option mirrors it per-descriptor.
EVENT_RECONCILER_PASS_BUDGET = "LEGBA_EVENT_RECONCILER_PASS_BUDGET_SECONDS"
DEFAULT_PASS_BUDGET_SECONDS = 120.0

_ALLOWED_EDGE_TYPES = {
    "part_of", "caused_by", "evolves_from", "correlated_with", "contradicts"
}
_UNPRODUCED_EDGE_TYPES = {"caused_by", "contradicts"}
_SYMMETRIC_EDGE_TYPES = {"correlated_with", "contradicts"}


class EventEdgeUnsupportedError(RuntimeError):
    """An edge type the deterministic writer deliberately does not produce."""


def _uuid(value: Any) -> UUID | None:
    """Coerce ``value`` to UUID, returning ``None`` on malformed input."""
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return None


def _utc(value: Any) -> datetime | None:
    """Coerce a datetime / ISO string to aware UTC."""
    if isinstance(value, datetime):
        dt = value
    elif value:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    else:
        return None
    return (
        dt.replace(tzinfo=timezone.utc)
        if dt.tzinfo is None
        else dt.astimezone(timezone.utc)
    )


def _pass_budget_seconds(options: Mapping[str, Any]) -> float:
    """Per-tick wall-clock ceiling: descriptor option, env, then 120 s."""
    raw: Any = options.get("pass_budget_seconds")
    if raw is None:
        raw = os.getenv(EVENT_RECONCILER_PASS_BUDGET, "").strip()
        if not raw:
            return DEFAULT_PASS_BUDGET_SECONDS
    try:
        return float(raw)
    except (TypeError, ValueError):
        logger.warning(
            "event_reconciler.bad_pass_budget %r; using %s",
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


def _ctx(options: Mapping[str, Any]) -> AnalystContext:
    """Provenance context for one reconciler side-write."""
    return AnalystContext(
        analyst_id=str(options.get("analyst_id") or SUB_HANDLER_NAME),
        analyst_version=str(
            options.get("analyst_version") or EDGE_WRITER_VERSION
        ),
        run_id=_uuid(options.get("run_id")) or uuid4(),
        target_id=options.get("target_id"),
        target_version=options.get("target_version"),
    )


def _finding(data: Mapping[str, Any], *, changed: bool) -> AnalystMethodResult:
    """The reconciler's per-run receipt."""
    return AnalystMethodResult(
        finding=FindingPayload(
            title=f"Event reconciler ({'changed' if changed else 'no-op'})",
            body=f"event_reconciler {EDGE_WRITER_VERSION}: {dict(data)}",
            confidence=1.0,
            evidence=[],
            tags=["deterministic", SUB_HANDLER_NAME, "v3_events"],
            data={"sub_handler": SUB_HANDLER_NAME,
                  "pipeline_version": EDGE_WRITER_VERSION,
                  **dict(data)},
        ),
        usage={"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0},
        derived_from=[],
        force_trace_only=not changed,
    )


def _canonical_pair(edge_type: str, src: UUID, dst: UUID) -> tuple[UUID, UUID]:
    """Store symmetric edges in the CHECK's canonical uuid order."""
    if edge_type in _SYMMETRIC_EDGE_TYPES and str(src) > str(dst):
        return dst, src
    return src, dst


async def write_event_edge(
    conn: Any,
    *,
    src_event_id: Any,
    dst_event_id: Any,
    edge_type: str,
    confidence: float = 0.5,
    why: str,
    derived_from: Iterable[Any] = (),
    analyst_ctx: AnalystContext,
    valid_from: Any = None,
) -> bool:
    """Write one open event edge idempotently.

    Returns True only on insert; an existing open edge is confidence-maxed and
    lineage-unioned but counts as unchanged. ``caused_by`` and ``contradicts``
    are guarded by SEAM #56 and raise before SQL can mint an unearned edge.
    """
    edge = str(edge_type or "")
    if edge in _UNPRODUCED_EDGE_TYPES:
        raise EventEdgeUnsupportedError(
            f"SEAMS #56: deterministic event edges do not produce {edge!r}; "
            "causality and contradiction need an explicit adjudication lane"
        )
    if edge not in _ALLOWED_EDGE_TYPES:
        raise ValueError(f"unknown event edge_type {edge!r}")
    src = _uuid(src_event_id)
    dst = _uuid(dst_event_id)
    if src is None or dst is None or src == dst:
        raise ValueError("an event edge requires two distinct event ids")
    src, dst = _canonical_pair(edge, src, dst)
    derived = [
        x for x in (_uuid(v) for v in derived_from) if x is not None
    ]
    inserted = await conn.fetchval(
        """
        INSERT INTO event_edges
            (src_event_id, dst_event_id, edge_type, confidence, why,
             derived_from, valid_from, analyst_id, analyst_version, run_id)
        VALUES ($1, $2, $3, $4, $5, $6::uuid[], $7, $8, $9, $10)
        ON CONFLICT (src_event_id, dst_event_id, edge_type)
            WHERE valid_until IS NULL AND superseded_by IS NULL
        DO UPDATE SET
            confidence = GREATEST(event_edges.confidence, EXCLUDED.confidence),
            why = EXCLUDED.why,
            derived_from = (
                SELECT array_agg(DISTINCT x)
                  FROM unnest(event_edges.derived_from
                              || EXCLUDED.derived_from) AS x
            ),
            updated_at = NOW()
        RETURNING (xmax = 0) AS inserted
        """,
        src,
        dst,
        edge,
        max(0.0, min(1.0, float(confidence))),
        str(why or "")[:2000],
        derived,
        _utc(valid_from),
        analyst_ctx.analyst_id,
        analyst_ctx.analyst_version,
        analyst_ctx.run_id,
    )
    return bool(inserted)


_EVENT_ROWS_SQL = """
    SELECT e.id, e.event_signature, e.analyst_id, e.title, e.category,
           e.time_start, e.time_end, e.produced_at, e.derived_from,
           COALESCE(stats.latest_linked_at, e.time_end, e.produced_at)
               AS evidence_end,
           COALESCE(actors.entity_ids, '{}'::uuid[]) AS actor_ids
      FROM events e
      LEFT JOIN LATERAL (
          SELECT max(sel.linked_at) AS latest_linked_at
            FROM signal_event_links sel
           WHERE sel.event_id = e.id
      ) stats ON TRUE
      LEFT JOIN LATERAL (
          SELECT array_agg(DISTINCT eel.entity_id) AS entity_ids
            FROM event_entity_links eel
           WHERE eel.event_id = e.id
             AND eel.role = 'actor'
      ) actors ON TRUE
     ORDER BY e.time_start ASC NULLS LAST, e.id ASC
     LIMIT $1
"""


def _event_time_bounds(row: Mapping[str, Any]) -> tuple[datetime | None, datetime | None]:
    """Evidence span for adjacency, preferring measured linked_at."""
    start = _utc(row.get("time_start")) or _utc(row.get("produced_at"))
    end = _utc(row.get("evidence_end")) or _utc(row.get("time_end")) or start
    return start, end


def _shared_actors(left: Mapping[str, Any], right: Mapping[str, Any]) -> int:
    """Count shared resolved actors between two event rows."""
    a = {_uuid(v) for v in (left.get("actor_ids") or ())}
    b = {_uuid(v) for v in (right.get("actor_ids") or ())}
    return len({x for x in a if x is not None} & {x for x in b if x is not None})


def correlated_pairs(
    rows: Iterable[Mapping[str, Any]],
    *,
    should_stop: Callable[[], bool] | None = None,
) -> list[tuple[Mapping[str, Any], Mapping[str, Any]]]:
    """Same-signature rows produced under different analyst ids.

    ``should_stop`` (P1b) truncates the signature walk when the pass budget is
    spent; the partial list is still monotone-safe because edge writes are
    idempotent and the next tick recomputes the rest.
    """
    groups: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        sig = str(row.get("event_signature") or "")
        if sig:
            groups.setdefault(sig, []).append(row)
    pairs: list[tuple[Mapping[str, Any], Mapping[str, Any]]] = []
    for sig in sorted(groups):
        if should_stop is not None and should_stop():
            break
        producers = sorted(
            groups[sig], key=lambda r: (str(r.get("analyst_id")), str(r.get("id")))
        )
        for left, right in combinations(producers, 2):
            if left.get("analyst_id") != right.get("analyst_id"):
                pairs.append((left, right))
    return pairs


def evolves_pairs(
    rows: Iterable[Mapping[str, Any]],
    *,
    min_actors: int = DEFAULT_EVOLVES_MIN_ACTORS,
    max_gap_hours: int = DEFAULT_EVOLVES_MAX_GAP_HOURS,
    should_stop: Callable[[], bool] | None = None,
) -> list[tuple[Mapping[str, Any], Mapping[str, Any], int]]:
    """Latest temporally adjacent predecessor sharing ``min_actors`` actors.

    The source scan is evaluated NEWEST-first (``reversed``): a truncated pass
    then always covers the fresh frontier and the stable backlog catches up as
    capacity allows, rather than starving the live edge of the table.
    """
    items = [dict(r) for r in rows if _uuid(r.get("id")) is not None]
    out: list[tuple[Mapping[str, Any], Mapping[str, Any], int]] = []
    for src in reversed(items):
        if should_stop is not None and should_stop():
            break
        src_start, _src_end = _event_time_bounds(src)
        if src_start is None:
            continue
        best: tuple[datetime, int, str, Mapping[str, Any]] | None = None
        for dst in items:
            if dst["id"] == src["id"]:
                continue
            _dst_start, dst_end = _event_time_bounds(dst)
            if dst_end is None or dst_end > src_start:
                continue
            gap_hours = (src_start - dst_end).total_seconds() / 3600.0
            if gap_hours > max_gap_hours:
                continue
            shared = _shared_actors(src, dst)
            if shared < min_actors:
                continue
            key = (dst_end, shared, str(dst.get("id")), dst)
            if best is None or key[:3] > best[:3]:
                best = key
        if best is not None:
            out.append((src, best[3], best[1]))
    return out


async def _write_correlated(
    conn: Any,
    pairs: Iterable[tuple[Mapping[str, Any], Mapping[str, Any]]],
    *,
    ctx: AnalystContext,
    budget: PassBudget | None = None,
) -> int:
    """Write same-signature cross-producer ``correlated_with`` edges."""
    written = 0
    for left, right in pairs:
        if budget is not None and budget.exhausted():
            break
        lid, rid = _uuid(left.get("id")), _uuid(right.get("id"))
        if lid is None or rid is None:
            continue
        why = (
            "same event_signature minted by "
            f"{left.get('analyst_id')} and {right.get('analyst_id')}"
        )
        if await write_event_edge(
            conn,
            src_event_id=lid,
            dst_event_id=rid,
            edge_type="correlated_with",
            confidence=1.0,
            why=why,
            derived_from=[lid, rid],
            analyst_ctx=ctx,
        ):
            written += 1
    return written


async def _write_evolves(
    conn: Any,
    pairs: Iterable[tuple[Mapping[str, Any], Mapping[str, Any], int]],
    *,
    ctx: AnalystContext,
    min_actors: int = DEFAULT_EVOLVES_MIN_ACTORS,
    budget: PassBudget | None = None,
) -> int:
    """Write temporally adjacent same-actor ``evolves_from`` edges."""
    written = 0
    for src, dst, shared in pairs:
        if budget is not None and budget.exhausted():
            break
        sid, did = _uuid(src.get("id")), _uuid(dst.get("id"))
        if sid is None or did is None:
            continue
        start, _end = _event_time_bounds(src)
        if await write_event_edge(
            conn,
            src_event_id=sid,
            dst_event_id=did,
            edge_type="evolves_from",
            confidence=min(1.0, shared / max(1, min_actors)),
            why=f"temporally adjacent events share {shared} resolved actors",
            derived_from=[sid, did],
            analyst_ctx=ctx,
            valid_from=start,
        ):
            written += 1
    return written


def _receipt_data(
    counters: Counter,
    phase_seconds: Mapping[str, float],
    stopped_in_phase: str | None,
    *,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The uniform run receipt — counters, phase timings and budget state."""
    data = {
        **dict(counters),
        "phase_seconds": {
            k: round(float(v), 3) for k, v in phase_seconds.items()
        },
        "budget_exceeded": stopped_in_phase is not None,
        "stopped_in_phase": stopped_in_phase,
    }
    if extra:
        data.update(extra)
    return data


async def handle(
    inputs: list[dict[str, Any]],
    options: Mapping[str, Any],
    deps: Any | None = None,
) -> AnalystMethodResult:
    """Reconcile bounded event rows into deterministic edge writes."""
    counters = Counter({
        "events_examined": 0,
        "correlated_pairs": 0,
        "correlated_written": 0,
        "evolves_pairs": 0,
        "evolves_written": 0,
        "edge_refusals": 0,
    })
    phase_seconds = {"scan": 0.0, "correlated": 0.0, "evolves": 0.0}
    stopped_in_phase: str | None = None
    if not events_enabled():
        return _finding(
            _receipt_data(
                counters, phase_seconds, None,
                extra={"events_enabled": False},
            ),
            changed=False,
        )
    pool = getattr(deps, "pg_pool", None) if deps is not None else None
    max_events = max(1, int(options.get("max_events") or DEFAULT_MAX_EVENTS))
    min_actors = max(
        1, int(options.get("evolves_min_actors") or DEFAULT_EVOLVES_MIN_ACTORS)
    )
    max_gap = max(
        1,
        int(options.get("evolves_max_gap_hours") or DEFAULT_EVOLVES_MAX_GAP_HOURS),
    )
    budget = PassBudget(seconds=_pass_budget_seconds(options))
    if pool is None:
        rows = [r for r in inputs if isinstance(r, Mapping)]
        counters["events_examined"] = len(rows)
        with _phase(phase_seconds, "correlated"):
            counters["correlated_pairs"] = len(correlated_pairs(rows))
        with _phase(phase_seconds, "evolves"):
            counters["evolves_pairs"] = len(
                evolves_pairs(
                    rows, min_actors=min_actors, max_gap_hours=max_gap
                )
            )
        return _finding(
            _receipt_data(
                counters, phase_seconds, None, extra={"synthetic": True}
            ),
            changed=False,
        )
    ctx = _ctx(options)
    async with pool.acquire() as conn:
        with _phase(phase_seconds, "scan"):
            rows = await conn.fetch(_EVENT_ROWS_SQL, max_events)
        counters["events_examined"] = len(rows)

        # Each write_event_edge is one atomic upsert — no wrapping
        # transaction, so a budget-truncated pass keeps the edges it did land
        # and the next tick resumes for free on the idempotent writes.
        if not budget.exhausted():
            with _phase(phase_seconds, "correlated"):
                correlated = correlated_pairs(
                    rows, should_stop=budget.exhausted
                )
                counters["correlated_pairs"] = len(correlated)
                counters["correlated_written"] = await _write_correlated(
                    conn, correlated, ctx=ctx, budget=budget
                )
        if budget.exhausted() and stopped_in_phase is None:
            stopped_in_phase = "correlated"

        if not budget.exhausted():
            with _phase(phase_seconds, "evolves"):
                evolves = evolves_pairs(
                    rows,
                    min_actors=min_actors,
                    max_gap_hours=max_gap,
                    should_stop=budget.exhausted,
                )
                counters["evolves_pairs"] = len(evolves)
                counters["evolves_written"] = await _write_evolves(
                    conn, evolves, ctx=ctx, min_actors=min_actors,
                    budget=budget,
                )
        if budget.exhausted() and stopped_in_phase is None:
            stopped_in_phase = "evolves"
    changed = bool(counters["correlated_written"] or counters["evolves_written"])
    return _finding(
        _receipt_data(counters, phase_seconds, stopped_in_phase),
        changed=changed,
    )


__all__ = [
    "EDGE_WRITER_VERSION",
    "EVENT_RECONCILER_PASS_BUDGET",
    "EventEdgeUnsupportedError",
    "SUB_HANDLER_NAME",
    "correlated_pairs",
    "evolves_pairs",
    "handle",
    "write_event_edge",
]
