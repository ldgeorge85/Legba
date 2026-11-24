# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""W9 VIEW-1 — the per-profile projection read: refusing BY NAME.

``GET /api/v1/registry/projection/{plane}`` — every section of one plane.
``GET /api/v1/registry/projection/{plane}/{section}`` — one section.

A plane is one profile's projection (``legba.data.profile_projection``), built
by the ``profile_projector`` analyst into ``profile_projection``. The contract
is ``graph_arcs``' (spec §4.2 rules 4–6), one vocabulary:

  * **Refusals are distinct states, never ``found=False``.** HTTP 409 with a
    machine-readable ``detail.state``: ``projection_disabled``
    (``LEGBA_PROFILE_PROJECTION`` off — nothing is built — or the plane's
    profile declares no projection; ``detail.reason`` says which),
    ``projection_empty`` (never built, built with zero rows, or the table not
    yet migrated) or ``projection_stale`` (``projected_at`` older than
    ``LEGBA_PROFILE_PROJECTION_MAX_AGE_SECONDS``; the detail states the age
    and the threshold). HTTP 404 with ``plane_unknown`` for a plane no profile
    declares and ``section_undeclared`` for a section the plane's view does
    not carry — each naming what IS known.
  * **Every 200 publishes the build stamp** — ``projected_at``,
    ``age_seconds``, ``max_age_seconds`` and the view digest — so a stale read
    is loud on the payload.
  * **A declared section with no rows is a 200 with ``rows: []``**, and when
    the build could not measure it (the frame family unloaded, a unit with no
    head) ``unmeasured`` says so in words: an empty section and an unread one
    are different answers.

Registry-slim: imports only the ``legba.data.profile_projection`` leaf and the
registry's own ``.api`` — never the analyst runtime (the poisoned-import test
asserts it).
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from ..profile_projection import (
    PLANE_UNKNOWN,
    PROFILE_PROJECTION_MAX_AGE_ENV,
    PROJECTION_DISABLED,
    PROJECTION_EMPTY,
    PROJECTION_OK,
    PROJECTION_STALE,
    SECTION_UNDECLARED,
    plane_catalog,
    projection_max_age_seconds,
    projection_state,
    read_plane,
    read_section,
)
from .api import RegistryAPIDeps, require_bearer

logger = logging.getLogger(__name__)

#: Deploy marker — the roll checklist greps for this line.
PROJECTION_API_VERSION = "2026-10/view1"


class ProjectionRow(BaseModel):
    key: str
    ord: int
    value: Any = None


class _Stamped(BaseModel):
    state: Literal["ok"] = PROJECTION_OK  # refusals never reach the envelope
    plane: str
    profile: str | None = None
    projected_at: datetime | None = None
    age_seconds: float | None = None
    max_age_seconds: float
    view_digest: str | None = None
    version: str | None = None


class ProjectionPlaneOut(_Stamped):
    """Every declared section of the plane, in the closed section order."""

    sections: dict[str, list[ProjectionRow]] = Field(default_factory=dict)
    #: section -> why the build could not measure (part of) it.
    unmeasured: dict[str, str] = Field(default_factory=dict)


class ProjectionSectionOut(_Stamped):
    section: str
    rows: list[ProjectionRow] = Field(default_factory=list)
    unmeasured: str | None = None


def _refuse(code: int, state_name: str, reason: str, **extra: Any) -> HTTPException:
    return HTTPException(status_code=code, detail={"state": state_name, "reason": reason, **extra})


def _refusal(state_name: str, plane: str, meta: dict[str, Any] | None) -> HTTPException:
    meta = meta or {}
    if state_name == PLANE_UNKNOWN:
        return _refuse(
            status.HTTP_404_NOT_FOUND, state_name, meta.get("reason") or "unknown plane",
            plane=plane, planes=meta.get("planes") or [],
        )
    if state_name == PROJECTION_DISABLED:
        return _refuse(status.HTTP_409_CONFLICT, state_name,
                       meta.get("reason") or "projection disabled", plane=plane)
    if state_name == PROJECTION_EMPTY:
        return _refuse(
            status.HTTP_409_CONFLICT, state_name,
            "the plane has never been built, holds zero rows, or the "
            "profile_projection table is not migrated",
            plane=plane,
        )
    if state_name == PROJECTION_STALE:
        at = meta.get("projected_at")
        age = meta.get("age_seconds")
        threshold = meta.get("max_age_seconds", projection_max_age_seconds())
        return _refuse(
            status.HTTP_409_CONFLICT, state_name,
            f"projected_at {at.isoformat() if at else '?'} is "
            f"{age:.0f} s old, past {PROFILE_PROJECTION_MAX_AGE_ENV}={threshold:.0f} s"
            if age is not None else f"older than {PROFILE_PROJECTION_MAX_AGE_ENV}",
            plane=plane,
            projected_at=at.isoformat() if at else None,
            age_seconds=age,
            max_age_seconds=threshold,
        )
    return _refuse(status.HTTP_409_CONFLICT, state_name, "projection unreadable", plane=plane)


def _stamp(plane: str, meta: dict[str, Any]) -> dict[str, Any]:
    return {
        "plane": plane,
        "profile": meta.get("profile"),
        "projected_at": meta.get("projected_at"),
        "age_seconds": meta.get("age_seconds"),
        "max_age_seconds": meta.get("max_age_seconds", projection_max_age_seconds()),
        "view_digest": meta.get("view_digest"),
        "version": meta.get("version"),
    }


def build_projection_router(deps: RegistryAPIDeps) -> APIRouter:
    """Mounted at ``/api/v1/registry``: ``/projection/{plane}[/{section}]``."""
    router = APIRouter(tags=["projection"])

    def _get_deps(request: Request) -> RegistryAPIDeps:
        return getattr(request.app.state, "registry_deps", deps)

    async def _ok_meta(conn: Any, plane: str) -> dict[str, Any]:
        state_name, meta = await projection_state(conn, plane)
        if state_name != PROJECTION_OK:
            raise _refusal(state_name, plane, meta)
        return meta or {}

    @router.get("/projection/{plane}", response_model=ProjectionPlaneOut)
    async def projection_plane(
        plane: str, request: Request, principal: str = Depends(require_bearer),
    ) -> ProjectionPlaneOut:
        catalog = plane_catalog()
        async with _get_deps(request).descriptor_registry.pg.acquire() as conn:
            meta = await _ok_meta(conn, plane)
            rows = await read_plane(conn, plane)
        declared = catalog.declared_sections(plane)
        return ProjectionPlaneOut(
            **_stamp(plane, meta),
            sections={s: [ProjectionRow(**r) for r in rows.get(s, [])] for s in declared},
            unmeasured=dict(meta.get("unmeasured") or {}),
        )

    @router.get("/projection/{plane}/{section}", response_model=ProjectionSectionOut)
    async def projection_section(
        plane: str, section: str, request: Request,
        principal: str = Depends(require_bearer),
    ) -> ProjectionSectionOut:
        catalog = plane_catalog()
        async with _get_deps(request).descriptor_registry.pg.acquire() as conn:
            meta = await _ok_meta(conn, plane)
            declared = catalog.declared_sections(plane)
            if section not in declared:
                raise _refuse(
                    status.HTTP_404_NOT_FOUND, SECTION_UNDECLARED,
                    f"plane {plane!r} does not carry section {section!r}",
                    plane=plane, sections=list(declared),
                )
            rows = await read_section(conn, plane, section)
        return ProjectionSectionOut(
            **_stamp(plane, meta),
            section=section,
            rows=[ProjectionRow(**r) for r in rows],
            unmeasured=(meta.get("unmeasured") or {}).get(section),
        )

    return router


__all__ = ["PROJECTION_API_VERSION", "build_projection_router"]
