# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Head-of-line protection for the reconcile loop.

Regression for a release-blocking stall: the reconcile ``_main_loop`` processes
its work queue serially, so a single ``run_once`` that blocks (a wedged actor
activation, a slow registry/DB call) used to starve EVERY descriptor enqueued
behind it. The observed symptom was a newly-registered active analyst head
(``integrity_sweep``) that was enqueued on every resync but never reconciled —
because an earlier item in the queue wedged the loop. Durability re-asserts for
everything behind the wedge also stop.

The fix bounds each ``run_once`` with ``run_once_timeout``: a hung pass is
abandoned + logged, and the queue keeps draining. The timed-out descriptor is
re-enqueued by the next resync (reconcile is idempotent).
"""
from __future__ import annotations

import asyncio
from datetime import timedelta

from legba.runtime.reconcile import DesiredState, ReconcileLoop


class _FakeStore:
    """Minimal ActorStateStore stand-in — every actor is brand-new (no row)."""

    async def get(self, actor_id):  # noqa: ANN001
        return None

    async def list_live_siblings(self, *, actor_kind, descriptor_id, exclude_actor_id):  # noqa: ANN001
        return []


def test_hung_run_once_does_not_starve_the_queue() -> None:
    async def _body() -> None:
        executed: list[str] = []
        hang_started = asyncio.Event()

        async def resolver(descriptor_id: str):
            return DesiredState(
                descriptor_id=descriptor_id,
                descriptor_kind="analyst",
                descriptor_version="0" * 16,
                lifecycle_target="active",
                body={},
            )

        async def lister():
            return []

        async def executor(action) -> None:  # noqa: ANN001
            did = action.detail.get("descriptor_id")
            if did == "hangs":
                hang_started.set()
                await asyncio.Event().wait()  # never completes — wedges this pass
            executed.append(did)

        loop = ReconcileLoop(
            state_store=_FakeStore(),
            desired_resolver=resolver,
            desired_lister=lister,
            action_executor=executor,
            run_once_timeout=timedelta(seconds=0.3),
        )
        await loop.start()
        try:
            loop.enqueue("hangs")    # head of line — will wedge for 0.3s then time out
            loop.enqueue("normal")   # must still be reconciled
            for _ in range(50):      # up to ~5s
                if "normal" in executed:
                    break
                await asyncio.sleep(0.1)
        finally:
            await loop.stop()

        assert hang_started.is_set(), "the hanging reconcile should have started"
        assert "normal" in executed, (
            "head-of-line: a hung run_once must not starve later queue items"
        )

    asyncio.run(_body())


# ---------------------------------------------------------------------------
# Task #236 RIDER — the optional pack_staleness_check callback fires on the
# SAME periodic resync cadence as reminder_gc, best-effort (never stalls
# reconcile on a raise). Mirrors reminder_gc's existing (untested-at-this-
# level) contract in _resync_loop.
# ---------------------------------------------------------------------------


def test_pack_staleness_check_fires_on_periodic_resync() -> None:
    async def _body() -> None:
        fired = asyncio.Event()

        async def resolver(descriptor_id: str):
            return None

        async def lister():
            return []

        async def executor(action) -> None:  # noqa: ANN001
            pass

        async def pack_staleness_check() -> None:
            fired.set()

        loop = ReconcileLoop(
            state_store=_FakeStore(),
            desired_resolver=resolver,
            desired_lister=lister,
            action_executor=executor,
            resync_interval=timedelta(seconds=0.05),
            pack_staleness_check=pack_staleness_check,
        )
        await loop.start()
        try:
            await asyncio.wait_for(fired.wait(), timeout=5.0)
        finally:
            await loop.stop()

    asyncio.run(_body())


def test_pack_staleness_check_failure_does_not_stall_resync() -> None:
    """Best-effort, logged-and-continued — same shape as reminder_gc. A
    raising pack_staleness_check must not prevent the SAME resync pass from
    still enqueueing the live descriptor set, nor prevent later resyncs."""
    async def _body() -> None:
        enqueue_count = 0
        check_calls = 0

        async def resolver(descriptor_id: str):
            return DesiredState(
                descriptor_id=descriptor_id,
                descriptor_kind="analyst",
                descriptor_version="0" * 16,
                lifecycle_target="active",
                body={},
            )

        async def lister():
            nonlocal enqueue_count
            enqueue_count += 1
            return []

        async def executor(action) -> None:  # noqa: ANN001
            pass

        async def pack_staleness_check() -> None:
            nonlocal check_calls
            check_calls += 1
            raise RuntimeError("registry unreachable")

        loop = ReconcileLoop(
            state_store=_FakeStore(),
            desired_resolver=resolver,
            desired_lister=lister,
            action_executor=executor,
            resync_interval=timedelta(seconds=0.05),
            pack_staleness_check=pack_staleness_check,
        )
        await loop.start()
        try:
            for _ in range(50):  # up to ~5s — wait for >=2 resync passes
                if check_calls >= 2:
                    break
                await asyncio.sleep(0.1)
        finally:
            await loop.stop()

        assert check_calls >= 2, "a raising check must not stop future resyncs"
        assert enqueue_count >= 2, "the resync's own list()+enqueue must survive the raise"

    asyncio.run(_body())
