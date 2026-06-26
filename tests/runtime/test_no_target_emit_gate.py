# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""D32 no-target emit gate — the D4 contamination tail.

A run whose target_id failed to thread used to emit:
  * an ``unknown``-keyed STIX bundle (subject ``legba.outputs.stix.unknown``,
    collection ``legba_target_unknown_collection``); and
  * a NULL/unknown-target alert.

These pure-Python unit tests (no DB / no live NATS) assert the emit path now
REFUSES to publish such junk while leaving real-target + legitimate
target-less META emits untouched.
"""

from __future__ import annotations

from typing import Any

import pytest

from legba.data.outputs import alert as alert_kind
from legba.data.outputs import stix_bundle
from legba.data.outputs._contract import OutputContext, OutputDeps
from legba.data.provenance.models import AlertPayload, FindingPayload


class _RecordingNats:
    """Records both publish surfaces: stix_bundle uses ``publish_json``; the
    alert nats sink uses ``publish_core`` (duck-typed, streamless subject)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    async def publish_json(self, subject: str, payload: Any) -> None:
        self.calls.append((subject, payload))

    async def publish_core(self, subject: str, body: Any) -> None:
        self.calls.append((subject, body))


# ---------------------------------------------------------------------------
# STIX bundle — D32 unknown-keyed bundle gate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stix_emit_skips_unknown_target_no_publish():
    nats = _RecordingNats()
    bundle = await stix_bundle.emit(
        FindingPayload(title="leak probe", body="b", confidence=0.5),
        descriptor=None,           # no target_id in config
        deps=OutputDeps(nats=nats),
        ctx=None,                  # no ctx.target_id either → would be "unknown"
    )
    # Empty bundle (target identity only would have populated objects); nothing
    # published to NATS, so no legba.outputs.stix.unknown subject leaks.
    assert nats.calls == []
    assert "objects" not in bundle or not getattr(bundle, "objects", None)


@pytest.mark.asyncio
async def test_stix_emit_skips_explicit_unknown_sentinel():
    nats = _RecordingNats()
    ctx = OutputContext(target_id="unknown")
    await stix_bundle.emit(
        FindingPayload(title="p", body="b", confidence=0.5),
        descriptor=None,
        deps=OutputDeps(nats=nats),
        ctx=ctx,
    )
    assert nats.calls == []


@pytest.mark.asyncio
async def test_stix_emit_publishes_for_real_target():
    nats = _RecordingNats()
    ctx = OutputContext(target_id="country_g20_id")
    bundle = await stix_bundle.emit(
        FindingPayload(title="real", body="b", confidence=0.6),
        descriptor=None,
        deps=OutputDeps(nats=nats),
        ctx=ctx,
    )
    # Real target → exactly one publish on the per-target subject.
    assert len(nats.calls) == 1
    assert nats.calls[0][0] == "legba.outputs.stix.country_g20_id"
    assert getattr(bundle, "objects", None)


# ---------------------------------------------------------------------------
# Alert — D32 NULL/unknown-target alert gate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_alert_emit_skips_unknown_sentinel_target():
    nats = _RecordingNats()
    results = await alert_kind.emit(
        AlertPayload(title="page", body="b", confidence=0.9, severity="high"),
        descriptor=None,
        deps=OutputDeps(nats=nats),
        ctx=OutputContext(target_id="None"),   # stringified None sentinel
    )
    assert results == []
    assert nats.calls == []


@pytest.mark.asyncio
async def test_alert_emit_require_target_skips_empty_target():
    nats = _RecordingNats()
    descriptor = {"outputs": [{"kind": "alert", "config": {"require_target": True}}]}
    results = await alert_kind.emit(
        AlertPayload(title="page", body="b", confidence=0.9, severity="high"),
        descriptor=descriptor,
        deps=OutputDeps(nats=nats),
        ctx=OutputContext(target_id=""),       # empty + require_target → skip
    )
    assert results == []
    assert nats.calls == []


@pytest.mark.asyncio
async def test_alert_emit_meta_empty_target_still_fires():
    # A legitimately target-less META alert (no require_target) still delivers.
    nats = _RecordingNats()
    results = await alert_kind.emit(
        AlertPayload(title="world page", body="b", confidence=0.9, severity="high"),
        descriptor=None,
        deps=OutputDeps(nats=nats),
        ctx=OutputContext(target_id=""),
    )
    # NATS fires for a high-severity alert regardless of target.
    assert any(r.surface == "nats" for r in results)
    assert nats.calls, "meta alert must still publish to NATS"


@pytest.mark.asyncio
async def test_alert_emit_real_target_fires():
    nats = _RecordingNats()
    results = await alert_kind.emit(
        AlertPayload(title="country page", body="b", confidence=0.9, severity="high"),
        descriptor=None,
        deps=OutputDeps(nats=nats),
        ctx=OutputContext(target_id="country_g20_id"),
    )
    assert any(r.surface == "nats" for r in results)
    assert nats.calls
