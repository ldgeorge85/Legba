# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Actor-level lifecycle idempotency + resume/resurrect (runtime FSM fix).

These cover the family of runtime lifecycle bugs reproduced live while
pausing+resuming an analyst:

  * idempotent retire — RETIRE_ACTOR on an already-retired actor no-ops
    (no IllegalTransition / 500; the reconcile.version_drift.retire_failed
    symptom);
  * idempotent pause — re-pausing a paused actor no-ops;
  * resume — PAUSED → ACTIVE re-registers the cadence reminder pause tore
    down, and the run path stops NOOP'ing ``lifecycle=paused``;
  * resurrect-on-restore — activate() on a PAUSED/RETIRED record (driven
    when the descriptor head declares active, e.g. a rollback-restored
    version) brings the record back to ACTIVE so runs execute again.

Pure-logic: a fake state manager + a stubbed deps resolver, no daprd.
Mirrors the harness in test_analyst_concurrent_workers.py.
"""

from __future__ import annotations

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
    TypeSignature,
)
from legba.data.schemas.lifecycle import LifecycleState
from legba.data.schemas.properties import Property
from legba.runtime import dapr_actors
from legba.runtime.dapr_actors import (
    ACTIVE,
    PAUSED,
    RETIRED,
    ActorRunOutcome,
    AnalystActor,
    _AnalystDeps,
)


# ---------------------------------------------------------------------------
# Fakes (mirrors test_analyst_concurrent_workers.py)
# ---------------------------------------------------------------------------


class _FakeStateManager:
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
    def __init__(self) -> None:
        self.pg_pool = _FakePool()
        self.nats_publish = None


def _make_actor(actor_id: str) -> AnalystActor:
    actor = object.__new__(AnalystActor)
    actor.id = _FakeActorId(actor_id)
    actor._state_manager = _FakeStateManager()
    return actor


def _build_descriptor(version: str = "0" * 64) -> AnalystDescriptor:
    return AnalystDescriptor(
        identity=AnalystIdentity(
            id="country_optimizer",
            name="country_optimizer (lifecycle-test)",
            schema_uri="legba/analyst/1.0.0",
            version=version,
            kind=AnalystKind.INLINE_TARGET,
            type_signature=TypeSignature(
                input_type="legba.runtime.SignalList",
                output_type="legba.runtime.Finding",
            ),
            state=LifecycleState.ACTIVE,
            owner="lifecycle_test",
        ),
        # Meta analyst (no subscription.targets) → single global run path,
        # so _make_actor's primary id carries the cadence reminder + record.
        subscription=SubscriptionBlock(targets=None),
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


def _make_deps(descriptor: AnalystDescriptor, *, read_slice=None) -> _AnalystDeps:
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


async def _unused_run_method(*args, **kwargs):  # pragma: no cover - guard
    raise AssertionError("run_method should not be reached in these tests")


async def _empty_read_slice(conn, *, descriptor, target_filter):
    """No inputs → run() passes the lifecycle gate then NOOPs on empty slice,
    isolating the lifecycle behaviour from the heavy dispatch."""
    return []


@pytest.fixture(autouse=True)
def _reset_deps_registry():
    dapr_actors.clear_deps_registry()
    yield
    dapr_actors.clear_deps_registry()


# A primary (meta) id whose tail is the descriptor content hash[:16].
_VERSION = "deadbeefdeadbeef" + "0" * 48
_PRIMARY_ID = "analyst::country_optimizer::" + _VERSION[:16]


def _register(deps: _AnalystDeps) -> None:
    async def resolver(actor_id: str):
        return deps

    dapr_actors.register_analyst_deps_resolver(resolver)


# ---------------------------------------------------------------------------
# (c) idempotent retire — retiring an already-retired actor no-ops
# ---------------------------------------------------------------------------


async def test_retire_on_retired_is_noop() -> None:
    deps = _make_deps(_build_descriptor(version=_VERSION))
    _register(deps)
    actor = _make_actor(_PRIMARY_ID)
    # Seed a RETIRED record directly.
    await actor._set_record(
        {
            "actor_id": _PRIMARY_ID,
            "actor_kind": "analyst",
            "descriptor_id": "country_optimizer",
            "descriptor_version": _VERSION,
            "lifecycle": RETIRED,
        }
    )
    # A second retire (the version-drift sweep re-issuing RETIRE_ACTOR) must
    # NOT raise — pre-fix this was IllegalTransition("('retired','retire')") → 500.
    rec = await actor.retire()
    assert rec["lifecycle"] == RETIRED


async def test_pause_on_paused_is_noop() -> None:
    deps = _make_deps(_build_descriptor(version=_VERSION))
    _register(deps)
    actor = _make_actor(_PRIMARY_ID)
    await actor._set_record(
        {
            "actor_id": _PRIMARY_ID,
            "actor_kind": "analyst",
            "descriptor_id": "country_optimizer",
            "descriptor_version": _VERSION,
            "lifecycle": PAUSED,
        }
    )
    rec = await actor.pause()
    assert rec["lifecycle"] == PAUSED


# ---------------------------------------------------------------------------
# (b) resume-after-pause — lifecycle=active + reminder re-registered + run runs
# ---------------------------------------------------------------------------


async def test_pause_then_resume_re_registers_reminder_and_runs() -> None:
    deps = _make_deps(_build_descriptor(version=_VERSION), read_slice=_empty_read_slice)
    _register(deps)
    actor = _make_actor(_PRIMARY_ID)

    reminders: list[str] = []
    unregistered: list[str] = []

    async def _capture_register(*, name, state, due_time, period, ttl=None):
        reminders.append(name)

    async def _capture_unregister(name):
        unregistered.append(name)

    actor.register_reminder = _capture_register  # type: ignore[assignment]
    actor.unregister_reminder = _capture_unregister  # type: ignore[assignment]

    # Cold activate → ACTIVE record + cadence reminder.
    await actor._on_activate()
    assert (await actor._get_record())["lifecycle"] == ACTIVE
    assert reminders == ["run_cadence"]

    # Pause → PAUSED + reminder unregistered.
    await actor.pause()
    assert (await actor._get_record())["lifecycle"] == PAUSED
    assert unregistered == ["run_cadence"]

    # A run while paused is a NOOP with reason=lifecycle=paused (the gate).
    paused_out = await actor.run({})
    assert paused_out["outcome"] == ActorRunOutcome.NOOP.value
    assert paused_out["reason"] == f"lifecycle={PAUSED}"

    # Resume → ACTIVE + cadence reminder re-registered.
    reminders.clear()
    rec = await actor.resume()
    assert rec["lifecycle"] == ACTIVE
    assert reminders == ["run_cadence"]

    # And a run now PASSES the lifecycle gate (empty slice → NOOP, but NOT the
    # lifecycle=paused no-op): the analyst executes again.
    resumed_out = await actor.run({})
    assert resumed_out["reason"] != f"lifecycle={PAUSED}"


# ---------------------------------------------------------------------------
# (d) resurrect-on-restore — activate() un-retires / un-pauses a parked record
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("parked", [PAUSED, RETIRED])
async def test_activate_resurrects_parked_record(parked: str) -> None:
    """A rollback that restores a previously-RETIRED version as the active head
    drives reconcile → proxy.activate() on the now-retired actor. activate()
    must bring it back to ACTIVE (the descriptor head is authoritative)."""
    deps = _make_deps(_build_descriptor(version=_VERSION), read_slice=_empty_read_slice)
    _register(deps)
    actor = _make_actor(_PRIMARY_ID)

    async def _noop_register(*, name, state, due_time, period, ttl=None):
        return None

    actor.register_reminder = _noop_register  # type: ignore[assignment]

    # Seed a parked record (retired sibling restored as head / paused then
    # descriptor flipped active).
    await actor._set_record(
        {
            "actor_id": _PRIMARY_ID,
            "actor_kind": "analyst",
            "descriptor_id": "country_optimizer",
            "descriptor_version": _VERSION,
            "lifecycle": parked,
        }
    )

    rec = await actor.activate()
    assert rec["lifecycle"] == ACTIVE

    # Run path now executes (not the lifecycle no-op).
    out = await actor.run({})
    assert out["reason"] != f"lifecycle={parked}"
