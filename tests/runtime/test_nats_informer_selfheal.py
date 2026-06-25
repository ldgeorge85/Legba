# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Self-heal regression for the NATS reconcile informer.

A dropped durable consumer surfaces as a non-timeout ``fetch()`` error (a 503
ServiceUnavailable). The pre-fix loop retried the *same* dead subscription
forever — a ~3h, 10k-log-line incident on 2026-06-11. The informer must instead
RE-BIND the consumer (re-create + re-subscribe) and recover on its own.
"""
from __future__ import annotations

import asyncio

from legba.runtime.nats_informer import NatsReconcileInformer


class _FakeMsg:
    def __init__(self, subject: str) -> None:
        self.subject = subject
        self.acked = False

    async def ack(self) -> None:
        self.acked = True


class _FakeSub:
    """A pull subscription whose ``fetch()`` is scripted per call.

    Each script item is ``"error"`` (non-timeout failure → wedged consumer),
    ``"timeout"`` (no messages), or a ``list[_FakeMsg]`` to deliver.
    """

    def __init__(self, script: list) -> None:
        self._script = script
        self._i = 0
        self.unsubscribed = False

    async def fetch(self, batch: int, timeout: float):  # noqa: ANN001
        # Cooperative yield — the real pull-fetch awaits network I/O; without
        # this the scripted "timeout" branch busy-spins and starves the loop.
        await asyncio.sleep(0.001)
        item = self._script[min(self._i, len(self._script) - 1)]
        self._i += 1
        if item == "error":
            raise RuntimeError("ServiceUnavailableError: code=None")
        if item == "timeout":
            raise asyncio.TimeoutError
        return item

    async def unsubscribe(self) -> None:
        self.unsubscribed = True


class _FakeJS:
    """Hands out the next scripted subscription on each ``pull_subscribe``."""

    def __init__(self, subs: list[_FakeSub]) -> None:
        self._subs = subs
        self._i = 0
        self.add_consumer_calls = 0
        self.pull_subscribe_calls = 0

    async def add_consumer(self, stream, config):  # noqa: ANN001
        self.add_consumer_calls += 1

    async def pull_subscribe(self, subject, durable, stream):  # noqa: ANN001
        self.pull_subscribe_calls += 1
        sub = self._subs[min(self._i, len(self._subs) - 1)]
        self._i += 1
        return sub


class _FakeStore:
    def __init__(self, js: _FakeJS) -> None:
        self.js = js


class _FakeLoop:
    def __init__(self) -> None:
        self.enqueued: list[tuple[str, str]] = []

    def enqueue(self, descriptor_id: str, reason: str) -> None:
        self.enqueued.append((descriptor_id, reason))


def test_informer_rebinds_dead_consumer_and_recovers() -> None:
    async def _body() -> None:
        sub1 = _FakeSub(["error"])  # dead consumer → forces a re-bind
        msg = _FakeMsg("descriptor.upsert.target.country_g20_us")
        sub2 = _FakeSub([[msg], "timeout"])  # handed out on re-bind, then idle
        js = _FakeJS([sub1, sub2])
        loop = _FakeLoop()

        informer = NatsReconcileInformer(_FakeStore(js), loop)  # type: ignore[arg-type]
        informer._fetch_error_backoff = (0.01, 0.02)  # keep the regression fast

        await informer.start()
        try:
            for _ in range(100):  # up to ~5s
                if loop.enqueued:
                    break
                await asyncio.sleep(0.05)
        finally:
            await informer.stop()

        assert sub1.unsubscribed, "the dead subscription must be unsubscribed on re-bind"
        assert js.pull_subscribe_calls >= 2, "re-bind must re-subscribe, not reuse the dead sub"
        assert informer.stats.fetch_errors == 1
        assert informer.stats.rebinds == 1
        assert loop.enqueued == [
            ("country_g20_us", "nats_event:descriptor.upsert.target.country_g20_us")
        ], "the informer must recover and enqueue the post-rebind message"
        assert msg.acked

    asyncio.run(_body())
