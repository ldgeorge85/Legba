# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``GET/PUT /v3/me/seen`` — the signed-in person's own seen cursor (S-1).

The phone shell's "since you last looked" (plan 4.3): one timestamp per
surface per person, so the cursor follows the person across devices. Stored
in ``user_seen_cursors`` (migration 0259).

WHO
---
* Sign-in switch OFF (``LEGBA_AUTH_SIGNIN`` unset, today's default): there is
  no person to key on — every caller is the shared bearer — so both methods
  answer ``404`` with ``detail.error = "seen_cursor_unavailable"`` and
  ``detail.reason = "signin_off"``. The client keeps its cursor on the device
  (the plan's rule). Nothing is read or written.
* Switch ON: the caller's OWN rows only, keyed by the session's user id —
  never by anything in the request. There is no parameter naming another
  person, so no role (admin included) reads or moves anyone else's cursor
  through this route. A caller with no user behind it (a service credential,
  the break-glass admin token) gets the same ``404`` with
  ``reason = "no_user"``.

BOUNDS
------
* ``surface`` is one of :data:`SEEN_SURFACES` (the phone's five
  destinations; the table's CHECK is the same list), so a person holds at
  most five rows; nothing else is stored but the timestamp.
* A ``PUT`` names 1..5 surfaces. A ``seen_at`` more than
  :data:`FUTURE_SKEW_S` ahead of the server, or more than
  :data:`MAX_SEEN_LOOKBACK_DAYS` behind it, is a ``400``; one inside the skew
  is stored as the server's now.
* MONOTONIC: a stored cursor only moves forward (``GREATEST``), so a stale
  device cannot rewind what a newer one recorded.

Registry-slim: stdlib + fastapi + pydantic + ``.api`` + ``.route_roles``.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

from .api import RegistryAPIDeps, require_bearer
from .route_policy import signin as _signin
from .route_roles import request_principal

logger = logging.getLogger(__name__)

_ROUTE = "/me/seen"

#: The closed list — the phone shell's five destinations (plan 2.1). Must
#: equal the CHECK in migration 0259 (pinned by a test).
SEEN_SURFACES: tuple[str, ...] = ("changed", "watch", "ledger", "consult", "subjects")

#: A client clock this far ahead of the server is accepted (stored as now).
FUTURE_SKEW_S: float = 300.0
#: The ``/since`` cursor bound: an older seen time is refused.
MAX_SEEN_LOOKBACK_DAYS: int = 90

_SELECT_SQL = """
    SELECT surface, seen_at
      FROM user_seen_cursors
     WHERE user_id = $1::uuid
     ORDER BY surface
"""

_UPSERT_SQL = """
    INSERT INTO user_seen_cursors (user_id, surface, seen_at)
    SELECT $1::uuid, u.surface, u.seen_at
      FROM unnest($2::text[], $3::timestamptz[]) AS u(surface, seen_at)
    ON CONFLICT (user_id, surface) DO UPDATE
       SET seen_at = GREATEST(user_seen_cursors.seen_at, EXCLUDED.seen_at),
           updated_at = now()
"""


class MeSeenOut(BaseModel):
    """The caller's cursors: ``seen`` has a key only for a surface the person
    has marked (absent = never; the client falls back to its own default)."""

    seen: dict[str, datetime] = Field(default_factory=dict)
    surfaces: list[str] = Field(default_factory=lambda: list(SEEN_SURFACES))
    server_now: datetime


class MeSeenIn(BaseModel):
    """``{"seen": {"<surface>": "<iso-8601>"}}`` — 1..5 surfaces."""

    model_config = ConfigDict(extra="forbid")

    seen: dict[str, datetime] = Field(min_length=1, max_length=len(SEEN_SURFACES))


def _unavailable(reason: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={
            "error": "seen_cursor_unavailable",
            "reason": reason,
            "message": message,
        },
    )


def caller_user_id(request: Request) -> str:
    """The signed-in caller's user id, or the 404 this route answers."""
    if not _signin.signin_enabled():
        raise _unavailable(
            "signin_off",
            "sign-in is off on this server, so there is no per-person cursor; "
            "keep the cursor on the device",
        )
    who = request_principal(request)
    if who.user_id is None:
        raise _unavailable(
            "no_user",
            "this credential is not a signed-in person, so it has no seen cursor",
        )
    return who.user_id


def validate_seen(
    seen: dict[str, datetime], *, now: datetime,
) -> tuple[list[str], list[datetime]]:
    """Check a ``PUT`` body against the bounds. Clear 400s. Pure."""
    unknown = sorted(set(seen) - set(SEEN_SURFACES))
    if unknown:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"unknown surface {unknown!r}; one of {list(SEEN_SURFACES)}",
        )
    surfaces: list[str] = []
    stamps: list[datetime] = []
    for surface in sorted(seen):
        at = seen[surface]
        if at.tzinfo is None:
            at = at.replace(tzinfo=timezone.utc)
        if at - now > timedelta(seconds=FUTURE_SKEW_S):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"seen_at for {surface!r} is in the future",
            )
        if now - at > timedelta(days=MAX_SEEN_LOOKBACK_DAYS):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"seen_at for {surface!r} is more than "
                    f"{MAX_SEEN_LOOKBACK_DAYS} days old"
                ),
            )
        surfaces.append(surface)
        stamps.append(min(at, now))
    return surfaces, stamps


async def _read(conn: Any, user_id: str, now: datetime) -> MeSeenOut:
    rows = await conn.fetch(_SELECT_SQL, user_id)
    return MeSeenOut(
        seen={str(r["surface"]): r["seen_at"] for r in rows},
        server_now=now,
    )


def build_me_seen_router(deps: RegistryAPIDeps) -> APIRouter:
    """The two routes (mounted at ``/api/v1/v3``)."""
    router = APIRouter(tags=["me"])

    def _get_deps(request: Request) -> RegistryAPIDeps:
        return getattr(request.app.state, "registry_deps", deps)

    @router.get(_ROUTE, response_model=MeSeenOut)
    async def get_seen(
        request: Request,
        _principal: str = Depends(require_bearer),
    ) -> MeSeenOut:
        user_id = caller_user_id(request)
        now = datetime.now(timezone.utc)
        async with _get_deps(request).descriptor_registry.pg.acquire() as conn:
            return await _read(conn, user_id, now)

    @router.put(_ROUTE, response_model=MeSeenOut)
    async def put_seen(
        request: Request,
        body: MeSeenIn,
        _principal: str = Depends(require_bearer),
    ) -> MeSeenOut:
        user_id = caller_user_id(request)
        now = datetime.now(timezone.utc)
        surfaces, stamps = validate_seen(body.seen, now=now)
        async with _get_deps(request).descriptor_registry.pg.acquire() as conn:
            await conn.execute(_UPSERT_SQL, user_id, surfaces, stamps)
            return await _read(conn, user_id, now)

    return router


__all__ = [
    "FUTURE_SKEW_S",
    "MAX_SEEN_LOOKBACK_DAYS",
    "MeSeenIn",
    "MeSeenOut",
    "SEEN_SURFACES",
    "build_me_seen_router",
    "caller_user_id",
    "validate_seen",
]
