# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared inbound-webhook FastAPI router (L-137 supporting infrastructure).

Some source kinds — Discord, GitHub, Stripe, generic webhook — are *push*
sources: external systems POST events into Legba rather than Legba polling
them. Per L-102 §2 the source-kind contract has a polling shape (`pull`);
this module supplies the inbound counterpart.

Architecture:

  * A single :class:`InboundWebhookRouter` is mounted on the L-113 server at
    ``/api/v1/webhooks/`` (separate prefix from the registry router at
    ``/api/v1/registry/``).
  * Source handlers register themselves at ``on_activate`` time by calling
    :meth:`InboundWebhookRouter.register_handler`; they get a stable URL
    path ``/api/v1/webhooks/<source_id>`` and a deregistration call to make
    at ``on_retire`` time.
  * Every inbound POST is dispatched to the handler's ``handle_webhook``
    callback. The router itself is intentionally dumb: signature verification
    and event parsing live on the handler so each kind can implement its own
    scheme (Ed25519 for Discord, HMAC-SHA256 for GitHub, Stripe-signature
    for Stripe, …).

The router uses ``include_in_schema=False`` for the per-source endpoints —
they're not part of Legba's public API surface; external systems learn URLs
through the descriptor registration response.

This module never imports a concrete handler kind. The dispatch interface
is the :class:`InboundWebhookHandler` protocol below, so any handler
satisfying it can register.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Protocol, runtime_checkable

from fastapi import APIRouter, HTTPException, Request, Response, status

logger = logging.getLogger(__name__)


WEBHOOK_PREFIX = "/api/v1/webhooks"


# ---------------------------------------------------------------------------
# Handler protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class InboundWebhookHandler(Protocol):
    """Structural-typing surface for objects that can receive a webhook POST.

    The router calls :meth:`handle_webhook` with the raw request. The handler
    is responsible for:

      1. Reading the body (``await request.body()``) — once.
      2. Verifying the signature using whatever scheme its source defines.
      3. Parsing the payload and emitting Signals via the configured
         ``emit_signal`` callback.
      4. Returning the appropriate response — Discord requires a JSON body
         for some interaction types (e.g. PING → PONG).

    On failure the handler raises HTTPException (mapped to a 4xx) or returns
    a Response with the appropriate status. The router does not log payloads;
    handlers log via their bound telemetry handle.
    """

    source_id: str

    async def handle_webhook(self, request: Request) -> Response: ...


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


class InboundWebhookRouter:
    """Owns the FastAPI router for ``/api/v1/webhooks/<source_id>``.

    Construct one instance per process. Mount via :meth:`mount` on the L-113
    FastAPI app:

        webhook_router = InboundWebhookRouter()
        webhook_router.mount(app)

    Source handlers register at activate time:

        webhook_router.register_handler(my_handler)  # source_id from handler
        ...
        webhook_router.unregister_handler(my_handler.source_id)
    """

    def __init__(self) -> None:
        self._handlers: dict[str, InboundWebhookHandler] = {}
        self._router = APIRouter(tags=["webhooks"])
        self._wire_routes()

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register_handler(self, handler: InboundWebhookHandler) -> str:
        """Register `handler` at ``/api/v1/webhooks/<handler.source_id>``.

        Returns the absolute URL path the external system should POST to.
        Re-registration of the same source_id is allowed (the second call
        replaces the first); the previous handler is logged.
        """
        if not handler.source_id:
            raise ValueError("InboundWebhookHandler must have a non-empty source_id")
        if "/" in handler.source_id or " " in handler.source_id:
            raise ValueError(
                f"source_id must be URL-safe, got {handler.source_id!r}"
            )
        if handler.source_id in self._handlers:
            logger.info(
                "inbound-webhook handler replacement source_id=%s", handler.source_id
            )
        self._handlers[handler.source_id] = handler
        return f"{WEBHOOK_PREFIX}/{handler.source_id}"

    def unregister_handler(self, source_id: str) -> bool:
        """Remove a registered handler. Returns True iff a row was removed."""
        return self._handlers.pop(source_id, None) is not None

    def is_registered(self, source_id: str) -> bool:
        return source_id in self._handlers

    def get_handler(self, source_id: str) -> InboundWebhookHandler | None:
        return self._handlers.get(source_id)

    def registered_source_ids(self) -> list[str]:
        return sorted(self._handlers.keys())

    # ------------------------------------------------------------------
    # FastAPI wiring
    # ------------------------------------------------------------------

    @property
    def router(self) -> APIRouter:
        return self._router

    def mount(self, app: Any) -> None:
        """Mount the router on the given FastAPI app under WEBHOOK_PREFIX."""
        app.include_router(self._router, prefix=WEBHOOK_PREFIX)

    def _wire_routes(self) -> None:
        router = self._router

        @router.post("/{source_id}", include_in_schema=False)
        async def _dispatch(source_id: str, request: Request) -> Response:
            handler = self._handlers.get(source_id)
            if handler is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"no inbound-webhook handler registered for {source_id!r}",
                )
            try:
                return await handler.handle_webhook(request)
            except HTTPException:
                raise
            except Exception as exc:  # pragma: no cover - defensive
                logger.exception(
                    "inbound-webhook handler raised source_id=%s err=%s",
                    source_id,
                    exc,
                )
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="inbound webhook handler error",
                ) from exc

        @router.get("/{source_id}/healthz", include_in_schema=False)
        async def _healthz(source_id: str) -> dict[str, Any]:
            registered = source_id in self._handlers
            return {
                "source_id": source_id,
                "registered": registered,
            }

        @router.get("", include_in_schema=False)
        async def _list_registered() -> dict[str, Any]:
            return {
                "registered": self.registered_source_ids(),
                "prefix": WEBHOOK_PREFIX,
            }


# ---------------------------------------------------------------------------
# Process-wide default instance
# ---------------------------------------------------------------------------


_default_router: InboundWebhookRouter | None = None


def default_router() -> InboundWebhookRouter:
    """Return the process-wide default router (lazy-initialized).

    The L-113 server constructs and mounts this at app startup; source
    handlers reach it via this accessor at ``on_activate`` time when they
    aren't passed one explicitly via context.

    Multiple Legba processes (test runs in particular) each get their own
    instance — there's no shared global state across processes.
    """
    global _default_router
    if _default_router is None:
        _default_router = InboundWebhookRouter()
    return _default_router


def reset_default_router() -> None:
    """Test helper: drop the process-wide default router (next call to
    :func:`default_router` reconstructs)."""
    global _default_router
    _default_router = None


__all__ = [
    "InboundWebhookHandler",
    "InboundWebhookRouter",
    "WEBHOOK_PREFIX",
    "default_router",
    "reset_default_router",
]
