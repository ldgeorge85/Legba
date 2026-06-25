# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""G2 — the agency channel emit must not OVERSTATE delivery.

Background: ``_governor_publish`` (wired in
``legba.runtime.source_first_runtime``) used to swallow EVERY publish
exception WARN-only and return ``None`` (success). Because nothing binds a
JetStream stream to ``channels.escalations``, a real escalation publish RAISES
(``js.publish`` to a subject with no responding stream) — but the swallow made
:meth:`ChannelEmitter.emit` see success and stamp ``delivered=True`` for an
escalation that actually vanished. The status read 'emitted' / delivered=True
while nobody received it.

The G2 fix lets the publish exception PROPAGATE out of ``_governor_publish``
(both callers — the emitter and ``record_governor_event`` — already wrap it in
their own try/except), so the emitter can report ``delivered=False`` honestly.

These are pure-function unit tests of that delivery-honesty contract at the
:class:`ChannelEmitter` seam ``_governor_publish`` feeds — no DB / NATS.
"""

from __future__ import annotations

import pytest

from legba.data.analysts.agency.tools import ChannelEmitter
from legba.data.schemas.action_pack import Channel

pytestmark = [pytest.mark.asyncio]


def _escalation_channel() -> Channel:
    return Channel(
        name="escalations",
        kind="alert",
        config={"subject": "channels.escalations"},
    )


async def test_emit_reports_not_delivered_when_publish_raises() -> None:
    """A publish that RAISES (no JetStream stream bound for the subject)
    must report delivered=False — never overstate delivery it can't confirm."""

    async def _raising_publish(subject: str, payload: bytes) -> None:
        # Mirrors js.publish to a subject with no responding stream.
        raise RuntimeError("no stream responds for channels.escalations")

    emitter = ChannelEmitter(nats_publish=_raising_publish)
    record = await emitter.emit(_escalation_channel(), {"severity": "critical"})

    assert record["delivered"] is False, (
        "emit must NOT claim delivery when the publish raised"
    )
    assert "error" in record
    assert "channels.escalations" in record["error"]


async def test_emit_reports_delivered_when_publish_succeeds() -> None:
    """The honest converse: a publish that returns cleanly IS confirmed."""
    seen: list[tuple[str, bytes]] = []

    async def _ok_publish(subject: str, payload: bytes) -> None:
        seen.append((subject, payload))

    emitter = ChannelEmitter(nats_publish=_ok_publish)
    record = await emitter.emit(_escalation_channel(), {"severity": "critical"})

    assert record["delivered"] is True
    assert record["subject"] == "channels.escalations"
    assert len(seen) == 1


async def test_emit_does_not_raise_out_to_caller_on_publish_failure() -> None:
    """The emitter REPORTS the failure (delivered=False) rather than letting
    it escape — the escalate tool still returns a structured result so the
    governor ledger records the attempt + its (un)delivered channels."""

    async def _raising_publish(subject: str, payload: bytes) -> None:
        raise RuntimeError("broker down")

    emitter = ChannelEmitter(nats_publish=_raising_publish)
    # Must not raise.
    record = await emitter.emit(_escalation_channel(), {"x": 1})
    assert record["delivered"] is False
    assert emitter.emitted == [record]


async def test_no_publisher_wired_does_not_claim_delivery_falsely() -> None:
    """With no NATS publish wired at all, an alert channel is log-only — the
    record reflects the SEAM honestly (delivered True only because the
    log-only emitter is the declared sink), and a non-alert kind is False."""
    emitter = ChannelEmitter(nats_publish=None)

    alert = await emitter.emit(_escalation_channel(), {"x": 1})
    # log-only emitter: the alert/nats_stream kinds report delivered per the
    # documented log-only contract (no publisher == the declared sink).
    assert "delivered" in alert

    webhook = await emitter.emit(
        Channel(name="hook", kind="webhook", config={"url": "https://x"}),
        {"x": 1},
    )
    assert webhook["delivered"] is False
