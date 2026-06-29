# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the lifecycle FSM (per legba_runtime_spec.md §6)."""

from __future__ import annotations

import pytest

from legba.runtime.lifecycle import (
    ACTIVE,
    CONFIGURED,
    DRAFT,
    ERROR,
    IllegalTransition,
    LifecycleEvent,
    LifecycleFSM,
    PAUSED,
    RETIRED,
)


def test_default_state_is_draft() -> None:
    fsm = LifecycleFSM()
    assert fsm.state == DRAFT


def test_legal_path_draft_to_active() -> None:
    fsm = LifecycleFSM()
    fsm.transition(LifecycleEvent.CONFIGURE)
    assert fsm.state == CONFIGURED
    fsm.transition(LifecycleEvent.ACTIVATE)
    assert fsm.state == ACTIVE
    fsm.transition(LifecycleEvent.PAUSE)
    assert fsm.state == PAUSED
    fsm.transition(LifecycleEvent.RESUME)
    assert fsm.state == ACTIVE
    fsm.transition(LifecycleEvent.RETIRE)
    assert fsm.state == RETIRED


def test_history_records_transitions() -> None:
    fsm = LifecycleFSM()
    fsm.transition(LifecycleEvent.CONFIGURE, initiated_by="setup")
    fsm.transition(LifecycleEvent.ACTIVATE)
    assert len(fsm.history) == 2
    assert fsm.history[0].from_state == DRAFT
    assert fsm.history[0].to_state == CONFIGURED
    assert fsm.history[0].initiated_by == "setup"
    assert fsm.history[1].from_state == CONFIGURED


def test_illegal_transition_raises() -> None:
    fsm = LifecycleFSM()
    # draft has no ACTIVATE event (must go through CONFIGURE first).
    with pytest.raises(IllegalTransition):
        fsm.transition(LifecycleEvent.ACTIVATE)
    # draft has no RETIRE.
    with pytest.raises(IllegalTransition):
        fsm.transition(LifecycleEvent.PAUSE)


def test_retired_is_terminal() -> None:
    fsm = LifecycleFSM(state=RETIRED)
    for ev in (LifecycleEvent.PAUSE, LifecycleEvent.ACTIVATE,
               LifecycleEvent.CONFIGURE, LifecycleEvent.RESUME):
        with pytest.raises(IllegalTransition):
            fsm.transition(ev)


def test_error_reachable_from_active_states() -> None:
    for start in (DRAFT, CONFIGURED, ACTIVE, PAUSED):
        fsm = LifecycleFSM(state=start)
        rec = fsm.transition(LifecycleEvent.ERROR)
        assert fsm.state == ERROR
        assert rec.from_state == start
        assert rec.to_state == ERROR


def test_error_not_reachable_from_retired_or_error() -> None:
    for start in (RETIRED, ERROR):
        fsm = LifecycleFSM(state=start)
        with pytest.raises(IllegalTransition):
            fsm.transition(LifecycleEvent.ERROR)


def test_error_reset_goes_to_configured() -> None:
    fsm = LifecycleFSM(state=ACTIVE)
    fsm.transition(LifecycleEvent.ERROR)
    assert fsm.state == ERROR
    fsm.transition(LifecycleEvent.RESET)
    assert fsm.state == CONFIGURED


def test_can_returns_bool_without_mutating() -> None:
    fsm = LifecycleFSM()
    assert fsm.can(LifecycleEvent.CONFIGURE) is True
    assert fsm.can(LifecycleEvent.ACTIVATE) is False
    # State unchanged.
    assert fsm.state == DRAFT


def test_legal_events_from_active() -> None:
    fsm = LifecycleFSM(state=ACTIVE)
    legal = set(fsm.legal_events())
    assert LifecycleEvent.PAUSE in legal
    assert LifecycleEvent.RETIRE in legal
    assert LifecycleEvent.ERROR in legal
    assert LifecycleEvent.RESUME not in legal


# ---------------------------------------------------------------------------
# Same-state idempotency (the runtime lifecycle-bug fix). transition_idempotent
# no-ops when already at the event's target, but a genuinely-illegal move (no
# edge AND not same-state) still raises — the FSM's real semantics are intact.
# ---------------------------------------------------------------------------


def test_transition_idempotent_noops_at_target_state() -> None:
    # retire-on-retired, pause-on-paused, activate/resume-on-active all no-op.
    cases = [
        (RETIRED, LifecycleEvent.RETIRE),
        (PAUSED, LifecycleEvent.PAUSE),
        (ACTIVE, LifecycleEvent.ACTIVATE),
        (ACTIVE, LifecycleEvent.RESUME),
        (CONFIGURED, LifecycleEvent.CONFIGURE),
    ]
    for start, ev in cases:
        fsm = LifecycleFSM(state=start)
        rec = fsm.transition_idempotent(ev)
        assert rec is None, f"{start}/{ev} should no-op"
        assert fsm.state == start
        assert fsm.history == []


def test_transition_idempotent_applies_real_transition() -> None:
    fsm = LifecycleFSM(state=ACTIVE)
    rec = fsm.transition_idempotent(LifecycleEvent.PAUSE)
    assert rec is not None
    assert fsm.state == PAUSED
    assert rec.from_state == ACTIVE and rec.to_state == PAUSED
    # And resume back.
    rec2 = fsm.transition_idempotent(LifecycleEvent.RESUME)
    assert rec2 is not None
    assert fsm.state == ACTIVE


def test_transition_idempotent_still_rejects_illegal() -> None:
    # draft -> RETIRE has no edge AND draft != retired, so it must still raise
    # (idempotency relaxes ONLY same-state, never a genuinely-illegal move).
    fsm = LifecycleFSM(state=DRAFT)
    with pytest.raises(IllegalTransition):
        fsm.transition_idempotent(LifecycleEvent.RETIRE)
    # error is not reachable from retired even via the idempotent helper.
    fsm_retired = LifecycleFSM(state=RETIRED)
    with pytest.raises(IllegalTransition):
        fsm_retired.transition_idempotent(LifecycleEvent.ERROR)


def test_is_noop_does_not_mutate() -> None:
    fsm = LifecycleFSM(state=PAUSED)
    assert fsm.is_noop(LifecycleEvent.PAUSE) is True
    assert fsm.is_noop(LifecycleEvent.RESUME) is False
    assert fsm.state == PAUSED
