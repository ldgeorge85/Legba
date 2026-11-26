# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""RUST-2 + RUST-3 (2026-08-21) — the absence rubric rewrite and the FOURTH verdict.

RUST-2. The 2026-08-16 panel measured the adjudicated judge errors per prompt
BRANCH: ABSENCE 6 wrong of 7 (86%), GENERIC 8 of 30 (27%), NULL-RESULT 1 of 5.
The negative route was the worst surface in the system, and its rubric was a
verdict definition with no doctrine in it at all. ``absence.v4`` is the rewrite:
identity, both error costs, what a slice-scoped negative IS, what the evidence
map actually contains, this route's OWN adjudicated failure record, and five
fences. The tests here pin the properties that make it that rewrite rather than
a paraphrase, and pin the two containment properties the measurement depends on
(the generic route did not move; the platform is defined in-text and never
named).

RUST-3. The verdict contract grows ``not_a_proposition`` — the answer a judge
handed a heading, a scaffold row or a fragment of tool output never had. It is
EARNED: an earned one leaves the graded population entirely (V-F's treatment of
a split-time drop), an unearned one is WITHDRAWN and the claim still fails soft.

EVERY behavioural test here drives ``verify_finding_faithfulness`` — the real
verify entry — with a judge double at the LLM boundary, so the claim splitter,
``_is_judgeable_claim``, the kind classifier, the partitioner, the severity
chain, the tally reconciliation, the ledger and the payload builder all run for
real. Nothing calls ``_severity`` or ``nonproposition_is_earned`` directly to
assert pipeline behaviour; the unit-level tests of the earn recogniser are
labelled as such and exist only to pin the recogniser's own boundary.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from legba.data.provenance.verify import (
    CLAIM_KIND_ABSENCE,
    CLAIM_KIND_CITATION_SUPPORT,
    FAIL_CLASS_SOFT,
    JUDGE_VERDICT_TOKENS,
    VERDICT_NOT_A_PROPOSITION,
    _ABSENCE_JUDGE_SYSTEM,
    _GENERIC_JUDGE_SYSTEM,
    _JUDGE_NONPROP_UNEARNED,
    _NULL_RESULT_JUDGE_SYSTEM,
    _claim_kind,
    _is_judgeable_claim,
    build_faithfulness_critique_payload,
    fail_class_for_reason,
    nonproposition_is_earned,
    verify_finding_faithfulness,
)


# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------


class _Usage:
    prompt_tokens = 10
    completion_tokens = 5
    reasoning_tokens = 0


class _Response:
    def __init__(self, content: str) -> None:
        self.content = content
        self.usage = _Usage()


class _RouteJudge:
    """Judge double that answers per ROUTE, keyed on prompt IDENTITY.

    Identity, never a substring: RUST-2 rewrote the absence rubric wholesale, and
    a double keyed on a phrase inside a prompt silently mis-routes the moment the
    prompt is edited — which is exactly how a rewrite would ship untested.
    """

    subprovider = "stub"

    def __init__(
        self,
        *,
        absence_json: str = '{"verdicts": []}',
        shared_json: str = '{"verdicts": []}',
        survey_json: str = '{"verdicts": []}',
    ) -> None:
        self._absence = absence_json
        self._shared = shared_json
        self._survey = survey_json
        self.absence_calls = 0
        self.shared_calls = 0
        self.survey_calls = 0
        self.systems: list[str] = []

    async def chat_complete(
        self, messages, *, max_tokens=None, temperature=None, system=None, **kw
    ):
        self.systems.append(system or "")
        if system == _ABSENCE_JUDGE_SYSTEM:
            self.absence_calls += 1
            return _Response(self._absence)
        if system == _NULL_RESULT_JUDGE_SYSTEM:
            self.survey_calls += 1
            return _Response(self._survey)
        self.shared_calls += 1
        return _Response(self._shared)


def _fact_rich_with_absence(sid: str) -> tuple[str, list[dict]]:
    """Two positive cited claims + ONE absence claim.

    Two positives keep ``_is_null_result_finding`` False (it fires at <=1), so
    the finding takes the V3 per-claim partition and the absence rubric is
    actually exercised rather than the M14 whole-finding survey.
    """
    body = (
        "The lira fell three percent today [1].\n"
        "The central bank spent two billion dollars defending the peg [1].\n"
        "No evidence of capital-flight controls appears in the reviewed signals.\n"
    )
    citations = [
        {"marker": "[1]", "signal_id": sid, "title": "Lira drops 3% on the day"}
    ]
    return body, citations


# ---------------------------------------------------------------------------
# 1. RUST-2 — the rubric is the doctrine rewrite, and it is CONTAINED
# ---------------------------------------------------------------------------


def test_absence_rubric_defines_verdicts_by_scope_not_by_lean() -> None:
    """The rubric does not lean either way. A correctly scoped negative over a
    set that lacks the thing is supported with no citation; a negative that
    reaches past what was searched, or rests on evidence too thin to tell, is
    unsupported; a refutation must be source reporting at the denied scale."""
    low = _ABSENCE_JUDGE_SYSTEM.lower()
    assert (
        "a correctly scoped negative over a set that lacks the thing is "
        "supported even with no citation" in low
    )
    assert "too thin to tell" in low
    assert "at the scale and of the kind denied" in low
    # Analyst prose is recognised by its shape, not by a label.
    assert "recognise it by its shape" in low
    # A consequence drawn from an absence is graded as an inference.
    assert "graded as an inference" in low


def test_absence_rubric_carries_no_error_record_or_lean() -> None:
    """The earlier rubric quoted an error record ("six in seven ... every
    recurring error is a FALSE FAIL", "No adjudicator has ever caught this seat
    passing a fabricated absence"), which told the judge which way to lean.
    Statistics and history are not model-visible text; the cases live in the
    judge regression set."""
    low = _ABSENCE_JUDGE_SYSTEM.lower()
    for gone in (
        "six in seven", "false fail", "no adjudicator", "this house",
        "when in doubt", "nobody re-reads",
    ):
        assert gone not in low, gone


def test_absence_rubric_never_names_the_platform_or_the_example_domain() -> None:
    """Platform text speaks in no profile's nouns and never names the project."""
    low = _ABSENCE_JUDGE_SYSTEM.lower()
    assert "legba" not in low
    for word in ("per-country", "public wire", "strikes", "mobilisation", "blockade"):
        assert word not in low, word


def test_absence_rubric_imports_the_shared_blocks() -> None:
    """The scale, plan, party-statement and baseline rules and the pasted-text
    rule are the shared text every grader carries, imported, not restated."""
    from legba.data.grading_text import (
        ABSENCE_VERDICTS,
        CLAIM_EVIDENCE_RULES,
        PASTED_TEXT_IS_DATA,
    )

    assert ABSENCE_VERDICTS in _ABSENCE_JUDGE_SYSTEM
    assert CLAIM_EVIDENCE_RULES in _ABSENCE_JUDGE_SYSTEM
    assert PASTED_TEXT_IS_DATA in _ABSENCE_JUDGE_SYSTEM


def test_absence_reply_shape_puts_basis_and_span_before_the_verdict() -> None:
    shape = _ABSENCE_JUDGE_SYSTEM[_ABSENCE_JUDGE_SYSTEM.index('{"verdicts"'):]
    assert shape.index('"basis"') < shape.index('"quote"') < shape.index('"verdict"')


def test_absence_rewrite_did_not_leak_onto_the_other_routes() -> None:
    """CONTAINMENT. D5 ratified that no candidate judge ships and the incumbent
    generic prompt stays; the absence route was deliberately held out of that
    matrix as a control so this rewrite could be attributed on its own. If the
    doctrine text reaches the generic or survey rubric, that attribution is
    gone."""
    assert "graded as an inference" not in _GENERIC_JUDGE_SYSTEM
    assert "graded as an inference" not in _NULL_RESULT_JUDGE_SYSTEM
    # And only the absence rubric ADVERTISES the fourth verdict (RUST-3).
    assert VERDICT_NOT_A_PROPOSITION in _ABSENCE_JUDGE_SYSTEM
    assert VERDICT_NOT_A_PROPOSITION not in _GENERIC_JUDGE_SYSTEM
    assert VERDICT_NOT_A_PROPOSITION not in _NULL_RESULT_JUDGE_SYSTEM


async def test_absence_partition_sends_the_v6_rubric_through_the_real_entry(
    monkeypatch,
) -> None:
    """REAL BINDING PATH: the pipeline entry routes an absence span to the
    absence rubric (v6) and every other span to the generic prompt, in one
    run."""
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "1")
    body, citations = _fact_rich_with_absence(str(uuid4()))
    judge = _RouteJudge(
        shared_json='{"verdicts": ["supported", "supported"]}',
        absence_json='{"verdicts": ["supported"]}',
    )
    rep = await verify_finding_faithfulness(
        body=body, citations=citations, judge_llm=judge
    )
    assert rep.judge_status == "llm"
    assert judge.absence_calls == 1 and judge.shared_calls == 1
    assert judge.survey_calls == 0
    assert _ABSENCE_JUDGE_SYSTEM in judge.systems
    assert _GENERIC_JUDGE_SYSTEM in judge.systems
    payload = build_faithfulness_critique_payload(rep, analyzed_output_id=uuid4())
    assert payload["data"]["verification"]["branch_versions"]["absence"] == "absence.v6"


# ---------------------------------------------------------------------------
# 2. RUST-3 — the contract
# ---------------------------------------------------------------------------


def test_the_contract_is_six_tokens() -> None:
    """The faithfulness words plus the absence route's not_a_proposition."""
    assert JUDGE_VERDICT_TOKENS == {
        "supported",
        "supported_as_inference",
        "unsupported",
        "contradicted",
        "unverifiable_excerpt",
        "not_a_proposition",
    }


def test_earned_nonproposition_has_no_fail_class_but_the_withdrawal_does() -> None:
    """An earned declination is not a failure and so is not in the fail-class
    table at all; the WITHDRAWN form is soft. Two different things, and the
    ledger has to be able to tell them apart."""
    assert fail_class_for_reason(_JUDGE_NONPROP_UNEARNED) == FAIL_CLASS_SOFT
    assert fail_class_for_reason(VERDICT_NOT_A_PROPOSITION) == FAIL_CLASS_SOFT
    # ... but the earned verdict never becomes a reason in the first place —
    # that is what the two behavioural tests below actually assert.


# The span used for every earned-declination test: tool residue that reached the
# finding body. lens 5's own R5-S7 specimen, and it is a JUDGEABLE claim — the
# splitter keeps it and ``_is_judgeable_claim`` passes it — which is precisely
# why the judge needed a way to say it asserts nothing.
_TOOL_RESIDUE = "Let's do vector_search."


def test_the_residue_span_really_does_reach_the_judge() -> None:
    """Guards the premise of the tests below: if the splitter or the judgeable
    filter ever drops this span, those tests would pass vacuously."""
    assert _is_judgeable_claim(_TOOL_RESIDUE)
    assert nonproposition_is_earned(_TOOL_RESIDUE)


async def test_earned_nonproposition_leaves_the_population(monkeypatch) -> None:
    """REAL BINDING PATH. An EARNED ``not_a_proposition``:
      * is not credited as supported and not charged as a failure;
      * produces no unsupported span and no ledger row;
      * leaves ``checkable`` and every ``branch_scores`` denominator;
      * is COUNTED, so the class can never grow silently.
    """
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "1")
    sid = str(uuid4())
    body = (
        "The lira fell three percent today [1].\n"
        f"{_TOOL_RESIDUE}\n"
        "No evidence of capital-flight controls appears in the reviewed signals.\n"
    )
    citations = [
        {"marker": "[1]", "signal_id": sid, "title": "Lira drops 3% on the day"}
    ]
    # The shared partition holds two spans: the cited fact and the residue.
    judge = _RouteJudge(
        shared_json='{"verdicts": ["supported", "not_a_proposition"]}',
        absence_json='{"verdicts": ["supported"]}',
    )
    rep = await verify_finding_faithfulness(
        body=body, citations=citations, judge_llm=judge
    )
    assert rep.judge_status == "llm"
    # Three spans reached the judge; two were graded.
    assert rep.checkable_claims == 2
    assert rep.supported_claims == 2
    assert rep.faithfulness_score == pytest.approx(1.0)
    assert not any(s.reason.startswith("judge_") for s in rep.unsupported_spans)
    assert all(_TOOL_RESIDUE not in cv.text for cv in rep.claim_verdicts)
    assert rep.counters.get("claims_ungraded_nonpropositional") == 1
    # It is in NO branch denominator — citation_support graded one claim, not two.
    assert rep.branch_scores[CLAIM_KIND_CITATION_SUPPORT]["checkable"] == 1
    assert rep.branch_scores[CLAIM_KIND_ABSENCE]["checkable"] == 1
    # ... and the counter survives to the persisted verification block.
    payload = build_faithfulness_critique_payload(rep, analyzed_output_id=uuid4())
    counters = payload["data"]["verification"]["counters"]
    assert counters["claims_ungraded_nonpropositional"] == 1


async def test_unearned_nonproposition_is_withdrawn_and_still_fails(
    monkeypatch,
) -> None:
    """REAL BINDING PATH. A span carrying a checkable particular cannot be
    "nothing", so the declination is WITHDRAWN: the claim stays in the
    denominator and fails soft under its own reason, with its own counter.

    This is the pass-side guard. Without it the fourth verdict is a hole a judge
    could push any inconvenient claim through.
    """
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "1")
    sid = str(uuid4())
    fabricated = "Tehran resumed enrichment at Natanz on 8 August [1]."
    body = (
        "The lira fell three percent today [1].\n"
        f"{fabricated}\n"
        "No evidence of capital-flight controls appears in the reviewed signals.\n"
    )
    citations = [
        {"marker": "[1]", "signal_id": sid, "title": "Lira drops 3% on the day"}
    ]
    judge = _RouteJudge(
        shared_json='{"verdicts": ["supported", "not_a_proposition"]}',
        absence_json='{"verdicts": ["supported"]}',
    )
    rep = await verify_finding_faithfulness(
        body=body, citations=citations, judge_llm=judge
    )
    assert rep.judge_status == "llm"
    # Nothing left the population: all three spans are still graded.
    assert rep.checkable_claims == 3
    assert rep.supported_claims == 2
    assert "claims_ungraded_nonpropositional" not in rep.counters
    assert rep.counters.get("nonprop_withdrawn_carries_particular") == 1
    spans = [s for s in rep.unsupported_spans if s.reason == _JUDGE_NONPROP_UNEARNED]
    assert len(spans) == 1 and "Natanz" in spans[0].text
    ledger = [cv for cv in rep.claim_verdicts if cv.reason == _JUDGE_NONPROP_UNEARNED]
    assert len(ledger) == 1 and ledger[0].verdict == FAIL_CLASS_SOFT


async def test_a_real_uncited_claim_cannot_be_declined_away(monkeypatch) -> None:
    """The pass-side property that makes the earned branch safe to have at all.

    An earned declination withdraws the FLOOR's span on the same text as well as
    the judge's — otherwise the artifact would survive under another name. What
    bounds that is the EARN TEST: here the judge declines an UNCITED, fact-
    asserting claim that names a checkable particular. The declination is
    withdrawn, and the defect stays visible on BOTH arms.
    """
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "1")
    sid = str(uuid4())
    uncited = "Airbase activity intensified overnight across Bandar Abbas."
    body = f"The lira fell three percent today [1].\n{uncited}\n"
    citations = [
        {"marker": "[1]", "signal_id": sid, "title": "Lira drops 3% on the day"}
    ]
    judge = _RouteJudge(
        shared_json='{"verdicts": ["supported", "not_a_proposition"]}'
    )
    rep = await verify_finding_faithfulness(
        body=body, citations=citations, judge_llm=judge
    )
    assert rep.judge_status == "llm"
    assert "claims_ungraded_nonpropositional" not in rep.counters
    assert rep.counters.get("nonprop_withdrawn_carries_particular") == 1
    assert any(uncited in s.text for s in rep.unsupported_spans)
    assert rep.checkable_claims == 2
    assert rep.faithfulness_score < 1.0


async def test_fourth_verdict_is_accepted_on_the_generic_route_too(
    monkeypatch,
) -> None:
    """The CONTRACT is route-agnostic even though only the absence rubric
    advertises the token. Before RUST-3 a model volunteering it was coerced to
    ``unsupported`` — the pipeline's dominant error class — so a judge giving the
    most honest answer available was scored as if it had found a defect."""
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "1")
    sid = str(uuid4())
    body = f"The lira fell three percent today [1].\n{_TOOL_RESIDUE}\n"
    citations = [
        {"marker": "[1]", "signal_id": sid, "title": "Lira drops 3% on the day"}
    ]
    judge = _RouteJudge(
        shared_json='{"verdicts": ["supported", "not_a_proposition"]}'
    )
    rep = await verify_finding_faithfulness(
        body=body, citations=citations, judge_llm=judge
    )
    # No absence span in this body — the generic route carried both claims.
    assert judge.absence_calls == 0 and judge.shared_calls == 1
    assert rep.counters.get("claims_ungraded_nonpropositional") == 1
    assert rep.checkable_claims == 1


async def test_unknown_verdict_tokens_fail_the_call_loudly(
    monkeypatch, caplog
) -> None:
    """An invented verdict word is never read as some verdict the judge did not
    give. It used to coerce to ``unsupported`` (a silent soft fail); it now
    fails the judge call, the finding falls to the deterministic floor under
    ``judge_error``, and the word is named in the log line."""
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "1")
    sid = str(uuid4())
    body = "The lira fell three percent today [1].\nThe peg held overnight [1].\n"
    citations = [
        {"marker": "[1]", "signal_id": sid, "title": "Lira drops 3% on the day"}
    ]
    judge = _RouteJudge(shared_json='{"verdicts": ["supported", "probably_fine"]}')
    with caplog.at_level("WARNING"):
        rep = await verify_finding_faithfulness(
            body=body, citations=citations, judge_llm=judge
        )
    assert rep.judge_status == "deterministic"
    assert rep.judge_unavailable_reason == "judge_error"
    assert "probably_fine" in caplog.text
    assert not any(s.reason == "judge_unsupported" for s in rep.unsupported_spans)


async def test_fullwidth_markers_still_normalize_on_the_absence_route(
    monkeypatch,
) -> None:
    """NO REGRESSION on the full-width bracket normalization (C1). Core-plane
    models emit ``【N】`` rather than ``[N]``; the splitter normalizes before
    segmentation, so a body written with full-width markers must route, resolve
    and score identically to the ASCII spelling."""
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "1")
    sid = str(uuid4())
    citations = [
        {"marker": "[1]", "signal_id": sid, "title": "Lira drops 3% on the day"}
    ]
    wide = (
        "The lira fell three percent today 【1】.\n"
        "The central bank spent two billion dollars defending the peg 【1】.\n"
        "No evidence of capital-flight controls appears in the reviewed signals.\n"
    )
    ascii_body, _ = _fact_rich_with_absence(sid)
    responses = {
        "shared_json": '{"verdicts": ["supported", "supported"]}',
        "absence_json": '{"verdicts": ["supported"]}',
    }
    wide_rep = await verify_finding_faithfulness(
        body=wide, citations=citations, judge_llm=_RouteJudge(**responses)
    )
    ascii_rep = await verify_finding_faithfulness(
        body=ascii_body, citations=citations, judge_llm=_RouteJudge(**responses)
    )
    assert wide_rep.checkable_claims == ascii_rep.checkable_claims == 3
    assert wide_rep.faithfulness_score == pytest.approx(ascii_rep.faithfulness_score)
    assert (
        wide_rep.branch_scores[CLAIM_KIND_ABSENCE]
        == ascii_rep.branch_scores[CLAIM_KIND_ABSENCE]
    )
    # The absence span classified the same way under both spellings.
    assert _claim_kind(
        "No evidence of capital-flight controls appears in the reviewed signals."
    ) == CLAIM_KIND_ABSENCE


# ---------------------------------------------------------------------------
# 3. The earn recogniser's own boundary (unit-level, deliberately)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "span",
    [
        "### Assessment",  # heading
        "**Drivers**",  # bold label
        "(not_observed)",  # status token
        '"verdict": "supported",',  # JSON residue (Q-1d's class)
        "Let's do vector_search.",  # R5-S7, verbatim
        "Calling vector_search now",
        "[guard] the slice returned no rows",
        "Indicators to watch",
        "**Severity:** elevated",  # labelled scaffold, short value
        "",
    ],
)
def test_earned_shapes(span: str) -> None:
    assert nonproposition_is_earned(span) is True


@pytest.mark.parametrize(
    "span",
    [
        # An ordinary world claim. The recogniser must never admit one, and it
        # cannot tell this apart from "nothing happened here" by shape — which
        # is exactly why it admits only shapes it can NAME.
        "Tehran resumed enrichment at Natanz.",
        "Six launches occurred on 8 August.",
        "Rallies have been smaller in scale and more sporadic.",
        # R4-S9's class: a coverage / denominator statement about this run IS a
        # proposition about the searched set (V-I6 owns the class, and the
        # rubric says so in as many words). Veto 1.
        "No reads were available for other regional members in this cycle.",
        # A "heading" carrying a number. Veto 2 — a particular could hide there.
        "Casualties: 47",
        # A real absence claim: the route that advertises the token is the one
        # that must never be able to use it to dodge its own subject matter.
        "- No other sanctions or tariff hikes were observed in the slice.",
    ],
)
def test_unearned_shapes(span: str) -> None:
    assert nonproposition_is_earned(span) is False


def test_earn_recogniser_never_raises() -> None:
    for junk in (None, 0, [], {}, "   ", "\n\n"):
        assert isinstance(nonproposition_is_earned(junk), bool)
