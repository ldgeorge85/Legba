# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``/api/v1/auth/admin/*`` — the console's Users page: accounts for admins.

The routes (each ``admin`` in ``route_policy.ROUTE_MIN_ROLES`` and
``operator`` in ``route_roles.ROUTE_ROLES``; the service credentials are
refused on all of them, ``_deps.HUMAN_ONLY_ROUTES``):

  * ``GET    /auth/admin/users``                     every account past its
    invite (name, email, role, owner flag, state, created, last sign-in, last
    seen, live sessions) and every pending invite (no token, no hash).
  * ``POST   /auth/admin/invites``                   invite one person as a
    role. The answer carries the link (``/#invite=<token>``) ONCE; only the
    token's sha256 is stored, it expires (``LEGBA_INVITE_TTL_HOURS``) and it
    works once (``legba.data.users.create_invite`` / ``accept_invite``).
  * ``DELETE /auth/admin/invites/{user_id}``         withdraw a pending invite.
  * ``POST   /auth/admin/users/{user_id}/role``      change the role.
  * ``POST   /auth/admin/users/{user_id}/disable``   disable; every session the
    account holds ends in the same transaction.
  * ``POST   /auth/admin/users/{user_id}/enable``    enable again.
  * ``POST   /auth/admin/users/{user_id}/end-sessions``  sign the account out
    everywhere.

The rules live in ``legba.data.users`` so the command line and every route
share them: the owner cannot be demoted or disabled (409 ``owner_protected``)
and no route touches the owner flag; no change may leave the instance without
an active admin (409 ``last_admin``). Every change writes a signed audit row
(``descriptor_audit_log``, namespace ``user``) naming who made it, in the same
transaction as the change.

With the sign-in switch off there are no individual accounts to manage: every
route answers 409 ``signin_not_enabled`` with a message that says how to turn
it on, and reads or writes nothing.

Registry-slim: fastapi, pydantic, the ``_deps`` kernel, the ``route_roles``
leaf and ``legba.data.users``. Never the ``api`` hub.
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field

from .. import users as _users
from . import route_roles as _rr
from ._deps import RegistryAPIDeps, require_bearer
from .route_policy import signin as _signin

logger = logging.getLogger(__name__)

#: The audit namespace of an account change (``descriptor_audit_log``).
AUDIT_NAMESPACE = "user"
AUDIT_ACTIONS = {
    "invite": "user_invite",
    "revoke_invite": "user_invite_revoke",
    "role": "user_role_change",
    "disable": "user_disable",
    "enable": "user_enable",
    "end_sessions": "user_sessions_end",
}

SIGNIN_NOT_ENABLED = {
    "error": "signin_not_enabled",
    "message": (
        "Sign-in is not enabled on this instance, so there are no individual "
        "accounts to manage: everyone shares one password. Set "
        "LEGBA_AUTH_SIGNIN=1 and create the owner on the host to turn it on "
        "(docs/OPERATING_YOUR_INSTANCE.md, 'Turning on sign-in')."
    ),
}

RoleName = Literal["reader", "contributor", "admin"]


class InviteIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(min_length=3, max_length=320)
    role: RoleName = "reader"
    display_name: str = Field(default="", max_length=200)


class RoleIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: RoleName


def _refused(exc: _users.AuthRefused) -> HTTPException:
    return HTTPException(status_code=exc.status, detail=exc.as_detail())


def _require_admin_and_signin(request: Request) -> _rr.RequestPrincipal:
    """The route gate already refused anyone below admin; this says so again
    at the handler (a router mounted without the gate stays closed), then
    refuses every call while the sign-in switch is off."""
    principal = _rr.resolve_principal(request)
    if not principal.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": "role_forbidden",
                "required_role": _signin.ROLE_ADMIN,
                "message": "managing accounts needs an admin",
            },
        )
    if not _signin.signin_enabled():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=SIGNIN_NOT_ENABLED)
    return _rr.request_principal(request)


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


def _refuse_invited(user: _users.User) -> None:
    if user.status == "invited":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "user_invited",
                "message": (
                    "this person has not accepted their invite yet; withdraw "
                    "the invite and send a new one instead"
                ),
            },
        )


def build_users_admin_router(deps: RegistryAPIDeps) -> APIRouter:
    """Mount with ``app.include_router(build_users_admin_router(deps), prefix="/api/v1")``."""
    router = APIRouter(tags=["auth-admin"])
    pg = deps.descriptor_registry.pg

    async def _audit(
        conn: Any, who: _rr.RequestPrincipal, *, action: str, user_id: str,
        summary: dict[str, Any],
    ) -> None:
        await deps.audit_logger.record(
            conn, actor_id=who.actor, actor_role=who.role,
            namespace=AUDIT_NAMESPACE, descriptor_id=str(user_id),
            action=AUDIT_ACTIONS[action],
            change_summary={**summary, "actor_kind": who.kind},
        )

    async def _load(conn: Any, user_id: str) -> _users.User:
        user = await _users.get_user_with_owner(conn, user_id)
        if user is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "user_unknown", "message": "no account with that id"},
            )
        return user

    @router.get("/auth/admin/users")
    async def list_accounts(
        request: Request, response: Response,
        actor: str = Depends(require_bearer),
    ) -> dict[str, Any]:
        """Every account and every pending invite (admin)."""
        who = _require_admin_and_signin(request)
        _no_store(response)
        async with pg.acquire() as conn:
            accounts = await _users.list_accounts(conn)
            invites = await _users.list_pending_invites(conn)
        return {
            "signin": True,
            "users": [a.public() for a in accounts],
            "invites": [i.public() for i in invites],
            "roles": list(_signin.ROLES),
            "you": who.user_id,
        }

    @router.post("/auth/admin/invites", status_code=status.HTTP_201_CREATED)
    async def invite(
        body: InviteIn, request: Request, response: Response,
        actor: str = Depends(require_bearer),
    ) -> dict[str, Any]:
        """Invite one person; the link is in this answer and nowhere else."""
        who = _require_admin_and_signin(request)
        _no_store(response)
        try:
            async with pg.acquire() as conn:
                async with conn.transaction():
                    made = await _users.create_invite(
                        conn, email=body.email, role=body.role,
                        invited_by=who.actor, display_name=body.display_name,
                    )
                    await _audit(conn, who, action="invite", user_id=made.user.id,
                                 summary={"role": made.role, "expires_at": made.expires_at})
        except _users.AuthRefused as exc:
            raise _refused(exc) from exc
        logger.info("auth.admin.invite user=%s role=%s by=%s", made.user.id, made.role, who.actor)
        return {
            "invite": {
                "user_id": made.user.id,
                "email": made.user.email,
                "display_name": made.user.display_name,
                "role": made.role,
                "invited_by": who.actor,
                "expires_at": made.expires_at,
            },
            "invite_path": f"/#invite={made.token}",
            "expires_at": made.expires_at,
        }

    @router.delete("/auth/admin/invites/{user_id}")
    async def revoke_invite(
        user_id: str, request: Request, actor: str = Depends(require_bearer),
    ) -> dict[str, Any]:
        """Withdraw a pending invite; its link stops working at once."""
        who = _require_admin_and_signin(request)
        try:
            async with pg.acquire() as conn:
                async with conn.transaction():
                    gone = await _users.revoke_invite(conn, user_id)
                    await _audit(conn, who, action="revoke_invite", user_id=gone.id,
                                 summary={"role": _signin.normalize_role(gone.role) or gone.role})
        except _users.AuthRefused as exc:
            raise _refused(exc) from exc
        logger.info("auth.admin.invite_revoked user=%s by=%s", gone.id, who.actor)
        return {"revoked": True, "user_id": gone.id}

    async def _change(
        request: Request, user_id: str, *, action: str,
        role: str | None = None, new_status: str | None = None,
    ) -> dict[str, Any]:
        who = _require_admin_and_signin(request)
        try:
            async with pg.acquire() as conn:
                async with conn.transaction():
                    before = await _load(conn, user_id)
                    _refuse_invited(before)
                    before_role = before.effective_role
                    if (role is not None and role == before_role) or (
                        new_status is not None and new_status == before.status
                    ):
                        return {"user": before.public(), "changed": False}
                    await _users.update_user(conn, before.id, role=role, status=new_status)
                    summary: dict[str, Any] = {}
                    if role is not None:
                        summary = {"from_role": before_role, "to_role": role}
                    if new_status is not None:
                        summary = {"from_status": before.status, "to_status": new_status}
                    await _audit(conn, who, action=action, user_id=before.id, summary=summary)
                    after = await _load(conn, before.id)
        except _users.AuthRefused as exc:
            raise _refused(exc) from exc
        logger.info("auth.admin.%s user=%s by=%s", action, after.id, who.actor)
        return {"user": after.public(), "changed": True}

    @router.post("/auth/admin/users/{user_id}/role")
    async def change_role(
        user_id: str, body: RoleIn, request: Request,
        actor: str = Depends(require_bearer),
    ) -> dict[str, Any]:
        """Change an account's role. The owner stays an admin."""
        return await _change(request, user_id, action="role", role=body.role)

    @router.post("/auth/admin/users/{user_id}/disable")
    async def disable(
        user_id: str, request: Request, actor: str = Depends(require_bearer),
    ) -> dict[str, Any]:
        """Disable an account; its sessions end in the same transaction."""
        return await _change(request, user_id, action="disable", new_status="disabled")

    @router.post("/auth/admin/users/{user_id}/enable")
    async def enable(
        user_id: str, request: Request, actor: str = Depends(require_bearer),
    ) -> dict[str, Any]:
        """Let a disabled account sign in again."""
        return await _change(request, user_id, action="enable", new_status="active")

    @router.post("/auth/admin/users/{user_id}/end-sessions")
    async def end_sessions(
        user_id: str, request: Request, actor: str = Depends(require_bearer),
    ) -> dict[str, Any]:
        """Sign an account out everywhere; it can sign in again."""
        who = _require_admin_and_signin(request)
        async with pg.acquire() as conn:
            async with conn.transaction():
                user = await _load(conn, user_id)
                _refuse_invited(user)
                ended = await _users.revoke_user_sessions(conn, user.id)
                await _audit(conn, who, action="end_sessions", user_id=user.id,
                             summary={"sessions_ended": ended})
        logger.info("auth.admin.end_sessions user=%s ended=%d by=%s", user.id, ended, who.actor)
        return {"user_id": user.id, "sessions_ended": ended}

    return router


__all__ = [
    "AUDIT_ACTIONS",
    "AUDIT_NAMESPACE",
    "InviteIn",
    "RoleIn",
    "SIGNIN_NOT_ENABLED",
    "build_users_admin_router",
]
