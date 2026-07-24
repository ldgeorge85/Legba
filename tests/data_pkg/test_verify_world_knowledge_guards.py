# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""M13 / M14 / M15 (2026-07-06 live audit) — write/verify-time correctness guards.

Deterministic: the optional LLM judge is OFF by default (floor) or a canned stub.

  * M13 — the stale-cutoff current-leader guard (``stale_leader_spans``): calling
    the SITTING US president "former" is FLAGGED (demotes effective_confidence),
    a correct "former President Biden" is NOT.
  * M14 — honest NULL-RESULT / survey findings: the RANGE citation ``[1-92]``
    resolves, a ``[no citation]`` line is floor-exempt, and a corpus-negative
    routes to the survey judge rubric (an absence scores healthy; a fabrication
    still scores low).
  * M15 — the cross-target guard (``cross_target_leak_span``): a per-country
    finding naming ONLY other countries than its desk target is FLAGGED.
"""

from __future__ import annotations

import pytest

from legba.data.provenance.verify import (
    _NULL_RESULT_JUDGE_SYSTEM,
    _deterministic_floor,
    _is_fact_asserting,
    _is_null_result_finding,
    _range_markers,
    cross_target_leak_span,
    stale_leader_spans,
    verify_finding_faithfulness,
)


class _Usage:
    prompt_tokens = 10
    completion_tokens = 5
    reasoning_tokens = 0


class _Response:
    def __init__(self, content: str) -> None:
        self.content = content
        self.usage = _Usage()


class _CapturingJudge:
    """Judge stub that returns canned verdicts AND records the system prompt used."""

    subprovider = "vllm:stub"

    def __init__(self, verdicts_json: str) -> None:
        self._json = verdicts_json
        self.systems: list[str] = []
        self.prompts: list[str] = []

    async def chat_complete(self, messages, *, max_tokens=None, temperature=None, system=None, **kw):
        self.systems.append(system or "")
        self.prompts.append(messages[0]["content"] if messages else "")
        return _Response(self._json)


# ---------------------------------------------------------------------------
# M13 — stale-cutoff current-leader guard
# ---------------------------------------------------------------------------


def test_stale_leader_flags_sitting_president_called_former():
    spans = stale_leader_spans(
        "renewed US-Russia cooperation via former President Trump [1]"
    )
    assert len(spans) == 1
    assert spans[0].reason == "stale_leader"


def test_stale_leader_flags_reversed_shape_and_wrong_current_holder():
    assert stale_leader_spans("Trump, the former president, brokered the deal")
    assert stale_leader_spans("the current US president Joe Biden signed the order")


def test_stale_leader_flags_hyphenated_ex_president_trump():
    # FIX #3: the hyphenated "ex-President Trump" (no space after 'ex-') must flag.
    assert stale_leader_spans("brokered by ex-President Trump last week")
    assert stale_leader_spans("past President Trump weighed in")


def test_stale_leader_ignores_correct_references():
    # Biden IS a former president — correct, must NOT flag.
    assert stale_leader_spans("under former President Biden, policy shifted") == []
    # FIX #3: "President Biden, now a private citizen" CORRECTLY says he is out of
    # office — the dropped bare now/today proximity must no longer false-flag it.
    assert stale_leader_spans("President Biden, now a private citizen, spoke out") == []
    assert stale_leader_spans("President Biden today defended his record") == []
    # A different former president named plainly — must NOT flag.
    assert stale_leader_spans("President Obama attended the summit") == []
    # The sitting president named correctly — must NOT flag.
    assert stale_leader_spans("President Trump announced new sanctions today") == []
    assert stale_leader_spans("") == []


@pytest.mark.asyncio
async def test_verify_demotes_stale_leader_finding():
    """A cited clause naming the sitting president 'former' still demotes: the
    stale-leader guard folds an extra unsupported checkable claim (flag, not delete)."""
    body = "US-Russia cooperation is being renewed via former President Trump [1]."
    citations = [{"marker": "[1]", "signal_id": "sig-1"}]
    report = await verify_finding_faithfulness(body=body, citations=citations)
    # 1 supported cited clause + 1 stale_leader guard span → 1/2 = 0.5.
    assert report.faithfulness_score == pytest.approx(0.5)
    assert any(s.reason == "stale_leader" for s in report.unsupported_spans)


# ---------------------------------------------------------------------------
# M14 — range parser, [no citation] exemption, null-result rubric
# ---------------------------------------------------------------------------


def test_range_markers_expands_and_caps():
    assert _range_markers("survey [3-6]") == {3, 4, 5, 6}
    assert _range_markers("en-dash [1–3]") == {1, 2, 3}
    assert _range_markers("no range here [5]") == set()
    # Pathological width is ignored (guarded).
    assert _range_markers("[1-100000]") == set()


def test_range_citation_resolves_survey_clause():
    """``[1-92]`` cites the whole enumerated corpus; a member signal resolves it.
    Without the range parser the clause would floor as ``no_citation`` (0.0)."""
    body = "The signals concern floods, sports fixtures, and trade logistics [1-92]."
    citations = [{"marker": "[5]", "signal_id": "sig-5"}]
    report = _deterministic_floor(body, citations)
    assert report.faithfulness_score == pytest.approx(1.0)
    assert report.supported_claims == 1


def test_no_citation_marker_is_floor_exempt():
    line = "Tehran's posture is best read as steady in our synthesis [no citation]"
    assert _is_fact_asserting(line) is False
    report = _deterministic_floor(line, [])
    assert report.checkable_claims == 0
    assert report.faithfulness_score == pytest.approx(1.0)


def test_null_result_finding_detected_and_positive_not():
    null_body = (
        "None of the 78 signals reference political unrest concerning Romania's "
        "leadership. The signals focus on floods, sports, and trade."
    )
    assert _is_null_result_finding(null_body) is True
    positive_body = (
        "Iran resumed uranium enrichment at Natanz [1]. Centrifuges were "
        "reinstalled at Fordow [2]. The IAEA confirmed the breakout [3]."
    )
    assert _is_null_result_finding(positive_body) is False


@pytest.mark.asyncio
async def test_null_result_uses_survey_rubric_and_scores_healthy(monkeypatch):
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "1")
    body = (
        "No proliferation activity was observed. The 51 signals focus on floods, "
        "sports, and trade."
    )
    judge = _CapturingJudge('{"verdicts": ["supported", "supported"]}')
    report = await verify_finding_faithfulness(body=body, citations=[], judge_llm=judge)
    # The survey rubric was selected...
    assert any("NULL-RESULT" in s for s in judge.systems)
    assert judge.systems[0] == _NULL_RESULT_JUDGE_SYSTEM
    # ...and an honest null scores healthy rather than collapsing to ~0.
    assert report.judge_status == "llm"
    assert report.faithfulness_score >= 0.9


@pytest.mark.asyncio
async def test_fabrication_still_scores_low_with_standard_rubric(monkeypatch):
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "1")
    body = "Iran launched a nuclear warhead at Tel Aviv this morning [1]."
    citations = [{"marker": "[1]", "signal_id": "sig-1", "title": "IRNA sports roundup"}]
    judge = _CapturingJudge('{"verdicts": ["contradicted"]}')
    report = await verify_finding_faithfulness(body=body, citations=citations, judge_llm=judge)
    # A positive fabrication is NOT a null-result → standard rubric, scores low.
    assert judge.systems[0] != _NULL_RESULT_JUDGE_SYSTEM
    assert report.faithfulness_score < 0.5


# ---------------------------------------------------------------------------
# M15 — cross-target leak guard
# ---------------------------------------------------------------------------


def test_cross_target_leak_flags_wrong_country():
    span = cross_target_leak_span(
        title="Romania – Leadership transition risk low",
        body="Near-term change in Romania's head of government is unlikely.",
        target_id="country_g20_tr",
    )
    assert span is not None
    assert span.reason == "cross_target_leak"
    assert "romania" in span.text.lower()


def test_cross_target_leak_common_word_slug_desks_are_not_inert():
    """FIX #1: desks whose ISO-2 slug is a common English word (India='in',
    Italy='it', US='us', Indonesia='id') must still flag — the slug must NOT be in
    the on-target mention set, or the word 'in'/'it'/'us' in normal prose fires the
    early-return on every finding and silently disables the guard."""
    # India desk, finding entirely about Pakistan, never says "India" → FLAGGED.
    span = cross_target_leak_span(
        title="Leadership steady",
        body="Pakistan's coalition faces no near-term instability in the capital.",
        target_id="country_g20_in",
    )
    assert span is not None and span.reason == "cross_target_leak"
    assert "pakistan" in span.text.lower()
    # A legitimate India finding that names India → NOT flagged (even though the
    # word 'in' appears throughout the prose).
    assert cross_target_leak_span(
        title="India leadership steady",
        body="India's ruling coalition remains stable in the interim.",
        target_id="country_g20_in",
    ) is None


def test_cross_target_leak_none_when_on_target_or_generic():
    # Mentions its own country (Turkey) → on-target.
    assert cross_target_leak_span(
        title="Turkey leadership steady",
        body="President Erdogan faces no near-term challenge in Turkey.",
        target_id="country_g20_tr",
    ) is None
    # Names no country at all → generic/thin, not flagged.
    assert cross_target_leak_span(
        title="Leadership transition risk low",
        body="No drivers of leadership change are observed in the signals.",
        target_id="country_g20_tr",
    ) is None
    # Non-country / meta target → never flagged.
    assert cross_target_leak_span(
        title="World assessment", body="Romania and Poland see elections.",
        target_id=None,
    ) is None


@pytest.mark.asyncio
async def test_verify_demotes_cross_target_finding():
    """The live M15 shape: a Turkey desk head entirely about Romania → flagged +
    demoted (the guard adds an unsupported checkable claim), not deleted."""
    body = (
        "**BLUF:** Near-term change in Romania's head of government or head of "
        "state is unlikely.\nRomania's current leaders are not under pressure."
    )
    report = await verify_finding_faithfulness(
        body=body, citations=[], title="Romania – Leadership transition risk low",
        target_id="country_g20_tr",
    )
    assert any(s.reason == "cross_target_leak" for s in report.unsupported_spans)
    assert report.faithfulness_score < 1.0
