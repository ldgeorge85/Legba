# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""country_assessor DSPy prompt module (the GEPA parent).

dspy is a worker-only dependency (never installed in the runtime/host
image), so these tests SKIP where dspy is absent and run in the GEPA
worker image / any dspy-present environment. The live worker-image proof
is the authoritative check; this guards against regressions.
"""
from __future__ import annotations

import importlib.util

import pytest

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("dspy") is None,
    reason="dspy is a worker-only dependency; not installed on the host",
)


def test_build_returns_module_with_baseline_instructions():
    import legba.prompts.country_assessor.v1 as m
    from legba.data.analysts.inline_target import _SYSTEM_PROMPT

    # Single source of truth: GEPA evolves exactly the prompt inference uses.
    assert m.BASELINE_INSTRUCTIONS == _SYSTEM_PROMPT

    mod = m.build()
    predictors = list(mod.predictors())
    assert len(predictors) == 1
    assert predictors[0].signature.instructions == _SYSTEM_PROMPT


def test_parent_path_resolves_to_real_text_not_the_missing_marker():
    """The whole point of #37 stage C: the optimizer's parent-text load must
    return the real prompt, not the ``<<missing prompt module>>`` seam."""
    import asyncio

    from legba.runtime.dapr_workflow.gepa import _load_parent_prompt_text

    text = asyncio.run(
        _load_parent_prompt_text("legba.prompts.country_assessor.v1")
    )
    assert not text.startswith("<<missing")
    assert "intelligence analyst" in text.lower()


def test_seed_from_promoted_champion_instructions():
    """A promoted champion's instruction text seeds the module instead of the
    baseline — the mechanism the promotion loop reuses."""
    import legba.prompts.country_assessor.v1 as m

    champion = "You are a SHARPER analyst. Respond with strict JSON only."
    mod = m.CountryAssessorCycle(instructions=champion)
    assert list(mod.predictors())[0].signature.instructions == champion
