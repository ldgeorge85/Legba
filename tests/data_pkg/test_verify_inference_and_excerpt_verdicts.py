# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The two verdict words the faithfulness judges gained, and the loud failure
on any other word.

* ``supported_as_inference`` — a claim worded as a judgement whose facts are
  supported. It PASSES: it counts as supported in the score, its ledger row is
  ``supported`` with a detail saying it passed as an inference, and the run
  counts it under ``claims_supported_as_inference``.
* ``unverifiable_excerpt`` — a claim cited to an article shown only in part,
  whose specifics are not in the part shown. It is not a pass and not a
  finding that the claim is false: it counts in the denominator and not the
  numerator, under its own SOFT reason ``judge_unverifiable_excerpt`` (never
  ``judge_unsupported``, never hard), counted ``claims_unverifiable_excerpt``.
* Any other word fails the call loudly (see the absence-rubric tests for the
  end-to-end case); spelling variants of a contract word are read as that word.

Every test drives ``verify_finding_faithfulness``, the real verify entry, with
a judge double at the model boundary.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

import pytest

import legba.data.provenance.verify as V
from legba.data import grading_text as GT
from legba.data.provenance.judge_transport import normalize_verdict_token
from legba.data.provenance.verify import (
    FAIL_CLASS_SOFT,
    build_faithfulness_critique_payload,
    fail_class_for_reason,
    verify_finding_faithfulness,
)


class _Response:
    def __init__(self, content: str) -> None:
        self.content = content
        self.usage = None


class _StubJudge:
    subprovider = "stub-judge"

    def __init__(self, payload: dict[str, Any]) -> None:
        self._json = json.dumps(payload)
        self.prompts: list[str] = []
        self.systems: list[str] = []

    async def chat_complete(
        self, messages, *, max_tokens=None, temperature=None, system=None, **kw
    ):
        self.prompts.append(messages[0]["content"])
        self.systems.append(system or "")
        return _Response(self._json)


_BODY = (
    "The central bank raised rates by fifty basis points [1].\n"
    "The move likely signals further tightening next quarter [1].\n"
    "Reserves rose sharply on the month [1].\n"
)


def _citations() -> list[dict[str, Any]]:
    return [
        {
            "marker": "[1]",
            "signal_id": str(uuid4()),
            "title": "Bank raises rates",
            "source_text": "The central bank raised its policy rate by fifty basis points.",
            "source_truncated": True,
        }
    ]


async def _run(verdicts: list[Any]):
    judge = _StubJudge({"verdicts": verdicts})
    report = await verify_finding_faithfulness(
        body=_BODY, citations=_citations(), judge_llm=judge
    )
    return report, judge


async def test_an_inference_passes_and_says_so(monkeypatch) -> None:
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "1")
    report, _ = await _run(["supported", "supported_as_inference", "supported"])
    assert report.judge_status == "llm"
    assert report.supported_claims == 3
    assert report.faithfulness_score == pytest.approx(1.0)
    assert report.counters.get("claims_supported_as_inference") == 1
    row = next(cv for cv in report.claim_verdicts if "likely signals" in cv.text)
    assert row.verdict == "supported"
    assert row.reason is None
    assert row.detail and "inference" in row.detail
    assert not report.unsupported_spans


async def test_an_unverifiable_excerpt_is_counted_but_never_called_unsupported(
    monkeypatch,
) -> None:
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "1")
    report, _ = await _run(["supported", "supported", "unverifiable_excerpt"])
    assert report.judge_status == "llm"
    # In the denominator, not the numerator: the claim was not confirmed.
    assert report.supported_claims == 2
    assert report.faithfulness_score == pytest.approx(2 / 3)
    assert report.counters.get("claims_unverifiable_excerpt") == 1
    span = next(s for s in report.unsupported_spans if "Reserves" in s.text)
    assert span.reason == "judge_unverifiable_excerpt"
    assert fail_class_for_reason(span.reason) == FAIL_CLASS_SOFT
    row = next(cv for cv in report.claim_verdicts if "Reserves" in cv.text)
    assert row.verdict == FAIL_CLASS_SOFT
    assert row.reason == "judge_unverifiable_excerpt"
    payload = build_faithfulness_critique_payload(report, analyzed_output_id=uuid4())
    assert payload["data"]["verification"]["claim_verdicts"]


async def test_object_entries_with_problem_and_quote_before_verdict_parse(
    monkeypatch,
) -> None:
    """The reply shape the prompt now asks for: span and problem first."""
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "1")
    report, _ = await _run([
        {"claim_index": 1, "quote": "", "problem": "none", "verdict": "supported"},
        {"claim_index": 2, "quote": "", "problem": "none",
         "verdict": "Supported as inference"},
        {"claim_index": 3, "quote": "", "problem": "not in the part shown",
         "verdict": "unverifiable-excerpt"},
    ])
    assert report.judge_status == "llm"
    assert report.counters.get("claims_supported_as_inference") == 1
    assert report.counters.get("claims_unverifiable_excerpt") == 1


def test_spelling_variants_normalize_but_never_change_the_verdict() -> None:
    assert normalize_verdict_token("Supported as inference") == "supported_as_inference"
    assert normalize_verdict_token(" CONTRADICTED ") == "contradicted"
    assert normalize_verdict_token("unverifiable-excerpt") == "unverifiable_excerpt"
    assert normalize_verdict_token("partly supported") == "partly_supported"


def test_no_failure_reason_is_invented_for_a_passing_verdict() -> None:
    with pytest.raises(ValueError):
        V._judge_reason("supported_as_inference")
    with pytest.raises(ValueError):
        V._judge_reason("probably_fine")
    assert V._judge_reason("unsupported") == "judge_unsupported"
    assert V._judge_reason("unverifiable_excerpt") == "judge_unverifiable_excerpt"


async def test_the_judge_is_told_both_words_and_the_four_checks(monkeypatch) -> None:
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "1")
    _, judge = await _run(["supported", "supported", "supported"])
    system, prompt = judge.systems[0], judge.prompts[0]
    assert GT.FAITHFULNESS_VERDICTS in system
    assert GT.CLAIM_EVIDENCE_RULES in system
    assert GT.PASTED_TEXT_IS_DATA in system
    for token in GT.FAITHFULNESS_VERDICT_TOKENS:
        assert f'"{token}"' in prompt
    # The span and problem come before the verdict in the reply shape.
    shape = prompt[prompt.index('{"verdicts"'):]
    assert shape.index('"quote"') < shape.index('"problem"') < shape.index('"verdict"')
    # A summary line with no marker must restate the cited claims.
    assert "at the same strength" in prompt
