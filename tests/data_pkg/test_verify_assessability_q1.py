# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Q-1 — the zero-claim faithfulness hole (R-train, 2026-08-05).

The regression set for the four halves of the fix:

  (a) labeled-scaffold bodies must still yield propositional claims;
  (b) zero / near-zero checkable claims must never publish a passing score;
  (c) a non-``llm`` verdict publishes PROVISIONAL, under a ceiling;
  (d) literal JSON syntax never reaches the verdict ledger as a claim.

The Italy body below is the real one — ``analyst_outputs`` row behind critique
``cd5e3413`` in the 08-04 adjudication annex §9, 1,546 characters, twelve
citations, and a live ``faithfulness_score = 1.0`` earned on **zero** graded
claims. If this file goes green on a build where the segmenter has regressed,
the fixture is doing nothing; it is written verbatim for that reason.
"""
from __future__ import annotations

from uuid import uuid4

from legba.data.provenance.judge_assessability import (
    MIN_ASSESSABLE_CLAIMS,
    PROVISIONAL_SCORE_CEILING,
    SCORE_STATE_SCORED,
    SCORE_STATE_UNASSESSABLE,
    UNASSESSABLE_GATE_SCORE,
    is_assessment_scaffold,
    is_coverage_statement,
    is_json_syntax_claim,
    is_labeled_scaffold,
    labeled_scaffold_label,
)
from legba.data.provenance.verify import (
    _claim_kind,
    _is_fact_asserting,
    _segment_claims,
    build_faithfulness_critique_payload,
    verify_finding_faithfulness,
)

# asyncio_mode = "auto" (pyproject) — async tests need no per-test marker, and a
# module-level one would mis-mark the synchronous shape tests in this file.

# The verbatim live body (critique cd5e5413 / finding 95f9478c), NFC as stored —
# note the U+2011 non-breaking hyphens the core plane emits inside labels.
ITALY_BODY = (
    "**BLUF:** Italy is currently under elevated energy‑security pressure due "
    "to concurrent heat‑wave driven demand spikes, seismic‑related "
    "infrastructure risk, and ongoing EU‑sanction enforcement actions that "
    "could disrupt Russian oil imports. [6][42][45][70][29][43][33][41][46][51]"
    "[55][60]\n\n"
    "**Key points**\n"
    "- **Heat‑wave alerts:** Red alerts have been issued for 25‑27 of "
    "Italy’s major cities, creating record electricity demand. [6][42][45][70]\n"
    "- **Seismic risk:** A magnitude‑4.7 earthquake near Campi Flegrei caused "
    "evacuations and property damage, raising the risk of power‑grid "
    "disruptions. [29][43]\n"
    "- **Sanctions‑related interdiction:** The Italian navy has boarded the "
    "sanctioned Russian “shadow‑fleet” tanker Toa Payoh multiple times "
    "in the Mediterranean, indicating heightened risk to Russian oil imports. "
    "[33][41][46][51][55][60]\n\n"
    "**Assessment**\n"
    "- **Severity:** elevated – multiple supply‑side disruptions and demand "
    "pressures are evident, though no nationwide blackout or price shock has been "
    "reported.\n"
    "- **Near‑term trajectory:** steady – the heatwave is expected to persist "
    "for at least a week and interdiction actions are ongoing, suggesting pressure "
    "will remain at the current elevated level unless a major outage or price shock "
    "materialises.\n\n"
    "**Indicators to watch**\n"
    "- Unplanned outage or damage at an LNG terminal serving Italy.\n"
)

ITALY_CITATIONS = [
    {"marker": f"[{n}]", "signal_id": f"sig-{n}", "title": "t", "snippet": "s"}
    for n in (6, 42, 45, 70, 29, 43, 33, 41, 46, 51, 55, 60)
]


# ---------------------------------------------------------------------------
# (a) SEGMENTATION — the labeled-scaffold exemption reads past the label
# ---------------------------------------------------------------------------


def test_short_value_stamps_are_still_scaffolding():
    """The shape #116(b) was written for is untouched."""
    for stamp in (
        "**Severity:** High",
        "**Confidence:** Moderate",
        "**Time horizon:** 3-6 months",
        "- **Severity**: low",
        "**Confidence:** moderate, trending down",
        "**Severity:** High [3]",  # markers are stripped before the value is sized
        "**Risk:** elevated",
    ):
        assert is_labeled_scaffold(stamp), stamp
        assert not _is_fact_asserting(stamp), stamp


def test_labeled_bullet_carrying_prose_is_a_claim():
    """THE DEFECT. A bold signpost introducing cited fact is not scaffolding."""
    claim = (
        "- **Heat-wave alerts:** Red alerts have been issued for 25-27 of Italy's "
        "major cities, creating record electricity demand. [6][42][45][70]"
    )
    assert not is_labeled_scaffold(claim)
    assert _is_fact_asserting(claim)
    assert _claim_kind(claim) == "citation_support"


def test_italy_body_yields_claims_and_no_longer_scores_one_on_nothing():
    """The whole-body regression: 0 claims -> a real, graded claim list."""
    claims = [c for c in _segment_claims(ITALY_BODY) if _is_fact_asserting(c)]
    # Live behaviour before the fix was exactly zero.
    assert len(claims) >= 3, claims
    # The three factual bullets are all present and all carry their markers.
    joined = " ".join(claims)
    assert "Red alerts have been issued" in joined
    assert "Campi Flegrei" in joined
    assert "Toa Payoh" in joined


def test_bluf_and_severity_stay_synthesis_not_uncited_facts():
    """The floor's synthesis exemption must cover the LABELED spelling too.

    Otherwise the fix trades a false 1.0 for a false ``no_citation`` — grading an
    analyst's own derived read as a missing citation.
    """
    bluf = "**BLUF:** Italy is currently under elevated pressure. [6]"
    sev = (
        "- **Severity:** elevated – multiple supply-side disruptions are "
        "evident, though no nationwide blackout has been reported."
    )
    traj = (
        "- **Near‑term trajectory:** steady – the heatwave is expected to "
        "persist for at least a week and interdiction actions are ongoing."
    )
    for span in (bluf, sev, traj):
        assert not _is_fact_asserting(span), span
        assert _claim_kind(span) == "synthesis", span
    # The U+2011 non-breaking hyphen must normalize, or the label set silently
    # misses every dashed label the core plane emits.
    assert labeled_scaffold_label(traj) == "near-term trajectory"
    assert is_assessment_scaffold(traj)


def test_signpost_labels_are_not_treated_as_assessments():
    """The assessment exemption is a closed vocabulary, not 'any label'."""
    assert not is_assessment_scaffold("- **Heat-wave alerts:** Red alerts issued. [6]")
    assert not is_assessment_scaffold("- **Seismic risk:** A 4.7 quake struck. [29]")


async def test_italy_end_to_end_score_is_earned():
    report = await verify_finding_faithfulness(
        body=ITALY_BODY, citations=ITALY_CITATIONS
    )
    assert report.checkable_claims >= 3
    assert report.score_state == SCORE_STATE_SCORED
    # Whatever the score is, it is now computed over a non-empty denominator.
    assert report.score_denominator == report.checkable_claims


# ---------------------------------------------------------------------------
# (b) SCORE STATE — a vacuous pass is not a score
# ---------------------------------------------------------------------------


async def test_zero_checkable_claims_is_unassessable_not_one():
    report = await verify_finding_faithfulness(
        body="**Key points**\n\n**Severity:** High\n", citations=[]
    )
    assert report.checkable_claims == 0
    assert report.score_state == SCORE_STATE_UNASSESSABLE
    assert report.score_state_reason == "no_checkable_claims"
    assert report.counters.get("unassessable_no_checkable_claims") == 1

    payload = build_faithfulness_critique_payload(
        report, analyzed_output_id=uuid4()
    )
    # NOT 1.0, and NOT 0.0.
    assert payload["overall_score"] == UNASSESSABLE_GATE_SCORE
    assert 0.0 < payload["overall_score"] < 1.0
    assert payload["title"] == "Faithfulness verify (unassessable)"
    # The title PREFIX is load-bearing: every verify lateral pins it.
    assert payload["title"].startswith("Faithfulness verify")
    assert "unassessable" in payload["tags"]
    assert "faithfulness_score=unassessable" in payload["body"]
    verification = payload["data"]["verification"]
    assert verification["score_state"] == SCORE_STATE_UNASSESSABLE
    assert verification["score_state_reason"] == "no_checkable_claims"
    # Rec #8, SECOND half (2026-08-09, round-5 §9b): the published
    # ``faithfulness_score`` is NULL — a tally over zero claims is not a
    # measurement, and 1.0 here entered the population mean and read as a
    # perfect pass to every consumer that did not also read ``score_state``.
    # ``scores`` (typed dict[str, float]) omits the key for the same reason.
    assert verification["faithfulness_score"] is None
    assert payload["scores"] == {}
    # …while the LATERAL / gate key stays a real float: thirteen SQL laterals
    # filter ``cr.data->>'overall_score' IS NOT NULL``, and a null THERE would
    # delete the finding from the goldset/archiver/composition basis.
    assert payload["overall_score"] is not None
    assert verification["overall_score"] == UNASSESSABLE_GATE_SCORE


async def test_thin_claims_on_a_substantive_body_is_also_unassessable():
    """One claim pulled out of 1,500 characters is the same defect, quieter."""
    body = (
        "**Key points**\n"
        + "".join(f"- **Field {i}:** value\n" for i in range(80))
        + "- The plant reopened this week [1].\n"
    )
    assert len(body) >= 800
    report = await verify_finding_faithfulness(
        body=body,
        citations=[{"marker": "[1]", "signal_id": "sig-1"}],
    )
    assert report.checkable_claims < MIN_ASSESSABLE_CLAIMS
    assert report.score_state == SCORE_STATE_UNASSESSABLE
    assert report.score_state_reason == "thin_claims_on_substantive_body"


async def test_short_honest_finding_is_not_punished():
    """A genuinely short finding with one good claim is SCORED, not unassessable —
    the rule keys on a substantive body, not on a low claim count alone."""
    report = await verify_finding_faithfulness(
        body="The plant reopened this week [1].\n",
        citations=[{"marker": "[1]", "signal_id": "sig-1"}],
    )
    assert report.checkable_claims == 1
    assert report.score_state == SCORE_STATE_SCORED


# ---------------------------------------------------------------------------
# (c) PROVISIONAL — a floor-only verdict is not an adjudicated one
# ---------------------------------------------------------------------------


async def test_floor_only_verdict_publishes_provisional_under_a_ceiling():
    report = await verify_finding_faithfulness(
        body="The plant reopened this week [1].\n",
        citations=[{"marker": "[1]", "signal_id": "sig-1"}],
    )
    assert report.judge_status != "llm"
    assert report.provisional is True
    assert report.faithfulness_score == 1.0  # the tally is unchanged

    payload = build_faithfulness_critique_payload(
        report, analyzed_output_id=uuid4()
    )
    assert payload["overall_score"] == PROVISIONAL_SCORE_CEILING
    assert "provisional" in payload["tags"]
    assert "PROVISIONAL" in payload["body"]
    verification = payload["data"]["verification"]
    assert verification["provisional"] is True
    assert verification["provisional_score_ceiling"] == PROVISIONAL_SCORE_CEILING
    # The RAW tally is still on the row — the cap demotes, it does not erase.
    assert verification["faithfulness_score"] == 1.0


async def test_as_dict_publishes_the_capped_gate_number():
    """The escalation gate caps on ``overall_score`` from this block. A zero-claim
    report used to hand it a raw 1.0, i.e. no demotion on exactly the findings
    that had not been checked.

    Rec #8, second half (2026-08-09): the block's ``faithfulness_score`` is now
    NULL on an unassessable report — the raw 1.0 also fed the trajectory
    ledger's admission floor, which admitted delta claims on exactly the
    findings that were never graded. The raw tally itself stays on the report
    object (the arithmetic is unchanged); only the published block says "never
    checked" instead of borrowing the top of the scale."""
    report = await verify_finding_faithfulness(
        body="**Key points**\n\n**Severity:** High\n", citations=[]
    )
    assert report.faithfulness_score == 1.0  # raw tally, unchanged
    block = report.as_dict()
    assert block["faithfulness_score"] is None  # published: no measurement
    assert block["overall_score"] == UNASSESSABLE_GATE_SCORE  # published, capped
    assert block["score_state"] == SCORE_STATE_UNASSESSABLE
    assert block["provisional"] is True


def test_unassessable_floor_wins_over_the_provisional_ceiling():
    """Both caps apply; the lower one is the answer."""
    from legba.data.provenance.judge_assessability import gate_score

    assert (
        gate_score(
            score=1.0,
            ceiling=None,
            score_state=SCORE_STATE_UNASSESSABLE,
            provisional=True,
        )
        == UNASSESSABLE_GATE_SCORE
    )
    # And no cap ever RAISES a score.
    assert (
        gate_score(
            score=0.2,
            ceiling=None,
            score_state=SCORE_STATE_UNASSESSABLE,
            provisional=True,
        )
        == 0.2
    )


# ---------------------------------------------------------------------------
# (d) THE JSON TRIPWIRE
# ---------------------------------------------------------------------------


def test_json_syntax_spans_are_recognised():
    for span in (
        '"verdict": "supported",',
        '{"claims": [{"text": "x"}]}',
        "{",
        "}",
        "],",
        '  "reason": null,',
    ):
        assert is_json_syntax_claim(span), span


def test_prose_is_never_mistaken_for_json():
    for span in (
        "Tehran resumed enrichment at Fordow [1].",
        '"Tehran resumed enrichment" is what the ministry said [1].',
        "The ratio was 3:1 across the period [2].",
    ):
        assert not is_json_syntax_claim(span), span


def test_a_citation_marker_span_is_not_a_json_array():
    """``[1]`` parses as a JSON array and is not one. Both classes are dropped
    either way, so no score moves — but the JSON counter's job is to point at a
    producer with a broken output contract, and firing it on an ordinary orphaned
    marker would send someone hunting a defect that is not there."""
    for span in ("[1]", "[1][2]", "[[ref:3]]", "[3-7]", "[1] ."):
        assert not is_json_syntax_claim(span), span


async def test_json_body_drops_with_its_own_counter_and_never_grades():
    """A producer with a broken output contract must not have its machine artefacts
    scored as claims — in either direction."""
    body = (
        "Here is the result:\n"
        '{\n'
        '  "verdict": "supported",\n'
        '  "reason": null,\n'
        '}\n'
    )
    report = await verify_finding_faithfulness(body=body, citations=[])
    assert report.counters.get("claims_dropped_json_syntax", 0) >= 2
    ledger = " ".join(v.text for v in report.claim_verdicts)
    assert '"verdict"' not in ledger
    assert '"reason"' not in ledger


# ---------------------------------------------------------------------------
# V-I6 (2026-08-05) — what is IN the denominator. Counters, not gates.
# ---------------------------------------------------------------------------


def test_coverage_statements_are_recognised_and_ordinary_prose_is_not():
    """The round-4 panel's second pass-side caveat. P4 — "All five country reads
    (United States, Canada, Brazil, Argentina, Mexico) were included; no target
    lacked a read" — is true, verified, and a statement about the platform's own
    input set rather than about the world. The vocabulary is bounded to things
    only this system says about itself, so an ordinary sentence about reading is
    not swept up."""
    for claim in (
        "All five country reads (United States, Canada, Brazil, Argentina, "
        "Mexico) were included; no target lacked a read.",
        "No reads were available for other regional members in this cycle.",
        "As of 5 August 2026; composed from 7 unit reads, latest 09:28 UTC.",
        "Coverage: 10 of 10 country reads resolved.",
    ):
        assert is_coverage_statement(claim), claim
    for claim in (
        "Iran resumed uranium enrichment at Natanz [4].",
        "The minister reads the report each morning.",
        "Elevated energy-security pressure persists [[ref:5]].",
    ):
        assert not is_coverage_statement(claim), claim


async def test_the_two_pass_side_caveats_are_counted_and_change_no_arithmetic():
    """Both classes sit INSIDE the supported denominator today — 97 `triggered
    indicator:` rows in the frozen population, all supported, ~4.6% of it. They
    are made COUNTABLE, not dropped: removing them would move every pass-side
    score in a train that also carries five severity changes, and nobody could
    then say which change moved the number.

    The arithmetic pin is the point of this test. ``checkable`` is 2 prose claims
    + 1 triggered indicator either way; the counters ride alongside."""
    body = (
        "- All five country reads (United States, Canada, Brazil, Argentina, "
        "Mexico) were included; no target lacked a read.\n"
        "- Iran resumed uranium enrichment at Natanz [1].\n"
    )
    citations = [
        {"marker": "[1]", "signal_id": str(uuid4()), "title": "Enrichment resumed"}
    ]
    indicators = [
        {"status": "triggered", "statement": "Electoral loss", "cites": [1]},
        {"status": "not_observed", "statement": "Border closure"},
    ]
    report = await verify_finding_faithfulness(
        body=body, citations=citations, indicators=indicators
    )
    assert report.counters["denominator_triggered_indicator_scaffold"] == 1
    assert report.counters["denominator_coverage_statement"] == 1
    assert report.checkable_claims == 3
    assert report.supported_claims == 1


async def test_a_finding_with_neither_class_carries_neither_counter():
    """No-op for every finding that has no indicators and no coverage prose."""
    report = await verify_finding_faithfulness(
        body="- Iran resumed uranium enrichment at Natanz [1].\n",
        citations=[{"marker": "[1]", "signal_id": str(uuid4()), "title": "T"}],
    )
    assert "denominator_triggered_indicator_scaffold" not in report.counters
    assert "denominator_coverage_statement" not in report.counters
