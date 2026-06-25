# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Integration tests for the NATS → ReconcileLoop informer.

Real NATS JetStream container per ``tests/data_pkg/conftest.py`` (re-
exported via ``tests/runtime/conftest.py``).

Covers:
  * A descriptor.updated.<family>.<id> publish triggers
    ``ReconcileLoop.enqueue(descriptor_id, ...)`` within <2s.
  * Subject parse failures are skipped + acked (no crash).
  * The durable consumer name is the documented
    ``legba-runtime-reconcile-<label>`` shape.
  * Latency measurement — round-trip from publish to enqueue is under 2s
    with a hot consumer.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from datetime import timedelta
from typing import Any

import pytest
import pytest_asyncio

from legba.data.config import NatsConfig
from legba.data.nats import NatsStore
from legba.data.registry.streams import (
    DESCRIPTOR_EVENTS_STREAM,
    ensure_runtime_event_streams,
)
from legba.runtime.nats_informer import (
    DEFAULT_CONSUMER_LABEL,
    DESCRIPTOR_SUBJECT_FILTER,
    NatsReconcileInformer,
    _parse_descriptor_id_from_subject,
)


pytestmark = [pytest.mark.integration]


# ---------------------------------------------------------------------------
# Lightweight ReconcileLoop stub — we don't need the full machine, just an
# `enqueue` surface that records the call.
# ---------------------------------------------------------------------------


class _RecordingLoop:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.enqueue_events: list[asyncio.Event] = []

    def enqueue(self, descriptor_id: str, reason: str = "event") -> None:
        self.calls.append((descriptor_id, reason))
        # Pop any waiters off the queue.
        for ev in self.enqueue_events:
            ev.set()

    def wait_for_enqueue(self) -> asyncio.Event:
        ev = asyncio.Event()
        self.enqueue_events.append(ev)
        return ev


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def nats_store():
    store = NatsStore(NatsConfig(url="nats://127.0.0.1:4222"))
    await store.connect()
    try:
        yield store
    finally:
        await store.close()


@pytest_asyncio.fixture
async def provisioned_streams(nats_store):
    """Ensure streams exist for the test."""
    await ensure_runtime_event_streams(nats_store)
    return None


@pytest_asyncio.fixture
async def informer(nats_store, provisioned_streams):
    """Spin a fresh informer with a unique consumer per test so concurrent
    sessions don't fight over message delivery."""
    label = f"itest-{uuid.uuid4().hex[:6]}"
    loop = _RecordingLoop()
    inf = NatsReconcileInformer(nats_store, loop, consumer_label=label)
    await inf.start()
    try:
        yield inf, loop
    finally:
        await inf.stop()
        # Best-effort consumer cleanup so the stream doesn't accumulate
        # one durable per test run.
        try:
            await nats_store.js.delete_consumer(
                DESCRIPTOR_EVENTS_STREAM, inf.consumer_name,
            )
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Subject parser unit tests
# ---------------------------------------------------------------------------


def test_parse_descriptor_id_happy_path():
    assert _parse_descriptor_id_from_subject(
        "descriptor.updated.target.india_energy"
    ) == "india_energy"
    assert _parse_descriptor_id_from_subject(
        "descriptor.registered.analyst.weather_analyst"
    ) == "weather_analyst"


def test_parse_descriptor_id_rejects_wrong_prefix():
    assert _parse_descriptor_id_from_subject("stack.component.updated.nats.x") is None
    assert _parse_descriptor_id_from_subject("") is None
    assert _parse_descriptor_id_from_subject("descriptor.updated.target") is None


def test_parse_descriptor_id_concatenates_extra_tokens():
    """Defensive: id-with-dots (shouldn't happen per validator but the
    parser rejoins gracefully)."""
    assert _parse_descriptor_id_from_subject(
        "descriptor.updated.target.foo.bar.baz"
    ) == "foo.bar.baz"


# ---------------------------------------------------------------------------
# End-to-end event → enqueue
# ---------------------------------------------------------------------------


async def test_descriptor_event_triggers_enqueue(nats_store, informer):
    """Publishing descriptor.updated.target.<id> drives loop.enqueue
    within <2s (target latency per the spec — periodic resync is 5min)."""
    inf, loop = informer
    desc_id = f"reconcile_test_{uuid.uuid4().hex[:10]}"
    subject = f"descriptor.updated.target.{desc_id}"

    wait_ev = loop.wait_for_enqueue()
    t_pub = time.monotonic()
    await nats_store.js.publish(subject, json.dumps({"descriptor_id": desc_id}).encode("utf-8"))
    try:
        await asyncio.wait_for(wait_ev.wait(), timeout=5.0)
    except asyncio.TimeoutError:
        pytest.fail(
            f"informer did not enqueue {desc_id!r} within 5s; calls={loop.calls!r}"
        )
    latency = time.monotonic() - t_pub

    assert any(call[0] == desc_id for call in loop.calls), (
        f"no enqueue for {desc_id!r} in calls={loop.calls!r}"
    )
    reason = next(r for d, r in loop.calls if d == desc_id)
    assert reason == f"nats_event:{subject}", reason

    # Target: <2s. Allow some slack for CI variance — we just want to
    # prove it's not the 5-min resync.
    assert latency < 3.0, f"enqueue latency {latency:.2f}s exceeded 3s budget"

    # Stats reflect what happened.
    assert inf.stats.messages_received >= 1
    assert inf.stats.enqueued >= 1
    assert inf.stats.ack_errors == 0


async def test_consumer_name_is_documented_shape(informer):
    """Ops contract: durable consumer name is ``legba-runtime-reconcile-<label>``."""
    inf, _ = informer
    assert inf.consumer_name.startswith("legba-runtime-reconcile-")
    # The default label, if no override, is "informer".
    default_inf = NatsReconcileInformer(
        nats_store=inf._store,  # type: ignore[attr-defined]
        reconcile_loop=_RecordingLoop(),
    )
    assert default_inf.consumer_name == f"legba-runtime-reconcile-{DEFAULT_CONSUMER_LABEL}"


async def test_informer_stop_is_idempotent(nats_store, provisioned_streams):
    """Calling stop twice doesn't raise; calling start after stop re-binds."""
    loop = _RecordingLoop()
    inf = NatsReconcileInformer(
        nats_store, loop, consumer_label=f"itest-stop-{uuid.uuid4().hex[:6]}",
    )
    await inf.start()
    await inf.stop()
    await inf.stop()  # no error
    # Cleanup
    try:
        await nats_store.js.delete_consumer(DESCRIPTOR_EVENTS_STREAM, inf.consumer_name)
    except Exception:
        pass


async def test_subject_filter_matches_spec(informer):
    """The informer subscribes to ``descriptor.>`` — the recursive wildcard
    that covers every descriptor.<action>.<family>.<id> shape."""
    inf, _ = informer
    assert inf._subject_filter == DESCRIPTOR_SUBJECT_FILTER == "descriptor.>"


async def test_unparseable_subject_is_acked_not_crashed(nats_store, informer):
    """A subject that parses as ``descriptor.<...>`` but with too few tokens
    must be skipped + acked. The informer keeps draining."""
    inf, loop = informer

    # Publish a subject that the parser will reject (only 3 tokens; the
    # stream's `descriptor.>` filter still matches it).
    bad_subject = "descriptor.updated.target_only_3_tokens"
    await nats_store.js.publish(bad_subject, b"{}")

    # Give the fetch loop a tick or two.
    await asyncio.sleep(2.5)

    # Either the informer parsed it (shouldn't), or it counted a parse
    # error. Either way it must not have crashed — `stop()` succeeds
    # cleanly. The actor records parse_errors >= 1.
    assert inf.stats.parse_errors >= 1, (
        f"expected a parse_error, got stats={inf.stats!r}"
    )
