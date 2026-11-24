# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The consult front door's WRITE half and the write routes (C-2b-1; design C-2a §2, §6).

Kept out of ``consult_api.py`` (at its size-gate entry threshold): the chat
front door gains only the calls into :class:`WritesFront`.

* :meth:`WritesFront.require_available` — an ARMED request (``allow_writes``)
  on a registry where the ``consult_write`` pack is not registered, or the
  consult descriptor does not grant it, is refused **409
  ``writes_unavailable: consult_write not bound``** before anything runs.
* :meth:`WritesFront.arm` — threads ``allow_writes`` onto the actor's first
  input ONLY when armed (an unarmed invoke body is byte-identical to before)
  and records, registry-side, which run is armed, for which session, under
  which durable operator turn and on which plane. The executor reads those
  from HERE, never from the runtime or the model.
* :meth:`WritesFront.intercept_typed_code` — a chat message whose whole
  trimmed text is ``confirm XXXX`` is matched BEFORE any actor invoke: the
  staged write executes from its stored args through the executor, a user
  turn and an assistant receipt turn are appended, and no model is invoked.
* :func:`build_router` — ``POST /consult/writes`` (the runtime handler only:
  the request id must be a LIVE armed run this registry started),
  ``POST /consult/writes/{id}/confirm|cancel|revert`` (the operator's hand) and
  ``GET /consult/writes`` (the ordered log, scoped to a session or a run; a
  pending row carries its code on a scoped read while this process holds it,
  never on the ``?all=1`` whole-ledger read).
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any, Callable, Mapping

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field

from .. import consult_write_vocabulary as W
from . import consult_persistence
from . import workers as _workers
from ._deps import ANONYMOUS_PRINCIPAL
from .api import require_bearer
from .consult_write_exec import Armed, ConsultWriteExecutor, executor_for
from .descriptor import Family
from .errors import DescriptorNotFound

logger = logging.getLogger(__name__)

__all__ = ["WRITES_UNAVAILABLE", "WritesFront", "build_router", "receipt_line"]

WRITES_UNAVAILABLE = f"writes_unavailable: {W.CONSULT_WRITE_PACK_ID} not bound"


def is_writes_unavailable(text: Any) -> bool:
    """Did the actor refuse an armed turn for want of the ``consult_write`` binding?"""
    return f"writes_unavailable: {W.CONSULT_WRITE_PACK_ID} not bound" in str(text or "")

_TYPED_CODE = re.compile(r"^\s*confirm\s+([A-Za-z0-9]{4})\s*$", re.IGNORECASE)


class WriteCall(BaseModel):
    """``POST /consult/writes`` — the runtime handler's body. Closed."""

    model_config = ConfigDict(extra="forbid")

    request_id: str = Field(min_length=1, max_length=200)
    tool: str = Field(min_length=1, max_length=64)
    args: dict[str, Any] = Field(default_factory=dict)
    requested_by: str | None = Field(default=None, max_length=600)
    turn_cost_usd: float | None = None
    runtime_preflight: str | None = Field(default=None, max_length=2000)


class OperatorAct(BaseModel):
    """Confirm / cancel / revert bodies — optional, closed."""

    model_config = ConfigDict(extra="forbid")

    session_id: str | None = None


def receipt_line(result: Mapping[str, Any]) -> str:
    """The one-line, server-composed account of what an operator act did."""
    state = result.get("status")
    wid = str(result.get("write_id") or "")
    if state == "executed":
        r = result.get("receipt") or {}
        pre, post = r.get("pre") or {}, r.get("post") or {}
        move = (
            f"{(pre.get('version') or '')[:8]}→{(post.get('version') or '')[:8]}"
            if pre else f"created {(post.get('version') or '')[:8]}"
        )
        if pre and pre.get("state") != post.get("state"):
            move += f" ({pre.get('state')}→{post.get('state')})"
        cost = r.get("turn_cost_usd_at_write")
        return (
            f"Written · {r.get('class')} {r.get('object')} {r.get('verb')} {move} · "
            f"{r.get('plane') or '—'} · "
            f"{'$%.2f' % cost if isinstance(cost, (int, float)) else 'cost not measured'} · "
            f"activation pending · w-{wid}"
        )
    cause = result.get("cause")
    return f"Not written · {cause + ': ' if cause else ''}{result.get('detail') or state} · w-{wid}"


def _grants(body: Mapping[str, Any] | None) -> bool:
    for ref in (body or {}).get("action_packs") or ():
        if isinstance(ref, Mapping) and ref.get("pack_id") == W.CONSULT_WRITE_PACK_ID:
            return True
    return False


class WritesFront:
    """The write plane's registry half: the armed runs and the executor."""

    def __init__(
        self,
        deps: Any,
        *,
        is_live: Callable[[str], bool],
        executor: ConsultWriteExecutor | None = None,
    ) -> None:
        self.deps = deps
        self.is_live = is_live
        self.executor = executor or executor_for(deps.descriptor_registry)
        self.armed: dict[str, Armed] = {}

    async def require_available(self, consult_row: Any) -> None:
        if not _grants(getattr(consult_row, "body", None)):
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=WRITES_UNAVAILABLE)
        try:
            await self.deps.descriptor_registry.get(W.CONSULT_WRITE_PACK_ID, family=Family.ACTION_PACK)
        except DescriptorNotFound as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=WRITES_UNAVAILABLE) from exc

    def arm(
        self, first_input: dict[str, Any], *, allow_writes: bool, request_id: str,
        session_id: str | None, user_turn_id: str | None, plane: str | None,
        model_component: str | None,
    ) -> None:
        """Thread the flag — and nothing at all when unarmed."""
        if not allow_writes:
            return
        if not session_id or not user_turn_id:
            # Fail closed (C-2a §3): a write needs a durable operator turn.
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="writes_unavailable: the operator turn could not be recorded; nothing ran.",
            )
        first_input["allow_writes"] = True
        for done in [rid for rid in self.armed if not self.is_live(rid)]:
            self.armed.pop(done, None)  # runs that ended no longer arm anything
        self.armed[request_id] = Armed(
            request_id=request_id, session_id=session_id, user_turn_id=user_turn_id,
            plane=plane, model_component=model_component,
        )

    async def on_stop(self, request_id: str, session_id: str | None) -> None:
        if request_id in self.armed:
            await self.executor.expire_for_request(request_id, session_id=session_id)

    async def intercept_typed_code(self, body: Any) -> dict[str, Any] | None:
        """``confirm XXXX`` as a whole message → execute the staged write, no model."""
        match = _TYPED_CODE.match(str(getattr(body, "question", "") or ""))
        session_id = getattr(body, "session_id", None)
        if match is None or not session_id:
            return None
        code = match.group(1).upper()
        pg = self.deps.descriptor_registry.pg
        row = await self.executor.find_by_code(session_id, code)
        if row is None:
            answer = (
                f"No pending change in this conversation matches {code} — it may have "
                "expired, been cancelled, or already run. Nothing was written."
            )
            result: dict[str, Any] = {"status": "not_found"}
        else:
            result = await self.executor.confirm(row["id"], via="typed_code", code=code, session_id=session_id)
            answer = receipt_line(result)
        user_text = f"Confirmed write w-{row['id']}" if row is not None else str(body.question).strip()
        await consult_persistence.append_turn(pg, session_id=session_id, role="user", content=user_text)
        entries = [_bag_entry(result)] if row is not None else []
        await consult_persistence.append_turn(
            pg, session_id=session_id, role="assistant", content=answer,
            response={**dict.fromkeys(consult_persistence.RESPONSE_BAG_KEYS), "writes": entries},
            usage={},
        )
        return {"answer": answer, "session_id": session_id, "writes": entries, "usage": {}}


def _bag_entry(result: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "write_id": result.get("write_id"),
        "status": result.get("status"),
        "cause": result.get("cause"),
        "summary": receipt_line(result) if result.get("write_id") else str(result.get("detail") or ""),
    }


def build_router(front: WritesFront) -> APIRouter:
    router = APIRouter(tags=["consult"])
    ex = front.executor

    @router.post("/consult/writes")
    async def submit_write(body: WriteCall, _principal: str = Depends(require_bearer)) -> dict[str, Any]:
        armed = front.armed.get(body.request_id)
        if armed is None or not front.is_live(body.request_id):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="writes_not_armed: no live armed consult run has that request id."
                + _workers.run_miss_hint(),
            )
        try:
            return await ex.submit(
                armed, tool=body.tool, args=body.args, requested_by=body.requested_by,
                turn_cost_usd=body.turn_cost_usd, runtime_preflight=body.runtime_preflight,
            )
        except W.WriteKindNotBuilt as exc:
            raise HTTPException(
                status_code=status.HTTP_501_NOT_IMPLEMENTED,
                detail={"error": "kind_not_built", "seam": exc.seam, "detail": str(exc)},
            ) from exc

    @router.post("/consult/writes/{write_id}/confirm")
    async def confirm_write(
        write_id: str, body: OperatorAct | None = None, _principal: str = Depends(require_bearer),
    ) -> dict[str, Any]:
        result = await ex.confirm(write_id, via="click", session_id=body.session_id if body else None)
        if result.get("status") == "not_found":
            raise HTTPException(status_code=404, detail=f"no consult write {write_id!r}")
        return {**result, "line": receipt_line(result)}

    @router.post("/consult/writes/{write_id}/cancel")
    async def cancel_write(
        write_id: str, body: OperatorAct | None = None, _principal: str = Depends(require_bearer),
    ) -> dict[str, Any]:
        result = await ex.cancel(write_id, session_id=body.session_id if body else None)
        if result.get("status") == "not_found":
            raise HTTPException(status_code=404, detail=f"no consult write {write_id!r}")
        return result

    @router.post("/consult/writes/{write_id}/revert")
    async def revert_write(
        write_id: str, body: OperatorAct | None = None, _principal: str = Depends(require_bearer),
    ) -> dict[str, Any]:
        result = await ex.revert_button(write_id, session_id=body.session_id if body else None)
        if result.get("status") == "not_found":
            raise HTTPException(status_code=404, detail=f"no consult write {write_id!r}")
        return {**result, "line": receipt_line(result)}

    @router.get("/consult/writes")
    async def list_writes(
        session_id: str | None = None,
        request_id: str | None = None,
        cls: str | None = Query(default=None, alias="class"),
        object_ref: str | None = Query(default=None, alias="object"),
        since: datetime | None = None,
        limit: int = 200,
        all_sessions: bool = Query(default=False, alias="all"),
        principal: str = Depends(require_bearer),
    ) -> dict[str, Any]:
        """The ledger, scoped (W5-F8).

        A read names its scope: ``session_id`` (the caller's own conversation) or
        ``request_id`` (one run). The whole ledger across sessions is ``?all=1``
        and only an EXPLICIT principal may ask for it (the registry has one
        bearer today, so "explicit" means a presented credential, not dev mode's
        ``anonymous``). A pending row carries its typed confirm code ONLY on a
        scoped read; the whole-ledger read never carries a code (nor a hash).
        """
        scoped = bool(session_id or request_id)
        if not scoped:
            if not all_sessions:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="scope_required: pass session_id or request_id, or all=1 for the whole ledger.",
                )
            if principal == ANONYMOUS_PRINCIPAL:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="all=1 needs an explicit operator principal (a presented bearer).",
                )
        rows = await ex.list(
            session_id=session_id, request_id=request_id, cls=cls,
            object_ref=object_ref, since=since, limit=limit,
        )
        for row in rows:
            row.pop("confirm_code_hash", None)
            if not scoped:
                row.pop("confirm_code", None)
        return {"writes": rows, "count": len(rows)}

    return router
