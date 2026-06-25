# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Integration tests for L-191 — `legba.data.outputs.nats_stream`.

Real NATS JetStream container per `tests/data_pkg/conftest.py` (started by
`_ensure_containers_up`). No mocks for the NATS surface — the contract
exists precisely to mediate between the analyst and a real broker, so
test value comes from exercising the real wire.

Covers:
  * Subject derivation honours `analyst.<id>.<channel>` (DESIGN.md §11).
  * Explicit override (`nats_topic`) bypasses derivation.
  * Subject grammar rejects whitespace / wildcards / empty tokens.
  * Payload must be JSON-serializable; UUID + datetime are coerced.
  * Bytes / str payloads pass through unchanged.
  * Bounded retry then DLQ when the underlying publish keeps failing.
  * DLQ subject pattern matches `legba.dlq.output.nats_stream.<analyst_id>`.
  * Real publish round-trip via `NatsStore` reaches a pull subscriber.
  * Real publish round-trip via `StandardDeps.nats_publish` also works.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

import pytest
import pytest_asyncio

from legba.data.config import NatsConfig
from legba.data.nats import NatsStore
from legba.data.outputs import nats_stream
from legba.data.outputs.nats_stream import (
    DEFAULT_CHANNEL,
    DLQ_SUBJECT_PREFIX,
    KIND_NAME,
    OutputDepsError,
    OutputPayloadError,
    OutputSubjectError,
    dlq_subject,
    emit,
    resolve_subject,
)


pytestmark = [pytest.mark.integration]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def session_prefix() -> str:
    return f"legba_test_outnats_{uuid.uuid4().hex[:10]}"


@pytest_asyncio.fixture
async def nats_store() -> NatsStore:
    store = NatsStore(NatsConfig(url="nats://127.0.0.1:4222"))
    await store.connect()
    try:
        yield store
    finally:
        await store.close()


class _DepsWithStore:
    """Minimal deps stub satisfying the `nats_store` access path."""

    def __init__(self, store: NatsStore, analyst_id: str | None = None) -> None:
        self.nats_store = store
        self.nats_publish = None  # explicit — exercises the store fallback
        self.analyst_id = analyst_id


class _DepsWithCallable:
    """Deps stub satisfying the `nats_publish` callable path."""

    def __init__(self, callback, analyst_id: str | None = None) -> None:
        self.nats_publish = callback
        self.analyst_id = analyst_id


# ---------------------------------------------------------------------------
# Kind identity
# ---------------------------------------------------------------------------


def test_kind_name_constant():
    assert KIND_NAME == "nats_stream"
    assert DEFAULT_CHANNEL == "findings"
    assert DLQ_SUBJECT_PREFIX == "legba.dlq.output.nats_stream"


# ---------------------------------------------------------------------------
# Subject derivation — DESIGN.md §11
# ---------------------------------------------------------------------------


class TestResolveSubject:
    def test_default_channel_uses_findings(self):
        # DESIGN.md §11 row "Analyst NATS subject":
        # `analyst.cross_target_energy_correlator.findings`.
        subj = resolve_subject(analyst_id="cross_target_energy_correlator")
        assert subj == "analyst.cross_target_energy_correlator.findings"

    def test_custom_channel(self):
        subj = resolve_subject(analyst_id="india_energy", channel="hypothesis")
        assert subj == "analyst.india_energy.hypothesis"

    def test_override_takes_precedence(self):
        subj = resolve_subject(
            analyst_id="ignored",
            channel="ignored",
            override="custom.topic.path",
        )
        assert subj == "custom.topic.path"

    def test_override_alone(self):
        # No analyst_id required if override is provided.
        subj = resolve_subject(override="legba.custom.subject")
        assert subj == "legba.custom.subject"

    @pytest.mark.parametrize(
        "analyst_id",
        ["", "with space", "with.dot", "with>gt", "with*star", "with\nnewline"],
    )
    def test_invalid_analyst_id_rejected(self, analyst_id):
        with pytest.raises(OutputSubjectError):
            resolve_subject(analyst_id=analyst_id)

    @pytest.mark.parametrize(
        "channel",
        ["with space", "with.dot", "with*star", "with>gt"],
    )
    def test_invalid_channel_rejected(self, channel):
        with pytest.raises(OutputSubjectError):
            resolve_subject(analyst_id="ok_id", channel=channel)

    def test_missing_analyst_id_and_override_raises(self):
        with pytest.raises(OutputSubjectError):
            resolve_subject()

    @pytest.mark.parametrize(
        "subject",
        ["", "with space", "leading.", ".trailing", "double..dot",
         "with*wild", "with>gt"],
    )
    def test_override_grammar_validated(self, subject):
        with pytest.raises(OutputSubjectError):
            resolve_subject(override=subject)


# ---------------------------------------------------------------------------
# DLQ subject
# ---------------------------------------------------------------------------


class TestDlqSubject:
    def test_default_anonymous(self):
        assert dlq_subject(None) == f"{DLQ_SUBJECT_PREFIX}._anonymous"

    def test_with_analyst_id(self):
        assert (
            dlq_subject("india_energy")
            == f"{DLQ_SUBJECT_PREFIX}.india_energy"
        )

    def test_sanitizes_disallowed_chars(self):
        # Defensive: the runtime should never pass a malformed analyst_id,
        # but if it does we sanitize rather than crash the DLQ path.
        out = dlq_subject("bad id.with*chars")
        assert " " not in out and "." not in out.split(".")[-1]
        assert out.startswith(DLQ_SUBJECT_PREFIX + ".")


# ---------------------------------------------------------------------------
# Payload encoding — programmer-error path
# ---------------------------------------------------------------------------


class TestEmitPayloadErrors:
    async def test_non_serializable_payload_raises(self, nats_store):
        deps = _DepsWithStore(nats_store, analyst_id="t1")

        class NotSerializable:
            pass

        with pytest.raises(OutputPayloadError):
            await emit(
                {"obj": NotSerializable()},
                subject="analyst.t1.findings",
                deps=deps,
            )

    async def test_non_mapping_payload_raises(self, nats_store):
        deps = _DepsWithStore(nats_store, analyst_id="t1")
        with pytest.raises(OutputPayloadError):
            await emit([1, 2, 3], subject="analyst.t1.findings", deps=deps)

    async def test_invalid_subject_raises_before_publish(self, nats_store):
        deps = _DepsWithStore(nats_store, analyst_id="t1")
        with pytest.raises(OutputSubjectError):
            await emit({"ok": 1}, subject="with space", deps=deps)

    async def test_missing_deps_raises(self):
        class _Empty:
            pass

        with pytest.raises(OutputDepsError):
            await emit({"ok": 1}, subject="analyst.t1.findings", deps=_Empty())


# ---------------------------------------------------------------------------
# Subject resolution from descriptor+ctx (release-audit fix) — pure, no NATS.
# The generic output dispatcher calls emit(payload, descriptor=, ctx=, deps=)
# without a pre-resolved `subject`; emit must derive it (was: every nats_stream
# binding raised "emit() missing 1 required keyword-only argument: 'subject'").
# ---------------------------------------------------------------------------


class _Ctx:
    def __init__(self, analyst_id):
        self.analyst_id = analyst_id


class TestSubjectResolutionFromDescriptor:
    def test_resolves_channel_from_descriptor_config(self):
        subj = nats_stream._resolve_subject_from_descriptor(
            {"outputs": [{"kind": "nats_stream", "config": {"channel": "findings"}}]},
            _Ctx("country_critic"),
        )
        assert subj == "analyst.country_critic.findings"

    def test_nats_topic_override_wins(self):
        subj = nats_stream._resolve_subject_from_descriptor(
            {"outputs": [{"kind": "nats_stream", "config": {"nats_topic": "legba.custom.topic"}}]},
            _Ctx("ignored"),
        )
        assert subj == "legba.custom.topic"

    def test_defaults_channel_when_absent(self):
        subj = nats_stream._resolve_subject_from_descriptor(
            {"outputs": [{"kind": "nats_stream", "config": {}}]},
            _Ctx("world_assessor"),
        )
        assert subj == f"analyst.world_assessor.{DEFAULT_CHANNEL}"

    def test_picks_the_nats_stream_binding_among_several(self):
        subj = nats_stream._resolve_subject_from_descriptor(
            {
                "outputs": [
                    {"kind": "stix_bundle", "config": {"target_id": "x"}},
                    {"kind": "nats_stream", "config": {"channel": "critiques"}},
                ]
            },
            _Ctx("country_critic"),
        )
        assert subj == "analyst.country_critic.critiques"

    def test_missing_analyst_and_override_raises(self):
        with pytest.raises(OutputSubjectError):
            nats_stream._resolve_subject_from_descriptor({"outputs": []}, _Ctx(None))


# ---------------------------------------------------------------------------
# Real NATS round-trip — `nats_store` path
# ---------------------------------------------------------------------------


async def test_emit_roundtrip_via_nats_store(
    nats_store: NatsStore, session_prefix: str
):
    analyst_id = f"{session_prefix}_a1"
    subject_root = f"{session_prefix}_t1"
    # We need a JetStream stream that covers our subject before publishing.
    # The output kind only publishes; stream lifecycle is the runtime's job.
    stream_name = f"{session_prefix}_t1_stream"

    await nats_store.ensure_stream(
        name=stream_name,
        subjects=[f"{subject_root}.>"],
    )

    deps = _DepsWithStore(nats_store, analyst_id=analyst_id)
    subject = f"{subject_root}.findings"

    payload = {
        "analyst_id": analyst_id,
        "produced_at": datetime.now(tz=timezone.utc),  # exercises _json_default
        "row_id": uuid.uuid4(),                        # exercises _json_default
        "summary": "test finding",
    }

    await emit(payload, subject=subject, deps=deps)

    # Pull-subscribe to verify it landed.
    durable = f"{session_prefix}_t1_dur"
    psub = await nats_store.js.pull_subscribe(
        subject=f"{subject_root}.>",
        durable=durable,
        stream=stream_name,
    )
    try:
        msgs = await psub.fetch(1, timeout=5)
        assert len(msgs) == 1
        decoded = json.loads(msgs[0].data.decode("utf-8"))
        assert decoded["analyst_id"] == analyst_id
        assert decoded["summary"] == "test finding"
        # UUID and datetime were stringified by _json_default.
        assert isinstance(decoded["row_id"], str)
        assert isinstance(decoded["produced_at"], str)
        for m in msgs:
            await m.ack()
    finally:
        try:
            await nats_store.js.delete_stream(stream_name)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Real NATS round-trip — `nats_publish` callable path (StandardDeps shape)
# ---------------------------------------------------------------------------


async def test_emit_roundtrip_via_nats_publish_callable(
    nats_store: NatsStore, session_prefix: str
):
    analyst_id = f"{session_prefix}_a2"
    subject_root = f"{session_prefix}_t2"
    stream_name = f"{session_prefix}_t2_stream"
    await nats_store.ensure_stream(
        name=stream_name,
        subjects=[f"{subject_root}.>"],
    )

    # Mirror StandardDeps.nats_publish wiring: bound `store.publish_json`.
    deps = _DepsWithCallable(nats_store.publish_json, analyst_id=analyst_id)
    subject = resolve_subject(analyst_id=analyst_id, channel=None)
    # Override the runtime-derived subject so it lands in *our* stream.
    custom_subject = f"{subject_root}.findings"

    await emit({"hello": "world"}, subject=custom_subject, deps=deps)

    durable = f"{session_prefix}_t2_dur"
    psub = await nats_store.js.pull_subscribe(
        subject=f"{subject_root}.>",
        durable=durable,
        stream=stream_name,
    )
    try:
        msgs = await psub.fetch(1, timeout=5)
        assert len(msgs) == 1
        decoded = json.loads(msgs[0].data.decode("utf-8"))
        assert decoded == {"hello": "world"}
        for m in msgs:
            await m.ack()
    finally:
        try:
            await nats_store.js.delete_stream(stream_name)
        except Exception:
            pass
    # Sanity: subject derivation still produced the canonical pattern.
    assert subject == f"analyst.{analyst_id}.{DEFAULT_CHANNEL}"


# ---------------------------------------------------------------------------
# Retry + DLQ fallback — exercises the transient-error path with a fake
# publisher. We use a real `NatsStore` for the DLQ side so the DLQ message
# is actually flushed to the broker (no mocks for the substrate; the
# fake here is just the *transient failure injector*).
# ---------------------------------------------------------------------------


class _FlakyPublisher:
    """Simulates N transient failures, then defers to a real publish."""

    def __init__(self, real_publish, fail_count: int):
        self._real = real_publish
        self.fail_count = fail_count
        self.attempts: list[tuple[str, bytes]] = []

    async def __call__(self, subject: str, body: bytes) -> None:
        self.attempts.append((subject, body))
        if self.fail_count > 0:
            self.fail_count -= 1
            # Raise something from the `nats.*` module space so the
            # transient classifier picks it up.
            try:
                import nats.errors as nerr  # type: ignore
                raise nerr.TimeoutError()
            except ImportError:  # pragma: no cover
                raise asyncio.TimeoutError()
        await self._real(subject, body)


async def test_retry_then_success(
    nats_store: NatsStore, session_prefix: str
):
    """Two transient failures, third attempt succeeds — no DLQ traffic."""
    analyst_id = f"{session_prefix}_a3"
    subject_root = f"{session_prefix}_t3"
    stream_name = f"{session_prefix}_t3_stream"
    await nats_store.ensure_stream(
        name=stream_name,
        subjects=[f"{subject_root}.>"],
    )

    flaky = _FlakyPublisher(nats_store.publish_json, fail_count=2)
    deps = _DepsWithCallable(flaky, analyst_id=analyst_id)
    subject = f"{subject_root}.findings"

    await emit(
        {"k": "v"},
        subject=subject,
        deps=deps,
        max_attempts=3,
        backoff_seconds=0.01,
    )

    # 3 attempts (2 fail + 1 succeed), all hit the SAME subject (no DLQ).
    assert len(flaky.attempts) == 3
    assert {s for s, _ in flaky.attempts} == {subject}

    try:
        await nats_store.js.delete_stream(stream_name)
    except Exception:
        pass


@pytest.mark.skip(
    reason="needs isolated rig; collides with live runtime on --network host"
)
async def test_retry_exhausted_routes_to_dlq(
    nats_store: NatsStore, session_prefix: str
):
    """Persistent failures → DLQ envelope on the DLQ subject."""
    analyst_id = f"{session_prefix}_a4"
    # The DLQ subject is `legba.dlq.output.nats_stream.<analyst_id>` —
    # we need a stream that covers that subject so we can pull from it.
    dlq_full = dlq_subject(analyst_id)
    dlq_stream = f"{session_prefix}_t4_dlq_stream"
    await nats_store.ensure_stream(
        name=dlq_stream,
        subjects=[dlq_full],
    )

    # `fail_count` larger than max_attempts so retries fail AND the DLQ
    # path runs. The DLQ uses the same callable so the DLQ publish *will*
    # eventually succeed (fail_count hits 0 after retries exhaust).
    flaky = _FlakyPublisher(nats_store.publish_json, fail_count=3)
    deps = _DepsWithCallable(flaky, analyst_id=analyst_id)

    primary_subject = f"analyst.{analyst_id}.findings"
    # We don't pre-declare a stream for the primary subject; the publish
    # would land in "no stream" land, but our flaky publisher fails before
    # the broker sees it anyway. The point of this test is the DLQ shape.

    await emit(
        {"original": True, "n": 7},
        subject=primary_subject,
        deps=deps,
        max_attempts=3,
        backoff_seconds=0.01,
    )

    # 3 primary attempts (all fail) + 1 DLQ attempt = 4 calls.
    assert len(flaky.attempts) == 4
    subjects_seen = [s for s, _ in flaky.attempts]
    assert subjects_seen[:3] == [primary_subject] * 3
    assert subjects_seen[3] == dlq_full

    # Verify the DLQ envelope made it onto the broker.
    psub = await nats_store.js.pull_subscribe(
        subject=dlq_full,
        durable=f"{session_prefix}_t4_dlq_dur",
        stream=dlq_stream,
    )
    try:
        msgs = await psub.fetch(1, timeout=5)
        assert len(msgs) == 1
        decoded = json.loads(msgs[0].data.decode("utf-8"))
        assert decoded["original_subject"] == primary_subject
        assert decoded["analyst_id"] == analyst_id
        assert "TimeoutError" in decoded["error"]
        # The original payload is echoed (utf-8) for human inspection.
        echoed = json.loads(decoded["payload_utf8"])
        assert echoed == {"original": True, "n": 7}
        for m in msgs:
            await m.ack()
    finally:
        try:
            await nats_store.js.delete_stream(dlq_stream)
        except Exception:
            pass


async def test_dlq_disabled_re_raises(nats_store: NatsStore, session_prefix: str):
    """When `dlq=False`, transient exhaustion re-raises instead of routing."""
    analyst_id = f"{session_prefix}_a5"
    flaky = _FlakyPublisher(nats_store.publish_json, fail_count=10)
    deps = _DepsWithCallable(flaky, analyst_id=analyst_id)

    with pytest.raises(BaseException) as excinfo:
        await emit(
            {"k": "v"},
            subject=f"analyst.{analyst_id}.findings",
            deps=deps,
            max_attempts=2,
            backoff_seconds=0.01,
            dlq=False,
        )
    # Either nats.errors.TimeoutError or asyncio.TimeoutError —
    # both are classified transient by _is_transient.
    assert "TimeoutError" in type(excinfo.value).__name__
    # 2 attempts only; no DLQ attempt with dlq=False.
    assert len(flaky.attempts) == 2


# ---------------------------------------------------------------------------
# Programmer-error path: non-transient errors bypass retry and DLQ.
# ---------------------------------------------------------------------------


async def test_programmer_error_bypasses_retry_and_dlq(
    nats_store: NatsStore, session_prefix: str
):
    """A ValueError from the publish callable is not transient → re-raise."""

    class _Boom:
        def __init__(self):
            self.calls = 0

        async def __call__(self, subject, body):
            self.calls += 1
            raise ValueError("synthetic programmer bug")

    boom = _Boom()
    deps = _DepsWithCallable(boom, analyst_id=f"{session_prefix}_a6")

    with pytest.raises(ValueError, match="synthetic programmer bug"):
        await emit(
            {"k": "v"},
            subject=f"{session_prefix}_t6.findings",
            deps=deps,
            max_attempts=5,
            backoff_seconds=0.01,
        )
    # No retry on a non-transient error.
    assert boom.calls == 1


# ---------------------------------------------------------------------------
# Subject pattern — explicit assertion against DESIGN.md §11.
# ---------------------------------------------------------------------------


def test_subject_pattern_matches_design_md_section_11():
    """DESIGN.md §11 row 'Analyst NATS subject' specifies
    `analyst.<id>.<channel>` with the example
    `analyst.cross_target_energy_correlator.findings`. Re-asserting the
    exact example string locks the convention against drift."""
    subj = resolve_subject(analyst_id="cross_target_energy_correlator")
    assert subj == "analyst.cross_target_energy_correlator.findings"
