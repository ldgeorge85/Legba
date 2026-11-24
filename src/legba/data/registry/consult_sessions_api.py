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

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from . import cited_ref_kinds as _ref_kinds
from . import consult_persistence
from . import session_auth as _session_auth
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
    # Recovery address + state (migration 0195). Without these a turn re-seeded
    # from History could not be re-synthesised: the panel had no id to POST to
    # and no way to tell a finished answer from a cut one, which is exactly the
    # state the ~$10 c8a0105c turn is in. ``request_id`` is NULL on every turn
    # written before 0195 — the panel then addresses the turn by its own id.
    request_id: str | None = None
    synthesis_status: str = "complete"
    # The response bag (migration 0223), lifted. ``response_recorded`` is False
    # for a turn with no bag — written before 0223, or a run that produced no
    # answer to measure — and every field below is then None: the panel says
    # "not measured", never zero. When it is True, ``hold_reason`` None means
    # "not held" and ``uncertainty_floor_applied`` None means "no floor moved".
    response_recorded: bool = False
    uncertainty: float | None = None
    provenance_census: dict[str, Any] | None = None
    hold_reason: str | None = None
    uncertainty_floor_applied: dict[str, Any] | None = None
    watch_candidates: list[str] | None = None
    # W3 / F-4 — what became of each candidate (minted / folded / refused).
    watch_minted: list[dict[str, Any]] | None = None
    model: str | None = None
    # W2 / F-6 — the context-ceiling stamp; None = it did not bite (or not recorded).
    context_compaction: dict[str, Any] | None = None
    # C-1b — the case this turn presented (``consult_case_schema.CaseRecord``);
    # None = it presented none. The workstation's Replay reads it from here.
    case: dict[str, Any] | None = None
    # C-2b-1 — the turn's consult writes (``None`` = not armed; ``[]`` = armed,
    # nothing written). The cards themselves render from ``GET /consult/writes``.
    writes: list[dict[str, Any]] | None = None
    # Q-1 — the turn contract (window, coverage, explanations, grounding, on_screen).
    contract: dict[str, Any] | None = None
    # X2 C1 — the usage receipt (``consult_usage/1``; ``{}`` = none recorded)
    # and the verification stamp (``consult_verification/1``; ``None`` = the
    # row predates judging, 0234). The turn header prints both.
    usage: dict[str, Any] = Field(default_factory=dict)
    verification: dict[str, Any] | None = None


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
        request: Request,
        limit: int = Query(default=50, ge=1, le=200),
        mode: Literal["chat", "deep"] | None = Query(default=None),
        _principal: str = Depends(require_bearer),
    ) -> list[ConsultSessionSummary]:
        """List consult session headers, most-recently-active first.

        X3-B3: a signed-in user lists ONLY the sessions they opened
        (``session_auth.consult_owner_filter``); the operator's bearer lists
        every session, as before."""
        rows = await consult_persistence.list_sessions(
            pg, limit=limit, mode=mode,
            principal=_session_auth.consult_owner_filter(request),
        )
        return [ConsultSessionSummary(**r) for r in rows]

    @router.get(
        "/consult/sessions/{session_id}",
        response_model=ConsultSessionDetail,
        status_code=status.HTTP_200_OK,
    )
    async def load_consult_session(
        request: Request,
        session_id: str,
        _principal: str = Depends(require_bearer),
    ) -> ConsultSessionDetail:
        """Load one session + its ordered turns (continue a prior conversation).

        X3-B3: a signed-in user is refused (403 ``not_row_owner``) on a
        session another principal opened."""
        await _session_auth.require_consult_session_owner(pg, request, session_id)
        session = await consult_persistence.load_session(pg, session_id)
        if session is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"consult session {session_id!r} not found",
            )
        # The refs a turn stored were labelled ``signal`` whatever they were
        # (or, from the deep transport, stored as bare ids): type them by the
        # table each id is in, so the Record sheet opens the right walk.
        # One lookup for the whole session, split back per turn.
        turns = [t for t in session.get("turns") or [] if t.get("cited_refs")]
        flat = [(i, r) for i, t in enumerate(turns) for r in t["cited_refs"]]
        typed = await _ref_kinds.typed_refs(pg, [r for _, r in flat])
        for t in turns:
            t["cited_refs"] = []
        for (i, _), r in zip(flat, typed):
            turns[i]["cited_refs"].append(r)
        return ConsultSessionDetail(**session)

    return router


__all__ = [
    "ConsultSessionDetail",
    "ConsultSessionSummary",
    "ConsultTurnOut",
    "build_consult_sessions_router",
]
