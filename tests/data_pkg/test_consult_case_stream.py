# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""C-1b — case frames over the existing ``/consult/stream/{request_id}`` SSE.

Design C-1a §4: one frame per action, riding the step stream as
``{"type": "step", "phase": "case", "kind": "case_action", ...}`` exactly as
the actor's ``step_publish`` closure publishes every other step. The relay is
unchanged — this pins that a case frame passes through it byte-for-byte, the
reads it rests on included, in the order published.

Also the slim-image guard for the two registry-side case modules: they ship in
the registry image and must import with the analyst/runtime stack absent.

Integration — the relay test needs the test NATS broker (127.0.0.1:4222).
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import textwrap
import uuid

import pytest
import pytest_asyncio

import legba.data.registry.consult_stream_api as stream_api
from legba.data.analysts.consult_case_schema import (
    CaseActionFrame,
    CaseRefusedFrame,
    CaseSealedFrame,
    Cite,
    RestsOn,
)
from legba.data.config import NatsConfig
from legba.data.nats import NatsStore
from legba.data.registry.consult_stream_api import build_consult_stream_router


@pytest_asyncio.fixture
async def nats_store() -> NatsStore:
    store = NatsStore(NatsConfig(url="nats://127.0.0.1:4222"))
    await store.connect()
    try:
        yield store
    finally:
        await store.close()


class _Deps:
    def __init__(self, store: NatsStore) -> None:
        self.nats_store = store


@pytest.mark.integration
@pytest.mark.asyncio
async def test_case_frames_relay_one_per_action_with_their_reads(nats_store, monkeypatch):
    monkeypatch.setattr(stream_api, "_authorize_ws_token", lambda *a, **k: "test")
    request_id = uuid.uuid4().hex
    subject = f"legba.consult.steps.{request_id}"
    router = build_consult_stream_router(_Deps(nats_store))  # type: ignore[arg-type]
    endpoint = next(
        r.endpoint for r in router.routes
        if getattr(r, "path", "") == "/consult/stream/{request_id}"
    )
    resp = await endpoint(request_id, token="t", authorization=None)

    steps = [
        CaseActionFrame(
            seq=1, action="set_scope", args={"kind": "target", "id": "subject_alpha"},
            cites=[Cite("target", "subject_alpha")], rests_on=[RestsOn(1, "list_targets")],
            at="2026-10-03T12:00:00Z", round=2,
        ).to_step(),
        CaseRefusedFrame(action="focus", cause="ref_not_read", detail="read it first",
                         args={}, round=2).to_step(),
        CaseActionFrame(
            seq=2, action="narrate", args={"text": "Alpha first."},
            cites=[Cite("target", "subject_alpha")], rests_on=[RestsOn(1, "list_targets")],
            at="2026-10-03T12:00:01Z", round=2,
        ).to_step(),
        CaseSealedFrame(seq_through=2, summary=None, at="2026-10-03T12:00:02Z", round=3).to_step(),
    ]

    async def _publish() -> None:
        await asyncio.sleep(0.3)
        for step in steps:
            await nats_store.nc.publish(subject, json.dumps({"type": "step", **step}).encode())
        await nats_store.nc.publish(subject, json.dumps({
            "type": "final", "final_source": "registry", "request_id": request_id,
            "status": "complete", "response": {"answer": "a", "case": None},
        }).encode())
        await nats_store.nc.flush()

    pub = asyncio.create_task(_publish())
    frames: list[bytes] = []

    async def _drain() -> None:
        async for chunk in resp.body_iterator:
            frames.append(chunk)

    await asyncio.wait_for(_drain(), timeout=10.0)
    await pub
    data = [json.loads(f[len(b"data: "):]) for f in frames if f.startswith(b"data: ")]
    relayed = [d for d in data if d.get("type") == "step"]
    assert relayed == [{"type": "step", **s} for s in steps]
    actions = [d for d in relayed if d["kind"] == "case_action"]
    assert [a["seq"] for a in actions] == [1, 2]
    assert actions[0]["rests_on"] == [{"round": 1, "tool": "list_targets"}]
    assert data[-1]["final_source"] == "registry"


def test_registry_case_modules_import_without_the_runtime_stack() -> None:
    probe = textwrap.dedent(
        """
        import sys
        for blocked in ("feedparser", "telethon", "warcio", "aiobotocore",
                        "pycountry", "networkx", "qdrant_client"):
            sys.modules[blocked] = None
        import legba.data.consult_case_record as rec
        import legba.data.registry.consult_case_front as front
        import legba.data.registry.consult_api as api
        leaked = sorted(
            m for m in sys.modules
            if m.startswith("legba.data.analysts") or m.startswith("legba.runtime")
        )
        assert not leaked, "analyst/runtime package reachable: %r" % leaked
        assert rec.CaseFrameAccumulator and front.require_case_available and api.build_consult_router
        print("OK")
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, timeout=120,
        env={**os.environ},
    )
    assert result.returncode == 0, result.stderr[-2000:]
    assert result.stdout.strip().endswith("OK")
