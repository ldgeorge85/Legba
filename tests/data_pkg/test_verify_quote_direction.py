# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""V-I (2026-08-05) — the round-4 panel's judge-path defects, pinned by case.

Failure-verdict precision has sat on a ~50% plateau for four acceptance rounds.
Round 4 is the first measured on a fully healthy judge (337/338 `judge_status=llm`,
zero `judge_failed` lines) and it finally says WHY: of the 14 hard fails in the
full census, 8 are wrong, and 5 of those rest on a quote that CONFIRMS, resolves
or restates the claim rather than refuting it.

Three of the panel's five named defects are on this path and are pinned here,
each against the round's own specimen:

* **V-I1 — number-wording blindness (H9).** "sixteen lives and thirty-six
  injuries" hard-failed by "16 people were killed, and another 36 were injured".
  An exact numeric match, scored as a contradiction.
* **V-I4 — CAMEO machine rows reach the judge (H13).** `absence_slice_machine_
  rows_excluded` fired 2,109 times on the V-B path the same day; the judge's own
  evidence view never had the filter, so a GDELT event coding grounded a hard
  fail.
* **V-I5 — the continuity router is bypassable (H13 again).** `absence_slice_
  route_excluded_continuity_claim` fired on the very claim the judge then
  hard-failed. One claim, two authorities.

Every test is either the round's specimen or a guard proving the rule does not
swallow one of the round's SIX correct hard fails (H1, H3, H4, H7, H8, H10).
"""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

import pytest

from legba.data.provenance.judge_evidence import (
    _marker_to_evidence,
    machine_coded_ordinals,
)
from legba.data.provenance.judge_quote_rules import (
    _endpoint_fingerprint,
    _numeral_fingerprint,
    _prose_direction_diverges,
    claim_is_routed_out,
    quote_confirms_the_claim,
)
from legba.data.provenance.verify import (
    FAIL_CLASS_HARD,
    FAIL_CLASS_SOFT,
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

    async def chat_complete(
        self, messages, *, max_tokens=None, temperature=None, system=None, **kw
    ):
        return _Response(self._json)


def _signal_citations(*entries: Any) -> list[dict[str, Any]]:
    """``[N]`` signal citations. A str entry is a title; a dict entry is merged
    over the defaults so a test can set ``source_id`` / ``source_text``."""
    out: list[dict[str, Any]] = []
    for i, entry in enumerate(entries, start=1):
        cite: dict[str, Any] = {"marker": f"[{i}]", "signal_id": str(uuid4())}
        cite.update({"title": entry} if isinstance(entry, str) else entry)
        out.append(cite)
    return out


@pytest.fixture(autouse=True)
def _judge_on(monkeypatch):
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "1")


# ---------------------------------------------------------------------------
# 1. V-I1 — the numeral fingerprint
# ---------------------------------------------------------------------------

_H9_CLAIM = (
    "Russia's broader assault on Ukraine's Kyiv region claimed sixteen lives "
    "and thirty-six injuries [2]."
)
_H9_QUOTE = (
    "In the Kyiv region, 36 people have been wounded, including four children, "
    "and 16 people have been killed."
)


def test_word_numerals_and_digits_fingerprint_identically() -> None:
    """The H9 pair. One side spells its numbers, the other prints them; W2's
    restatement test cannot see the equivalence because the two strings share
    almost no characters."""
    assert _numeral_fingerprint(_H9_CLAIM) == {16.0, 36.0}
    assert {16.0, 36.0} <= _numeral_fingerprint(_H9_QUOTE)


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("a 25% tariff on Brazilian goods", "a 25 per cent tariff was imposed"),
        ("a 25% tariff", "twenty-five percent"),
        ("1.2 billion in exports", "1,200,000,000 in exports"),
        ("the sixteenth strike this month", "the 16th strike this month"),
        ("thirty-six were injured", "36 were injured"),
    ],
)
def test_the_adjacent_equivalences_collapse_onto_one_magnitude(a: str, b: str) -> None:
    """Percent spellings, scaled units, ordinals and hyphenated compounds are the
    same number wearing different clothes."""
    assert _numeral_fingerprint(a) & _numeral_fingerprint(b)


def test_a_citation_marker_is_not_a_quantity() -> None:
    """The guard that keeps the rule from grading claims that assert no numbers
    at all: ``[20]`` is a pointer. Without the strip, every cited claim would
    carry a numeral and become demotable on its marker."""
    assert _numeral_fingerprint("The visit went ahead [20].") == set()
    assert _numeral_fingerprint("Sixteen died [2][11].") == {16.0}


# ---------------------------------------------------------------------------
# 2. V-I1 — the confirmation rule, and what it must not swallow
# ---------------------------------------------------------------------------


def test_the_h9_quote_confirms_the_h9_claim() -> None:
    assert quote_confirms_the_claim(_H9_QUOTE, _H9_CLAIM) is True


def test_h10_the_rounds_cleanest_true_positive_stays_refuting() -> None:
    """H10 is a genuine fabrication — a future scheduled visit reported as a
    completed past event, with the wrong date — and the SUBSET condition is what
    keeps it hard: the claim's "4 August" appears nowhere in the quote."""
    claim = (
        "President Min Aung Hlaing made his first official visit to Thailand "
        "on 4 August [20]."
    )
    quote = (
        "Myanmar President Min Aung Hlaing will make his first official visit "
        "to Thailand later this week. Bangkok will host Min Aung Hlaing from "
        "Aug. 6-7."
    )
    assert quote_confirms_the_claim(quote, claim) is False


def test_a_claim_asserting_no_numbers_is_not_gradeable_here() -> None:
    """H1/H7 (Brazil, "no evidence of economic tools"), H3/H4/H8 (contradicted
    composition refs) assert no quantity. The rule declines and the hard class
    stands — which is how five of the round's six CORRECT hard fails survive it."""
    claim = "There is no evidence of economic tools being used to compel Brazil."
    quote = "After Washington imposed a 25% tariff last month on Brazilian products"
    assert quote_confirms_the_claim(quote, claim) is False


def test_a_lone_ordinal_is_too_weak_to_license_a_demotion() -> None:
    """"first" normalizes to 1, and so do "a" and "one". A claim whose only
    number is determiner-like must not be demotable on it."""
    claim = "This was the first such incursion of the year."
    quote = "The first such incursion was recorded in 2019, not this year."
    assert quote_confirms_the_claim(quote, claim) is False


def test_the_same_numbers_about_a_different_subject_do_not_confirm() -> None:
    """Topical binding: 16 dead in Kyiv is not confirmed by 16 dead in Gaza."""
    quote = "In Gaza, 36 people have been wounded and 16 people have been killed."
    assert quote_confirms_the_claim(quote, _H9_CLAIM) is False


# ---------------------------------------------------------------------------
# 2b. V-I1 guard 5 (2026-08-09) — endpoint binding, the round-5 tightening
# ---------------------------------------------------------------------------
# Round 5 scored V-I1 0-for-1 on live fires: its one absorption was a CORRECT
# hard fail. Critique `b14bf715` (journal_assessor, claim idx 15) — the texts
# below are verbatim from the persisted claim ledger, U+2011 hyphen included.
# The signal shows a 35-MINUTE warning expiring 6 August; the journal restates
# it as a TWO-DAY warning expiring 8 August and gets the issue time wrong
# (06:00 vs 07:25). The magnitude subset held ({6, 0, 8} inside {6, 7, 25, 8,
# 0}) because flattening "06:00" and "8 Aug" to digits loses WHICH endpoint
# each number pins. Guard 5 keeps the structure and withdraws the confirmation.

_R5_MARINE_CLAIM = (
    "North‑American weather services also issue a **Special Marine Warning** "
    "for the Gulf of Mexico (issued 6 Aug 06:00 EDT, expires 8 Aug 08:00 EDT) "
    "[16]."
)
_R5_MARINE_QUOTE = (
    "Special Marine Warning issued August 6 at 7:25AM EDT until August 6 at "
    "8:00AM EDT by NWS Key West FL"
)


def test_endpoint_fingerprint_reads_clocks_and_dates() -> None:
    """12h/24h clock spellings collapse; month-day dates read in either order;
    range forms contribute both days; a claim with no endpoints yields none."""
    assert _endpoint_fingerprint("issued 6 Aug 06:00 EDT, expires 8 Aug 08:00 EDT") == {
        ("date", 8, 6), ("time", 6 * 60),
        ("date", 8, 8), ("time", 8 * 60),
    }
    assert _endpoint_fingerprint("August 6 at 7:25AM EDT until August 6 at 8:00AM") == {
        ("date", 8, 6), ("time", 7 * 60 + 25), ("time", 8 * 60),
    }
    # "8:00AM" and "08:00" are ONE endpoint; "8:00PM" is another.
    assert _endpoint_fingerprint("at 08:00") == _endpoint_fingerprint("at 8:00AM")
    assert _endpoint_fingerprint("at 8:00PM") == {("time", 20 * 60)}
    # Range forms, both orders ("Bangkok will host ... from Aug. 6-7").
    assert _endpoint_fingerprint("from Aug. 6-7") == {("date", 8, 6), ("date", 8, 7)}
    assert _endpoint_fingerprint("on the 6-7 August visit") == {
        ("date", 8, 6), ("date", 8, 7),
    }
    # H9 carries no endpoint at all — guard 5 never sees it.
    assert _endpoint_fingerprint(_H9_CLAIM) == set()
    # A citation marker is not an endpoint source, and a score line is not a
    # clock ("won 3:1" — one digit after the colon).
    assert _endpoint_fingerprint("The visit went ahead [16].") == set()
    assert _endpoint_fingerprint("won 3:1 on aggregate") == set()


def test_the_round5_marine_warning_no_longer_confirms() -> None:
    """THE FLIP. The claim pins 06:00 and 8 Aug; the quote's endpoints are
    7:25AM, 8:00AM and August 6. Diverging endpoints refute — the suppression
    must not fire, and the judge's hard fail stands."""
    assert quote_confirms_the_claim(_R5_MARINE_QUOTE, _R5_MARINE_CLAIM) is False


def test_matching_endpoints_still_confirm() -> None:
    """The guard withdraws confirmation ONLY on divergence: the same claim
    against a quote agreeing on every pinned endpoint still confirms."""
    agreeing = (
        "Special Marine Warning for the Gulf of Mexico issued August 6 at "
        "6:00AM EDT until August 8 at 8:00AM EDT by NWS Key West FL"
    )
    assert quote_confirms_the_claim(agreeing, _R5_MARINE_CLAIM) is True


def test_h9_still_confirms_after_the_tightening() -> None:
    """One-directional by construction: an empty claim endpoint set passes
    untouched, so the rule's type specimen keeps its demotion."""
    assert quote_confirms_the_claim(_H9_QUOTE, _H9_CLAIM) is True


async def test_the_marine_warning_row_stays_hard_end_to_end() -> None:
    """Through the real severity chain: the round-5 shape earns and KEEPS the
    hard class, and the V-I1 counter does not fire."""
    report = await verify_finding_faithfulness(
        body=f"- {_R5_MARINE_CLAIM}\n",
        citations=_signal_citations(
            *[f"filler {i}" for i in range(15)],
            {
                "title": "NWS Key West: Special Marine Warning",
                "source_text": _R5_MARINE_QUOTE,
            },
        ),
        judge_llm=_StubJudge(
            {"verdicts": ["contradicted"], "quotes": [_R5_MARINE_QUOTE]}
        ),
    )
    cv = next(
        cv for cv in report.claim_verdicts if "Special Marine Warning" in cv.text
    )
    assert cv.verdict == FAIL_CLASS_HARD
    assert cv.reason == "judge_contradicted"
    assert "hardfail_demoted_quote_confirms" not in report.counters


# ---------------------------------------------------------------------------
# 2c. V-I1 guard 6 (2026-08-10) — direction binding, the round-5 §10-5 arm
# ---------------------------------------------------------------------------
# Guard 5 closed the NUMERIC half of the divergence class; this is the PROSE
# half. Critique `037f769f` (region_composition, claim idx 3) — texts verbatim
# from the persisted claim ledger, U+2011/U+202F intact. The claim asserts
# CONTINUITY ("no material change since the prior 7 August read"); the judge's
# quote asserts NOVELTY about the same prior read ("a concrete casualty figure
# that was ABSENT from the prior 7 August read"). Every numeral the claim
# asserts is in the quote and its only endpoint (7 Aug) is stated back, so
# guards 1-5 correctly stay quiet — the wrongness is DIRECTIONAL, exactly the
# §10-5 class ("the refuting-quote direction check still has no semantic arm").

_R5_BURKINA_CLAIM = (
    "Mali\u2019s proxy\u2011led attacks, South\u202fAfrica\u2019s diplomatic "
    "rupture over migration, and Burkina\u202fFaso\u2019s army retaliation "
    "against civilians each lift their national escalation risk, while "
    "Niger\u2019s risk stays low; the Burkina\u202fFaso assessment shows no "
    "material change since the prior 7\u202fAugust read [[ref:5]][[ref:7]], "
    "and these trends are reflected in the open situation register entries "
    "for Mali, Sudan, South\u202fAfrica, Burkina\u202fFaso, and the stable "
    "low\u2011risk frame for Niger [[ref:8]]."
)
_R5_BURKINA_QUOTE = (
    "The escalation desk reports that Burkinabe troops entered the villages "
    "of Alkoma and Sambonaye on 4 August, killing at least 48 civilians, a "
    "concrete casualty figure that was absent from the prior 7 August read"
)


def test_the_burkina_continuity_pair_no_longer_confirms() -> None:
    """THE FLIP. The claim asserts no-change; the quote asserts a change that
    was absent from the very read the claim anchors on. Diverging direction
    refutes — the suppression must not fire."""
    assert quote_confirms_the_claim(_R5_BURKINA_QUOTE, _R5_BURKINA_CLAIM) is False


def test_the_live_pair_diverges_on_the_change_axis() -> None:
    assert _prose_direction_diverges(_R5_BURKINA_CLAIM, _R5_BURKINA_QUOTE) is True


def test_same_direction_prose_does_not_diverge() -> None:
    """The H2 shape from the same panel section: "stays low" against "remains
    steady" points the SAME way — the guard abstains, minting nothing."""
    assert _prose_direction_diverges(
        "Niger's internal-stability risk stays low this cycle [3].",
        "The security situation in Niger remains stable, observers said.",
    ) is False


def test_opposite_direction_about_a_different_subject_abstains() -> None:
    """Subject binding: a rise in one thing does not oppose a fall in another.
    The directional clauses must share content terms before the guard fires."""
    assert _prose_direction_diverges(
        "Cocoa export volumes through San-Pédro fell in July [4].",
        "Rainfall totals rose sharply across the northern highlands in July.",
    ) is False


def test_mixed_polarity_on_one_side_abstains() -> None:
    """A text hitting BOTH sides of an axis asserts no single direction the
    guard can bind — conservative abstention, not a guess."""
    assert _prose_direction_diverges(
        "France's output rose while Germany's output fell over the quarter [2].",
        "German industrial output fell for a third consecutive month.",
    ) is False


def test_negated_direction_words_assert_nothing() -> None:
    """"did not rise" is not a rise: treating it as one would fire the guard
    against a quote that says "fell" — and the two AGREE."""
    assert _prose_direction_diverges(
        "Border crossings did not rise in July, staying near June's count [8].",
        "Crossings fell slightly in July, the border agency reported.",
    ) is False


def test_polysemous_surfaces_are_not_direction_hits() -> None:
    """The lexicon's deliberate absences: "behind closed doors" is not a
    closure and "declined to comment" is not a decline."""
    assert _prose_direction_diverges(
        "The ministers reopened the border talks on Monday [5].",
        "The border talks were held behind closed doors, a spokesman said.",
    ) is False
    assert _prose_direction_diverges(
        "Grain shipment volumes rose through the corridor in July [9].",
        "The corridor authority declined to comment on grain shipment volumes.",
    ) is False


def test_the_tariff_proposal_suppression_survives_guard6() -> None:
    """151cef06, the round's other live V-I1 fire and a CORRECT suppression:
    the claim asserts a proposal ("could impose 100% tariffs... no tariff has
    been enacted yet") and the quote states the same proposal back. No
    direction axis is asserted on either side — the guard abstains and the
    demotion stands, byte-stable in the 69-pair replay."""
    claim = (
        "- **Threats or proposals:** A U.S. Senate‑passed Russian‑sanctions "
        "bill could impose 100 % tariffs on Indian imports of Russian oil, "
        "but no tariff has been enacted yet [55]."
    )
    quote = (
        "impose tariffs of up to 100% on goods from countries that purchase "
        "Russian oil and gas, including India and China"
    )
    assert quote_confirms_the_claim(quote, claim) is True


def test_h9_still_confirms_after_guard6() -> None:
    """One-directional, again: H9's pair carries no direction language at all,
    so the rule's type specimen keeps its demotion through a THIRD tightening."""
    assert quote_confirms_the_claim(_H9_QUOTE, _H9_CLAIM) is True


def test_guard6_only_withdraws_never_mints() -> None:
    """The one-directionality property. A pair the base rule already rejects
    (H10: the claim's "4 August" is missing from the quote) stays rejected no
    matter how directional the prose gets — the guard has no confirm arm — and
    the guard-5 agreeing-endpoints pair still confirms (same-direction prose
    fires nothing)."""
    claim = (
        "Checkpoint incursions fell to a total of 4 incidents on 4 August [20]."
    )
    quote = "Checkpoint incursions rose over the week of Aug. 6-7."
    # Direction diverges (fell vs rose, same subject)…
    assert _prose_direction_diverges(claim, quote) is True
    # …and the answer is still False, exactly as before guard 6: numbers and
    # endpoints already refused the confirmation. Nothing was minted.
    assert quote_confirms_the_claim(quote, claim) is False
    agreeing = (
        "Special Marine Warning for the Gulf of Mexico issued August 6 at "
        "6:00AM EDT until August 8 at 8:00AM EDT by NWS Key West FL"
    )
    assert quote_confirms_the_claim(agreeing, _R5_MARINE_CLAIM) is True


async def test_the_burkina_row_end_to_end_loses_the_false_confirms_label() -> None:
    """Through the real severity chain, with the guard in place the FALSE
    diagnosis is gone: `hardfail_demoted_quote_confirms` does not fire on the
    037f769f shape. The claim is then caught by V-I5's SEPARATE authority —
    the V-B router had already classed it a continuity read (the live critique
    counted `absence_slice_route_excluded_continuity_claim`), and one claim
    cannot have two authorities — so it lands soft under the router's honest
    label, not under a confirmation that never happened."""
    report = await verify_finding_faithfulness(
        body=f"- {_R5_BURKINA_CLAIM}\n",
        citations=_signal_citations(
            *[f"filler {i}" for i in range(6)],
            {
                "title": "escalation desk: Burkina Faso read",
                "source_text": _R5_BURKINA_QUOTE,
            },
            "regional situation register",
        ),
        judge_llm=_StubJudge(
            {"verdicts": ["contradicted"], "quotes": [_R5_BURKINA_QUOTE]}
        ),
    )
    cv = next(cv for cv in report.claim_verdicts if "no material change" in cv.text)
    assert cv.verdict == FAIL_CLASS_SOFT
    assert cv.reason == "judge_contradicted_route_excluded"
    assert "hardfail_demoted_quote_confirms" not in report.counters


async def test_a_direction_divergence_with_no_other_authority_stays_hard() -> None:
    """The restored hard class, end to end: matching numerals, no routing
    qualifier, opposite prose direction. Without guard 6 this pair confirms
    ({12} inside {12, 5}, four shared terms) and the earned hard fail would
    demote; with it the judge's contradiction stands."""
    claim = "Checkpoint attacks fell to twelve incidents in July [1]."
    quote = (
        "Checkpoint attacks rose to 12 incidents in July, up from five in "
        "June, the monitoring mission said."
    )
    report = await verify_finding_faithfulness(
        body=f"- {claim}\n",
        citations=_signal_citations(
            {"title": "Monitoring mission: checkpoint attacks", "source_text": quote}
        ),
        judge_llm=_StubJudge({"verdicts": ["contradicted"], "quotes": [quote]}),
    )
    cv = next(cv for cv in report.claim_verdicts if "twelve incidents" in cv.text)
    assert cv.verdict == FAIL_CLASS_HARD
    assert cv.reason == "judge_contradicted"
    assert "hardfail_demoted_quote_confirms" not in report.counters


# ---------------------------------------------------------------------------
# 3. V-I1 end to end, through the real severity chain
# ---------------------------------------------------------------------------


async def test_the_h9_row_demotes_with_its_own_class_and_counter() -> None:
    report = await verify_finding_faithfulness(
        body=f"- {_H9_CLAIM}\n",
        citations=_signal_citations(
            "Ukrinform: casualties reported in the Kyiv region", _H9_QUOTE
        ),
        judge_llm=_StubJudge({"verdicts": ["contradicted"], "quotes": [_H9_QUOTE]}),
    )
    cv = next(cv for cv in report.claim_verdicts if "sixteen lives" in cv.text)
    assert cv.verdict == FAIL_CLASS_SOFT
    assert cv.reason == "judge_quote_confirms_claim"
    assert report.counters["hardfail_demoted_quote_confirms"] == 1
    # Auditable: the quote that failed to earn the hard class is persisted with
    # the reason it did not.
    assert "16 people have been killed" in (cv.detail or "")


async def test_a_real_numeric_refutation_stays_hard() -> None:
    """The direction that matters: a quote carrying DIFFERENT numbers about the
    same subject refutes, and the hard class is earned."""
    claim = "Russia's assault on Ukraine's Kyiv region claimed sixteen lives [2]."
    quote = (
        "In the Kyiv region, three people have been killed, the emergency "
        "service said."
    )
    report = await verify_finding_faithfulness(
        body=f"- {claim}\n",
        citations=_signal_citations("Kyiv region casualty report", quote),
        judge_llm=_StubJudge({"verdicts": ["contradicted"], "quotes": [quote]}),
    )
    cv = next(cv for cv in report.claim_verdicts if "sixteen lives" in cv.text)
    assert cv.verdict == FAIL_CLASS_HARD
    assert cv.reason == "judge_contradicted"
    assert "hardfail_demoted_quote_confirms" not in report.counters


# ---------------------------------------------------------------------------
# 4. V-I4 — a CAMEO coding is not testimony
# ---------------------------------------------------------------------------

_CAMEO_QUOTE = "STUDENT <-> PAPUA: protest in Jakarta, Jakarta Raya, Indonesia"
_CAMEO_TEXT = (
    "GDELT/CAMEO structured event record (machine coding of a news report, not "
    f"article text)\n{_CAMEO_QUOTE}\naction: protest (CAMEO 145)"
)


def test_the_discriminator_is_the_evidence_text_not_the_source_id() -> None:
    """THE REPLAY CORRECTION, and the reason a fix gets replayed before it ships.

    On the V-B route a `source.gdelt.files` row IS a coding, so the source id
    settles it there. On the CITATION path it does not: the same handler supplies
    entries whose TITLE is CAMEO-shaped while their `source_text` is the real
    article. That entry is `[11]` of the round-4 H1/H7 — "THE US <-> BRAZIL:
    coerce in Brazil" over "The United States has revoked the visa of Brazil's
    ambassador in Washington…" — and H1/H7 are two of the SIX hard fails the
    panel scored CORRECT. Keying on the source id demoted them both.
    """
    ordinals = machine_coded_ordinals(
        [
            # A rendered coding: the tag opens the evidence.
            {"marker": "[1]", "signal_id": "a", "snippet": _CAMEO_TEXT},
            # The H1/H7 shape: CAMEO title + gdelt source id, REAL article text.
            {
                "marker": "[2]",
                "signal_id": "b",
                "source_id": "source.gdelt.files",
                "title": "THE US <-> BRAZIL: coerce in Brazil",
                "source_text": (
                    "The United States has revoked the visa of Brazil's "
                    "ambassador in Washington after Brazil withheld approval."
                ),
            },
            # Nothing to read but the coding — the V-B markers answer.
            {"marker": "[3]", "signal_id": "c", "source_id": "source.gdelt.files"},
            {"marker": "[4]", "signal_id": "d", "title": "Reuters: talks resume"},
        ]
    )
    assert ordinals == {1, 3}


async def test_the_h1_shape_keeps_its_hard_class() -> None:
    """The regression the replay caught, end to end: a gdelt-sourced citation
    whose SOURCE is a real article can still earn a hard fail."""
    claim = (
        "**BLUF:** Brazil is not currently subject to any observed coercive "
        "economic measures in this slice [1]."
    )
    wire = (
        "After Washington imposed a 25% tariff last month on several Brazilian "
        "products, trade between the countries has been disrupted."
    )
    report = await verify_finding_faithfulness(
        body=f"- {claim}\n",
        citations=[
            {
                "marker": "[1]",
                "signal_id": str(uuid4()),
                "source_id": "source.gdelt.files",
                "title": "THE US <-> BRAZIL: coerce in Brazil",
                "source_text": wire,
            }
        ],
        judge_llm=_StubJudge({"verdicts": ["contradicted"], "quotes": [wire]}),
    )
    cv = next(cv for cv in report.claim_verdicts if "coercive economic" in cv.text)
    assert cv.verdict == FAIL_CLASS_HARD
    assert cv.reason == "judge_contradicted"
    assert "hardfail_demoted_machine_row" not in report.counters


def test_the_judges_evidence_view_caveats_the_coding() -> None:
    """The judge is TOLD what it is holding, even when the analyst's rendering
    did not survive into the citation entry."""
    evidence = _marker_to_evidence(
        [
            {
                "marker": "[1]",
                "signal_id": "a",
                "source_id": "source.gdelt.files",
                "title": "PAPUA: protest",
            }
        ]
    )
    assert "MACHINE-CODED" in evidence[1]
    assert "not testimony" in evidence[1]


async def test_the_h13_coding_cannot_ground_a_hard_fail() -> None:
    """The round-4 H13. `absence_slice_machine_rows_excluded`=15 fired on this
    very critique on the V-B path; the judge path had no filter and hard-failed
    the claim on a code label whose underlying article SUPPORTS it."""
    claim = "Indonesia shows no material change since the prior read [1]."
    report = await verify_finding_faithfulness(
        body=f"- {claim}\n",
        citations=[
            {
                "marker": "[1]",
                "signal_id": str(uuid4()),
                "title": "Prior read: Indonesia internal stability",
                "snippet": "Assessed steady; protest activity remains localised.",
            },
            {
                "marker": "[2]",
                "signal_id": str(uuid4()),
                "source_id": "source.gdelt.files",
                "title": "PAPUA protest coding",
                "snippet": _CAMEO_TEXT,
            },
        ],
        judge_llm=_StubJudge(
            {"verdicts": ["contradicted"], "quotes": [_CAMEO_QUOTE]}
        ),
    )
    cv = next(cv for cv in report.claim_verdicts if "material change" in cv.text)
    assert cv.verdict == FAIL_CLASS_SOFT
    assert cv.reason == "judge_contradicted_machine_row"
    assert report.counters["hardfail_demoted_machine_row"] == 1


async def test_a_quote_from_real_reporting_is_unaffected_by_the_filter() -> None:
    """The filter is scoped to quotes that resolve ONLY in a coding. A finding
    that cites BOTH a CAMEO row and a wire report, refuted by the wire report,
    keeps its hard class."""
    claim = "Jakarta reported no protest activity in the capital this week [1]."
    wire = "Thousands marched through central Jakarta on Tuesday, police said."
    report = await verify_finding_faithfulness(
        body=f"- {claim}\n",
        citations=[
            {"marker": "[1]", "signal_id": str(uuid4()), "title": wire},
            {
                "marker": "[2]",
                "signal_id": str(uuid4()),
                "source_id": "source.gdelt.files",
                "snippet": _CAMEO_TEXT,
            },
        ],
        judge_llm=_StubJudge({"verdicts": ["contradicted"], "quotes": [wire]}),
    )
    cv = next(cv for cv in report.claim_verdicts if "protest activity" in cv.text)
    assert cv.verdict == FAIL_CLASS_HARD
    assert cv.reason == "judge_contradicted"
    assert "hardfail_demoted_machine_row" not in report.counters


# ---------------------------------------------------------------------------
# 5. V-I5 — the routing decision is binding on the judge too
# ---------------------------------------------------------------------------


def test_the_router_answers_the_same_question_on_both_paths() -> None:
    """H13's claim and H12's shape are both continuity reads; a scoped negative
    the router does NOT take off the slice route is untouched, and a claim with
    no scope qualifier never enters the gate at all."""
    assert claim_is_routed_out(
        "No material change since the prior read [121]."
    ) == "continuity_claim"
    assert claim_is_routed_out(
        "Compared with the prior read, energy security shows no new outages, "
        "price spikes, attacks on energy assets, or trade sanctions [12]."
    ) == "continuity"
    assert claim_is_routed_out(
        "The current signal set adds no new supply-side incidents or outages [3]."
    ) is None
    assert claim_is_routed_out("Iran resumed enrichment at Natanz [4].") is None


async def test_a_routed_out_claim_cannot_be_hard_failed_by_the_judge() -> None:
    """The round-4 §6.3: `absence_slice_route_excluded_continuity_claim` fired
    on the VERY claim the judge then hard-failed. 08-03 rec #2 shipped and works
    — 115 fires on 114 rows — and was bypassable by the more expensive path."""
    claim = "No material change since the prior read [1]."
    wire = "The finance ministry announced a new export levy on Tuesday."
    report = await verify_finding_faithfulness(
        body=f"- {claim}\n",
        citations=_signal_citations("Prior read: steady", wire),
        judge_llm=_StubJudge({"verdicts": ["contradicted"], "quotes": [wire]}),
    )
    cv = next(cv for cv in report.claim_verdicts if "material change" in cv.text)
    assert cv.verdict == FAIL_CLASS_SOFT
    assert cv.reason == "judge_contradicted_route_excluded"
    assert report.counters["hardfail_demoted_route_excluded"] == 1
    # The claim still FAILS — only the severity moved.
    assert cv.detail and "two authorities" in cv.detail


async def test_a_claim_the_router_never_took_stays_hard() -> None:
    """The bound is exactly the router's own decision. A positive fact claim is
    not routed out and a genuine refutation of it still earns the hard class."""
    claim = "Jakarta imposed a new export levy on nickel ore on 4 August [1]."
    wire = "The finance ministry said no new levy on nickel ore is under review."
    report = await verify_finding_faithfulness(
        body=f"- {claim}\n",
        citations=_signal_citations("Nickel policy report", wire),
        judge_llm=_StubJudge({"verdicts": ["contradicted"], "quotes": [wire]}),
    )
    cv = next(cv for cv in report.claim_verdicts if "export levy" in cv.text)
    assert cv.verdict == FAIL_CLASS_HARD
    assert cv.reason == "judge_contradicted"
    assert "hardfail_demoted_route_excluded" not in report.counters
