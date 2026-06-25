# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Integration tests for L-124 — NATSClusterHandler.

Real NATS JetStream container per `tests/data_pkg/conftest.py` (started
by `_ensure_containers_up`). Test resources use a unique per-session
prefix (`legba_test_<uuid>_*`) so concurrent test sessions never collide
and so leftover state from a crashed run is easy to identify.

Covers:
  * Lifecycle: configure -> activate -> pause -> retire transitions.
  * Stream / consumer idempotency (re-ensure does not error).
  * Publish + pull_subscribe roundtrip (10 messages, ack all, fetch again returns nothing).
  * Push subscribe + drain on pause.
  * KV roundtrip: put / get / delete; idempotent delete.
  * Healthcheck: streams_info + account_info paths.
  * Per-target / per-analyst stream name helpers.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid

import pytest

from legba.data.schemas.properties import Property
from legba.data.schemas.stack import NATSClusterConfig
from legba.data.stack.nats import (
    ConfigureContext,
    HandlerHealth,
    NATSClusterHandler,
    RuntimeContext,
    SubscriptionHandle,
    analyst_stream_name,
    target_stream_name,
)
from legba.data.stack.nats.jetstream import (
    analyst_subject_prefix,
    target_subject_prefix,
)


pytestmark = [pytest.mark.integration]


# ---------------------------------------------------------------------------
# Session-scoped fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def session_prefix() -> str:
    """Per-session unique prefix for stream / consumer / KV names."""
    return f"legba_test_{uuid.uuid4().hex[:10]}"


def _make_config() -> NATSClusterConfig:
    """Build a NATSClusterConfig pointing at the conftest-started container."""
    return NATSClusterConfig(
        servers=Property.List(raw=["nats://127.0.0.1:4222"], item_kind="text"),
        credentials=None,
        jetstream=Property.Dropdown.Static.of("enabled", ["enabled", "disabled"]),
    )


async def _new_handler(instance_id: str) -> NATSClusterHandler:
    """Helper: build + configure + activate a handler against the live cluster."""
    handler = NATSClusterHandler()
    cfg = _make_config()
    cctx = ConfigureContext(
        instance_id=instance_id,
        instance_version="test",
        config=cfg,
        logger=logging.getLogger("test_stack_nats"),
    )
    await handler.on_configure(cctx)
    rctx = RuntimeContext(
        instance_id=instance_id,
        instance_version="test",
        config=cfg,
        logger=logging.getLogger("test_stack_nats"),
    )
    await handler.on_activate(rctx)
    return handler


async def _close_handler(handler: NATSClusterHandler) -> None:
    rctx = RuntimeContext(
        instance_id=handler.instance_id,
        instance_version="test",
        config=None,
        logger=logging.getLogger("test_stack_nats"),
    )
    await handler.on_retire(rctx)


# ---------------------------------------------------------------------------
# Naming helpers
# ---------------------------------------------------------------------------


def test_target_stream_name_convention():
    # Stream names use '_' (NATS rejects '.' in stream names).
    assert target_stream_name("brazil") == "legba_target_brazil_signals"
    assert (
        analyst_stream_name("country_summary")
        == "legba_analyst_country_summary_findings"
    )
    # Subject prefixes use '.' (NATS subject convention).
    assert target_subject_prefix("brazil") == "legba.target.brazil.signals"
    assert (
        analyst_subject_prefix("country_summary")
        == "legba.analyst.country_summary.findings"
    )


def test_stream_name_rejects_disallowed_chars():
    with pytest.raises(ValueError):
        target_stream_name("bad id")
    with pytest.raises(ValueError):
        analyst_stream_name("foo.bar")
    with pytest.raises(ValueError):
        target_stream_name("")
    with pytest.raises(ValueError):
        target_stream_name("with*star")
    with pytest.raises(ValueError):
        analyst_stream_name("with>gt")


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lifecycle_configure_activate_retire(session_prefix: str):
    handler = NATSClusterHandler()
    cfg = _make_config()
    instance_id = f"{session_prefix}_lifecycle"
    cctx = ConfigureContext(
        instance_id=instance_id, config=cfg,
        logger=logging.getLogger("test_stack_nats"),
    )
    await handler.on_configure(cctx)
    assert handler.lifecycle_state == "configured"

    rctx = RuntimeContext(
        instance_id=instance_id, config=cfg,
        logger=logging.getLogger("test_stack_nats"),
    )
    await handler.on_activate(rctx)
    assert handler.lifecycle_state == "active"

    await handler.on_pause(rctx)
    assert handler.lifecycle_state == "paused"

    await handler.on_resume(rctx)
    assert handler.lifecycle_state == "active"

    await handler.on_retire(rctx)
    assert handler.lifecycle_state == "retired"


# ---------------------------------------------------------------------------
# Stream + consumer + pull roundtrip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ensure_stream_publish_pull_consumer_roundtrip(session_prefix: str):
    handler = await _new_handler(f"{session_prefix}_pull")
    stream = f"{session_prefix}_stream"
    subject_root = f"{session_prefix}_subj"
    subject = f"{subject_root}.events"
    durable = f"{session_prefix}_dur"

    try:
        created = await handler.ensure_stream(
            stream, [f"{subject_root}.>"], max_age_seconds=300,
        )
        assert created is True

        # Idempotent re-ensure must not raise and must report False.
        created_again = await handler.ensure_stream(
            stream, [f"{subject_root}.>"], max_age_seconds=300,
        )
        assert created_again is False

        # Publish 10 messages with JetStream ack.
        for i in range(10):
            ack = await handler.publish(
                subject,
                {"i": i, "msg": f"hello-{i}"},
                headers={"x-test-prefix": session_prefix},
            )
            assert ack.stream == stream
            assert ack.seq > 0

        # Idempotent consumer.
        consumer_created = await handler.ensure_consumer(
            stream, durable, filter_subject=f"{subject_root}.>",
            ack_wait_seconds=30, max_deliver=5,
        )
        assert consumer_created is True
        consumer_again = await handler.ensure_consumer(
            stream, durable, filter_subject=f"{subject_root}.>",
            ack_wait_seconds=30, max_deliver=5,
        )
        assert consumer_again is False

        # Pull subscribe and fetch all 10.
        sub_handle = await handler.pull_subscribe(stream, durable)
        assert isinstance(sub_handle, SubscriptionHandle)

        received: list[int] = []
        msgs = await sub_handle.sub.fetch(10, timeout=5)
        for m in msgs:
            data = json.loads(m.data.decode("utf-8"))
            received.append(int(data["i"]))
            await m.ack()
        assert sorted(received) == list(range(10))

        # After ack, a second fetch must time out (no redelivery).
        with pytest.raises(Exception):
            await sub_handle.sub.fetch(1, timeout=1)

        # Healthcheck must see at least our stream.
        health = await handler.health_check()
        assert isinstance(health, HandlerHealth)
        assert health.state == "healthy"
        assert health.detail["streams_count"] >= 1
        assert stream in health.detail["streams"]

        # streams_info gives back our stream as a dict.
        infos = await handler.streams_info()
        names = {info["name"] for info in infos}
        assert stream in names

        # account_info returns the expected keys.
        acct = await handler.account_info()
        assert "memory" in acct and "storage" in acct
    finally:
        # Drop the stream so the test is self-cleaning.
        try:
            await handler.store.js.delete_stream(stream)
        except Exception:
            pass
        await _close_handler(handler)


# ---------------------------------------------------------------------------
# Push subscribe + pause-drain
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_push_subscribe_and_pause_drain(session_prefix: str):
    handler = await _new_handler(f"{session_prefix}_push")
    stream = f"{session_prefix}_push_stream"
    subject_root = f"{session_prefix}_pushsubj"
    # Push subscribe creates its own ephemeral push consumer with a
    # deliver_subject — we don't pre-create a durable here (durables for
    # push need an explicit deliver_subject; pull is the durable path).
    durable = f"{session_prefix}_push_dur"

    try:
        await handler.ensure_stream(stream, [f"{subject_root}.>"])

        received: list[bytes] = []
        ready = asyncio.Event()

        async def cb(msg):
            received.append(msg.data)
            await msg.ack()
            if len(received) >= 3:
                ready.set()

        # nats-py creates the push consumer (with deliver_subject) on demand.
        sub_handle = await handler.subscribe(
            f"{subject_root}.>", cb, stream=stream, durable=durable,
        )
        assert sub_handle.kind == "push"
        assert sub_handle in handler.subscriptions

        for i in range(3):
            await handler.publish(f"{subject_root}.evt", f"payload-{i}")

        # Wait for the dispatcher to flush before pausing.
        await asyncio.wait_for(ready.wait(), timeout=5)

        # Pause drains in-flight + clears the tracker.
        rctx = RuntimeContext(
            instance_id=handler.instance_id, config=None,
            logger=logging.getLogger("test_stack_nats"),
        )
        await handler.on_pause(rctx)
        assert handler.lifecycle_state == "paused"
        assert handler.subscriptions == []
        assert len(received) == 3
    finally:
        try:
            await handler.store.js.delete_stream(stream)
        except Exception:
            pass
        await _close_handler(handler)


# ---------------------------------------------------------------------------
# KV roundtrip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_kv_roundtrip(session_prefix: str):
    handler = await _new_handler(f"{session_prefix}_kv")
    bucket = f"{session_prefix}_bucket"

    try:
        created = await handler.ensure_kv_bucket(bucket, history=3)
        assert created is True

        # Idempotent ensure.
        again = await handler.ensure_kv_bucket(bucket, history=3)
        assert again is False

        # Put / get bytes.
        rev1 = await handler.kv_put(bucket, "alpha", b"first")
        assert rev1 >= 1
        got = await handler.kv_get(bucket, "alpha")
        assert got == b"first"

        # Put / get string.
        await handler.kv_put(bucket, "beta", "second")
        assert await handler.kv_get(bucket, "beta") == b"second"

        # Put / get dict (json-encoded).
        await handler.kv_put(bucket, "gamma", {"x": 1, "y": [2, 3]})
        gamma = await handler.kv_get(bucket, "gamma")
        assert json.loads(gamma.decode("utf-8")) == {"x": 1, "y": [2, 3]}

        # Missing key.
        assert await handler.kv_get(bucket, "missing") is None

        # Delete + verify gone.
        assert await handler.kv_delete(bucket, "alpha") is True
        assert await handler.kv_get(bucket, "alpha") is None

        # Idempotent delete of an already-missing key is still True.
        assert await handler.kv_delete(bucket, "alpha") is True
    finally:
        # Clean up the bucket.
        try:
            await handler.store.js.delete_key_value(bucket)
        except Exception:
            pass
        await _close_handler(handler)


# ---------------------------------------------------------------------------
# Healthcheck before any stream exists
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_healthcheck_paths(session_prefix: str):
    """Pre-config health is unhealthy; post-activate it succeeds."""
    handler = NATSClusterHandler()
    # Unconfigured -> unhealthy.
    pre_health = await handler.health_check()
    assert pre_health.state == "unhealthy"
    assert pre_health.last_error

    # Configure + activate then probe.
    handler = await _new_handler(f"{session_prefix}_health")
    try:
        h = await handler.health_check()
        # State is either healthy or degraded depending on whether other
        # streams happen to exist in this nats account.
        assert h.state in ("healthy", "degraded")
        assert "streams_count" in h.detail
        assert "memory" in h.detail
    finally:
        await _close_handler(handler)


# ---------------------------------------------------------------------------
# Per-target stream creation via the naming helper
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_per_target_stream_creation(session_prefix: str):
    handler = await _new_handler(f"{session_prefix}_target")
    # Use a session-unique target id so we don't pollute the namespace.
    target_id = f"{session_prefix}_brazil"
    stream_name = target_stream_name(target_id)
    subject_prefix = target_subject_prefix(target_id)

    try:
        created = await handler.ensure_stream(
            stream_name, [f"{subject_prefix}.>"],
        )
        assert created is True

        ack = await handler.publish(
            f"{subject_prefix}.energy", {"event": "test"},
        )
        assert ack.stream == stream_name

        infos = await handler.streams_info()
        assert any(i["name"] == stream_name for i in infos)
    finally:
        try:
            await handler.store.js.delete_stream(stream_name)
        except Exception:
            pass
        await _close_handler(handler)
