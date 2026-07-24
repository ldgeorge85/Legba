# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A2 — per-(analyst, target) CONCURRENT analyst workers.

Today ``country_assessor``'s ~19 per-country cadence runs serialize through
ONE primary actor turn-queue. A2 makes them concurrent:

  * Worker actor id = ``analyst::<descriptor_id>::<target_id>`` — a DISTINCT
    Dapr virtual actor per target ⇒ distinct turn-queue ⇒ concurrent.
  * The analyst deps fallback resolver keys off segment-1 (the descriptor_id)
    via ``split("::", 2)[1]``, so a worker reconstructs the analyst's deps
    with NO new registration.
  * The PRIMARY keeps the cadence reminder; its ``receive_reminder`` fan-out
    dispatches to the worker actors BOUNDED-CONCURRENT (semaphore ~5) instead
    of serial ``self.run``.
  * The coalesced-fire path routes to the worker, not the primary.
  * Workers LAZY-ACTIVATE in ``run`` (minimal ACTIVE record, NO reminder).
  * Meta analysts (no ``subscription.targets``) keep the single global run.

These tests exercise the logic at the function / actor-method level with a
fake state manager + a monkeypatched worker-proxy factory, so they need no
live daprd sidecar. They run in-container against ``legba_pivot_test`` only
incidentally (no DB access here) — pure-logic coverage of the A2 contract.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from legba.data.schemas.analyst import (
    AnalystDescriptor,
    AnalystIdentity,
    AnalystKind,
    CadenceBlock,
    MappingBlock,
    MethodBlock,
    SubscriptionBlock,
    SubscriptionTargets,
    TypeSignature,
)
from legba.data.schemas.lifecycle import LifecycleState
from legba.data.schemas.properties import Property
from legba.runtime import dapr_actors, source_first_runtime
from legba.runtime.dapr_actors import (
    ACTIVE,
    AnalystActor,
    _AnalystDeps,
    _split_actor_id,
    _worker_actor_id,
)


pytestmark = [pytest.mark.integration]


# ---------------------------------------------------------------------------
# Lightweight fakes — let us drive AnalystActor methods without a sidecar.
# ---------------------------------------------------------------------------


class _FakeStateManager:
    """In-memory stand-in for Dapr's ActorStateManager.

    Stores state names in a dict; ``save_state`` is a no-op (the dict IS the
    durable store for the test). Mirrors the ``try_get_state`` /
    ``set_state`` surface the actor uses.
    """

    def __init__(self) -> None:
        self._store: dict[str, Any] = {}

    async def try_get_state(self, name: str):
        if name in self._store:
            return True, self._store[name]
        return False, None

    async def set_state(self, name: str, value: Any) -> None:
        self._store[name] = value

    async def save_state(self) -> None:
        return None


class _FakeActorId:
    def __init__(self, actor_id: str) -> None:
        self.id = actor_id


class _FakeConn:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakePool:
    def acquire(self):
        return _FakeConn()


class _FakeStandardDeps:
    """Stub StandardDeps — only the fields the run path touches."""

    def __init__(self) -> None:
        self.pg_pool = _FakePool()
        self.nats_publish = None


def _make_actor(actor_id: str) -> AnalystActor:
    """Instantiate an AnalystActor bypassing the Dapr ctor (no runtime ctx)."""
    actor = object.__new__(AnalystActor)
    actor.id = _FakeActorId(actor_id)
    actor._state_manager = _FakeStateManager()
    return actor


def _build_descriptor(
    *,
    analyst_id: str = "country_assessor",
    with_targets: bool = True,
    version: str = "0" * 64,
) -> AnalystDescriptor:
    """Minimal target-bound (or meta) analyst descriptor.

    ``with_targets=False`` builds a META analyst (no subscription.targets) —
    the global-run path that A2 leaves UNCHANGED.
    """
    sub = SubscriptionBlock(
        targets=SubscriptionTargets(
            predicate=None,
            data_types=["signal"],
            time_window="24h",
        )
        if with_targets
        else None,
    )
    return AnalystDescriptor(
        identity=AnalystIdentity(
            id=analyst_id,
            name=f"{analyst_id} (a2-test)",
            schema_uri="legba/analyst/1.0.0",
            version=version,
            kind=AnalystKind.INLINE_TARGET,
            type_signature=TypeSignature(
                input_type="legba.runtime.SignalList",
                output_type="legba.runtime.Finding",
            ),
            state=LifecycleState.ACTIVE,
            owner="a2_test",
        ),
        subscription=sub,
        mapping=MappingBlock(),
        method=MethodBlock(
            kind="llm_single_turn",
            prompt_module="legba.runtime.analyst_method:_DEFAULT_SYSTEM",
            llm={
                "primary": Property.StackRef(
                    raw="llm.primary.openai_compat",
                    expected_family="llm_provider",
                ).model_dump(),
                "max_tokens": 1024,
            },
        ),
        cadence=CadenceBlock(
            fallback_schedule="*/10 * * * *",
            cooldown_seconds=300,
        ),
        outputs=[],
    )


def _make_deps(
    descriptor: AnalystDescriptor,
    *,
    read_slice=None,
) -> _AnalystDeps:
    # ``_AnalystDeps.deps`` is strictly typed as the StandardDeps dataclass
    # (pydantic arbitrary-type isinstance check), and StandardDeps.pg_pool is
    # an ``asyncpg.Pool`` we can't cheaply fake through validation. Use
    # ``model_construct`` to bypass validation — these are pure test stubs and
    # the run path only touches ``deps.deps.pg_pool`` / ``nats_publish``.
    return _AnalystDeps.model_construct(
        descriptor=descriptor,
        deps=_FakeStandardDeps(),
        run_method=_unused_run_method,
        kind_deps=None,
        output_kind=dapr_actors.OutputKind.FINDING,
        budget=None,
        fallback_run_method=None,
        fallback_kind_deps=None,
        primary_llm_ref="",
        fallback_llm_ref="",
        receipt_chain=None,
        read_slice=read_slice,
    )


async def _unused_run_method(*args, **kwargs):  # pragma: no cover — guard
    raise AssertionError("run_method should not be reached in these tests")


async def _empty_read_slice(conn, *, descriptor, target_filter):
    """A read_slice that yields no inputs — run() short-circuits to NOOP
    AFTER any record creation, so it isolates the lazy-activate behaviour."""
    return []


@pytest.fixture(autouse=True)
def _reset_deps_registry():
    dapr_actors.clear_deps_registry()
    yield
    dapr_actors.clear_deps_registry()


# ---------------------------------------------------------------------------
# 1. Worker id derivation + decomposition
# ---------------------------------------------------------------------------


def test_worker_actor_id_grammar():
    """Worker id = analyst::<descriptor_id>::<target_id>; segment-1 is the
    descriptor_id either way (primary or worker)."""
    wid = _worker_actor_id("country_assessor", "brazil")
    assert wid == "analyst::country_assessor::brazil"

    kind, descriptor_id, tail = _split_actor_id(wid)
    assert kind == "analyst"
    assert descriptor_id == "country_assessor"  # ← the deps-resolver key
    assert tail == "brazil"


def test_worker_id_constructors_agree_across_modules():
    """The dapr_actors constructor and the source_first_runtime mirror must
    produce the SAME id so the fan-out and the coalesced-fire route to the
    same Dapr actor."""
    a = _worker_actor_id("country_assessor", "us")
    b = source_first_runtime.worker_actor_id("country_assessor", "us")
    assert a == b == "analyst::country_assessor::us"


def test_primary_vs_worker_share_segment1():
    """A primary id (content-hash tail) and a worker id (target_id tail) share
    segment-1 — that is WHY the segment-1 fallback resolver reconstructs the
    SAME analyst's deps for a worker with no new registration."""
    primary = "analyst::country_assessor::" + "a" * 16
    worker = _worker_actor_id("country_assessor", "brazil")
    assert _split_actor_id(primary)[1] == _split_actor_id(worker)[1]


# ---------------------------------------------------------------------------
# 2. Deps resolve for a worker id via the segment-1 resolver (no new reg)
# ---------------------------------------------------------------------------


async def test_worker_id_resolves_deps_via_segment1():
    """The analyst deps fallback resolver keys off ``split('::', 2)[1]``. A
    resolver that returns deps for the descriptor_id therefore serves BOTH the
    primary and any per-target worker — no per-worker registration needed."""
    descriptor = _build_descriptor()
    deps = _make_deps(descriptor)

    seen_ids: list[str] = []

    async def resolver(actor_id: str):
        seen_ids.append(actor_id)
        # Mimic dapr_host._analyst_deps_resolver: it parses segment-1 and
        # fetches the head descriptor. We assert segment-1 is the descriptor_id.
        _kind, descriptor_id, _tail = _split_actor_id(actor_id)
        assert descriptor_id == "country_assessor"
        return deps

    dapr_actors.register_analyst_deps_resolver(resolver)

    worker_id = _worker_actor_id("country_assessor", "brazil")
    out = await dapr_actors._resolve_analyst_deps(worker_id)
    assert out is deps
    assert seen_ids == [worker_id]


# ---------------------------------------------------------------------------
# 3. Bounded-concurrent fan-out from the primary's receive_reminder
# ---------------------------------------------------------------------------


async def test_cadence_fanout_is_bounded_concurrent(monkeypatch):
    """receive_reminder on a TARGET-BOUND primary fans out to per-target
    WORKER actors via ActorProxy, bounded at _FANOUT_CHUNK. Each call carries
    the target_filter; the primary does NOT run the work itself."""
    descriptor = _build_descriptor()
    deps = _make_deps(descriptor)

    async def resolver(actor_id: str):
        return deps

    dapr_actors.register_analyst_deps_resolver(resolver)

    targets = [f"country_{i}" for i in range(19)]

    primary_id = "analyst::country_assessor::" + "0" * 16
    actor = _make_actor(primary_id)

    # Stub _cadence_targets so we don't need a DB; this is the matched set.
    async def _fake_targets():
        return list(targets)

    actor._cadence_targets = _fake_targets  # type: ignore[assignment]

    # The primary must NOT run the per-target work itself.
    async def _no_self_run(payload=None):  # pragma: no cover — guard
        raise AssertionError("primary must fan out, not self.run per target")

    actor.run = _no_self_run  # type: ignore[assignment]

    # Capture worker dispatches + observe live concurrency.
    dispatched: list[tuple[str, str]] = []  # (worker_id, target_filter)
    live = 0
    max_live = 0
    lock = asyncio.Lock()

    class _FakeProxy:
        def __init__(self, worker_id: str) -> None:
            self._wid = worker_id

        async def run(self, payload: dict[str, Any]) -> dict[str, Any]:
            nonlocal live, max_live
            async with lock:
                live += 1
                max_live = max(max_live, live)
            await asyncio.sleep(0.01)  # hold the slot so concurrency is observable
            dispatched.append((self._wid, payload.get("target_filter")))
            async with lock:
                live -= 1
            return {"outcome": ACTIVE}

    def _fake_factory():
        return lambda worker_id: _FakeProxy(worker_id)

    monkeypatch.setattr(dapr_actors, "_worker_proxy_factory", _fake_factory)
    monkeypatch.setattr(dapr_actors, "_FANOUT_CHUNK", 5)

    await actor.receive_reminder("run_cadence", b"{}", None, None)

    # Every target got exactly one worker dispatch, target-scoped.
    assert len(dispatched) == 19
    dispatched_targets = {t for _wid, t in dispatched}
    assert dispatched_targets == set(targets)
    # Each dispatch routed to the right worker actor id.
    for wid, t in dispatched:
        assert wid == _worker_actor_id("country_assessor", t)
    # Concurrency was REALIZED (not serial: max_live > 1) yet BOUNDED (<= chunk).
    assert max_live > 1
    assert max_live <= 5


async def test_meta_analyst_cadence_stays_single_global_run():
    """A META analyst (no subscription.targets) keeps the single global
    self.run — A2 leaves this path UNCHANGED (no fan-out, no workers)."""
    descriptor = _build_descriptor(with_targets=False)
    deps = _make_deps(descriptor)

    async def resolver(actor_id: str):
        return deps

    dapr_actors.register_analyst_deps_resolver(resolver)

    actor = _make_actor("analyst::optimizer::" + "0" * 16)

    # Real _cadence_targets would return None for a meta analyst (no targets);
    # assert that here directly so we exercise the discriminator.
    targets = await actor._cadence_targets()
    assert targets is None

    runs: list[dict[str, Any]] = []

    async def _capture_run(payload=None):
        runs.append(payload or {})
        return {"outcome": "noop"}

    actor.run = _capture_run  # type: ignore[assignment]

    # The fan-out helper must NOT be reached for a meta analyst.
    async def _no_fanout(_targets):  # pragma: no cover — guard
        raise AssertionError("meta analyst must not fan out to workers")

    actor._fanout_to_workers = _no_fanout  # type: ignore[assignment]

    await actor.receive_reminder("run_cadence", b"{}", None, None)

    assert len(runs) == 1
    # Single GLOBAL run — no target_filter.
    assert "target_filter" not in runs[0]
    assert runs[0].get("trigger_kind") == "cadence"


# ---------------------------------------------------------------------------
# 3b. Critic tier-2 fan-out — a critic META analyst grades ungraded
#     analyzed-analyst findings (one bounded worker run per finding id),
#     instead of NOOPing a global run forever (the inert-eval-loop fix).
# ---------------------------------------------------------------------------


def _build_critic_descriptor(
    *, analyst_id: str = "country_critic", pinned: str | None = "country_assessor",
) -> AnalystDescriptor:
    """A META critic descriptor (no subscription.targets) pinned to grade
    ``pinned`` via ``eval.optimizer.analyzed_analyst_id``."""
    from legba.data.schemas.analyst import EvalBlock
    return AnalystDescriptor(
        identity=AnalystIdentity(
            id=analyst_id,
            name=f"{analyst_id} (critic-test)",
            schema_uri="legba/analyst/1.0.0",
            version="0" * 64,
            kind=AnalystKind.CRITIC,
            type_signature=TypeSignature(
                input_type="legba.runtime.AnalystOutput",
                output_type="legba.runtime.Critique",
            ),
            state=LifecycleState.ACTIVE,
            owner="critic_test",
        ),
        subscription=SubscriptionBlock(targets=None),  # META — no target binding
        mapping=MappingBlock(),
        method=MethodBlock(
            kind="critic",
            prompt_module="legba.runtime.analyst_method:_DEFAULT_SYSTEM",
            llm={
                "primary": Property.StackRef(
                    raw="llm.primary.anthropic",
                    expected_family="llm_provider",
                ).model_dump(),
                "max_tokens": 1024,
            },
        ),
        cadence=CadenceBlock(
            fallback_schedule="0 */2 * * *",
            cooldown_seconds=6600,
        ),
        outputs=[],
        eval=EvalBlock(
            optimizer={"analyzed_analyst_id": pinned} if pinned else None,
        ),
    )


class _FetchConn:
    """Fake asyncpg conn whose ``fetch`` returns preset rows."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows
        self.fetched_args: tuple[Any, ...] | None = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def fetch(self, _query: str, *args: Any) -> list[dict[str, Any]]:
        self.fetched_args = args
        return self._rows


class _FetchPool:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def acquire(self):
        return _FetchConn(self._rows)


def _critic_deps(descriptor: AnalystDescriptor, rows: list[dict[str, Any]]) -> _AnalystDeps:
    deps = _make_deps(descriptor)
    deps.deps.pg_pool = _FetchPool(rows)
    return deps


async def test_critic_ungraded_targets_returns_finding_ids():
    """A critic resolves its newest-N ungraded analyzed-analyst finding ids."""
    rows = [{"id": "fid-1"}, {"id": "fid-2"}]
    deps = _critic_deps(_build_critic_descriptor(), rows)
    dapr_actors.register_analyst_deps_resolver(lambda actor_id: _coro(deps))
    actor = _make_actor("analyst::country_critic::" + "0" * 16)
    out = await actor._critic_ungraded_targets()
    assert out == ["fid-1", "fid-2"]


async def test_critic_ungraded_targets_none_for_non_critic():
    """Non-critic meta analysts return None → caller does the global run."""
    deps = _critic_deps(_build_descriptor(with_targets=False), [{"id": "x"}])
    dapr_actors.register_analyst_deps_resolver(lambda actor_id: _coro(deps))
    actor = _make_actor("analyst::world_assessor::" + "0" * 16)
    assert await actor._critic_ungraded_targets() is None


async def test_critic_ungraded_targets_empty_without_pin():
    """A critic with no analyzed-analyst pin grades nothing (no NOOP loop)."""
    deps = _critic_deps(_build_critic_descriptor(pinned=None), [{"id": "x"}])
    dapr_actors.register_analyst_deps_resolver(lambda actor_id: _coro(deps))
    actor = _make_actor("analyst::country_critic::" + "0" * 16)
    assert await actor._critic_ungraded_targets() == []


async def test_critic_cadence_fans_out_ungraded_findings(monkeypatch):
    """receive_reminder on a critic META analyst dispatches ONE bounded worker
    grade per ungraded finding (target_filter=<finding_id>), NOT a global
    self.run — end-to-end through the real _cadence_targets/_critic_ungraded
    path."""
    rows = [{"id": "fid-1"}, {"id": "fid-2"}, {"id": "fid-3"}]
    deps = _critic_deps(_build_critic_descriptor(), rows)
    dapr_actors.register_analyst_deps_resolver(lambda actor_id: _coro(deps))

    actor = _make_actor("analyst::country_critic::" + "0" * 16)

    async def _no_self_run(payload=None):  # pragma: no cover — guard
        raise AssertionError("critic must fan out per finding, not global self.run")

    actor.run = _no_self_run  # type: ignore[assignment]

    dispatched: list[tuple[str, str]] = []

    class _FakeProxy:
        def __init__(self, worker_id: str) -> None:
            self._wid = worker_id

        async def run(self, payload: dict[str, Any]) -> dict[str, Any]:
            dispatched.append((self._wid, payload.get("target_filter")))
            return {"outcome": ACTIVE}

    monkeypatch.setattr(
        dapr_actors, "_worker_proxy_factory",
        lambda: (lambda worker_id: _FakeProxy(worker_id)),
    )

    await actor.receive_reminder("run_cadence", b"{}", None, None)

    assert {t for _wid, t in dispatched} == {"fid-1", "fid-2", "fid-3"}
    for wid, t in dispatched:
        assert wid == _worker_actor_id("country_critic", t)


async def _coro(value: Any) -> Any:
    """Wrap a value as an awaitable for the deps resolver (it is awaited)."""
    return value


# ---------------------------------------------------------------------------
# 4. Lazy-activate: worker creates a minimal ACTIVE record inline, no reminder
# ---------------------------------------------------------------------------


async def test_worker_lazy_activates_minimal_active_record():
    """A worker reached with NO prior record + a target_filter creates a
    minimal ACTIVE record inline and proceeds (here to NOOP/no_inputs because
    the read_slice is empty). It registers NO reminder."""
    descriptor = _build_descriptor()
    deps = _make_deps(descriptor, read_slice=_empty_read_slice)

    async def resolver(actor_id: str):
        return deps

    dapr_actors.register_analyst_deps_resolver(resolver)

    worker_id = _worker_actor_id("country_assessor", "brazil")
    actor = _make_actor(worker_id)

    # Guard: a worker must NOT register a reminder (lazy-activate path only).
    actor.register_reminder = _forbid_reminder  # type: ignore[assignment]

    # No record exists yet (fresh fake state manager).
    assert await actor._get_record() is None

    result = await actor.run({"trigger_kind": "cadence", "target_filter": "brazil"})

    # The run created the record (lazy-activate) and then short-circuited on
    # the empty slice.
    assert result["outcome"] == dapr_actors.ActorRunOutcome.NOOP.value
    assert result["reason"] == "no_inputs"

    rec = await actor._get_record()
    assert rec is not None
    assert rec["lifecycle"] == ACTIVE
    assert rec["actor_kind"] == "analyst"
    assert rec["descriptor_id"] == "country_assessor"
    assert rec["worker_target"] == "brazil"


async def test_worker_no_target_filter_stays_noop_no_state():
    """A worker-shaped run with NO record AND no target_filter does NOT
    lazy-activate — it stays NOOP/no_state (only target-scoped fires/fan-outs
    reach a worker, so a no-filter run with no record is a defensive edge)."""
    descriptor = _build_descriptor()
    deps = _make_deps(descriptor, read_slice=_empty_read_slice)

    async def resolver(actor_id: str):
        return deps

    dapr_actors.register_analyst_deps_resolver(resolver)

    actor = _make_actor(_worker_actor_id("country_assessor", "brazil"))
    actor.register_reminder = _forbid_reminder  # type: ignore[assignment]

    result = await actor.run({"trigger_kind": "cadence"})  # no target_filter
    assert result["outcome"] == dapr_actors.ActorRunOutcome.NOOP.value
    assert result["reason"] == "no_state"
    assert await actor._get_record() is None


async def test_on_activate_skips_record_and_reminder_for_worker():
    """_on_activate on a WORKER actor (tail = target_id, not the descriptor
    content-hash) creates NO record and registers NO reminder — the worker is
    purely on-demand, reachable only via the primary's fan-out / a fire."""
    version = "deadbeefdeadbeef" + "0" * 48  # 64-char content hash
    descriptor = _build_descriptor(version=version)
    deps = _make_deps(descriptor)

    async def resolver(actor_id: str):
        return deps

    dapr_actors.register_analyst_deps_resolver(resolver)

    worker_id = _worker_actor_id("country_assessor", "brazil")
    actor = _make_actor(worker_id)
    actor.register_reminder = _forbid_reminder  # type: ignore[assignment]

    await actor._on_activate()

    # No record was created — the worker stays dormant until run() lazy-acts.
    assert await actor._get_record() is None


async def test_on_activate_creates_record_and_reminder_for_primary(monkeypatch):
    """_on_activate on a PRIMARY actor (tail == descriptor.version[:16])
    creates the ACTIVE record AND registers the cadence reminder — the
    behaviour A2 must leave intact for the primary."""
    version = "deadbeefdeadbeef" + "0" * 48
    descriptor = _build_descriptor(version=version)
    deps = _make_deps(descriptor)

    async def resolver(actor_id: str):
        return deps

    dapr_actors.register_analyst_deps_resolver(resolver)

    primary_id = "analyst::country_assessor::" + version[:16]
    actor = _make_actor(primary_id)

    reminders: list[str] = []

    async def _capture_reminder(*, name, state, due_time, period, ttl=None):
        reminders.append(name)

    actor.register_reminder = _capture_reminder  # type: ignore[assignment]

    await actor._on_activate()

    rec = await actor._get_record()
    assert rec is not None
    assert rec["lifecycle"] == ACTIVE
    assert "worker_target" not in rec  # primary record, not a worker record
    assert reminders == ["run_cadence"]


# ---------------------------------------------------------------------------
# 5. Coalesced-fire path routes to the worker (source_first_runtime)
# ---------------------------------------------------------------------------


class _FakeReason:
    value = "accumulation"


class _FakeFire:
    def __init__(self, analyst_id: str, target_id: str | None) -> None:
        self.analyst_id = analyst_id
        self.target_id = target_id
        self.reason = _FakeReason()
        self.pending_count = 3


async def test_coalesced_fire_routes_to_worker(monkeypatch):
    """A target-scoped fire routes to the WORKER actor id, not the primary —
    so per-target fires hit distinct virtual actors and run concurrently."""
    created: list[str] = []
    run_payloads: list[dict[str, Any]] = []

    class _FakeProxy:
        async def run(self, payload):
            run_payloads.append(payload)
            return {"outcome": "success"}

    class _FakeActorProxy:
        @staticmethod
        def create(actor_type, actor_id, iface=None, actor_proxy_factory=None):
            # Mirror the real dapr ``ActorProxy.create`` signature — the
            # trigger-dispatch + fan-out paths now pass an explicit
            # ``actor_proxy_factory`` carrying the larger invoke-timeout budget.
            created.append(str(actor_id))
            return _FakeProxy()

    class _FakeActorId:
        def __init__(self, s):
            self._s = s

        def __str__(self):
            return self._s

    import dapr.actor as dapr_actor_mod

    monkeypatch.setattr(dapr_actor_mod, "ActorProxy", _FakeActorProxy)
    monkeypatch.setattr(dapr_actor_mod, "ActorId", _FakeActorId)

    # §2.1 lifecycle gate: only LIVE analysts dispatch — mark this one live.
    primary = "analyst::country_assessor::" + "a" * 16
    source_first_runtime.remember_analyst_actor_id("country_assessor", primary)
    try:
        work = source_first_runtime.build_trigger_work(None)
        out = await work(_FakeFire("country_assessor", "brazil"))

        assert created == ["analyst::country_assessor::brazil"]
        assert run_payloads[0]["target_filter"] == "brazil"
        assert run_payloads[0]["trigger_kind"] == "coalesced_fire"
        assert out["target_id"] == "brazil"
    finally:
        source_first_runtime.forget_analyst_actor_id("country_assessor", primary)


async def test_coalesced_fire_without_target_falls_back_to_primary(monkeypatch):
    """A fire with NO target_id (meta-style) routes to the PRIMARY (global
    run), not a worker — preserving the unchanged meta path."""
    created: list[str] = []

    class _FakeProxy:
        async def run(self, payload):
            return {"outcome": "success"}

    class _FakeActorProxy:
        @staticmethod
        def create(actor_type, actor_id, iface=None, actor_proxy_factory=None):
            # Mirror the real dapr ``ActorProxy.create`` signature — the
            # trigger-dispatch + fan-out paths now pass an explicit
            # ``actor_proxy_factory`` carrying the larger invoke-timeout budget.
            created.append(str(actor_id))
            return _FakeProxy()

    class _FakeActorId:
        def __init__(self, s):
            self._s = s

        def __str__(self):
            return self._s

    import dapr.actor as dapr_actor_mod

    monkeypatch.setattr(dapr_actor_mod, "ActorProxy", _FakeActorProxy)
    monkeypatch.setattr(dapr_actor_mod, "ActorId", _FakeActorId)

    # Prime the analyst_id → actor_id cache so the primary id is deterministic.
    source_first_runtime.remember_analyst_actor_id(
        "optimizer", "analyst::optimizer::" + "f" * 16
    )

    work = source_first_runtime.build_trigger_work(None)
    await work(_FakeFire("optimizer", None))

    assert created == ["analyst::optimizer::" + "f" * 16]


async def _forbid_reminder(*args, **kwargs):  # pragma: no cover — guard
    raise AssertionError("worker actors must NOT register a reminder")


# ---------------------------------------------------------------------------
# C1 — gather_only must NOT NOOP at the ACTOR-level no_inputs gate.
# ---------------------------------------------------------------------------


class _ReachedDispatch(Exception):
    """Sentinel raised by the recorder run_method to prove the actor reached
    dispatch (past the no_inputs gate) without traversing the heavy downstream."""


def _build_gather_only_descriptor() -> AnalystDescriptor:
    """A META inline_target that OPTS IN to gather_only — NO subscription.targets,
    substrate carries ``gather_only: true`` (the corpus_researcher shape)."""
    return AnalystDescriptor(
        identity=AnalystIdentity(
            id="corpus_researcher",
            name="corpus researcher (gather_only test)",
            schema_uri="legba/analyst/1.0.0",
            version="0" * 64,
            kind=AnalystKind.INLINE_TARGET,
            type_signature=TypeSignature(
                input_type="legba.runtime.SignalList",
                output_type="legba.runtime.Finding",
            ),
            state=LifecycleState.ACTIVE,
            owner="stage4_test",
        ),
        subscription=SubscriptionBlock(
            targets=None,
            substrate={"direct_queries": True, "gather_only": True},
        ),
        mapping=MappingBlock(),
        method=MethodBlock(
            kind="llm_single_turn",
            prompt_module="legba.runtime.analyst_method:_DEFAULT_SYSTEM",
            llm={
                "primary": Property.StackRef(
                    raw="llm.primary.openai_compat",
                    expected_family="llm_provider",
                ).model_dump(),
                "max_tokens": 1024,
            },
        ),
        cadence=CadenceBlock(
            fallback_schedule="*/10 * * * *",
            cooldown_seconds=300,
        ),
        outputs=[],
    )


async def test_actor_gather_only_empty_slice_reaches_dispatch():
    """C1 (BLOCKER): the ACTOR-level no_inputs gate must NOT short-circuit a
    gather_only descriptor. With ``_read_substrate_slice`` returning [], the actor
    must FALL THROUGH and dispatch run_method(inputs=[], options.gather_only=True)
    instead of returning NOOP/no_inputs — otherwise the corpus_researcher NOOPs
    every tick forever and its whole gather_only path is dead code. Exercised over
    the real AnalystActor.run seam (NOT a direct run_method call)."""
    descriptor = _build_gather_only_descriptor()

    reached: dict[str, Any] = {"called": False}

    async def _recorder_run_method(inputs, options):
        # kind_deps is None in this stub → _invoke_run_method uses the 2-arg call.
        reached["called"] = True
        reached["inputs"] = list(inputs)
        reached["gather_only"] = options.get("gather_only")
        # Short-circuit the heavy post-dispatch write/emit path — the C1 claim is
        # only that DISPATCH was reached, which is now recorded.
        raise _ReachedDispatch()

    deps = _AnalystDeps.model_construct(
        descriptor=descriptor,
        deps=_FakeStandardDeps(),
        run_method=_recorder_run_method,
        kind_deps=None,
        output_kind=dapr_actors.OutputKind.FINDING,
        budget=None,
        fallback_run_method=None,
        fallback_kind_deps=None,
        primary_llm_ref="",
        fallback_llm_ref="",
        receipt_chain=None,
        read_slice=_empty_read_slice,
    )

    async def resolver(actor_id: str):
        return deps

    dapr_actors.register_analyst_deps_resolver(resolver)

    actor = _make_actor("analyst::corpus_researcher::" + "0" * 16)
    # Seed the ACTIVE meta/primary record _on_activate would have written (this
    # test bypasses activation), so run() proceeds past the no_state / lifecycle /
    # cooldown guards to the no_inputs gate — the seam under test.
    await actor._set_record({
        "actor_id": actor.id.id,
        "actor_kind": "analyst",
        "descriptor_id": "corpus_researcher",
        "descriptor_version": "0" * 64,
        "lifecycle": ACTIVE,
        "last_run_at": None,
        "last_outcome": None,
        "cooldown_until": None,
        "error_count": 0,
        "last_error": None,
    })

    result: Any = None
    try:
        result = await actor.run({"trigger_kind": "cadence"})
    except _ReachedDispatch:
        pass
    except Exception:
        # Downstream hard-fail handling over the fake conn/state may raise AFTER
        # dispatch; the C1 assertion is that dispatch was REACHED (recorder set).
        pass

    assert reached["called"] is True, (
        "gather_only descriptor NOOPed at the actor gate — run_method was never "
        "dispatched (C1 regression: the feature is dead)"
    )
    assert reached["inputs"] == []          # dispatched with the empty slice
    assert reached["gather_only"] is True   # the flag threaded through options
    if result is not None:
        assert result.get("reason") != "no_inputs"


async def test_actor_non_gather_only_empty_slice_still_noops():
    """Control for C1: a NORMAL (non-gather_only) descriptor with an empty slice
    still NOOPs at the actor gate — the fall-through is scoped to gather_only."""
    descriptor = _build_descriptor()  # no gather_only substrate

    async def _forbid_run_method(*args, **kwargs):  # pragma: no cover — guard
        raise AssertionError("normal empty-slice run must NOOP, not dispatch")

    deps = _make_deps(descriptor, read_slice=_empty_read_slice)
    object.__setattr__(deps, "run_method", _forbid_run_method)

    async def resolver(actor_id: str):
        return deps

    dapr_actors.register_analyst_deps_resolver(resolver)

    actor = _make_actor(_worker_actor_id("country_assessor", "brazil"))
    result = await actor.run({"trigger_kind": "cadence", "target_filter": "brazil"})

    assert result["outcome"] == dapr_actors.ActorRunOutcome.NOOP.value
    assert result["reason"] == "no_inputs"
