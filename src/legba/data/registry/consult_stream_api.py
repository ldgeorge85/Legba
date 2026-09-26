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

Two terminal frames, and which one closes the stream
====================================================

The actor publishes a ``final`` frame from ``dapr_actors`` before its method
returns, but that frame carries only ``output_id`` / ``mode`` — **not the
answer**. It said "the run ended"; the answer still had to come back on the
POST response. That is exactly the dependency the 2026-09-16 504 severed.

So the registry publishes a second terminal frame once the detached run has
persisted its turn (``consult_runs.publish_terminal_frame``), tagged
``final_source: "registry"`` and carrying the whole projected response — or, on
a failure, the reason plus the partial trace the run accumulated. THAT is what
closes this stream.

The actor's bare ``final`` is still relayed (the panel shows "synthesising"),
but it only starts a bounded grace window: if the registry frame doesn't follow
within :data:`_FINAL_GRACE_SECONDS` the relay closes anyway, so a registry task
that died can never pin the connection open forever.

Race note
=========

Steps published before the browser attaches are lost (core pub/sub, no
replay). The SPA mints ``request_id`` client-side and subscribes here *before*
it POSTs, which minimises the window. What is no longer best-effort is the
*answer*: it is persisted as the session's assistant turn regardless of who is
listening, so a client that missed frames recovers it from
``GET /consult/runs/{request_id}`` or ``GET /consult/sessions/{id}``.
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

#: How long the relay waits for the REGISTRY's terminal frame after the actor's
#: bare ``final``. Normally milliseconds (one audit INSERT); generous here
#: because a deep run reads its row back first. Past it the relay closes on its
#: own rather than holding a connection open for a frame that isn't coming.
_FINAL_GRACE_SECONDS = 45.0


def _is_registry_terminal(frame: dict) -> bool:
    """True for the registry's answer-bearing terminal frame.

    Keyed on ``final_source`` rather than on the presence of an answer, because
    a failed run's terminal frame legitimately has no answer — it carries the
    reason and the partial — and must still close the stream.
    """
    return frame.get("type") == "final" and frame.get("final_source") == "registry"


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
            # Set when the actor's bare ``final`` arrives: from then on the
            # relay is waiting only for the registry's answer-bearing frame,
            # under a bounded grace window.
            grace_deadline: float | None = None
            try:
                while True:
                    if grace_deadline is not None:
                        remaining = grace_deadline - asyncio.get_running_loop().time()
                        if remaining <= 0:
                            logger.info(
                                "consult_stream.grace_expired request_id=%s",
                                request_id,
                            )
                            break
                        timeout = min(remaining, _KEEPALIVE_TIMEOUT_SECONDS)
                    else:
                        timeout = _KEEPALIVE_TIMEOUT_SECONDS
                    try:
                        data = await asyncio.wait_for(queue.get(), timeout=timeout)
                    except asyncio.TimeoutError:
                        # SSE comment frame — keeps the connection warm.
                        yield b": keepalive\n\n"
                        continue
                    yield b"data: " + data + b"\n\n"
                    try:
                        frame = json.loads(data)
                    except Exception:
                        # Malformed frame — relay it but don't close on it.
                        continue
                    if not isinstance(frame, dict):
                        continue
                    if _is_registry_terminal(frame):
                        break
                    if frame.get("type") == "final" and grace_deadline is None:
                        # The actor is done; the registry still has to persist
                        # and publish the answer. Wait for it, but not forever.
                        grace_deadline = (
                            asyncio.get_running_loop().time()
                            + _FINAL_GRACE_SECONDS
                        )
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
