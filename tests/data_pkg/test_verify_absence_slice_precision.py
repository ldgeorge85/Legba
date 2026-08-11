# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""W1 (2026-08-02) — CONTRADICTED-BRANCH PRECISION, from the adjudicated cases.

The 08-02 acceptance readout measured ``absence_slice_contradicted`` at ~46%
precision (6/13 correct) while it carried 50% of ALL hard fails, and the operator
kept the class HARD: the FILTERS make it precise. Every test below is a live
adjudicated failure from that readout, pulled from the stamped-critique ledger
(``judge_pipeline_version = 2026-07-31/1``) and reproduced here verbatim enough
to bite:

  (a) TARGET SCOPE — a Benin coup headline hard-failed a SOUTH AFRICA claim.
  (b) COMPOSITION BODIES — "Mexico – Narrative Coordination … Assessment" (a unit
      finding's TITLE, which names the topic and never the verdict) hard-failed
      the Americas region_composition clause that says Mexico has NO coordinated
      narrative — which is what that finding's own BODY says too.
  (c) MACHINE-STRUCTURED ROWS — "COLLEGE: protest in Japan", a GDELT/CAMEO event
      coding, hard-failed a Japan "no reports of protests" claim.
  (d) CARVE-OUTS — the Italy Schengen claim ("no new material escalation BEYOND
      the existing diplomatic and border measures") was contradicted by the
      exempted measure; the Niger claim ("no CONFIRMED changes … GIVEN the
      low-confidence, below-floor signals") by the below-floor signal itself.
  (e) ROUTE — volume / continuity / trajectory reads are not slice-checkable
      negatives and must not be decided against a slice row.
  (f) SLICE-SIZE HONESTY — a pass over a 1-row slice must not read as strong
      verification.
"""

from __future__ import annotations

import json
import re
from typing import Any
from uuid import uuid4

import legba.data.provenance.verify as V
from legba.data.provenance.verify import (
    FAIL_CLASS_HARD,
    VERDICT_SUPPORTED,
    load_absence_slice_rows,
    verify_finding_faithfulness,
)

# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------


class _Response:
    def __init__(self, content: str) -> None:
        self.content = content
        self.usage = None


#: A judge prompt's numbered claim entries — ``1. <claim>`` at line start.
_NUMBERED_CLAIM_RE = re.compile(r"^\d+\.\s", re.MULTILINE)


class _StubJudge:
    """Canned payload per SYSTEM prompt, so the V-B verdict is isolated."""

    subprovider = "stub-judge"

    def __init__(self, *, slice_payload: dict[str, Any] | None = None) -> None:
        self._slice = slice_payload
        self.slice_calls = 0
        self.slice_prompts: list[str] = []

    async def chat_complete(
        self, messages, *, max_tokens=None, temperature=None, system=None, **kw
    ):
        if system == V._ABSENCE_SLICE_JUDGE_SYSTEM:
            self.slice_calls += 1
            self.slice_prompts.append(messages[0]["content"])
            return _Response(json.dumps(self._slice or {}))
        # Count NUMBERED entries, not lines: V-G3 gives an annotated claim an
        # indented QUALIFIERS continuation line, exactly as stage 2 has always
        # done for carve-outs.
        n = len(_NUMBERED_CLAIM_RE.findall(messages[0]["content"]))
        return _Response(json.dumps({"verdicts": ["supported"] * max(n, 1)}))


class _SliceConn:
    """asyncpg-shaped double over ``analyst_traces`` + the two slice tables.

    Rows are ``(title, body, source_id, provenance_kind, row_kind)`` — the exact
    projection :func:`load_absence_slice_rows` reads.
    """

    def __init__(self, rows: list[dict[str, str]] | None) -> None:
        self._rows = rows

    async def fetchrow(self, sql: str, *args):
        if "analyst_traces" in sql:
            if self._rows is None:
                return None
            return {"input_row_refs": [uuid4() for _ in range(max(len(self._rows), 1))]}
        return None

    async def fetch(self, sql: str, *args):
        return [
            {
                "title": r.get("title", ""),
                "body": r.get("body", ""),
                "source_id": r.get("source_id", ""),
                "provenance_kind": r.get("provenance_kind", ""),
                "row_kind": r.get("row_kind", "signal"),
            }
            for r in (self._rows or [])
        ]


def _signal(title: str, **kw: str) -> dict[str, str]:
    return {"title": title, "row_kind": "signal", **kw}


def _output(title: str, body: str) -> dict[str, str]:
    return {"title": title, "body": body, "row_kind": "output"}


def _citations(n: int = 3) -> list[dict[str, Any]]:
    return [
        {"marker": f"[{i}]", "signal_id": str(uuid4()), "title": f"cited row {i}"}
        for i in range(1, n + 1)
    ]


def _verdict(report, needle: str):
    return next((cv for cv in report.claim_verdicts if needle in cv.text), None)


def _hard_fails(report) -> list[Any]:
    return [
        cv
        for cv in report.claim_verdicts
        if cv.reason == V._ABSENCE_SLICE_CONTRADICTED
    ]


# ---------------------------------------------------------------------------
# (a) TARGET SCOPE — the Benin-vs-South-Africa case
# ---------------------------------------------------------------------------

# The live violator row (leadership_transition, South Africa desk).
_BENIN_ROW = (
    "Benin is the latest African country to experience a coup. Here is a look "
    "at other military takeovers"
)


def test_benin_row_is_out_of_a_south_africa_claim_scope() -> None:
    """The adjudicated (a) case: an off-target row cannot violate the claim.

    Stage 1 strips the desk's OWN country tokens (every title on a country desk
    names the country, so they discriminate nothing) — which is exactly what let
    a Benin headline through as a violator of a South Africa claim.
    """
    scope = V._slice_scope_countries(
        "No coup attempts or elite defections were observed.",
        target_id="country_g20_za",
    )
    assert "south africa" in scope
    assert V._row_in_claim_scope(_BENIN_ROW, scope) is False
    # An ON-target row is untouched, and so is one naming no country at all.
    assert V._row_in_claim_scope("Pretoria cabinet reshuffle announced", scope) is True
    assert V._row_in_claim_scope("Ruling party congress opens", scope) is True


def test_scope_is_empty_and_inert_for_a_non_country_desk() -> None:
    """Fail-OPEN: an unmapped / non-country desk whose claim names nobody tells
    us nothing about scope, so every row stays eligible."""
    scope = V._slice_scope_countries("No new designations were observed.", target_id=None)
    assert scope == frozenset()
    assert V._row_in_claim_scope(_BENIN_ROW, scope) is True


def test_a_claim_that_enumerates_countries_scopes_itself() -> None:
    """The Americas region_composition shape: the clause's OWN enumeration is the
    scope, so a row about a country it does not name is off-scope."""
    claim = (
        "In contrast, Canada, Brazil, Haiti, and Mexico each find no coordinated "
        "narrative across their media environments [[ref:2]][[ref:4]]."
    )
    scope = V._slice_scope_countries(claim, target_id="region_americas")
    assert {"canada", "brazil", "haiti", "mexico"} <= scope
    assert V._row_in_claim_scope("Argentina peso slides after capital controls", scope) is False
    assert V._row_in_claim_scope("Mexico City march draws thousands", scope) is True


def test_irregular_demonyms_count_as_naming_the_country() -> None:
    """"Spanish enclave Ceuta" names Spain; "Italian troop movements" names Italy."""
    assert V._names_country("spain", "migrants at the spanish enclave of ceuta")
    assert V._names_country("italy", "no confirmed italian troop deployments")
    assert V._names_country("haiti", "three haitian gang financiers sanctioned")
    assert not V._names_country("spain", "no material change was observed")


async def test_off_target_row_no_longer_earns_a_hard_fail(monkeypatch) -> None:
    """END-TO-END (a): the Benin row is filtered out of the violator set, so the
    stage-2 call is never even shown it."""
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "1")
    claim = (
        "- No new coup attempts, elite defections, or forced resignations were "
        "observed in the collected reporting [1]."
    )
    judge = _StubJudge(
        slice_payload={"verdicts": ["contradicted"], "quotes": [_BENIN_ROW]}
    )
    report = await verify_finding_faithfulness(
        body=f"{claim}\n",
        citations=_citations(),
        judge_llm=judge,
        target_id="country_g20_za",
        slice_conn=_SliceConn(
            [
                _signal(_BENIN_ROW),
                _signal("South Africa: cabinet holds routine budget session"),
                _signal("Johannesburg transport strike talks continue"),
            ]
        ),
        run_id=uuid4(),
    )
    assert not _hard_fails(report)
    assert report.counters["absence_slice_off_scope_rows_excluded"] == 1
    if judge.slice_prompts:
        assert "Benin" not in judge.slice_prompts[0]


# ---------------------------------------------------------------------------
# (b) COMPOSITION BODIES, NOT TITLES — the Mexico case
# ---------------------------------------------------------------------------

_MEXICO_TITLE = "Mexico – Narrative Coordination and Economic Coercion Assessment"
_MEXICO_BODY = (
    "**BLUF:** Mexico's media environment is fragmented and organic; no "
    "coordinated narrative campaign is evident in the collected signals. "
    "Coverage of the US-Iran conflict is carried independently by several "
    "outlets with no shared framing."
)


async def test_composition_slice_is_screened_by_body_not_title(monkeypatch) -> None:
    """The adjudicated (b) case: the row's TITLE names the topic ("Narrative
    Coordination"), its BODY says the opposite of a violation."""
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "1")
    rows = await load_absence_slice_rows(
        _SliceConn([_output(_MEXICO_TITLE, _MEXICO_BODY)]), uuid4()
    )
    assert rows is not None and len(rows) == 1
    assert rows[0].kind == "output"
    assert rows[0].text.startswith("**BLUF:** Mexico's media environment")
    assert "Assessment" not in rows[0].text


async def test_composition_title_alone_no_longer_earns_a_hard_fail(monkeypatch) -> None:
    """END-TO-END (b): a stage-2 quote naming only the TITLE resolves against
    nothing now that the row is shown as its body — it decides nothing."""
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "1")
    claim = (
        "In contrast, Canada, Brazil, Haiti, and Mexico each find no coordinated "
        "narrative across their media environments, describing fragmented and "
        "organic coverage instead [[ref:2]][[ref:4]]."
    )
    judge = _StubJudge(
        slice_payload={"verdicts": ["contradicted"], "quotes": [_MEXICO_TITLE]}
    )
    report = await verify_finding_faithfulness(
        body=f"{claim}\n",
        citations=[
            {"ref_kind": "finding", "ordinal": n, "ref_id": str(uuid4()),
             "evidence_text": f"sub-claim {n}"}
            for n in (2, 4)
        ],
        judge_llm=judge,
        target_id="region_americas",
        slice_conn=_SliceConn(
            [
                _output(_MEXICO_TITLE, _MEXICO_BODY),
                _output("Canada – Narrative Coordination", "No coordinated narrative."),
                _output("Brazil – Narrative Coordination", "Coverage is organic."),
            ]
        ),
        run_id=uuid4(),
    )
    assert not _hard_fails(report)
    assert report.counters.get("absence_slice_unresolved")


def test_a_body_quote_resolves_but_a_short_fragment_never_does() -> None:
    """A composed row is SHOWN as a body excerpt, so the violating quote is a
    verbatim run out of it — resolved by containment, at the V-D quote floor."""
    shown = [_MEXICO_BODY]
    assert V._resolve_violating_row(
        "no coordinated narrative campaign is evident", shown
    ) == _MEXICO_BODY
    assert V._resolve_violating_row("Mexico", shown) is None
    assert V._resolve_violating_row("a campaign we invented wholesale", shown) is None
    # Exact whole-row match — the pre-W1 TITLE behavior — still resolves.
    assert V._resolve_violating_row(_MEXICO_TITLE, [_MEXICO_TITLE]) == _MEXICO_TITLE


# ---------------------------------------------------------------------------
# (c) MACHINE-STRUCTURED ROWS — the Japan CAMEO case
# ---------------------------------------------------------------------------


def test_cameo_event_codings_are_recognized_as_machine_structured() -> None:
    """The adjudicated (c) case + its siblings from the same ledger."""
    for title in (
        "COLLEGE: protest in Japan",
        "STUDENT <-> JAPANESE: protest in Kyoto, Kyoto, Japan",
        "SAUDI <-> SANAA: protest in Saudi Arabia",
        "GOMA: exert coercion / show force posture in Kinshasa, Kinshasa, DRC",
        "POPULATION <-> JEWISH: reduce relations in Israel",
    ):
        assert V._is_machine_structured_row(
            source_id="", provenance_kind="", title=title
        ), title
    # REAL headlines — including the ALL-CAPS-acronym opener — are NOT excluded.
    for title in (
        "Blast during protest in Swat injures several , fatalities feared : police",
        "UN: Sudan famine declared in two more districts",
        "TN to withdraw cases against students booked during NEET protests",
        "Tibetan activists protest China ethnic unity law at bank",
    ):
        assert not V._is_machine_structured_row(
            source_id="", provenance_kind="", title=title
        ), title


def test_machine_rows_are_recognized_by_source_and_provenance_too() -> None:
    """Source id and the raw-provenance kind stamp are independent markers, so a
    renamed feed cannot reopen the hole; the GDELT DOC API (real headlines) is
    deliberately NOT in either set."""
    assert V._is_machine_structured_row(
        source_id="source.gdelt.files", provenance_kind="", title="A real headline"
    )
    assert V._is_machine_structured_row(
        source_id="", provenance_kind="gdelt_files", title="A real headline"
    )
    assert not V._is_machine_structured_row(
        source_id="source.gdelt.doc_api", provenance_kind="", title="A real headline"
    )


async def test_cameo_protest_row_no_longer_hard_fails_a_no_protests_claim(
    monkeypatch,
) -> None:
    """END-TO-END (c): the live Japan case."""
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "1")
    claim = (
        "- A magnitude-7.1 earthquake in Kumamoto caused dozens of deaths, "
        "widespread damage and water shortages, but no reports of new protests, "
        "strikes, or government crackdowns [1][2]."
    )
    judge = _StubJudge(
        slice_payload={
            "verdicts": ["contradicted"],
            "quotes": ["COLLEGE: protest in Japan"],
        }
    )
    report = await verify_finding_faithfulness(
        body=f"{claim}\n",
        citations=_citations(),
        judge_llm=judge,
        target_id="country_g20_jp",
        slice_conn=_SliceConn(
            [
                _signal("COLLEGE: protest in Japan", source_id="source.gdelt.files"),
                _signal("Japan quake relief effort widens in Kumamoto"),
                _signal("Japan water restoration crews reach outlying towns"),
            ]
        ),
        run_id=uuid4(),
    )
    assert not _hard_fails(report)
    assert report.counters["absence_slice_machine_rows_excluded"] == 1


# ---------------------------------------------------------------------------
# (d) CARVE-OUTS — the Italy Schengen and Niger below-floor cases
# ---------------------------------------------------------------------------


def test_italy_schengen_carve_out_is_extracted() -> None:
    """The adjudicated (d) case: the claim EXEMPTS the existing measures, and the
    exempted measure was read back as its violation."""
    claim = (
        "- The situation is unchanged; no new material escalation beyond the "
        "existing diplomatic and border measures [83]"
    )
    carve = V._absence_carve_outs(claim)
    assert any("beyond the existing diplomatic and border measures" in c for c in carve)


def test_niger_below_floor_qualifier_is_extracted() -> None:
    """The adjudicated (d) sibling: 'no CONFIRMED changes … GIVEN the
    low-confidence, below-floor signals' — contradicted by the below-floor
    signal the claim itself discounts."""
    claim = (
        "Judgment: No confirmed changes in military capability or economic "
        "coercion are evident, given the low-confidence, below-floor signals "
        "from the military_posture and economic_coercion units (low confidence)."
    )
    carve = V._absence_carve_outs(claim)
    assert any("below-floor signals" in c for c in carve)


def test_a_claim_that_carves_nothing_out_yields_no_clauses() -> None:
    assert V._absence_carve_outs("No new sanctions designations were observed [1].") == []


async def test_carve_outs_ride_into_the_stage_two_prompt(monkeypatch) -> None:
    """END-TO-END (d): the exemption is stated to the judge as an exemption."""
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "1")
    claim = (
        "- No major new price spikes or collapses are noted except a downward "
        "adjustment in retail fuel rates [1]."
    )
    judge = _StubJudge(slice_payload={"verdicts": ["supported"], "quotes": [""]})
    await verify_finding_faithfulness(
        body=f"{claim}\n",
        citations=_citations(),
        judge_llm=judge,
        target_id="country_g20_in",
        slice_conn=_SliceConn(
            [
                _signal("Petrol , diesel prices today : fuel rates cut in Delhi"),
                _signal("India refinery maintenance schedule published"),
                _signal("Fuel import volumes steady, ministry says"),
            ]
        ),
        run_id=uuid4(),
    )
    assert judge.slice_calls == 1
    prompt = judge.slice_prompts[0]
    assert "CARVE-OUTS this claim already exempts" in prompt
    assert "except a downward adjustment in retail fuel rates" in prompt


def test_the_stage_two_rubric_states_both_override_rules() -> None:
    """The carve-out and epistemic-qualifier rules are the two the readout's
    (d) class needs, and they must OVERRIDE the topical-collision reading."""
    system = V._ABSENCE_SLICE_JUDGE_SYSTEM
    assert "CARVE-OUTS" in system
    assert "EPISTEMIC QUALIFIERS" in system
    assert "below the verification floor" in system
    assert "bill under consideration" in system


# ---------------------------------------------------------------------------
# (e) ROUTE — volume / continuity / trajectory are not slice-checkable
# ---------------------------------------------------------------------------


def test_volume_reads_do_not_route_through_the_slice_branch() -> None:
    """The live case: a desk-baseline VOLUME read's truthmaker is a metric, not a
    slice row."""
    claim = (
        "The volume is within the desk baseline (signal_volume_24h = 17.0, within "
        "normal band [84]), and the situation aligns with the existing "
        "open-situation register with no material change."
    )
    assert V._absence_route_exclusion(claim) == "volume"


def test_a_prior_read_frame_that_governs_the_negative_is_excluded() -> None:
    """The live case: 'Compared with the prior read … that asserted no material
    change, the confirmed strike represents a material INCREASE' — the claim's
    own assertion is positive."""
    claim = (
        "JUDGMENT: Compared with the prior composition read (12 h earlier) that "
        "asserted no material change in the U.S. escalation posture toward Iran, "
        "the confirmed strike and public threat represent a material increase in "
        "escalation risk [[ref:8]][[ref:1]]."
    )
    assert V._absence_route_exclusion(claim) == "continuity"


def test_a_trailing_continuity_corroboration_stays_on_the_route() -> None:
    """THE PASS-SIDE KEEP-TEST for (e): a genuine scoped negative that merely
    corroborates against the prior read in its TAIL is exactly the class V-B
    exists to verify. Only a frame that PRECEDES the negative takes a claim off
    the route."""
    claim = (
        "- No reports of new mass protests, strikes, riots or other popular unrest "
        "appear in the current signal set, consistent with prior assessment [121]."
    )
    assert V._absence_route_exclusion(claim) is None


def test_a_labelled_trajectory_line_carrying_a_real_negative_stays_on_route() -> None:
    """The narrow trajectory rule must not swallow a scaffold label whose content
    IS a slice-checkable negative (measured: the bare-word form would have pulled
    9.6% of the live verified passes off a route deciding them correctly)."""
    claim = (
        "- **Near-term trajectory:** steady - with no new energy-related events "
        "observed, the situation is likely to remain at low pressure."
    )
    assert V._absence_route_exclusion(claim) is None


def test_a_trajectory_whose_complement_is_the_negative_is_excluded() -> None:
    claim = (
        "Consequently the most plausible near-term trajectory is no new "
        "leadership change over the coming quarter."
    )
    assert V._absence_route_exclusion(claim) == "trajectory"


def test_a_leading_premise_clause_is_excluded() -> None:
    """'Given the absence of X, [judgement]' — the negative is a PREMISE, and
    letting it decide the claim would certify the judgement it supports (the same
    anti-laundering asymmetry V-C's metadata-dominance rule encodes)."""
    claim = (
        "Assessed: Given the absence of any new credible reports of elite "
        "defections or coup attempts, the probability of a leadership transition "
        "is assessed as low."
    )
    assert V._absence_route_exclusion(claim) == "subordinate"
    # A LEADING negative that IS the assertion is not a premise.
    assert (
        V._absence_route_exclusion(
            "While no new confirmed large-scale supply disruption is present in "
            "the current signal set, the threat environment remains live."
        )
        is None
    )


async def test_an_excluded_route_makes_no_slice_call_and_is_counted(
    monkeypatch,
) -> None:
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "1")
    claim = (
        "The volume is within the desk baseline (signal_volume_24h = 17.0, within "
        "normal band [1]) with no new material change."
    )
    judge = _StubJudge(
        slice_payload={"verdicts": ["contradicted"], "quotes": ["Italy tightens borders"]}
    )
    report = await verify_finding_faithfulness(
        body=f"{claim}\n",
        citations=_citations(),
        judge_llm=judge,
        target_id="country_g20_it",
        slice_conn=_SliceConn([_signal("Italy tightens borders with Spain")]),
        run_id=uuid4(),
    )
    assert report.counters["absence_slice_route_excluded"] == 1
    assert judge.slice_calls == 0
    assert not _hard_fails(report)


# ---------------------------------------------------------------------------
# (f) SLICE-SIZE HONESTY
# ---------------------------------------------------------------------------


async def test_a_thin_slice_says_so_instead_of_reading_as_verification(
    monkeypatch,
) -> None:
    """The readout's finding: 3/24 sampled passes 'verified' against a 1-row
    slice with a detail string that read as strong verification."""
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "1")
    claim = "- No new sanctions designations were observed in the collected reporting [1]."
    report = await verify_finding_faithfulness(
        body=f"{claim}\n",
        citations=_citations(),
        judge_llm=_StubJudge(),
        target_id="country_watch_ht",
        slice_conn=_SliceConn([_signal("Haiti: humanitarian corridors reopen")]),
        run_id=uuid4(),
    )
    cv = _verdict(report, "sanctions designations")
    assert cv is not None and cv.verdict == VERDICT_SUPPORTED
    detail = cv.detail or ""
    assert "THIN 1-row" in detail
    assert "not strong verification" in detail or "too few rows" in detail
    assert report.counters["absence_slice_thin"] == 1


async def test_an_empty_eligible_slice_never_reads_as_verified(monkeypatch) -> None:
    """The extreme of (f), and the B3 honesty rule one filter further down: a
    screen that ran over ZERO eligible rows finds no collision BY CONSTRUCTION.
    A slice emptied by the machine-row / scope filters verifies nothing."""
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "1")
    claim = "- No new protests or strikes were observed in the collected reporting [1]."
    report = await verify_finding_faithfulness(
        body=f"{claim}\n",
        citations=_citations(),
        judge_llm=_StubJudge(),
        target_id="country_g20_jp",
        # Every row is a CAMEO event coding — the eligible set is empty.
        slice_conn=_SliceConn(
            [
                _signal("COLLEGE: protest in Japan", source_id="source.gdelt.files"),
                _signal("WORKER: protest in Osaka, Japan", source_id="source.gdelt.files"),
            ]
        ),
        run_id=uuid4(),
    )
    assert "absence_slice_verified" not in report.counters
    assert report.counters["absence_slice_no_eligible_rows"] == 1
    assert report.counters["absence_slice_unresolved"] == 1
    assert not _hard_fails(report)


async def test_a_real_empty_input_slice_verifies_nothing(monkeypatch) -> None:
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "1")
    report = await verify_finding_faithfulness(
        body="- No new sanctions designations were observed [1].\n",
        citations=_citations(),
        judge_llm=_StubJudge(),
        target_id="country_watch_ht",
        slice_conn=_SliceConn([]),
        run_id=uuid4(),
    )
    assert "absence_slice_verified" not in report.counters
    assert report.counters["absence_slice_no_eligible_rows"] == 1


async def test_a_healthy_slice_reports_its_size_without_the_thin_caveat(
    monkeypatch,
) -> None:
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "1")
    claim = "- No new sanctions designations were observed in the collected reporting [1]."
    report = await verify_finding_faithfulness(
        body=f"{claim}\n",
        citations=_citations(),
        judge_llm=_StubJudge(),
        target_id="country_watch_ht",
        slice_conn=_SliceConn(
            [
                _signal("Haiti: humanitarian corridors reopen"),
                _signal("Haiti transitional council names interior minister"),
                _signal("Port-au-Prince fuel deliveries resume"),
            ]
        ),
        run_id=uuid4(),
    )
    cv = _verdict(report, "sanctions designations")
    assert cv is not None and cv.verdict == VERDICT_SUPPORTED
    assert "3-row" in (cv.detail or "")
    assert "THIN" not in (cv.detail or "")
    assert "absence_slice_thin" not in report.counters


# ---------------------------------------------------------------------------
# The branch still BITES — precision, not disarmament
# ---------------------------------------------------------------------------


async def test_a_genuine_on_target_violation_is_still_a_hard_fail(
    monkeypatch,
) -> None:
    """The operator decision was NO interim demotion: a real violator — on
    target, real reporting, not carved out — still earns the hard fail."""
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "1")
    violator = "US Treasury adds three Haitian gang financiers to the sanctions list"
    claim = (
        "- None of the recent signals report new or tightened sanctions "
        "designations affecting Haiti or its entities [1][2][3]."
    )
    judge = _StubJudge(
        slice_payload={"verdicts": ["contradicted"], "quotes": [violator]}
    )
    report = await verify_finding_faithfulness(
        body=f"{claim}\n",
        citations=_citations(),
        judge_llm=judge,
        target_id="country_watch_ht",
        slice_conn=_SliceConn(
            [
                _signal(violator),
                _signal("Haiti: humanitarian corridors reopen in the capital"),
                _signal("Haiti transitional council names an interior minister"),
            ]
        ),
        run_id=uuid4(),
    )
    cv = _verdict(report, "tightened sanctions")
    assert cv is not None and cv.verdict == FAIL_CLASS_HARD
    assert cv.reason == V._ABSENCE_SLICE_CONTRADICTED
    assert "Treasury adds three Haitian gang financiers" in (cv.detail or "")


# ---------------------------------------------------------------------------
# V-G2 (2026-08-03) — CONTINUITY CLAIMS LEAVE THE ROUTE.
#
# The 08-02 readout pre-registered this class for router removal. The 08-03
# re-run measured it still routed: 107 of 704 V-B claims (15.2%) matching
# "no material change" / "prior read" / "remains at the level", producing 9 of
# the 15 surviving absence hard fails. The type specimen is `hard_fail#9` below —
# and its "violator" was one of the finding's OWN AGREEING citations.
# ---------------------------------------------------------------------------

_HARD_FAIL_9_CLAIM = (
    "No material change since the prior world read of "
    "2026-08-03T00:00:15.278605+00:00 [[ref:7]]."
)


async def test_hard_fail_9_the_world_read_continuity_claim_leaves_the_route(
    monkeypatch,
) -> None:
    """A DIFF against a previous assessment is not decidable from a slice row.

    A sibling desk's BLUF describes the CURRENT state; it cannot establish or
    refute a change relative to a PRIOR one. The check is structurally incapable
    of adjudicating this shape, so the honest move is to exempt it — it grades on
    citation support like any other claim.
    """
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "1")
    judge = _StubJudge(
        slice_payload={
            "verdicts": ["contradicted"],
            "quotes": ["Escalation composition — three hotspots remain active"],
        }
    )
    report = await verify_finding_faithfulness(
        body=f"{_HARD_FAIL_9_CLAIM}\n",
        citations=_citations(),
        judge_llm=judge,
        slice_conn=_SliceConn(
            [
                _output(
                    "Escalation composition — global",
                    "Escalation composition — three hotspots remain active",
                )
            ]
        ),
        run_id=uuid4(),
    )
    assert report.counters["absence_slice_route_excluded"] == 1
    assert report.counters["absence_slice_route_excluded_continuity_claim"] == 1
    assert judge.slice_calls == 0, "no slice call should be made at all"
    assert not _hard_fails(report)


def test_the_continuity_claim_test_is_not_positional() -> None:
    """W1(e)'s frame test is positional — which is why the class survived it.

    Every shape below leads with the negative, so the prior-read referent TRAILS
    it and the positional test cannot reach any of them.
    """
    shapes = [
        _HARD_FAIL_9_CLAIM,
        "No material change in the physical flow through the Strait of Hormuz.",
        "Escalation risk remains at the level identified in the prior read.",
        "No new change in posture relative to the previous assessment.",
        "Force posture is unchanged since the prior read.",
        "No deviation from the prior read is visible in force posture.",
    ]
    for claim in shapes:
        assert V._absence_route_exclusion(claim) == "continuity_claim", claim


def test_the_positional_frame_class_still_reports_itself_separately() -> None:
    """The two continuity classes stay distinguishable in the receipts."""
    assert (
        V._absence_route_exclusion(
            "Compared with the prior read, the confirmed strike is a material increase"
        )
        == "continuity"
    )


def test_real_scoped_negatives_stay_on_the_route() -> None:
    """The expensive error is pulling a working check off its own traffic.

    A negative that merely MENTIONS a prior read in passing carries no negated
    CHANGE noun and is exactly what V-B exists to verify — as is every negative
    scoped to the collected signal set.
    """
    keepers = [
        "No reports of mass protests appear in the current signal set, consistent "
        "with prior assessment [121]",
        "None of the 25 recent signals report new or tightened sanctions "
        "designations affecting Haiti",
        "No new large-scale outages were reported in the collected reporting",
        "**Near-term trajectory:** steady — with no new energy events observed",
        "No confirmed new mobilizations were observed this window",
    ]
    for claim in keepers:
        assert V._absence_route_exclusion(claim) is None, claim


def test_r2_reaches_the_qualifier_forms_the_producers_actually_emit() -> None:
    """`hard_fail#9` missed W2/R2 on the READ arm's two fixed bigrams."""
    for framing in (
        "since the prior world read of 2026-08-03T00:00:15Z",
        "unchanged from the prior verified read",
        "the previous composition read concluded otherwise",
        "the prior read noted a moderate stance",
    ):
        assert V._CLAIM_CITES_PRIOR_READ_RE.search(framing), framing


# ---------------------------------------------------------------------------
# V-G3 (2026-08-03) — SCALE QUALIFIERS. "Dozens" is the NEGATION of "mass".
#
# `hard_fail#3` and its twin on a second Canada desk: "No evidence of MASS
# protests…" violated by "DOZENS protest Meta plan for MASSIVE data centre in
# Morinville." The row is the claim's own evidence, and the lexical decoy is
# right there in the string — "massive" sits two words from "protest", modifying
# the data centre.
# ---------------------------------------------------------------------------

_MASS_PROTEST_CLAIM = (
    "- No evidence of mass protests, state-led crackdowns, security-force "
    "defections, or elite/regime fractures was found in the current signal set "
    "[1][6][7]."
)
_MORINVILLE_ROW = (
    "Dozens protest Meta plan for massive data centre in Morinville"
)


async def test_hard_fail_3_dozens_does_not_violate_a_no_mass_protests_claim(
    monkeypatch,
) -> None:
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "1")
    judge = _StubJudge(
        slice_payload={"verdicts": ["contradicted"], "quotes": [_MORINVILLE_ROW]}
    )
    report = await verify_finding_faithfulness(
        body=f"{_MASS_PROTEST_CLAIM}\n",
        citations=_citations(),
        judge_llm=judge,
        target_id="country_g20_ca",
        slice_conn=_SliceConn([_signal(_MORINVILLE_ROW)]),
        run_id=uuid4(),
    )
    assert judge.slice_calls == 1, "the screen must still collide and ask"
    assert report.counters["absence_slice_scale_undershoot"] == 1
    assert not _hard_fails(report), "a smaller-scale report is not a violation"
    assert V._ABSENCE_SLICE_CONTRADICTED not in report.counters


async def test_the_scale_the_claim_denies_rides_into_the_stage_two_prompt(
    monkeypatch,
) -> None:
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "1")
    judge = _StubJudge(slice_payload={"verdicts": ["supported"], "quotes": [""]})
    await verify_finding_faithfulness(
        body=f"{_MASS_PROTEST_CLAIM}\n",
        citations=_citations(),
        judge_llm=judge,
        target_id="country_g20_ca",
        slice_conn=_SliceConn([_signal(_MORINVILLE_ROW)]),
        run_id=uuid4(),
    )
    prompt = judge.slice_prompts[0]
    assert "SCALE this claim denies: 'mass'" in prompt
    assert "SMALLER scale" in prompt


async def test_a_genuine_mass_scale_row_is_still_a_hard_fail(monkeypatch) -> None:
    """The keep-test: the guard must not launder a real large-scale violation."""
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "1")
    row = "Mass protests sweep Ottawa as thousands march on parliament"
    judge = _StubJudge(
        slice_payload={"verdicts": ["contradicted"], "quotes": [row]}
    )
    report = await verify_finding_faithfulness(
        body=f"{_MASS_PROTEST_CLAIM}\n",
        citations=_citations(),
        judge_llm=judge,
        target_id="country_g20_ca",
        slice_conn=_SliceConn([_signal(row)]),
        run_id=uuid4(),
    )
    cv = _verdict(report, "mass protests")
    assert cv is not None and cv.verdict == FAIL_CLASS_HARD
    assert cv.reason == V._ABSENCE_SLICE_CONTRADICTED
    assert "absence_slice_scale_undershoot" not in report.counters


def test_the_undershoot_guard_needs_all_three_conditions() -> None:
    terms = V._absence_content_terms(_MASS_PROTEST_CLAIM, target_id="country_g20_ca")
    assert V._scale_undershoots_claim("mass", _MORINVILLE_ROW, terms) is True
    # (1) NOVELTY / EPISTEMIC qualifiers are untouched — one new sanction still
    #     violates "no new sanctions".
    assert V._scale_undershoots_claim("new", _MORINVILLE_ROW, terms) is False
    assert V._scale_undershoots_claim("confirmed", _MORINVILLE_ROW, terms) is False
    # (2) no small-quantity marker at all.
    assert (
        V._scale_undershoots_claim(
            "mass", "Mass protests sweep Ottawa as thousands march", terms
        )
        is False
    )
    # (3) the ADJACENCY window is what separates the signal from the decoy: the
    #     quantity must bind to one of the CLAIM'S OWN subjects.
    assert (
        V._scale_undershoots_claim(
            "mass", "Meta unveils massive data centre plan in Morinville", terms
        )
        is False
    )
    assert (
        V._scale_undershoots_claim(
            "mass", "Three of the ten provinces raised their carbon levy", terms
        )
        is False
    )
