# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""L-221 — DLQ → NATS live-tail routing.

Verifies that an insert into ``descriptor_dead_letter`` (via
:class:`DescriptorDeadLetter.record`) fires a JetStream event on
``legba.dlq.descriptor.{row_id}``, and that an insert into
``output_dead_letter`` (via :func:`route_to_output_dead_letter`) fires on
``legba.dlq.output.{row_id}``.

Both tests run against the real NATS JetStream container per
``tests/data_pkg/conftest.py``. The descriptor side uses the registry's
:class:`NATSEventEmitter`; the output side uses a direct ``nats_publish``
closure shaped like :attr:`StandardDeps.nats_publish`.

The DLQ stream ``LEGBA_DLQ_EVENTS`` must be provisioned for the publishes
to land — both tests call :func:`ensure_runtime_event_streams` in their
setup. That call is idempotent so concurrent test sessions don't fight.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

import pytest
import pytest_asyncio

from legba.data.config import NatsConfig, PostgresConfig
from legba.data.nats import NatsStore
from legba.data.postgres import PostgresStore
from legba.data.provenance.dlq import (
    dlq_output_row_subject,
    route_to_output_dead_letter,
)
from legba.data.registry.dlq import DescriptorDeadLetter, dlq_descriptor_row_subject
from legba.data.registry.emitter import NATSEventEmitter
from legba.data.registry.streams import (
    DLQ_EVENTS_STREAM,
    ensure_runtime_event_streams,
)


pytestmark = [pytest.mark.integration]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def nats_store():
    """Connected NatsStore against the conftest substrate."""
    store = NatsStore(NatsConfig(url="nats://127.0.0.1:4222"))
    await store.connect()
    try:
        # Ensure the DLQ stream exists — the test substrate may not have it
        # provisioned yet (the registry-server lifespan would normally do
        # this on app startup; we don't bring the whole app up here).
        await ensure_runtime_event_streams(store)
        yield store
    finally:
        await store.close()


@pytest_asyncio.fixture
async def pg_store(migrated_pg: PostgresConfig) -> PostgresStore:
    """Function-scoped Postgres store with a fresh DLQ table."""
    s = PostgresStore(migrated_pg)
    await s.connect()
    async with s.acquire() as conn:
        await conn.execute("TRUNCATE TABLE descriptor_dead_letter")
        await conn.execute("TRUNCATE TABLE output_dead_letter")
    try:
        yield s
    finally:
        await s.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _subscribe_and_collect(
    nats_store: NatsStore,
    subject_filter: str,
    *,
    consumer_name: str,
) -> "tuple[Any, list[dict[str, Any]]]":
    """Subscribe ephemerally to a subject filter via JetStream pull mode.

    Returns ``(psub, collected)`` where ``collected`` is a list that the
    caller is expected to populate by polling ``psub.fetch(...)`` after
    the publish under test has run.
    """
    psub = await nats_store.js.pull_subscribe(
        subject_filter,
        durable=consumer_name,
        stream=DLQ_EVENTS_STREAM,
    )
    return psub, []


async def _drain(psub, max_msgs: int = 8, timeout: float = 3.0) -> list[dict[str, Any]]:
    """Fetch up to ``max_msgs`` from a pull subscription, decoding JSON."""
    out: list[dict[str, Any]] = []
    try:
        msgs = await psub.fetch(max_msgs, timeout=timeout)
    except asyncio.TimeoutError:
        return out
    for m in msgs:
        try:
            out.append(json.loads(m.data.decode("utf-8")))
        except Exception:
            out.append({"_raw": m.data})
        await m.ack()
    return out


# ---------------------------------------------------------------------------
# Descriptor DLQ → legba.dlq.descriptor.{id}
# ---------------------------------------------------------------------------


async def test_descriptor_dlq_insert_emits_nats_event(
    pg_store: PostgresStore,
    nats_store: NatsStore,
):
    """A successful DLQ insert publishes a per-row JetStream event."""
    emitter = NATSEventEmitter(nats_store)
    dlq = DescriptorDeadLetter(pg_store, emitter=emitter)

    # Subscribe BEFORE publishing so the consumer captures the event.
    consumer_name = f"test_desc_{uuid.uuid4().hex[:8]}"
    psub, _ = await _subscribe_and_collect(
        nats_store, "legba.dlq.descriptor.>", consumer_name=consumer_name,
    )

    descriptor_id = f"target_dlq_test_{uuid.uuid4().hex[:8]}"
    entry = await dlq.record(
        actor="test-operator",
        namespace="target",
        attempted_payload={
            "descriptor_id": descriptor_id,
            "schema_uri": "schema://target/v1",
            "junk_field": "would_fail_validation",
        },
        validation_error={
            "rendered": "1 validation error for TargetDescriptor\nname: field required",
            "errors": [{"loc": ["name"], "msg": "field required", "type": "value_error.missing"}],
            "model": "TargetDescriptor",
        },
        declared_schema_uri="schema://target/v1",
    )

    # The row landed in Postgres.
    async with pg_store.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, namespace FROM descriptor_dead_letter WHERE id = $1",
            entry.id,
        )
    assert row is not None
    assert row["namespace"] == "target"

    # The NATS event lands.
    collected = await _drain(psub, max_msgs=4, timeout=3.0)
    matching = [m for m in collected if m.get("id") == str(entry.id)]
    assert matching, (
        f"no live-tail event seen for DLQ row {entry.id}; "
        f"collected={collected}"
    )
    event = matching[0]
    assert event["namespace"] == "target"
    assert event["descriptor_id"] == descriptor_id
    # The reason summary is the first line of the rendered ValidationError;
    # the structured details live in the Postgres row.
    assert "validation error" in event["reason"].lower()
    assert event["actor"] == "test-operator"
    assert event["declared_schema_uri"] == "schema://target/v1"


async def test_descriptor_dlq_subject_keyed_by_row_id(
    pg_store: PostgresStore,
    nats_store: NatsStore,
):
    """The subject is the row id, not the descriptor id — confirms the
    panel can match an event to a specific DLQ row even if multiple
    rows share the same upstream descriptor id."""
    emitter = NATSEventEmitter(nats_store)
    dlq = DescriptorDeadLetter(pg_store, emitter=emitter)

    consumer_name = f"test_desc_subj_{uuid.uuid4().hex[:8]}"
    psub = await nats_store.js.pull_subscribe(
        "legba.dlq.descriptor.>",
        durable=consumer_name,
        stream=DLQ_EVENTS_STREAM,
    )

    entry = await dlq.record(
        actor="op",
        namespace="analyst",
        attempted_payload={"descriptor_id": "x"},
        validation_error={"rendered": "boom"},
    )

    expected_subject = dlq_descriptor_row_subject(entry.id)

    msgs = await psub.fetch(4, timeout=3.0)
    subjects = [m.subject for m in msgs]
    for m in msgs:
        await m.ack()
    assert expected_subject in subjects, (
        f"expected subject {expected_subject!r} not in {subjects!r}"
    )


async def test_descriptor_dlq_no_emitter_is_backward_compatible(
    pg_store: PostgresStore,
):
    """A DLQ writer without an emitter still inserts the row — no NATS
    publish, no exception, no behavior change for legacy callers."""
    dlq = DescriptorDeadLetter(pg_store)  # no emitter kwarg

    entry = await dlq.record(
        actor="legacy-caller",
        namespace="wiring",
        attempted_payload={"descriptor_id": "w"},
        validation_error={"rendered": "legacy err"},
    )

    async with pg_store.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id FROM descriptor_dead_letter WHERE id = $1", entry.id,
        )
    assert row is not None


# ---------------------------------------------------------------------------
# Output DLQ → legba.dlq.output.{id}
# ---------------------------------------------------------------------------


async def test_output_dlq_insert_emits_nats_event(
    pg_store: PostgresStore,
    nats_store: NatsStore,
):
    """``route_to_output_dead_letter`` with a ``nats_publish`` closure
    fires a JetStream event on ``legba.dlq.output.{row_id}``."""

    # Shape-matches StandardDeps.nats_publish: async (subject, bytes) -> None.
    async def nats_publish(subject: str, payload: bytes) -> None:
        await nats_store.js.publish(subject, payload)

    consumer_name = f"test_output_{uuid.uuid4().hex[:8]}"
    psub = await nats_store.js.pull_subscribe(
        "legba.dlq.output.>",
        durable=consumer_name,
        stream=DLQ_EVENTS_STREAM,
    )

    async with pg_store.acquire() as conn:
        entry = await route_to_output_dead_letter(
            conn,
            analyst_id="test_analyst_a",
            analyst_version="v0.0.1",
            run_id=None,
            declared_schema_uri="schema://output/finding/v1",
            attempted_payload={"bad_field": True},
            error="output payload missing required field 'title'",
            nats_publish=nats_publish,
        )

    # Row landed in Postgres.
    async with pg_store.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, analyst_id, declared_schema_uri FROM output_dead_letter WHERE id = $1",
            entry.id,
        )
    assert row is not None
    assert row["analyst_id"] == "test_analyst_a"

    # NATS event lands on the row-keyed subject.
    msgs = await psub.fetch(4, timeout=3.0)
    decoded = []
    for m in msgs:
        decoded.append((m.subject, json.loads(m.data.decode("utf-8"))))
        await m.ack()

    expected_subject = dlq_output_row_subject(entry.id)
    matching = [(s, p) for (s, p) in decoded if s == expected_subject]
    assert matching, (
        f"no event for {expected_subject!r}; got subjects "
        f"{[s for (s, _) in decoded]!r}"
    )
    _, payload = matching[0]
    assert payload["id"] == str(entry.id)
    assert payload["analyst_id"] == "test_analyst_a"
    assert payload["schema_uri"] == "schema://output/finding/v1"
    # The error was a raw string — the summary takes the first line of
    # whatever "rendered" the helper produced.
    assert "title" in payload["reason"] or "missing" in payload["reason"]


async def test_output_dlq_no_nats_publish_is_backward_compatible(
    pg_store: PostgresStore,
):
    """Legacy callers that omit ``nats_publish`` still insert the row."""
    async with pg_store.acquire() as conn:
        entry = await route_to_output_dead_letter(
            conn,
            analyst_id="legacy_analyst",
            analyst_version="v0",
            run_id=None,
            declared_schema_uri="schema://output/finding/v1",
            attempted_payload={"x": 1},
            error="something",
        )

    async with pg_store.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id FROM output_dead_letter WHERE id = $1", entry.id,
        )
    assert row is not None


async def test_output_dlq_publish_failure_does_not_break_insert(
    pg_store: PostgresStore,
):
    """A misbehaving ``nats_publish`` closure must not roll back the
    insert — the DLQ row is the source of truth."""

    async def broken_publish(subject: str, payload: bytes) -> None:
        raise RuntimeError("nats is down")

    async with pg_store.acquire() as conn:
        entry = await route_to_output_dead_letter(
            conn,
            analyst_id="resilient_analyst",
            analyst_version="v0",
            run_id=None,
            declared_schema_uri="schema://output/finding/v1",
            attempted_payload={"x": 1},
            error="something",
            nats_publish=broken_publish,
        )

    async with pg_store.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id FROM output_dead_letter WHERE id = $1", entry.id,
        )
    assert row is not None, "publish failure must not roll back the insert"
