# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""S1 accept-and-enqueue inbound front + drain (signals ingestion track).

Proves the S1 contract WITHOUT a live NATS/DB — the front and the drain are
driven against fakes (the same shape the ``nats_informer`` self-heal regression
uses), so the request-path/off-request-path split, the idempotence keys, and the
dead-letter policy are all exercised hermetically:

  1. The webhook front does MINIMAL work — validate + auth + publish the RAW
     envelope + return **202** — and NEVER writes a signal on the request path;
     an auth failure is **401 BEFORE any enqueue** (fail-closed).
  2. The drain decodes a published envelope, runs the handler's
     ``ingest_and_emit`` (the existing write path), and **acks AFTER** the write.
  3. Redelivery is idempotent — the deterministic ``signal_id`` (source_id +
     content_hash) re-derives the same id, so a second delivery would no-op the
     ``ON CONFLICT (id) DO NOTHING`` write (asserted at the id/hash level here;
     the DB-level no-op is covered by the P-06 acquisition suite).
  4. An unparseable envelope / an ``ingest()`` ``ValueError`` is **dead-lettered
     (term)**, never silently acked; a not-yet-registered handler **NAKs** for
     redelivery.
"""
from __future__ import annotations

import asyncio

import pytest

from legba.data.sources._contract import (
    InMemoryStateStore,
    Signal,
    SourceContext,
)
from legba.data.sources.generic_webhook import GenericWebhookSourceHandler
from legba.data.sources.webhook_router import (
    InboundWebhookRouter,
    decode_inbound_envelope,
    encode_inbound_envelope,
)
from legba.runtime.inbound_drain import InboundWebhookDrain


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeRequest:
    """Minimal Starlette-Request stand-in for handle_webhook."""

    def __init__(self, body: bytes, headers: dict[str, str]) -> None:
        self._body = body
        self.headers = headers

    async def body(self) -> bytes:
        return self._body


class _RecordingSink:
    """Captures every (subject, payload) the front publishes."""

    def __init__(self) -> None:
        self.published: list[tuple[str, bytes]] = []

    async def __call__(self, subject: str, payload: bytes) -> None:
        self.published.append((subject, payload))


class _RaisingSink:
    """A sink whose publish raises — the buffer-full / backpressure case."""

    async def __call__(self, subject: str, payload: bytes) -> None:
        raise RuntimeError("maximum messages exceeded")


class _FakeMsg:
    def __init__(self, data: bytes, subject: str = "legba.inbound.src.x",
                 num_delivered: int = 1) -> None:
        self.data = data
        self.subject = subject
        self.acked = False
        self.nakd = False
        self.termd = False

        class _Meta:
            pass

        self.metadata = _Meta()
        self.metadata.num_delivered = num_delivered

    async def ack(self) -> None:
        self.acked = True

    async def nak(self) -> None:
        self.nakd = True

    async def term(self) -> None:
        self.termd = True


def _ctx(source_id: str) -> SourceContext:
    from pydantic import BaseModel

    class _Cfg(BaseModel):
        pass

    return SourceContext(
        target_id=source_id, target_version="v", source_id=source_id,
        config=_Cfg(), state_store=InMemoryStateStore(),
    )


def _bound_handler(source_id: str, emitted: list[Signal], *,
                   shared_secret: str | None = None) -> GenericWebhookSourceHandler:
    """A generic-webhook handler whose emit callback records signals (no DB)."""
    h = GenericWebhookSourceHandler(
        GenericWebhookSourceHandler.config_schema(
            id_field="camera_id", shared_secret=shared_secret,
        )
    )
    ctx = _ctx(source_id)

    async def _emit(sig: Signal) -> None:
        emitted.append(sig)

    h.bind_emit(ctx, _emit)
    return h


# ---------------------------------------------------------------------------
# 1. Front: validate + auth + publish + 202 (no DB write on the request path)
# ---------------------------------------------------------------------------


def test_front_publishes_and_202_without_write() -> None:
    async def _body() -> None:
        source_id = "src.cam"
        emitted: list[Signal] = []
        h = _bound_handler(source_id, emitted)
        router = InboundWebhookRouter()
        sink = _RecordingSink()
        router.bind_inbound_sink(sink)
        h._router = router  # bound at on_activate in production

        body = b'{"camera_id":"cam-42","location":"gate-3"}'
        resp = await h.handle_webhook(
            _FakeRequest(body, {"x-webhook-token": "", "content-type": "application/json"})
        )

        assert resp.status_code == 202, "front must accept-for-processing, not 204/created"
        assert emitted == [], "the front must NOT write/emit on the request path"
        assert len(sink.published) == 1, "exactly one raw envelope enqueued"
        subject, payload = sink.published[0]
        assert subject == "legba.inbound.src.cam"
        env = decode_inbound_envelope(payload)
        assert env["source_id"] == source_id
        assert env["body"] == body, "the RAW body is carried verbatim"

    asyncio.run(_body())


def test_front_auth_fail_401_before_enqueue() -> None:
    async def _body() -> None:
        from fastapi import HTTPException

        emitted: list[Signal] = []
        h = _bound_handler("src.cam", emitted, shared_secret="s3cret")
        router = InboundWebhookRouter()
        sink = _RecordingSink()
        router.bind_inbound_sink(sink)
        h._router = router

        with pytest.raises(HTTPException) as ei:
            await h.handle_webhook(
                _FakeRequest(b'{"camera_id":"x"}', {"x-webhook-token": "wrong"})
            )
        assert ei.value.status_code == 401
        assert sink.published == [], "a bad token must NEVER reach the stream (fail-closed)"

    asyncio.run(_body())


def test_front_backpressure_maps_to_503() -> None:
    async def _body() -> None:
        from fastapi import HTTPException

        h = _bound_handler("src.cam", [])
        router = InboundWebhookRouter()
        router.bind_inbound_sink(_RaisingSink())
        h._router = router

        with pytest.raises(HTTPException) as ei:
            await h.handle_webhook(
                _FakeRequest(b'{"camera_id":"x"}', {})
            )
        assert ei.value.status_code == 503, "a full/failed enqueue is honest backpressure, not a drop"

    asyncio.run(_body())


def test_front_empty_body_400() -> None:
    async def _body() -> None:
        from fastapi import HTTPException

        h = _bound_handler("src.cam", [])
        router = InboundWebhookRouter()
        sink = _RecordingSink()
        router.bind_inbound_sink(sink)
        h._router = router

        with pytest.raises(HTTPException) as ei:
            await h.handle_webhook(_FakeRequest(b"", {}))
        assert ei.value.status_code == 400
        assert sink.published == []

    asyncio.run(_body())


# ---------------------------------------------------------------------------
# 2. Drain: decode → ingest_and_emit → ack-after-write
# ---------------------------------------------------------------------------


def test_drain_ingests_writes_and_acks() -> None:
    async def _body() -> None:
        source_id = "src.cam"
        emitted: list[Signal] = []
        h = _bound_handler(source_id, emitted)
        router = InboundWebhookRouter()
        router.register_handler(_HandlerShim(source_id, h))

        body = b'{"camera_id":"cam-42"}'
        env = encode_inbound_envelope(source_id, body, {"content-type": "application/json"})
        msg = _FakeMsg(env)

        drain = InboundWebhookDrain(_FakeStore(), router)
        await drain._handle_message(msg)

        assert len(emitted) == 1, "the drain must run the existing ingest/emit write path"
        assert emitted[0].source_id == source_id
        assert msg.acked, "ack ONLY after a successful write"
        assert not msg.nakd and not msg.termd
        assert drain.stats.written == 1 and drain.stats.acked == 1

    asyncio.run(_body())


def test_drain_redelivery_is_idempotent_deterministic_id() -> None:
    async def _body() -> None:
        source_id = "src.cam"
        emitted: list[Signal] = []
        h = _bound_handler(source_id, emitted)
        router = InboundWebhookRouter()
        router.register_handler(_HandlerShim(source_id, h))

        body = b'{"camera_id":"cam-42"}'
        env = encode_inbound_envelope(source_id, body, {})
        drain = InboundWebhookDrain(_FakeStore(), router)

        # First delivery then a redelivery (crash-after-write-before-ack window).
        await drain._handle_message(_FakeMsg(env, num_delivered=1))
        await drain._handle_message(_FakeMsg(env, num_delivered=2))

        assert len(emitted) == 2, "both deliveries reach the emit path"
        # The deterministic signal_id is identical across redeliveries, so the
        # real write_canonical_signal ON CONFLICT (id) DO NOTHING no-ops the
        # second write — no duplicate CANONICAL signal.
        assert emitted[0].signal_id == emitted[1].signal_id, (
            "redelivery must re-derive the SAME deterministic signal_id"
        )
        assert emitted[0].content_hash == emitted[1].content_hash

    asyncio.run(_body())


# ---------------------------------------------------------------------------
# 3. Dead-letter / redeliver policy
# ---------------------------------------------------------------------------


def test_drain_deadletters_unparseable_envelope() -> None:
    async def _body() -> None:
        router = InboundWebhookRouter()
        drain = InboundWebhookDrain(_FakeStore(), router)
        msg = _FakeMsg(b"not-a-json-envelope")

        await drain._handle_message(msg)

        assert msg.termd, "a hard-unparseable envelope is dead-lettered (term), not acked"
        assert not msg.acked and not msg.nakd
        assert drain.stats.parse_errors == 1 and drain.stats.dead_lettered == 1

    asyncio.run(_body())


def test_drain_deadletters_ingest_value_error() -> None:
    async def _body() -> None:
        source_id = "src.cam"
        h = _bound_handler(source_id, [])  # id_field default parse; empty body -> ValueError
        router = InboundWebhookRouter()
        router.register_handler(_HandlerShim(source_id, h))

        # An envelope whose BODY is empty: ingest()._parse_body raises ValueError.
        env = encode_inbound_envelope(source_id, b"", {})
        msg = _FakeMsg(env)
        drain = InboundWebhookDrain(_FakeStore(), router)
        await drain._handle_message(msg)

        assert msg.termd, "an unparseable body (ingest ValueError) is dead-lettered"
        assert not msg.acked
        assert drain.stats.dead_lettered == 1

    asyncio.run(_body())


def test_drain_naks_when_handler_not_yet_registered() -> None:
    async def _body() -> None:
        router = InboundWebhookRouter()  # no handler registered (boot race)
        env = encode_inbound_envelope("src.cam", b'{"camera_id":"x"}', {})
        drain = InboundWebhookDrain(_FakeStore(), router)

        # Early deliveries NAK for redelivery ...
        early = _FakeMsg(env, num_delivered=1)
        await drain._handle_message(early)
        assert early.nakd and not early.termd and not early.acked
        assert drain.stats.handler_missing == 1 and drain.stats.nak_redeliver == 1

        # ... but once max_deliver is reached it is dead-lettered (term).
        exhausted = _FakeMsg(env, num_delivered=drain._max_deliver)
        await drain._handle_message(exhausted)
        assert exhausted.termd and not exhausted.nakd

    asyncio.run(_body())


# ---------------------------------------------------------------------------
# 4. Self-heal rebind (mirror the nats_informer regression)
# ---------------------------------------------------------------------------


def test_drain_rebinds_dead_consumer_and_recovers() -> None:
    async def _body() -> None:
        source_id = "src.cam"
        emitted: list[Signal] = []
        h = _bound_handler(source_id, emitted)
        router = InboundWebhookRouter()
        router.register_handler(_HandlerShim(source_id, h))

        env = encode_inbound_envelope(source_id, b'{"camera_id":"cam-42"}', {})
        sub1 = _FakeSub(["error"])                 # dead consumer -> forces rebind
        sub2 = _FakeSub([[_FakeMsg(env)], "timeout"])
        js = _FakeJS([sub1, sub2])
        store = _FakeStore(js)

        drain = InboundWebhookDrain(store, router)
        drain._fetch_error_backoff = (0.01, 0.02)
        await drain.start()
        try:
            for _ in range(100):
                if emitted:
                    break
                await asyncio.sleep(0.05)
        finally:
            await drain.stop()

        assert sub1.unsubscribed, "the dead subscription must be unsubscribed on rebind"
        assert js.pull_subscribe_calls >= 2, "rebind must re-subscribe"
        assert drain.stats.rebinds == 1
        assert len(emitted) == 1, "the drain recovers and processes the post-rebind envelope"

    asyncio.run(_body())


# ---------------------------------------------------------------------------
# Fakes for the drain fetch-loop / store
# ---------------------------------------------------------------------------


class _HandlerShim:
    """Adapts a GenericWebhookSourceHandler into the router's handler protocol
    with an explicit source_id (the real handler derives it from a bound ctx)."""

    def __init__(self, source_id: str, handler: GenericWebhookSourceHandler) -> None:
        self.source_id = source_id
        self._h = handler

    async def handle_webhook(self, request):  # pragma: no cover - unused here
        return await self._h.handle_webhook(request)

    async def ingest_and_emit(self, body: bytes, headers: dict[str, str]) -> int:
        return await self._h.ingest_and_emit(body, headers)


class _FakeSub:
    def __init__(self, script: list) -> None:
        self._script = script
        self._i = 0
        self.unsubscribed = False

    async def fetch(self, batch: int, timeout: float):
        await asyncio.sleep(0.001)
        item = self._script[min(self._i, len(self._script) - 1)]
        self._i += 1
        if item == "error":
            raise RuntimeError("ServiceUnavailableError: code=None")
        if item == "timeout":
            raise asyncio.TimeoutError
        return item

    async def unsubscribe(self) -> None:
        self.unsubscribed = True


class _FakeJS:
    def __init__(self, subs: list[_FakeSub] | None = None) -> None:
        self._subs = subs or []
        self._i = 0
        self.add_consumer_calls = 0
        self.pull_subscribe_calls = 0

    async def add_consumer(self, stream, config):
        self.add_consumer_calls += 1

    async def pull_subscribe(self, subject, durable, stream):
        self.pull_subscribe_calls += 1
        sub = self._subs[min(self._i, len(self._subs) - 1)]
        self._i += 1
        return sub


class _FakeStore:
    def __init__(self, js: _FakeJS | None = None) -> None:
        self.js = js or _FakeJS()
