# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Deciding a forecast's outcome from the console (F-1) — the write routes.

The resolver job only PROPOSES (``forecast_resolution.run_resolution_job``);
nothing closes a forecast until a person confirms. Until now that person had
to be on the host (``scripts/resolve_forecasts.py confirm``). These routes do
EXACTLY what the host command does, through the same store functions — no
second implementation:

  ``POST /v3/ledger/resolutions/{record_id}/confirm``
      accept the proposal's yes | no | void (``forecast_resolution.
      confirm_proposal``). The body repeats the outcome the person read; a
      proposal whose reading differs, or that a newer proposal superseded, is
      refused (409). Confirming ``void`` needs a reason (the void rule the
      person applies).
  ``POST /v3/ledger/resolutions/{record_id}/reject``
      reject the proposal with a reason (``reject_proposal``): nothing closes,
      the question stays open for the resolver, the rejected reading can no
      longer be confirmed.
  ``POST /v3/ledger/resolutions/{record_id}/judgement``
      decide what the rule cannot (a ``needs_judgement`` proposal) with a
      reason (``override_resolution`` with the proposal as its basis).
  ``POST /v3/ledger/resolutions/{record_id}/correct``
      a confirmation / override / correction (``record_id``, the act on
      record) is wrong: a NEW correction record supersedes it with a reason
      (``correct_resolution``); both are kept, nothing is edited. Admins and
      the owner only.
  ``GET  /v3/ledger/resolution-queue``
      what waits on a person, for THIS caller: a contributor sees only the
      questions they may decide (and how many others are hidden, and why).

WHO MAY DECIDE (enforced HERE, server-side, beside the route-role table):
admins and the owner decide any question; a contributor decides any question
EXCEPT one where they entered a number themselves (a ``forecast_numbers`` row
whose ``issuer_ref`` — or ``detail.entered_by`` — is their actor or user id);
readers and services never (a resolution judgement is a person's). With the
sign-in switch off there is one principal, the bearer's stable audit actor
(``operator:bearer:<sha12>`` — never the token), recorded as an admin.

Every act is one append-only resolution record (``actor`` and, migration
0262, ``actor_role``), written with a signed, chained audit-log row in the
same transaction (``namespace forecast_ledger``). Idempotent: repeating a
confirmation, rejection, judgement or correction returns the first one (200,
``already_recorded``). Refusals are ``{"error": <rule>, "message"}``.

Registry-slim: stdlib + fastapi + pydantic + the leaf ledger modules.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .. import forecast_ledger as fl
from .. import forecast_resolution as fr
from .api import RegistryAPIDeps, require_bearer
from .route_roles import RequestPrincipal, request_principal

logger = logging.getLogger(__name__)

FORECAST_RESOLUTION_ROUTE_VERSION = "2026-11/f1"
AUDIT_NAMESPACE = "forecast_ledger"

_UUID = r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
_REASON_MAX = 2000

#: Refusal rule → HTTP status.
_REFUSAL_STATUS: dict[str, int] = {
    "invalid": 422,
    "operator_required": 403,
    "unknown_question": 404,
    "needs_judgement": 409,
    "stale_proposal": 409,
    "stale_act": 409,
    "already_resolved": 409,
    "already_decided": 409,
    "proposal_rejected": 409,
    "before_due": 409,
    "early_without_evidence": 409,
    "unsealed": 409,
    "sealed": 409,
}

Outcome = Literal["yes", "no", "void"]


class ConfirmBody(BaseModel):
    #: The outcome the person read on the proposal — must still be its outcome.
    outcome: Outcome
    #: Required for ``void`` (the void rule applied, in words); optional otherwise.
    reason: Optional[str] = Field(default=None, max_length=_REASON_MAX)


class RejectBody(BaseModel):
    reason: str = Field(min_length=1, max_length=_REASON_MAX)


class JudgementBody(BaseModel):
    outcome: Outcome
    reason: str = Field(min_length=1, max_length=_REASON_MAX)
    #: The proposal's judgement keys this decides (default: all of them).
    judgements: list[str] = Field(default_factory=list, max_length=20)


class CorrectBody(BaseModel):
    outcome: Outcome
    reason: str = Field(min_length=1, max_length=_REASON_MAX)


@dataclass(frozen=True)
class Decider:
    """The person acting and the role an act records."""

    actor: str
    role: str  # owner | admin | contributor
    identities: tuple[str, ...]

    @property
    def is_admin(self) -> bool:
        return self.role in ("owner", "admin")


def _refuse(code: int, error: str, message: str) -> HTTPException:
    return HTTPException(status_code=code, detail={"error": error, "message": message})


def decider_for(rp: RequestPrincipal) -> Decider:
    """Who may act, as which role — or a 403 naming why not."""
    actor = (rp.actor or "").strip()
    if rp.kind == "service" or actor.startswith("service:"):
        raise _refuse(403, "person_required",
                      "a forecast's outcome is decided by a person, never by a service credential")
    role = (rp.role or "").strip()
    if role in ("admin", "operator"):
        product = "owner" if rp.is_owner else "admin"
    elif role == "contributor":
        product = "contributor"
    else:
        raise _refuse(403, "role_forbidden",
                      f"deciding a forecast's outcome needs the contributor role or above; yours is {role!r}")
    if not fr.is_person_actor(actor):
        raise _refuse(403, "person_required",
                      "this credential does not name a person, so it cannot decide a forecast's outcome")
    ids = [actor]
    if rp.user_id:
        ids += [str(rp.user_id), f"user:{rp.user_id}"]
    return Decider(actor=actor, role=product, identities=tuple(dict.fromkeys(ids)))


async def _may_decide(conn: Any, who: Decider, question_id: str) -> None:
    if who.is_admin:
        return
    if str(question_id) in await fr.entered_number_questions(conn, [str(question_id)], who.identities):
        raise _refuse(403, "entered_a_number",
                      "you entered a number on this question, so its outcome is decided by an admin or the owner")


def _audit_logger(request: Request, deps: RegistryAPIDeps) -> Any:
    d = getattr(request.app.state, "registry_deps", deps)
    logger_ = getattr(d, "audit_logger", None)
    if logger_ is None:
        raise _refuse(503, "audit_unavailable",
                      "the audit log is not configured, so no consequential write is taken")
    return logger_


async def _audit(audit: Any, conn: Any, who: Decider, *, action: str, slug: str,
                 basis: str | None, record_id: str, summary: dict[str, Any]) -> None:
    await audit.record(
        conn, actor_id=who.actor, namespace=AUDIT_NAMESPACE, descriptor_id=slug,
        action=action, from_version=basis, to_version=record_id,
        change_summary={**summary, "actor_role": who.role},
    )


def _done(code: int, body: dict[str, Any]) -> JSONResponse:
    return JSONResponse(status_code=code, content={"version": FORECAST_RESOLUTION_ROUTE_VERSION, **body})


async def _question(conn: Any, question_id: str) -> Any:
    return await fl._question(conn, question_id)  # noqa: SLF001 — the store's own read


async def _record(conn: Any, record_id: str) -> Any:
    rec = await conn.fetchrow(fr.RECORD_BY_ID_SQL, record_id)
    if rec is None:
        raise _refuse(404, "unknown_record", f"no resolution record {record_id}")
    return rec


def build_forecast_resolution_router(deps: RegistryAPIDeps) -> APIRouter:
    """Build the router (mounted at ``/api/v1/v3``)."""
    router = APIRouter(tags=["ledger"])

    def _pool(request: Request) -> Any:
        return getattr(request.app.state, "registry_deps", deps).descriptor_registry.pg

    async def _run(request: Request, fn: Any) -> JSONResponse:
        try:
            async with _pool(request).acquire() as conn:
                return await fn(conn)
        except HTTPException:
            raise
        except fl.LedgerRefusal as exc:
            raise _refuse(_REFUSAL_STATUS.get(exc.code, 409), exc.code, str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 — the record unreadable / unwritable
            logger.warning("v3.ledger.resolution_write.failed err_class=%s", type(exc).__name__)
            raise _refuse(503, "record_unavailable",
                          f"the resolution record could not be written ({type(exc).__name__}); nothing was changed") from exc

    rid = Path(..., pattern=_UUID)

    @router.post("/ledger/resolutions/{record_id}/confirm")
    async def confirm_resolution(
        request: Request, body: ConfirmBody, record_id: str = rid,
        _bearer: str = Depends(require_bearer),
        rp: RequestPrincipal = Depends(request_principal),
    ) -> JSONResponse:
        who = decider_for(rp)
        audit = _audit_logger(request, deps)
        record_id = record_id.lower()

        async def go(conn: Any) -> JSONResponse:
            rec = await _record(conn, record_id)
            if rec["record_kind"] != "proposal":
                raise _refuse(409, "not_a_proposal", f"{record_id} is a {rec['record_kind']}, not a proposal")
            await _may_decide(conn, who, rec["question_id"])
            q = await _question(conn, rec["question_id"])
            for act in await fr.acts_on_basis(conn, record_id):
                if act["record_kind"] == "confirmation":
                    return _done(200, {"status": "already_recorded", "record_id": act["id"],
                                       "record_kind": "confirmation", "outcome": act["outcome"],
                                       "question_id": rec["question_id"], "slug": q["slug"]})
            if rec["outcome"] != body.outcome:
                raise _refuse(409, "stale_proposal",
                              f"the proposal reads {rec['outcome']!r}, not {body.outcome!r}; read it again")
            reason = (body.reason or "").strip() or None
            if body.outcome == "void" and not reason:
                raise _refuse(422, "reason_required", "confirming void needs the reason: which void rule applies")
            async with conn.transaction():
                cid = await fr.confirm_proposal(conn, record_id, actor=who.actor, note=reason,
                                                actor_role=who.role)
                await _audit(audit, conn, who, action="forecast_resolution_confirm", slug=q["slug"],
                             basis=record_id, record_id=cid,
                             summary={"record_kind": "confirmation", "outcome": body.outcome,
                                      "question_id": rec["question_id"], "reason": reason})
            return _done(201, {"status": "confirmed", "record_id": cid, "record_kind": "confirmation",
                               "outcome": body.outcome, "question_id": rec["question_id"],
                               "slug": q["slug"], "actor_role": who.role,
                               "question_status": "void" if body.outcome == "void" else "resolved"})

        return await _run(request, go)

    @router.post("/ledger/resolutions/{record_id}/reject")
    async def reject_resolution(
        request: Request, body: RejectBody, record_id: str = rid,
        _bearer: str = Depends(require_bearer),
        rp: RequestPrincipal = Depends(request_principal),
    ) -> JSONResponse:
        who = decider_for(rp)
        audit = _audit_logger(request, deps)
        record_id = record_id.lower()
        reason = body.reason.strip()
        if not reason:
            raise _refuse(422, "reason_required", "a rejection says why")

        async def go(conn: Any) -> JSONResponse:
            rec = await _record(conn, record_id)
            if rec["record_kind"] != "proposal":
                raise _refuse(409, "not_a_proposal", f"{record_id} is a {rec['record_kind']}, not a proposal")
            await _may_decide(conn, who, rec["question_id"])
            q = await _question(conn, rec["question_id"])
            async with conn.transaction():
                rej, new = await fr.reject_proposal(conn, record_id, actor=who.actor, reason=reason,
                                                    actor_role=who.role)
                if new:
                    await _audit(audit, conn, who, action="forecast_resolution_reject", slug=q["slug"],
                                 basis=record_id, record_id=rej,
                                 summary={"record_kind": "rejection", "outcome": rec["outcome"],
                                          "question_id": rec["question_id"], "reason": reason})
            return _done(201 if new else 200, {
                "status": "rejected" if new else "already_recorded", "record_id": rej,
                "record_kind": "rejection", "outcome": rec["outcome"],
                "question_id": rec["question_id"], "slug": q["slug"], "actor_role": who.role,
                "question_status": "open"})

        return await _run(request, go)

    @router.post("/ledger/resolutions/{record_id}/judgement")
    async def judge_resolution(
        request: Request, body: JudgementBody, record_id: str = rid,
        _bearer: str = Depends(require_bearer),
        rp: RequestPrincipal = Depends(request_principal),
    ) -> JSONResponse:
        who = decider_for(rp)
        audit = _audit_logger(request, deps)
        record_id = record_id.lower()

        async def go(conn: Any) -> JSONResponse:
            rec = await _record(conn, record_id)
            if rec["record_kind"] != "proposal":
                raise _refuse(409, "not_a_proposal", f"{record_id} is a {rec['record_kind']}, not a proposal")
            await _may_decide(conn, who, rec["question_id"])
            q = await _question(conn, rec["question_id"])
            for act in await fr.acts_on_basis(conn, record_id):
                if act["record_kind"] == "override" and act["outcome"] == body.outcome:
                    return _done(200, {"status": "already_recorded", "record_id": act["id"],
                                       "record_kind": "override", "outcome": act["outcome"],
                                       "question_id": rec["question_id"], "slug": q["slug"]})
                raise _refuse(409, "already_decided", f"proposal {record_id} was answered by {act['id']}")
            newest = await conn.fetchrow(fr.LATEST_RECORD_SQL, rec["question_id"], ["proposal"])
            if newest is not None and str(newest["id"]) != record_id:
                raise _refuse(409, "stale_proposal", f"{record_id} is superseded by {newest['id']}")
            if rec["outcome"] != "needs_judgement":
                raise _refuse(409, "no_judgement_needed",
                              f"the proposal reads {rec['outcome']!r}: confirm or reject it instead")
            keys = [str(j.get("key")) for j in fr._loads(rec["judgements"], []) if isinstance(j, dict)]
            chosen = [k for k in (body.judgements or keys) if k]
            unknown = sorted(set(chosen) - set(keys))
            if unknown:
                raise _refuse(422, "unknown_judgement", f"the proposal waits on no judgement {unknown}")
            async with conn.transaction():
                oid = await fr.override_resolution(
                    conn, rec["question_id"], outcome=body.outcome, actor=who.actor,
                    reason=body.reason.strip(), judgements=chosen, basis_record_id=record_id,
                    actor_role=who.role)
                await _audit(audit, conn, who, action="forecast_resolution_judgement", slug=q["slug"],
                             basis=record_id, record_id=oid,
                             summary={"record_kind": "override", "outcome": body.outcome,
                                      "question_id": rec["question_id"], "judgements": chosen,
                                      "reason": body.reason.strip()})
            return _done(201, {"status": "decided", "record_id": oid, "record_kind": "override",
                               "outcome": body.outcome, "question_id": rec["question_id"],
                               "slug": q["slug"], "actor_role": who.role,
                               "question_status": "void" if body.outcome == "void" else "resolved"})

        return await _run(request, go)

    @router.post("/ledger/resolutions/{record_id}/correct")
    async def correct_resolution(
        request: Request, body: CorrectBody, record_id: str = rid,
        _bearer: str = Depends(require_bearer),
        rp: RequestPrincipal = Depends(request_principal),
    ) -> JSONResponse:
        who = decider_for(rp)
        if not who.is_admin:
            raise _refuse(403, "role_forbidden", "correcting a decided outcome is for an admin or the owner")
        audit = _audit_logger(request, deps)
        record_id = record_id.lower()

        async def go(conn: Any) -> JSONResponse:
            rec = await _record(conn, record_id)
            q = await _question(conn, rec["question_id"])
            for c in await fr.corrections_of(conn, record_id):
                if c["outcome"] == body.outcome:
                    return _done(200, {"status": "already_recorded", "record_id": c["id"],
                                       "record_kind": "correction", "outcome": c["outcome"],
                                       "question_id": rec["question_id"], "slug": q["slug"]})
            if rec["record_kind"] not in fr.ACT_KINDS:
                raise _refuse(409, "not_an_act",
                              f"{record_id} is a {rec['record_kind']}; only a decided outcome is corrected")
            async with conn.transaction():
                cid = await fr.correct_resolution(
                    conn, rec["question_id"], outcome=body.outcome, actor=who.actor,
                    reason=body.reason.strip(), actor_role=who.role, expected_prior_id=record_id)
                await _audit(audit, conn, who, action="forecast_resolution_correct", slug=q["slug"],
                             basis=record_id, record_id=cid,
                             summary={"record_kind": "correction", "outcome": body.outcome,
                                      "supersedes": record_id, "question_id": rec["question_id"],
                                      "reason": body.reason.strip()})
            return _done(201, {"status": "corrected", "record_id": cid, "record_kind": "correction",
                               "supersedes": record_id, "outcome": body.outcome,
                               "question_id": rec["question_id"], "slug": q["slug"],
                               "actor_role": who.role,
                               "question_status": "void" if body.outcome == "void" else "resolved"})

        return await _run(request, go)

    @router.get("/ledger/resolution-queue")
    async def resolution_queue(
        request: Request,
        limit: int = Query(default=100, ge=1, le=200),
        _bearer: str = Depends(require_bearer),
        rp: RequestPrincipal = Depends(request_principal),
    ) -> dict[str, Any]:
        """What waits on THIS person: open questions whose newest proposal no
        act answers, each with what the caller may do on it."""
        who = decider_for(rp)
        base: dict[str, Any] = {"version": FORECAST_RESOLUTION_ROUTE_VERSION,
                                "role": who.role, "may_correct": who.is_admin}
        try:
            async with _pool(request).acquire() as conn:
                items = await fr.resolution_queue(conn, limit=limit)
                mine = await fr.entered_number_questions(
                    conn, [i["question_id"] for i in items], who.identities)
        except Exception as exc:  # noqa: BLE001 — unreadable, said so, HTTP 200
            logger.info("v3.ledger.resolution_queue.unavailable err_class=%s", type(exc).__name__)
            return {**base, "available": False, "items": [], "hidden": 0}
        shown, hidden = [], 0
        for it in items:
            if not who.is_admin and it["question_id"] in mine:
                hidden += 1
                continue
            outcome = it["proposal"]["outcome"]
            it["may"] = {
                "confirm": outcome in fr.CLOSING_OUTCOMES,
                "reject": outcome in fr.CLOSING_OUTCOMES or outcome == "needs_judgement",
                "judgement": outcome == "needs_judgement",
            }
            shown.append(it)
        return {**base, "available": True, "items": shown, "hidden": hidden,
                "hidden_reason": ("questions where you entered a number: an admin or the owner decides those"
                                  if hidden else None)}

    return router
