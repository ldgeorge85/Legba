# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Piece 1 (T7) — consult SSE step-relay route.

Integration test against a real ``NatsStore`` (the in-container test NATS at
127.0.0.1:4222). Publishes step frames + a terminal ``final`` to
``legba.consult.steps.<id>`` and asserts the route's generator yields one
``data:`` frame per message and STOPS after ``final``. Also asserts the idle
keepalive path with a short timeout override.

Marked ``integration`` — it requires the test NATS broker (skipped out of
container where the broker is absent).
"""

from __future__ import annotations

import asyncio
import json
import uuid

import pytest
import pytest_asyncio

import legba.data.registry.consult_stream_api as stream_api
from legba.data.config import NatsConfig
from legba.data.nats import NatsStore
from legba.data.registry.consult_stream_api import build_consult_stream_router


pytestmark = [pytest.mark.integration]


@pytest_asyncio.fixture
async def nats_store() -> NatsStore:
    store = NatsStore(NatsConfig(url="nats://127.0.0.1:4222"))
    await store.connect()
    try:
        yield store
    finally:
        await store.close()


class _Deps:
    """Minimal RegistryAPIDeps-shaped object — only nats_store is read."""

    def __init__(self, store: NatsStore) -> None:
        self.nats_store = store


def _get_stream_endpoint():
    """Reach into the router for the GET /consult/stream/{request_id} handler."""
    router = build_consult_stream_router(_Deps.__new__(_Deps))  # type: ignore[arg-type]
    for route in router.routes:
        if getattr(route, "path", "") == "/consult/stream/{request_id}":
            return route.endpoint
    raise AssertionError("stream route not registered")


async def _drain(body_iterator, *, limit: int = 50) -> list[bytes]:
    out: list[bytes] = []
    async for chunk in body_iterator:
        out.append(chunk)
        if len(out) >= limit:
            break
    return out


@pytest.mark.asyncio
async def test_relay_yields_steps_then_stops_on_final(nats_store, monkeypatch):
    # Disable dev-mode token gate friction: the route only calls
    # _authorize_ws_token — patch it to a no-op so the test focuses on relay.
    monkeypatch.setattr(stream_api, "_authorize_ws_token", lambda *a, **k: "test")

    request_id = uuid.uuid4().hex
    subject = f"legba.consult.steps.{request_id}"
    deps = _Deps(nats_store)
    router = build_consult_stream_router(deps)  # type: ignore[arg-type]
    endpoint = None
    for route in router.routes:
        if getattr(route, "path", "") == "/consult/stream/{request_id}":
            endpoint = route.endpoint
    assert endpoint is not None

    resp = await endpoint(request_id, token="t", authorization=None)

    # Give the generator a moment to subscribe before we publish.
    gen = resp.body_iterator

    async def _publish_after_subscribe():
        await asyncio.sleep(0.3)
        for i in range(3):
            await nats_store.nc.publish(
                subject,
                json.dumps({"type": "step", "round": i}).encode("utf-8"),
            )
        await nats_store.nc.publish(
            subject,
            json.dumps({"type": "final", "request_id": request_id}).encode("utf-8"),
        )
        await nats_store.nc.flush()

    pub = asyncio.create_task(_publish_after_subscribe())
    frames = await asyncio.wait_for(_drain(gen), timeout=10.0)
    await pub

    # 3 step frames + 1 final = 4 data frames; the generator stopped after
    # final (it didn't keep yielding).
    data_frames = [f for f in frames if f.startswith(b"data: ")]
    assert len(data_frames) == 4
    # Last frame is the final marker.
    assert b'"type": "final"' in data_frames[-1] or b'"type":"final"' in data_frames[-1]


@pytest.mark.asyncio
async def test_relay_keepalive_on_idle(nats_store, monkeypatch):
    monkeypatch.setattr(stream_api, "_authorize_ws_token", lambda *a, **k: "test")
    # Short keepalive so the idle path triggers fast.
    monkeypatch.setattr(stream_api, "_KEEPALIVE_TIMEOUT_SECONDS", 0.2)

    request_id = uuid.uuid4().hex
    subject = f"legba.consult.steps.{request_id}"
    deps = _Deps(nats_store)
    router = build_consult_stream_router(deps)  # type: ignore[arg-type]
    endpoint = [
        r.endpoint
        for r in router.routes
        if getattr(r, "path", "") == "/consult/stream/{request_id}"
    ][0]
    resp = await endpoint(request_id, token="t", authorization=None)
    gen = resp.body_iterator

    # First yield should be a keepalive comment (no message published yet).
    first = await asyncio.wait_for(gen.__anext__(), timeout=5.0)
    assert first == b": keepalive\n\n"

    # Now publish a final so the generator terminates cleanly.
    await nats_store.nc.publish(
        subject,
        json.dumps({"type": "final"}).encode("utf-8"),
    )
    await nats_store.nc.flush()
    # Drain to the final.
    saw_final = False
    for _ in range(20):
        chunk = await asyncio.wait_for(gen.__anext__(), timeout=5.0)
        if chunk.startswith(b"data: "):
            saw_final = True
            break
    assert saw_final
