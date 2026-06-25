# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the reconcile loop (per legba_runtime_spec.md §3)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from legba.runtime.lifecycle import ACTIVE, CONFIGURED, PAUSED, RETIRED
from legba.runtime.reconcile import (
    ActionKind,
    AnalystReconciler,
    DesiredState,
    ObservedState,
    TargetReconciler,
)
from legba.runtime.state import ActorStateRecord


def _now():
    return datetime.now(tz=timezone.utc)


def _observed_with_state(state: str, *, version: str = "deadbeef00000000") -> ObservedState:
    rec = ActorStateRecord(
        actor_id="target::india_energy::deadbeef",
        actor_kind="target",
        descriptor_id="india_energy_infra",
        descriptor_version=version,
        lifecycle=state,
    )
    return ObservedState(actor_id=rec.actor_id, state_record=rec)


def _desired(version: str = "deadbeef00000000", lifecycle: str = ACTIVE) -> DesiredState:
    return DesiredState(
        descriptor_id="india_energy_infra",
        descriptor_kind="target",
        descriptor_version=version,
        lifecycle_target=lifecycle,
    )


@pytest.mark.asyncio
async def test_no_actor_yet_creates() -> None:
    rec = TargetReconciler()
    observed = ObservedState(actor_id="target::x::00000000", state_record=None)
    action = await rec.reconcile(observed, _desired())
    assert action.kind == ActionKind.CREATE_ACTOR
    assert action.detail["descriptor_id"] == "india_energy_infra"


@pytest.mark.asyncio
async def test_active_matching_state_is_noop() -> None:
    rec = TargetReconciler()
    observed = _observed_with_state(ACTIVE)
    action = await rec.reconcile(observed, _desired(lifecycle=ACTIVE))
    assert action.kind == ActionKind.NOOP


@pytest.mark.asyncio
async def test_lifecycle_drift_transitions() -> None:
    rec = TargetReconciler()
    observed = _observed_with_state(ACTIVE)
    action = await rec.reconcile(observed, _desired(lifecycle=PAUSED))
    assert action.kind == ActionKind.TRANSITION_LIFECYCLE
    assert action.detail["from"] == ACTIVE
    assert action.detail["to"] == PAUSED
    # A-1: every actionable detail carries the descriptor coordinates so
    # the executor can write observed state after the proxy call.
    assert action.detail["descriptor_id"] == "india_energy_infra"
    assert action.detail["descriptor_kind"] == "target"


@pytest.mark.asyncio
async def test_identity_drift_restarts() -> None:
    rec = TargetReconciler()
    observed = _observed_with_state(ACTIVE, version="oldhash000000000")
    action = await rec.reconcile(observed, _desired(version="newhash000000000"))
    assert action.kind == ActionKind.RESTART_ACTOR
    assert action.detail["old_version"] == "oldhash000000000"
    assert action.detail["new_version"] == "newhash000000000"


@pytest.mark.asyncio
async def test_retired_descriptor_retires_actor() -> None:
    rec = TargetReconciler()
    observed = _observed_with_state(ACTIVE)
    action = await rec.reconcile(observed, _desired(lifecycle=RETIRED))
    assert action.kind == ActionKind.RETIRE_ACTOR


@pytest.mark.asyncio
async def test_retired_descriptor_no_actor_is_noop() -> None:
    rec = TargetReconciler()
    observed = ObservedState(actor_id="target::x::00000000", state_record=None)
    action = await rec.reconcile(observed, _desired(lifecycle=RETIRED))
    assert action.kind == ActionKind.NOOP


@pytest.mark.asyncio
async def test_reconciler_is_idempotent() -> None:
    """Two reconcile calls with the same input return the same action."""
    rec = TargetReconciler()
    observed = _observed_with_state(ACTIVE)
    desired = _desired(lifecycle=PAUSED)
    a1 = await rec.reconcile(observed, desired)
    a2 = await rec.reconcile(observed, desired)
    assert a1.kind == a2.kind
    assert a1.detail == a2.detail
    assert a1.actor_id == a2.actor_id


@pytest.mark.asyncio
async def test_analyst_reconciler_same_shape() -> None:
    rec = AnalystReconciler()
    observed = ObservedState(actor_id="analyst::x::00000000", state_record=None)
    desired = DesiredState(
        descriptor_id="analyst_x",
        descriptor_kind="analyst",
        descriptor_version="ff" * 8,
        lifecycle_target=ACTIVE,
    )
    action = await rec.reconcile(observed, desired)
    assert action.kind == ActionKind.CREATE_ACTOR
    assert action.detail["descriptor_kind"] == "analyst"


@pytest.mark.asyncio
async def test_active_analyst_reasserts_reminder_on_resync() -> None:
    # Durability heal: an in-sync, active ANALYST re-asserts its cadence
    # reminder each resync (ENSURE_ACTIVE) so a silently-dropped Dapr reminder
    # self-heals before the 30m idle-timeout strands it. An in-sync active
    # TARGET (passive subscriber, no reminder) stays NOOP.
    observed = _observed_with_state(ACTIVE)  # version matches _desired default
    desired = DesiredState(
        descriptor_id="india_energy_infra",
        descriptor_kind="analyst",
        descriptor_version="deadbeef00000000",
        lifecycle_target=ACTIVE,
    )
    analyst_action = await AnalystReconciler().reconcile(observed, desired)
    assert analyst_action.kind == ActionKind.ENSURE_ACTIVE
    assert analyst_action.detail["descriptor_id"] == "india_energy_infra"

    target_action = await TargetReconciler().reconcile(
        observed, _desired(lifecycle=ACTIVE)
    )
    assert target_action.kind == ActionKind.NOOP
