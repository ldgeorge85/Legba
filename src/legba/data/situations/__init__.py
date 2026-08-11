# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""legba.data.situations — the situation TRAJECTORY runtime (continuity P2).

One package, one job: how a situation EVOLVES, as data. The `situations` table
itself (a 20-minute re-materialization owned by ``situation_clustering``) says
what a frame looks like NOW; this package owns the append-only ledger that says
how it got there.

Deliberately a package with a leaf module rather than code inlined in the
``situation_tracker`` handler — plan D6. The Phase 3 exemplar warning-problem
monitors are "a situation whose attachment rule is a pattern-thread match": same
state machine, same ledger, same dormancy semantics, a different way of deciding
what counts as attached evidence. One runtime, two entry points. Anything a
second entry point would need lives HERE; only the attachment query lives in the
handler.
"""

from __future__ import annotations

from .trajectory import (
    DELTA_BROADENS,
    DELTA_DE_ESCALATES,
    DELTA_ESCALATES,
    DELTA_KINDS,
    DELTA_UNCHANGED_CHECKPOINT,
    DORMANCY_DAYS,
    INITIAL_STATE,
    LEDGER_FAITHFULNESS_FLOOR,
    STATE_CLOSED,
    STATE_DE_ESCALATING,
    STATE_DORMANT,
    STATE_ESCALATING,
    STATE_WATCHING,
    TRAJECTORY_STATES,
    TrajectoryEvent,
    TrajectoryTransitionError,
    delta_requires_evidence,
    next_state,
    read_current_states,
    read_trajectories,
    read_trajectory,
    record_situation_events,
)

__all__ = [
    "DELTA_BROADENS",
    "DELTA_DE_ESCALATES",
    "DELTA_ESCALATES",
    "DELTA_KINDS",
    "DELTA_UNCHANGED_CHECKPOINT",
    "DORMANCY_DAYS",
    "INITIAL_STATE",
    "LEDGER_FAITHFULNESS_FLOOR",
    "STATE_CLOSED",
    "STATE_DE_ESCALATING",
    "STATE_DORMANT",
    "STATE_ESCALATING",
    "STATE_WATCHING",
    "TRAJECTORY_STATES",
    "TrajectoryEvent",
    "TrajectoryTransitionError",
    "delta_requires_evidence",
    "next_state",
    "read_current_states",
    "read_trajectories",
    "read_trajectory",
    "record_situation_events",
]
