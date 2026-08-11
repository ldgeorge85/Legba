# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""S-6 — a hung activate must degrade to skip-and-retry, never a frozen plane.

Regression suite for the 2026-08-01 mechanism. A strict-mode parse bug made
``proxy.activate()`` HANG rather than fail; ``ENSURE_ACTIVE`` fires against
every active analyst and source on every resync; the reconcile loop is strictly
serial; and Dapr actors are turn-based with reentrancy disabled. Those four
facts compose into a fleet-wide freeze: each hung activate ate the full 90 s
``run_once`` bound AND held its actor's turn forever, so every cadence reminder
and coalesced fire queued behind a turn that would never complete.

The fakes here are the hung activate. Every test either proves the queue keeps
draining, or proves an actor turn TERMINATES instead of parking — those are the
two halves, and the suite is deliberately explicit that neither alone suffices
(see ``test_deadline_does_not_by_itself_unwedge_the_actor``).
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from legba.runtime import actor_turn
from legba.runtime.actor_turn import (
    HealBreaker,
    TurnBudgetExceeded,
    bounded_turn_op,
    bounded_turn_op_or,
)
from legba.runtime.dapr_host import execute_reconcile_action
from legba.runtime.reconcile import ActionKind, ReconcileAction


# ---------------------------------------------------------------------------
# Budgets — fail safe, never silently disabled
# ---------------------------------------------------------------------------


def test_budget_defaults(monkeypatch) -> None:
    for env in (
        actor_turn.HEAL_TIMEOUT_ENV,
        actor_turn.TURN_OP_TIMEOUT_ENV,
        actor_turn.HEAL_BREAKER_TRIPS_ENV,
        actor_turn.HEAL_BREAKER_COOLOFF_ENV,
    ):
        monkeypatch.delenv(env, raising=False)
    assert actor_turn.heal_timeout_seconds() == actor_turn.HEAL_TIMEOUT_DEFAULT_S
    assert actor_turn.turn_op_timeout_seconds() == actor_turn.TURN_OP_TIMEOUT_DEFAULT_S
    assert actor_turn.heal_breaker_trips() == actor_turn.HEAL_BREAKER_TRIPS_DEFAULT
    assert (
        actor_turn.heal_breaker_cooloff_seconds()
        == actor_turn.HEAL_BREAKER_COOLOFF_DEFAULT_S
    )


def test_heal_deadline_is_well_under_the_run_once_bound() -> None:
    """The whole point of the heal deadline is that ONE wedged actor cannot
    cost the reconcile pass its entire 90 s budget. If these two ever converge,
    the 08-01 amplifier is back."""
    from legba.runtime.reconcile import ReconcileLoop

    default_run_once = ReconcileLoop(
        state_store=None, desired_resolver=None,
        desired_lister=None, action_executor=None,
    )._run_once_timeout.total_seconds()
    assert actor_turn.HEAL_TIMEOUT_DEFAULT_S * 3 <= default_run_once


@pytest.mark.parametrize("bad", ["", "not-an-int", "0", "-5"])
def test_malformed_budget_falls_back_to_default(monkeypatch, bad) -> None:
    """A typo must never disable a budget — that is a budget removed by
    accident rather than by decision."""
    monkeypatch.setenv(actor_turn.HEAL_TIMEOUT_ENV, bad)
    assert actor_turn.heal_timeout_seconds() == actor_turn.HEAL_TIMEOUT_DEFAULT_S


def test_env_override_is_honoured(monkeypatch) -> None:
    monkeypatch.setenv(actor_turn.TURN_OP_TIMEOUT_ENV, "7")
    assert actor_turn.turn_op_timeout_seconds() == 7


# ---------------------------------------------------------------------------
# bounded_turn_op — the turn TERMINATES
# ---------------------------------------------------------------------------


def test_bounded_turn_op_releases_a_hung_op() -> None:
    async def _body() -> None:
        async def hangs() -> None:
            await asyncio.Event().wait()      # the zombie turn, exactly

        with pytest.raises(TurnBudgetExceeded) as ei:
            await bounded_turn_op(
                hangs(), op="test.hang", actor_id="analyst::x::0", timeout=0.05,
            )
        assert ei.value.op == "test.hang"
        assert ei.value.actor_id == "analyst::x::0"

    asyncio.run(_body())


def test_bounded_turn_op_passes_the_value_through_when_fast() -> None:
    async def _body() -> None:
        async def quick() -> str:
            return "deps"

        got = await bounded_turn_op(
            quick(), op="test.ok", actor_id="a", timeout=5,
        )
        assert got == "deps"

    asyncio.run(_body())


def test_bounded_turn_op_cancels_the_hung_coroutine() -> None:
    """Not cosmetic: an abandoned-but-still-running coroutine would keep the
    turn's work alive underneath us, which is the state we are trying to leave."""
    async def _body() -> None:
        cancelled = asyncio.Event()

        async def hangs() -> None:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

        with pytest.raises(TurnBudgetExceeded):
            await bounded_turn_op(
                hangs(), op="t", actor_id="a", timeout=0.05,
            )
        await asyncio.sleep(0.05)
        assert cancelled.is_set()

    asyncio.run(_body())


def test_bounded_turn_op_or_rejoins_the_existing_none_branch() -> None:
    async def _body() -> None:
        async def hangs() -> str:
            await asyncio.Event().wait()
            return "never"

        got = await bounded_turn_op_or(
            hangs(), None, op="t", actor_id="a", timeout=0.05,
        )
        assert got is None

    asyncio.run(_body())


def test_bounded_turn_op_does_not_swallow_real_errors() -> None:
    async def _body() -> None:
        async def boom() -> None:
            raise ValueError("registry said no")

        with pytest.raises(ValueError):
            await bounded_turn_op(boom(), op="t", actor_id="a", timeout=5)

    asyncio.run(_body())


def test_an_upstreams_own_timeout_is_not_claimed_as_our_budget() -> None:
    """httpx maps its read timeout onto ``TimeoutError``, the same type our
    deadline raises. Reporting that as ``budget_exceeded`` would send the
    operator to tune a knob that had nothing to do with it, and would hide a
    real remote fault. It must propagate as itself."""
    async def _body() -> None:
        async def upstream_times_out() -> None:
            raise TimeoutError("httpx read timeout")

        with pytest.raises(TimeoutError) as ei:
            await bounded_turn_op(
                upstream_times_out(), op="t", actor_id="a", timeout=30,
            )
        assert not isinstance(ei.value, TurnBudgetExceeded)
        assert "httpx" in str(ei.value)

    asyncio.run(_body())


# ---------------------------------------------------------------------------
# HealBreaker — repeated timeouts stop costing the queue
# ---------------------------------------------------------------------------


class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def test_breaker_opens_after_consecutive_timeouts() -> None:
    clock = _Clock()
    brk = HealBreaker(trips=3, cooloff_seconds=100, clock=clock)
    aid = "analyst::wedged::0"
    assert not brk.should_skip(aid)
    for expected in (1, 2):
        assert brk.record_timeout(aid) == expected
        assert not brk.should_skip(aid), "must not open before the trip count"
    assert brk.record_timeout(aid) == 3
    assert brk.should_skip(aid)
    assert brk.open_actors() == [aid]


def test_breaker_allows_one_probe_after_the_cooloff() -> None:
    clock = _Clock()
    brk = HealBreaker(trips=2, cooloff_seconds=100, clock=clock)
    aid = "analyst::wedged::0"
    brk.record_timeout(aid)
    brk.record_timeout(aid)
    assert brk.should_skip(aid)
    clock.t += 101
    assert not brk.should_skip(aid), "the cooloff must expire — recovery is automatic"


def test_breaker_success_clears_the_streak() -> None:
    clock = _Clock()
    brk = HealBreaker(trips=2, cooloff_seconds=100, clock=clock)
    aid = "analyst::flaky::0"
    brk.record_timeout(aid)
    brk.record_success(aid)
    assert brk.record_timeout(aid) == 1, "a success must reset, not merely pause"
    assert not brk.should_skip(aid)
    assert brk.open_actors() == []


def test_breaker_is_per_actor() -> None:
    clock = _Clock()
    brk = HealBreaker(trips=1, cooloff_seconds=100, clock=clock)
    brk.record_timeout("analyst::wedged::0")
    assert brk.should_skip("analyst::wedged::0")
    assert not brk.should_skip("analyst::healthy::0"), (
        "one wedged actor must never suppress the heal for the rest of the fleet"
    )


def test_breaker_tracking_is_bounded() -> None:
    clock = _Clock()
    brk = HealBreaker(trips=1, cooloff_seconds=100, clock=clock)
    for i in range(actor_turn._MAX_TRACKED + 50):
        clock.t += 1
        brk.record_timeout(f"analyst::a{i}::0")
    assert len(brk._state) <= actor_turn._MAX_TRACKED


# ---------------------------------------------------------------------------
# The reconcile executor — hung activate fakes
# ---------------------------------------------------------------------------


class _HungProxy:
    """The 08-01 shape: activate() accepts the call and never returns."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def activate(self) -> None:
        self.calls.append("activate")
        await asyncio.Event().wait()

    async def pause(self) -> None:
        self.calls.append("pause")
        await asyncio.Event().wait()

    async def retire(self) -> None:
        self.calls.append("retire")
        await asyncio.Event().wait()

    async def resume(self) -> None:
        self.calls.append("resume")
        await asyncio.Event().wait()


class _LiveProxy:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def activate(self) -> None:
        self.calls.append("activate")

    async def pause(self) -> None:
        self.calls.append("pause")

    async def retire(self) -> None:
        self.calls.append("retire")

    async def resume(self) -> None:
        self.calls.append("resume")


class _RecordingStore:
    def __init__(self) -> None:
        self.writes: list[tuple[str, str]] = []

    async def get(self, actor_id):  # noqa: ANN001
        return None

    async def upsert(self, rec):  # noqa: ANN001
        self.writes.append((rec.actor_id, rec.lifecycle))


def _ensure_active(actor_id: str = "analyst::wedged::0") -> ReconcileAction:
    return ReconcileAction(
        kind=ActionKind.ENSURE_ACTIVE,
        actor_id=actor_id,
        detail={
            "descriptor_id": actor_id.split("::")[1],
            "descriptor_kind": "analyst",
            "descriptor_version": "0" * 16,
        },
    )


def test_hung_activate_is_bounded_not_awaited_forever(monkeypatch) -> None:
    """Before S-6 this call never returned. It now returns inside the deadline."""
    monkeypatch.setenv(actor_turn.HEAL_TIMEOUT_ENV, "1")

    async def _body() -> None:
        proxy = _HungProxy()
        store = _RecordingStore()
        started = asyncio.get_running_loop().time()
        await asyncio.wait_for(
            execute_reconcile_action(
                _ensure_active(),
                proxy_for=lambda kind, aid: proxy,
                state_store=store,
                breaker=HealBreaker(),
            ),
            timeout=10,
        )
        elapsed = asyncio.get_running_loop().time() - started
        assert proxy.calls == ["activate"]
        assert elapsed < 5, f"heal took {elapsed:.1f}s — the deadline did not bite"

    asyncio.run(_body())


def test_hung_activate_does_not_record_a_heal_that_never_landed(monkeypatch) -> None:
    """The reconciler's observed state is how it decides what still needs
    converging. Writing 'active' for an activate that timed out is how it goes
    blind — it would stop even trying to heal the actor that is actually broken."""
    monkeypatch.setenv(actor_turn.HEAL_TIMEOUT_ENV, "1")

    async def _body() -> None:
        store = _RecordingStore()
        await execute_reconcile_action(
            _ensure_active(),
            proxy_for=lambda kind, aid: _HungProxy(),
            state_store=store,
            breaker=HealBreaker(),
        )
        assert store.writes == []

        live_store = _RecordingStore()
        await execute_reconcile_action(
            _ensure_active("analyst::healthy::0"),
            proxy_for=lambda kind, aid: _LiveProxy(),
            state_store=live_store,
            breaker=HealBreaker(),
        )
        assert live_store.writes == [("analyst::healthy::0", "active")]

    asyncio.run(_body())


def test_repeated_hung_heals_open_the_breaker_and_stop_calling(monkeypatch) -> None:
    """The deadline bounds each poke; the breaker bounds their SUM. Without it a
    fleet-wide wedge costs 217 x the deadline per resync, every resync."""
    monkeypatch.setenv(actor_turn.HEAL_TIMEOUT_ENV, "1")
    monkeypatch.setenv(actor_turn.HEAL_BREAKER_TRIPS_ENV, "2")

    async def _body() -> None:
        proxy = _HungProxy()
        brk = HealBreaker()
        for _ in range(5):
            await execute_reconcile_action(
                _ensure_active(),
                proxy_for=lambda kind, aid: proxy,
                state_store=_RecordingStore(),
                breaker=brk,
            )
        assert len(proxy.calls) == 2, (
            f"breaker should have suppressed after 2 misses, got {proxy.calls}"
        )
        assert brk.should_skip("analyst::wedged::0")

    asyncio.run(_body())


def test_a_wedged_actor_never_suppresses_a_healthy_one(monkeypatch) -> None:
    monkeypatch.setenv(actor_turn.HEAL_TIMEOUT_ENV, "1")
    monkeypatch.setenv(actor_turn.HEAL_BREAKER_TRIPS_ENV, "1")

    async def _body() -> None:
        brk = HealBreaker()
        hung, live = _HungProxy(), _LiveProxy()

        def proxy_for(kind, aid):  # noqa: ANN001
            return hung if "wedged" in aid else live

        for _ in range(3):
            await execute_reconcile_action(
                _ensure_active("analyst::wedged::0"),
                proxy_for=proxy_for, state_store=_RecordingStore(), breaker=brk,
            )
            await execute_reconcile_action(
                _ensure_active("analyst::healthy::0"),
                proxy_for=proxy_for, state_store=_RecordingStore(), breaker=brk,
            )
        assert len(hung.calls) == 1, "wedged actor suppressed after its trip"
        assert len(live.calls) == 3, "healthy actor healed on every pass"

    asyncio.run(_body())


def test_one_shot_convergence_actions_are_not_breaker_gated(monkeypatch) -> None:
    """CREATE / RETIRE / TRANSITION are deadline-bounded but never SKIPPED by
    the breaker: unlike the heal they do not re-run for free, so suppressing one
    means a descriptor that never reaches its declared state."""
    monkeypatch.setenv(actor_turn.HEAL_TIMEOUT_ENV, "1")
    monkeypatch.setenv(actor_turn.HEAL_BREAKER_TRIPS_ENV, "1")

    async def _body() -> None:
        brk = HealBreaker()
        proxy = _HungProxy()
        create = ReconcileAction(
            kind=ActionKind.CREATE_ACTOR,
            actor_id="analyst::new::0",
            detail={
                "descriptor_id": "new", "descriptor_kind": "analyst",
                "descriptor_version": "0" * 16, "target_lifecycle": "active",
            },
        )
        for _ in range(3):
            await execute_reconcile_action(
                create, proxy_for=lambda kind, aid: proxy,
                state_store=_RecordingStore(), breaker=brk,
            )
        assert len(proxy.calls) == 3

    asyncio.run(_body())


def test_hung_heal_does_not_starve_the_reconcile_queue(monkeypatch) -> None:
    """End to end, against the real ReconcileLoop: the wedged descriptor is
    skipped inside the heal deadline and the rest of the queue still drains.
    This is the 08-01 signature — one descriptor GET every 90 s — not recurring."""
    monkeypatch.setenv(actor_turn.HEAL_TIMEOUT_ENV, "1")

    from legba.runtime.reconcile import DesiredState, ReconcileLoop

    class _Store:
        async def get(self, actor_id):  # noqa: ANN001
            return None

        async def list_live_siblings(self, **kw):  # noqa: ANN003
            return []

    async def _body() -> None:
        healed: list[str] = []
        brk = HealBreaker()
        hung, live = _HungProxy(), _LiveProxy()

        async def resolver(descriptor_id: str):
            return DesiredState(
                descriptor_id=descriptor_id, descriptor_kind="analyst",
                descriptor_version="0" * 16, lifecycle_target="active", body={},
            )

        async def lister():
            return []

        def proxy_for(kind, aid):  # noqa: ANN001
            return hung if "wedged" in aid else live

        async def executor(action) -> None:  # noqa: ANN001
            await execute_reconcile_action(
                action, proxy_for=proxy_for, state_store=None, breaker=brk,
            )
            if action.actor_id.split("::")[1] != "wedged":
                healed.append(action.actor_id)

        loop = ReconcileLoop(
            state_store=_Store(), desired_resolver=resolver,
            desired_lister=lister, action_executor=executor,
            run_once_timeout=timedelta(seconds=30),
        )
        await loop.start()
        try:
            loop.enqueue("wedged")     # head of line, hangs
            loop.enqueue("healthy_a")
            loop.enqueue("healthy_b")
            for _ in range(100):
                if len(healed) >= 2:
                    break
                await asyncio.sleep(0.1)
        finally:
            await loop.stop()

        assert len(healed) == 2, (
            "a hung heal must not starve the queue behind it — "
            f"drained {healed}"
        )

    asyncio.run(_body())


# ---------------------------------------------------------------------------
# The actor side — the turn itself terminates (layer 2, against real actors)
# ---------------------------------------------------------------------------


class _FakeStateManager:
    def __init__(self) -> None:
        self._store: dict = {}

    async def try_get_state(self, name: str):
        if name in self._store:
            return True, self._store[name]
        return False, None

    async def set_state(self, name: str, value) -> None:  # noqa: ANN001
        self._store[name] = value

    async def save_state(self) -> None:
        return None


class _FakeActorId:
    def __init__(self, actor_id: str) -> None:
        self.id = actor_id


@pytest.fixture()
def _clean_deps_registry():
    from legba.runtime import dapr_actors

    dapr_actors.clear_deps_registry()
    yield dapr_actors
    dapr_actors.clear_deps_registry()


def test_analyst_on_activate_releases_the_turn_on_a_hung_deps_lookup(
    monkeypatch, _clean_deps_registry,
) -> None:
    """THE 2026-08-01 HANG POINT, against the real ``AnalystActor``.

    ``_resolve_analyst_deps`` falls through to a registry fetch and caches
    nothing on failure, so a saturated registry made every activation an
    unbounded wait — and a turn-based actor holds its turn for the whole of it.
    ``_on_activate`` must now RETURN.
    """
    dapr_actors = _clean_deps_registry
    from legba.runtime.dapr_actors import AnalystActor

    monkeypatch.setenv(actor_turn.TURN_OP_TIMEOUT_ENV, "1")

    resolver_entered = asyncio.Event()

    async def hanging_resolver(actor_id: str):
        resolver_entered.set()
        await asyncio.Event().wait()      # the registry that never answers

    dapr_actors.register_analyst_deps_resolver(hanging_resolver)

    async def _body() -> None:
        actor = object.__new__(AnalystActor)
        actor.id = _FakeActorId("analyst::military_posture::deadbeefdeadbeef")
        actor._state_manager = _FakeStateManager()

        await asyncio.wait_for(actor._on_activate(), timeout=10)
        assert resolver_entered.is_set(), "the hang must actually have been entered"

    asyncio.run(_body())


def test_analyst_activate_returns_instead_of_parking(
    monkeypatch, _clean_deps_registry,
) -> None:
    """The public ``activate()`` — what reconcile's ENSURE_ACTIVE invokes — must
    also come back, so the turn queue behind it drains."""
    dapr_actors = _clean_deps_registry
    from legba.runtime.dapr_actors import AnalystActor

    monkeypatch.setenv(actor_turn.TURN_OP_TIMEOUT_ENV, "1")

    async def hanging_resolver(actor_id: str):
        await asyncio.Event().wait()

    dapr_actors.register_analyst_deps_resolver(hanging_resolver)

    async def _body() -> None:
        actor = object.__new__(AnalystActor)
        actor.id = _FakeActorId("analyst::military_posture::deadbeefdeadbeef")
        actor._state_manager = _FakeStateManager()

        rec = await asyncio.wait_for(actor.activate(), timeout=10)
        assert rec == {}, "no record was created — activation did not confirm"

    asyncio.run(_body())


def test_source_core_releases_the_turn_on_a_hung_deps_lookup(monkeypatch) -> None:
    """``SourceActor._core`` is the source-side copy of the same hang point; a
    timeout must rejoin the existing ``sd is None`` branch."""
    from legba.runtime import source_actor as sa

    if sa.SourceActor is None:  # pragma: no cover — dapr SDK absent
        pytest.skip("SourceActor unavailable (dapr SDK not installed)")

    monkeypatch.setenv(actor_turn.TURN_OP_TIMEOUT_ENV, "1")

    async def hanging_resolve(actor_id: str):
        await asyncio.Event().wait()

    monkeypatch.setattr(sa, "resolve_source_deps", hanging_resolve)

    async def _body() -> None:
        actor = object.__new__(sa.SourceActor)
        actor.id = _FakeActorId("source::reuters_rss::deadbeefdeadbeef")
        actor._state_manager = _FakeStateManager()

        core = await asyncio.wait_for(actor._core(), timeout=10)
        assert core is None

    asyncio.run(_body())


def test_deadline_does_not_by_itself_unwedge_the_actor() -> None:
    """The half of S-6 that is easy to get wrong, pinned so it is not forgotten.

    Cancelling the CALLER's coroutine does not release the callee's turn: the
    actor runtime holds a per-id lock inside the app process. So the reconcile
    deadline stops the queue PAYING for a wedged actor, but only bounding the
    op INSIDE the turn (see the ``bounded_turn_op`` wiring in
    ``AnalystActor._on_activate`` / ``SourceActor._core``) makes the turn
    complete and lets the reminders queued behind it proceed. This test models
    that lock and asserts the property, so a future 'simplification' that drops
    the actor-side bounds has to argue with it.
    """
    async def _body() -> None:
        turn_lock = asyncio.Lock()
        second_turn_ran = asyncio.Event()

        async def actor_call(bounded: bool) -> None:
            async with turn_lock:
                inner = asyncio.Event().wait()
                if bounded:
                    await bounded_turn_op_or(
                        inner, None, op="t", actor_id="a", timeout=0.05,
                    )
                else:
                    await inner

        async def queued_behind() -> None:
            async with turn_lock:
                second_turn_ran.set()

        # UNBOUNDED inside the turn: the caller's deadline fires, but the turn
        # lock is still held, so the fire queued behind it never runs.
        caller = asyncio.create_task(actor_call(bounded=False))
        behind = asyncio.create_task(queued_behind())
        await asyncio.sleep(0.2)
        assert not second_turn_ran.is_set(), (
            "a caller-side deadline alone cannot release a held actor turn"
        )
        caller.cancel()
        behind.cancel()
        await asyncio.gather(caller, behind, return_exceptions=True)

        # BOUNDED inside the turn: the turn completes and the queue drains.
        turn_lock = asyncio.Lock()
        second_turn_ran = asyncio.Event()
        caller = asyncio.create_task(actor_call(bounded=True))
        behind = asyncio.create_task(queued_behind())
        await asyncio.wait_for(second_turn_ran.wait(), timeout=5)
        await asyncio.gather(caller, behind, return_exceptions=True)

    asyncio.run(_body())
