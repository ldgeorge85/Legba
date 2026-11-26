# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Origins and origin groups over HTTP.

  * ``GET  /api/v1/registry/origins`` — every origin with its group, the rule
    that put it there and whether an admin corrected it (any signed-in role).
  * ``GET  /api/v1/registry/origins/{origin_id}/log`` — the assignment log of
    one origin: every derived assignment and every correction, with who and why.
  * ``PUT  /api/v1/registry/origins/{origin_id}/group`` — an ADMIN moves one
    origin to another group (created if new). Body: ``group_key``
    (``<kind>:<name>``, e.g. ``host:example.org``), optional ``group_label``,
    required ``reason``. The origin is marked corrected (never re-derived) and
    an append-only ``origin_assignment_log`` row names the admin and reason.
  * ``POST /api/v1/registry/origins/derive`` — an ADMIN writes the derived
    assignment for every source head, ledger host and knowledge set that has
    none yet (corrected rows are never touched). Idempotent.

Who wrote each row is the request principal (``principals.handle_for_actor``
of the server-stamped actor); with sign-in off that is ``shared:admin-token``.
Role gates are in ``route_roles.ROUTE_ROLES`` (``docs/API_ROLES.md``).

REGISTRY-SLIM: stdlib + fastapi + pydantic + the stdlib-only ``origins`` and
``principals`` leaves.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from ..origins import OriginCorrectionError, correct_origin, derived_assignments, sync_derived
from ..owner_map import load_owner_map
from ..principals import handle_for_actor
from .api import RegistryAPIDeps, require_bearer

_LIST_SQL = """
SELECT o.id::text AS id, o.key, o.origin_kind, o.ref, o.label, o.rule, o.rule_detail,
       o.corrected, o.updated_at, g.id::text AS group_id, g.key AS group_key,
       g.label AS group_label, p.handle AS written_by
  FROM origins o
  JOIN origin_groups g ON g.id = o.origin_group_id
  LEFT JOIN principals p ON p.id = o.written_by
 WHERE ($1::text IS NULL OR o.origin_kind = $1)
 ORDER BY g.key, o.key
 LIMIT $2
"""
_LOG_SQL = """
SELECT l.id, l.at, l.rule, l.reason, gf.key AS from_group, gt.key AS to_group,
       p.handle AS written_by
  FROM origin_assignment_log l
  LEFT JOIN origin_groups gf ON gf.id = l.from_group_id
  JOIN origin_groups gt ON gt.id = l.to_group_id
  JOIN principals p ON p.id = l.written_by
 WHERE l.origin_id = $1::uuid
 ORDER BY l.at, l.id
"""

MAX_LIST = 5000


class OriginOut(BaseModel):
    id: str
    key: str
    origin_kind: str
    ref: str
    label: str
    rule: str
    rule_detail: dict[str, Any]
    corrected: bool
    group_id: str
    group_key: str
    group_label: str
    written_by: str | None


class OriginLogOut(BaseModel):
    id: int
    at: str
    rule: str
    reason: str
    from_group: str | None
    to_group: str
    written_by: str


class GroupCorrection(BaseModel):
    group_key: str = Field(min_length=3, max_length=260)
    group_label: str | None = Field(default=None, max_length=300)
    reason: str = Field(min_length=1, max_length=1000)


def _origin_out(r: Any) -> OriginOut:
    detail = r["rule_detail"]
    if isinstance(detail, (str, bytes)):
        detail = json.loads(detail)
    return OriginOut(
        id=r["id"], key=r["key"], origin_kind=r["origin_kind"], ref=r["ref"], label=r["label"],
        rule=r["rule"], rule_detail=dict(detail or {}), corrected=bool(r["corrected"]),
        group_id=r["group_id"], group_key=r["group_key"], group_label=r["group_label"],
        written_by=r["written_by"],
    )


def _uuid(raw: str) -> UUID:
    try:
        return UUID(raw)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail=f"origin_id {raw!r} is not a uuid") from exc


def _writer(principal: str) -> str:
    who = handle_for_actor(principal)
    if who is None:
        # Every admin caller resolves to a principal; an unnamed writer would
        # make an audit row that says nobody — refuse instead.
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="this caller resolves to no principal; it cannot write origins")
    return who


def build_origins_router(deps: RegistryAPIDeps) -> APIRouter:
    """Mount under ``/api/v1/registry``."""
    router = APIRouter(tags=["origins"])

    @router.get("/origins", response_model=list[OriginOut])
    async def list_origins(
        origin_kind: str | None = Query(default=None, max_length=32),
        limit: int = Query(default=MAX_LIST, ge=1, le=MAX_LIST),
        principal: str = Depends(require_bearer),
    ) -> list[OriginOut]:
        async with deps.descriptor_registry.pg.acquire() as conn:
            rows = await conn.fetch(_LIST_SQL, origin_kind, limit)
        return [_origin_out(r) for r in rows]

    @router.get("/origins/{origin_id}/log", response_model=list[OriginLogOut])
    async def origin_log(
        origin_id: str,
        principal: str = Depends(require_bearer),
    ) -> list[OriginLogOut]:
        oid = _uuid(origin_id)
        async with deps.descriptor_registry.pg.acquire() as conn:
            rows = await conn.fetch(_LOG_SQL, str(oid))
        return [OriginLogOut(
            id=int(r["id"]), at=r["at"].isoformat(), rule=r["rule"], reason=r["reason"],
            from_group=r["from_group"], to_group=r["to_group"], written_by=r["written_by"],
        ) for r in rows]

    @router.put("/origins/{origin_id}/group")
    async def correct_origin_group(
        origin_id: str,
        body: GroupCorrection,
        principal: str = Depends(require_bearer),
    ) -> dict[str, Any]:
        oid = _uuid(origin_id)
        who = _writer(principal)
        async with deps.descriptor_registry.pg.acquire() as conn:
            try:
                return await correct_origin(
                    conn, oid, group_key=body.group_key.strip(),
                    group_label=(body.group_label or "").strip() or None,
                    reason=body.reason, written_by=who,
                )
            except OriginCorrectionError as exc:
                code = (status.HTTP_404_NOT_FOUND if str(exc).startswith("no origin")
                        else status.HTTP_422_UNPROCESSABLE_ENTITY)
                raise HTTPException(status_code=code, detail=str(exc)) from exc

    @router.post("/origins/derive")
    async def derive_origins(
        principal: str = Depends(require_bearer),
    ) -> dict[str, Any]:
        """Write the derived assignment. The rows name the derivation rule;
        the request is the admin's (logged)."""
        _writer(principal)
        async with deps.descriptor_registry.pg.acquire() as conn:
            counts = await sync_derived(
                conn, await derived_assignments(conn, owner_map=load_owner_map()))
        return {"counts": counts}

    return router
