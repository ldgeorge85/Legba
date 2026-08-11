# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``subscription.targets.id_list`` = ONE union run, on every trigger path.

W1-E declared ``id_list`` on :class:`SubscriptionTargets` and shipped
``descriptors/analyst_cross_target_raw_g20.yaml`` as a draft binding for the
``cross_target_raw`` kind, but the runtime didn't understand the field: the
kind's ``READ_SLICE`` resolved the union off the descriptor while
``AnalystActor._cadence_targets()`` only understood ``predicate`` and fell
through to "selector, no predicate -> ALL active targets". Declaring the
descriptor's own ``targets`` block therefore fanned out ONE RUN PER ACTIVE
TARGET, each of which re-resolved the identical union inside READ_SLICE — N
redundant, byte-identical cross-target runs instead of the single union run
the kind exists to do. The signal-coalescing path
(``source_first_runtime._analyst_ids_for_target``) had the same hole.

These tests drive the REAL resolution code — the shipped descriptor YAML
parsed into a real :class:`AnalystDescriptor`, the actor's own
``_cadence_targets`` / ``receive_reminder``, the kind's own ``READ_SLICE``,
and the real trigger-wiring matcher. Nothing about the union is hand-built.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest
import yaml

from legba.data.analysts import cross_target_raw
from legba.data.schemas.analyst import AnalystDescriptor
from legba.runtime import dapr_actors, source_first_runtime
from legba.runtime.dapr_actors import AnalystActor, _AnalystDeps


pytestmark = [pytest.mark.integration]


_DESCRIPTOR_PATH = (
    Path(__file__).resolve().parents[2]
    / "descriptors"
    / "analyst_cross_target_raw_g20.yaml"
)


def _union_descriptor() -> AnalystDescriptor:
    """The SHIPPED draft descriptor, parsed by the real schema.

    Not a fixture hand-rolled to match the code: if the descriptor's
    ``subscription.targets`` block drifts, these tests move with it.
    """
    body = yaml.safe_load(_DESCRIPTOR_PATH.read_text())
    return AnalystDescriptor.model_validate(body, strict=False)


# ---------------------------------------------------------------------------
# Actor plumbing — same shape as tests/runtime/test_analyst_concurrent_workers
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


class _RefusingPool:
    """A pg pool that FAILS the test if the union path queries the DB.

    A union subscription needs no ``target_descriptors`` scan — the target set
    is declared. Acquiring here means the carve-out fell through to the
    predicate/all-targets branch.
    """

    def acquire(self):  # pragma: no cover — invoked only on regression
        raise AssertionError(
            "union cadence resolution must not scan target_descriptors"
        )


class _FakeStandardDeps:
    def __init__(self) -> None:
        self.pg_pool = _RefusingPool()
        self.nats_publish = None


def _make_actor(actor_id: str) -> AnalystActor:
    actor = object.__new__(AnalystActor)
    actor.id = _FakeActorId(actor_id)
    actor._state_manager = _FakeStateManager()
    return actor


async def _unused_run_method(*_a: Any, **_kw: Any):  # pragma: no cover — guard
    raise AssertionError("run_method must not be reached by these tests")


def _register(descriptor: AnalystDescriptor, actor_id: str) -> None:
    # ``_AnalystDeps.deps`` is strictly typed as the StandardDeps dataclass and
    # ``StandardDeps.pg_pool`` is an asyncpg.Pool we can't cheaply fake through
    # validation — same ``model_construct`` stub the A2 worker tests use. The
    # DESCRIPTOR is the real thing; only the I/O deps are stubbed.
    deps = _AnalystDeps.model_construct(
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
        read_slice=None,
    )

    async def resolver(_actor_id: str):
        return deps

    dapr_actors.register_analyst_deps_resolver(resolver)
    # `_resolve_analyst_deps` CACHES by actor_id in a process-global dict, so
    # each test binds its own descriptor to its own actor id (a shared id would
    # silently replay the first test's descriptor).
    dapr_actors._ANALYST_DEPS.pop(actor_id, None)


# ---------------------------------------------------------------------------
# 1. Cadence resolution
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_union_subscription_resolves_to_a_single_global_run() -> None:
    """The shipped descriptor's ``id_list`` block resolves to ONE run.

    ``_cadence_targets() is None`` is the actor's single-global-run signal —
    the same one a meta analyst gets — and it is what lets READ_SLICE own the
    union. The previous behaviour returned every active target id.
    """
    descriptor = _union_descriptor()
    assert descriptor.subscription.targets.id_list, "descriptor lost its id_list"
    assert descriptor.subscription.targets.predicate is None

    actor_id = "analyst::cross_target_raw_g20::" + "0" * 16
    _register(descriptor, actor_id)
    actor = _make_actor(actor_id)

    assert await actor._cadence_targets() is None


@pytest.mark.asyncio
async def test_union_cadence_tick_runs_once_and_never_fans_out() -> None:
    """The cadence reminder fires ONE global run, no per-target workers."""
    actor_id = "analyst::cross_target_raw_g20_tick::" + "0" * 16
    _register(_union_descriptor(), actor_id)
    actor = _make_actor(actor_id)

    runs: list[dict[str, Any]] = []

    async def _capture_run(payload=None):
        runs.append(payload or {})
        return {"outcome": "noop"}

    async def _no_fanout(_targets):  # pragma: no cover — guard
        raise AssertionError("a union subscription must not fan out per target")

    actor.run = _capture_run  # type: ignore[assignment]
    actor._fanout_to_workers = _no_fanout  # type: ignore[assignment]

    await actor.receive_reminder("run_cadence", b"{}", None, None)

    assert len(runs) == 1
    assert runs[0].get("trigger_kind") == "cadence"
    # GLOBAL: no target_filter, so READ_SLICE falls to the descriptor's id_list.
    assert "target_filter" not in runs[0]


@pytest.mark.asyncio
async def test_id_list_beats_predicate_and_says_so(caplog) -> None:
    """Declaring both is a misconfiguration; id_list wins, loudly.

    READ_SLICE prefers ``id_list`` over ``target_filter`` unconditionally, so
    honouring the predicate here would re-create the N-identical-runs bug with
    the actor and the kind disagreeing about what the descriptor means.
    """
    body = yaml.safe_load(_DESCRIPTOR_PATH.read_text())
    body["subscription"]["targets"]["predicate"] = 'has_tag("g20")'
    descriptor = AnalystDescriptor.model_validate(body, strict=False)

    actor_id = "analyst::cross_target_raw_g20_pred::" + "0" * 16
    _register(descriptor, actor_id)
    actor = _make_actor(actor_id)

    caplog.set_level(logging.WARNING, logger="legba.runtime.dapr_actors")
    assert await actor._cadence_targets() is None

    assert any(
        "union_predicate_ignored" in r.getMessage() for r in caplog.records
    ), "an ignored predicate must be logged, never silently dropped"


# ---------------------------------------------------------------------------
# 2. The kind's own READ_SLICE resolves the SAME set the cadence path implies
# ---------------------------------------------------------------------------


class _RecordingConn:
    """Records the target_descriptors lookups READ_SLICE performs."""

    def __init__(self) -> None:
        self.looked_up: list[str] = []

    async def fetchrow(self, _query: str, *params: Any):
        self.looked_up.append(str(params[0]))
        return None

    async def fetch(self, _query: str, *_params: Any):  # pragma: no cover
        return []


@pytest.mark.asyncio
async def test_read_slice_resolves_the_declared_union_on_a_global_run() -> None:
    """With NO target_filter (the shape a union cadence tick produces), the
    kind's READ_SLICE resolves exactly the descriptor's declared id_list.

    This is the other half of the contract: the actor stops fanning out
    because READ_SLICE is the thing that owns the union — so it had better
    resolve the whole set from the same descriptor block.
    """
    descriptor = _union_descriptor()
    conn = _RecordingConn()

    rows = await cross_target_raw.READ_SLICE(
        conn, descriptor=descriptor, target_filter=None,
    )

    assert rows == []  # no target resolved a source/geo scope in this fake conn
    assert conn.looked_up == list(descriptor.subscription.targets.id_list)
    assert len(conn.looked_up) == 19, "the G20 roster is the declared scope"


# ---------------------------------------------------------------------------
# 3. The signal-coalescing path must not fan out either
# ---------------------------------------------------------------------------


def test_union_analyst_registers_no_per_target_coalescing_trigger() -> None:
    """``_analyst_ids_for_target`` skips a union subscription.

    Before: ``id_list`` with no predicate hit the "selector, no predicate ->
    all targets" branch, so EVERY active target registered its own
    per-(analyst, target) trigger with its own cooldown — N more redundant
    union runs, this time on the event path.
    """
    descriptor = _union_descriptor()
    body = descriptor.model_dump(mode="json")
    analysts_by_id = {descriptor.identity.id: {"body": body}}

    class _Ident:
        id = "country_geopolitical_us"
        kind = "country"
        abstraction_level = "L2"

    class _Scope:
        geo = ["US"]
        entity_classes: list[str] = []
        tags = ["g20"]

    class _Target:
        identity = _Ident()
        scope = _Scope()
        analyst = None

    matched = source_first_runtime._analyst_ids_for_target(
        _Target(), analysts_by_id,
    )

    assert descriptor.identity.id not in matched, (
        "a union analyst is cadence-driven; per-target coalescing triggers "
        "would fire one redundant union run per member target"
    )
