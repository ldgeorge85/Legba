# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The BEARER credential → request-state stamp, before the route-role gate (X4 GATE).

WHY (W11-F75, HIGH). X2-B2's route-role gate is an APP-LEVEL dependency: it
runs before every route's own dependencies. X2-B1's bearer stamp landed INSIDE
``require_bearer`` — a ROUTE dependency — so at the gate every bearer caller
was still unstamped, and the gate's unstamped default was the OPERATOR class.
A route that forgot ``Depends(require_bearer)`` was therefore open at the gate
as operator. X3-B3's session stamp was already a middleware and was not
affected.

WHAT THIS IS. One pure-ASGI middleware, :class:`BearerPrincipalMiddleware`,
mounted ONCE by ``server.create_app`` as the OUTERMOST layer (added after the
session middleware, so it runs first). For every HTTP request and WebSocket
upgrade it resolves the bearer credential through the SAME functions the gates
use (``_deps.resolve_bearer_header`` / ``_deps.resolve_ws_credential`` — one
constant-time compare, one answer) and either

  * STAMPS the state (``_deps.stamp_bearer``): ``principal`` (``operator`` |
    ``service``), ``principal_actor`` (the hashed audit actor),
    ``legba_role`` = ``operator`` + ``legba_subject`` — or the dev-mode
    operator stamp when no token is configured and ``LEGBA_DEV_MODE=1``; or
  * RECORDS the refusal the gate would have raised (``legba_auth_refusal``:
    401 no credential, 403 a bad one or the service token on a human-only
    route, 503 misconfigured) and stamps nothing.

Either way it records whether ANY bearer credential was presented
(``legba_bearer_presented``) — the session middleware, which runs next,
resolves a cookie only when none was. The route-role gate then admits a
stamped caller by class and REFUSES an unstamped one (the recorded refusal, or
``403 role_unresolved``) on every non-public route.

THE CREDENTIAL CARRIERS. ``Authorization: Bearer`` on every request; on a
WebSocket upgrade also the ``legba.bearer.v1`` subprotocol and the deprecated
``?token=``; on the consult SSE relay (:data:`QUERY_TOKEN_HTTP_PREFIXES`) the
``?token=`` an ``EventSource`` must use, since it can set no header. Nowhere
else is a query-string credential read.

THE RAW HEADER. This module is the ONE place the registry app reads the
``Authorization`` header of a request (the audit-actor guard,
``tests/data_pkg/test_audit_actor_never_token.py``, allow-lists it beside the
``_deps`` kernel). It hands the value to the ``_deps`` resolvers and keeps
nothing: the state holds the hashed actor, never the credential.

REGISTRY-SLIM: the stdlib, fastapi's ``HTTPException`` and the ``_deps`` /
``route_roles`` leaves. Never imports ``api`` or a router module.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping
from urllib.parse import parse_qs

from fastapi import HTTPException

from . import route_roles as _rr
from ._deps import (
    ANONYMOUS_PRINCIPAL,
    _offers_bearer_subprotocol,
    _configured_principals,
    resolve_bearer_header,
    resolve_ws_credential,
    stamp_bearer,
)

logger = logging.getLogger(__name__)

#: HTTP path prefixes whose ``?token=`` is a credential: the consult SSE relay
#: (``consult_stream_api``) — ``EventSource`` can set neither a header nor a
#: subprotocol. Everywhere else a query-string token is ignored.
QUERY_TOKEN_HTTP_PREFIXES: tuple[str, ...] = ("/api/v1/consult/stream/",)


def _header(scope: Mapping[str, Any], name: bytes) -> str | None:
    for key, value in scope.get("headers") or ():
        if key.lower() == name:
            return value.decode("latin-1")
    return None


def _query_token(scope: Mapping[str, Any]) -> str | None:
    raw = scope.get("query_string") or b""
    values = parse_qs(raw.decode("latin-1")).get("token")
    return (values[0] or None) if values else None


def _record_refusal(state: dict[str, Any], exc: HTTPException) -> None:
    state[_rr.STATE_AUTH_REFUSAL_ATTR] = {
        "status_code": exc.status_code, "detail": exc.detail,
    }


def resolve_scope(scope: Mapping[str, Any], state: dict[str, Any]) -> None:
    """Resolve the bearer credential of ``scope`` and stamp / record on ``state``.

    The middleware's whole decision, callable on its own (the tree test drives
    it per route).
    """
    authorization = _header(scope, b"authorization")
    method = str(scope.get("method") or "GET")
    path = str(scope.get("path") or "")
    is_ws = scope.get("type") == "websocket"
    if is_ws or path.startswith(QUERY_TOKEN_HTTP_PREFIXES):
        token = _query_token(scope)
        protocol = _header(scope, b"sec-websocket-protocol") if is_ws else None
        state[_rr.STATE_BEARER_PRESENTED_ATTR] = bool(
            token
            or (authorization or "").lower().startswith("bearer ")
            or _offers_bearer_subprotocol(protocol)
        )
        try:
            resolved = resolve_ws_credential(
                token, authorization, protocol,
                surface="websocket" if is_ws else "sse",
            )
        except HTTPException as exc:
            _record_refusal(state, exc)
            return
        if resolved is None:
            # No credential: dev mode is the operator; otherwise the WS/SSE
            # gate's own answer — unless the session middleware, next, stamps
            # a signed-in user.
            if _configured_principals():
                _record_refusal(state, HTTPException(status_code=403, detail="invalid token"))
                return
            stamp_bearer(state, None, ANONYMOUS_PRINCIPAL)
            return
        stamp_bearer(state, *resolved)
        return
    state[_rr.STATE_BEARER_PRESENTED_ATTR] = (authorization or "").lower().startswith(
        "bearer "
    )
    try:
        principal, actor = resolve_bearer_header(authorization, method=method, path=path)
    except HTTPException as exc:
        _record_refusal(state, exc)
        return
    stamp_bearer(state, principal, actor)


class BearerPrincipalMiddleware:
    """Resolve the bearer credential and stamp the request state (docstring)."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") in ("http", "websocket"):
            resolve_scope(scope, scope.setdefault("state", {}))
        await self.app(scope, receive, send)


__all__ = [
    "BearerPrincipalMiddleware",
    "QUERY_TOKEN_HTTP_PREFIXES",
    "resolve_scope",
]
