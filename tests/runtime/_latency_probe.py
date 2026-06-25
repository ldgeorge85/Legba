# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Out-of-tree latency probe — runs as a pytest case to capture numbers
for the post-bringup-review report.

Not a regression test; run on demand. Excluded from the default suite by
the leading underscore so pytest doesn't auto-collect it.
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid

import pytest
import pytest_asyncio

from legba.data.config import NatsConfig
from legba.data.nats import NatsStore
from legba.data.registry.streams import (
    DESCRIPTOR_EVENTS_STREAM,
    ensure_runtime_event_streams,
)
from legba.runtime.nats_informer import NatsReconcileInformer


pytestmark = [pytest.mark.integration]


class _LatencyLoop:
    def __init__(self):
        self.last_seen_at: float | None = None

    def enqueue(self, descriptor_id: str, reason: str = "event") -> None:
        self.last_seen_at = time.monotonic()


async def test_publish_to_enqueue_latency():
    """Round-trip from `js.publish` to `loop.enqueue` for 10 samples."""
    store = NatsStore(NatsConfig(url="nats://127.0.0.1:4222"))
    await store.connect()
    try:
        await ensure_runtime_event_streams(store)
        loop = _LatencyLoop()
        inf = NatsReconcileInformer(
            store, loop, consumer_label=f"lat-{uuid.uuid4().hex[:6]}",
        )
        await inf.start()
        try:
            # Warm the fetch loop so the first publish doesn't pay
            # consumer-bind cold-start.
            await asyncio.sleep(1.5)
            samples: list[float] = []
            for _ in range(10):
                did = f"lat_{uuid.uuid4().hex[:10]}"
                loop.last_seen_at = None
                t_pub = time.monotonic()
                await store.js.publish(
                    f"descriptor.updated.target.{did}",
                    json.dumps({"descriptor_id": did}).encode("utf-8"),
                )
                while loop.last_seen_at is None:
                    await asyncio.sleep(0.005)
                samples.append(loop.last_seen_at - t_pub)
            ms = [round(s * 1000, 1) for s in samples]
            print(f"\nlatencies ms: {ms}")
            print(
                f"min={min(samples) * 1000:.1f}  "
                f"max={max(samples) * 1000:.1f}  "
                f"avg={sum(samples) / len(samples) * 1000:.1f}"
            )
            assert max(samples) < 3.0
        finally:
            await inf.stop()
            try:
                await store.js.delete_consumer(
                    DESCRIPTOR_EVENTS_STREAM, inf.consumer_name,
                )
            except Exception:
                pass
    finally:
        await store.close()
