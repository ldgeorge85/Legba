# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Wave B prereq #4 — DSPy ``build_prompt_module`` backfill across 5 kinds.

Each LLM-bearing analyst kind now exposes a real :class:`dspy.Module`
subclass per L-105 §2.3.  These tests verify:

  1. The kind module's ``build_prompt_module()`` returns an instance of
     the expected DSPy module class.
  2. The DSPy module's :class:`dspy.Signature` carries the expected
     typed input/output fields.
  3. ``ModuleNotFoundError`` propagates cleanly when dspy isn't installed
     (matches the inline_target contract).

The 5 kinds covered here:
  * cross_target_raw           (L-171)
  * meta_findings_synthesizer  (L-172)
  * predictor                  (L-174)  — narrative wrapper only
  * cross_analyst_correlator   (L-177)
  * consult_on_demand          (L-178)  — per-round ReAct step

The inline_target kind (L-170) is already covered in
``test_analyst_inline_target.py``.
"""

from __future__ import annotations

import pytest

# ---------------------------------------------------------------------------
# cross_target_raw (L-171)
# ---------------------------------------------------------------------------


def test_cross_target_raw_build_prompt_module():
    pytest.importorskip("dspy")
    from legba.data.analysts.cross_target_raw import (
        PROMPT_MODULE_PATH,
        build_prompt_module,
    )
    from legba.prompts.cross_target_raw.v1 import (
        BroaderDataSignature,
        CrossTargetRawCycle,
    )

    assert PROMPT_MODULE_PATH == "legba.prompts.cross_target_raw.v1"

    built = build_prompt_module()
    assert isinstance(built, CrossTargetRawCycle)
    assert hasattr(built, "reason")

    fields = BroaderDataSignature.model_fields
    for fname in (
        "target_ids", "signals_block",  # inputs
        "rationale", "title", "body", "confidence", "evidence", "tags",
    ):
        assert fname in fields, f"missing field: {fname}"


# ---------------------------------------------------------------------------
# meta_findings_synthesizer (L-172)
# ---------------------------------------------------------------------------


def test_meta_findings_synthesizer_build_prompt_module():
    pytest.importorskip("dspy")
    from legba.data.analysts.meta_findings_synthesizer import (
        PROMPT_MODULE_PATH,
        build_prompt_module,
    )
    from legba.prompts.meta_findings_synthesizer.v1 import (
        MetaFindingsSynthesizerCycle,
        MetaSynthesisSignature,
    )

    assert PROMPT_MODULE_PATH == "legba.prompts.meta_findings_synthesizer.v1"

    built = build_prompt_module()
    assert isinstance(built, MetaFindingsSynthesizerCycle)
    assert hasattr(built, "synthesize")

    fields = MetaSynthesisSignature.model_fields
    for fname in (
        "findings_block", "contributing_analysts",  # inputs
        "rationale", "title", "body", "confidence", "evidence", "tags",
    ):
        assert fname in fields, f"missing field: {fname}"


# ---------------------------------------------------------------------------
# predictor (L-174) — narrative wrapper only
# ---------------------------------------------------------------------------


def test_predictor_build_prompt_module():
    pytest.importorskip("dspy")
    from legba.data.analysts.predictor import (
        PROMPT_MODULE_PATH,
        build_prompt_module,
    )
    from legba.prompts.predictor.v1 import (
        ForecastNarrativeSignature,
        PredictorNarrative,
    )

    assert PROMPT_MODULE_PATH == "legba.prompts.predictor.v1"

    built = build_prompt_module()
    assert isinstance(built, PredictorNarrative)
    assert hasattr(built, "narrate")

    fields = ForecastNarrativeSignature.model_fields
    for fname in (
        # Inputs — match the kind handler's _render_narrative_prompt shape
        "target_id", "observed_window", "daily_counts",
        "forecast_method", "forecast_horizon_days",
        "point_estimate", "ci_low", "ci_high", "ci_level",
        "recent_signals_block",
        # Outputs
        "narrative",
    ):
        assert fname in fields, f"missing field: {fname}"


# ---------------------------------------------------------------------------
# cross_analyst_correlator (L-177)
# ---------------------------------------------------------------------------


def test_cross_analyst_correlator_build_prompt_module():
    pytest.importorskip("dspy")
    from legba.data.analysts.cross_analyst_correlator import (
        PROMPT_MODULE_PATH,
        build_prompt_module,
    )
    from legba.prompts.cross_analyst_correlator.v1 import (
        CorrelationSignature,
        CrossAnalystCorrelatorCycle,
    )

    assert PROMPT_MODULE_PATH == "legba.prompts.cross_analyst_correlator.v1"

    built = build_prompt_module()
    assert isinstance(built, CrossAnalystCorrelatorCycle)
    assert hasattr(built, "correlate")

    fields = CorrelationSignature.model_fields
    for fname in (
        "outputs_block",  # input
        "rationale", "correlation_type", "title", "body",
        "referenced_outputs", "referenced_analyst_ids",
        "confidence", "tags",
    ):
        assert fname in fields, f"missing field: {fname}"


# ---------------------------------------------------------------------------
# consult_on_demand (L-178) — per-round ReAct step
# ---------------------------------------------------------------------------


def test_consult_on_demand_build_prompt_module():
    pytest.importorskip("dspy")
    from legba.data.analysts.consult_on_demand import (
        PROMPT_MODULE_PATH,
        build_prompt_module,
    )
    from legba.prompts.consult_on_demand.v1 import (
        ConsultOnDemandRound,
        ConsultRoundSignature,
    )

    assert PROMPT_MODULE_PATH == "legba.prompts.consult_on_demand.v1"

    built = build_prompt_module()
    assert isinstance(built, ConsultOnDemandRound)
    assert hasattr(built, "step")

    fields = ConsultRoundSignature.model_fields
    for fname in (
        # Inputs
        "question", "scope_predicate", "accumulated_context", "rounds_remaining",
        # Outputs — both branches in one signature
        "is_final", "tool", "tool_args_json",
        "answer", "uncertainty", "cited_refs", "unanswered_aspects",
    ):
        assert fname in fields, f"missing field: {fname}"


# ---------------------------------------------------------------------------
# critic (L-175)
# ---------------------------------------------------------------------------


def test_critic_build_prompt_module():
    pytest.importorskip("dspy")
    from legba.data.analysts.critic import (
        PROMPT_MODULE_PATH,
        build_prompt_module,
    )
    from legba.prompts.critic.v1 import (
        CriticJudge,
        CriticSignature,
    )

    assert PROMPT_MODULE_PATH == "legba.prompts.critic.v1"

    built = build_prompt_module()
    assert isinstance(built, CriticJudge)
    assert hasattr(built, "judge")

    fields = CriticSignature.model_fields
    for fname in (
        # Inputs
        "analyzed_output", "rubric", "analyzed_analyst_id",
        # Outputs
        "rationale", "scores", "overall_score", "revision_delta", "confidence",
    ):
        assert fname in fields, f"missing field: {fname}"


# ---------------------------------------------------------------------------
# Lazy-import contract (matches inline_target)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kind_module_path,build_attr",
    [
        ("legba.data.analysts.cross_target_raw", "build_prompt_module"),
        ("legba.data.analysts.meta_findings_synthesizer", "build_prompt_module"),
        ("legba.data.analysts.predictor", "build_prompt_module"),
        ("legba.data.analysts.cross_analyst_correlator", "build_prompt_module"),
        ("legba.data.analysts.consult_on_demand", "build_prompt_module"),
        ("legba.data.analysts.critic", "build_prompt_module"),
    ],
)
def test_kind_modules_import_without_dspy_loaded(kind_module_path, build_attr):
    """The kind modules must import cleanly even without dspy installed.

    ``build_prompt_module`` lazy-imports its dspy.Module — so just importing
    the kind module (without calling build_prompt_module) must NOT raise
    ModuleNotFoundError, even if dspy is absent.  This test verifies the
    import path doesn't accidentally regress to a top-level dspy import.
    """
    import importlib
    mod = importlib.import_module(kind_module_path)
    assert hasattr(mod, build_attr)
    assert callable(getattr(mod, build_attr))
