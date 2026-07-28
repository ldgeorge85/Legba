# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""P4-4 — the validity-window timeline read route.

ONE read-only GET endpoint mounted under ``/api/v1/v3`` beside the v3
telemetry + since routers (the SAME ``RegistryAPIDeps`` bundle +
``require_bearer`` gate, per the ``v3_api`` / ``since_api`` /
``substrate_reads_api`` wiring convention):

  * ``GET /timeline?target_id=<desk>&days=30`` — the temporal substrate as
    RANGED items over one window: facts (``[valid_from, valid_until)``),
    situations (lifecycle ``[valid_from, valid_until | last_event_at)``), and
    findings (``[produced_at, superseded_at)``). ``end=None`` is an OPEN window
    (the row is still the current head / has no close stamp) — the client
    extends it to "now" and marks it live, never fabricating a close.

There is NO existing read that carries validity windows: ``/findings`` returns
finding points with no ``superseded_at``/``superseded_by``, and facts have no
read route at all. So this route projects the three temporal tables' window +
supersession columns (``facts.valid_from``/``valid_until``/``superseded_by``
[mig 0032]; ``situations.valid_from``/``valid_until``/``superseded_by``
[mig 0040]; ``analyst_outputs.superseded_at``/``superseded_by`` [baseline]) into
one ranged-item envelope the `system.timeline` panel brushes.

Registry-slim (the ``since_api`` precedent): this module NEVER imports the
runtime / deterministic handlers. Pure timestamp reads over the persisted
window columns — no watermark machinery, no fabricated ends.

Honesty rules (house style):

  * Every kind reports its FULL matching ``count`` + an explicit ``truncated``
    flag — a capped list is never presented as the whole story.
  * ``end=None`` is surfaced verbatim (an OPEN, still-valid window); the server
    never invents a close timestamp for a row that has none.
  * A window with no matching rows returns a valid all-empty envelope
    (HTTP 200), never a 404.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Mapping

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from .api import RegistryAPIDeps, require_bearer

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Default window (days) when the client omits ``days``.
DEFAULT_DAYS: int = 30

#: Hard bound on the window — the timeline is a recent-validity view, not an
#: archive walk (mirrors the ``since_api`` / ``band_trajectory`` day bound).
MAX_DAYS: int = 90

#: Per-kind item cap (each kind still reports its full ``count`` + ``truncated``).
KIND_CAP: int = 300

#: The ranged-item kinds this route surfaces, in stable order.
TIMELINE_KINDS: tuple[str, ...] = ("fact", "situation", "finding")


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class TimelineItem(BaseModel):
    """One ranged validity-window item.

    ``end=None`` is an OPEN window (still the current head / no close stamp) —
    the client extends it to "now" and renders it live. ``superseded_by`` is the
    id of the row that replaced this one (the supersession-chain edge), NULL for
    a current head.
    """
    id: str
    kind: str            # 'fact' | 'situation' | 'finding'
    label: str
    start: datetime
    end: datetime | None
    status: str | None = None      # situation lifecycle status
    severity: str | None = None    # finding severity
    category: str | None = None    # situation category
    target_id: str | None = None
    superseded_by: str | None = None


class TimelineResponse(BaseModel):
    """The composed ranged-item envelope over the window.

    ``items`` interleaves all three kinds, newest window-start first.
    ``counts`` / ``truncated`` carry each kind's FULL matching total + whether
    its list was capped, so a capped list is honest, never presented as whole.
    """
    days: int
    server_now: datetime
    target_id: str | None = None
    items: list[TimelineItem] = Field(default_factory=list)
    counts: dict[str, int] = Field(default_factory=dict)
    truncated: dict[str, bool] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# SQL — pure window reads over the three temporal tables.
# ---------------------------------------------------------------------------

# Facts: validity window [COALESCE(valid_from, produced_at), valid_until).
# A closed fact carries valid_until (set to NOW() at close, mig 0049); an open
# one leaves it NULL — surfaced verbatim as end=None. Window filter keeps a row
# whose close (or, if open, its start) landed inside the window.
_FACTS_SQL = """
    SELECT id::text                              AS id,
           subject, predicate, value,
           COALESCE(valid_from, produced_at)     AS start_at,
           valid_until                           AS end_at,
           superseded_by::text                   AS superseded_by,
           target_id                             AS target_id,
           count(*) OVER ()                      AS total
      FROM facts
     WHERE ($1::text IS NULL OR target_id = $1)
       AND COALESCE(valid_until, valid_from, produced_at)
             > now() - make_interval(days => $2)
     ORDER BY start_at DESC, id DESC
     LIMIT $3
"""

# Situations: lifecycle window [COALESCE(valid_from, produced_at), end). The
# close is valid_until when stamped (superseded/closed), else last_event_at once
# the situation has resolved, else NULL (still open — end=None). Pure column
# reads; no clustering-decay recompute.
_SITUATIONS_SQL = """
    SELECT id::text                              AS id,
           name, status, category,
           COALESCE(valid_from, produced_at)     AS start_at,
           COALESCE(
               valid_until,
               CASE WHEN status = 'resolved' THEN last_event_at END
           )                                     AS end_at,
           superseded_by::text                   AS superseded_by,
           target_id                             AS target_id,
           count(*) OVER ()                      AS total
      FROM situations
     WHERE ($1::text IS NULL OR target_id = $1)
       AND COALESCE(valid_until, last_event_at, produced_at)
             > now() - make_interval(days => $2)
     ORDER BY start_at DESC, id DESC
     LIMIT $3
"""

# Findings: validity window [produced_at, superseded_at). A current head has
# superseded_at NULL (end=None, open); a superseded one carries the stamp + the
# superseding row's id (the chain edge). Structural / non-finding kinds excluded.
_FINDINGS_SQL = """
    SELECT id::text            AS id,
           title, severity,
           produced_at         AS start_at,
           superseded_at       AS end_at,
           superseded_by::text AS superseded_by,
           target_id           AS target_id,
           count(*) OVER ()    AS total
      FROM analyst_outputs
     WHERE kind = 'finding'
       AND ($1::text IS NULL OR target_id = $1)
       AND COALESCE(superseded_at, produced_at)
             > now() - make_interval(days => $2)
     ORDER BY start_at DESC, id DESC
     LIMIT $3
"""


# ---------------------------------------------------------------------------
# Pure helpers (unit-testable with no database)
# ---------------------------------------------------------------------------


def _validate_days(days: int) -> int:
    """Clamp-validate the window; a clear 400 rather than a silent default."""
    if days < 1 or days > MAX_DAYS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"days must be in [1, {MAX_DAYS}]",
        )
    return days


def fact_label(subject: Any, predicate: Any, value: Any) -> str:
    """A fact's ranged-item label — its ``subject · predicate · value`` triple,
    with empty parts dropped so a sparse fact never reads as ``· ·``."""
    parts = [str(p).strip() for p in (subject, predicate, value) if p is not None]
    parts = [p for p in parts if p]
    return " · ".join(parts) if parts else "(fact)"


def merge_items(*kind_lists: list[TimelineItem]) -> list[TimelineItem]:
    """Interleave per-kind item lists into one, newest window-start first.

    Pure reducer (no DB): the route fetches each kind separately (its own cap +
    honest total) then folds them here. Ties on ``start`` fall back to ``id`` so
    the order is deterministic across equal timestamps (batch inserts).
    """
    merged: list[TimelineItem] = []
    for lst in kind_lists:
        merged.extend(lst)
    merged.sort(key=lambda it: (it.start, it.id), reverse=True)
    return merged


# ---------------------------------------------------------------------------
# Row hydration
# ---------------------------------------------------------------------------


def _fact_item(row: Mapping[str, Any]) -> TimelineItem:
    return TimelineItem(
        id=row["id"],
        kind="fact",
        label=fact_label(row["subject"], row["predicate"], row["value"]),
        start=row["start_at"],
        end=row["end_at"],
        target_id=row["target_id"],
        superseded_by=row["superseded_by"],
    )


def _situation_item(row: Mapping[str, Any]) -> TimelineItem:
    return TimelineItem(
        id=row["id"],
        kind="situation",
        label=row["name"],
        start=row["start_at"],
        end=row["end_at"],
        status=row["status"],
        category=row["category"],
        target_id=row["target_id"],
        superseded_by=row["superseded_by"],
    )


def _finding_item(row: Mapping[str, Any]) -> TimelineItem:
    return TimelineItem(
        id=row["id"],
        kind="finding",
        label=row["title"],
        start=row["start_at"],
        end=row["end_at"],
        severity=row["severity"],
        target_id=row["target_id"],
        superseded_by=row["superseded_by"],
    )


def _total(rows: list[Any]) -> int:
    return int(rows[0]["total"]) if rows else 0


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------


def build_timeline_router(deps: RegistryAPIDeps) -> APIRouter:
    """Construct the P4-4 timeline router bound to the registry deps.

    The endpoint is a read-only GET, bearer-gated via ``require_bearer``,
    reading from the primary pool via ``deps.descriptor_registry.pg.acquire()``
    — the same path the rest of the v3 surface uses.
    """
    router = APIRouter(tags=["timeline"])

    @router.get("/timeline", response_model=TimelineResponse)
    async def timeline(
        target_id: str | None = Query(default=None),
        days: int = Query(default=DEFAULT_DAYS),
        principal: str = Depends(require_bearer),
    ) -> TimelineResponse:
        """Ranged validity-window items over ``days`` (see module doc)."""
        window = _validate_days(days)
        server_now = datetime.now(timezone.utc)
        cap = int(KIND_CAP)

        async with deps.descriptor_registry.pg.acquire() as conn:
            fact_rows = await conn.fetch(_FACTS_SQL, target_id, window, cap)
            situation_rows = await conn.fetch(_SITUATIONS_SQL, target_id, window, cap)
            finding_rows = await conn.fetch(_FINDINGS_SQL, target_id, window, cap)

        facts = [_fact_item(r) for r in fact_rows]
        situations = [_situation_item(r) for r in situation_rows]
        findings = [_finding_item(r) for r in finding_rows]

        counts = {
            "fact": _total(fact_rows),
            "situation": _total(situation_rows),
            "finding": _total(finding_rows),
        }
        truncated = {
            "fact": counts["fact"] > len(facts),
            "situation": counts["situation"] > len(situations),
            "finding": counts["finding"] > len(findings),
        }

        return TimelineResponse(
            days=window,
            server_now=server_now,
            target_id=target_id,
            items=merge_items(facts, situations, findings),
            counts=counts,
            truncated=truncated,
        )

    return router
