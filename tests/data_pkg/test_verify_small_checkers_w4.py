# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""W4 (2026-08-02) — the small checkers the acceptance readout itemized.

Four residual defect classes, each measured on live stamped critiques:

  1. The V-C metadata matcher could not read INEQUALITY phrasing — "confidence
     >=0.80" was compared for EQUALITY against columns that were all >= 0.80 and
     flagged as prose misquoting its own numbers.
  2. ``stale_leader_vs_facts`` fired on a NAME FORM: "Trump's" against a facts
     row naming "Donald Trump". ``'`` is a word character to the token splitter,
     so the genitive never intersected.
  3. V-F residuals still reached the ledger — a trailing "… vs." fragment, a
     claim carrying a welded "## Assessment" heading, and "Indicators to watch"
     bullets graded on citation support. The last is the worst of the three: an
     INLINE ``## Indicators to watch`` heading (no newline before it) defeated
     the section skip entirely, so a whole forward-looking block was graded as
     uncited present fact.
  4. V-C's ``_metadata_dominant`` anti-laundering gate suppresses nearly every
     outcome (verified=1 / mismatch=1 / not_dominant=36). Reviewed; NOT loosened
     — see the module note on the annotate-only path. (The 08-03 panel re-raised
     it with the evidence W4 lacked — the residual was the CITED TITLE — and
     V-H3 opened a second, evidence-bearing arm. The arm this file pins, the
     residual judged ALONE, is unchanged.)
"""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

import legba.data.provenance.verify as V
from legba.data.provenance.verify import (
    VERDICT_SUPPORTED,
    verify_finding_faithfulness,
)


class _Response:
    def __init__(self, content: str) -> None:
        self.content = content
        self.usage = None


class _StubJudge:
    """Marks every claim UNSUPPORTED, so a V-C override is visible when it lands."""

    subprovider = "stub-judge"

    def __init__(self, verdict: str = "unsupported") -> None:
        self._verdict = verdict
        self.prompts: list[str] = []

    async def chat_complete(
        self, messages, *, max_tokens=None, temperature=None, system=None, **kw
    ):
        self.prompts.append(messages[0]["content"])
        claims = messages[0]["content"].split("CLAIMS:\n")[-1].strip().splitlines()
        return _Response(
            json.dumps({"verdicts": [self._verdict] * max(len(claims), 1)})
        )


def _subclaims(confidences: list[float]) -> list[dict[str, Any]]:
    """A composition citation list carrying captured effective_confidence."""
    return [
        {
            "ref_kind": "finding",
            "ordinal": n,
            "ref_id": str(uuid4()),
            "evidence_text": f"sub-claim {n}",
            "effective_confidence": c,
        }
        for n, c in enumerate(confidences, start=1)
    ]


def _row(report, needle: str):
    return next((cv for cv in report.claim_verdicts if needle in cv.text), None)


# ---------------------------------------------------------------------------
# 1. Comparator-aware metadata compare
# ---------------------------------------------------------------------------


def test_comparators_are_read_off_the_phrasing() -> None:
    assert V._metadata_comparator(" >= ") == "ge"
    assert V._metadata_comparator(" of at least ") == "ge"
    assert V._metadata_comparator(" ≥ ") == "ge"
    assert V._metadata_comparator(" at most ") == "le"
    assert V._metadata_comparator(" no more than ") == "le"
    assert V._metadata_comparator(" greater than ") == "gt"
    # "no less than" must win over the "less than" it contains.
    assert V._metadata_comparator(" no less than ") == "ge"
    # A bare equality keeps the pre-W4 exact-match semantics.
    assert V._metadata_comparator(" of ") is None


def test_ambiguous_english_maps_to_the_inclusive_comparison() -> None:
    """A false MISMATCH manufactures a soft fail — the expensive error — so
    "above"/"below" read inclusively while the SYMBOLS stay strict."""
    assert V._metadata_comparator(" above ") == "ge"
    assert V._metadata_comparator(" below ") == "le"
    assert V._metadata_comparator(" > ") == "gt"
    assert V._metadata_comparator(" < ") == "lt"


async def test_inequality_holding_over_every_value_verifies(monkeypatch) -> None:
    """THE ADJUDICATED CASE: "confidence >=0.80" against columns all >= 0.80."""
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "1")
    claim = "All cited sub-claims carry confidence >=0.80 [[ref:1]][[ref:2]]."
    report = await verify_finding_faithfulness(
        body=f"{claim}\n",
        citations=_subclaims([0.84, 0.80, 0.91]),
        judge_llm=_StubJudge(),
    )
    cv = _row(report, "confidence >=0.80")
    assert cv is not None and cv.verdict == VERDICT_SUPPORTED
    assert "effective_confidence>=0.8 holds" in (cv.detail or "")
    assert report.counters.get("metadata_verified") == 1
    assert "metadata_mismatch" not in report.counters


async def test_an_inequality_one_value_breaks_is_a_real_mismatch(monkeypatch) -> None:
    """An inequality is a UNIVERSAL claim about the cited set — one value below
    the bar is prose overstating its own numbers, which is what V-C exists to
    see."""
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "1")
    claim = "All cited sub-claims carry confidence of at least 0.80 [[ref:1]][[ref:2]]."
    report = await verify_finding_faithfulness(
        body=f"{claim}\n",
        citations=_subclaims([0.84, 0.61]),
        judge_llm=_StubJudge(),
    )
    cv = _row(report, "at least 0.80")
    assert cv is not None and cv.reason == "metadata_mismatch"
    assert "0.61" in (cv.detail or "")


async def test_bare_equality_semantics_are_unchanged(monkeypatch) -> None:
    """The pre-W4 rule: an exact assertion matches when ANY cited value rounds
    to it."""
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "1")
    report = await verify_finding_faithfulness(
        body="The unit's effective confidence of 0.68 [[ref:1]].\n",
        citations=_subclaims([0.6812, 0.42]),
        judge_llm=_StubJudge(),
    )
    cv = _row(report, "effective confidence of 0.68")
    assert cv is not None and cv.verdict == VERDICT_SUPPORTED


# ---------------------------------------------------------------------------
# 2. stale_leader name-form normalization
# ---------------------------------------------------------------------------


def test_possessive_name_forms_match_the_full_name() -> None:
    """THE ADJUDICATED CASE: "Trump's" vs a facts row naming "Donald Trump"."""
    claimed = V._person_name_tokens("Trump's")
    assert claimed & V._person_name_tokens("Donald Trump")
    assert claimed & V._person_name_tokens("Donald J. Trump")
    # The curly apostrophe the models actually emit.
    assert V._person_name_tokens("Trump’s") & V._person_name_tokens("Donald Trump")


def test_partial_and_accented_forms_still_match() -> None:
    assert V._person_name_tokens("Janša") & V._person_name_tokens("Robert Jansa")
    assert V._person_name_tokens("Meloni") & V._person_name_tokens("Giorgia Meloni")


def test_honorifics_alone_never_make_two_people_match() -> None:
    """A shared "President" is not a shared identity — dropping the particles is
    what stops the normalization from over-matching in the other direction."""
    assert not (
        V._person_name_tokens("President Lee")
        & V._person_name_tokens("President Kim")
    )
    assert not (
        V._person_name_tokens("Mr. Smith") & V._person_name_tokens("Mr. Jones")
    )


# ---------------------------------------------------------------------------
# 3. V-F residuals
# ---------------------------------------------------------------------------


def test_an_inline_heading_is_broken_out_of_the_claim() -> None:
    """THE LIVE SHAPE: "…measures on Russia.## Key points" — no newline, so the
    sentence split (which needs whitespace after the terminator) never fired."""
    body = "**BLUF:** No confirmed coercive economic measures are imposed.## Key points\n"
    spans = V._segment_claims(body)
    claim = next(s for s in spans if "No confirmed coercive economic measures" in s)
    # The heading no longer rides on the claim's tail into the ledger…
    assert "## Key points" not in claim
    assert claim.endswith("imposed.")
    # …it is its own span, and a heading span is inert for BOTH graders.
    heading = next(s for s in spans if s.startswith("## Key points"))
    assert V._is_judgeable_claim(heading) is False
    assert V._is_fact_asserting(heading) is False


def test_an_inline_watch_heading_skips_its_whole_section() -> None:
    """The worst of the three: the welded heading defeated the SECTION skip, so
    every forward-looking bullet under it was graded as an uncited present fact."""
    body = (
        "Confidence in this judgment is low given the thin evidence.## Indicators "
        "to watch\n"
        "- A formal sanctions designation naming the central bank.\n"
        "- Any move to restrict fuel exports.\n"
    )
    spans = V._segment_claims(body)
    assert any("Confidence in this judgment is low" in s for s in spans)
    assert not any("formal sanctions designation" in s for s in spans)
    assert not any("restrict fuel exports" in s for s in spans)


def test_a_hashtag_in_prose_is_never_treated_as_a_heading() -> None:
    body = "Exchange-rate chatter continued on channel #Myanmar throughout.\n"
    assert V._segment_claims(body) == [
        "Exchange-rate chatter continued on channel #Myanmar throughout."
    ]


def test_a_trailing_abbreviation_fragment_is_rejoined_not_dropped() -> None:
    """THE LIVE SHAPE: "…(signal_volume_24h = 101 vs." + "84 last window)". The
    fragment stops existing AND the assertion it leads survives — dropping it
    would have thrown the claim away with the fragment."""
    body = "While the volume is anomalously high (signal_volume_24h = 101 vs. 84 last window), no new escalation is evident.\n"
    spans = V._segment_claims(body)
    assert len(spans) == 1
    assert "101 vs. 84 last window" in spans[0]
    assert "no new escalation is evident" in spans[0]


def test_a_dangling_fragment_with_nothing_after_it_is_dropped() -> None:
    """End-of-body residue asserts nothing — there is no next span to rejoin."""
    kept, dropped = V._segment_claims_with_drops("Change vs.\n")
    assert kept == []
    assert dropped == ["Change vs."]


def test_abbreviations_that_do_end_sentences_are_not_merge_triggers() -> None:
    """A false positive here MERGES two real claims into one ledger row, so the
    trigger list carries only abbreviations that essentially never end an English
    sentence. "etc." and "Jr." routinely do."""
    for body in (
        "The delivery included armored vehicles, drones, radars, etc. "
        "Deliveries continued through the week.\n",
        "The claim named John Smith Jr. He resigned on Tuesday.\n",
    ):
        assert len(V._segment_claims(body)) == 2, body


def test_a_backtick_heading_is_structure_not_a_claim() -> None:
    """The third section-label style, alongside ``#`` and ``**bold**``."""
    line = "`Indicators to watch`"
    assert V._is_backtick_heading(line) is True
    assert V._is_judgeable_claim(line) is False
    assert V._is_fact_asserting(line) is False
    assert V._claim_kind(line) == V.CLAIM_KIND_STRUCTURE
    # A backticked identifier inside a factual sentence is NOT a heading.
    assert V._is_backtick_heading("`signal_volume_24h` rose to 101 this window") is False


async def test_watch_bullets_never_reach_the_verdict_ledger(monkeypatch) -> None:
    """END-TO-END: indicators are forward-looking by construction and are routed
    out of citation-support grading entirely, not graded and excused."""
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "1")
    body = (
        "Rates were raised by fifty basis points [1].## Indicators to watch\n"
        "- Official announcements of fuel rationing would confirm a supply crisis.\n"
    )
    report = await verify_finding_faithfulness(
        body=body,
        citations=[{"marker": "[1]", "signal_id": str(uuid4()), "title": "Rate rise"}],
        judge_llm=_StubJudge("supported"),
    )
    assert not any("fuel rationing" in cv.text for cv in report.claim_verdicts)
    assert not any("Indicators to watch" in cv.text for cv in report.claim_verdicts)


# ---------------------------------------------------------------------------
# 4. _metadata_dominant — reviewed, NOT loosened, made VISIBLE
# ---------------------------------------------------------------------------


def test_the_anti_laundering_gate_is_unchanged() -> None:
    """A metadata value decides a claim only when the metadata assertion IS the
    claim. Left as-is deliberately: the live split (verified=1 / mismatch=1 /
    not_dominant=36) is the gate doing its job, and loosening it would let a
    checkable number certify the first-order prose beside it.

    V-H3 (2026-08-04) added a SECOND, evidence-bearing arm — the residual passes
    when the CITED text covers it and agrees on polarity — and left this one
    untouched. Both calls below still pass no ``residual_evidence``, which is the
    arm this test pins."""
    assert V._metadata_dominant(
        "The unit's effective confidence of 0.68 [[ref:1]].", "confidence of 0.68"
    )
    assert not V._metadata_dominant(
        "Iran resumed enrichment at Fordow and moved centrifuges to a hardened "
        "site, though the effective confidence of 0.68 is moderate.",
        "confidence of 0.68",
    )


async def test_a_non_dominant_metadata_check_is_recorded_not_erased(
    monkeypatch,
) -> None:
    """The readout's documented case: the number WAS checked and DID hold, and
    the row shipped a soft fail with no trace of the check. The verdict still
    does not move — the finding is simply no longer invisible."""
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "1")
    claim = (
        "Iran resumed enrichment at Fordow and moved centrifuges to a hardened "
        "site, though the effective confidence of 0.71 is moderate [[ref:1]]."
    )
    report = await verify_finding_faithfulness(
        body=f"{claim}\n",
        citations=_subclaims([0.7142857]),
        judge_llm=_StubJudge(),
    )
    cv = _row(report, "resumed enrichment at Fordow")
    assert cv is not None
    # The verdict is untouched — the judge's unsupported stands.
    assert cv.reason == "judge_unsupported"
    assert cv.verdict != VERDICT_SUPPORTED
    # …and the check that ran is now legible on the row.
    assert "metadata leg checked and holds" in (cv.detail or "")
    assert "0.71" in (cv.detail or "")
    assert report.counters["metadata_verified_not_dominant"] == 1


async def test_an_annotation_moves_neither_score_nor_span_set(monkeypatch) -> None:
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "1")
    claim = (
        "Iran resumed enrichment at Fordow and moved centrifuges to a hardened "
        "site, though the effective confidence of 0.71 is moderate [[ref:1]]."
    )
    annotated = await verify_finding_faithfulness(
        body=f"{claim}\n", citations=_subclaims([0.7142857]), judge_llm=_StubJudge()
    )
    # The same finding whose cited output carries NO confidence column: no
    # metadata check runs at all, so this is the un-annotated baseline.
    bare = await verify_finding_faithfulness(
        body=f"{claim}\n",
        citations=[
            {
                "ref_kind": "finding",
                "ordinal": 1,
                "ref_id": str(uuid4()),
                "evidence_text": "sub-claim 1",
            }
        ],
        judge_llm=_StubJudge(),
    )
    assert annotated.faithfulness_score == bare.faithfulness_score
    assert annotated.checkable_claims == bare.checkable_claims
    assert annotated.supported_claims == bare.supported_claims
    assert [s.reason for s in annotated.unsupported_spans] == [
        s.reason for s in bare.unsupported_spans
    ]
