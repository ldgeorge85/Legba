# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Regression for the bring-up registrar's lifecycle-advance on fresh register.

A fresh cold-start registers descriptors at the core registry's initial DRAFT
state, then the registrar walks them to their declared state along the legal
FSM path (`_legal_path`). Before the fix, that walk only ran on RE-registration
of an existing draft head, so a first boot left every declared-`active`
descriptor (G20 targets, the analyst set) stuck at `draft` — which then excluded
them from the `state='active'` enumeration paths (boot wiring, cross-target).

These assert the FSM path the fix depends on; the full fresh-register walk is
exercised end-to-end on the next cold-start re-seed.
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))

from _p17_registrar import _legal_path  # noqa: E402

from legba.data.schemas.lifecycle import LifecycleState as LS  # noqa: E402


def test_draft_to_active_walks_through_configured() -> None:
    assert _legal_path(LS.DRAFT, LS.ACTIVE) == [LS.CONFIGURED, LS.ACTIVE]


def test_configured_to_active_is_one_hop() -> None:
    assert _legal_path(LS.CONFIGURED, LS.ACTIVE) == [LS.ACTIVE]


def test_same_state_is_a_noop_path() -> None:
    assert _legal_path(LS.ACTIVE, LS.ACTIVE) == []


def test_active_is_unreachable_from_terminal_retired() -> None:
    assert _legal_path(LS.RETIRED, LS.ACTIVE) is None
