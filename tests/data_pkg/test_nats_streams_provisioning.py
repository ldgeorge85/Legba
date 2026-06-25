# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Integration tests for the runtime event stream provisioner.

Real NATS JetStream container per ``tests/data_pkg/conftest.py``.

Covers:
  * The three streams exist with the expected subjects + interest retention
    + 1h max_age after ``ensure_runtime_event_streams`` runs.
  * Re-running the helper is idempotent (no error, returns ``created=False``).
  * A descriptor.* publish against the descriptor stream is durably stored
    (proves the subject filter actually matches what the registry emits).
"""

from __future__ import annotations

import asyncio
import json
import uuid

import pytest
import pytest_asyncio

from legba.data.config import NatsConfig
from legba.data.nats import NatsStore
from legba.data.registry.streams import (
    DESCRIPTOR_EVENTS_STREAM,
    DESCRIPTOR_EVENTS_SUBJECTS,
    DLQ_EVENTS_STREAM,
    STACK_EVENTS_STREAM,
    STACK_EVENTS_SUBJECTS,
    VOCABULARY_EVENTS_STREAM,
    VOCABULARY_EVENTS_SUBJECTS,
    ensure_runtime_event_streams,
)


pytestmark = [pytest.mark.integration]


@pytest_asyncio.fixture
async def nats_store():
    """Connected NatsStore against the conftest substrate."""
    store = NatsStore(NatsConfig(url="nats://127.0.0.1:4222"))
    await store.connect()
    try:
        yield store
    finally:
        await store.close()


async def _delete_stream_if_present(store: NatsStore, name: str) -> None:
    """Best-effort cleanup so the test starts from a known state."""
    try:
        await store.js.delete_stream(name)
    except Exception:
        pass


@pytest.fixture(scope="module")
def stream_name_suffix() -> str:
    """Unique per-session suffix so we can run with concurrent test sessions.

    The production stream names are global, but for THIS test we don't
    rename — we instead delete + re-provision the canonical names and
    verify behavior. The 3 streams are shared state across sessions, so
    parallel pytest workers can race. We accept that — the assertions
    only check shape, not exclusive ownership.
    """
    return uuid.uuid4().hex[:6]


async def test_ensure_runtime_event_streams_creates_all_three(nats_store, stream_name_suffix):
    # Start clean — delete any pre-existing streams so we get `created=True`.
    # NOTE: the helper now provisions four streams (L-221 added
    # LEGBA_DLQ_EVENTS); this test still focuses on the original three,
    # but we delete + assert all four so the result-count assertion stays
    # honest.
    for name in (
        DESCRIPTOR_EVENTS_STREAM,
        STACK_EVENTS_STREAM,
        VOCABULARY_EVENTS_STREAM,
        DLQ_EVENTS_STREAM,
    ):
        await _delete_stream_if_present(nats_store, name)

    results = await ensure_runtime_event_streams(nats_store)

    assert len(results) == 4
    by_name = {r.name: r for r in results}
    assert by_name[DESCRIPTOR_EVENTS_STREAM].created is True
    assert by_name[STACK_EVENTS_STREAM].created is True
    assert by_name[VOCABULARY_EVENTS_STREAM].created is True
    assert by_name[DLQ_EVENTS_STREAM].created is True

    # Subject + retention shape per the spec.
    desc = by_name[DESCRIPTOR_EVENTS_STREAM]
    assert list(desc.subjects) == DESCRIPTOR_EVENTS_SUBJECTS
    assert desc.retention == "interest"
    assert desc.max_age_seconds == 3600

    stack = by_name[STACK_EVENTS_STREAM]
    assert list(stack.subjects) == STACK_EVENTS_SUBJECTS

    vocab = by_name[VOCABULARY_EVENTS_STREAM]
    assert list(vocab.subjects) == VOCABULARY_EVENTS_SUBJECTS


async def test_ensure_runtime_event_streams_is_idempotent(nats_store):
    # First call may create or no-op depending on prior state; second call
    # MUST return created=False for all three.
    await ensure_runtime_event_streams(nats_store)
    results = await ensure_runtime_event_streams(nats_store)
    assert all(r.created is False for r in results), [
        (r.name, r.created) for r in results
    ]


async def test_descriptor_publish_lands_in_stream(nats_store):
    """A descriptor.<...> publish gets durably stored — i.e. the stream's
    subject filter actually matches the registry's emit pattern."""
    await ensure_runtime_event_streams(nats_store)

    # Use a unique descriptor_id so concurrent sessions don't see each
    # other's messages.
    desc_id = f"streams_test_{uuid.uuid4().hex[:10]}"
    subject = f"descriptor.updated.target.{desc_id}"

    pub_ack = await nats_store.js.publish(
        subject, json.dumps({"descriptor_id": desc_id}).encode("utf-8"),
    )
    # PubAck.stream tells us which stream the message landed in.
    assert pub_ack.stream == DESCRIPTOR_EVENTS_STREAM, (
        f"publish to {subject!r} landed in {pub_ack.stream!r} "
        f"(expected {DESCRIPTOR_EVENTS_STREAM!r}) — subject filter wrong"
    )


async def test_stack_and_vocabulary_subjects_route_correctly(nats_store):
    """Cross-stream isolation — stack publishes don't land in descriptor
    stream and vice versa."""
    await ensure_runtime_event_streams(nats_store)

    suffix = uuid.uuid4().hex[:8]
    stack_subj = f"stack.component.updated.nats.cluster_{suffix}"
    vocab_subj = f"vocabulary.updated.target_{suffix}"

    ack_stack = await nats_store.js.publish(stack_subj, b"{}")
    ack_vocab = await nats_store.js.publish(vocab_subj, b"{}")

    assert ack_stack.stream == STACK_EVENTS_STREAM
    assert ack_vocab.stream == VOCABULARY_EVENTS_STREAM
