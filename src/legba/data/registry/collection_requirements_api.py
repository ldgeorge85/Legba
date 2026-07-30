# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Collection-requirements operator review — ``/api/v1/v3/collection-requirements`` (R-2).

The read + disposition surface over the ``collection_requirements`` table
(migration 0113): the durable proposal object the ``collection_gap`` analyst
writes ("desk X lacks coverage of topic/dimension Y; here is the evidence;
here is what would satisfy it"). This module is the ONLY write surface for
these rows other than the analyst itself, and it can only ever touch the
small disposition sidecar (``status`` / ``reviewed_by`` / ``reviewed_at`` /
``disposition_note``) — never the content columns (topic / rationale /
evidence / candidate_sources / ...), and it has NO path to
``source_descriptors``. Registering or activating a source stays a wholly
separate operator action through the existing descriptor-registration
surface; marking a requirement ``registered`` here is bookkeeping ("I acted on
this"), never itself an activation.

Conventions mirror ``watchlist_api`` (the first v3 WRITE surface):
bearer-gated via :func:`~legba.data.registry.api.require_bearer`; validation
errors are 422 with a stated reason; no DELETE (content is append-only —
``dismissed`` is the closest thing to removal, and it is reversible: a later
PATCH can move it to any other status).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Literal, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from .api import RegistryAPIDeps, require_bearer

logger = logging.getLogger(__name__)

STATUSES = ("proposed", "reviewed", "registered", "dismissed")

_MAX_LIST = 500
_MAX_NOTE = 4000
_MAX_REVIEWER = 200


# ---------------------------------------------------------------------------
# Wire models
# ---------------------------------------------------------------------------


class RequirementOut(BaseModel):
    id: str
    natural_key: str
    origin: str
    desk: Optional[str] = None
    dimension: Optional[str] = None
    topic: str
    rationale: str
    evidence_kind: str
    evidence_id: str
    source_classes_wanted: list[str]
    candidate_sources: list[dict[str, Any]]
    suggested_fetch_url: Optional[str] = None
    fillable: bool
    unfillable_reason: Optional[str] = None
    priority_rank: int
    status: str
    reviewed_by: Optional[str] = None
    reviewed_at: Optional[datetime] = None
    disposition_note: Optional[str] = None
    created_at: datetime
    updated_at: datetime


class DispositionUpdate(BaseModel):
    """The ONLY writable surface: advance status + optionally stamp who/why.

    ``reviewed_by`` defaults to ``operator`` (the watchlist convention) — the
    principal is the raw bearer token value when a token is configured, and a
    secret must never be persisted into a data table.
    """

    status: Literal["proposed", "reviewed", "registered", "dismissed"]
    reviewed_by: str = Field(default="operator", min_length=1, max_length=_MAX_REVIEWER)
    disposition_note: Optional[str] = Field(default=None, max_length=_MAX_NOTE)


# ---------------------------------------------------------------------------
# SQL
# ---------------------------------------------------------------------------

_COLUMNS = """
    id::text AS id, natural_key, origin, desk, dimension, topic, rationale,
    evidence_kind, evidence_id::text AS evidence_id, source_classes_wanted,
    candidate_sources, suggested_fetch_url, fillable, unfillable_reason,
    priority_rank, status, reviewed_by, reviewed_at, disposition_note,
    created_at, updated_at
"""

_LIST_SQL = f"""
    SELECT {_COLUMNS}
      FROM collection_requirements
     WHERE ($1::text IS NULL OR status = $1)
       AND ($2::text IS NULL OR desk = $2)
     ORDER BY priority_rank, created_at DESC
     LIMIT $3
"""

_GET_SQL = f"SELECT {_COLUMNS} FROM collection_requirements WHERE id = $1"

_UPDATE_SQL = f"""
    UPDATE collection_requirements
       SET status = $2, reviewed_by = $3, reviewed_at = now(),
           disposition_note = $4, updated_at = now()
     WHERE id = $1
    RETURNING {_COLUMNS}
"""


def _requirement_out(row: Any) -> RequirementOut:
    def _jsonish(raw: Any, default: Any) -> Any:
        if isinstance(raw, str):
            try:
                return json.loads(raw)
            except (ValueError, TypeError):
                return default
        return raw if raw is not None else default

    return RequirementOut(
        id=str(row["id"]),
        natural_key=str(row["natural_key"]),
        origin=str(row["origin"]),
        desk=row["desk"],
        dimension=row["dimension"],
        topic=str(row["topic"]),
        rationale=str(row["rationale"] or ""),
        evidence_kind=str(row["evidence_kind"]),
        evidence_id=str(row["evidence_id"]),
        source_classes_wanted=list(_jsonish(row["source_classes_wanted"], []) or []),
        candidate_sources=list(_jsonish(row["candidate_sources"], []) or []),
        suggested_fetch_url=row["suggested_fetch_url"],
        fillable=bool(row["fillable"]),
        unfillable_reason=row["unfillable_reason"],
        priority_rank=int(row["priority_rank"]),
        status=str(row["status"]),
        reviewed_by=row["reviewed_by"],
        reviewed_at=row["reviewed_at"],
        disposition_note=row["disposition_note"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _coerce_uuid(raw: str) -> UUID:
    try:
        return UUID(str(raw))
    except (ValueError, TypeError):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"requirement id must be a UUID; got {raw!r}",
        )


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


def build_collection_requirements_router(deps: RegistryAPIDeps) -> APIRouter:
    """Construct the collection-requirements review router (mount under
    ``/api/v1/v3``)."""
    router = APIRouter(tags=["collection-requirements"])

    @router.get("/collection-requirements", response_model=list[RequirementOut])
    async def list_requirements(
        status_filter: Optional[str] = Query(default=None, alias="status"),
        desk: Optional[str] = Query(default=None),
        principal: str = Depends(require_bearer),
    ) -> list[RequirementOut]:
        """Open proposals first (default: no filter — every status), highest
        priority (lowest ``priority_rank``) first, newest tie-break. The
        WHAT/WHY/EVIDENCE an operator needs to act: ``topic`` + ``rationale``
        name the gap, ``evidence_kind``/``evidence_id`` point at the row that
        raised it, ``candidate_sources`` names what's already known,
        ``fillable``/``unfillable_reason`` says honestly whether we know of
        anything that could satisfy it."""
        if status_filter is not None and status_filter not in STATUSES:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"status must be one of {list(STATUSES)}; got {status_filter!r}",
            )
        async with deps.descriptor_registry.pg.acquire() as conn:
            rows = await conn.fetch(_LIST_SQL, status_filter, desk, _MAX_LIST)
        return [_requirement_out(r) for r in rows]

    @router.get(
        "/collection-requirements/{requirement_id}", response_model=RequirementOut
    )
    async def get_requirement(
        requirement_id: str,
        principal: str = Depends(require_bearer),
    ) -> RequirementOut:
        rid = _coerce_uuid(requirement_id)
        async with deps.descriptor_registry.pg.acquire() as conn:
            row = await conn.fetchrow(_GET_SQL, rid)
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"requirement {requirement_id} not found",
            )
        return _requirement_out(row)

    @router.patch(
        "/collection-requirements/{requirement_id}", response_model=RequirementOut
    )
    async def disposition_requirement(
        requirement_id: str,
        body: DispositionUpdate,
        principal: str = Depends(require_bearer),
    ) -> RequirementOut:
        """The ONLY write this route allows: advance ``status`` + stamp who
        and why. Never touches ``source_descriptors`` — marking a requirement
        ``registered`` records that the operator separately added/activated a
        real source through the existing registration path; it does not
        perform that activation."""
        rid = _coerce_uuid(requirement_id)
        async with deps.descriptor_registry.pg.acquire() as conn:
            row = await conn.fetchrow(
                _UPDATE_SQL,
                rid,
                body.status,
                body.reviewed_by.strip(),
                body.disposition_note,
            )
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"requirement {requirement_id} not found",
            )
        logger.info(
            "collection_requirements.dispositioned id=%s status=%s by=%r",
            row["id"], body.status, body.reviewed_by,
        )
        return _requirement_out(row)

    return router


__all__ = [
    "STATUSES",
    "DispositionUpdate",
    "RequirementOut",
    "build_collection_requirements_router",
]
