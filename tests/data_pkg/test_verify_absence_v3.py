# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""V3 (MP:DEC-E) — the per-claim ABSENCE judge branch.

The absence branch replaces the blanket floor-exemption + free-latitude generic
judge (which scored honest null absence claims 0.0/0.2/1.0 across runs on
identical prose) with a DETERMINISTIC claim-kind classifier routing absence
spans to a dedicated NEGATIVE-claim judge prompt. These tests are deterministic:
the LLM judge is either OFF (``LEGBA_VERIFY_LLM_JUDGE`` unset) or mocked. No test
depends on a live LLM call.

Coverage maps to the design doc §3.6:
  * the classifier assigns ``absence`` to the absence lexical set + bare-``no ``
    openers, and does NOT mis-assign the guarded idioms or a forward-looking
    signpost;
  * a scoped absence over an evidence set lacking the thing → supported;
  * "no strikes reported" with a cited strike → ``judge_contradicted`` span +
    demoted headline;
  * an unbounded/unscoped absence → unsupported;
  * the SAME absence prose graded across repeated (mock-deterministic) runs →
    the SAME verdict (the anti-0.0/0.2/1.0 guard);
  * the critique payload stamps ``branch_versions.absence == 'absence.v3'``;
  * a finding with ZERO absence spans is byte-identical vs pre-V3 (the pooled-
    ratio + judge-call-count invariant) — the regression that non-absence claims
    are unaffected;
  * a WHOLE-FINDING null (≤1 positive) still routes to the M14 survey rubric,
    not the per-claim absence branch (M14 coexistence, design §3.5) — the
    absence branch owns only the embedded-absence-in-a-fact-rich-finding case.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from legba.data.provenance.verify import (
    CLAIM_KIND_ABSENCE,
    CLAIM_KIND_CITATION_SUPPORT,
    CLAIM_KIND_FORWARD_LOOKING,
    CLAIM_KIND_STRUCTURE,
    CLAIM_KIND_SYNTHESIS,
    _claim_kind,
    _is_fact_asserting,
    build_faithfulness_critique_payload,
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


class _PartitionJudge:
    """Judge stub that answers each judge route differently, keyed on the
    system prompt.

    V3 partitions the graded claims into an absence call (system prompt names
    'ABSENCE / NEGATIVE claims') and a shared unit/composition call; the M14
    whole-finding null-result route keeps its own survey rubric (system names
    'NULL-RESULT'). This stub returns ``absence_json`` / ``survey_json`` /
    ``shared_json`` per route, so a test can drive each branch's verdict
    independently. It records per-route call counts + the last absence system
    prompt so a test can assert the dedicated rubric actually fired.
    """

    subprovider = "stub"

    def __init__(self, *, absence_json: str = '{"verdicts": []}',
                 shared_json: str = '{"verdicts": []}',
                 survey_json: str = '{"verdicts": []}') -> None:
        self._absence = absence_json
        self._shared = shared_json
        self._survey = survey_json
        self.absence_calls = 0
        self.shared_calls = 0
        self.survey_calls = 0
        self.calls = 0
        self.last_absence_system = ""

    async def chat_complete(self, messages, *, max_tokens=None, temperature=None,
                            system=None, **kw):
        self.calls += 1
        sys = system or ""
        if "ABSENCE / NEGATIVE claims" in sys:
            self.absence_calls += 1
            self.last_absence_system = sys
            return _Response(self._absence)
        if "NULL-RESULT" in sys:
            self.survey_calls += 1
            return _Response(self._survey)
        self.shared_calls += 1
        return _Response(self._shared)


# ---------------------------------------------------------------------------
# 1. The deterministic claim-kind classifier (design §2.1 / §3.6)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "span",
    [
        "No evidence of coordinated military activity was found in the reviewed signals.",
        "There is no indication of an imminent strike.",
        "No reports of casualties emerged this week.",
        "The absence of any credible mobilization is notable.",
        "None were observed across the monitored corpus.",
        "No confirmed movement of armor near the border.",
    ],
)
def test_absence_classifier_routes_absence(span):
    """Each absence lexical-set phrase / bare-``no `` opener classifies ``absence``."""
    assert _claim_kind(span) == CLAIM_KIND_ABSENCE


@pytest.mark.parametrize(
    "span",
    [
        "No fewer than twelve battalions crossed the line [1].",
        "No longer confined to the coast, the offensive widened [2].",
        "No doubt the escalation is deliberate, per the cited cable [3].",
        "No single actor controls the corridor [4].",
    ],
)
def test_absence_classifier_does_not_route_guarded_idioms(span):
    """The positive ``no fewer/longer/doubt/single`` idioms are NOT absence —
    they must stay ``citation_support`` (the same guard the floor applies)."""
    assert _claim_kind(span) != CLAIM_KIND_ABSENCE
    assert _claim_kind(span) == CLAIM_KIND_CITATION_SUPPORT


def test_absence_classifier_forward_looking_beats_absence():
    """A future-conditional signpost is ``forward_looking``, never absence — the
    priority order (forward_looking > absence) stops a 'no X would confirm'
    watch bullet being mis-routed to the negative-claim prompt."""
    span = "No further escalation to watch for would confirm de-escalation."
    assert _claim_kind(span) == CLAIM_KIND_FORWARD_LOOKING


def test_absence_classifier_structure_beats_absence():
    """A markdown/bold heading is ``structure`` even if it contains a negative —
    structure is the highest priority so a heading is never a claim of any kind."""
    assert _claim_kind("## No developments") == CLAIM_KIND_STRUCTURE
    assert _claim_kind("**No change**") == CLAIM_KIND_STRUCTURE


def test_absence_classifier_synthesis_and_citation_support():
    """The residual kinds still classify: a BLUF opener is ``synthesis``; a plain
    cited fact is ``citation_support``."""
    assert _claim_kind("BLUF: the theater is quiet this week.") == CLAIM_KIND_SYNTHESIS
    assert (
        _claim_kind("The central bank raised rates by fifty basis points [1].")
        == CLAIM_KIND_CITATION_SUPPORT
    )


def test_classifier_absence_route_agrees_with_floor_exemption():
    """The classifier's ``absence`` route and the floor's absence EXEMPTION share
    one definition — a span routed ``absence`` is floor-exempt (``_is_fact_asserting``
    False), and a guarded idiom is neither."""
    absent = "No evidence of an attack appears in the reviewed signals."
    guarded = "No fewer than three units mobilized [1]."
    assert _claim_kind(absent) == CLAIM_KIND_ABSENCE
    assert _is_fact_asserting(absent) is False  # floor exempts the same span
    assert _claim_kind(guarded) == CLAIM_KIND_CITATION_SUPPORT
    assert _is_fact_asserting(guarded) is True  # guarded idiom is a real fact


# ---------------------------------------------------------------------------
# 2. The absence JUDGE branch (design §3.4 / §3.5)
# ---------------------------------------------------------------------------


def _mixed_body_one_absence(sid: str) -> tuple[str, list[dict]]:
    """A FACT-RICH finding with ONE embedded absence claim — the case the M14
    whole-finding null path misses (design §3.2 #4). Two positive cited claims
    keep ``_is_null_result_finding`` False (it fires only at ≤1 positive), so
    the finding routes to the V3 per-claim partition, not the survey rubric."""
    body = (
        "The lira fell three percent today [1].\n"
        "The central bank spent two billion dollars defending the peg [1].\n"
        "No evidence of capital-flight controls appears in the reviewed signals.\n"
    )
    citations = [{"marker": "[1]", "signal_id": sid, "title": "Lira drops 3% on the day"}]
    return body, citations


async def test_absence_judge_supported(monkeypatch):
    """A scoped absence over an evidence set lacking the thing → supported; the
    absence partition fired its dedicated rubric; headline stays high."""
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "1")
    body, citations = _mixed_body_one_absence(str(uuid4()))
    judge = _PartitionJudge(
        shared_json='{"verdicts": ["supported", "supported"]}',
        absence_json='{"verdicts": ["supported"]}',
    )
    rep = await verify_finding_faithfulness(body=body, citations=citations, judge_llm=judge)
    assert rep.judge_status == "llm"
    # Two partitions → two judge calls (one citation_support, one absence); the
    # M14 survey route did NOT fire (fact-rich finding).
    assert judge.shared_calls == 1
    assert judge.absence_calls == 1
    assert judge.survey_calls == 0
    assert "ABSENCE / NEGATIVE claims" in judge.last_absence_system
    assert rep.faithfulness_score == pytest.approx(1.0)
    assert not any(s.reason.startswith("judge_") for s in rep.unsupported_spans)
    # The absence sub-score is recorded (never hidden).
    assert rep.branch_scores[CLAIM_KIND_ABSENCE]["supported"] == 1
    assert rep.branch_scores[CLAIM_KIND_ABSENCE]["score"] == pytest.approx(1.0)


async def test_absence_judge_contradicted(monkeypatch):
    """'no strikes reported' with a cited strike → the absence judge returns
    'contradicted' → a ``judge_contradicted`` span (the existing highest-severity
    machine) + the headline is demoted."""
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "1")
    sid = str(uuid4())
    # Two positive cited claims keep the finding fact-rich (M14 off).
    body = (
        "Airbase activity intensified overnight [1].\n"
        "Ground units massed near the perimeter fence overnight [1].\n"
        "No strikes were reported in the monitored area.\n"
    )
    citations = [
        {"marker": "[1]", "signal_id": sid, "title": "Missile strike hits the airbase, casualties reported"}
    ]
    judge = _PartitionJudge(
        shared_json='{"verdicts": ["supported", "supported"], "quotes": ["", ""]}',
        # V-D: the contradiction QUOTES the cited evidence verbatim, so it EARNS
        # the hard class (an unquotable contradiction demotes — see
        # test_verify_hardfail_quote.py).
        absence_json=(
            '{"verdicts": ["contradicted"], "quotes": '
            '["Missile strike hits the airbase, casualties reported"]}'
        ),
    )
    rep = await verify_finding_faithfulness(body=body, citations=citations, judge_llm=judge)
    assert rep.judge_status == "llm"
    assert judge.survey_calls == 0
    assert any(s.reason == "judge_contradicted" for s in rep.unsupported_spans)
    # 3 claims graded, 2 supported → headline demoted below 1.0.
    assert rep.faithfulness_score == pytest.approx(2 / 3)
    assert rep.branch_scores[CLAIM_KIND_ABSENCE]["supported"] == 0


async def test_absence_judge_unbounded_unsupported(monkeypatch):
    """An UNBOUNDED / unscoped absence over a small scoped evidence set →
    'unsupported' (a claim the searched evidence cannot possibly establish).

    NOTE: the absence claim must carry an ``_ABSENCE_MARKERS`` phrase to REACH the
    absence branch (the deterministic classifier routes on the calibrated lexical
    set; a bare 'nothing is happening anywhere' with no marker classifies
    ``citation_support`` — a scope limitation of the reused set, design §5.1
    risk 2). Here the UNBOUNDED scope ('anywhere in the world') is what the
    absence JUDGE flags as unsupported, not the marker itself."""
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "1")
    sid = str(uuid4())
    # Two positive cited claims keep the finding fact-rich (M14 off).
    body = (
        "The summit concluded without a joint statement [1].\n"
        "Delegations departed within an hour of the closing session [1].\n"
        "No evidence of any threat exists anywhere in the world.\n"
    )
    citations = [{"marker": "[1]", "signal_id": sid, "title": "Summit ends, no joint statement"}]
    judge = _PartitionJudge(
        shared_json='{"verdicts": ["supported", "supported"]}',
        absence_json='{"verdicts": ["unsupported"]}',
    )
    rep = await verify_finding_faithfulness(body=body, citations=citations, judge_llm=judge)
    assert rep.judge_status == "llm"
    assert judge.absence_calls == 1  # the unbounded absence reached the absence branch
    assert any(s.reason == "judge_unsupported" for s in rep.unsupported_spans)
    assert rep.faithfulness_score == pytest.approx(2 / 3)


async def test_absence_variance_regression(monkeypatch):
    """The SAME absence prose graded across repeated runs yields the SAME verdict
    — the anti-0.0/0.2/1.0 guard. A deterministic classifier + a fixed (mocked)
    rubric response removes the run-to-run latitude."""
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "1")
    body, citations = _mixed_body_one_absence(str(uuid4()))
    scores = []
    for _ in range(5):
        judge = _PartitionJudge(
            shared_json='{"verdicts": ["supported", "supported"]}',
            absence_json='{"verdicts": ["supported"]}',
        )
        rep = await verify_finding_faithfulness(
            body=body, citations=citations, judge_llm=judge
        )
        scores.append(rep.faithfulness_score)
    # No spread — every run identical (contrast the historical 0.0/0.2/1.0).
    assert len(set(scores)) == 1
    assert scores[0] == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# 3. Telemetry — versioned branch stamp (design §2.2 / §2.3)
# ---------------------------------------------------------------------------


async def test_absence_branch_version_stamped(monkeypatch):
    """The critique payload carries ``branch_versions.absence == 'absence.v3'``
    (and the citation_support profile version), so a recalibration is a visible,
    greppable per-kind version bump."""
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "1")
    body, citations = _mixed_body_one_absence(str(uuid4()))
    judge = _PartitionJudge(
        shared_json='{"verdicts": ["supported", "supported"]}',
        absence_json='{"verdicts": ["supported"]}',
    )
    rep = await verify_finding_faithfulness(body=body, citations=citations, judge_llm=judge)
    payload = build_faithfulness_critique_payload(rep, analyzed_output_id=uuid4())
    verification = payload["data"]["verification"]
    assert verification["branch_versions"]["absence"] == "absence.v3"
    assert verification["branch_versions"]["citation_support"] == "citsupp.v5"
    # branch_scores surfaces both kinds' sub-ratios (never an opaque single number).
    assert verification["branch_scores"]["absence"]["checkable"] == 1
    assert verification["branch_scores"]["citation_support"]["checkable"] == 2


async def test_branch_telemetry_empty_on_judge_off(monkeypatch):
    """On the deterministic (judge-off) path, ``branch_scores`` /
    ``branch_versions`` are empty — a non-judge run + a pre-V3 reader are
    byte-identical."""
    monkeypatch.delenv("LEGBA_VERIFY_LLM_JUDGE", raising=False)
    body, citations = _mixed_body_one_absence(str(uuid4()))
    rep = await verify_finding_faithfulness(body=body, citations=citations)
    assert rep.judge_status == "deterministic"
    assert rep.branch_scores == {}
    payload = build_faithfulness_critique_payload(rep, analyzed_output_id=uuid4())
    verification = payload["data"]["verification"]
    assert verification["branch_scores"] == {}
    assert verification["branch_versions"] == {}


# ---------------------------------------------------------------------------
# 4. Invariants — non-absence findings are UNAFFECTED (the regression guard)
# ---------------------------------------------------------------------------


async def test_headline_arithmetic_unchanged_when_no_absence(monkeypatch):
    """A finding with ZERO absence spans makes EXACTLY ONE judge call with the
    shared prompt (no absence partition), and the pooled headline is the plain
    supported/checkable — the pre-V3 path, byte-identical (design §2.3 point 3)."""
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "1")
    sid = str(uuid4())
    body = (
        "The central bank raised rates by fifty basis points [1].\n"
        "Reserves fell for a third straight month [1].\n"
    )
    citations = [{"marker": "[1]", "signal_id": sid, "title": "Central bank hikes rates 50bps"}]
    judge = _PartitionJudge(
        shared_json='{"verdicts": ["supported", "supported"]}',
        absence_json='{"verdicts": []}',
    )
    rep = await verify_finding_faithfulness(body=body, citations=citations, judge_llm=judge)
    assert rep.judge_status == "llm"
    # No absence partition → no absence call; one shared call, exactly as pre-V3.
    assert judge.absence_calls == 0
    assert judge.shared_calls == 1
    assert judge.calls == 1
    assert rep.faithfulness_score == pytest.approx(1.0)
    assert rep.supported_claims == 2
    # branch_scores records ONLY citation_support (no absence key when none exist).
    assert set(rep.branch_scores) == {CLAIM_KIND_CITATION_SUPPORT}
    assert CLAIM_KIND_ABSENCE not in rep.branch_scores


async def test_non_absence_contradiction_still_works(monkeypatch):
    """A contradicted NON-absence (citation_support) claim still routes through
    the shared prompt and produces a ``judge_contradicted`` span — the branch
    change did not alter the existing citation_support behaviour."""
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "1")
    sid = str(uuid4())
    body = (
        "The central bank raised rates by fifty basis points [1].\n"
        "Reserves rose sharply on the month [1].\n"
    )
    citations = [{"marker": "[1]", "signal_id": sid, "title": "Reserves fell for a third month"}]
    judge = _PartitionJudge(
        # V-D: the contradiction quotes the cited evidence verbatim → hard class.
        shared_json=(
            '{"verdicts": ["supported", "contradicted"], '
            '"quotes": ["", "Reserves fell for a third month"]}'
        ),
        absence_json='{"verdicts": []}',
    )
    rep = await verify_finding_faithfulness(body=body, citations=citations, judge_llm=judge)
    assert judge.absence_calls == 0
    assert any(s.reason == "judge_contradicted" for s in rep.unsupported_spans)
    assert rep.faithfulness_score == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# 5. M14 coexistence — the whole-finding null still owns the survey rubric
# ---------------------------------------------------------------------------


async def test_null_result_still_uses_survey(monkeypatch):
    """A WHOLE-FINDING null (≤1 positive claim) still routes to the M14 survey
    rubric — ONE call over the whole claim list, NOT the per-claim absence
    branch (design §3.5: 'the whole-finding M14 survey path is retained as-is').
    The absence branch owns only the embedded-absence-in-a-fact-rich case."""
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "1")
    body = (
        "No coordinated military activity was observed across the monitored theater.\n"
        "The 51 signals focus on floods, sports fixtures, and trade logistics.\n"
    )
    judge = _PartitionJudge(
        survey_json='{"verdicts": ["supported", "supported"]}',
        shared_json='{"verdicts": []}',
        absence_json='{"verdicts": []}',
    )
    rep = await verify_finding_faithfulness(body=body, citations=[], judge_llm=judge)
    assert rep.judge_status == "llm"
    # The M14 survey route fired ONCE over the whole claim list; the V3 absence
    # partition did NOT fire despite the absence-marker opener.
    assert judge.survey_calls == 1
    assert judge.absence_calls == 0
    assert judge.shared_calls == 0
    assert judge.calls == 1
    assert rep.faithfulness_score == pytest.approx(1.0)
    # One rubric graded everything → no per-branch attribution is recorded
    # (branch telemetry on the M14 path would be fabricated).
    assert rep.branch_scores == {}
    payload = build_faithfulness_critique_payload(rep, analyzed_output_id=uuid4())
    assert payload["data"]["verification"]["branch_scores"] == {}
    assert payload["data"]["verification"]["branch_versions"] == {}
