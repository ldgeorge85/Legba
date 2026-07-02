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

import base64
import json
import logging
from typing import Any, Awaitable, Callable, Protocol, runtime_checkable

from fastapi import APIRouter, HTTPException, Request, Response, status

logger = logging.getLogger(__name__)


WEBHOOK_PREFIX = "/api/v1/webhooks"

# --- S1 accept-and-enqueue inbound front (signals ingestion track) ---------
#
# The inbound handler does MINIMAL work — validate + auth + publish the RAW
# payload onto a NATS JetStream subject — and returns 202 immediately; a durable
# drain (:class:`legba.runtime.inbound_drain.InboundWebhookDrain`) pulls the
# stream and does the ingest -> write_canonical_signal -> legba.signals.> OFF
# the request path. One subject per push source: ``legba.inbound.<source_id>``.
INBOUND_SUBJECT_ROOT = "legba.inbound"
INBOUND_STREAM_NAME = "legba_inbound"

# The header keys the drain's ``ingest()`` needs re-materialised on the far
# side — the shared-secret token (re-verified in the drain) and the content
# type. Everything else on the inbound request is dropped from the envelope.
INBOUND_HEADER_ALLOW: tuple[str, ...] = ("x-webhook-token", "content-type")


def inbound_subject(source_id: str) -> str:
    """Compose the per-source inbound subject ``legba.inbound.<source_id>``.

    ``source_id`` may itself carry dots (``source.reuters.world``); the extra
    tokens are harmless — the ``legba.inbound.>`` stream subject + drain filter
    both match multi-token tails, and the drain resolves the handler from the
    envelope's ``source_id`` field (not the subject), so no flattening is
    needed here.
    """
    return f"{INBOUND_SUBJECT_ROOT}.{source_id}"


def encode_inbound_envelope(
    source_id: str, body: bytes, headers: dict[str, str],
) -> bytes:
    """Serialise the RAW inbound POST into a JetStream-publishable envelope.

    JSON bytes ``{"source_id", "body_b64", "headers"}`` — the raw body is
    base64-encoded because it is arbitrary bytes and NATS/JSON is text (a ~33%
    inflation; a very large frame can approach the NATS ``max_payload``, see the
    S1 risks). Only the :data:`INBOUND_HEADER_ALLOW` header subset the drain's
    ``ingest()`` re-reads is carried.
    """
    hdrs = {k: headers[k] for k in INBOUND_HEADER_ALLOW if k in headers}
    envelope = {
        "source_id": source_id,
        "body_b64": base64.b64encode(body).decode("ascii"),
        "headers": hdrs,
    }
    return json.dumps(envelope).encode("utf-8")


def decode_inbound_envelope(data: bytes) -> dict[str, Any]:
    """Parse an inbound envelope back into ``{source_id, body, headers}``.

    Raises :class:`ValueError` on a structurally-bad envelope (not JSON, not an
    object, missing/typo'd ``source_id`` / ``body_b64``, bad base64) so the
    drain dead-letters it rather than replaying un-parseable bytes forever.
    """
    try:
        envelope = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"inbound envelope is not valid UTF-8 JSON: {exc}") from exc
    if not isinstance(envelope, dict):
        raise ValueError("inbound envelope must be a JSON object")
    source_id = envelope.get("source_id")
    if not source_id or not isinstance(source_id, str):
        raise ValueError("inbound envelope missing a non-empty string source_id")
    body_b64 = envelope.get("body_b64")
    if not isinstance(body_b64, str):
        raise ValueError("inbound envelope missing a string body_b64")
    try:
        body = base64.b64decode(body_b64, validate=True)
    except Exception as exc:
        raise ValueError(f"inbound envelope body_b64 is not valid base64: {exc}") from exc
    headers = envelope.get("headers") or {}
    if not isinstance(headers, dict):
        raise ValueError("inbound envelope headers must be an object")
    return {"source_id": source_id, "body": body, "headers": headers}


# The injected sink shape: ``async (subject, payload_bytes) -> None`` — bound at
# bring-up to :meth:`legba.data.nats.NatsStore.publish_json` (JetStream). The
# awaited publish-ack is the durability boundary the front's 202 promises.
InboundSink = Callable[[str, bytes], Awaitable[None]]


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
        self._inbound_sink: InboundSink | None = None
        self._router = APIRouter(tags=["webhooks"])
        self._wire_routes()

    # ------------------------------------------------------------------
    # S1 accept-and-enqueue inbound front
    # ------------------------------------------------------------------

    def bind_inbound_sink(self, publish_callable: InboundSink) -> None:
        """Bind the JetStream publish sink for the accept-and-enqueue front.

        Called ONCE at bring-up with a closure over
        :meth:`legba.data.nats.NatsStore.publish_json`. Injected (not imported)
        so this module — mounted on the registry-side L-113 server — never pulls
        ``nats`` into the slim image. Until a sink is bound
        :meth:`publish_inbound` fails closed (the front returns 503).
        """
        self._inbound_sink = publish_callable

    @property
    def inbound_sink_bound(self) -> bool:
        return self._inbound_sink is not None

    async def publish_inbound(
        self, source_id: str, body: bytes, headers: dict[str, str],
    ) -> None:
        """Publish the RAW inbound envelope to ``legba.inbound.<source_id>``.

        The awaited JetStream publish-ack IS the durability boundary the front's
        202 promises (accepted-for-processing == persisted in the stream, NOT
        written to Postgres). Raises when no sink is bound OR when the stream's
        buffer cap (``max_msgs``) is hit — the caller (the webhook front) maps a
        raise to a 503 so backpressure is HONEST, never a silent drop.
        """
        if self._inbound_sink is None:
            raise RuntimeError(
                "inbound sink not bound — accept-and-enqueue front is not wired "
                "(call bind_inbound_sink at bring-up)"
            )
        payload = encode_inbound_envelope(source_id, body, headers)
        await self._inbound_sink(inbound_subject(source_id), payload)

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
    "INBOUND_HEADER_ALLOW",
    "INBOUND_STREAM_NAME",
    "INBOUND_SUBJECT_ROOT",
    "InboundSink",
    "InboundWebhookHandler",
    "InboundWebhookRouter",
    "WEBHOOK_PREFIX",
    "decode_inbound_envelope",
    "default_router",
    "encode_inbound_envelope",
    "inbound_subject",
    "reset_default_router",
]
