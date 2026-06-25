# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Guard: every LLM analyst's system prompt is composed with the shared
analytic-tradecraft preamble (planning/PROMPT_REWRITE_PLAN.md). Catches an
accidental un-wrapping of a constant during future edits.
"""

import importlib

import pytest

from legba.data.analysts._tradecraft import ANALYTIC_PREAMBLE

CASES = [
    ("legba.data.analysts.inline_target", "_SYSTEM_PROMPT"),
    ("legba.runtime.analyst_method", "_WORLD_ASSESSOR_SYSTEM"),
    ("legba.data.analysts.critic", "_SYSTEM_PROMPT_DEFAULT"),
    ("legba.data.analysts.consult_on_demand", "_SYSTEM_PROMPT"),
    ("legba.data.analysts.relationship_reifier", "_SYSTEM_PROMPT"),
    ("legba.data.analysts.competing_hypotheses", "_SYSTEM_PROMPT"),
    ("legba.data.analysts.meta_findings_synthesizer", "_SYSTEM_PROMPT"),
    ("legba.data.analysts.cross_analyst_correlator", "_DEFAULT_SYSTEM_PROMPT"),
    ("legba.data.analysts.cross_target_raw", "_BROADER_DATA_SYSTEM"),
]


@pytest.mark.parametrize("module,attr", CASES)
def test_system_prompt_carries_tradecraft_preamble(module: str, attr: str) -> None:
    mod = importlib.import_module(module)
    prompt = getattr(mod, attr)
    assert isinstance(prompt, str)
    assert prompt.startswith(ANALYTIC_PREAMBLE), f"{module}.{attr} missing preamble"
    # a real task block follows the preamble
    assert len(prompt) > len(ANALYTIC_PREAMBLE) + 40


def test_with_preamble_if_absent_is_idempotent() -> None:
    from legba.data.analysts._tradecraft import with_preamble, with_preamble_if_absent

    composed = with_preamble("TASK — do the thing.")
    # already-composed prompt is returned unchanged (no double preamble)
    assert with_preamble_if_absent(composed) == composed
    assert with_preamble_if_absent(composed).count("You are a senior all-source") == 1
    # a bare (promoted-candidate-style) prompt gets the preamble prepended
    bare = "Evolved instruction: focus on second-order effects."
    assert with_preamble_if_absent(bare).startswith(ANALYTIC_PREAMBLE)
    assert with_preamble_if_absent(None) is None
