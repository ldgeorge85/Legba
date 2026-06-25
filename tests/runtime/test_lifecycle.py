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
