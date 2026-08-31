# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""V-J (2026-08-28) — the HEDGED-CONFLICT defect, pinned by case, both ways.

Source: ``planning/HARDFAIL_STEPCHANGE_CHECK_2026-08-27.md``, a read-only
adjudication of 13 of the 24 live ``absence_slice_contradicted`` hard fails on
the ``2026-08-25/1`` stamp. Its tally was 5 true catches, 5 clear over-fires and
3 borderline, and its §5 named the one template that recurred:

  *"a weakly-supported / unverified read says NO-X, which conflicts with the
  verified finding of X; the former is below the verification floor"* — hard
  failed by a row resolving back to **the same weak side the sentence had
  already named, already cited and already rejected**.

Three instances in the sample (two on ``country_g20_fr``'s composition, one on
``country_g20_mx``), plus a fourth found unprompted on the GENERIC
``judge_contradicted`` route (Mali / Africa Corps) — which is why the fix has
two levers rather than one, and why the generic-route specimen is the load-
bearing test here: it carries no scope qualifier, so V-I5's gate never sees it.

EVERY TEST IS EITHER a case from that document or a guard proving the fix does
not swallow one of its FIVE CONFIRMED GENUINE CATCHES. Both halves are the
regression: a guard that only demonstrates suppression measures nothing.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

import pytest

import legba.data.provenance.verify as V
from legba.data.provenance.absence_slice import (
    _absence_route_exclusion,
    absence_scope_qualifier,
    hedged_conflict_disclosure,
)
from legba.data.provenance.judge_quote_rules import claim_is_routed_out
from legba.data.provenance.verify import (
    FAIL_CLASS_HARD,
    FAIL_CLASS_SOFT,
    verify_finding_faithfulness,
)

# ---------------------------------------------------------------------------
# THE THREE HEDGED CASES (§4 rows 8-9 + §5's generic-route specimen)
# ---------------------------------------------------------------------------

#: §4 row 8, instance 1 — ``country_g20_fr`` country_composition.
FR_NARRATIVE = (
    "The narrative-coordination desk's verified find of a coordinated "
    "pro-government story conflicts with a weakly-supported report claiming no "
    "coordinated narrative across French outlets [[ref:3]]."
)
#: §4 row 8, instance 2 — the paired France finding, same template.
FR_AMPLIFICATION = (
    "The narrative-coordination desk's verified finding of a coordinated "
    "amplification campaign conflicts with an unverified report claiming no "
    "coordinated amplification was observed [[ref:5]]."
)
#: §4 row 9 — ``country_g20_mx`` country_composition, the fullest specimen: it
#: names the weak pole, the strong pole, the conflict AND the reason for the
#: preference ("below the verification floor").
MX_POSTURE = (
    "A weakly-supported report indicates no new evidence of a shift in Mexico's "
    "military posture, which conflicts with the verified finding of an "
    "advancing UAV procurement pact; the former is below the verification floor "
    "and therefore less reliable [[ref:2]]."
)
#: §5's GENERIC-route specimen (sampled separately, unprompted). No scope
#: qualifier — the reason V-I5 cannot reach it and the guard has its own rule.
ML_AFRICA_CORPS = (
    "A weakly-supported read indicates no observable posture shift in Mali, "
    "which conflicts with the verified report of continued reliance on Russian "
    "Africa Corps forces [1]."
)

HEDGED_CASES = (FR_NARRATIVE, FR_AMPLIFICATION, MX_POSTURE, ML_AFRICA_CORPS)

# ---------------------------------------------------------------------------
# THE FIVE CONFIRMED GENUINE CATCHES (§4 rows 1-5) — these MUST keep failing.
# ---------------------------------------------------------------------------

CA_TARIFFS = (
    "No new escalation or relief in Canada's economic-coercion posture was "
    "observed in this 72h window [1]."
)
IL_UNRWA = (
    "No new deployment steps were added; Israel's standing posture remains as "
    "assessed in the reviewed signal set [1]."
)
RED_SEA = (
    "No new incidents have disrupted port operations or forced rerouting in the "
    "Red Sea lane during the collection window [1]."
)
GB_STRIKE = (
    "No new mass-protest, strike, or security-force defection reports appear in "
    "the collected reporting for the United Kingdom [1]."
)
IT_NARRATIVE = (
    "No economic coercion measures were detected, and media analysis found no "
    "coordinated narrative across Italian outlets [1]."
)

GENUINE_CATCHES = (CA_TARIFFS, IL_UNRWA, RED_SEA, GB_STRIKE, IT_NARRATIVE)


class _Response:
    def __init__(self, content: str) -> None:
        self.content = content
        self.usage = None


class _StubJudge:
    subprovider = "stub-judge"

    def __init__(self, payload: dict[str, Any]) -> None:
        self._json = json.dumps(payload)

    async def chat_complete(
        self, messages, *, max_tokens=None, temperature=None, system=None, **kw
    ):
        return _Response(self._json)


def _signal_citations(*titles: str) -> list[dict[str, Any]]:
    return [
        {"marker": f"[{i}]", "signal_id": str(uuid4()), "title": t}
        for i, t in enumerate(titles, start=1)
    ]


@pytest.fixture(autouse=True)
def _judge_on(monkeypatch):
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "1")


# ---------------------------------------------------------------------------
# 1. The DETERMINISTIC arm — the shape, and what it names
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("claim", HEDGED_CASES)
def test_every_census_hedged_case_is_recognised(claim: str) -> None:
    """All three §4/§5 template instances plus the generic-route fourth."""
    assert hedged_conflict_disclosure(claim) is not None


@pytest.mark.parametrize("claim", HEDGED_CASES)
def test_the_detail_names_both_poles_verbatim(claim: str) -> None:
    """V-D's earned rule: a verdict must point at the thing it decides on. The
    detail carries the weak pole, the strong pole and the connective, all lifted
    from the claim — so nobody has to re-derive the call from claim text."""
    detail = hedged_conflict_disclosure(claim)
    assert detail is not None
    assert "NAMES BOTH POLES" in detail
    assert "conflicts with" in detail
    # The strong pole is quoted as the FINDING it is, not as a bare adjective.
    assert "verified find" in detail or "verified report" in detail


@pytest.mark.parametrize("claim", GENUINE_CATCHES)
def test_the_five_genuine_catches_are_untouched_by_the_guard(claim: str) -> None:
    """The pass-side half of the regression. Not one of the census's confirmed
    catches carries a weakness marker OR a conflict connective, so none is
    reachable here — and the guard must be able to say so case by case."""
    assert hedged_conflict_disclosure(claim) is None
    assert _absence_route_exclusion(claim) is None


@pytest.mark.parametrize(
    "claim",
    [
        # The weakness marker downweights the OTHER side and the negative is
        # this sentence's own assertion — across a ';' and a conflict connective.
        "Unverified social-media claims of a coup attempt conflict with the "
        "verified report from state media; no new security-force defections "
        "were observed in the window [3].",
        # A conflict connective and a strong finding, but nothing marked weak.
        "The desk's reviewed sources show no new large-scale protests, which "
        "conflicts with the verified finding of rising unrest [8].",
        # A weakness marker before the negative, but no disagreement is relayed.
        "An unverified single-source report and no new confirmed strikes appear "
        "in the collected reporting [2].",
        # "confirmed reports" is a claim about the WORLD, not a named strong
        # POLE — the finding-noun requirement is what keeps it out.
        "A weakly-supported tip contradicts nothing: no confirmed reports of "
        "new sanctions designations appear in the reviewed corpus [4].",
        # The only "strong pole" present sits INSIDE the weak side's own clause,
        # so there is one side here, not two. All four census specimens name the
        # strong pole either before the weakness marker or after the negative.
        "An unverified report claiming no confirmed findings of coordination "
        "contradicts the desk's own summary [6].",
    ],
)
def test_the_three_conditions_are_conjunctive(claim: str) -> None:
    """Each of these drops exactly one condition and must stay on its route."""
    assert hedged_conflict_disclosure(claim) is None


# ---------------------------------------------------------------------------
# 2. LEVER ONE — the V-B route (``absence_slice_contradicted`` never happens)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("claim", [FR_NARRATIVE, FR_AMPLIFICATION, MX_POSTURE])
def test_the_composition_cases_leave_the_v_b_route(claim: str) -> None:
    """All three carry a scope qualifier ("coordinated" / "new"), so absent the
    guard they would be screened against the slice and stage-2 adjudicated."""
    assert absence_scope_qualifier(claim) is not None
    assert _absence_route_exclusion(claim) == "hedged_conflict"


def test_the_generic_route_case_is_not_reachable_by_the_router() -> None:
    """THE reason the judge path needs its own rule rather than reusing V-I5:
    the §5 generic-route specimen carries no scope qualifier at all, so
    ``claim_is_routed_out`` — which mirrors the V-B fold's gate exactly —
    answers None on the very claim the judge hard-failed."""
    assert absence_scope_qualifier(ML_AFRICA_CORPS) is None
    assert claim_is_routed_out(ML_AFRICA_CORPS) is None
    assert hedged_conflict_disclosure(ML_AFRICA_CORPS) is not None


async def test_a_hedged_composition_claim_never_reaches_stage_two() -> None:
    """End to end on the V-B path: the route exclusion is counted per class (the
    V-G2 receipts rule) and no slice judge call is spent."""
    from tests.data_pkg.test_verify_absence_slice_precision import (
        _SliceConn,
        _StubJudge as _SliceStubJudge,
        _signal,
    )

    judge = _SliceStubJudge(
        slice_payload={"verdicts": ["contradicted"], "quotes": ["x"]}
    )
    report = await verify_finding_faithfulness(
        body=f"- {MX_POSTURE}\n",
        citations=_signal_citations("Mexico UAV procurement pact advances"),
        judge_llm=judge,
        target_id="country_g20_mx",
        slice_conn=_SliceConn(
            [_signal("Mexico signs UAV procurement pact with supplier")]
        ),
        run_id=uuid4(),
    )
    assert judge.slice_calls == 0
    assert report.counters.get("absence_slice_route_excluded_hedged_conflict") == 1
    assert not any(
        cv.reason == "absence_slice_contradicted" for cv in report.claim_verdicts
    )


# ---------------------------------------------------------------------------
# 3. LEVER TWO — the judge severity chain (the generic route)
# ---------------------------------------------------------------------------


async def test_a_hedged_claim_cannot_be_hard_failed_by_the_judge() -> None:
    """§5's cross-route specimen: the judge hard-failed the Mali sentence with a
    span that IS the weak read the sentence disclosed. The claim still FAILS —
    only the severity moves, as with every other demotion in the chain."""
    wire = "Analysts report no observable posture shift in Bamako this week."
    report = await verify_finding_faithfulness(
        body=f"- {ML_AFRICA_CORPS}\n",
        citations=_signal_citations("Mali posture roundup", wire),
        judge_llm=_StubJudge({"verdicts": ["contradicted"], "quotes": [wire]}),
    )
    cv = next(cv for cv in report.claim_verdicts if "posture shift" in cv.text)
    assert cv.verdict == FAIL_CLASS_SOFT
    assert cv.reason == "judge_contradicted_hedged_conflict"
    assert report.counters["hardfail_demoted_hedged_conflict"] == 1
    # The persisted detail names both poles AND keeps the judge's own span, so
    # the demotion is auditable from the ledger row alone.
    assert cv.detail and "NAMES BOTH POLES" in cv.detail
    assert "verified report" in cv.detail
    assert wire in cv.detail


async def test_the_demotion_is_its_own_class_not_v_i5s() -> None:
    """Two mechanisms, two counters (the V-G8 fidelity rule). Pooling this into
    ``judge_contradicted_route_excluded`` would make it impossible to say which
    of the two fixes moved the hard-fail share."""
    wire = "Analysts report no observable posture shift in Bamako this week."
    report = await verify_finding_faithfulness(
        body=f"- {ML_AFRICA_CORPS}\n",
        citations=_signal_citations("Mali posture roundup", wire),
        judge_llm=_StubJudge({"verdicts": ["contradicted"], "quotes": [wire]}),
    )
    assert "hardfail_demoted_route_excluded" not in report.counters
    assert V.fail_class_for_reason("judge_contradicted_hedged_conflict") == (
        FAIL_CLASS_SOFT
    )


# ---------------------------------------------------------------------------
# 3b. V-J1 ACTIVATION (2026-08-29, LRF stamp) — THE HYPHEN THAT KEPT THE GUARD
#     SWITCHED OFF FOR ITS ENTIRE LIFE.
#
# V-J1 shipped 2026-08-28 and fired ZERO times in production: 0 of 573 graded
# claims on the stamp carried ``hardfail_demoted_hedged_conflict`` or
# ``absence_slice_route_excluded_hedged_conflict``. Every test above passed.
#
# The two facts are consistent because the fixtures above are hand-typed with
# ASCII hyphens and the producers are not: 41.3% of live segmented spans (58.2%
# of GRADED claims, R3's census) carry U+2011 NON-BREAKING HYPHEN, and
# ``_HEDGED_WEAK_MARKER_RE`` is spelled ``weakly[-\s]+supported``. The module's
# own ``_UNICODE_HYPHENS`` fold existed the whole time, filed 430 lines away as a
# local helper of the enumeration screen, and ``hedged_conflict_disclosure``
# never called it.
#
# THE LESSON, which is why these tests exist rather than a one-line diff: a
# regression suite written in the same keyboard's characters cannot detect a
# defect that lives in the producer's characters. The specimens below are
# R3's ARCHIVED LIVE TEXT (planning/PROOF_ROUND_2026-08-29/mech/
# hedged_conflict_livefire.json), copied byte-for-byte, U+2011 included.
# ---------------------------------------------------------------------------

#: Archived live composition prose — U+2011 throughout. R3 replayed all seven and
#: recorded 0/7 firing as shipped, 4/7 with the fold applied.
U2011_FIRES = (
    "A weakly‑supported read from the military‑posture desk claims no "
    "new shift in posture [[ref:8]], which conflicts with the verified "
    "assessment of a modest rise in mobilisation readiness [[ref:1]].",
    "A weakly‑supported read indicates no observable posture shift "
    "[[ref:8]], which conflicts with the verified report of continued reliance "
    "on Russian Africa Corps forces [[ref:1]].",
    "A weakly‑supported read (ref 8) claims no observable shift in Mali’s "
    "military posture, conflicting with the verified finding of growing reliance "
    "on Russian Africa Corps forces (ref 1).",
    "A weakly‑supported report indicates no new evidence of a shift in "
    "Mexico’s military posture【[[ref:8]]】, which conflicts with the "
    "verified finding of an advancing UAV procurement pact【[[ref:1]]】; "
    "the former is below the verification floor and therefore less reliable.",
)

#: The three archived specimens that must STAY unrecognised even with the fold —
#: the guard is three CONJUNCTIVE conditions, not a hyphen test. (1) names no
#: strong-side FINDING NOUN; (2) separates the poles with "whereas", a clause
#: break; (3) says the two reads AGREE.
U2011_DOES_NOT_FIRE = (
    "A weakly‑supported read below the verification floor asserts no "
    "observed shift in Burkina Faso’s standing military posture [[ref:8]], "
    "which conflicts with the verified modest rise reported in [[ref:1]].",
    "The narrative‑coordination desk’s verified assessment indicates a "
    "coordinated OAS‑focused narrative across outlets [[ref:6]], whereas a "
    "weakly‑supported read suggests no coordinated narrative is evident "
    "[[ref:11]]; the conflict between these views merits monitoring.",
    "The verified narrative‑coordination finding (ref4) aligns with the "
    "weakly‑supported signal on the same theme (ref7), indicating consistent "
    "reporting rather than conflict.",
)


@pytest.mark.parametrize("claim", U2011_FIRES)
def test_archived_u2011_specimens_are_recognised(claim: str) -> None:
    """The four R3 replayed as firing WITH the house fold — and the guard is why
    they were not firing without it."""
    assert "‑" in claim, "the specimen must carry the live hyphen"
    detail = hedged_conflict_disclosure(claim)
    assert detail is not None, claim
    assert "NAMES BOTH POLES" in detail


@pytest.mark.parametrize("claim", U2011_DOES_NOT_FIRE)
def test_archived_u2011_non_specimens_stay_unrecognised(claim: str) -> None:
    """Folding the hyphen must not turn the guard into a hyphen detector — the
    three conditions stay conjunctive on live text too."""
    assert hedged_conflict_disclosure(claim) is None, claim


async def test_a_u2011_hedged_claim_is_demoted_through_the_real_route() -> None:
    """THE activation proof, end to end: the archived Mali sentence in its LIVE
    spelling reaches the judge severity chain and comes out SOFT.

    Before the fold this claim was hard-failed by a span that IS the weak read it
    disclosed — the exact defect V-J1 was built for, on the exact text V-J1 could
    not see.
    """
    claim = U2011_FIRES[1]
    wire = "Analysts report no observable posture shift in Bamako this week."
    report = await verify_finding_faithfulness(
        body=f"- {claim}\n",
        citations=_signal_citations("Mali posture roundup", wire),
        judge_llm=_StubJudge({"verdicts": ["contradicted"], "quotes": [wire]}),
    )
    cv = next(cv for cv in report.claim_verdicts if "posture shift" in cv.text)
    assert cv.verdict == FAIL_CLASS_SOFT
    assert cv.reason == "judge_contradicted_hedged_conflict"
    assert report.counters["hardfail_demoted_hedged_conflict"] == 1


async def test_the_ascii_spelling_still_demotes_after_the_fold() -> None:
    """The pair to the test above. A 1:1 character map cannot change text that
    carries no mapped character, so the ASCII specimen must be byte-identical in
    behaviour — this is what says the fix ADDED a population rather than moved
    one."""
    wire = "Analysts report no observable posture shift in Bamako this week."
    report = await verify_finding_faithfulness(
        body=f"- {ML_AFRICA_CORPS}\n",
        citations=_signal_citations("Mali posture roundup", wire),
        judge_llm=_StubJudge({"verdicts": ["contradicted"], "quotes": [wire]}),
    )
    cv = next(cv for cv in report.claim_verdicts if "posture shift" in cv.text)
    assert cv.verdict == FAIL_CLASS_SOFT
    assert cv.reason == "judge_contradicted_hedged_conflict"
    assert report.counters["hardfail_demoted_hedged_conflict"] == 1


def test_the_fold_is_applied_where_the_guard_reads_the_claim() -> None:
    """The mechanism pin, not the symptom. ``hedged_conflict_disclosure`` must
    normalize through the module's OWN fold; asserting the behaviour alone would
    let a future edit re-introduce the omission behind a special case."""
    import inspect

    from legba.data.provenance import absence_slice as A

    src = inspect.getsource(A.hedged_conflict_disclosure)
    assert "translate(_UNICODE_HYPHENS)" in src
    # ...and the fold is a SHARED primitive, declared before its first use, not a
    # local helper of one screen 430 lines below the matchers that need it.
    module_src = inspect.getsource(A)
    assert module_src.index("_UNICODE_HYPHENS = ") < module_src.index(
        "def hedged_conflict_disclosure"
    )


#: The census's five TRUE CATCHES with the reporting that refuted each (§4 rows
#: 1-5). The wire text is the violating headline the document resolved.
CATCH_PAIRS = (
    (
        CA_TARIFFS,
        "Trump slams Canada with new 50 percent auto tariffs for 2027, the White "
        "House confirmed on Wednesday.",
    ),
    (
        IL_UNRWA,
        "Israel bolsters forces at seized UNRWA training center in occupied "
        "Jerusalem, officials said.",
    ),
    (
        RED_SEA,
        "Yemen's Houthis fire ballistic missiles at al-Markha; at least three "
        "missiles struck the port area at Mocha.",
    ),
    (
        GB_STRIKE,
        "UK Foreign Office employees have gone on strike over planned "
        "redundancies, the union said.",
    ),
    (
        IT_NARRATIVE,
        "A coordinated narrative has emerged across Italian media and foreign "
        "outlets over the past three days.",
    ),
)


@pytest.mark.parametrize(("claim", "wire"), CATCH_PAIRS)
async def test_the_five_genuine_catches_still_fail(claim: str, wire: str) -> None:
    """THE catch-side regression, end to end, on all five: each still FAILS and
    none of them leaves by this train's door. Both halves matter — a guard
    measured only on what it suppresses cannot see over-correction, which is the
    lesson RUST-2's suppression-only gate already paid for."""
    report = await verify_finding_faithfulness(
        body=f"- {claim}\n",
        citations=_signal_citations("Desk roundup", wire),
        judge_llm=_StubJudge({"verdicts": ["contradicted"], "quotes": [wire]}),
    )
    cv = report.claim_verdicts[0]
    assert cv.verdict in (FAIL_CLASS_HARD, FAIL_CLASS_SOFT)
    assert cv.reason != "judge_contradicted_hedged_conflict"
    assert "hardfail_demoted_hedged_conflict" not in report.counters


@pytest.mark.parametrize(
    ("claim", "wire"),
    [p for p in CATCH_PAIRS if p[0] in (IL_UNRWA, GB_STRIKE, IT_NARRATIVE)],
)
async def test_the_unenumerated_genuine_catches_keep_the_hard_class(
    claim: str, wire: str
) -> None:
    """§4 rows 2, 4 and 5 — the UNRWA reinforcement, the Foreign Office walkout
    and the composition contradicted by its own subsidiary desk. Each survives
    the WHOLE severity chain as HARD, which is the strongest form of the
    catch-side guard.

    Rows 1 and 3 are deliberately not here. Both state ENUMERATED denials in the
    census's own wording ("no new escalation or relief", "disrupted port
    operations or forced rerouting") and the reporting that refutes each names
    only one listed item, so V-H4 (2026-08-04) demotes them — a pre-existing rule
    about the QUOTE, four stamps older than this train and untouched by it. The
    test above is what pins them: they still fail, and not as hedged conflicts.
    """
    report = await verify_finding_faithfulness(
        body=f"- {claim}\n",
        citations=_signal_citations("Desk roundup", wire),
        judge_llm=_StubJudge({"verdicts": ["contradicted"], "quotes": [wire]}),
    )
    cv = report.claim_verdicts[0]
    assert cv.verdict == FAIL_CLASS_HARD, cv.reason
    assert cv.reason == "judge_contradicted"


# ---------------------------------------------------------------------------
# 4. V-J2 — the two OVER-FIRE families, as stage-2 rubric negatives
# ---------------------------------------------------------------------------


def test_the_stage_two_rubric_carries_the_two_domain_collision_negatives() -> None:
    """§4 rows 6 and 7 — the sanctions-type conflation (a Kalimantan forest-fire
    penalty read as trade coercion) and the civilian/military procurement
    conflation (an EPR power station read as a military programme). Neither is
    lexically decidable, so both ship as few-shot NEGATIVES worded from the
    cases rather than as a fence in code."""
    system = V._ABSENCE_SLICE_JUDGE_SYSTEM
    assert "DOMAIN COLLISION" in system
    assert "Kalimantan" in system
    assert "secondary sanctions" in system
    assert "Second EPR site authorised for site preparations" in system
    assert "CIVILIAN nuclear power station" in system
    # It is an OVERRIDE rule, listed with the other two, and the lead-in counts.
    assert "Three rules that override everything above" in system
    # The two pre-existing overrides are untouched.
    assert "CARVE-OUTS" in system
    assert "EPISTEMIC QUALIFIERS" in system
