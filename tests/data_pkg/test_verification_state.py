# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""X4 READPATH (W11-F53) — the pending|absent state of a block-less finding."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from legba.data.registry.verification_state import (
    JUDGE_PENDING_WINDOW_S,
    SCORE_STATE_ABSENT,
    SCORE_STATE_PENDING,
    unverified_state,
)

NOW = datetime(2026, 10, 9, 4, 50, tzinfo=timezone.utc)


def test_window_is_above_the_measured_verify_lag():
    # 7-day max lag from a finding to its faithfulness critique, measured live
    # 2026-10-13: 549 s. The window must clear it, or a slow pass reads `absent`.
    assert JUDGE_PENDING_WINDOW_S > 549


def test_pending_inside_the_window_absent_at_and_after_it():
    inside = NOW - timedelta(seconds=JUDGE_PENDING_WINDOW_S - 1)
    edge = NOW - timedelta(seconds=JUDGE_PENDING_WINDOW_S)
    pending = unverified_state(inside, "escalation", now=NOW)
    assert pending["score_state"] == SCORE_STATE_PENDING
    assert pending["reason"] == "awaiting_verify"
    at_edge = unverified_state(edge, "escalation", now=NOW)
    assert at_edge["score_state"] == SCORE_STATE_ABSENT
    assert at_edge["reason"] == "no_verdict"


def test_exempt_analyst_is_absent_immediately():
    out = unverified_state(NOW, "reference_builder", now=NOW)
    assert out == {
        "score_state": SCORE_STATE_ABSENT,
        "since": NOW.isoformat(),
        "judge_window_s": JUDGE_PENDING_WINDOW_S,
        "reason": "verify_exempt",
    }


def test_callers_exemption_verdict_wins_over_the_registry():
    # The full row's `verify_exempt` stamp also knows a deterministic ROLLUP by
    # its payload, which the analyst registry alone cannot.
    rollup = unverified_state(NOW, "region_composition", exempt=True, now=NOW)
    assert rollup["score_state"] == SCORE_STATE_ABSENT
    assert rollup["reason"] == "verify_exempt"
    stamped_not_exempt = unverified_state(NOW, "graph_mining", exempt=False, now=NOW)
    assert stamped_not_exempt["score_state"] == SCORE_STATE_PENDING


def test_never_carries_a_score_and_reads_a_naive_stamp_as_utc():
    # 04:41:26 → 04:50:00 is 514 s: inside the window, so still pending.
    out = unverified_state(datetime(2026, 10, 9, 4, 41, 26), None, now=NOW)
    assert out["score_state"] == SCORE_STATE_PENDING
    assert out["since"] == "2026-10-09T04:41:26+00:00"
    assert not any("score" in k and k != "score_state" for k in out)


def test_states_never_collide_with_a_real_verify_blocks_score_state():
    # A REAL block also carries `score_state` (Q-1 / SW-9); the UI tells the two
    # apart by the value, so the vocabularies must stay disjoint.
    from legba.data.provenance import judge_assessability as ja

    verify_states = {
        v for k, v in vars(ja).items() if k.startswith("SCORE_STATE_") and isinstance(v, str)
    }
    assert verify_states, "judge_assessability no longer names its score states"
    assert {SCORE_STATE_PENDING, SCORE_STATE_ABSENT}.isdisjoint(verify_states)
