# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``GET /v3/system/receipt-anchors`` — the external-timestamp ledger read.

H13. The ``receipt_anchor`` deterministic handler lands one
``receipt_anchors`` row per (day, calendar): the day's Merkle root over the
latest ``analyst_traces.receipt_hash`` per analyst, POSTed to a public
OpenTimestamps calendar. This route is the read surface — each row reports the
day, the root, the leaf count, the calendar, the status (``submitted`` /
``pending`` — a calendar that was unreachable, retried on the next tick) and
the stored proof bytes (hex).

Fully defensive (the ``system_escalations`` precedent): any query failure —
including migration 0212 not yet applied — degrades to an empty list at HTTP
200, the same honest "no anchors yet" a genuinely empty ledger produces.
Verification is deliberately NOT built in-tree: the operator verifies a stored
proof offline with the ``ots`` CLI (docs/ANALYSIS.md §measurement).

Registry-slim: stdlib + fastapi + pydantic + ``.api`` only. Nothing from
``legba.data.analysts`` or ``legba.runtime`` — this module ships in the slim
registry image.
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field

from .api import RegistryAPIDeps, require_bearer

logger = logging.getLogger(__name__)

RECEIPT_ANCHORS_ROUTE_VERSION = "2026-09/h13"

_ROUTE = "/system/receipt-anchors"


class ReceiptAnchorRow(BaseModel):
    """One (day, calendar) anchor. ``proof`` is the calendar's raw response
    bytes, hex-encoded (NULL while the row is ``pending``)."""

    id: str
    day: str
    root_hash: str
    leaf_count: int
    calendar: str
    status: str
    submitted_at: Optional[str] = None
    proof: Optional[str] = None


class ReceiptAnchorsOut(BaseModel):
    """The ledger listing — ``anchors`` newest-day-first; ``version`` stamps
    the route contract (the anchor contract itself is
    ``receipt_anchor.RECEIPT_ANCHOR_VERSION``, handler-side)."""

    version: str = RECEIPT_ANCHORS_ROUTE_VERSION
    anchors: list[ReceiptAnchorRow] = Field(default_factory=list)


def build_receipt_anchors_router(deps: RegistryAPIDeps) -> APIRouter:
    """Build the read-only router (mounted at ``/api/v1/v3``)."""
    router = APIRouter(tags=["system"])

    def _get_deps(request: Request) -> RegistryAPIDeps:
        return getattr(request.app.state, "registry_deps", deps)

    @router.get(_ROUTE, response_model=ReceiptAnchorsOut)
    async def system_receipt_anchors(
        request: Request,
        limit: int = Query(default=90, ge=1, le=365),
        principal: str = Depends(require_bearer),
    ) -> ReceiptAnchorsOut:
        """The anchor ledger, newest day first. ``limit`` bounds days worth of
        rows (two calendar rows per day; default 90 ≈ 45 days)."""
        d = _get_deps(request)
        try:
            async with d.descriptor_registry.pg.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT id::text AS id, day::text AS day, root_hash,
                           leaf_count, calendar, status,
                           submitted_at::text AS submitted_at,
                           encode(proof, 'hex') AS proof
                      FROM receipt_anchors
                     ORDER BY day DESC, calendar
                     LIMIT $1
                    """,
                    limit,
                )
        except Exception as exc:  # noqa: BLE001 — degrade to [], HTTP 200
            logger.info(
                "v3.system.receipt_anchors.unavailable err_class=%s",
                type(exc).__name__,
            )
            return ReceiptAnchorsOut()
        return ReceiptAnchorsOut(
            anchors=[ReceiptAnchorRow(**dict(r)) for r in rows],
        )

    return router
