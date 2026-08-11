# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""R2 + R3 — the two computed input checks (R-train, 2026-08-05).

R2: a desk published "the Strait of Hormuz remains effectively shut" and, 79
minutes later, "no concrete closure is in place". Both verified. Both entered the
same composition. The composition agreed with both.

R3: ``_build_salience_check`` catches a buried lead and has always been advisory,
filed in ``data.eval`` where nothing read it — including on the composition that
ranked 250 howitzers above a war.

These tests pin the detection, the fold arithmetic, and the two behaviours that
keep the checks honest: an unmeasurable verdict is never scored as a failure, and
a composition that DOES surface a contradiction pays nothing for it.
"""
from __future__ import annotations

from legba.data.analysts.claim_contradiction import (
    ClaimContradiction,
    detect_contradictions,
    render_tension_block,
)
from legba.data.provenance.verify import (
    BURIED_LEAD_SALIENCE,
    UNSURFACED_CONTRADICTION,
    fail_class_for_reason,
    verify_finding_faithfulness,
)

_CITATIONS = [
    {"marker": f"[[ref:{n}]]", "ref": f"r{n}", "evidence_text": "e"}
    for n in (1, 3, 7)
]


# ---------------------------------------------------------------------------
# R2 — the detector
# ---------------------------------------------------------------------------


def test_detects_the_live_hormuz_pair():
    """THE case. Same subject, opposite sides of the closure opposition, one of
    them expressed as a NEGATION of the other's term."""
    found = detect_contradictions(
        {
            3: ["The Strait of Hormuz remains effectively shut to commercial "
                "transit [1]."],
            7: ["No concrete closure of the Strait of Hormuz is in place, and "
                "traffic continues [4]."],
        }
    )
    assert len(found) == 1
    pair = found[0]
    assert pair.group == "closure"
    assert {"strait", "hormuz"} <= set(pair.subject)
    assert {pair.a_ref, pair.b_ref} == {3, 7}
    # One side asserts the state, the other denies it.
    assert pair.a_sign == -pair.b_sign


def test_unrelated_subjects_never_pair():
    found = detect_contradictions(
        {
            3: ["The Strait of Hormuz remains shut [1]."],
            7: ["The port of Rotterdam is fully open [2]."],
        }
    )
    assert found == []


def test_agreement_never_pairs():
    found = detect_contradictions(
        {
            3: ["The Strait of Hormuz remains shut [1]."],
            7: ["The Strait of Hormuz is closed to tanker traffic [2]."],
        }
    )
    assert found == []


def test_a_finding_never_contradicts_itself():
    """Internal hedging is the unit's own verify problem, not a desk disagreement
    — surfacing it as one would put a single analyst's caution into a composition
    as though two desks were at odds."""
    found = detect_contradictions(
        {
            3: [
                "The Strait of Hormuz remains shut [1].",
                "No closure of the Strait of Hormuz has been confirmed [2].",
            ]
        }
    )
    assert found == []


def test_mixed_polarity_in_one_claim_is_dropped_not_guessed():
    """A sentence taking BOTH sides of one opposition is ambiguous; forcing a sign
    would manufacture contradictions out of ordinary prose."""
    found = detect_contradictions(
        {
            3: ["The Hormuz strait reopened but the Bab el-Mandeb strait stays "
                "shut and closed [1]."],
            7: ["No closure of the Hormuz strait is in place [2]."],
        }
    )
    assert found == []


def test_detector_is_total_on_junk():
    assert detect_contradictions({}) == []
    assert detect_contradictions({"x": ["a"], 3: [None, "", 5]}) == []  # type: ignore[list-item]


# ---------------------------------------------------------------------------
# PRECISION — the calibration, pinned as cases
# ---------------------------------------------------------------------------
# Swept against the live corpus during the build: 32 country desks, 264 input
# refs, 1,592 verified claims. The first cut produced 57 pairs across 24 of 32
# desks and almost every one was false. The shipped cut produces ZERO. Each case
# below is a real pair from that sweep, reduced to the shape that caused it —
# because a detector wired into a composition's input as a stated fact and
# counted as a verify failure must not invent disagreement, and the way that
# guarantee rots is one plausible-looking vocabulary addition at a time.


def test_two_domains_on_one_desk_do_not_contradict():
    """country_g20_id: a stability read and an energy read shared exactly
    ``{"bluf", "indonesia"}`` — one markup token and one country name."""
    assert detect_contradictions(
        {
            3: ["**BLUF:** Indonesia's internal political stability remains "
                "steady with a low coup-risk [1]."],
            6: ["**BLUF:** Indonesia's energy-security pressure has risen to "
                "moderate as recent power outages signal supply disruptions [2]."],
        }
    ) == []


def test_a_balanced_clause_is_not_a_position():
    """country_watch_tw: "neither escalating further nor de-escalating" against a
    steady-tension BLUF. Both say the same thing; the first says it by naming
    both sides at once, which is not taking either."""
    assert detect_contradictions(
        {
            3: ["The most plausible near-term trajectory is steady tension — the "
                "situation is neither escalating further nor de-escalating [1]."],
            7: ["**BLUF:** Taiwan's live-fire exercise keeps near-term escalation "
                "risk at a steady-tension level [2]."],
        }
    ) == []


def test_an_enumerating_sentence_is_not_one_proposition():
    """country_watch_il: a BLUF listing five domains at once was read as an
    assertion about every state it named, so any claim mentioning any of them
    'contradicted' it."""
    assert detect_contradictions(
        {
            1: ["**BLUF:** Israel faces low energy-security pressure, with no "
                "supply disruptions, price shocks, infrastructure attacks, "
                "import/export shifts, or sanctions reported in this window, and "
                "no economic coercion observed against or by Israel, leaving the "
                "prior assessment unchanged across every tracked domain [1]."],
            4: ["Israeli forces conducted strikes in Gaza overnight [2]."],
        }
    ) == []


def test_the_desks_own_country_is_not_a_subject():
    """Every claim on a country desk names that country, so it identifies
    nothing. See ``_PROPER_NOUN_IS_SUFFICIENT`` for the measurement."""
    assert detect_contradictions(
        {
            3: ["Near-term escalation risk for France is rising, driven by "
                "escalating border friction [1]."],
            7: ["Domestically the political environment in France remains calm "
                "[2]."],
        }
    ) == []


def test_tension_block_names_both_handles_and_which_side_is_which():
    block = render_tension_block(
        detect_contradictions(
            {
                3: ["The Strait of Hormuz remains effectively shut [1]."],
                7: ["No closure of the Strait of Hormuz is in place [4]."],
            }
        )
    )
    assert "[[tension:1]]" in block
    assert "[[ref:3]] ASSERTS it" in block
    assert "[[ref:7]] DENIES it" in block
    assert "## Tension" in block or "'## Tension'" in block
    assert render_tension_block([]) == ""


# ---------------------------------------------------------------------------
# R2 — the verify-side fold
# ---------------------------------------------------------------------------


_PAIR = {
    "a_ref": 3,
    "b_ref": 7,
    "group": "closure",
    "subject": ["hormuz", "strait"],
}


async def test_unsurfaced_contradiction_is_a_counted_soft_failure():
    report = await verify_finding_faithfulness(
        body="BLUF: The strait is open and traffic is normal [[ref:3]].\n",
        citations=_CITATIONS,
        eval_block={"contradictions": [_PAIR]},
    )
    assert report.counters.get("input_contradiction_unsurfaced") == 1
    reasons = [s.reason for s in report.unsupported_spans]
    assert UNSURFACED_CONTRADICTION in reasons
    assert fail_class_for_reason(UNSURFACED_CONTRADICTION) == "soft_fail"
    # The ledger row exists and names both handles.
    row = next(
        v for v in report.claim_verdicts if v.reason == UNSURFACED_CONTRADICTION
    )
    assert "[[ref:3]]" in row.text and "[[ref:7]]" in row.text


async def test_a_composition_that_surfaces_the_tension_pays_nothing():
    """The check wants this behaviour — charging for it would punish the fix."""
    body = (
        "BLUF: The picture is mixed [[ref:3]].\n\n"
        "## Tension\nThe two reads contradict each other: [[ref:3]] reports the "
        "strait shut while [[ref:7]] reports no closure; [[ref:7]] is better "
        "sourced.\n"
    )
    report = await verify_finding_faithfulness(
        body=body, citations=_CITATIONS, eval_block={"contradictions": [_PAIR]},
    )
    assert report.counters.get("input_contradiction_surfaced") == 1
    assert "input_contradiction_unsurfaced" not in report.counters
    assert UNSURFACED_CONTRADICTION not in [
        s.reason for s in report.unsupported_spans
    ]


async def test_citing_both_refs_without_disagreeing_still_fails():
    """The live failure composed both sides into AGREEMENT while citing both — so
    handle presence alone cannot be the test."""
    body = (
        "BLUF: Both reads agree the situation is stable [[ref:3]] [[ref:7]].\n"
    )
    report = await verify_finding_faithfulness(
        body=body, citations=_CITATIONS, eval_block={"contradictions": [_PAIR]},
    )
    assert report.counters.get("input_contradiction_unsurfaced") == 1


async def test_no_eval_block_is_inert():
    body = "BLUF: The strait is open [[ref:3]].\n"
    with_none = await verify_finding_faithfulness(body=body, citations=_CITATIONS)
    with_empty = await verify_finding_faithfulness(
        body=body, citations=_CITATIONS, eval_block={}
    )
    assert with_none.checkable_claims == with_empty.checkable_claims
    assert with_none.faithfulness_score == with_empty.faithfulness_score
    assert "input_contradiction_unsurfaced" not in with_none.counters


# ---------------------------------------------------------------------------
# R3 — the salience lead, promoted from advisory to counted
# ---------------------------------------------------------------------------


async def test_buried_lead_is_a_counted_soft_failure():
    report = await verify_finding_faithfulness(
        body="BLUF: A routine procurement was announced [[ref:3]].\n",
        citations=_CITATIONS,
        eval_block={
            "salience_check": {
                "pass": False,
                "lead_ref": 3,
                "lead_magnitude": 0.2,
                "top_magnitude": 0.9,
                "gap": 0.7,
                "top_title": "Sudan: sustained kinetic escalation",
                "reason": "possible burial",
            }
        },
    )
    assert report.counters.get("salience_lead_buried") == 1
    assert BURIED_LEAD_SALIENCE in [s.reason for s in report.unsupported_spans]
    assert fail_class_for_reason(BURIED_LEAD_SALIENCE) == "soft_fail"
    # The span points at the LEAD CLAIM, not at an abstraction.
    span = next(
        s for s in report.unsupported_spans if s.reason == BURIED_LEAD_SALIENCE
    )
    assert "routine procurement" in span.text
    assert span.markers == [3]


async def test_a_passing_salience_check_costs_nothing():
    report = await verify_finding_faithfulness(
        body="BLUF: The war escalated [[ref:3]].\n",
        citations=_CITATIONS,
        eval_block={"salience_check": {"pass": True, "lead_ref": 3, "gap": 0.0}},
    )
    assert "salience_lead_buried" not in report.counters


async def test_an_unjudgeable_salience_check_is_never_scored_as_a_failure():
    """``pass is None`` is the check's own honest "not judgeable" (no resolvable
    lead citation, or an unscored lead). Scoring an unmeasurable thing as a miss
    is the exact mistake this train exists to remove."""
    report = await verify_finding_faithfulness(
        body="BLUF: Something happened [[ref:3]].\n",
        citations=_CITATIONS,
        eval_block={
            "salience_check": {
                "pass": None,
                "lead_ref": None,
                "reason": "no resolvable [[ref:N]] citation — the lead is not "
                          "judgeable",
            }
        },
    )
    assert "salience_lead_buried" not in report.counters
    assert BURIED_LEAD_SALIENCE not in [
        s.reason for s in report.unsupported_spans
    ]


async def test_both_checks_fold_independently_and_additively():
    report = await verify_finding_faithfulness(
        body="BLUF: A routine procurement was announced [[ref:3]].\n",
        citations=_CITATIONS,
        eval_block={
            "salience_check": {"pass": False, "lead_ref": 3, "gap": 0.7},
            "contradictions": [_PAIR],
        },
    )
    assert report.counters.get("salience_lead_buried") == 1
    assert report.counters.get("input_contradiction_unsurfaced") == 1
    reasons = [s.reason for s in report.unsupported_spans]
    assert BURIED_LEAD_SALIENCE in reasons
    assert UNSURFACED_CONTRADICTION in reasons
    # Each fold adds exactly ONE checkable claim and no supported ones — the same
    # arithmetic every other soft guard span uses.
    assert report.score_denominator == report.checkable_claims


def test_contradiction_dataclass_round_trips():
    c = ClaimContradiction(
        group="closure", subject=("hormuz",), a_ref=1, a_text="x", b_ref=2,
        b_text="y", a_sign=1, b_sign=-1, overlap=2,
    )
    d = c.as_dict()
    assert d["a_ref"] == 1 and d["b_ref"] == 2 and d["group"] == "closure"
    assert d["subject"] == ["hormuz"]
