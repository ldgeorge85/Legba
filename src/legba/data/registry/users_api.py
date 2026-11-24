# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``/api/v1/auth/*`` — invites, sign-in, sign-out, who-am-I, user admin (X3-B3).

The routes (every one in ``route_roles.ROUTE_ROLES`` with its class):

  * ``POST  /auth/invite``            operator — an ``invited`` user + a
    single-use token; the link is ``/#invite=<token>`` (a URL FRAGMENT, so the
    token never reaches an access log).
  * ``POST  /auth/accept``            public  — spend an invite: set the
    password, activate, sign in (sets the cookie).
  * ``POST  /auth/sign-in``           public  — email + password (the
    ``local_password`` provider), sets the cookie.
  * ``POST  /auth/sign-out``          public  — revokes the cookie's session,
    clears the cookie. Asked by the phone app (``X-Legba-Clear-Device:
    phone``, P-1), it also answers ``Clear-Site-Data: "storage"``.
  * ``GET   /auth/me``                read    — who the registry thinks is
    calling: a signed-in user, the operator's bearer (the perimeter), the
    fleet, or dev mode. The UI's sign-in gate and identity chip read it.
  * ``GET   /auth/users``             operator — every user.
  * ``PATCH /auth/users/{user_id}``   operator — role, status (``active`` /
    ``disabled``; disabling revokes every session), display name, provider,
    OIDC subject.

"Public" routes carry the ``read`` class in the table and authenticate
THEMSELVES (a password, an invite token, the cookie); they never call the
bearer gate. The cookie is ``HttpOnly; Secure; SameSite=Lax; Path=/`` and
host-only (no ``Domain``) — scoped to the origin that served the UI.

A refusal is the provider's :class:`legba.data.users.AuthRefused` as
``detail = {"error": <code>, "message": ...}`` with its status.

Registry-slim: imports the ``_deps`` kernel, the ``session_auth`` and
``route_roles`` leaves and ``legba.data.users``.
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from .. import users as _users
from . import route_roles as _rr
from . import session_auth as _sa
from ._deps import RegistryAPIDeps, is_service_kind, require_bearer
from .route_policy import signin as _signin

logger = logging.getLogger(__name__)

RoleName = Literal["read", "reader-own-rows", "operator"]


class InviteBody(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    role: RoleName = "read"
    display_name: str = Field(default="", max_length=200)


#: A-1 — the role names an invite / an edit takes with ``LEGBA_AUTH_SIGNIN=1``
#: (the two models below replace :class:`InviteBody` / :class:`UserPatch` on
#: the router built while the switch is on; off, the October models serve).
SigninRoleName = Literal["reader", "contributor", "admin"]


class InviteBodySignin(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    role: SigninRoleName = "reader"
    display_name: str = Field(default="", max_length=200)


class UserPatchSignin(BaseModel):
    role: SigninRoleName | None = None
    status: Literal["active", "disabled"] | None = None
    display_name: str | None = Field(default=None, max_length=200)


class AcceptBody(BaseModel):
    token: str = Field(min_length=8, max_length=256)
    password: str = Field(min_length=1, max_length=_users.PASSWORD_MAX_CHARS)
    display_name: str | None = Field(default=None, max_length=200)


class SignInBody(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=_users.PASSWORD_MAX_CHARS)


class UserPatch(BaseModel):
    role: RoleName | None = None
    status: Literal["active", "disabled"] | None = None
    display_name: str | None = Field(default=None, max_length=200)
    auth_provider: Literal["local_password", "oidc"] | None = None
    external_subject: str | None = Field(default=None, max_length=500)


def _refused(exc: _users.AuthRefused) -> HTTPException:
    return HTTPException(status_code=exc.status, detail=exc.as_detail())


def set_session_cookie(response: Response, session: _users.Session) -> None:
    """``Set-Cookie`` for a fresh session — HttpOnly, Secure, SameSite=Lax."""
    response.set_cookie(
        key=_sa.SESSION_COOKIE_NAME,
        value=session.token,
        max_age=int(_users.session_ttl().total_seconds()),
        path="/",
        secure=_sa.cookie_secure(),
        httponly=True,
        samesite="lax",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(
        key=_sa.SESSION_COOKIE_NAME, path="/", secure=_sa.cookie_secure(),
        httponly=True, samesite="lax",
    )


#: P-1 — the phone app asks for its device to be cleared on sign-out.
CLEAR_DEVICE_HEADER = "x-legba-clear-device"
#: What is cleared: the origin's storage (localStorage, IndexedDB, Cache
#: Storage — the phone app's shell cache — and, per the spec, its service
#: worker registrations). Not ``"cookies"``: the session cookie is already
#: expired by ``Set-Cookie`` above, and ``"cookies"`` reaches every subdomain
#: of the registrable domain. Not ``"cache"``: partial in Chrome, with
#: reported multi-second hangs.
CLEAR_SITE_DATA_VALUE = '"storage"'


def clear_site_data_for(request: Request) -> str | None:
    """``Clear-Site-Data`` for a sign-out, or ``None``.

    Only when the phone app asks (``X-Legba-Clear-Device: phone``): storage is
    per ORIGIN, and the desktop at ``/`` shares it — a desktop sign-out must
    not wipe the desktop's saved layouts.
    """
    asked = (request.headers.get(CLEAR_DEVICE_HEADER) or "").strip().lower()
    return CLEAR_SITE_DATA_VALUE if asked == "phone" else None


def identity_payload(request: Request, actor: str) -> dict[str, Any]:
    """``GET /auth/me``'s body: who is calling and in which class."""
    if _signin.signin_enabled():
        return _identity_payload_signin(request, actor)
    user = _sa.session_user(request.state)
    principal = _rr.resolve_principal(request)
    if user is not None:
        kind = "user"
    else:
        stamped = getattr(request.state, "principal", None)
        kind = stamped if stamped in ("operator", "service") else "anonymous"
    return {
        "kind": kind,
        "actor": actor,
        "role": principal.role,
        "user": user,
        "session_rejected": bool(getattr(request.state, _sa.SESSION_REJECTED_ATTR, False)),
    }


def _identity_payload_signin(request: Request, actor: str) -> dict[str, Any]:
    """A-1 (switch on): ``kind`` is ``user`` / ``service`` / ``admin-token`` /
    ``dev``; ``role`` is reader / contributor / admin; ``signin`` says the
    console must carry a session; ``service`` names a service credential."""
    user = _sa.session_user(request.state)
    principal = _rr.resolve_principal(request)
    stamped = getattr(request.state, "principal", None)
    service: str | None = None
    if user is not None:
        kind = "user"
    elif is_service_kind(stamped):
        kind = "service"
        if isinstance(stamped, str) and stamped.startswith(_signin.SERVICE_ACTOR_PREFIX):
            service = stamped[len(_signin.SERVICE_ACTOR_PREFIX):]
    elif stamped == _signin.ADMIN_TOKEN_KIND:
        kind = _signin.ADMIN_TOKEN_KIND
    else:
        kind = "dev"
    return {
        "kind": kind,
        "actor": actor,
        "role": _signin.normalize_role(principal.role) or principal.role,
        "is_owner": bool(user and user.get("is_owner")),
        "user": user,
        "service": service,
        "signin": True,
        "session_rejected": bool(getattr(request.state, _sa.SESSION_REJECTED_ATTR, False)),
    }


async def _with_owner(conn: Any, user: _users.User) -> _users.User:
    """A-1: the user re-read with ``is_owner`` (sign-in responses)."""
    full = await _users.get_user_with_owner(conn, user.id)
    return full or user


def build_users_router(deps: RegistryAPIDeps) -> APIRouter:
    """Mount with ``app.include_router(build_users_router(deps), prefix="/api/v1")``."""
    router = APIRouter(tags=["auth"])
    pg = deps.descriptor_registry.pg
    # A-1: the switch is read ONCE here for the two body models (pydantic
    # validates a role against them before any handler runs); every other
    # switch read is per request.
    signin = _signin.signin_enabled()

    async def invite_user(body: Any, actor: str = Depends(require_bearer)) -> dict[str, Any]:
        """Invite one person (operator). The token is returned ONCE."""
        try:
            async with pg.acquire() as conn:
                invite = await _users.create_invite(
                    conn, email=body.email, role=body.role,
                    invited_by=actor, display_name=body.display_name,
                )
        except _users.AuthRefused as exc:
            raise _refused(exc) from exc
        logger.info(
            "auth.invite.created user=%s role=%s by=%s",
            invite.user.id, invite.role, actor,
        )
        return {
            "user": invite.user.public(),
            "role": invite.role,
            "invite_token": invite.token,
            "invite_path": f"/#invite={invite.token}",
            "expires_at": invite.expires_at,
        }

    invite_user.__annotations__["body"] = InviteBodySignin if signin else InviteBody
    router.post("/auth/invite", status_code=status.HTTP_201_CREATED)(invite_user)

    @router.post("/auth/accept")
    async def accept_invite(body: AcceptBody, response: Response) -> dict[str, Any]:
        """Spend an invite: set the password, activate, sign in."""
        try:
            async with pg.acquire() as conn:
                user = await _users.accept_invite(
                    conn, token=body.token, password=body.password,
                    display_name=body.display_name,
                )
                session = await _users.create_session(conn, user)
                shown = (
                    await _with_owner(conn, session.user)
                    if _signin.signin_enabled() else session.user
                )
        except _users.AuthRefused as exc:
            raise _refused(exc) from exc
        set_session_cookie(response, session)
        logger.info("auth.invite.accepted user=%s", user.id)
        return {"user": shown.public(), "expires_at": session.expires_at.isoformat()}

    @router.post("/auth/sign-in")
    async def sign_in(body: SignInBody, response: Response) -> dict[str, Any]:
        """Email + password (``local_password``) → a session cookie."""
        provider = _users.local_password_provider()
        try:
            async with pg.acquire() as conn:
                user = await provider.authenticate(
                    conn, {"email": body.email, "password": body.password},
                )
                session = await _users.create_session(conn, user)
                shown = (
                    await _with_owner(conn, session.user)
                    if _signin.signin_enabled() else session.user
                )
        except _users.AuthRefused as exc:
            logger.info("auth.sign_in.refused code=%s", exc.code)
            raise _refused(exc) from exc
        set_session_cookie(response, session)
        logger.info("auth.sign_in user=%s", user.id)
        return {"user": shown.public(), "expires_at": session.expires_at.isoformat()}

    @router.post("/auth/sign-out")
    async def sign_out(request: Request, response: Response) -> dict[str, Any]:
        """Revoke this browser's session and clear its cookie."""
        token = _sa.session_cookie_from_header(request.headers.get("cookie") or "")
        revoked = False
        if token:
            async with pg.acquire() as conn:
                revoked = await _users.revoke_session(conn, token)
        clear_session_cookie(response)
        clear_site = clear_site_data_for(request)
        if clear_site:
            response.headers["Clear-Site-Data"] = clear_site
        return {"signed_out": True, "session_revoked": revoked}

    @router.get("/auth/me")
    async def who_am_i(
        request: Request, actor: str = Depends(require_bearer),
    ) -> dict[str, Any]:
        """Who is calling: a signed-in user, the operator, the fleet, dev."""
        return identity_payload(request, actor)

    @router.get("/auth/users")
    async def list_users(
        _actor: str = Depends(require_bearer),
    ) -> dict[str, Any]:
        """Every user (operator)."""
        async with pg.acquire() as conn:
            rows = await _users.list_users(conn)
        return {"users": [u.public() for u in rows], "count": len(rows)}

    async def update_user(
        user_id: str, body: Any, actor: str = Depends(require_bearer),
    ) -> dict[str, Any]:
        """Change a user's role / status / name / provider / subject (operator)."""
        try:
            async with pg.acquire() as conn:
                user = await _users.update_user(
                    conn, user_id,
                    **body.model_dump(exclude_unset=True),
                )
        except _users.AuthRefused as exc:
            raise _refused(exc) from exc
        logger.info(
            "auth.user.updated user=%s fields=%s by=%s",
            user.id, sorted(body.model_dump(exclude_unset=True)), actor,
        )
        return {"user": user.public()}

    update_user.__annotations__["body"] = UserPatchSignin if signin else UserPatch
    router.patch("/auth/users/{user_id}")(update_user)

    return router


__all__ = [
    "AcceptBody",
    "InviteBody",
    "InviteBodySignin",
    "SignInBody",
    "UserPatch",
    "UserPatchSignin",
    "build_users_router",
    "clear_session_cookie",
    "identity_payload",
    "set_session_cookie",
]
