# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""V-G1 (2026-08-03) — SIGNALS-ONLY REFUTATION, from the adjudicated re-run.

The 08-03 panel traced all 23 surviving ``judge_contradicted`` quotes back to
their origin and found the platform's highest-severity verdict resting on the
wrong kind of text:

    a real signal (source article)        7   30%
    the SAME DESK'S OWN PRIOR READ       13   57%
    another analyst's output              1    4%
    traceable nowhere                     1    4%
    unquoted                              1    4%

14 of 24 hard fails are a finding refuting a finding. The mechanism penalises
exactly the behaviour the system exists to produce — a desk that correctly
updates on new signals is hard-failed by the assessment it just superseded.

Both adjudicated regression cases are reproduced below verbatim enough to bite:

  * ``hard_fail#5`` / ``disruption_status`` / ``lane_hormuz`` — the desk shifts
    holding -> degrading on four fresh attack signals, and the judge hard-fails
    the new BLUF by quoting its own prior read ([44], traced to
    ``7eb5a4f6-fb82-4a8f-b8cd-b384dae0f77e``, "Hormuz Strait – Interdiction risk
    – Holding").
  * ``hard_fail#4`` / ``escalation`` / ``country_g20_de`` — same shape, traced to
    ``5bb1ff12-a360-43fa-bbab-e706ea6415d3`` ("Germany – Border Control
    Extension"). Note the quote's tail is the PRIOR FINDING'S editorial phrasing,
    not source text at all: "verbatim evidence span" was true of the string and
    false of its status as evidence.

The rule: a hard fail must point at SOURCE REPORTING, or at evidence THE CLAIM
ITSELF CITES. A verbatim quote from an uncited finding is not discarded — it
demotes to the soft ``judge_prior_read_conflict``, because the disagreement is
real information (the desk changed its mind) and is emphatically not fabrication.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

import legba.data.provenance.verify as V
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
        self.prompts: list[str] = []

    async def chat_complete(
        self, messages, *, max_tokens=None, temperature=None, system=None, **kw
    ):
        self.prompts.append(messages[0]["content"])
        return _Response(self._json)


def _ledger_row(report, needle: str):
    return next(cv for cv in report.claim_verdicts if needle in cv.text)


# ---------------------------------------------------------------------------
# hard_fail#5 — lane_hormuz. The desk updates; its own prior read "refutes" it.
# ---------------------------------------------------------------------------

_HORMUZ_PRIOR_READ = (
    "PRIOR READ (this target's previous verified read): Hormuz Strait – "
    "Interdiction risk – Holding\n"
    "No material change in the physical flow through the Strait of Hormuz; "
    "interdiction risk remains the dominant vector and the situation is holding "
    "steady."
)
_HORMUZ_SIGNAL_13 = (
    "Second LNG tanker struck off Bandar Abbas; charterers declare force majeure "
    "on 24 cargoes"
)
_HORMUZ_SIGNAL_4 = "IRGC navy intercepts drone near the Strait of Hormuz"

_HORMUZ_BODY = (
    "BLUF: transit conditions through the Strait of Hormuz have DEGRADED from "
    "the prior holding assessment.\n"
    "A second LNG tanker was struck and charterers declared force majeure on "
    "twenty-four cargoes [13].\n"
    "The IRGC navy intercepted a drone near the strait [4].\n"
)


def _hormuz_citations() -> list[dict[str, Any]]:
    """Four fresh SIGNAL citations plus the desk GROUNDING block at [44].

    The grounding shape is the real one (``kinds.is_grounding_citation``): no
    ``signal_id``, ``ref_kind='prior_read'``, real captured ``evidence_text``.
    """
    return [
        {"marker": "[4]", "signal_id": str(uuid4()), "title": _HORMUZ_SIGNAL_4},
        {"marker": "[13]", "signal_id": str(uuid4()), "title": _HORMUZ_SIGNAL_13},
        {
            "marker": "[44]",
            "ref_kind": "prior_read",
            "title": "Hormuz Strait – Interdiction risk – Holding",
            "evidence_text": _HORMUZ_PRIOR_READ,
        },
    ]


async def test_hormuz_prior_read_can_no_longer_hard_fail_the_update(
    monkeypatch,
) -> None:
    """The adjudicated case: the BLUF is right, the prior read is superseded."""
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "1")
    judge = _StubJudge(
        {
            "verdicts": ["contradicted", "supported", "supported"],
            "quotes": [
                "No material change in the physical flow through the Strait of "
                "Hormuz; interdiction risk remains the dominant vector",
                "",
                "",
            ],
        }
    )
    report = await verify_finding_faithfulness(
        body=_HORMUZ_BODY,
        citations=_hormuz_citations(),
        judge_llm=judge,
        target_id="lane_hormuz",
    )
    cv = _ledger_row(report, "DEGRADED")
    assert cv.verdict == FAIL_CLASS_SOFT, "an update is not a hard fail"
    assert cv.reason == "judge_prior_read_conflict"
    assert report.counters["hardfail_demoted_prior_read"] == 1
    assert "hardfail_demoted_no_quote" not in report.counters
    assert "hardfail_demoted_not_refuting" not in report.counters


async def test_the_demoted_quote_is_still_persisted_and_says_why(
    monkeypatch,
) -> None:
    """A demotion nobody can audit is no better than a hard fail nobody can."""
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "1")
    judge = _StubJudge(
        {
            "verdicts": ["contradicted", "supported", "supported"],
            "quotes": ["No material change in the physical flow through the Strait", "", ""],
        }
    )
    report = await verify_finding_faithfulness(
        body=_HORMUZ_BODY,
        citations=_hormuz_citations(),
        judge_llm=judge,
        target_id="lane_hormuz",
    )
    cv = _ledger_row(report, "DEGRADED")
    detail = cv.detail or ""
    assert "No material change in the physical flow" in detail
    assert "an update, not a misstatement of evidence" in detail
    span = next(s for s in report.unsupported_spans if "DEGRADED" in s.text)
    assert span.reason == "judge_prior_read_conflict"


# ---------------------------------------------------------------------------
# hard_fail#4 — country_g20_de. The "verbatim evidence span" is the prior
# finding's own EDITORIAL phrasing; the underlying wire says something plainer.
# ---------------------------------------------------------------------------

_DE_PRIOR_READ = (
    "PRIOR READ (this target's previous verified read): Germany – Border Control "
    "Extension\n"
    "Berlin extended internal border controls, confirming a concrete security "
    "action rather than mere rhetoric."
)
_DE_WIRE = (
    "Germany to prolong border controls amid Ceuta migrant crisis — Berlin says "
    "it will maintain internal border checks beyond September, citing renewed "
    "pressure from irregular migration."
)
_DE_BODY = (
    "No reported border incidents this window; the prior border-control "
    "extension remains the only noted action [47].\n"
    "Federal police reported routine checks at three crossings [12].\n"
)


def _de_citations() -> list[dict[str, Any]]:
    return [
        {"marker": "[12]", "signal_id": str(uuid4()), "title": _DE_WIRE},
        {
            "marker": "[47]",
            "ref_kind": "prior_read",
            "title": "Germany – Border Control Extension",
            "evidence_text": _DE_PRIOR_READ,
        },
    ]


async def test_germany_editorial_phrasing_from_a_prior_read_is_not_evidence(
    monkeypatch,
) -> None:
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "1")
    judge = _StubJudge(
        {
            "verdicts": ["supported", "contradicted"],
            "quotes": [
                "",
                "confirming a concrete security action rather than mere rhetoric",
            ],
        }
    )
    report = await verify_finding_faithfulness(
        body=_DE_BODY,
        citations=_de_citations(),
        judge_llm=judge,
        target_id="country_g20_de",
    )
    cv = _ledger_row(report, "routine checks")
    assert cv.verdict == FAIL_CLASS_SOFT
    assert cv.reason == "judge_prior_read_conflict"
    assert report.counters["hardfail_demoted_prior_read"] == 1


# ---------------------------------------------------------------------------
# The class this must NOT swallow: a genuine catch off SOURCE reporting.
# ---------------------------------------------------------------------------


async def test_a_quote_from_a_real_signal_still_earns_the_hard_class(
    monkeypatch,
) -> None:
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "1")
    judge = _StubJudge(
        {
            "verdicts": ["supported", "contradicted"],
            "quotes": [
                "",
                "Berlin says it will maintain internal border checks beyond "
                "September",
            ],
        }
    )
    report = await verify_finding_faithfulness(
        body=_DE_BODY,
        citations=_de_citations(),
        judge_llm=judge,
        target_id="country_g20_de",
    )
    cv = _ledger_row(report, "routine checks")
    assert cv.verdict == FAIL_CLASS_HARD
    assert cv.reason == "judge_contradicted"
    assert "hardfail_demoted_prior_read" not in report.counters


async def test_an_unresolvable_quote_still_demotes_as_unquoted(monkeypatch) -> None:
    """V-D keeps precedence — V-G1 only judges quotes that already RESOLVE."""
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "1")
    judge = _StubJudge(
        {
            "verdicts": ["supported", "contradicted"],
            "quotes": ["", "Berlin abolished all border checks with immediate effect"],
        }
    )
    report = await verify_finding_faithfulness(
        body=_DE_BODY, citations=_de_citations(), judge_llm=judge
    )
    cv = _ledger_row(report, "routine checks")
    assert cv.reason == "judge_contradicted_unquoted"
    assert report.counters["hardfail_demoted_no_quote"] == 1
    assert "hardfail_demoted_prior_read" not in report.counters


# ---------------------------------------------------------------------------
# The COMPOSITION tower — where EVERY evidence entry is another finding. The
# escape hatch is the claim's own citation: a clause that cites [[ref:N]] chose
# that evidence and answers for it.
# ---------------------------------------------------------------------------

_REF2_BODY = (
    "BLUF: escalation risk for the Black Sea lane remains LOW; no interdiction "
    "of commercial traffic was observed this window."
)
_REF3_BODY = (
    "BLUF: energy-security pressure on Russia has risen sharply due to Ukrainian "
    "drone strikes on oil refineries."
)


def _composition_citations() -> list[dict[str, Any]]:
    return [
        {
            "marker": "[[ref:2]]",
            "ordinal": 2,
            "ref_kind": "finding",
            "ref_id": str(uuid4()),
            "title": "Black Sea – escalation – low",
            "evidence_text": _REF2_BODY,
        },
        {
            "marker": "[[ref:3]]",
            "ordinal": 3,
            "ref_kind": "finding",
            "ref_id": str(uuid4()),
            "title": "Russia – energy security",
            "evidence_text": _REF3_BODY,
        },
    ]


async def test_a_composition_clause_is_still_hard_failed_by_what_it_cites(
    monkeypatch,
) -> None:
    """The escape hatch: self-cited evidence CAN refute. The tower stays gradeable."""
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "1")
    body = (
        "Russia faces no meaningful energy-security pressure this window "
        "[[ref:3]].\n"
    )
    judge = _StubJudge(
        {
            "verdicts": ["contradicted"],
            "quotes": [
                "energy-security pressure on Russia has risen sharply due to "
                "Ukrainian drone strikes"
            ],
        }
    )
    report = await verify_finding_faithfulness(
        body=body, citations=_composition_citations(), judge_llm=judge
    )
    cv = _ledger_row(report, "no meaningful energy-security")
    assert cv.verdict == FAIL_CLASS_HARD
    assert cv.reason == "judge_contradicted"


async def test_an_uncited_sibling_finding_cannot_hard_fail_a_composition_clause(
    monkeypatch,
) -> None:
    """The §5 ``hard_fail#9`` shape: a sibling's BLUF is not counter-evidence."""
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "1")
    body = "Commercial traffic through the Black Sea was interdicted repeatedly.\n"
    judge = _StubJudge(
        {
            "verdicts": ["contradicted"],
            "quotes": [
                "no interdiction of commercial traffic was observed this window"
            ],
        }
    )
    report = await verify_finding_faithfulness(
        body=body, citations=_composition_citations(), judge_llm=judge
    )
    cv = _ledger_row(report, "interdicted repeatedly")
    assert cv.verdict == FAIL_CLASS_SOFT
    assert cv.reason == "judge_prior_read_conflict"
    assert report.counters["hardfail_demoted_prior_read"] == 1


# ---------------------------------------------------------------------------
# The discriminator itself, and the prompt-side statement of the same rule.
# ---------------------------------------------------------------------------


def test_signal_backed_ordinals_excludes_grounding_and_subclaims() -> None:
    assert V._signal_backed_ordinals(_hormuz_citations()) == {4, 13}
    assert V._signal_backed_ordinals(_composition_citations()) == set()
    assert V._signal_backed_ordinals(None) == set()
    assert V._signal_backed_ordinals([{"marker": "[9]"}]) == set()


def test_the_judge_prompt_states_the_anti_update_rule() -> None:
    """Enforcement is mechanical, but the judge is told the rule as well — a
    demotion the judge could have avoided is a wasted call and a worse verdict."""
    rule = V._JUDGE_QUOTE_RULE.lower()
    assert "prior read" in rule
    assert "baseline" in rule
    assert "source reporting" in rule


async def test_the_rule_reaches_every_judge_route(monkeypatch) -> None:
    """The unit lead, the composition lead and the absence rubric share ONE
    constant, so the rule cannot be live on some routes and missing on others."""
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "1")
    judge = _StubJudge({"verdicts": ["supported", "supported"], "quotes": ["", ""]})
    await verify_finding_faithfulness(
        body="No new border incidents were observed [12].\nChecks continued [12].\n",
        citations=_de_citations(),
        judge_llm=judge,
    )
    assert judge.prompts, "the judge must actually have been called"
    for prompt in judge.prompts:
        assert "BASELINE this claim is updating" in prompt


# ---------------------------------------------------------------------------
# V-G3 (2026-08-03) — CARVE-OUTS ON THE JUDGE PATH.
#
# F-A W1(d) handed a claim's exemption clauses to the V-B stage-2 prompt and that
# half worked. The equivalent blindness persists on the LLM judge path, which
# V-D's quote rule does not screen for: a quote can satisfy D1 perfectly while
# refuting only a clause the claim ALREADY EXCLUDED. `hard_fail#4`'s Germany BLUF
# is refuted by the border-control extension the finding explicitly discloses and
# carves out one bullet later.
# ---------------------------------------------------------------------------

_CARVE_OUT_CLAIM = (
    "No new material escalation was observed beyond the existing diplomatic and "
    "border measures [12]."
)
_EXEMPTED_WIRE = (
    "Berlin maintains the existing border measures and its earlier diplomatic "
    "protest note, officials confirmed"
)


def _carve_out_citations() -> list[dict[str, Any]]:
    return [{"marker": "[12]", "signal_id": str(uuid4()), "title": _EXEMPTED_WIRE}]


async def test_a_quote_landing_on_the_claims_own_carve_out_is_not_a_hard_fail(
    monkeypatch,
) -> None:
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "1")
    judge = _StubJudge(
        {
            "verdicts": ["contradicted"],
            "quotes": [
                "Berlin maintains the existing border measures and its earlier "
                "diplomatic protest note"
            ],
        }
    )
    report = await verify_finding_faithfulness(
        body=f"{_CARVE_OUT_CLAIM}\n",
        citations=_carve_out_citations(),
        judge_llm=judge,
        target_id="country_g20_de",
    )
    cv = _ledger_row(report, "beyond the existing")
    assert cv.verdict == FAIL_CLASS_SOFT
    assert cv.reason == "judge_contradicted_unrefuted"
    assert report.counters["hardfail_demoted_not_refuting"] == 1


async def test_a_quote_refuting_the_assertion_itself_still_earns_the_hard_class(
    monkeypatch,
) -> None:
    """The keep-test — the guard must fire on the EXEMPTION, never the assertion."""
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "1")
    wire = (
        "Germany expels three envoys and moves an armoured brigade east in a "
        "sharp new escalation"
    )
    judge = _StubJudge(
        {
            "verdicts": ["contradicted"],
            "quotes": ["expels three envoys and moves an armoured brigade east"],
        }
    )
    report = await verify_finding_faithfulness(
        body=f"{_CARVE_OUT_CLAIM}\n",
        citations=[{"marker": "[12]", "signal_id": str(uuid4()), "title": wire}],
        judge_llm=judge,
        target_id="country_g20_de",
    )
    cv = _ledger_row(report, "beyond the existing")
    assert cv.verdict == FAIL_CLASS_HARD
    assert cv.reason == "judge_contradicted"


async def test_the_carve_outs_and_scale_ride_into_the_judge_prompt(
    monkeypatch,
) -> None:
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "1")
    judge = _StubJudge({"verdicts": ["supported"], "quotes": [""]})
    await verify_finding_faithfulness(
        body=f"{_CARVE_OUT_CLAIM}\n",
        citations=_carve_out_citations(),
        judge_llm=judge,
    )
    prompt = judge.prompts[0]
    assert "QUALIFIERS" in prompt
    assert "beyond the existing diplomatic and border measures" in prompt
    assert "SCALE/KIND qualifier: 'new'" in prompt
    assert "already EXEMPTS" in prompt


def test_a_claim_with_no_qualifiers_renders_byte_identically() -> None:
    """The prompt diff is confined to the claims that need it."""
    plain = "The central bank raised rates by fifty basis points [1]."
    assert V._judge_claim_block(3, plain) == f"3. {plain}"


def test_the_carve_out_guard_needs_a_real_exemption_hit() -> None:
    assert (
        V._quote_hits_a_carve_out(
            "Berlin maintains the existing border measures and its earlier "
            "diplomatic protest note",
            _CARVE_OUT_CLAIM,
        )
        is True
    )
    # No carve-out in the claim at all → never fires.
    assert (
        V._quote_hits_a_carve_out(
            "Berlin maintains the existing border measures",
            "No new material escalation was observed [12].",
        )
        is False
    )
    # A quote that shares more with the ASSERTION than with the exemption is a
    # real refutation and is left alone.
    assert (
        V._quote_hits_a_carve_out(
            "a sharp new material escalation was observed in Berlin", _CARVE_OUT_CLAIM
        )
        is False
    )
    assert V._quote_hits_a_carve_out("", _CARVE_OUT_CLAIM) is False
