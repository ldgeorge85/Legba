# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""V-H4 (2026-08-04) — a quote that names none of the ENUMERATED denied things.

The 08-03 panel's ``hard_fail#8`` is the last unearned hard fail left on the
judge path, and the one W2's refutes-vs-resolves rule cannot see: the judge's
quote AFFIRMS the claim instead of refuting it, in words the claim never used, so
neither R1 (verbatim restatement of the claim) nor R2 (a prior-read-only span)
fires. The counter audit recorded the same gap as flag 5 — "the demotion rule
does not yet catch outright-affirming quotes".

The mechanism is an ENUMERATED DENIAL. When a claim lists what it denies —
"FX-reserve depletion, currency crises, SWIFT bans, or sovereign default
pressures" — it has said exactly what would refute it. A quote that names none of
those things IN FULL is evidencing something else: business and mortgage defaults
are not sovereign default pressure.

Every test below is either a row from the read-only replay of the 07-31/1 and
08-02/1 stamps, or a guard on one of the three conditions that keeps the rule
from swallowing a genuine catch. The replay is the reason the rule looks the way
it does: over 24 quoted ``judge_contradicted`` hard fails it fired ONCE, on the
adjudicated row, with no false demotion.

Section 4 covers the SIBLING rule on the V-B route, V-H5 — a scoped negative is
not violated by a row whose own leading assertion is a negative about the same
subject — and the two restatement shapes it does NOT close, recorded as strict
xfails so neither can quietly become done, or quietly stay owed.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

import pytest

from legba.data.provenance.absence_slice import (
    _absence_content_terms,
    denied_enumeration,
    quote_misses_the_denied_scope,
    row_restates_the_negative,
)
from legba.data.provenance.verify import (
    FAIL_CLASS_HARD,
    FAIL_CLASS_SOFT,
    verify_finding_faithfulness,
)

_ARGENTINA = (
    "- There are no reports of FX-reserve depletion, currency crises, SWIFT "
    "bans, or sovereign default pressures affecting Argentina; the economic "
    "commentary focuses on domestic growth challenges and reforms rather than "
    "external financial coercion [1]."
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


def _signal_citations(*titles: str) -> list[dict[str, Any]]:
    return [
        {"marker": f"[{i}]", "signal_id": str(uuid4()), "title": t}
        for i, t in enumerate(titles, start=1)
    ]


@pytest.fixture(autouse=True)
def _judge_on(monkeypatch):
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "1")


# ---------------------------------------------------------------------------
# 1. The classifier
# ---------------------------------------------------------------------------


def test_an_enumerated_denial_yields_one_term_set_per_listed_thing() -> None:
    items = denied_enumeration(_ARGENTINA)
    assert len(items) == 4
    assert any("depletion" in i for i in items)
    assert any("sovereign" in i for i in items)
    # The span STOPS at the semicolon: the second clause is what the claim
    # ASSERTS, not what it denies, and folding it in would invert the test.
    assert not any("commentary" in i for i in items)


def test_a_single_unenumerated_denial_declines_to_decide() -> None:
    """Condition 1. "No discernible shift" has not said what would refute it, so
    the branch keeps its hands off — which is what leaves a genuine catch HARD."""
    assert denied_enumeration("India's posture shows no discernible shift.") == []
    assert not quote_misses_the_denied_scope(
        "the new SIB deployment represents a material change to the posture",
        "India's posture shows no discernible shift.",
    )


def test_a_governing_head_distributes_over_the_enumeration() -> None:
    """"no shift in deployment, capability, or readiness" denies a shift in EACH
    of three things; a quote naming one of them squarely is on scope."""
    claim = (
        "India's posture shows no discernible shift in deployment, capability, "
        "or readiness."
    )
    assert not quote_misses_the_denied_scope(
        "the new SIB deployment represents a material change", claim
    )


# ---------------------------------------------------------------------------
# 2. The three conditions, each on its own
# ---------------------------------------------------------------------------


def test_a_quote_sharing_nothing_is_left_alone() -> None:
    """Condition 2. A refutation in words the claim never used is SEMANTIC — the
    ordinary shape of a genuine catch. The 08-02 US-leadership row is the
    specimen: "four criminal indictments" against "no credible reports of legal
    scandals, health issues, institutional challenges, or scheduled electoral
    events"."""
    claim = (
        "Given the absence of any credible reports of legal scandals, health "
        "issues, institutional challenges, or scheduled electoral events, the "
        "trajectory is no change."
    )
    quote = (
        "four criminal indictments and a seemingly endless catalogue of "
        "controversies so far in his second term"
    )
    assert not quote_misses_the_denied_scope(quote, claim)


def test_a_squarely_named_denied_thing_earns_the_hard_class() -> None:
    """Condition 3, and the case that decides whether the rule is safe. The
    08-02 Mexico row is the live specimen: a claim denying "power outages" among
    others, and a cited row that names power outages exactly."""
    claim = (
        "- The collected signals contain no mention of oil, gas, power outages, "
        "pipeline issues, or sanctions affecting Mexico's energy sector [1]."
    )
    quote = (
        "Mexico's Federal Electricity Commission has reduced power outages by "
        "what percentage since 2024?"
    )
    assert not quote_misses_the_denied_scope(quote, claim)


def test_an_epistemic_hedge_is_not_part_of_the_denied_thing() -> None:
    """"No CREDIBLE reports of X, Y, or Z" denies X/Y/Z. Counting "credible" as
    part of each item would make every hedged claim un-refutable by construction,
    because no source ever says "credible" about itself."""
    claim = "No credible reports of strikes, incursions, or blockades were found."
    assert all("credible" not in item for item in denied_enumeration(claim))
    # …so a row naming one of the denied things squarely still earns the hard
    # class, hedge or no hedge.
    assert not quote_misses_the_denied_scope(
        "Overnight strikes hit the eastern corridor, residents said", claim
    )


# ---------------------------------------------------------------------------
# 3. End to end, through the real severity chain
# ---------------------------------------------------------------------------


async def test_the_argentina_row_demotes_with_its_own_class_and_counter() -> None:
    wire = (
        "The rest of Argentina continues to struggle with sluggish growth, high "
        "inflation, weak consumer spending, and business and mortgage defaults"
    )
    report = await verify_finding_faithfulness(
        body=f"{_ARGENTINA}\n",
        citations=_signal_citations(wire),
        judge_llm=_StubJudge({"verdicts": ["contradicted"], "quotes": [wire]}),
        target_id="country_g20_ar",
    )
    cv = next(cv for cv in report.claim_verdicts if "sovereign default" in cv.text)
    assert cv.verdict == FAIL_CLASS_SOFT
    assert cv.reason == "judge_contradicted_off_scope"
    assert report.counters["hardfail_demoted_off_denied_scope"] == 1
    # The demotion is AUDITABLE: the quote that failed to earn the hard class is
    # persisted alongside the reason it did not.
    assert "business and mortgage defaults" in (cv.detail or "")


async def test_a_real_refutation_of_the_same_claim_stays_hard() -> None:
    claim = (
        "- There are no reports of FX-reserve depletion, currency crises, SWIFT "
        "bans, or sovereign default pressures affecting Argentina [1]."
    )
    wire = "Buenos Aires reports a sharp FX-reserve depletion after the peso rout"
    report = await verify_finding_faithfulness(
        body=f"{claim}\n",
        citations=_signal_citations(wire),
        judge_llm=_StubJudge({"verdicts": ["contradicted"], "quotes": [wire]}),
        target_id="country_g20_ar",
    )
    cv = next(cv for cv in report.claim_verdicts if "sovereign default" in cv.text)
    assert cv.verdict == FAIL_CLASS_HARD
    assert cv.reason == "judge_contradicted"
    assert "hardfail_demoted_off_denied_scope" not in report.counters


# ---------------------------------------------------------------------------
# 4. V-H5 — a negative cannot be refuted by another negative
# ---------------------------------------------------------------------------


def _terms(claim: str) -> set[str]:
    return _absence_content_terms(claim, target_id=None)


def test_a_row_that_asserts_the_same_negative_is_corroboration() -> None:
    """The live Sudan row. `country_composition` claims no coordinated narrative;
    the "violating" input-slice row OPENS by saying exactly that."""
    claim = (
        "Analysis finds no coordinated narrative across the collected signals, "
        "confirming the situation register's active narrative frame "
        "[[ref:1]][[ref:9]]."
    )
    row = (
        "**BLUF**: No coordinated narrative is evident in the collected Sudan "
        "signals; coverage appears organic and driven by disparate events.\n"
        "**Key signals**: The 14 signals span unrelated topics."
    )
    assert row_restates_the_negative(row, _terms(claim))


def test_a_row_reporting_the_denied_thing_after_an_unrelated_negative_still_violates() -> None:
    """The case that would be expensive to get wrong: the row OPENS with a
    negative, but about something else, and then reports the very thing the claim
    denies. The two NEGATIVES share no term, so the violation stands."""
    claim = "- No evidence of mass protests or crackdowns was found [1]."
    row = "No new sanctions were imposed, but protesters clashed with police"
    assert not row_restates_the_negative(row, _terms(claim))


def test_a_positive_row_is_never_a_restatement() -> None:
    """The one GENUINE absence catch in the 08-03 population stays a catch."""
    claim = (
        "- No confirmed delivery of new major weapons platforms or "
        "standing-posture level change; U.S. $14 bn arms sale remains on pause [2]"
    )
    row = (
        "Taiwan's government says US hasn't notified it of any pause in a "
        "planned $14B arms sale"
    )
    assert not row_restates_the_negative(row, _terms(claim))


# ---------------------------------------------------------------------------
# THE RESTATEMENT RESIDUALS — the two shapes V-H5 does NOT close.
#
# V-G2 named three "restatement-as-violator / composition-restates-a-unit" rows
# it deliberately left on the V-B route. V-H5 closes one of them mechanically.
# The other two are recorded here rather than quietly dropped, per the ledger's
# own contract: a strict xfail turns RED the day someone fixes it.
# ---------------------------------------------------------------------------


@pytest.mark.xfail(
    strict=True,
    reason=(
        "OWED. RESTATEMENT-AS-VIOLATOR where NEITHER text is a negative about "
        "the shared subject (the `ae1b1d1e` row). The claim reports the "
        "escalation unit's read — 'moderate near-term escalation risk … proxy "
        "activity … while noting no confirmed new mobilizations' — and the "
        "violating row is that unit's own BLUF saying the same thing in the same "
        "words. V-H5 cannot see it: the row's leading assertion is POSITIVE "
        "('risk remains moderate'), so the negative-vs-negative test does not "
        "apply, and the claim's denied thing ('confirmed new mobilizations') "
        "appears in neither text. Deciding this needs a claim-vs-row AGREEMENT "
        "test, which is the same semantic gap V-H4's replay ruled out a bolt-on "
        "model for — it belongs in the judge subsystem, the named next seam."
    ),
)
def test_restatement_residual_composition_restates_a_unit_assessment() -> None:
    claim = (
        "The escalation unit assesses a moderate near-term escalation risk, "
        "highlighting proxy activity such as reported plans for additional North "
        "Korean troops to Russia for the Ukraine war, while noting no confirmed "
        "new mobilizations [[ref:5]]."
    )
    row = (
        "**BLUF:** Near-term escalation risk for North Korea remains moderate, "
        "with proxy activity (potential troop deployment to Russia/Ukraine) as "
        "the dominant vector, and the most plausible trajectory is steady."
    )
    assert row_restates_the_negative(row, _terms(claim))


@pytest.mark.xfail(
    strict=True,
    reason=(
        "OWED. SELF-CITATION-AS-VIOLATOR (the `ac025a67` row). The claim reads "
        "the visa-bond headline and draws a negative FROM it — '30 African "
        "countries … South Africa is not listed among them' — and the slice "
        "hands that same headline back as the violator. The row is the claim's "
        "OWN cited evidence, so the fix is provenance (a row the claim CITES "
        "cannot violate it), not lexical: V-G1 established exactly that rule on "
        "the judge path, and the V-B route has no equivalent because the slice "
        "carries row text without the citation identity to match against. That "
        "plumbing is a substrate change, out of this branch's scope."
    ),
)
def test_restatement_residual_self_citation_as_violator() -> None:
    claim = (
        "- The United States visa-bond program enumerates 30 African countries, "
        "but South Africa is not listed among them, confirming no new US "
        "coercive visa-related economic measure affecting South Africa [7]."
    )
    row = "30 African countries included in permanent visa bond scheme"
    assert row_restates_the_negative(row, _terms(claim))
