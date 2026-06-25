# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Generic inbound-webhook push source (P-06 working example).

The minimal-but-complete *push* counterpart to the RSS poll example. An
upstream system POSTs a JSON event to ``/api/v1/webhooks/<source_id>``; this
handler verifies an optional shared-secret (``X-Webhook-Token`` header, HMAC
not required for the generic kind), parses the body into one target-agnostic
:class:`Signal`, and hands it to the actor's ``emit_signal`` callback — which
runs the baseline, writes the canonical row, and publishes to NATS (the same
downstream path the poll branch uses).

This is the reference push kind: it has no external dependency (no Ed25519,
no partner SDK) so the inbound-POST seam is exercised end-to-end on the dev
rig. The facial-rec fleet kind (§4.10) is the same shape with the fleet's
signature scheme + a :class:`ProvisionBlock` for the upstream watch.

Satisfies :class:`legba.data.sources._protocols.PushSource` (it exposes
``ingest``) AND the base :class:`SourceHandler` (``pull`` is an empty
generator — push sources are never polled).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Awaitable, Callable, ClassVar

from fastapi import HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field

from ._contract import Signal, SourceContext, SourceHealth

logger = logging.getLogger(__name__)


EmitSignal = Callable[[Signal], Awaitable[None]]


class GenericWebhookConfig(BaseModel):
    """Config for :class:`GenericWebhookSourceHandler`.

    ``shared_secret`` (optional) is compared, constant-time, against the
    ``X-Webhook-Token`` header — a minimal auth so a random POST can't inject
    signals. ``modality`` stamps the emitted signal; ``id_field`` /
    ``url_field`` name the payload keys used for the external id + canonical
    URL. ``media_ref_field`` (optional) names a key whose value becomes the
    signal's ``media_ref`` (the facial-rec ``vod_url`` shape).
    """

    model_config = ConfigDict(extra="forbid")

    shared_secret: str | None = Field(default=None, max_length=512)
    modality: str = Field(default="structured")
    id_field: str = Field(default="id", max_length=128)
    url_field: str = Field(default="url", max_length=128)
    media_ref_field: str | None = Field(default=None, max_length=128)


class GenericWebhookSourceHandler:
    """Inbound-webhook push source — the P-06 reference push kind."""

    kind: ClassVar[str] = "generic_webhook"
    family: ClassVar[str] = "source"
    schema_version: ClassVar[str] = "legba/source.generic_webhook/1-0-0"
    handler_version: ClassVar[str] = "0.1.0"
    config_schema: ClassVar[type[BaseModel]] = GenericWebhookConfig
    push_source: ClassVar[bool] = True

    def __init__(
        self,
        config: GenericWebhookConfig,
        *,
        emit_signal: EmitSignal | None = None,
    ) -> None:
        self.config = config
        self._emit_signal = emit_signal
        self._ctx: SourceContext | None = None
        self._signals_total = 0
        self._last_inbound_at: datetime | None = None
        self._last_error: str | None = None
        self._paused = False
        self._router: Any | None = None
        self.webhook_path: str | None = None

    # ----- router protocol -------------------------------------------------

    @property
    def source_id(self) -> str:
        if self._ctx is None:
            raise RuntimeError("GenericWebhookSourceHandler not bound to a context")
        return self._ctx.source_id

    def bind_emit(self, ctx: SourceContext, emit_signal: EmitSignal) -> None:
        """Bind the runtime context + emit callback (called by the actor)."""
        self._ctx = ctx
        self._emit_signal = emit_signal

    # ----- PushSource.ingest ----------------------------------------------

    async def ingest(
        self,
        ctx: SourceContext,
        body: bytes,
        headers: dict[str, str],
    ) -> AsyncIterator[Signal]:
        """Verify + parse an inbound POST into zero-or-more Signals.

        The :class:`PushSource` surface (used directly in tests + by the
        actor when it owns request reading). Raises ``ValueError`` on a bad
        token / unparseable body so the caller maps it to a 4xx.
        """
        self._verify_token(headers)
        payload = self._parse_body(body)
        signal = self._build_signal(ctx, payload)
        yield signal

    # ----- InboundWebhookHandler.handle_webhook ---------------------------

    async def handle_webhook(self, request: Request) -> Response:
        """Router entrypoint: read the request, ingest, emit each signal."""
        if self._paused:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="generic webhook source is paused",
            )
        if self._ctx is None or self._emit_signal is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="handler not bound to a runtime emit callback",
            )
        body = await request.body()
        headers = {k.lower(): v for k, v in request.headers.items()}
        try:
            count = 0
            async for sig in self.ingest(self._ctx, body, headers):
                await self._emit_signal(sig)
                count += 1
            self._signals_total += count
            self._last_inbound_at = datetime.now(tz=timezone.utc)
            self._last_error = None
        except ValueError as exc:
            self._last_error = str(exc)
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc),
            ) from exc
        except PermissionError as exc:
            self._last_error = str(exc)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid webhook token",
            ) from exc
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    # ----- lifecycle -------------------------------------------------------

    async def on_configure(self, ctx: SourceContext) -> None:
        self._ctx = ctx

    async def on_activate(self, ctx: SourceContext) -> None:
        self._ctx = ctx
        self._paused = False
        if self._router is not None:
            self.webhook_path = self._router.register_handler(self)

    async def on_pause(self, ctx: SourceContext) -> None:
        self._paused = True

    async def on_resume(self, ctx: SourceContext) -> None:
        self._paused = False

    async def on_retire(self, ctx: SourceContext) -> None:
        if self._router is not None:
            self._router.unregister_handler(ctx.source_id)
        self.webhook_path = None

    async def pull(
        self, ctx: SourceContext, since: datetime | None = None,
    ) -> AsyncIterator[Signal]:
        """Push sources are never polled — empty generator."""
        return
        yield  # pragma: no cover

    async def health_check(self, ctx: SourceContext) -> SourceHealth:
        return SourceHealth(
            state="paused" if self._paused else "healthy",
            last_success_at=self._last_inbound_at,
            last_error=self._last_error,
            rows_pulled_24h=self._signals_total,
            detail={"push": True, "path": self.webhook_path},
        )

    # ----- internals -------------------------------------------------------

    def _verify_token(self, headers: dict[str, str]) -> None:
        secret = self.config.shared_secret
        if not secret:
            return
        token = headers.get("x-webhook-token", "")
        if not hmac.compare_digest(token, secret):
            raise PermissionError("webhook token mismatch")

    def _parse_body(self, body: bytes) -> dict[str, Any]:
        if not body:
            raise ValueError("empty webhook body")
        try:
            data = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"body is not valid UTF-8 JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise ValueError("webhook body must be a JSON object")
        return data

    def _build_signal(self, ctx: SourceContext, payload: dict[str, Any]) -> Signal:
        cfg = self.config
        external_id = str(payload.get(cfg.id_field) or "")
        canonical = payload.get(cfg.url_field)
        media_ref = payload.get(cfg.media_ref_field) if cfg.media_ref_field else None
        basis = json.dumps(payload, sort_keys=True, default=str)
        content_hash = hashlib.sha256(basis.encode("utf-8")).hexdigest()
        return Signal(
            source_id=ctx.source_id,
            modality=cfg.modality,  # type: ignore[arg-type]
            media_ref=media_ref if isinstance(media_ref, str) else None,
            payload={"external_id": external_id, **payload},
            content_hash=content_hash,
            canonical_url=canonical if isinstance(canonical, str) else None,
            raw_provenance={"fetch_kind": "generic_webhook"},
        )


__all__ = [
    "EmitSignal",
    "GenericWebhookConfig",
    "GenericWebhookSourceHandler",
]
