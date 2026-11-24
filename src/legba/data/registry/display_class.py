# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Who is asking, for the display policy — and the REST choke point that obeys it.

``data.display_policy`` is the RULE (what a reader may see). This module answers
the question the rule needs on the registry's own transports — is this caller
an OPERATOR or a READER — and applies the rule to every JSON body the registry
serves, in ONE place (:class:`DisplayPolicyMiddleware`, mounted by
``server.create_app``) rather than in each of ~110 GET handlers. A route added
tomorrow is covered the day it is mounted.

THE CLASS (:func:`class_for_principal`), in precedence order:

  1. a ROLE STAMP on the request state — B2's role gate (``role``: ``read`` /
     ``reader-own-rows`` → READER, ``operator`` → OPERATOR) or B1's principal
     kind (``principal_kind`` / ``principal``: ``service`` / ``operator`` →
     OPERATOR). A reader stamp wins over an operator one.
  2. the credential itself: the label of the configured registry token (the
     single admin/human token every caller presents until B3) → OPERATOR. Dev
     mode with no token configured → OPERATOR (the gate admits everyone; the
     local developer is the operator).
  3. anything else → READER. A credential this module cannot place fails
     CLOSED — the reader sees the projection, never the row.

Until B1/B2/B3 land, every credential the gate admits is the admin token, so
every caller is an OPERATOR and every response is byte-identical to before;
the reader path is live the moment a reader stamp or a non-admin credential
arrives. Slim-image safe: imports ``_deps`` (the auth kernel) and the leaf
policy only.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from fastapi import Request

from ..display_policy import OPERATOR, READER, display_payload
from ._deps import _current_token, _dev_mode, principal_label

#: Request-state attributes a role / principal-kind stamp may sit under, in
#: precedence order (B2's role first, then B1's principal kind). Read in
#: parallel with B1/B2 (same wave); the integration names the one they chose.
ROLE_STAMP_ATTRS = (
    # ``legba_role`` is the name B2's route gate reads for B3's user stamp
    # (merge follow-up, X2): one spelling for both gates.
    "legba_role",
    "role", "principal_kind", "principal_class", "principal_type", "principal",
)

#: B1's gates return the AUDIT ACTOR ``<principal>:bearer:<sha12>`` — both
#: principals are the operator class for display (the fleet must receive the
#: full registry JSON; merge follow-up, X2).
#: A-1 (sign-in switch on): the break-glass admin token's actor is
#: ``admin-token:bearer:<sha12>`` — a value the switch-off path never produces.
B1_PRINCIPAL_PREFIXES = ("operator:", "service:", "admin-token:")

#: Stamp values that mean an operator-class caller.
#: ``admin`` is the A-1 role name (sign-in switch on) — never stamped when off.
OPERATOR_STAMPS = frozenset({"operator", "service", "admin"})

#: Stamp values that mean a reader-class caller.
READER_STAMPS = frozenset({"read", "reader", "reader-own-rows"})


def _stamp(state: Any, attr: str) -> Any:
    if state is None:
        return None
    if isinstance(state, Mapping):
        return state.get(attr)
    try:
        return getattr(state, attr, None)
    except Exception:  # noqa: BLE001 — a State object raising is "no stamp"
        return None


def class_for_principal(label: str | None, state: Any = None) -> str:
    """The display class of an admitted caller (module docstring, steps 1-3).

    ``label`` is what the bearer gate returned (``principal_label(token)`` or
    ``anonymous``); ``state`` is the request / websocket state (a Starlette
    ``State`` or the raw ``scope["state"]`` mapping) or ``None``.
    """
    stamps = [_stamp(state, attr) for attr in ROLE_STAMP_ATTRS]
    if any(isinstance(s, str) and s in READER_STAMPS for s in stamps):
        return READER
    if any(isinstance(s, str) and s in OPERATOR_STAMPS for s in stamps):
        return OPERATOR
    configured = _current_token()
    if configured is None:
        return OPERATOR if _dev_mode() else READER
    if label is not None and label == principal_label(configured):
        return OPERATOR
    if isinstance(label, str) and label.startswith(B1_PRINCIPAL_PREFIXES):
        return OPERATOR
    return READER


def _stamped_label(state: Any) -> str | None:
    """The audit-actor label the bearer gate stamped (``principal_actor``,
    X2-B1) — the ONLY credential-derived value this module reads. The raw
    ``Authorization`` header is the gate's alone (the audit-actor guard)."""
    return _stamp(state, "principal_actor")


def class_for_state(state: Any) -> str:
    """:func:`class_for_principal` from the request / websocket state the
    bearer gate stamped. Unstamped (no gate ran) fails closed to reader
    unless the registry runs in dev mode."""
    return class_for_principal(_stamped_label(state), state)


def request_display_class(request: Request) -> str:
    """FastAPI dependency: the display class of this HTTP request."""
    return class_for_state(request.state)


def _header(headers: list[tuple[bytes, bytes]], name: bytes) -> str:
    for key, value in headers:
        if key.lower() == name:
            return value.decode("latin-1")
    return ""


#: What a reader receives when a JSON body cannot be parsed for projection —
#: withheld, never relayed unprojected (fail closed).
_UNPARSEABLE_BODY = json.dumps({
    "detail": "withheld by the display policy: the response body could not "
              "be projected for a reader",
}).encode("utf-8")


class DisplayPolicyMiddleware:
    """Pure-ASGI REST choke point: a READER's JSON responses pass through
    :func:`display_payload`; an operator's pass through untouched (not even
    buffered). Non-JSON bodies (``text/event-stream``, markdown, files) and
    websockets pass through — those transports apply the policy themselves
    (the SSE relay, the events socket, the export route)."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        start: dict[str, Any] | None = None
        chunks: list[bytes] = []
        buffering = False

        async def _send(message: Any) -> None:
            nonlocal start, buffering
            if message["type"] == "http.response.start":
                headers = list(message.get("headers") or [])
                if "application/json" not in _header(headers, b"content-type"):
                    await send(message)
                    return
                # The route has run by now: the bearer gate's stamp is on
                # scope["state"] (Starlette's request.state). Never the header.
                cls = class_for_state(scope.get("state"))
                if cls == OPERATOR:
                    await send(message)
                    return
                buffering = True
                start = dict(message)
                return
            if message["type"] == "http.response.body" and buffering:
                chunks.append(message.get("body", b""))
                if message.get("more_body"):
                    return
                try:
                    body = json.dumps(
                        display_payload(json.loads(b"".join(chunks)), READER),
                        ensure_ascii=False, allow_nan=False,
                        separators=(",", ":"), default=str,
                    ).encode("utf-8")
                except ValueError:
                    body = _UNPARSEABLE_BODY
                assert start is not None
                start["headers"] = [
                    (k, v) for k, v in (start.get("headers") or [])
                    if k.lower() != b"content-length"
                ] + [(b"content-length", str(len(body)).encode("latin-1"))]
                await send(start)
                await send({
                    "type": "http.response.body", "body": body, "more_body": False,
                })
                return
            await send(message)

        await self.app(scope, receive, _send)


__all__ = [
    "DisplayPolicyMiddleware",
    "OPERATOR_STAMPS",
    "READER",
    "READER_STAMPS",
    "ROLE_STAMP_ATTRS",
    "class_for_state",
    "class_for_principal",
    "request_display_class",
]
