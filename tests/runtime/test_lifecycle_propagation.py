# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A-1 / review-G1 — lifecycle propagation through the observed-state plane.

Regression suite for the four verified control-plane failures (review
2026-06 §3.2 G1):

  * nothing ever wrote observed state → reconcile was blind, every resync
    re-CREATEd everything, and RETIRE/TRANSITION were unreachable;
  * CREATE_ACTOR ignored ``target_lifecycle`` → pausing a descriptor
    RE-ACTIVATED it;
  * version bumps minted a new actor_id and left the old actor running;
  * stale reminders had no self-disarm.

These tests drive :func:`legba.runtime.dapr_host.execute_reconcile_action`
and :class:`legba.runtime.reconcile.ReconcileLoop` with recording fakes —
the proxy layer is exactly what daprd would receive. The live-stack
verification (real registry + daprd) is the wave's adversarial pass, not
this file.
"""

from __future__ import annotations

from typing import Any

import pytest
import pytest_asyncio

from legba.runtime.dapr_host import execute_reconcile_action
from legba.runtime.lifecycle import ACTIVE, PAUSED, RETIRED
from legba.runtime.reconcile import (
    ActionKind,
    AnalystReconciler,
    DesiredState,
    ReconcileAction,
    ReconcileLoop,
    TargetReconciler,
    _default_actor_id,
)
from legba.runtime.dapr_actors import reminder_guard_decision
from legba.runtime.source_first_runtime import (
    _ANALYST_ACTOR_IDS,
    forget_analyst_actor_id,
    remember_analyst_actor_id,
)
from legba.runtime.state import ActorStateRecord, ActorStateStore


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeProxy:
    def __init__(self, actor_id: str, calls: list[tuple[str, str]]) -> None:
        self._actor_id = actor_id
        self._calls = calls

    async def activate(self) -> dict:
        self._calls.append((self._actor_id, "activate"))
        return {"lifecycle": ACTIVE}

    async def pause(self) -> dict:
        self._calls.append((self._actor_id, "pause"))
        return {"lifecycle": PAUSED}

    async def resume(self) -> dict:
        self._calls.append((self._actor_id, "resume"))
        return {"lifecycle": ACTIVE}

    async def retire(self) -> dict:
        self._calls.append((self._actor_id, "retire"))
        return {"lifecycle": RETIRED}


class FakeStateStore:
    """In-memory stand-in for ActorStateStore (get/upsert/list_live_siblings)."""

    def __init__(self) -> None:
        self.rows: dict[str, ActorStateRecord] = {}

    async def get(self, actor_id: str) -> ActorStateRecord | None:
        return self.rows.get(actor_id)

    async def upsert(self, rec: ActorStateRecord) -> None:
        self.rows[rec.actor_id] = rec

    async def list_live_siblings(
        self, *, actor_kind: str, descriptor_id: str, exclude_actor_id: str,
    ) -> list[ActorStateRecord]:
        return [
            r for r in self.rows.values()
            if r.actor_kind == actor_kind
            and r.descriptor_id == descriptor_id
            and r.actor_id != exclude_actor_id
            and r.lifecycle != RETIRED
        ]


class Harness:
    """execute_reconcile_action with every hook recording."""

    def __init__(self) -> None:
        self.store = FakeStateStore()
        self.proxy_calls: list[tuple[str, str]] = []
        self.remembered: list[tuple[str, str]] = []
        self.forgotten: list[tuple[str, str]] = []
        self.a2a_registered: list[tuple[str, str]] = []
        self.a2a_unregistered: list[str] = []
        self.triggers_unregistered: list[str] = []

    def proxy_for(self, actor_kind: str, actor_id: str) -> FakeProxy:
        if actor_kind not in ("target", "analyst", "source"):
            raise ValueError(f"unknown actor kind {actor_kind!r}")
        return FakeProxy(actor_id, self.proxy_calls)

    async def execute(self, action: ReconcileAction) -> None:
        async def _register_a2a(descriptor_id: str, version: str) -> None:
            self.a2a_registered.append((descriptor_id, version))

        def _unregister_a2a(descriptor_id: str) -> int:
            self.a2a_unregistered.append(descriptor_id)
            return 1

        def _unregister_triggers(descriptor_id: str) -> int:
            self.triggers_unregistered.append(descriptor_id)
            return 1

        await execute_reconcile_action(
            action,
            proxy_for=self.proxy_for,
            state_store=self.store,
            remember_analyst=lambda d, a: self.remembered.append((d, a)),
            forget_analyst=lambda d, a: self.forgotten.append((d, a)),
            register_a2a_skills=_register_a2a,
            unregister_a2a_skills=_unregister_a2a,
            unregister_triggers=_unregister_triggers,
        )


def _create_action(
    actor_id: str,
    *,
    kind: str = "analyst",
    descriptor_id: str = "an_x",
    version: str = "a" * 64,
    target_lifecycle: str = ACTIVE,
) -> ReconcileAction:
    return ReconcileAction(
        kind=ActionKind.CREATE_ACTOR,
        actor_id=actor_id,
        detail={
            "descriptor_id": descriptor_id,
            "descriptor_kind": kind,
            "descriptor_version": version,
            "target_lifecycle": target_lifecycle,
        },
    )


# ---------------------------------------------------------------------------
# Executor — observed state + commanded lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_active_activates_and_writes_observed_state() -> None:
    h = Harness()
    aid = _default_actor_id("analyst", "an_x", "a" * 64)
    await h.execute(_create_action(aid))
    assert h.proxy_calls == [(aid, "activate")]
    rec = h.store.rows[aid]
    assert rec.lifecycle == ACTIVE
    assert rec.actor_kind == "analyst"
    assert rec.descriptor_id == "an_x"
    assert rec.descriptor_version == "a" * 64
    assert h.remembered == [("an_x", aid)]
    assert h.a2a_registered == [("an_x", "a" * 64)]


@pytest.mark.asyncio
async def test_create_paused_pauses_and_never_activates() -> None:
    # G1's worst symptom: pausing a descriptor whose observed row was
    # missing (i.e. always, pre-fix) routed to CREATE → unconditional
    # activate(). The executor must honor target_lifecycle=paused.
    h = Harness()
    aid = _default_actor_id("source", "src_y", "b" * 64)
    await h.execute(_create_action(
        aid, kind="source", descriptor_id="src_y", version="b" * 64,
        target_lifecycle=PAUSED,
    ))
    assert h.proxy_calls == [(aid, "pause")]
    assert all(m != "activate" for _, m in h.proxy_calls)
    assert h.store.rows[aid].lifecycle == PAUSED


@pytest.mark.asyncio
async def test_create_draft_touches_nothing() -> None:
    h = Harness()
    aid = _default_actor_id("target", "tg_z", "c" * 64)
    await h.execute(_create_action(
        aid, kind="target", descriptor_id="tg_z", version="c" * 64,
        target_lifecycle="draft",
    ))
    assert h.proxy_calls == []
    assert h.store.rows == {}


@pytest.mark.asyncio
async def test_retire_propagates_and_evicts_dispatch_cache() -> None:
    h = Harness()
    aid = _default_actor_id("analyst", "an_x", "a" * 64)
    h.store.rows[aid] = ActorStateRecord(
        actor_id=aid, actor_kind="analyst", descriptor_id="an_x",
        descriptor_version="a" * 64, lifecycle=ACTIVE,
    )
    await h.execute(ReconcileAction(
        kind=ActionKind.RETIRE_ACTOR,
        actor_id=aid,
        detail={
            "descriptor_id": "an_x",
            "descriptor_kind": "analyst",
            "descriptor_version": "a" * 64,
            "from_state": ACTIVE,
        },
    ))
    assert h.proxy_calls == [(aid, "retire")]
    assert h.store.rows[aid].lifecycle == RETIRED
    assert h.forgotten == [("an_x", aid)]
    assert h.a2a_unregistered == ["an_x"]
    # §2.1: retire also tears down the analyst's trigger registrations at the
    # source, so the engine stops marking its pairs dirty.
    assert h.triggers_unregistered == ["an_x"]


@pytest.mark.asyncio
async def test_pause_forgets_dispatch_cache_so_reactive_fires_noop() -> None:
    # §2.1: pausing an analyst must evict it from the dispatch live-set so the
    # trigger gate NOOPs its reactive fires (the cadence reminder is already
    # unregistered by A-1; this closes the per-target-worker spend leak). Pause
    # is reversible, so it does NOT tear down trigger registrations.
    h = Harness()
    aid = _default_actor_id("analyst", "an_x", "a" * 64)
    h.store.rows[aid] = ActorStateRecord(
        actor_id=aid, actor_kind="analyst", descriptor_id="an_x",
        descriptor_version="a" * 64, lifecycle=ACTIVE,
    )
    await h.execute(ReconcileAction(
        kind=ActionKind.TRANSITION_LIFECYCLE,
        actor_id=aid,
        detail={
            "descriptor_id": "an_x",
            "descriptor_kind": "analyst",
            "descriptor_version": "a" * 64,
            "from": ACTIVE,
            "to": PAUSED,
        },
    ))
    assert h.proxy_calls == [(aid, "pause")]
    assert h.store.rows[aid].lifecycle == PAUSED
    assert h.forgotten == [("an_x", aid)]      # evicted from the live-set
    assert h.triggers_unregistered == []        # reversible — regs stay


@pytest.mark.asyncio
async def test_transition_to_paused_pauses_and_records() -> None:
    h = Harness()
    aid = _default_actor_id("analyst", "an_x", "a" * 64)
    h.store.rows[aid] = ActorStateRecord(
        actor_id=aid, actor_kind="analyst", descriptor_id="an_x",
        descriptor_version="a" * 64, lifecycle=ACTIVE,
    )
    await h.execute(ReconcileAction(
        kind=ActionKind.TRANSITION_LIFECYCLE,
        actor_id=aid,
        detail={
            "descriptor_id": "an_x",
            "descriptor_kind": "analyst",
            "descriptor_version": "a" * 64,
            "from": ACTIVE,
            "to": PAUSED,
        },
    ))
    assert h.proxy_calls == [(aid, "pause")]
    assert h.store.rows[aid].lifecycle == PAUSED


@pytest.mark.asyncio
async def test_ensure_active_refreshes_observed_state() -> None:
    h = Harness()
    aid = _default_actor_id("source", "src_y", "b" * 64)
    await h.execute(ReconcileAction(
        kind=ActionKind.ENSURE_ACTIVE,
        actor_id=aid,
        detail={
            "descriptor_id": "src_y",
            "descriptor_kind": "source",
            "descriptor_version": "b" * 64,
        },
    ))
    assert h.proxy_calls == [(aid, "activate")]
    assert h.store.rows[aid].lifecycle == ACTIVE
    assert h.a2a_registered == []  # sources never carry a2a skills


@pytest.mark.asyncio
async def test_ensure_active_reregisters_a2a_for_analyst() -> None:
    # A restart re-asserts active analysts via ENSURE_ACTIVE (not CREATE), so
    # a2a skills must re-register here too — else the in-memory A2ASkillRegistry
    # stays empty after a runtime restart (the registration was CREATE-only).
    # The has_analyst_version guard inside the real hook makes the steady-state
    # resync a cheap no-op; this asserts the hook is wired on ENSURE.
    h = Harness()
    aid = _default_actor_id("analyst", "an_x", "a" * 64)
    await h.execute(ReconcileAction(
        kind=ActionKind.ENSURE_ACTIVE,
        actor_id=aid,
        detail={
            "descriptor_id": "an_x",
            "descriptor_kind": "analyst",
            "descriptor_version": "a" * 64,
        },
    ))
    assert h.proxy_calls == [(aid, "activate")]
    assert h.a2a_registered == [("an_x", "a" * 64)]


@pytest.mark.asyncio
async def test_observed_write_preserves_source_cursors() -> None:
    # The upsert is read-modify-write: fields the executor doesn't own
    # (cursors written by the acquisition plane) must survive a lifecycle
    # transition.
    from legba.runtime.state import SourceCursor

    h = Harness()
    aid = _default_actor_id("source", "src_y", "b" * 64)
    h.store.rows[aid] = ActorStateRecord(
        actor_id=aid, actor_kind="source", descriptor_id="src_y",
        descriptor_version="b" * 64, lifecycle=ACTIVE,
        source_cursors={"src_y": SourceCursor(source_id="src_y", rows_pulled=7)},
    )
    await h.execute(ReconcileAction(
        kind=ActionKind.TRANSITION_LIFECYCLE,
        actor_id=aid,
        detail={
            "descriptor_id": "src_y",
            "descriptor_kind": "source",
            "descriptor_version": "b" * 64,
            "from": ACTIVE,
            "to": PAUSED,
        },
    ))
    rec = h.store.rows[aid]
    assert rec.lifecycle == PAUSED
    assert rec.source_cursors["src_y"].rows_pulled == 7


# ---------------------------------------------------------------------------
# ReconcileLoop.run_once — CREATE→ENSURE convergence + version-drift sweep
# ---------------------------------------------------------------------------


def _loop_for(h: Harness, desired: dict[str, DesiredState]) -> ReconcileLoop:
    async def resolver(descriptor_id: str) -> DesiredState | None:
        return desired.get(descriptor_id)

    async def lister() -> list[DesiredState]:
        return list(desired.values())

    return ReconcileLoop(
        state_store=h.store,  # duck-typed
        desired_resolver=resolver,
        desired_lister=lister,
        action_executor=h.execute,
        reconcilers={
            "target": TargetReconciler(),
            "analyst": AnalystReconciler(),
            "source": TargetReconciler(),
        },
    )


@pytest.mark.asyncio
async def test_resync_converges_create_then_ensure_active() -> None:
    # First reconcile: no observed row → CREATE (and the row gets written).
    # Second reconcile: row in sync → ENSURE_ACTIVE, NOT a re-CREATE. This
    # is the assertion that the observed-state plane is actually alive —
    # pre-fix, every resync produced CREATE_ACTOR forever.
    h = Harness()
    desired = {
        "an_x": DesiredState(
            descriptor_id="an_x", descriptor_kind="analyst",
            descriptor_version="a" * 64, lifecycle_target=ACTIVE,
        ),
    }
    loop = _loop_for(h, desired)
    first = await loop.run_once("an_x")
    assert first.kind == ActionKind.CREATE_ACTOR
    second = await loop.run_once("an_x")
    assert second.kind == ActionKind.ENSURE_ACTIVE


@pytest.mark.asyncio
async def test_resync_source_gets_ensure_active_target_noop() -> None:
    # Sources keep the durability heal on purpose (pre-fix they were healed
    # by the accidental CREATE-per-resync; losing that silently would stall
    # poll reminders). Passive targets converge to NOOP.
    h = Harness()
    desired = {
        "src_y": DesiredState(
            descriptor_id="src_y", descriptor_kind="source",
            descriptor_version="b" * 64, lifecycle_target=ACTIVE,
        ),
        "tg_z": DesiredState(
            descriptor_id="tg_z", descriptor_kind="target",
            descriptor_version="c" * 64, lifecycle_target=ACTIVE,
        ),
    }
    loop = _loop_for(h, desired)
    await loop.run_once("src_y")
    await loop.run_once("tg_z")
    assert (await loop.run_once("src_y")).kind == ActionKind.ENSURE_ACTIVE
    assert (await loop.run_once("tg_z")).kind == ActionKind.NOOP


@pytest.mark.asyncio
async def test_g1_lost_retire_reasserts_on_resync() -> None:
    # G1: retire arrives as ONE NATS event; if it's lost, the head is now
    # `retired` in the registry but the actor's observed row is still `active`.
    # The periodic resync (which now enumerates non-active heads) must re-derive
    # the RETIRE and converge — pre-fix the lister filtered to active so the
    # retired head was never enumerated and the retire stuck until manual touch.
    h = Harness()
    aid = _default_actor_id("analyst", "an_x", "a" * 64)
    h.store.rows[aid] = ActorStateRecord(
        actor_id=aid, actor_kind="analyst", descriptor_id="an_x",
        descriptor_version="a" * 64, lifecycle=ACTIVE,
    )
    desired = {
        "an_x": DesiredState(
            descriptor_id="an_x", descriptor_kind="analyst",
            descriptor_version="a" * 64, lifecycle_target=RETIRED,
        ),
    }
    action = await _loop_for(h, desired).run_once("an_x")
    assert action.kind == ActionKind.RETIRE_ACTOR
    assert h.store.rows[aid].lifecycle == RETIRED
    assert h.proxy_calls == [(aid, "retire")]


@pytest.mark.asyncio
async def test_g1_lost_pause_reasserts_on_resync() -> None:
    # Same single-delivery-window recovery for pause: observed active, head
    # now paused → resync derives the TRANSITION→paused.
    h = Harness()
    aid = _default_actor_id("analyst", "an_x", "a" * 64)
    h.store.rows[aid] = ActorStateRecord(
        actor_id=aid, actor_kind="analyst", descriptor_id="an_x",
        descriptor_version="a" * 64, lifecycle=ACTIVE,
    )
    desired = {
        "an_x": DesiredState(
            descriptor_id="an_x", descriptor_kind="analyst",
            descriptor_version="a" * 64, lifecycle_target=PAUSED,
        ),
    }
    action = await _loop_for(h, desired).run_once("an_x")
    assert action.kind == ActionKind.TRANSITION_LIFECYCLE
    assert h.store.rows[aid].lifecycle == PAUSED
    assert h.proxy_calls == [(aid, "pause")]


@pytest.mark.asyncio
async def test_resume_paused_to_active_routes_to_resume() -> None:
    # The inverse of the pause path: observed PAUSED, head flipped back to
    # ACTIVE → TRANSITION(to=active, from=paused). The executor must route to
    # proxy.resume() (which re-registers the cadence reminder pause tore down)
    # — NOT a bare activate — and re-remember the analyst as live so reactive
    # fires dispatch again.
    h = Harness()
    aid = _default_actor_id("analyst", "an_x", "a" * 64)
    h.store.rows[aid] = ActorStateRecord(
        actor_id=aid, actor_kind="analyst", descriptor_id="an_x",
        descriptor_version="a" * 64, lifecycle=PAUSED,
    )
    desired = {
        "an_x": DesiredState(
            descriptor_id="an_x", descriptor_kind="analyst",
            descriptor_version="a" * 64, lifecycle_target=ACTIVE,
        ),
    }
    action = await _loop_for(h, desired).run_once("an_x")
    assert action.kind == ActionKind.TRANSITION_LIFECYCLE
    assert action.detail["from"] == PAUSED
    assert action.detail["to"] == ACTIVE
    assert h.proxy_calls == [(aid, "resume")]
    assert h.store.rows[aid].lifecycle == ACTIVE
    assert h.remembered == [("an_x", aid)]      # back in the live-set


def test_g1_reconcilable_states_include_non_active_exclude_pre_activation() -> None:
    # Locks the resync lister's filter (dapr_host.desired_lister): paused +
    # retired heads ARE re-asserted; draft/configured (no live-actor action)
    # are NOT.
    from legba.runtime.dapr_host import RECONCILABLE_LIFECYCLE_STATES

    assert {"active", "paused", "retired"} <= RECONCILABLE_LIFECYCLE_STATES
    assert "draft" not in RECONCILABLE_LIFECYCLE_STATES
    assert "configured" not in RECONCILABLE_LIFECYCLE_STATES


@pytest.mark.asyncio
async def test_version_bump_retires_old_actor_id() -> None:
    # Descriptor edit: head version v1 → v2 mints a new actor_id. run_once
    # must retire the live v1 actor (proxy.retire + observed row retired)
    # before creating v2 — pre-fix the old actor double-ran forever.
    h = Harness()
    v1, v2 = "1" * 64, "2" * 64
    old_id = _default_actor_id("analyst", "an_x", v1)
    new_id = _default_actor_id("analyst", "an_x", v2)
    h.store.rows[old_id] = ActorStateRecord(
        actor_id=old_id, actor_kind="analyst", descriptor_id="an_x",
        descriptor_version=v1, lifecycle=ACTIVE,
    )
    desired = {
        "an_x": DesiredState(
            descriptor_id="an_x", descriptor_kind="analyst",
            descriptor_version=v2, lifecycle_target=ACTIVE,
        ),
    }
    loop = _loop_for(h, desired)
    action = await loop.run_once("an_x")
    assert action.kind == ActionKind.CREATE_ACTOR
    assert (old_id, "retire") in h.proxy_calls
    assert (new_id, "activate") in h.proxy_calls
    assert h.proxy_calls.index((old_id, "retire")) < h.proxy_calls.index(
        (new_id, "activate")
    )
    assert h.store.rows[old_id].lifecycle == RETIRED
    assert h.store.rows[new_id].lifecycle == ACTIVE


@pytest.mark.asyncio
async def test_retired_descriptor_retires_live_actor() -> None:
    h = Harness()
    aid = _default_actor_id("source", "src_y", "b" * 64)
    h.store.rows[aid] = ActorStateRecord(
        actor_id=aid, actor_kind="source", descriptor_id="src_y",
        descriptor_version="b" * 64, lifecycle=ACTIVE,
    )
    desired = {
        "src_y": DesiredState(
            descriptor_id="src_y", descriptor_kind="source",
            descriptor_version="b" * 64, lifecycle_target=RETIRED,
        ),
    }
    loop = _loop_for(h, desired)
    action = await loop.run_once("src_y")
    assert action.kind == ActionKind.RETIRE_ACTOR
    assert h.proxy_calls == [(aid, "retire")]
    assert h.store.rows[aid].lifecycle == RETIRED


@pytest.mark.asyncio
async def test_pause_transition_does_not_activate() -> None:
    # Full-loop version of the headline G1 bug: active actor + descriptor
    # paused → TRANSITION(pause). The actor must see pause() and ONLY pause().
    h = Harness()
    aid = _default_actor_id("analyst", "an_x", "a" * 64)
    h.store.rows[aid] = ActorStateRecord(
        actor_id=aid, actor_kind="analyst", descriptor_id="an_x",
        descriptor_version="a" * 64, lifecycle=ACTIVE,
    )
    desired = {
        "an_x": DesiredState(
            descriptor_id="an_x", descriptor_kind="analyst",
            descriptor_version="a" * 64, lifecycle_target=PAUSED,
        ),
    }
    loop = _loop_for(h, desired)
    action = await loop.run_once("an_x")
    assert action.kind == ActionKind.TRANSITION_LIFECYCLE
    assert h.proxy_calls == [(aid, "pause")]
    assert h.store.rows[aid].lifecycle == PAUSED
    # And the paused pair is stable: reconcile again → NOOP, still no activate.
    again = await loop.run_once("an_x")
    assert again.kind == ActionKind.NOOP
    assert h.proxy_calls == [(aid, "pause")]


# ---------------------------------------------------------------------------
# Detail enrichment + dispatch-cache hygiene + reminder guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_actions_carry_descriptor_coordinates() -> None:
    h = Harness()
    desired = {
        "an_x": DesiredState(
            descriptor_id="an_x", descriptor_kind="analyst",
            descriptor_version="a" * 64, lifecycle_target=ACTIVE,
        ),
    }
    loop = _loop_for(h, desired)
    action = await loop.run_once("an_x")
    for key in ("descriptor_id", "descriptor_kind", "descriptor_version"):
        assert action.detail.get(key), f"missing {key} in {action.detail}"


def test_forget_analyst_actor_id_is_guarded() -> None:
    _ANALYST_ACTOR_IDS.clear()
    try:
        remember_analyst_actor_id("an_x", "analyst::an_x::old0000000000000")
        # Retire arriving AFTER the new version was remembered must not
        # clobber the live mapping.
        remember_analyst_actor_id("an_x", "analyst::an_x::new0000000000000")
        forget_analyst_actor_id("an_x", "analyst::an_x::old0000000000000")
        assert _ANALYST_ACTOR_IDS["an_x"] == "analyst::an_x::new0000000000000"
        forget_analyst_actor_id("an_x", "analyst::an_x::new0000000000000")
        assert "an_x" not in _ANALYST_ACTOR_IDS
    finally:
        _ANALYST_ACTOR_IDS.clear()


@pytest.mark.parametrize(
    ("record_lifecycle", "own_tail", "head_version", "expect"),
    [
        # Stale version → self-disarm, regardless of lifecycle.
        (ACTIVE, "1" * 16, "2" * 64, "unregister"),
        # Retired actor → self-disarm even when version still matches.
        (RETIRED, "2" * 16, "2" * 64, "unregister"),
        # Paused → skip the run, keep the reminder (pause owns its fate).
        (PAUSED, "2" * 16, "2" * 64, "skip"),
        # Healthy primary → run.
        (ACTIVE, "2" * 16, "2" * 64, "run"),
        # Conservative on missing evidence: no head (registry down) and no
        # record → never kill the reminder.
        (None, "2" * 16, None, "run"),
        (ACTIVE, "2" * 16, None, "run"),
        # No tail (malformed/two-segment id) → fall through to lifecycle.
        (RETIRED, None, "2" * 64, "unregister"),
        (ACTIVE, None, "2" * 64, "run"),
    ],
)
def test_reminder_guard_decision(
    record_lifecycle: str | None,
    own_tail: str | None,
    head_version: str | None,
    expect: str,
) -> None:
    assert reminder_guard_decision(
        record_lifecycle=record_lifecycle,
        own_tail=own_tail,
        head_version=head_version,
    ) == expect


# ---------------------------------------------------------------------------
# ActorStateStore.list_live_siblings — real Postgres
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def pg_pool(migrated_pg):
    import asyncpg

    pool = await asyncpg.create_pool(
        host=migrated_pg.host,
        port=migrated_pg.port,
        user=migrated_pg.user,
        password=migrated_pg.password,
        database=migrated_pg.database,
        min_size=1,
        max_size=4,
    )
    yield pool
    await pool.close()


@pytest.mark.asyncio
async def test_list_live_siblings_pg(pg_pool: Any) -> None:
    store = ActorStateStore(pg_pool)
    await store.ensure_schema()
    v1, v2 = "1" * 64, "2" * 64
    did = "an_sibling_test"
    old_id = _default_actor_id("analyst", did, v1)
    new_id = _default_actor_id("analyst", did, v2)
    retired_id = _default_actor_id("analyst", did, "3" * 64)
    other_kind = _default_actor_id("source", did, v1)
    for actor_id, kind, version, lifecycle in [
        (old_id, "analyst", v1, ACTIVE),
        (retired_id, "analyst", "3" * 64, RETIRED),
        (other_kind, "source", v1, ACTIVE),
    ]:
        await store.upsert(ActorStateRecord(
            actor_id=actor_id, actor_kind=kind, descriptor_id=did,
            descriptor_version=version, lifecycle=lifecycle,
        ))
    try:
        sibs = await store.list_live_siblings(
            actor_kind="analyst", descriptor_id=did, exclude_actor_id=new_id,
        )
        assert [s.actor_id for s in sibs] == [old_id]
    finally:
        async with pg_pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM public.actor_state WHERE descriptor_id = $1", did,
            )
