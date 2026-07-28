# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Cooldown coalescing + unconfigured-sink noise gate (operator feedback 07-28).

Suppressed-in-cooldown alerts must be DISTILLED onto the next allowed POST
(count + bounded preview), and unconfigured sinks must drop out of the fan-out
whenever a configured sibling exists (the no-sinks case keeps the P1-1
visibility rows).
"""
from __future__ import annotations

import asyncio

from legba.data.alerts.sinks import (
    AlertSinkDispatcher,
    AlertSinkPayload,
    DeliveryResult,
    _SUPPRESSED_PREVIEW_MAX,
)


class _FakeSink:
    def __init__(self, kind="fake", configured=True):
        self.sink_kind = kind
        self._configured = configured
        self.delivered: list[AlertSinkPayload] = []

    @property
    def configured(self):
        return self._configured

    def target_summary(self):
        return "fake-host"

    def accepts_severity(self, sev):
        return True

    async def deliver(self, payload):
        self.delivered.append(payload)
        return DeliveryResult(sink_kind=self.sink_kind, outcome="delivered", target="fake-host")


def _pl(summary, sev="high", row=None):
    return AlertSinkPayload(summary=summary, severity=sev, verify_state="unverified — test", alert_row_id=row)


def test_cooldown_suppressed_coalesce_onto_next_send():
    async def run():
        clock = [0.0]
        sink = _FakeSink()
        d = AlertSinkDispatcher(sinks=[sink], cooldown_seconds=60.0, monotonic=lambda: clock[0])
        await d.fan_out(_pl("first", row="r1"))            # delivered
        clock[0] = 10.0
        await d.fan_out(_pl("burst-a", row="r2"))          # cooldown
        clock[0] = 20.0
        await d.fan_out(_pl("burst-b", row="r3"))          # cooldown
        clock[0] = 90.0
        await d.fan_out(_pl("after", row="r4"))            # delivered + coalesced
        assert len(sink.delivered) == 2
        final = sink.delivered[-1]
        assert final.suppressed_in_cooldown == 2
        assert len(final.suppressed_preview) == 2
        assert any("burst-a" in p for p in final.suppressed_preview)
        # buffer clears after attach
        clock[0] = 180.0
        await d.fan_out(_pl("clean", row="r5"))
        assert sink.delivered[-1].suppressed_in_cooldown == 0
    asyncio.run(run())


def test_preview_capped_but_count_honest():
    async def run():
        clock = [0.0]
        sink = _FakeSink()
        d = AlertSinkDispatcher(sinks=[sink], cooldown_seconds=60.0, monotonic=lambda: clock[0])
        await d.fan_out(_pl("first", row="s0"))
        for i in range(_SUPPRESSED_PREVIEW_MAX + 3):
            clock[0] = 1.0 + i
            await d.fan_out(_pl(f"b{i}", row=f"s{i+1}"))
        clock[0] = 120.0
        await d.fan_out(_pl("after", row="tail"))
        final = sink.delivered[-1]
        assert final.suppressed_in_cooldown == _SUPPRESSED_PREVIEW_MAX + 3
        assert len(final.suppressed_preview) == _SUPPRESSED_PREVIEW_MAX
    asyncio.run(run())


def test_unconfigured_sink_dropped_when_sibling_configured():
    active = _FakeSink(kind="ntfy", configured=True)
    inactive = _FakeSink(kind="webhook", configured=False)
    d = AlertSinkDispatcher(sinks=[inactive, active])
    kinds = [s.sink_kind for s in d.sinks]
    assert kinds == ["ntfy"]


def test_all_unconfigured_keeps_visibility_rows():
    a = _FakeSink(kind="webhook", configured=False)
    b = _FakeSink(kind="ntfy", configured=False)
    d = AlertSinkDispatcher(sinks=[a, b])
    assert len(d.sinks) == 2  # P1-1 guarantee intact


def test_env_cooldown_knob(monkeypatch):
    monkeypatch.setenv("LEGBA_ALERT_SINK_COOLDOWN_SECONDS", "15")
    d = AlertSinkDispatcher(sinks=[_FakeSink()])
    assert d._cooldown_s == 15.0
