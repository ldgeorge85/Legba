# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Durable per-delivery audit for the agency channel-emit edge (migration 0061).

Before this, an escalate emit landed on NATS ``channels.escalations`` and the
in-memory ``ChannelEmitter.emitted`` list — NOWHERE durable, so "who got
alerted?" was unanswerable across a restart. The fix writes one
``alert_sink_deliveries`` row (repurposed into the unified per-delivery audit)
per emit, recording WHAT was delivered WHERE + whether the publish confirmed.

These are pure-function unit tests at the :class:`ChannelEmitter` seam (a fake
duck-typed pool captures the INSERT — mirrors
``tests/data_pkg/test_output_alert.py``) plus a gate test proving a
verify-DEMOTED finding writes NO row (it never reaches the emitter).
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

from legba.data.analysts.agency.tools import ChannelEmitter
from legba.data.schemas.action_pack import Channel

pytestmark = [pytest.mark.asyncio]


class _RecordingPool:
    """Duck-typed .execute — captures (sql, args) like an asyncpg pool would."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def execute(self, sql: str, *args: Any) -> None:
        self.calls.append((sql, args))


def _escalation_channel() -> Channel:
    return Channel(
        name="escalations",
        kind="alert",
        config={"subject": "channels.escalations"},
    )


def _escalation_payload(output_id: UUID) -> dict[str, Any]:
    return {
        "action": "escalate",
        "severity": "high",
        "title": "Coup-risk spike in country X",
        "detail": "Multiple corroborating signals.",
        "target_ref": f"analyst_outputs:{output_id}",
        "requested_by": "escalate_finding",
        "output_id": str(output_id),
        "target_id": "us",
        "effective_confidence": 0.91,
    }


async def test_escalation_emit_writes_exactly_one_audit_row_with_fields() -> None:
    """A fixture escalation emit that DELIVERS writes exactly one durable audit
    row carrying the channel/subject, finding id, target, severity, the
    verify-folded effective confidence, and a delivered outcome."""
    seen: list[tuple[str, bytes]] = []

    async def _ok_publish(subject: str, payload: bytes) -> None:
        seen.append((subject, payload))

    pool = _RecordingPool()
    emitter = ChannelEmitter(nats_publish=_ok_publish, pg_pool=pool)
    oid = uuid4()

    record = await emitter.emit(_escalation_channel(), _escalation_payload(oid))

    # The NATS emit stayed the delivery edge.
    assert record["delivered"] is True
    assert len(seen) == 1 and seen[0][0] == "channels.escalations"

    # EXACTLY one durable audit row.
    assert len(pool.calls) == 1, "an escalation emit must write exactly one audit row"
    sql, args = pool.calls[0]
    assert "INSERT INTO alert_sink_deliveries" in sql

    # Fields (positional order matches the INSERT column list).
    assert args[0] == oid                     # alert_row_id = the finding/output id
    assert args[1] == "escalations"           # channel_name
    assert args[2] == "alert"                 # sink_kind = channel kind
    assert args[3] == "channels.escalations"  # sink_target = subject
    assert args[4] == "us"                    # target_id (country)
    assert args[5] == "high"                  # severity
    assert args[6] == pytest.approx(0.91)     # effective_confidence
    assert args[7] == "delivered"             # status = honest publish outcome
    assert args[8] is None                    # error_message
    assert args[9] is not None                # delivered_at stamped on success
    summary = json.loads(args[10])
    assert summary["action"] == "escalate"
    assert summary["delivered"] is True


async def test_failed_publish_audits_as_failed_not_delivered() -> None:
    """A publish that RAISES (no stream bound for the subject) still writes one
    row — status='failed', no delivered_at, the error captured — so an
    escalation that vanished is DURABLY visible, never silently lost."""

    async def _raising_publish(subject: str, payload: bytes) -> None:
        raise RuntimeError("no stream responds for channels.escalations")

    pool = _RecordingPool()
    emitter = ChannelEmitter(nats_publish=_raising_publish, pg_pool=pool)
    oid = uuid4()

    record = await emitter.emit(_escalation_channel(), _escalation_payload(oid))

    assert record["delivered"] is False
    assert len(pool.calls) == 1
    _, args = pool.calls[0]
    assert args[7] == "failed"                # status
    assert args[8] is not None                # error_message captured
    assert "channels.escalations" in args[8]
    assert args[9] is None                    # no delivered_at on a failure


async def test_no_pool_wired_writes_no_row_and_does_not_break_emit() -> None:
    """The audit is opt-in: with no pool wired the emit still delivers and
    never raises — the durable row is observability, not correctness."""
    seen: list[tuple[str, bytes]] = []

    async def _ok_publish(subject: str, payload: bytes) -> None:
        seen.append((subject, payload))

    emitter = ChannelEmitter(nats_publish=_ok_publish, pg_pool=None)
    record = await emitter.emit(_escalation_channel(), _escalation_payload(uuid4()))
    assert record["delivered"] is True
    assert len(seen) == 1  # delivery unaffected by the (absent) audit


async def test_verify_demoted_finding_escalates_none_and_audits_none() -> None:
    """The verified-honesty thesis at the delivery edge: a finding the verify
    pass DEMOTED (folded effective confidence below the gate) does not escalate,
    so it never reaches the emitter and writes NO audit row — even with a
    high-severity tag. The gate blocks BEFORE any binding/emitter is touched.
    """
    from legba.runtime.actor_output_emit import _maybe_escalate_finding

    class _ExplodingBinding:
        # If the gate WRONGLY admitted a demoted finding, the escalate path would
        # reach here — turning a gate regression into a loud test failure rather
        # than a silent spurious audit row.
        def for_target(self, **_kw: Any) -> Any:  # pragma: no cover - must not run
            raise AssertionError(
                "verify-demoted finding must NOT reach the channel emitter"
            )

    escalation = SimpleNamespace(
        severity_gate="high",
        confidence_gate=0.85,
        binding=_ExplodingBinding(),
    )
    # A high-severity finding whose RAW confidence is high (0.9) but whose
    # faithfulness verdict floors the effective confidence to 0.1 → 0.1×1.2 < 0.85.
    demoted = SimpleNamespace(
        severity="high",
        confidence=0.9,
        data={},          # no activation_count → not an indicator flip either
        tags=["g20"],
        title="High-severity but unfaithful claim",
        body="Asserted with high confidence, unsupported by the cited evidence.",
    )

    # conn is only touched AFTER the gate passes; a demoted finding returns first.
    result = await _maybe_escalate_finding(
        None,
        escalation=escalation,
        payload=demoted,
        output_row_id=uuid4(),
        target_id="us",
        actor_id="unit-test",
        verification_block={"faithfulness_score": 0.1},
    )
    assert result is None  # returned via the gate, never reached the emitter
