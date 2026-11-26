# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The SESSION COOKIE → request-state stamp, and the own-sessions filter (X3-B3).

WHAT THIS IS. One pure-ASGI middleware, :class:`SessionCookieMiddleware`,
mounted ONCE by ``server.create_app``, just inside the bearer middleware
(``bearer_auth.BearerPrincipalMiddleware``, X4 GATE). For every HTTP request
and WebSocket upgrade that carries the ``legba_session`` cookie and NO bearer
credential (the bearer middleware's ``legba_bearer_presented`` flag) it
resolves the cookie (``legba.data.users.resolve_session`` — one indexed read)
and, for an active user, stamps the request state BEFORE any route dependency
runs:

  * ``legba_role``        — the user's role class (B2's gate reads it,
                            ``route_roles.resolve_principal``; B4's display
                            class reads it, ``display_class.class_for_state``);
  * ``legba_subject``     — the local user id (the gate's subject; own rows);
  * ``principal_actor``   — ``user:<id>``, the audit actor every write stamps
                            (``consult_sessions.principal``) — the bearer gate
                            returns it for a session request (``_deps``);
  * ``principal``         — ``"user"``;
  * ``legba_session_user``— the public user row (``GET /auth/me``).

A middleware, not a dependency, because B2's gate is an APP-LEVEL dependency
that runs before any route's own dependencies — the stamp must already be
there (the B2 seam: "it must do so in MIDDLEWARE"). The bearer path is
untouched: a request with ``Authorization: Bearer …`` is never resolved as a
session (the operator's curl and the fleet stay exactly as they were), and an
unknown / expired / revoked cookie stamps nothing, so the bearer gate answers
401 and the UI shows its sign-in screen.

THE OWN-SESSIONS FILTER. :func:`consult_owner_filter` is the
``consult_sessions.principal`` value a caller is confined to — ``None`` for
the operator's bearer (and dev mode), which sees every session as before;
``user:<id>`` for a signed-in user, whatever their role. Every consult read
that names a session (the history list, a session load, continuing one,
stop / synthesize / run status) goes through it
(:func:`require_consult_session_owner`).

REGISTRY-SLIM: fastapi/starlette, the stdlib, ``legba.data.users`` and the
``route_roles`` leaf. Never imports ``api``, ``_deps`` or a router module.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from typing import Any, Mapping
from urllib.parse import urlsplit

from fastapi import HTTPException, status
from starlette.requests import HTTPConnection

from .. import users as _users
from . import route_roles as _rr
from .route_policy import signin as _signin

logger = logging.getLogger(__name__)

#: The cookie the browser carries. Host-only (no ``Domain``), ``Path=/``.
SESSION_COOKIE_NAME = "legba_session"
#: Dev-only escape: drop ``Secure`` so a plain-http dev server can set it.
COOKIE_INSECURE_ENV = "LEGBA_SESSION_COOKIE_INSECURE"

#: request.state attribute holding the signed-in user's public row.
SESSION_USER_ATTR = "legba_session_user"
#: request.state attribute set when a cookie was presented but resolved to no
#: active session (expired, revoked, disabled user) — ``/auth/me`` says so.
SESSION_REJECTED_ATTR = "legba_session_rejected"
#: The ``principal`` value of a signed-in user (B1's are service / operator).
PRINCIPAL_USER = "user"


def cookie_secure() -> bool:
    return os.getenv(COOKIE_INSECURE_ENV, "").strip() != "1"


def _header(scope: Mapping[str, Any], name: bytes) -> str:
    for key, value in scope.get("headers") or ():
        if key.lower() == name:
            return value.decode("latin-1")
    return ""


def session_cookie_from_header(cookie_header: str) -> str | None:
    """The ``legba_session`` value from a ``Cookie`` header, or ``None``."""
    for part in cookie_header.split(";"):
        key, sep, value = part.strip().partition("=")
        if sep and key == SESSION_COOKIE_NAME and value:
            return value.strip().strip('"') or None
    return None


def _bearer_presented(scope: Mapping[str, Any]) -> bool:
    """Whether a bearer credential was presented — the flag the bearer
    middleware (outermost, X4 GATE) stamped. This module never reads the
    ``Authorization`` header itself: ``bearer_auth`` is the one place that
    does. With no bearer middleware mounted the flag is absent and
    the cookie is resolved; the route's bearer gate still prefers a Bearer."""
    return bool(_get(scope.get("state"), _rr.STATE_BEARER_PRESENTED_ATTR))


def stamp_session(state: Any, user: _users.User) -> None:
    """Write the five stamps (module docstring) onto ``state`` — a Starlette
    ``State`` or the raw ``scope["state"]`` dict."""
    stamps = {
        _rr.STATE_ROLE_ATTR: user.effective_role,
        _rr.STATE_SUBJECT_ATTR: user.id,
        "principal_actor": user.actor,
        "principal": PRINCIPAL_USER,
        SESSION_USER_ATTR: user.public(),
        _rr.STATE_AUTH_SOURCE_ATTR: _rr.AUTH_SOURCE_SESSION,
    }
    for key, value in stamps.items():
        if isinstance(state, dict):
            state[key] = value
        else:
            setattr(state, key, value)


def _get(state: Any, attr: str) -> Any:
    if state is None:
        return None
    if isinstance(state, Mapping):
        return state.get(attr)
    try:
        return getattr(state, attr, None)
    except Exception:  # noqa: BLE001 — a State raising is "no stamp"
        return None


def session_user(state: Any) -> dict[str, Any] | None:
    """The signed-in user's public row the middleware stamped, or ``None``."""
    user = _get(state, SESSION_USER_ATTR)
    return user if isinstance(user, dict) else None


def session_actor(state: Any, *, bearer_presented: bool = False) -> str | None:
    """``user:<id>`` when the request is a SESSION request (stamped, and no
    Bearer credential presented), else ``None``. The bearer gates call this;
    they say whether a Bearer was presented (they own the header)."""
    if bearer_presented:
        return None
    user = session_user(state)
    if user is None:
        return None
    actor = _get(state, "principal_actor")
    return actor if isinstance(actor, str) and actor.startswith(
        _users.USER_ACTOR_PREFIX) else _users.user_actor(user["id"])


def _pg_from_scope(scope: Mapping[str, Any]) -> Any:
    app = scope.get("app")
    deps = getattr(getattr(app, "state", None), "registry_deps", None)
    registry = getattr(deps, "descriptor_registry", None)
    return getattr(registry, "pg", None)


class SessionCookieMiddleware:
    """Resolve the session cookie and stamp the request state (docstring)."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") == "http" and _signin.signin_enabled():
            refusal = cross_site_write_refusal(scope)
            if refusal is not None:
                await _send_json(send, 403, refusal)
                return
        if scope.get("type") in ("http", "websocket"):
            token = session_cookie_from_header(_header(scope, b"cookie"))
            if token and not _bearer_presented(scope):
                state = scope.setdefault("state", {})
                user = await self._resolve(scope, token)
                if user is not None:
                    stamp_session(state, user)
                else:
                    state[SESSION_REJECTED_ATTR] = True
        await self.app(scope, receive, send)

    @staticmethod
    async def _resolve(scope: Mapping[str, Any], token: str) -> _users.User | None:
        pg = _pg_from_scope(scope)
        if pg is None:
            logger.warning("registry.session.no_store — cookie not resolved")
            return None
        try:
            async with pg.acquire() as conn:
                return await _users.resolve_session(conn, token)
        except Exception as exc:  # noqa: BLE001 — fail closed: no session
            logger.warning("registry.session.resolve_failed err=%s", exc)
            return None


# ---------------------------------------------------------------------------
# A-1 — the same-origin check on cookie writes (switch on)
# ---------------------------------------------------------------------------

#: Write methods the same-origin check covers.
_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
#: The credential routes: checked even before a cookie exists (sign-in CSRF).
AUTH_PATH_PREFIX = "/api/v1/auth/"
#: ``Sec-Fetch-Site`` values that are not cross-site.
_SAME_SITE_FETCH = frozenset({"same-origin", "none"})


def cross_site_write_refusal(scope: Mapping[str, Any]) -> dict[str, Any] | None:
    """The 403 body for a cross-site write a browser cookie would carry, else
    ``None`` (B2 section 5, gap 1 — ``SameSite=Lax`` alone lets same-site
    subdomains through).

    Checked: a write (POST/PUT/PATCH/DELETE) that carries the session cookie
    or targets ``/api/v1/auth/*``, and presents no bearer credential. It is
    refused when ``Sec-Fetch-Site`` says another site, or — without that
    header — when ``Origin`` names a host other than the request's ``Host``.
    A request with neither header is not a browser's and passes (a bearer
    client, curl); the route gate still decides who it is."""
    method = str(scope.get("method") or "").upper()
    if method not in _WRITE_METHODS:
        return None
    path = str(scope.get("path") or "")
    has_cookie = session_cookie_from_header(_header(scope, b"cookie")) is not None
    if not has_cookie and not path.startswith(AUTH_PATH_PREFIX):
        return None
    if _bearer_presented(scope):  # the bearer middleware's flag (it runs first)
        return None
    fetch_site = _header(scope, b"sec-fetch-site").strip().lower()
    origin = _header(scope, b"origin").strip()
    if fetch_site:
        if fetch_site in _SAME_SITE_FETCH:
            return None
        why = f"Sec-Fetch-Site: {fetch_site}"
    elif origin:
        host = _header(scope, b"host").strip().lower()
        origin_host = (urlsplit(origin).netloc or "").lower()
        if origin_host and origin_host == host:
            return None
        why = "Origin does not match Host"
    else:
        return None
    logger.info("registry.session.cross_site_refused method=%s path=%s", method, path)
    return {
        "detail": {
            "error": "cross_site_refused",
            "message": (
                f"{method} {path} was sent from another site ({why}); a "
                "signed-in browser may change things only from this console"
            ),
        },
    }


async def _send_json(send: Any, status_code: int, body: dict[str, Any]) -> None:
    payload = json.dumps(body).encode("utf-8")
    await send({
        "type": "http.response.start",
        "status": status_code,
        "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(payload)).encode("latin-1")),
        ],
    })
    await send({"type": "http.response.body", "body": payload})


# ---------------------------------------------------------------------------
# Own consult sessions
# ---------------------------------------------------------------------------


def consult_owner_filter(conn: HTTPConnection) -> str | None:
    """The ``consult_sessions.principal`` this caller is confined to.

    ``None`` = every session (the operator's bearer, the fleet, dev mode —
    unchanged). A signed-in user → ``user:<id>``, whatever their role. A
    non-operator principal WITHOUT a session (only the pytest role override
    can make one) → ``user:<subject>``: confined, never widened.
    """
    user = session_user(getattr(conn, "state", None))
    if user is not None:
        return _users.user_actor(user["id"])
    principal = _rr.resolve_principal(conn)
    if principal.is_operator:
        return None
    return _users.user_actor(principal.subject)


async def require_consult_session_owner(
    pg: Any, conn: HTTPConnection, session_id: Any, *, what: str = "consult session",
) -> None:
    """Refuse (403 ``not_row_owner``) a confined caller naming a session that
    another principal opened. An unknown session passes (the caller's own
    404 path answers it); an unconfined caller always passes."""
    owner = consult_owner_filter(conn)
    if owner is None or not session_id:
        return
    try:
        sid = uuid.UUID(str(session_id))
    except ValueError:
        return  # not a session id at all; the caller's own path answers it
    try:
        async with pg.acquire() as db:
            found = await db.fetchrow(
                "SELECT principal FROM consult_sessions WHERE id = $1", sid,
            )
    except Exception as exc:  # noqa: BLE001 — fail closed
        logger.warning("registry.session.owner_check_failed err=%s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="could not check who owns this consult session",
        ) from exc
    if found is None:
        return
    if found["principal"] != owner:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": "not_row_owner",
                "message": f"{what} {session_id} belongs to another user",
            },
        )


def require_operator_for_writes(conn: HTTPConnection, *, armed: bool) -> None:
    """Refuse (403 ``role_forbidden``) an ARMED consult turn below the operator
    class. ``POST /consult`` is a ``reader-own-rows`` route, but
    ``allow_writes`` arms the consult-write pack — substrate CRUD, whose own
    routes (``/consult/writes*``) are ``operator``; a reader must not reach
    them through the front door once users exist."""
    if not armed:
        return
    principal = _rr.resolve_principal(conn)
    if principal.is_operator:
        return
    if _signin.signin_enabled():
        # A-1: an armed turn reaches /consult/writes, an admin route.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": "role_forbidden",
                "required_role": _signin.ROLE_ADMIN,
                "principal_role": principal.role,
                "message": (
                    "allow_writes arms Consult changes to sources, targets, "
                    "units, entities and facts, which require role 'admin'; "
                    f"the caller's role is {principal.role!r}"
                ),
            },
        )
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail={
            "error": "role_forbidden",
            "required_role": _rr.ROLE_OPERATOR,
            "principal_role": principal.role,
            "message": (
                "allow_writes arms consult writes (substrate changes), which "
                f"require role {_rr.ROLE_OPERATOR!r}; the caller's role is "
                f"{principal.role!r}"
            ),
        },
    )


__all__ = [
    "COOKIE_INSECURE_ENV",
    "PRINCIPAL_USER",
    "SESSION_COOKIE_NAME",
    "SESSION_REJECTED_ATTR",
    "SESSION_USER_ATTR",
    "SessionCookieMiddleware",
    "consult_owner_filter",
    "cookie_secure",
    "cross_site_write_refusal",
    "require_consult_session_owner",
    "require_operator_for_writes",
    "session_actor",
    "session_cookie_from_header",
    "session_user",
    "stamp_session",
]
