# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A7 — per-source freshness grading against cadence-derived budgets.

Pure tests: the cadence→budget derivation (max fire-to-fire gap × grace, with
the floor) over the REAL cron shapes the in-tree source descriptors declare,
and the full grade truth table — including the honest ``ungraded`` (no cadence
declaration is NEVER a fake ``ok``) and ``empty`` states.
"""
from __future__ import annotations

import pytest

from legba.data.registry.source_freshness import (
    GRACE_MULTIPLE,
    MIN_BUDGET_MINUTES,
    WARN_MULTIPLE,
    cadence_interval_minutes,
    derive_budget_minutes,
    grade_freshness,
)


# ---------------------------------------------------------------------------
# Budget derivation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "expr,interval_min",
    [
        ("*/15 * * * *", 15),
        ("*/5 * * * *", 5),
        ("27 * * * *", 60),
        ("54 0-23/2 * * *", 120),
        # Ranged hour-step: fires 5:47/11:47/17:47/23:47 — the MAX gap is 6h
        # (incl. the 23:47→5:47 wrap), not a naive per-field read.
        ("47 5-23/6 * * *", 360),
        ("35 3 * * *", 1440),  # daily
    ],
)
def test_cadence_interval_is_the_max_fire_gap(expr, interval_min):
    assert cadence_interval_minutes(expr) == pytest.approx(interval_min)


@pytest.mark.parametrize(
    "expr,budget",
    [
        ("*/15 * * * *", 60),        # 15 × 4
        ("*/5 * * * *", 30),         # 5 × 4 = 20 → clamped to the 30min floor
        ("27 * * * *", 240),         # 60 × 4
        ("47 5-23/6 * * *", 1440),   # 360 × 4
    ],
)
def test_derive_budget_applies_grace_and_floor(expr, budget):
    assert derive_budget_minutes(expr) == budget
    # Contract constants the cases above encode.
    assert GRACE_MULTIPLE == 4.0 and MIN_BUDGET_MINUTES == 30


@pytest.mark.parametrize(
    "expr",
    [None, "", "   ", "not a cron", "61 * * * *", "* * * *"],
)
def test_underivable_cadence_yields_no_budget(expr):
    assert derive_budget_minutes(expr) is None
    assert cadence_interval_minutes(expr) is None


def test_budget_cache_is_stable_per_expression():
    assert derive_budget_minutes("*/15 * * * *") == 60
    assert derive_budget_minutes("*/15 * * * *") == 60  # memo hit, same value


# ---------------------------------------------------------------------------
# Grade truth table
# ---------------------------------------------------------------------------

_HOUR = 3600


@pytest.mark.parametrize(
    "state,age_seconds,budget_minutes,expected",
    [
        # -- ungraded: no honest grade exists ------------------------------
        ("active", 100, None, "ungraded"),        # no cadence declaration
        ("active", None, None, "ungraded"),       # no cadence AND no signal
        (None, 100, None, "ungraded"),            # NULL state reads active
        ("paused", 100, 60, "ungraded"),          # non-active: no expectation
        ("retired", None, 60, "ungraded"),
        ("active", 100, 0, "ungraded"),           # degenerate budget
        # -- empty: active + budgeted but NEVER produced a signal ----------
        ("active", None, 60, "empty"),
        (None, None, 60, "empty"),
        # -- ok / stale / warn against the budget --------------------------
        ("active", 30 * 60, 60, "ok"),
        ("active", 60 * 60, 60, "ok"),            # boundary: age == budget
        ("active", 60 * 60 + 60, 60, "stale"),    # just over budget
        ("active", 3 * 60 * 60, 60, "stale"),     # boundary: 3× budget
        ("active", 3 * 60 * 60 + 60, 60, "warn"),  # beyond 3× budget
        ("active", 48 * _HOUR, 60, "warn"),
        (None, 10, 60, "ok"),                     # NULL state grades normally
    ],
)
def test_grade_truth_table(state, age_seconds, budget_minutes, expected):
    assert (
        grade_freshness(
            state=state,
            age_seconds=age_seconds,
            budget_minutes=budget_minutes,
        )
        == expected
    )


def test_warn_multiple_contract():
    assert WARN_MULTIPLE == 3.0
