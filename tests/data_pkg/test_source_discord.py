# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for L-137 — Discord webhook source kind.

Coverage:

  * Signature verification: round-trip Ed25519 sign-then-verify, plus
    rejection of tampered timestamp / body / signature.
  * Payload parsing: interaction shapes (PING, APPLICATION_COMMAND,
    MESSAGE_COMPONENT, MODAL_SUBMIT), event-webhook MESSAGE_CREATE, and
    unknown shape fallback.
  * Allowed-event-types filtering: events outside the whitelist verify
    but are not emitted as Signals.
  * Integration: POST a Discord-shaped payload through FastAPI TestClient
    against the shared :class:`InboundWebhookRouter`; assert a Signal is
    emitted on the configured callback.
  * Health: degraded when not registered, healthy when registered + key
    present, unhealthy when no key.

These tests don't touch Postgres / NATS / Discord — they're entirely
process-local. They live in `tests/data_pkg/` to share the package test
harness conventions, not because they need substrate fixtures.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import pytest
from fastapi import FastAPI
from nacl.signing import SigningKey
from starlette.testclient import TestClient

from legba.data.sources._contract import (
    InMemoryStateStore,
    Signal,
    SourceContext,
)
from legba.data.sources.discord import (
    DiscordSignatureError,
    DiscordWebhookConfig,
    DiscordWebhookSourceHandler,
    EVENT_TYPE_APPLICATION_COMMAND,
    EVENT_TYPE_INTERACTION_PING,
    EVENT_TYPE_MESSAGE_COMPONENT,
    EVENT_TYPE_MESSAGE_CREATE,
    EVENT_TYPE_MODAL_SUBMIT,
    EVENT_TYPE_UNKNOWN,
    INTERACTION_TYPE_APPLICATION_COMMAND,
    INTERACTION_TYPE_MESSAGE_COMPONENT,
    INTERACTION_TYPE_MODAL_SUBMIT,
    INTERACTION_TYPE_PING,
    parse_discord_payload,
    verify_discord_signature,
)
from legba.data.sources.webhook_router import (
    WEBHOOK_PREFIX,
    InboundWebhookRouter,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_signed_envelope(
    signing_key: SigningKey,
    *,
    body: bytes,
    timestamp: str,
) -> dict[str, str]:
    """Produce the Discord-style signed headers for `body`/`timestamp`."""
    signature = signing_key.sign(timestamp.encode("utf-8") + body).signature
    return {
        "X-Signature-Ed25519": signature.hex(),
        "X-Signature-Timestamp": timestamp,
    }


def _make_source_context(
    *,
    source_id: str = "src-discord-test",
    target_id: str = "target-test",
    target_version: str = "v" + "0" * 63,
    config: Any = None,
) -> SourceContext:
    # SourceContext was extended (parallel Phase 3 task) to require a
    # ``config`` field — pass a placeholder pydantic model so the handler
    # tests don't depend on the concrete config type of any other kind.
    cfg = config or DiscordWebhookConfig(
        application_id="app-123",
        public_key_secret="discord.test.public_key",
    )
    return SourceContext(
        target_id=target_id,
        target_version=target_version,
        source_id=source_id,
        config=cfg,
        state_store=InMemoryStateStore(),
        logger=logging.getLogger("test.discord"),
    )


def _make_handler(
    *,
    public_key: bytes,
    config: DiscordWebhookConfig | None = None,
    webhook_router: InboundWebhookRouter | None = None,
    emit_signal=None,
) -> tuple[DiscordWebhookSourceHandler, list[Signal]]:
    """Construct a handler wired to an in-memory secret resolver + emit list.

    Returns the handler plus the list emitted Signals get appended to so
    tests can assert on what got published.
    """
    captured: list[Signal] = []

    async def _capture(signal: Signal) -> None:
        captured.append(signal)

    async def _resolver(secret_id: str) -> bytes:
        assert secret_id == "discord.test.public_key"
        return public_key

    cfg = config or DiscordWebhookConfig(
        application_id="app-123",
        public_key_secret="discord.test.public_key",
        allowed_event_types=None,
    )
    handler = DiscordWebhookSourceHandler(
        cfg,
        secret_resolver=_resolver,
        webhook_router=webhook_router,
        emit_signal=emit_signal or _capture,
    )
    return handler, captured


# ---------------------------------------------------------------------------
# Signature verification — round-trip + tamper rejection
# ---------------------------------------------------------------------------


def test_signature_round_trip_accepts_valid():
    """A signature produced by SigningKey.sign verifies against its
    VerifyKey under the exact ``timestamp + body`` scheme Discord uses."""
    sk = SigningKey.generate()
    vk_bytes = bytes(sk.verify_key)

    body = json.dumps({"type": 1}).encode("utf-8")
    timestamp = "1715817600"
    sig = sk.sign(timestamp.encode("utf-8") + body).signature

    # Should not raise.
    verify_discord_signature(
        public_key=vk_bytes,
        signature_hex=sig.hex(),
        timestamp=timestamp,
        body=body,
    )


def test_signature_round_trip_accepts_hex_public_key():
    """The key resolver may surface hex-encoded bytes (the form Discord
    publishes) — the handler must accept that shape."""
    sk = SigningKey.generate()
    vk_hex = bytes(sk.verify_key).hex()

    body = b'{"type": 1}'
    timestamp = "1715817700"
    sig = sk.sign(timestamp.encode("utf-8") + body).signature

    verify_discord_signature(
        public_key=vk_hex,
        signature_hex=sig.hex(),
        timestamp=timestamp,
        body=body,
    )
    # And the bytes-form of hex (the natural form coming out of the vault).
    verify_discord_signature(
        public_key=vk_hex.encode("ascii"),
        signature_hex=sig.hex(),
        timestamp=timestamp,
        body=body,
    )


def test_signature_rejects_tampered_body():
    sk = SigningKey.generate()
    body = b'{"type": 2}'
    timestamp = "1715817800"
    sig = sk.sign(timestamp.encode("utf-8") + body).signature

    with pytest.raises(DiscordSignatureError):
        verify_discord_signature(
            public_key=bytes(sk.verify_key),
            signature_hex=sig.hex(),
            timestamp=timestamp,
            body=b'{"type": 3}',  # tampered
        )


def test_signature_rejects_tampered_timestamp():
    sk = SigningKey.generate()
    body = b'{"type": 2}'
    sig = sk.sign(b"1715817900" + body).signature

    with pytest.raises(DiscordSignatureError):
        verify_discord_signature(
            public_key=bytes(sk.verify_key),
            signature_hex=sig.hex(),
            timestamp="1715817901",  # off by one
            body=body,
        )


def test_signature_rejects_malformed_hex():
    sk = SigningKey.generate()
    with pytest.raises(DiscordSignatureError):
        verify_discord_signature(
            public_key=bytes(sk.verify_key),
            signature_hex="not-hex!!",
            timestamp="1",
            body=b"{}",
        )


def test_signature_rejects_wrong_length_signature():
    sk = SigningKey.generate()
    # 32 bytes hex = wrong length (need 64 bytes).
    with pytest.raises(DiscordSignatureError):
        verify_discord_signature(
            public_key=bytes(sk.verify_key),
            signature_hex="aa" * 32,
            timestamp="1",
            body=b"{}",
        )


def test_signature_rejects_missing_headers():
    sk = SigningKey.generate()
    with pytest.raises(DiscordSignatureError):
        verify_discord_signature(
            public_key=bytes(sk.verify_key),
            signature_hex="",
            timestamp="1",
            body=b"{}",
        )
    with pytest.raises(DiscordSignatureError):
        verify_discord_signature(
            public_key=bytes(sk.verify_key),
            signature_hex="aa" * 64,
            timestamp="",
            body=b"{}",
        )


def test_signature_rejects_malformed_public_key():
    with pytest.raises(DiscordSignatureError):
        verify_discord_signature(
            public_key="zz" * 32,  # not hex
            signature_hex="aa" * 64,
            timestamp="1",
            body=b"{}",
        )


# ---------------------------------------------------------------------------
# Payload parsing
# ---------------------------------------------------------------------------


def test_parse_interaction_ping():
    payload = {"type": INTERACTION_TYPE_PING, "id": "ping-1"}
    event = parse_discord_payload(payload)
    assert event.event_type == EVENT_TYPE_INTERACTION_PING
    assert event.external_id == "ping-1"
    assert event.interaction_type == INTERACTION_TYPE_PING
    assert event.text == ""


def test_parse_interaction_application_command():
    payload = {
        "type": INTERACTION_TYPE_APPLICATION_COMMAND,
        "id": "interaction-42",
        "channel_id": "999",
        "guild_id": "777",
        "member": {"user": {"id": "user-7", "username": "lewis"}},
        "data": {
            "name": "intel",
            "options": [
                {"name": "country", "value": "BR"},
                {"name": "topic", "value": "energy"},
            ],
        },
    }
    event = parse_discord_payload(payload)
    assert event.event_type == EVENT_TYPE_APPLICATION_COMMAND
    assert event.external_id == "interaction-42"
    assert event.channel == "999"
    assert event.guild_id == "777"
    assert event.author == "user-7"
    assert event.text == "/intel country=BR topic=energy"


def test_parse_message_component():
    payload = {
        "type": INTERACTION_TYPE_MESSAGE_COMPONENT,
        "id": "comp-1",
        "channel_id": "111",
        "data": {"custom_id": "approve_btn"},
    }
    event = parse_discord_payload(payload)
    assert event.event_type == EVENT_TYPE_MESSAGE_COMPONENT
    assert event.text == "approve_btn"


def test_parse_modal_submit():
    payload = {
        "type": INTERACTION_TYPE_MODAL_SUBMIT,
        "id": "modal-1",
        "data": {"custom_id": "feedback_form"},
    }
    event = parse_discord_payload(payload)
    assert event.event_type == EVENT_TYPE_MODAL_SUBMIT
    assert event.text == "feedback_form"


def test_parse_message_create_event_webhook():
    """Event-webhook shape: top-level "type": "MESSAGE_CREATE" with nested data."""
    payload = {
        "type": "MESSAGE_CREATE",
        "data": {
            "id": "msg-100",
            "channel_id": "ch-1",
            "guild_id": "g-1",
            "author": {"id": "u-1", "username": "user"},
            "content": "hello",
            "timestamp": "2026-05-15T10:00:00+00:00",
        },
    }
    event = parse_discord_payload(payload)
    assert event.event_type == EVENT_TYPE_MESSAGE_CREATE
    assert event.external_id == "msg-100"
    assert event.text == "hello"
    assert event.channel == "ch-1"
    assert event.author == "u-1"
    assert event.guild_id == "g-1"
    assert event.published_at.year == 2026


def test_parse_unknown_shape_falls_back_safely():
    event = parse_discord_payload({"weird": "thing"})
    assert event.event_type == EVENT_TYPE_UNKNOWN
    # external_id is auto-generated when missing.
    assert event.external_id


# ---------------------------------------------------------------------------
# Allowed-event-types filtering
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_allowed_event_types_filters_out_non_whitelisted():
    sk = SigningKey.generate()
    config = DiscordWebhookConfig(
        application_id="app-1",
        public_key_secret="discord.test.public_key",
        allowed_event_types=[EVENT_TYPE_APPLICATION_COMMAND],  # only commands
    )
    handler, captured = _make_handler(public_key=bytes(sk.verify_key), config=config)
    ctx = _make_source_context(source_id="src-filter")

    router = InboundWebhookRouter()
    handler._router = router
    await handler.on_configure(ctx)
    await handler.on_activate(ctx)

    app = FastAPI()
    router.mount(app)
    client = TestClient(app)

    # 1. A non-whitelisted event (MESSAGE_CREATE) → no Signal emitted (204).
    body_drop = json.dumps({
        "type": "MESSAGE_CREATE",
        "data": {"id": "m-drop", "content": "noise"},
    }).encode("utf-8")
    headers_drop = _make_signed_envelope(sk, body=body_drop, timestamp="1715820000")
    resp = client.post(
        f"{WEBHOOK_PREFIX}/{ctx.source_id}",
        content=body_drop,
        headers=headers_drop,
    )
    assert resp.status_code == 204
    assert captured == []

    # 2. A whitelisted event (APPLICATION_COMMAND) → emitted.
    body_keep = json.dumps({
        "type": INTERACTION_TYPE_APPLICATION_COMMAND,
        "id": "i-keep",
        "data": {"name": "ping"},
    }).encode("utf-8")
    headers_keep = _make_signed_envelope(sk, body=body_keep, timestamp="1715820100")
    resp = client.post(
        f"{WEBHOOK_PREFIX}/{ctx.source_id}",
        content=body_keep,
        headers=headers_keep,
    )
    assert resp.status_code == 204, resp.text
    assert len(captured) == 1
    assert captured[0].payload["event_type"] == EVENT_TYPE_APPLICATION_COMMAND
    assert captured[0].payload["external_id"] == "i-keep"


def test_allowed_event_types_validator_rejects_empty_strings():
    with pytest.raises(ValueError):
        DiscordWebhookConfig(
            application_id="x",
            public_key_secret="y",
            allowed_event_types=["", "valid"],
        )


# ---------------------------------------------------------------------------
# Integration — FastAPI TestClient against the shared router
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fastapi_dispatch_emits_signal_on_valid_signature():
    sk = SigningKey.generate()
    handler, captured = _make_handler(public_key=bytes(sk.verify_key))
    ctx = _make_source_context(source_id="src-integration")
    router = InboundWebhookRouter()
    handler._router = router

    await handler.on_configure(ctx)
    await handler.on_activate(ctx)
    assert handler.webhook_path == f"{WEBHOOK_PREFIX}/{ctx.source_id}"
    assert router.is_registered(ctx.source_id)

    app = FastAPI()
    router.mount(app)
    client = TestClient(app)

    body = json.dumps({
        "type": INTERACTION_TYPE_APPLICATION_COMMAND,
        "id": "i-1",
        "channel_id": "c-1",
        "data": {"name": "status"},
        "member": {"user": {"id": "u-1"}},
    }).encode("utf-8")
    headers = _make_signed_envelope(sk, body=body, timestamp="1715900000")

    resp = client.post(
        f"{WEBHOOK_PREFIX}/{ctx.source_id}",
        content=body,
        headers=headers,
    )
    assert resp.status_code == 204, resp.text
    assert len(captured) == 1

    signal = captured[0]
    assert signal.source_id == ctx.source_id
    # Source-first pivot: Signal is target-agnostic — target_id left the schema
    # (it lives only on derived analyst outputs). Handler stamps source_id only.
    assert signal.payload["external_id"] == "i-1"
    assert signal.payload["event_type"] == EVENT_TYPE_APPLICATION_COMMAND
    assert signal.payload["author"] == "u-1"
    assert signal.payload["channel"] == "c-1"
    assert signal.payload["application_id"] == "app-123"
    assert signal.content_hash  # non-empty hash


@pytest.mark.asyncio
async def test_fastapi_dispatch_rejects_invalid_signature():
    sk_correct = SigningKey.generate()
    sk_wrong = SigningKey.generate()
    handler, captured = _make_handler(public_key=bytes(sk_correct.verify_key))
    ctx = _make_source_context(source_id="src-bad-sig")
    router = InboundWebhookRouter()
    handler._router = router

    await handler.on_configure(ctx)
    await handler.on_activate(ctx)

    app = FastAPI()
    router.mount(app)
    client = TestClient(app)

    body = json.dumps({"type": INTERACTION_TYPE_APPLICATION_COMMAND, "id": "x"}).encode()
    headers = _make_signed_envelope(sk_wrong, body=body, timestamp="100")
    resp = client.post(
        f"{WEBHOOK_PREFIX}/{ctx.source_id}",
        content=body,
        headers=headers,
    )
    assert resp.status_code == 401
    assert captured == []


@pytest.mark.asyncio
async def test_fastapi_dispatch_handles_ping_handshake():
    """Discord registration sends an interaction type=1 PING. Handler must
    respond {"type": 1} (PONG) WITHOUT emitting a Signal."""
    sk = SigningKey.generate()
    handler, captured = _make_handler(public_key=bytes(sk.verify_key))
    ctx = _make_source_context(source_id="src-ping")
    router = InboundWebhookRouter()
    handler._router = router

    await handler.on_configure(ctx)
    await handler.on_activate(ctx)

    app = FastAPI()
    router.mount(app)
    client = TestClient(app)

    body = json.dumps({"type": INTERACTION_TYPE_PING, "id": "ping-1"}).encode()
    headers = _make_signed_envelope(sk, body=body, timestamp="200")
    resp = client.post(
        f"{WEBHOOK_PREFIX}/{ctx.source_id}",
        content=body,
        headers=headers,
    )
    assert resp.status_code == 200
    assert resp.json() == {"type": 1}
    assert captured == []  # PING does not produce a Signal


@pytest.mark.asyncio
async def test_fastapi_dispatch_404_for_unknown_source():
    router = InboundWebhookRouter()
    app = FastAPI()
    router.mount(app)
    client = TestClient(app)
    resp = client.post(f"{WEBHOOK_PREFIX}/no-such-source", json={"foo": "bar"})
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_fastapi_dispatch_503_when_paused():
    sk = SigningKey.generate()
    handler, captured = _make_handler(public_key=bytes(sk.verify_key))
    ctx = _make_source_context(source_id="src-paused")
    router = InboundWebhookRouter()
    handler._router = router
    await handler.on_configure(ctx)
    await handler.on_activate(ctx)
    await handler.on_pause(ctx)

    app = FastAPI()
    router.mount(app)
    client = TestClient(app)

    body = json.dumps({"type": INTERACTION_TYPE_APPLICATION_COMMAND, "id": "x"}).encode()
    headers = _make_signed_envelope(sk, body=body, timestamp="300")
    resp = client.post(
        f"{WEBHOOK_PREFIX}/{ctx.source_id}",
        content=body,
        headers=headers,
    )
    assert resp.status_code == 503

    # Resume restores.
    await handler.on_resume(ctx)
    headers = _make_signed_envelope(sk, body=body, timestamp="301")
    resp = client.post(
        f"{WEBHOOK_PREFIX}/{ctx.source_id}",
        content=body,
        headers=headers,
    )
    assert resp.status_code == 204
    assert len(captured) == 1


# ---------------------------------------------------------------------------
# Lifecycle + pull is a no-op
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pull_is_noop_generator():
    sk = SigningKey.generate()
    handler, _ = _make_handler(public_key=bytes(sk.verify_key))
    ctx = _make_source_context()
    await handler.on_configure(ctx)
    # Iterating yields nothing — push-source convention per L-102.
    results = []
    async for sig in handler.pull(ctx, since=None):
        results.append(sig)
    assert results == []


@pytest.mark.asyncio
async def test_on_retire_unregisters_from_router():
    sk = SigningKey.generate()
    handler, _ = _make_handler(public_key=bytes(sk.verify_key))
    ctx = _make_source_context(source_id="src-retire")
    router = InboundWebhookRouter()
    handler._router = router
    await handler.on_configure(ctx)
    await handler.on_activate(ctx)
    assert router.is_registered(ctx.source_id)
    await handler.on_retire(ctx)
    assert not router.is_registered(ctx.source_id)
    assert handler.webhook_path is None


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_health_reports_healthy_when_registered_and_keyed():
    sk = SigningKey.generate()
    handler, _ = _make_handler(public_key=bytes(sk.verify_key))
    ctx = _make_source_context(source_id="src-health-1")
    router = InboundWebhookRouter()
    handler._router = router
    await handler.on_configure(ctx)
    await handler.on_activate(ctx)

    health = await handler.health_check(ctx)
    assert health.state == "healthy"
    assert health.detail["public_key_present"] is True
    assert health.detail["registered"] is True
    assert health.detail["webhook_path"] == f"{WEBHOOK_PREFIX}/{ctx.source_id}"


@pytest.mark.asyncio
async def test_health_reports_unhealthy_without_key():
    """Before on_configure, no public key resolved → unhealthy."""

    async def _failing_resolver(_: str) -> bytes:
        raise RuntimeError("vault unreachable")

    cfg = DiscordWebhookConfig(
        application_id="app",
        public_key_secret="discord.test.public_key",
    )
    handler = DiscordWebhookSourceHandler(
        cfg,
        secret_resolver=_failing_resolver,
        webhook_router=InboundWebhookRouter(),
        emit_signal=None,
    )
    ctx = _make_source_context(source_id="src-health-2")
    health = await handler.health_check(ctx)
    assert health.state == "unhealthy"
    assert health.detail["public_key_present"] is False


@pytest.mark.asyncio
async def test_health_reports_degraded_when_key_present_but_unregistered():
    sk = SigningKey.generate()
    handler, _ = _make_handler(public_key=bytes(sk.verify_key))
    ctx = _make_source_context(source_id="src-health-3")
    router = InboundWebhookRouter()
    handler._router = router
    await handler.on_configure(ctx)
    # Note: NOT on_activate — key resolved but not registered.
    health = await handler.health_check(ctx)
    assert health.state == "degraded"


@pytest.mark.asyncio
async def test_verify_test_envelope_self_check():
    """Operator-facing self-check: did the vault hold the right key?"""
    sk = SigningKey.generate()
    handler, _ = _make_handler(public_key=bytes(sk.verify_key))
    ctx = _make_source_context()
    await handler.on_configure(ctx)

    body = b'{"type": 1}'
    ts = "12345"
    sig = sk.sign(ts.encode() + body).signature
    assert handler._verify_test_envelope(
        signature_hex=sig.hex(), timestamp=ts, body=body
    )

    # Tampered → False, never raises.
    assert not handler._verify_test_envelope(
        signature_hex=sig.hex(), timestamp="99999", body=body
    )
    assert not handler._verify_test_envelope(
        signature_hex="zz" * 64, timestamp=ts, body=body
    )


# ---------------------------------------------------------------------------
# Webhook router unit coverage
# ---------------------------------------------------------------------------


def test_webhook_router_register_unregister_lifecycle():
    router = InboundWebhookRouter()

    class _Stub:
        source_id = "stub-source"

        async def handle_webhook(self, request):  # pragma: no cover - not called
            from fastapi import Response
            return Response(status_code=204)

    h = _Stub()
    path = router.register_handler(h)
    assert path == f"{WEBHOOK_PREFIX}/stub-source"
    assert router.is_registered("stub-source")
    assert "stub-source" in router.registered_source_ids()
    assert router.unregister_handler("stub-source") is True
    assert router.unregister_handler("stub-source") is False
    assert not router.is_registered("stub-source")


def test_webhook_router_rejects_unsafe_source_ids():
    router = InboundWebhookRouter()

    class _Stub:
        source_id = "bad/id"

        async def handle_webhook(self, request):  # pragma: no cover
            raise AssertionError("unreachable")

    with pytest.raises(ValueError):
        router.register_handler(_Stub())

    class _Empty:
        source_id = ""

        async def handle_webhook(self, request):  # pragma: no cover
            raise AssertionError("unreachable")

    with pytest.raises(ValueError):
        router.register_handler(_Empty())


def test_webhook_router_list_endpoint():
    router = InboundWebhookRouter()
    app = FastAPI()
    router.mount(app)
    client = TestClient(app)
    resp = client.get(WEBHOOK_PREFIX)
    assert resp.status_code == 200
    body = resp.json()
    assert body["prefix"] == WEBHOOK_PREFIX
    assert body["registered"] == []


def test_webhook_router_healthz_endpoint():
    router = InboundWebhookRouter()
    app = FastAPI()
    router.mount(app)
    client = TestClient(app)
    resp = client.get(f"{WEBHOOK_PREFIX}/no-such/healthz")
    assert resp.status_code == 200
    assert resp.json()["registered"] is False
