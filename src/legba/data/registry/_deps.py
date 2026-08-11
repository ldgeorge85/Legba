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
    answers to "is the gate configured?" in the process.
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
import hmac
import logging
import os
from dataclasses import dataclass
from typing import Any, Callable

from fastapi import HTTPException, Header, Request, Response, status

from .audit import AuditLogger
from .credentials import CredentialVault
from .descriptor import DescriptorRegistry
from .dlq import DescriptorDeadLetter
from .stack import StackRegistry
from .vocabulary_cache import VocabularyCache

#: Auth-path warnings emit under the ``legba.data.registry.api`` logger NAME,
#: not this module's, so operators' existing greps for the registry auth
#: posture keep working even though the code lives here (module docstring).
logger = logging.getLogger("legba.data.registry.api")


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

API_TOKEN_ENV = "LEGBA_REGISTRY_API_TOKEN"
DEV_MODE_ENV = "LEGBA_DEV_MODE"

# Returned on every guarded request when the gate is misconfigured (B-2).
MISCONFIGURED_AUTH_DETAIL = (
    f"registry API misconfigured: {API_TOKEN_ENV} is unset/empty and "
    f"{DEV_MODE_ENV}=1 is not set; refusing all guarded requests. "
    f"Set a bearer token, or set {DEV_MODE_ENV}=1 explicitly for local "
    "development."
)


def _current_token() -> str | None:
    raw = os.getenv(API_TOKEN_ENV, "").strip()
    return raw or None


def _dev_mode() -> bool:
    """Explicit development-mode opt-in (`LEGBA_DEV_MODE=1`).

    Only consulted when `LEGBA_REGISTRY_API_TOKEN` is unset/empty — a
    configured token is ALWAYS enforced, dev flag or not.
    """
    return os.getenv(DEV_MODE_ENV, "").strip() == "1"


def _token_matches(presented: str, configured: str) -> bool:
    """Constant-time bearer comparison (B-2)."""
    return hmac.compare_digest(
        presented.encode("utf-8"), configured.encode("utf-8"),
    )


def require_bearer(
    authorization: str | None = Header(default=None),
) -> str:
    """Bearer-token gate for the HTTP endpoints (B-2: fail-closed).

      * `LEGBA_REGISTRY_API_TOKEN` set → require `Authorization: Bearer
        <token>`; missing header → 401, mismatch → 403 (constant-time
        compare via `hmac.compare_digest`).
      * Unset/empty AND `LEGBA_DEV_MODE=1` → development mode: any
        bearer (and a missing header) accepted.
      * Unset/empty otherwise → 503 "service misconfigured" on EVERY
        guarded request. A deploy that forgot the token must not expose
        an open admin API.

    Returns the supplied principal token (or `"anonymous"` in dev mode) so
    handlers can stamp it as `actor` on audit-log rows.
    """
    configured = _current_token()
    if configured is None:
        if not _dev_mode():
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=MISCONFIGURED_AUTH_DETAIL,
            )
        if not authorization:
            return "anonymous"
        if not authorization.lower().startswith("bearer "):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Authorization must be 'Bearer <token>'",
            )
        return authorization[7:].strip() or "anonymous"
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
    if not _token_matches(presented, configured):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="invalid bearer token",
        )
    return presented


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
    paths changes. Returns the principal or raises HTTPException for the
    connection handler to convert to a WebSocket close frame.

    Mirrors `require_bearer` exactly (B-2): fail-closed 503 when the token
    is unconfigured and `LEGBA_DEV_MODE=1` is not set; constant-time
    comparison when configured."""
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
    configured = _current_token()
    if configured is None:
        if not _dev_mode():
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=MISCONFIGURED_AUTH_DETAIL,
            )
        return presented or "anonymous"
    if not presented or not _token_matches(presented, configured):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="invalid token",
        )
    return presented


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
