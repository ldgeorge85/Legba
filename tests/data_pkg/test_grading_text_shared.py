# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Every grader imports the shared grading text instead of restating it.

The pasted-text sentence goes in every system prompt that embeds outside text;
the claim-and-evidence rules in every grader that reads a claim against
evidence; the originators sentence wherever corroboration is counted. One
module holds each, so the wording cannot drift between prompts.
"""

from __future__ import annotations

import pytest

from legba.data import grading_text as GT
from legba.data.analysts.deterministic_handlers import (
    _correctness_rubric as RUB,
    _external_audit_grader as EXT,
    fact_contention_arbiter as ARB,
    standing_auditor as AUD,
)
from legba.data.provenance import verify as V
from legba.data.rounds import judge_render as JR

_PASTED = {
    "generic judge": V._GENERIC_JUDGE_SYSTEM,
    "survey judge": V._NULL_RESULT_JUDGE_SYSTEM,
    "absence judge": V._ABSENCE_JUDGE_SYSTEM,
    "absence slice judge": V._ABSENCE_SLICE_JUDGE_SYSTEM,
    "audit extraction": AUD._EXTRACT_SYSTEM,
    "audit verdict": AUD._JUDGE_SYSTEM,
    "width grader": EXT.GRADER_SYSTEM,
    "tie-break referee": ARB._TIEBREAK_SYSTEM_PROMPT,
    "correctness note": RUB.NOTE_TO_GRADER,
    "round judge contract": JR.judge_contract(JR.build_view([])),
}

_RULES = {
    "generic judge": V._GENERIC_JUDGE_SYSTEM,
    "survey judge": V._NULL_RESULT_JUDGE_SYSTEM,
    "absence judge": V._ABSENCE_JUDGE_SYSTEM,
    "absence slice judge": V._ABSENCE_SLICE_JUDGE_SYSTEM,
    "correctness note": RUB.NOTE_TO_GRADER,
    "round judge contract": JR.judge_contract(JR.build_view([])),
}

_ORIGINATORS = {
    "audit verdict": AUD._JUDGE_SYSTEM,
    "width grader": EXT.GRADER_SYSTEM,
    "tie-break referee": ARB._TIEBREAK_SYSTEM_PROMPT,
}


@pytest.mark.parametrize("name", sorted(_PASTED))
def test_every_grader_says_pasted_text_is_material(name: str) -> None:
    assert GT.PASTED_TEXT_IS_DATA in _PASTED[name]


@pytest.mark.parametrize("name", sorted(_RULES))
def test_every_claim_grader_carries_the_shared_rules(name: str) -> None:
    assert GT.CLAIM_EVIDENCE_RULES in _RULES[name]


@pytest.mark.parametrize("name", sorted(_ORIGINATORS))
def test_corroboration_is_counted_in_originators(name: str) -> None:
    assert GT.ORIGINATORS in _ORIGINATORS[name]


def test_the_rules_read_scale_the_same_way_everywhere() -> None:
    """The conflict the shared block resolves: a smaller-scale report
    contradicts a large-scale claim and supports its denial, on every grader,
    including the correctness rubric."""
    rule = (
        "A report of the same kind of event at a clearly smaller scale "
        "contradicts a claim that it happened at large scale, and supports a "
        "claim that it did not."
    )
    assert rule in GT.CLAIM_EVIDENCE_RULES
    assert (
        "clearly smaller scale contradicts a claim that it happened at large scale"
        in " ".join(RUB.RUBRIC_TEXT.split())
    )


def test_the_referee_is_not_shown_the_scorers_own_numbers() -> None:
    """Shown the scorer's score or confidence mean, a model picks the top
    number and the call adds nothing."""
    from datetime import datetime, timezone

    class _Agg:
        value_key = 'k "1"'
        representative_value = "one\nhundred"
        distinct_source_count = 2
        cred_sum = 1.0
        source_types = {"news"}
        latest_asserted_at = datetime(2026, 11, 1, tzinfo=timezone.utc)
        confidence_mean = 0.77

    prompt = ARB._build_tiebreak_prompt(
        "subj", "pred", [_Agg()], {'k "1"': 0.9123}, datetime.now(timezone.utc)
    )
    assert "score=" not in prompt and "confidence_mean" not in prompt
    assert "0.9123" not in prompt and "0.770" not in prompt
    assert '"k \\"1\\""' in prompt  # JSON-encoded, not repr
    assert "last_asserted=2026-11-01" in prompt
