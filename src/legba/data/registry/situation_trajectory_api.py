# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Continuity P2 — the situation TRAJECTORY read surface.

``GET /api/v1/v3/situations/{situation_id}/trajectory`` — the append-only ledger
for one situation, newest first: what changed, when the EVIDENCE said so, why,
and exactly which findings moved it.

Additive by design (plan D5): a route and no panel. Phase 3 owns the UI, and
shipping the endpoint now means the panel that lands then has real data behind
it from day one rather than a schema and a promise. It also gives the alert
plane's ``situation_escalation`` page somewhere to point — an alert that says a
situation escalated is much less useful than one that links to how it got there.

WHAT THIS ROUTE REFUSES TO DO
-----------------------------
It does not synthesize. There is no "trend" field, no direction summary computed
across rows, no prose. The state is read off the newest row's ``state_to``
because that is where the state lives; everything else is the rows as written.
A reader that wants a narrative reads the ``situation_update`` findings the rows
point at — those are graded, and this is not the place to mint an ungraded
summary of graded claims.

Honesty contract, the family rule:
  * an UNKNOWN situation is a 404. It is a real distinction from a known
    situation with an empty ledger, and collapsing them would let a typo read as
    "we have never seen this move".
  * a KNOWN situation with no ledger rows returns ``events: []`` and
    ``state: null`` — never a fabricated ``watching``. "We have never assessed
    this frame's trajectory" and "we assessed it and it is being watched" are
    different facts and the wire shape keeps them different.
  * any read failure logs at INFO and returns HTTP 200 with ``measured: false``
    and an empty list, so a polling panel is never handed a 500 to hammer. An
    unmeasured response is visibly unmeasured.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from pydantic import BaseModel, Field

from ..situations.trajectory import read_trajectory
from .api import RegistryAPIDeps, require_bearer

logger = logging.getLogger(__name__)

_ROUTE = "/situations/{situation_id}/trajectory"

#: Hard cap on rows returned. A situation accrues at most a handful of deltas a
#: day; the bound is defensive, not expected to bind.
_MAX_LIMIT = 500


class TrajectoryEventOut(BaseModel):
    """One ledger row, exactly as written."""

    id: str
    #: ``escalates`` | ``de_escalates`` | ``broadens`` | ``unchanged_checkpoint``.
    delta: str
    #: EVIDENCE time — the ``produced_at`` of the newest finding this delta rests
    #: on. Never the time the tracker ran; ``created_at`` carries that.
    occurred_at: Optional[datetime] = None
    state_from: str
    state_to: str
    why: str
    #: The NEW findings that moved it. Empty only on an
    #: ``unchanged_checkpoint`` — every other delta is schema-barred from
    #: existing without evidence.
    derived_from: list[str] = Field(default_factory=list)
    #: The graded ``situation_update`` finding whose prose asserted this delta.
    source_output_id: str
    created_at: Optional[datetime] = None


class SituationTrajectoryOut(BaseModel):
    """One situation's trajectory."""

    situation_id: str
    name: str = ""
    #: The CURRENT trajectory state — the newest row's ``state_to``. ``null``
    #: when the ledger has never spoken about this situation.
    state: Optional[str] = None
    events: list[TrajectoryEventOut] = Field(default_factory=list)
    #: False when the read itself failed. An empty ``events`` with
    #: ``measured=true`` means "no trajectory recorded"; with ``measured=false``
    #: it means "we could not look".
    measured: bool = True


def _row(row: dict[str, Any]) -> TrajectoryEventOut:
    return TrajectoryEventOut(
        id=row["id"],
        delta=row["delta"],
        occurred_at=row["occurred_at"],
        state_from=row["state_from"],
        state_to=row["state_to"],
        why=row["why"],
        derived_from=row["derived_from"],
        source_output_id=row["source_output_id"],
        created_at=row["created_at"],
    )


def build_situation_trajectory_router(deps: RegistryAPIDeps) -> APIRouter:
    router = APIRouter(tags=["situations"])

    def _get_deps(request: Request) -> RegistryAPIDeps:
        return getattr(request.app.state, "registry_deps", deps)

    @router.get(_ROUTE, response_model=SituationTrajectoryOut)
    async def situation_trajectory(
        situation_id: UUID = Path(..., description="The situation's uuid."),
        limit: int = Query(default=50, ge=1, le=_MAX_LIMIT),
        _principal: str = Depends(require_bearer),
        deps_: RegistryAPIDeps = Depends(_get_deps),
    ) -> SituationTrajectoryOut:
        """How this situation moved, newest first, dated by its evidence."""
        try:
            async with deps_.descriptor_registry.pg.acquire() as conn:
                name = await conn.fetchval(
                    "SELECT name FROM situations WHERE id = $1", situation_id,
                )
                if name is None:
                    # A 404 only for a situation that genuinely is not there —
                    # decided BEFORE the ledger read so an empty ledger can never
                    # be mistaken for an unknown frame.
                    raise HTTPException(status_code=404, detail="unknown situation")
                events = await read_trajectory(conn, situation_id, limit=limit)
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001 — a panel must not get a 500
            logger.info("v3.situations.trajectory.unavailable err=%s", exc)
            return SituationTrajectoryOut(
                situation_id=str(situation_id), measured=False,
            )

        return SituationTrajectoryOut(
            situation_id=str(situation_id),
            name=str(name),
            # The newest row IS the state. Nothing is derived across rows.
            state=(events[0]["state_to"] if events else None),
            events=[_row(e) for e in events],
        )

    return router


__all__ = [
    "SituationTrajectoryOut",
    "TrajectoryEventOut",
    "build_situation_trajectory_router",
]
