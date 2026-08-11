# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Consult step-stream relay — Piece 1 (chat consult rework), D5.

Mounts under ``/api/v1/consult/stream/{request_id}``. Built via
``build_consult_stream_router(deps)``; ``server.py`` wires it next to the
consult router.

What it does
============

When a chat consult run executes, the analyst actor publishes each ReAct
step to a request-scoped **core** NATS subject
``legba.consult.steps.<request_id>`` (see
``runtime/dapr_actors.py`` — the per-run ``step_publish`` closure) and a
terminal ``{"type": "final", ...}`` frame when the run ends. This route opens
an **ephemeral core subscription** on that subject (no JetStream consumer, no
retained state) and relays each frame to the browser as a Server-Sent Events
stream, closing deterministically when the ``final`` frame arrives.

Auth
====

``EventSource`` (the browser SSE client) cannot set an ``Authorization``
header, so the stream route accepts the bearer either as a ``?token=`` query
param (the SPA's path — it only holds ``localStorage.legba_token``) or as a
``Bearer`` header (Caddy-injected on the proxied request). This reuses the
registry's existing ``_authorize_ws_token`` gate — the same fail-closed,
constant-time check the WebSocket surface already uses — so no new auth logic
is introduced.

Race note
=========

Steps published before the browser attaches are lost (core pub/sub, no
replay). This is by design: the live view is best-effort; the authoritative
trace is always in the POST ``/consult`` response
(``consult_response.data`` for chat, the persisted row for deep). The SPA
mints ``request_id`` client-side and subscribes here *before* it POSTs, which
minimises the window.
"""

from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Header, Query, status
from fastapi.responses import StreamingResponse

from .api import RegistryAPIDeps, _authorize_ws_token

logger = logging.getLogger(__name__)

#: Idle keepalive cadence (seconds). Under the Dapr invoke timeout (300s) so a
#: long-running consult that's between steps keeps the connection warm without
#: the proxy reaping it.
_KEEPALIVE_TIMEOUT_SECONDS = 25.0

#: Bound the relay queue so a runaway publisher can't grow it unbounded; the
#: oldest-vs-newest tradeoff (drop newest on full) is acceptable for a
#: best-effort live view.
_QUEUE_MAXSIZE = 256


def build_consult_stream_router(deps: RegistryAPIDeps) -> APIRouter:
    """Construct the consult SSE relay router bound to the registry deps.

    Mount on a FastAPI app via::

        app.include_router(
            build_consult_stream_router(deps), prefix="/api/v1"
        )
    """
    router = APIRouter(tags=["consult"])

    @router.get("/consult/stream/{request_id}")
    async def consult_stream(
        request_id: str,
        token: str | None = Query(default=None),
        authorization: str | None = Header(default=None),
    ) -> StreamingResponse:
        # Bearer via ?token= (EventSource can set neither headers NOR
        # subprotocols) or Bearer header (Caddy-injected). Reuses the WS gate —
        # fail-closed, constant-time. `surface="sse"` suppresses the gate's
        # query-token deprecation warning: that deprecation is about the events
        # WEBSOCKET, which moved its credential to the `legba.bearer.v1`
        # subprotocol. SSE has no such replacement, so warning here would be a
        # false alarm indistinguishable from a stale UI build.
        _authorize_ws_token(token, authorization, surface="sse")

        if deps.nats_store is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="consult stream relay requires a connected NATS store",
            )

        subject = f"legba.consult.steps.{request_id}"
        queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=_QUEUE_MAXSIZE)

        async def _on_msg(msg) -> None:
            try:
                queue.put_nowait(msg.data)
            except asyncio.QueueFull:  # drop newest — best-effort live view
                pass

        async def event_gen():
            sub = await deps.nats_store.nc.subscribe(subject, cb=_on_msg)
            try:
                while True:
                    try:
                        data = await asyncio.wait_for(
                            queue.get(), timeout=_KEEPALIVE_TIMEOUT_SECONDS
                        )
                    except asyncio.TimeoutError:
                        # SSE comment frame — keeps the connection warm.
                        yield b": keepalive\n\n"
                        continue
                    yield b"data: " + data + b"\n\n"
                    try:
                        if json.loads(data).get("type") == "final":
                            break
                    except Exception:
                        # Malformed frame — relay it but don't close on it.
                        pass
            finally:
                try:
                    await sub.unsubscribe()
                except Exception:  # pragma: no cover — best-effort teardown
                    logger.debug(
                        "consult_stream.unsubscribe.failed", exc_info=True
                    )

        return StreamingResponse(
            event_gen(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    return router


__all__ = ["build_consult_stream_router"]
