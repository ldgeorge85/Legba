# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Consult session-history read API — the audit-trail read surface (0038).

Mounts under ``/api/v1/consult/sessions``. Built via
``build_consult_sessions_router(deps)``; ``server.py`` wires it next to the
consult + deep-consult routers.

Why this exists
===============

The consult write path (``consult_api.py`` chat + the persisted-finding branch,
``deep_consult_api.py`` task submit + completion) records every conversation
into ``consult_sessions`` + ``consult_turns`` (migration ``0038``). This module
is the matching READ surface the SPA history sidebars consume:

  * ``GET /consult/sessions`` — list session headers, most-recently-active
    first (the history sidebar in the Consult panel + the task-history list in
    Deep Consult; the latter filters ``mode=deep``).
  * ``GET /consult/sessions/{id}`` — load one session's ordered turns so the
    client can RE-SEED its ``messages[]`` transcript and CONTINUE a prior
    conversation.

Read-only; bearer-gated like the rest of the registry API.
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from . import consult_persistence
from .api import RegistryAPIDeps, require_bearer

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Response shapes
# ---------------------------------------------------------------------------


class ConsultSessionSummary(BaseModel):
    """One row in the history list."""

    id: str
    mode: str
    title: str
    task_id: str | None = None
    run_id: str | None = None
    turn_count: int = 0
    created_at: str | None = None
    updated_at: str | None = None


class ConsultTurnOut(BaseModel):
    """One persisted turn, as the client re-seeds it."""

    id: str
    role: str
    content: str
    steps: list[Any] = Field(default_factory=list)
    tool_calls: list[Any] = Field(default_factory=list)
    cited_refs: list[Any] = Field(default_factory=list)
    finding_id: str | None = None
    created_at: str | None = None


class ConsultSessionDetail(BaseModel):
    """A session header + its ordered turns."""

    id: str
    mode: str
    title: str
    task_id: str | None = None
    run_id: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    turns: list[ConsultTurnOut] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------


def build_consult_sessions_router(deps: RegistryAPIDeps) -> APIRouter:
    """Construct the consult session-history read router bound to the deps.

    Mount on a FastAPI app via::

        app.include_router(build_consult_sessions_router(deps), prefix="/api/v1")
    """
    router = APIRouter(tags=["consult"])
    pg = deps.descriptor_registry.pg

    @router.get(
        "/consult/sessions",
        response_model=list[ConsultSessionSummary],
        status_code=status.HTTP_200_OK,
    )
    async def list_consult_sessions(
        limit: int = Query(default=50, ge=1, le=200),
        mode: Literal["chat", "deep"] | None = Query(default=None),
        _principal: str = Depends(require_bearer),
    ) -> list[ConsultSessionSummary]:
        """List consult session headers, most-recently-active first."""
        rows = await consult_persistence.list_sessions(pg, limit=limit, mode=mode)
        return [ConsultSessionSummary(**r) for r in rows]

    @router.get(
        "/consult/sessions/{session_id}",
        response_model=ConsultSessionDetail,
        status_code=status.HTTP_200_OK,
    )
    async def load_consult_session(
        session_id: str,
        _principal: str = Depends(require_bearer),
    ) -> ConsultSessionDetail:
        """Load one session + its ordered turns (continue a prior conversation)."""
        session = await consult_persistence.load_session(pg, session_id)
        if session is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"consult session {session_id!r} not found",
            )
        return ConsultSessionDetail(**session)

    return router


__all__ = [
    "ConsultSessionDetail",
    "ConsultSessionSummary",
    "ConsultTurnOut",
    "build_consult_sessions_router",
]
