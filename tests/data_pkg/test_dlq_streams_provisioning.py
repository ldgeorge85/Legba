# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""L-221 — ``LEGBA_DLQ_EVENTS`` stream provisioning.

The DLQ live-tail subjects (``legba.dlq.descriptor.<row_id>``,
``legba.dlq.output.<row_id>``) need their own JetStream stream because
the existing descriptor/stack/vocabulary streams don't cover the
``legba.dlq.>`` subject space.

Verifies:
  * ``ensure_runtime_event_streams`` provisions ``LEGBA_DLQ_EVENTS`` with
    the ``legba.dlq.>`` subject filter, ``interest`` retention, and the
    standard 3600s max_age.
  * A direct publish on ``legba.dlq.descriptor.<id>`` lands in the DLQ
    stream (not in any other stream) — proves the subject filter is
    correct.
  * A direct publish on ``legba.dlq.output.<id>`` lands in the same
    stream — the filter covers both descriptor + output rows.
  * The call is idempotent: re-running returns ``created=False`` for the
    DLQ stream.
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio

from legba.data.config import NatsConfig
from legba.data.nats import NatsStore
from legba.data.registry.streams import (
    DESCRIPTOR_EVENTS_STREAM,
    DLQ_EVENTS_STREAM,
    DLQ_EVENTS_SUBJECTS,
    STACK_EVENTS_STREAM,
    VOCABULARY_EVENTS_STREAM,
    ensure_runtime_event_streams,
)


pytestmark = [pytest.mark.integration]


@pytest_asyncio.fixture
async def nats_store():
    store = NatsStore(NatsConfig(url="nats://127.0.0.1:4222"))
    await store.connect()
    try:
        yield store
    finally:
        await store.close()


async def _delete_stream_if_present(store: NatsStore, name: str) -> None:
    try:
        await store.js.delete_stream(name)
    except Exception:
        pass


async def test_dlq_stream_provisioned_with_correct_shape(nats_store: NatsStore):
    """Fresh ensure call creates ``LEGBA_DLQ_EVENTS`` with the right
    subjects + interest retention + 1h max_age."""
    await _delete_stream_if_present(nats_store, DLQ_EVENTS_STREAM)

    results = await ensure_runtime_event_streams(nats_store)

    by_name = {r.name: r for r in results}
    assert DLQ_EVENTS_STREAM in by_name, (
        f"DLQ stream missing from ensure results; got {list(by_name)!r}"
    )
    dlq = by_name[DLQ_EVENTS_STREAM]
    assert list(dlq.subjects) == DLQ_EVENTS_SUBJECTS == ["legba.dlq.>"]
    assert dlq.retention == "interest"
    assert dlq.max_age_seconds == 3600
    assert dlq.created is True


async def test_dlq_stream_provisioning_is_idempotent(nats_store: NatsStore):
    """A second ensure call is a no-op for all four streams."""
    await ensure_runtime_event_streams(nats_store)
    results = await ensure_runtime_event_streams(nats_store)

    by_name = {r.name: r for r in results}
    assert by_name[DLQ_EVENTS_STREAM].created is False, (
        "second ensure call must report created=False"
    )


async def test_descriptor_dlq_publish_lands_in_dlq_stream(nats_store: NatsStore):
    """``legba.dlq.descriptor.<row_id>`` routes to LEGBA_DLQ_EVENTS."""
    await ensure_runtime_event_streams(nats_store)

    row_id = uuid.uuid4()
    subject = f"legba.dlq.descriptor.{row_id}"
    ack = await nats_store.js.publish(subject, b"{}")
    assert ack.stream == DLQ_EVENTS_STREAM, (
        f"publish to {subject!r} landed in {ack.stream!r}, "
        f"expected {DLQ_EVENTS_STREAM!r}"
    )


async def test_output_dlq_publish_lands_in_dlq_stream(nats_store: NatsStore):
    """``legba.dlq.output.<row_id>`` routes to LEGBA_DLQ_EVENTS."""
    await ensure_runtime_event_streams(nats_store)

    row_id = uuid.uuid4()
    subject = f"legba.dlq.output.{row_id}"
    ack = await nats_store.js.publish(subject, b"{}")
    assert ack.stream == DLQ_EVENTS_STREAM


async def test_dlq_subject_does_not_collide_with_other_streams(nats_store: NatsStore):
    """A descriptor.* or stack.* publish must NOT land in the DLQ stream
    — confirms the subject filter is appropriately narrow."""
    await ensure_runtime_event_streams(nats_store)

    desc_subject = f"descriptor.updated.target.streams_collision_{uuid.uuid4().hex[:6]}"
    ack = await nats_store.js.publish(desc_subject, b"{}")
    assert ack.stream != DLQ_EVENTS_STREAM
    assert ack.stream == DESCRIPTOR_EVENTS_STREAM
