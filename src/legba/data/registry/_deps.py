# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The registry-API KERNEL — bearer auth, the deprecation stamp, the deps bundle.

Extracted from ``api.py`` (K-2, 2026-08-03), the phase-2A split in
``planning/CODE_CLEANUP_ANALYSIS_2026-08-02.md``. The measurement that motivated
it: **26 of the 50 modules in this package import ``api.py``**, and the *entire*
cross-module surface they reach for is five names — ``RegistryAPIDeps``,
``require_bearer``, ``sunset_headers``, ``_authorize_ws_token`` and
``build_router``. Four of those five are the ~200 lines gathered here; the fifth
(``build_router``) is the 1,400-line router factory that is the reason ``api.py``
is 2,500 lines. So a sibling router that wants nothing but the auth dependency
was pulling in the whole HTTP surface — every pydantic response model, every
route body, and a transitive edge to any module ``api.py`` grows an import of.
That is an import hub, and it is how circular imports get built by accident.

This module is a LEAF: it imports the six registry classes the deps bundle is
typed against (``descriptor``, ``stack``, ``credentials``, ``dlq``, ``audit``,
``vocabulary_cache``) plus fastapi and the stdlib — and nothing from ``api`` or
from any ``*_api`` router module. Nothing here may ever import ``api``; that
edge is what the extraction exists to remove.

WHAT LIVES HERE

  * the B-2 FAIL-CLOSED BEARER GATE — ``API_TOKEN_ENV`` / ``DEV_MODE_ENV``, the
    constant-time compare, ``require_bearer`` (HTTP) and ``_authorize_ws_token``
    (the WebSocket ``?token=`` + proxied-header path, ITEM 2.5). The two share
    ``_current_token`` / ``_dev_mode`` / ``_token_matches``, which is precisely
    why they must move as one unit: a half-move would leave two different
    answers to "is the gate configured?" in the process. Both return
    ``principal_actor(principal, token)`` — ``service:`` or ``operator:``
    before a hashed, NON-SECRET label — never the credential (W4 TOKEN-FIX;
    before it, every audit actor was the live token; X2-B1 split the one
    bearer into the fleet's SERVICE token and the operator's own).
  * the two RESOLVERS both gates are built on, ``resolve_bearer_header`` and
    ``resolve_ws_credential``, and the STAMP (``stamp_bearer``) — X4 GATE: the
    bearer middleware (``bearer_auth.BearerPrincipalMiddleware``) resolves
    every request through them BEFORE the route-role gate and stamps the
    state, so ``require_bearer`` / ``_authorize_ws_token`` read that stamp
    and only resolve the credential themselves on an app without the
    middleware (the unit-test stubs). One implementation, so the middleware's
    refusals are byte-for-byte the gates' own.
  * the HUMAN-ONLY ROUTE LIST (X2-B1) — ``HUMAN_ONLY_ROUTES``, the one place
    that says which routes refuse the service principal.
  * the C3 DEPRECATION STAMP — ``sunset_headers`` and its sunset date.
  * the DEPS BUNDLE — ``RegistryAPIDeps`` and the ``_get_deps`` request
    resolver every route depends on.

WHAT DELIBERATELY STAYS IN ``api.py`` — the router factory, every request /
response pydantic model, the row shapers, ``REQUIRED_MODEL_COMPONENT_KINDS``
(a first-run *config-status* contract, not an auth one), and the import-time
auth-posture log line, which stays put so it keeps emitting under the
``legba.data.registry.api`` logger exactly as operators' greps expect.

``api.py`` imports this module ONE WAY and RE-EXPORTS every name below, so all
26 importers — and every test reaching for ``api.API_TOKEN_ENV``,
``api.MISCONFIGURED_AUTH_DETAIL`` or ``api._authorize_ws_token`` — keep resolving
unchanged. Rewriting those call sites to import from here is a separate,
operator-gated step (K-5); this commit does not touch a single importer.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Callable

from fastapi import HTTPException, Header, Request, Response, status

from legba.shared import auth_signin as _signin
from legba.shared.registry_principal import (
    OPERATOR_TOKEN_ENV,
    PRINCIPAL_OPERATOR,
    PRINCIPAL_SERVICE,
    SERVICE_TOKEN_ENV,
)

from .audit import AuditLogger
from .credentials import CredentialVault
from .descriptor import DescriptorRegistry
from .dlq import DescriptorDeadLetter
from . import route_roles as _rr
from .session_auth import session_actor
from .stack import StackRegistry
from .vocabulary_cache import VocabularyCache

#: Auth-path warnings emit under the ``legba.data.registry.api`` logger NAME,
#: not this module's, so operators' existing greps for the registry auth
#: posture keep working even though the code lives here (module docstring).
logger = logging.getLogger("legba.data.registry.api")


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

API_TOKEN_ENV = OPERATOR_TOKEN_ENV  # "LEGBA_REGISTRY_API_TOKEN" — the operator's
DEV_MODE_ENV = "LEGBA_DEV_MODE"

# Returned on every guarded request when the gate is misconfigured (B-2).
MISCONFIGURED_AUTH_DETAIL = (
    f"registry API misconfigured: {API_TOKEN_ENV} and {SERVICE_TOKEN_ENV} are "
    f"unset/empty and {DEV_MODE_ENV}=1 is not set; refusing all guarded "
    f"requests. Set a bearer token, or set {DEV_MODE_ENV}=1 explicitly for "
    "local development."
)


def _current_token() -> str | None:
    """The OPERATOR token (``LEGBA_REGISTRY_API_TOKEN``), or ``None``."""
    raw = os.getenv(API_TOKEN_ENV, "").strip()
    return raw or None


def _current_service_token() -> str | None:
    """The FLEET token (``LEGBA_REGISTRY_SERVICE_TOKEN``), or ``None`` (X2-B1)."""
    raw = os.getenv(SERVICE_TOKEN_ENV, "").strip()
    return raw or None


def _configured_principals() -> list[tuple[str, str]]:
    """``[(principal, token), ...]`` for every configured credential.

    Operator first: when both keys are (wrongly) set to the same value the
    request is the operator's — the startup posture log says so loudly.

    A-1, sign-in switch ON: the admin token is the break-glass credential
    (kind ``admin-token``), each ``LEGBA_SERVICE_TOKEN_<NAME>`` is the kind
    ``service:<name>``, and the shared fleet token stays ``service``. OFF:
    the two-entry list it always was.
    """
    if _signin.signin_enabled():
        return _configured_principals_signin()
    out: list[tuple[str, str]] = []
    operator = _current_token()
    if operator is not None:
        out.append((PRINCIPAL_OPERATOR, operator))
    service = _current_service_token()
    if service is not None:
        out.append((PRINCIPAL_SERVICE, service))
    return out


def _configured_principals_signin() -> list[tuple[str, str]]:
    """The switch-on credential list (:func:`_configured_principals`)."""
    out: list[tuple[str, str]] = []
    admin = _current_token()
    if admin is not None:
        out.append((_signin.ADMIN_TOKEN_KIND, admin))
    for name, token in _signin.configured_service_tokens():
        out.append((f"{_signin.SERVICE_ACTOR_PREFIX}{name}", token))
    service = _current_service_token()
    if service is not None:
        out.append((PRINCIPAL_SERVICE, service))
    return out


def is_service_kind(principal: str | None) -> bool:
    """The shared fleet token (``service``) or a named one (``service:<name>``)."""
    return bool(principal) and (
        principal == PRINCIPAL_SERVICE
        or principal.startswith(_signin.SERVICE_ACTOR_PREFIX)
    )


#: Break-glass use of the admin token (switch on) logs at WARNING at most
#: once per this many seconds per process — a script loop cannot flood.
BREAK_GLASS_LOG_EVERY_S = 60.0
_break_glass_last = 0.0
_break_glass_count = 0


def _note_break_glass(method: str | None, path: str | None) -> None:
    global _break_glass_last, _break_glass_count
    _break_glass_count += 1
    now = time.monotonic()
    if now - _break_glass_last >= BREAK_GLASS_LOG_EVERY_S or _break_glass_count == 1:
        _break_glass_last = now
        logger.warning(
            "registry.auth.break_glass count=%d method=%s path=%s — the shared "
            "admin token (%s) was presented while %s=1; it is a break-glass "
            "credential now. Services should present their own "
            "LEGBA_SERVICE_TOKEN_<NAME>; people should sign in.",
            _break_glass_count, method or "-", path or "-", API_TOKEN_ENV,
            _signin.SIGNIN_ENV,
        )


def break_glass_count() -> int:
    """How many break-glass admin-token requests this process admitted."""
    return _break_glass_count


def _dev_mode() -> bool:
    """Explicit development-mode opt-in (`LEGBA_DEV_MODE=1`).

    Only consulted when NEITHER registry token is set — a configured token is
    ALWAYS enforced, dev flag or not.
    """
    return os.getenv(DEV_MODE_ENV, "").strip() == "1"


def _token_matches(presented: str, configured: str) -> bool:
    """Constant-time bearer comparison (B-2)."""
    return hmac.compare_digest(
        presented.encode("utf-8"), configured.encode("utf-8"),
    )


def _match_principal(presented: str, configured: list[tuple[str, str]]) -> str | None:
    """Which principal ``presented`` authenticates as, or ``None``.

    Compares against EVERY configured credential (no early exit), so the time
    taken does not say which one matched.
    """
    matched: str | None = None
    for kind, token in configured:
        if _token_matches(presented, token) and matched is None:
            matched = kind
    return matched


#: Prefix of the NON-SECRET principal label the gates return (W4 TOKEN-FIX).
PRINCIPAL_LABEL_PREFIX = "bearer:"

#: Hex characters of ``sha256(token)`` the label keeps. 12 hex = 48 bits: a
#: stable, distinct-enough handle for "which credential did this", useless for
#: recovering or brute-forcing a 64-hex secret.
PRINCIPAL_LABEL_HEX = 12

#: What the gates return when dev mode admits a request with no bearer at all.
ANONYMOUS_PRINCIPAL = "anonymous"


def principal_label(token: str) -> str:
    """The non-secret label for a bearer credential: ``bearer:<sha12>``.

    Deterministic, so the same credential is the same handle across requests,
    restarts and the historical re-stamp (``scripts/scrub_audit_actor_tokens.py``
    computes this same label for rows written before the two principals
    existed). The gates stamp :func:`principal_actor` — this label behind the
    principal kind — never the credential (W4 TOKEN-FIX).
    """
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    return f"{PRINCIPAL_LABEL_PREFIX}{digest[:PRINCIPAL_LABEL_HEX]}"


def principal_actor(principal: str, token: str) -> str:
    """The AUDIT ACTOR the gates return: ``<principal>:bearer:<sha12>`` (X2-B1).

    ``service:bearer:…`` for the fleet token, ``operator:bearer:…`` for the
    operator's. A-1 (switch on): a named service is ``service:<name>`` — its
    handle, no hash needed (the name says which credential) — and the admin
    token is ``admin-token:bearer:…``. Every handler that stamps the authenticated principal —
    ``descriptor_audit_log.actor_id``, ``consult_sessions.principal``,
    ``journal_proposals.decided_by``, labels' ``labeled_by``, the vault's
    ``created_by``, the DLQ ``actor`` — stamps THIS, so every write row says
    which of the two principals made it, and never the credential.
    """
    if principal.startswith(_signin.SERVICE_ACTOR_PREFIX):
        return principal
    return f"{principal}:{principal_label(token)}"


# ---------------------------------------------------------------------------
# Human-only routes (X2-B1) — declared HERE, in one place
# ---------------------------------------------------------------------------

#: Routes the SERVICE principal is refused on: ``(method, route template,
#: why)``. ``method`` ``"*"`` = every method; a template ending ``/`` is a
#: PREFIX (every route under it); ``{param}`` matches one path segment. It is
#: matched against the request's concrete, already-decoded path — the string
#: the router itself matches — so a request cannot dodge it by spelling.
#:
#: * the credential vault — storing, probing or deleting a secret is the
#:   operator's act; nothing in the fleet reads the vault over HTTP.
#: * descriptor lifecycle changes BY HAND — transition / retire / promote /
#:   rollback. The fleet never calls them (the walker writes direct-DB; the
#:   runtime never changes a head's state over HTTP), and the deploy
#:   runbook's ``draft -> active`` steps are the operator's.
#:
#: The ``--apply`` tools are NOT routes: every one of them (the actor-token
#: scrub, the backfills, the reclassifiers) writes direct-DB, so there is no
#: registry route of theirs to list.
HUMAN_ONLY_ROUTES: tuple[tuple[str, str, str], ...] = (
    ("*", "/api/v1/registry/vault/", "the credential vault"),
    ("POST", "/api/v1/registry/descriptors/{family}/{descriptor_id}/transition",
     "a descriptor lifecycle change by hand"),
    ("POST", "/api/v1/registry/descriptors/{family}/{descriptor_id}/retire",
     "a descriptor retire by hand"),
    ("POST", "/api/v1/registry/descriptors/{family}/{descriptor_id}/promote",
     "a descriptor promote by hand"),
    ("POST", "/api/v1/registry/descriptors/{family}/{descriptor_id}/rollback",
     "a descriptor rollback by hand"),
    # S-2: acknowledging or snoozing an alert says a PERSON has seen it; the
    # fleet never does either.
    ("*", "/api/v1/v3/system/alerts/{episode_id}/ack",
     "acknowledging an alert"),
    ("*", "/api/v1/v3/system/alerts/{episode_id}/snooze",
     "snoozing an alert"),
)


def _template_pattern(template: str) -> re.Pattern[str]:
    """``/a/{x}/b`` → ``^/a/[^/]+/b$``; a template ending ``/`` is a prefix."""
    parts = re.split(r"(\{[^}]+\})", template)
    body = "".join("[^/]+" if p.startswith("{") else re.escape(p) for p in parts)
    return re.compile("^" + body + ("" if template.endswith("/") else "$"))


_HUMAN_ONLY_PATTERNS = tuple(
    (m, _template_pattern(t), why) for m, t, why in HUMAN_ONLY_ROUTES
)


def human_only_reason(method: str, path: str) -> str | None:
    """Why ``method path`` is operator-only, or ``None`` if it is not.

    ``path`` is the CONCRETE request path (``scope["path"]``): FastAPI ≥0.140
    nests an included router, so the matched route's own template is relative
    to its prefix and cannot be compared against a full template.
    """
    for m, pattern, why in _HUMAN_ONLY_PATTERNS:
        if m != "*" and m != method.upper():
            continue
        if pattern.match(path):
            return why
    return None


def _misconfigured() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail=MISCONFIGURED_AUTH_DETAIL,
    )


def _refuse_service_on_human_only(
    principal: str | None, method: str | None, path: str | None,
) -> None:
    """403 ``service_principal_refused`` — the service token on a human-only route."""
    if not is_service_kind(principal) or not method:
        return
    why = human_only_reason(method, path or "")
    if why is not None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"service_principal_refused: {why} is operator-only — "
                f"present the operator token ({API_TOKEN_ENV}), not "
                f"{SERVICE_TOKEN_ENV}."
            ),
        )


def resolve_bearer_header(
    authorization: str | None, *, method: str | None = None, path: str | None = None,
) -> tuple[str | None, str]:
    """``(principal kind, audit actor)`` for an HTTP ``Authorization`` header.

    The HTTP gate's whole decision (B-2 / X2-B1), minus the session path —
    the principal kind is ``None`` in dev mode, where no principal is
    configured. Raises exactly what :func:`require_bearer` answers: 503
    misconfigured, 401 missing / malformed, 403 invalid, 403
    ``service_principal_refused`` (``method`` + ``path`` given).
    """
    configured = _configured_principals()
    if not configured and not _dev_mode():
        raise _misconfigured()
    if not configured:
        if not authorization:
            return None, ANONYMOUS_PRINCIPAL
        if not authorization.lower().startswith("bearer "):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Authorization must be 'Bearer <token>'",
            )
        dev_presented = authorization[7:].strip()
        return None, (principal_label(dev_presented) if dev_presented else ANONYMOUS_PRINCIPAL)
    if not authorization:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing Authorization: Bearer <token>",
        )
    if not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authorization must be 'Bearer <token>'",
        )
    presented = authorization[7:].strip()
    principal = _match_principal(presented, configured)
    if principal is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="invalid bearer token",
        )
    _refuse_service_on_human_only(principal, method, path)
    if principal == _signin.ADMIN_TOKEN_KIND:
        _note_break_glass(method, path)
    return principal, principal_actor(principal, presented)


def _put(state: Any, key: str, value: Any) -> None:
    if isinstance(state, dict):
        state[key] = value
    else:
        setattr(state, key, value)


def _read(state: Any, key: str) -> Any:
    if state is None:
        return None
    if isinstance(state, dict):
        return state.get(key)
    try:
        return getattr(state, key, None)
    except Exception:  # noqa: BLE001 — a State raising is "no stamp"
        return None


def stamp_bearer(state: Any, principal: str | None, actor: str) -> None:
    """Stamp a resolved bearer caller on ``state`` (a Starlette ``State`` or
    the raw ``scope["state"]`` dict) — X4 GATE.

    ``principal`` / ``principal_actor`` (B1's names), and the route gate's
    ``legba_role`` = ``operator`` for BOTH bearer principals with
    ``legba_subject`` = the principal kind. Dev mode (``principal`` None) is
    the operator class too, subject ``operator``, and stamps no ``principal``
    (``/auth/me`` keeps saying ``anonymous`` there, as before).
    """
    _put(state, "principal_actor", actor)
    if _signin.signin_enabled():
        # A-1: every bearer principal (break-glass, services, dev) is the
        # admin class; services are still refused on the human-only routes.
        _put(state, _rr.STATE_ROLE_ATTR, _signin.ROLE_ADMIN)
        _put(state, _rr.STATE_SUBJECT_ATTR, principal or _signin.ROLE_ADMIN)
        _put(
            state, _rr.STATE_AUTH_SOURCE_ATTR,
            _rr.AUTH_SOURCE_BEARER if principal else _rr.AUTH_SOURCE_DEV,
        )
        if principal:
            _put(state, "principal", principal)
        return
    _put(state, _rr.STATE_ROLE_ATTR, _rr.ROLE_OPERATOR)
    _put(state, _rr.STATE_SUBJECT_ATTR, principal or _rr.DEFAULT_SUBJECT)
    _put(
        state, _rr.STATE_AUTH_SOURCE_ATTR,
        _rr.AUTH_SOURCE_BEARER if principal else _rr.AUTH_SOURCE_DEV,
    )
    if principal:
        _put(state, "principal", principal)


def stamped_bearer_actor(state: Any) -> str | None:
    """The audit actor the BEARER middleware (or an earlier gate) stamped on
    ``state`` — ``None`` when it stamped nothing, or the stamp is a session's."""
    if _read(state, _rr.STATE_AUTH_SOURCE_ATTR) not in (
        _rr.AUTH_SOURCE_BEARER, _rr.AUTH_SOURCE_DEV,
    ):
        return None
    actor = _read(state, "principal_actor")
    return actor if isinstance(actor, str) and actor else None


def require_bearer(
    authorization: str | None = Header(default=None),
    request: Request = None,  # type: ignore[assignment] — FastAPI injects it
) -> str:
    """Bearer-token gate for the HTTP endpoints (B-2 fail-closed; X2-B1 two principals).

      * EITHER token set → require `Authorization: Bearer <token>`; missing
        header → 401, a credential matching neither → 403 (constant-time
        compare via `hmac.compare_digest`). `LEGBA_REGISTRY_API_TOKEN` is the
        `operator`; `LEGBA_REGISTRY_SERVICE_TOKEN` is the `service`.
      * the SERVICE principal on a :data:`HUMAN_ONLY_ROUTES` route → 403
        ``service_principal_refused``.
      * NEITHER set AND `LEGBA_DEV_MODE=1` → development mode: any bearer
        (and a missing header) accepted.
      * NEITHER set otherwise → 503 "service misconfigured" on EVERY
        guarded request. A deploy that forgot the token must not expose
        an open admin API.
      * a SESSION request (X3-B3: the cookie middleware stamped a signed-in
        user and no Bearer was presented) → that user's `user:<id>`; the
        middleware already stamped the state.

    Stamps the request state (:func:`stamp_bearer`: `principal` (`service` |
    `operator`), `principal_actor`, `legba_role`) and returns that NON-SECRET
    audit actor (:func:`principal_actor` — never the credential) for handlers
    to stamp as `actor`; `"anonymous"` / the bare :func:`principal_label` in
    dev mode, where no principal is configured.

    X4 GATE: on the real app the bearer MIDDLEWARE has already resolved the
    credential through :func:`resolve_bearer_header` and stamped the state
    before the route-role gate ran; this gate then READS that stamp (and
    re-checks the human-only rule). It resolves the header itself only when
    nothing was stamped — an app without the middleware. The 503 check stays
    first either way.
    """
    configured = _configured_principals()
    if not configured and not _dev_mode():
        raise _misconfigured()
    state = request.state if request is not None else None
    method = request.method if request is not None else None
    path = (request.scope.get("path") or request.url.path) if request is not None else None
    stamped = stamped_bearer_actor(state)
    if stamped is not None:
        _refuse_service_on_human_only(_read(state, "principal"), method, path)
        return stamped
    # X3-B3: a SESSION request (``session_auth.SessionCookieMiddleware``
    # resolved the cookie and stamped the state; no Bearer was presented) is
    # the signed-in user — ``user:<id>``. A Bearer always takes the path below.
    user_actor = session_actor(
        state, bearer_presented=_bearer_from_header(authorization) is not None,
    )
    if user_actor is not None:
        return user_actor
    principal, actor = resolve_bearer_header(authorization, method=method, path=path)
    if state is not None:
        stamp_bearer(state, principal, actor)
    return actor


#: End of the C3 deprecation window for the routes the source-quality ledger
#: merges (``/source_credibility`` reads + ``/sources/{id}/assurance``). They
#: KEEP SERVING their original wire shape until then — a 3xx would silently
#: hand callers a DIFFERENT response body, which is worse than a header they
#: can see. Removal is a later, deliberate commit, not a side effect of this
#: one (`docs/SEAMS.md`, the C3 entry).
DEPRECATION_SUNSET_HTTP_DATE = "Tue, 27 Oct 2026 00:00:00 GMT"


def sunset_headers(successor: str) -> Callable[[Response], None]:
    """A FastAPI dependency that marks a route deprecated, per RFC 8594/9745.

    Stamps ``Deprecation: true``, ``Sunset: <date>`` and a
    ``Link: <successor>; rel="successor-version"`` on the response. The route
    keeps serving its original body unchanged — this advertises the window, it
    does not shorten it. Attach per-route (``dependencies=[Depends(
    sunset_headers("/api/v1/v3/..."))]``) so a module's WRITE routes, which
    have no successor, are not swept up with its reads.
    """

    def _stamp(response: Response) -> None:
        response.headers["Deprecation"] = "true"
        response.headers["Sunset"] = DEPRECATION_SUNSET_HTTP_DATE
        response.headers["Link"] = f'<{successor}>; rel="successor-version"'

    return _stamp


def _bearer_from_header(authorization: str | None) -> str | None:
    """Extract the raw token from an `Authorization: Bearer <token>` header.

    Returns the stripped token, or `None` when the header is absent or not a
    well-formed `Bearer` credential (the caller then falls back to `?token=`).
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        return None
    return authorization[7:].strip() or None


#: The WebSocket subprotocol that carries the bearer credential.
#:
#: A browser cannot set `Authorization` on a WS upgrade, but it CAN offer
#: subprotocols (`new WebSocket(url, protocols)`), and those travel in a header
#: instead of the URL. The client offers exactly two tokens —
#: `legba.bearer.v1` then the base64url (unpadded) credential — and the server
#: echoes back only the SCHEME name, never the credential.
WS_BEARER_SUBPROTOCOL = "legba.bearer.v1"


def _offers_bearer_subprotocol(sec_websocket_protocol: str | None) -> bool:
    """True when the client's offer names the bearer scheme.

    The accept handshake needs this SEPARATELY from the credential: RFC 6455
    requires the server to echo one of the offered subprotocols, and it must
    echo the scheme name only — never the credential half of the offer.
    """
    if not sec_websocket_protocol:
        return False
    parts = [p.strip() for p in sec_websocket_protocol.split(",") if p.strip()]
    return bool(parts) and parts[0] == WS_BEARER_SUBPROTOCOL


def _bearer_from_subprotocol(sec_websocket_protocol: str | None) -> str | None:
    """Extract the credential from a `Sec-WebSocket-Protocol` offer.

    Expects `legba.bearer.v1, <base64url-token>`. Base64url (unpadded) keeps
    the value inside RFC 6455's `token` grammar for ANY credential — a raw
    secret containing `,`, `=` or whitespace would otherwise be unsendable or
    silently mangled.

    Returns `None` for any offer that isn't this scheme, so an unrelated
    subprotocol negotiation falls through to the other credential paths.
    """
    if not sec_websocket_protocol:
        return None
    parts = [p.strip() for p in sec_websocket_protocol.split(",") if p.strip()]
    if len(parts) < 2 or parts[0] != WS_BEARER_SUBPROTOCOL:
        return None
    raw = parts[1]
    try:
        padded = raw + "=" * (-len(raw) % 4)
        return base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
    except Exception:
        # A malformed offer is NOT a silent fall-through to "no credential
        # presented" — say so, then let the caller reject it as unauthorized.
        logger.warning(
            "registry.ws.auth.subprotocol_undecodable — client offered "
            "%s with a value that is not base64url utf-8",
            WS_BEARER_SUBPROTOCOL,
        )
        return None


def _authorize_ws_token(
    token: str | None,
    authorization: str | None = None,
    sec_websocket_protocol: str | None = None,
    *,
    surface: str = "websocket",
    state: Any = None,
) -> str:
    """WebSocket auth: the credential may arrive three ways —

      * `Sec-WebSocket-Protocol: legba.bearer.v1, <b64url>` — THE path for
        browsers. A URL credential is printed in console warnings, kept in
        the browser's history/referrer surface and written verbatim to
        every access log that records the request line; a subprotocol
        header is none of those things. This is what the SPA uses.
      * `Authorization: Bearer <token>` header — injected by the Caddy
        reverse proxy on the proxied WS upgrade for the canonical
        deployment (the operator's browser never sees it), so a header
        bearer also authenticates (ITEM 2.5).
      * `?token=` query-string — DEPRECATED on the WebSocket. Still accepted
        so a stale SPA build doesn't hard-break the moment the server rolls,
        but every use logs a warning naming the deprecation. Remove once no
        client sends it.

    `surface` names the transport, and exists ONLY to scope that deprecation
    warning. This gate is shared with the consult SSE relay, where `?token=`
    is not deprecated and has no replacement: `EventSource` can set neither
    headers nor subprotocols. Warning there would be a false alarm that reads
    exactly like a stale UI build. SSE callers pass `surface="sse"`.

    The subprotocol credential wins when present; below it the ORIGINAL
    query-then-header order is untouched, so nothing about the two existing
    paths changes. Returns the NON-SECRET audit actor
    (:func:`principal_actor`, the same one `require_bearer` returns — the WS
    handler logs it on connect/close) or raises HTTPException for the
    connection handler to convert to a WebSocket close frame.

    Mirrors `require_bearer` (B-2): fail-closed 503 when neither token is
    configured and `LEGBA_DEV_MODE=1` is not set; constant-time comparison
    against BOTH principals when configured (X2-B1). No WS/SSE route is
    human-only, so the service principal is never refused here.

    `state` (X3-B3) is the connection's state: when NO credential is
    presented and the session-cookie middleware stamped a signed-in user,
    that user's `user:<id>` is returned (the browser's cookie rides the WS
    upgrade and the SSE GET; caddy strips the perimeter header for it).

    X4 GATE: a connection the bearer middleware already stamped (the same
    resolution, :func:`resolve_ws_credential`) returns that stamp; the 503
    check stays first."""
    configured = _configured_principals()
    if not configured and not _dev_mode():
        raise _misconfigured()
    stamped = stamped_bearer_actor(state)
    if stamped is not None:
        return stamped
    resolved = resolve_ws_credential(
        token, authorization, sec_websocket_protocol, surface=surface,
    )
    if resolved is not None:
        return resolved[1]
    # X3-B3: no credential presented at all, but the cookie middleware
    # stamped a signed-in user on the connection's ``state`` → that user.
    user_actor = session_actor(state)
    if user_actor is not None:
        return user_actor
    if not configured:
        return ANONYMOUS_PRINCIPAL
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="invalid token")


def resolve_ws_credential(
    token: str | None,
    authorization: str | None = None,
    sec_websocket_protocol: str | None = None,
    *,
    surface: str = "websocket",
) -> tuple[str | None, str] | None:
    """``(principal kind, audit actor)`` for a WebSocket / SSE credential, or
    ``None`` when NONE was presented (the caller decides: a session, dev
    mode's ``anonymous``, or 403).

    The credential order of :func:`_authorize_ws_token` (subprotocol, then
    ``?token=``, then the header) with its deprecation warning; raises 503
    misconfigured and 403 ``invalid token``. The kind is ``None`` in dev mode.
    """
    presented = _bearer_from_subprotocol(sec_websocket_protocol)
    if presented is None:
        if token and surface == "websocket":
            logger.warning(
                "registry.ws.auth.deprecated_query_token — a client "
                "authenticated with ?token=, which leaks the credential into "
                "browser console warnings and access logs. Upgrade it to the "
                "%r subprotocol; this path will be removed.",
                WS_BEARER_SUBPROTOCOL,
            )
        # Unchanged from before the subprotocol path existed — query first,
        # then the Caddy-injected header bearer.
        presented = token or _bearer_from_header(authorization)
    configured = _configured_principals()
    if not configured and not _dev_mode():
        raise _misconfigured()
    if not presented:
        return None
    if not configured:
        return None, principal_label(presented)
    principal = _match_principal(presented, configured)
    if principal is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="invalid token",
        )
    return principal, principal_actor(principal, presented)


# ---------------------------------------------------------------------------
# Dependency bundle
# ---------------------------------------------------------------------------


@dataclass
class RegistryAPIDeps:
    """Injected at app-construction time; all routes resolve via this."""

    descriptor_registry: DescriptorRegistry
    stack_registry: StackRegistry
    vault: CredentialVault
    dlq: DescriptorDeadLetter
    audit_logger: AuditLogger
    vocabulary_cache: VocabularyCache
    nats_store: Any | None = None  # for WebSocket multiplexing
    conversion_registry: Any | None = None  # ConversionWebhookRegistry | None


def _get_deps(request: Request) -> RegistryAPIDeps:
    deps = getattr(request.app.state, "registry_deps", None)
    if deps is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="registry api not configured (missing RegistryAPIDeps)",
        )
    return deps
