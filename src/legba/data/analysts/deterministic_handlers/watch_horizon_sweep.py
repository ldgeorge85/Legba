# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``watch_horizon_sweep`` sub-handler — consult watches close at their horizon,
and a folded duplicate stops being watched twice (W5 WATCH-CLOSE).

WHY. F-4 mints a consult's watch candidate as a ``hypotheses`` ``open_question``
row whose horizon lives in the origin marker (``resolves_by`` inside
``diagnostic_evidence``; the table has no column for it). claim_watch watches
every open_question row and ignores that horizon, and W4b-fold stamps
``folded_onto`` on the later of two duplicates but leaves it open — so (a)
NOTHING closed a consult watch when its horizon passed (claim_watch kept matching
new signals against a watch whose window was over, and the mint's fold probe,
which already ignores lapsed watches, minted a fresh one beside it) and (b) a
folded duplicate was watched alongside the row it folded onto (W4b-F6). B-38 in
04_BEYOND_V1 is the general horizon sweep; this is its consult half.

THE RULE, over OPEN consult watches only (``status='open_question'`` and a
``created_by='consult'`` origin marker; a desk's own open question, a
``source_request`` row or anything not minted by a consult is never read):

  * the marker carries ``folded_onto``  -> closed, reason ``folded``,
    ``status='withdrawn'``;
  * else ``resolves_by`` is strictly before now -> closed, reason
    ``horizon_passed``, ``status='expired'``;
  * else (a live watch, or a marker whose horizon will not parse) -> untouched.

A closed row keeps its thesis, evidence and marker; the marker gains
``closed_reason`` / ``closed_by`` / ``closed_at``, and the row gets
``resolved_at = now()`` and ``resolved_by = 'watch_horizon_sweep'`` (the shape
``dispatched_question`` stamps on an ``answered`` question). NOTHING IS DELETED.

THE VOCABULARY, and why ``resolved_outcome`` is left NULL. ``hypotheses.status``
carries no CHECK constraint (the live values are active / open_question /
confirmed / refuted / answered); ``expired`` and ``withdrawn`` are the words the
inquiry ledger's own vocabulary uses for exactly these two ends, so no new
vocabulary is invented. ``resolved_outcome`` is a smallint CHECKed to 0/1 — the
EXOGENOUS label the Brier calibration sample reads — and a watch whose window
merely ran out is not an outcome: writing 0 would score "nothing happened" as a
refutation. It stays NULL; the reason lives in the marker.

Idempotent (a closed row is no longer open_question), bounded
(:data:`DEFAULT_BATCH_LIMIT` closes per tick, oldest first; the read is capped at
:data:`SCAN_LIMIT` rows), every close is its own short transaction that RE-CHECKS
status and marker under ``FOR UPDATE``. ``dry_run`` counts and writes nothing.
No LLM, no spend.

Output ``data`` keys (the cadence receipt): scanned, live, unparseable,
horizon_passed, folded, skipped_changed, batch_full, dry_run, batch_limit and
``closed_ids`` (``{"horizon_passed": [...], "folded": [...]}``).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Mapping

from ....runtime.analyst_method import AnalystMethodResult
from ...provenance.models import FindingPayload
from ..consult_watch_fold import CONSULT_WATCH_CONTAINMENT

logger = logging.getLogger(__name__)

SUB_HANDLER_NAME = "watch_horizon_sweep"

#: ``hypotheses.resolved_by`` of a row this sweep closed.
RESOLVED_BY = "watch_horizon_sweep"

#: Closes per tick.
DEFAULT_BATCH_LIMIT = 500
#: Open consult watches read per tick (oldest first, so the likeliest-lapsed rows
#: are always inside the window).
SCAN_LIMIT = 5000

REASON_HORIZON = "horizon_passed"
REASON_FOLDED = "folded"
#: reason -> the ``hypotheses.status`` the row moves to.
STATUS_FOR_REASON = {REASON_HORIZON: "expired", REASON_FOLDED: "withdrawn"}

_MARKER_KEY = "open_question_origin"
_ORIGIN = "consult"

# The first consult origin element is the row's marker (the same element
# consult_watch_fold reads). The containment filter rides the row it matched.
_SCAN_SQL = f"""
    SELECT h.id, m.e AS marker
      FROM hypotheses h
      CROSS JOIN LATERAL (
            SELECT e FROM jsonb_array_elements(h.diagnostic_evidence) e
             WHERE e ->> 'marker' = '{_MARKER_KEY}' AND e ->> 'origin' = '{_ORIGIN}'
               AND e ->> 'created_by' = '{_ORIGIN}'
             LIMIT 1
      ) m
     WHERE h.status = 'open_question'
       AND h.diagnostic_evidence @> $1::jsonb
     ORDER BY h.produced_at, h.id
     LIMIT {SCAN_LIMIT}
"""

_LOCK_SQL = (
    "SELECT diagnostic_evidence FROM hypotheses "
    "WHERE id = $1::uuid AND status = 'open_question' FOR UPDATE"
)

_CLOSE_SQL = """
    UPDATE hypotheses
       SET status = $2,
           diagnostic_evidence = $3::jsonb,
           resolved_at = $4,
           resolved_by = $5,
           updated_at = now()
     WHERE id = $1::uuid
"""


def _marker_of(raw: Any) -> dict[str, Any]:
    if isinstance(raw, (str, bytes)):
        try:
            raw = json.loads(raw)
        except ValueError:
            return {}
    return dict(raw) if isinstance(raw, Mapping) else {}


def _parse_horizon(raw: Any) -> datetime | None:
    if isinstance(raw, datetime):
        dt = raw
    else:
        try:
            dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def closing_reason(marker: Mapping[str, Any], now: datetime) -> str | None:
    """The reason this open consult watch should close, or ``None``.

    Folded wins over a passed horizon (the row's provenance is that it is a
    duplicate). A horizon that will not parse leaves the row alone."""
    if marker.get("folded_onto"):
        return REASON_FOLDED
    horizon = _parse_horizon(marker.get("resolves_by"))
    if horizon is not None and horizon < now:
        return REASON_HORIZON
    return None


def _zero_counters(*, batch_limit: int, dry_run: bool) -> dict[str, Any]:
    return {
        "dry_run": int(dry_run),
        "batch_limit": batch_limit,
        "scanned": 0,
        "live": 0,
        "unparseable": 0,
        "horizon_passed": 0,
        "folded": 0,
        "skipped_changed": 0,
        "batch_full": 0,
        "closed_ids": {REASON_HORIZON: [], REASON_FOLDED: []},
    }


def _positive_int(raw: Any, default: int) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


async def _close_one(
    pool: Any, row_id: Any, reason: str, now: datetime, counters: dict[str, Any],
) -> None:
    """Close one row under a lock, re-deciding from the locked marker."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            raw = await conn.fetchval(_LOCK_SQL, row_id)
            ev = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
            marker = None
            if isinstance(ev, list):
                marker = next(
                    (e for e in ev if isinstance(e, dict)
                     and e.get("marker") == _MARKER_KEY and e.get("origin") == _ORIGIN
                     and e.get("created_by") == _ORIGIN),
                    None,
                )
            if marker is None or closing_reason(marker, now) != reason:
                counters["skipped_changed"] += 1
                return
            marker.update(
                closed_reason=reason, closed_by=RESOLVED_BY, closed_at=now.isoformat(),
            )
            await conn.execute(
                _CLOSE_SQL, row_id, STATUS_FOR_REASON[reason], json.dumps(ev),
                now, RESOLVED_BY,
            )
    counters[reason] += 1
    counters["closed_ids"][reason].append(str(row_id))


async def sweep(
    pool: Any, options: Mapping[str, Any], *, now: datetime | None = None,
) -> dict[str, Any]:
    """One tick. ``now`` is injectable for tests; the handler passes none."""
    limit = _positive_int(options.get("batch_limit"), DEFAULT_BATCH_LIMIT)
    dry_run = bool(options.get("dry_run", False))
    now = now or datetime.now(timezone.utc)
    counters = _zero_counters(batch_limit=limit, dry_run=dry_run)

    async with pool.acquire() as conn:
        rows = await conn.fetch(_SCAN_SQL, CONSULT_WATCH_CONTAINMENT)
    counters["scanned"] = len(rows)

    due: list[tuple[Any, str]] = []
    for r in rows:
        marker = _marker_of(r["marker"])
        reason = closing_reason(marker, now)
        if reason is not None:
            due.append((r["id"], reason))
        elif _parse_horizon(marker.get("resolves_by")) is None:
            counters["unparseable"] += 1
        else:
            counters["live"] += 1
    counters["batch_full"] = int(len(due) > limit)
    due = due[:limit]

    if dry_run:
        for rid, reason in due:
            counters[reason] += 1
            counters["closed_ids"][reason].append(str(rid))
        return counters
    for rid, reason in due:
        await _close_one(pool, rid, reason, now, counters)
    return counters


def _build_finding(counters: Mapping[str, Any]) -> FindingPayload:
    verb = "would close" if counters.get("dry_run") else "closed"
    closed = int(counters.get("horizon_passed", 0)) + int(counters.get("folded", 0))
    title = (
        f"Consult watch horizon sweep: {verb} {closed} "
        f"({counters.get('horizon_passed', 0)} past horizon, "
        f"{counters.get('folded', 0)} folded); {counters.get('live', 0)} still live"
    )
    scalars = {k: v for k, v in counters.items() if k != "closed_ids"}
    body = "\n".join(f"{k}={v}" for k, v in scalars.items())
    tags = ["deterministic", SUB_HANDLER_NAME]
    if closed:
        tags.append("closed")
    return FindingPayload(
        title=title[:2048],
        body=body[:65536],
        confidence=1.0,
        evidence=[],
        tags=tags,
        data={
            "sub_handler": SUB_HANDLER_NAME,
            **scalars,
            "closed_ids": {k: list(v) for k, v in counters["closed_ids"].items()},
        },
    )


async def handle(
    inputs: list[dict[str, Any]],
    options: Mapping[str, Any],
    deps: Any | None,
) -> AnalystMethodResult:
    """Sub-handler entry point — see the module docstring. The ``inputs`` slice
    is ignored; the unit of work is the open consult watch set, read directly
    via ``deps.pg_pool``. A failure is logged and yields a zeroed receipt."""
    counters = _zero_counters(
        batch_limit=_positive_int(options.get("batch_limit"), DEFAULT_BATCH_LIMIT),
        dry_run=bool(options.get("dry_run", False)),
    )
    pool = getattr(deps, "pg_pool", None) if deps is not None else None
    if pool is not None:
        try:
            counters = await sweep(pool, options)
        except Exception as exc:  # noqa: BLE001 — a sweep failure never fails the tick
            logger.warning("watch_horizon_sweep.failed err=%s", exc)
    logger.info(
        "watch_horizon_sweep scanned=%s live=%s horizon_passed=%s folded=%s "
        "skipped_changed=%s unparseable=%s dry_run=%s",
        counters["scanned"], counters["live"], counters["horizon_passed"],
        counters["folded"], counters["skipped_changed"], counters["unparseable"],
        counters["dry_run"],
    )
    return AnalystMethodResult(
        finding=_build_finding(counters),
        usage={"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0},
    )


__all__ = [
    "DEFAULT_BATCH_LIMIT",
    "REASON_FOLDED",
    "REASON_HORIZON",
    "RESOLVED_BY",
    "SCAN_LIMIT",
    "STATUS_FOR_REASON",
    "SUB_HANDLER_NAME",
    "closing_reason",
    "handle",
    "sweep",
]
