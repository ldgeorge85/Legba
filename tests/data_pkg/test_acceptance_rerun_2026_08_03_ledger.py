# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The 08-03 panel's adjudicated rows, as an OFFLINE REPLAY ledger.

The acceptance protocol is a 30-claim human panel over a stamped day of live
critiques; it cannot run in CI. What CAN run in CI is every row that panel
DISAGREED with, reproduced from the adjudication file (§5) closely enough to
exercise the mechanism that decided it. That is what this module is: one entry
per adjudicated disagreement, plus the pass-side miss, each naming its row id,
its class, and which train resolves it.

Its job is to be TRUTHFUL rather than green. A row no train has fixed carries
``xfail(strict=True)``, which means:

  * the assertion states the CORRECT verdict, not the current one, so the file
    reads as a specification and never as an endorsement of the defect;
  * the day someone fixes it, the strict xfail turns RED and forces this ledger
    to be updated in the same commit. An owed fix cannot quietly become done and
    unrecorded, and a done fix cannot quietly stay marked owed.

That contract has now been exercised end to end. V-G shipped with six rows
resolved and five marked owed; the V-H residuals train (2026-08-04/1) turned four
of those five red and this file green again in the same commits — V-H1 the outlet
in the judge's citation view, V-H2 the undecorated watch heading, V-H3 the
evidence-bearing metadata arm, V-H4 the enumerated-denial scope miss.

ONE row is still owed, and it is the one that cannot be engineered away:
``soft_fail#7`` is judge quality, "clean judge error, no structural excuse" in
the panel's own words, and spec §4 puts judge-model changes out of scope. It
stays here so the next panel sees it counted rather than quietly dropped.

Two SIBLING residuals live next door in ``test_verify_denied_scope.py`` — the
restatement shapes V-H5 did not close — because they were never adjudicated rows
of their own and this file's count is one entry per adjudicated row.

Row ids are the adjudication file's own (``hard_fail#N`` / ``soft_fail#N`` /
``supported#N``), so a reader can go from a test name to the evidence table
without a search.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

import pytest

import legba.data.provenance.verify as V
from legba.data.provenance.verify import (
    FAIL_CLASS_HARD,
    FAIL_CLASS_SOFT,
    VERDICT_SUPPORTED,
    verify_finding_faithfulness,
)


class _Response:
    def __init__(self, content: str) -> None:
        self.content = content
        self.usage = None


class _Judge:
    """Canned verdicts; the SLICE rubric gets its own payload when supplied."""

    subprovider = "stub-judge"

    def __init__(
        self,
        payload: dict[str, Any] | None = None,
        *,
        slice_payload: dict[str, Any] | None = None,
    ) -> None:
        self._payload = payload
        self._slice = slice_payload
        self.slice_calls = 0

    async def chat_complete(
        self, messages, *, max_tokens=None, temperature=None, system=None, **kw
    ):
        import re

        if system == V._ABSENCE_SLICE_JUDGE_SYSTEM:
            self.slice_calls += 1
            return _Response(json.dumps(self._slice or {}))
        if self._payload is not None:
            return _Response(json.dumps(self._payload))
        n = len(re.findall(r"^\d+\.\s", messages[0]["content"], re.MULTILINE)) or 1
        return _Response(json.dumps({"verdicts": ["supported"] * n, "quotes": [""] * n}))


class _SliceConn:
    def __init__(self, rows: list[dict[str, str]]) -> None:
        self._rows = rows

    async def fetchrow(self, sql: str, *a: Any):
        if "analyst_traces" in sql:
            return {"input_row_refs": [uuid4() for _ in self._rows]}
        return None

    async def fetch(self, sql: str, *a: Any):
        return [
            {
                "title": r.get("title", ""),
                "body": r.get("body", ""),
                "source_id": r.get("source_id", "source.wire"),
                "provenance_kind": "",
                "row_kind": r.get("row_kind", "signal"),
            }
            for r in self._rows
        ]


def _signal_citations(*titles: str) -> list[dict[str, Any]]:
    return [
        {"marker": f"[{i}]", "signal_id": str(uuid4()), "title": t}
        for i, t in enumerate(titles, start=1)
    ]


def _verdict(report, needle: str):
    return next((cv for cv in report.claim_verdicts if needle in cv.text), None)


@pytest.fixture(autouse=True)
def _judge_on(monkeypatch):
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "1")


# ===========================================================================
# HARD STRATUM — 5 disagreements
# ===========================================================================


async def test_hard_fail_3_internal_stability_ca_scale_qualifier() -> None:
    """"No evidence of MASS protests" vs "DOZENS protest … MASSIVE data centre".

    RESOLVED by V-G3: dozens is the negation of mass, and the adjacency window
    separates the quantity that binds to the claim's subject from the lexical
    decoy two words away. Counted absence_slice_scale_undershoot.
    """
    claim = (
        "- No evidence of mass protests, state-led crackdowns, security-force "
        "defections, or elite/regime fractures was found in the current signal "
        "set [1]."
    )
    row = "Dozens protest Meta plan for massive data centre in Morinville"
    judge = _Judge(slice_payload={"verdicts": ["contradicted"], "quotes": [row]})
    report = await verify_finding_faithfulness(
        body=f"{claim}\n",
        citations=_signal_citations("Ottawa budget briefing"),
        judge_llm=judge,
        target_id="country_g20_ca",
        slice_conn=_SliceConn([{"title": row}]),
        run_id=uuid4(),
    )
    assert report.counters["absence_slice_scale_undershoot"] == 1
    assert V._ABSENCE_SLICE_CONTRADICTED not in report.counters


async def test_hard_fail_4_escalation_de_prior_read_and_carve_out() -> None:
    """The Germany BLUF, "refuted" by its own prior read's editorial phrasing.

    RESOLVED by V-G1: the quote resolves only inside an analyst finding the claim
    never cited, so the hard class is not earned. (V-G3 independently covers the
    carve-out half on the judge path — see test_verify_signals_only_refutation.)
    """
    prior = (
        "PRIOR READ (this target's previous verified read): Germany – Border "
        "Control Extension\nBerlin extended internal border controls, confirming "
        "a concrete security action rather than mere rhetoric."
    )
    body = "Federal police reported routine checks at three crossings [12].\n"
    citations = [
        {
            "marker": "[12]",
            "signal_id": str(uuid4()),
            "title": "Germany to prolong border controls amid Ceuta migrant crisis",
        },
        {
            "marker": "[47]",
            "ref_kind": "prior_read",
            "title": "Germany – Border Control Extension",
            "evidence_text": prior,
        },
    ]
    judge = _Judge(
        {
            "verdicts": ["contradicted"],
            "quotes": ["confirming a concrete security action rather than mere rhetoric"],
        }
    )
    report = await verify_finding_faithfulness(
        body=body, citations=citations, judge_llm=judge, target_id="country_g20_de"
    )
    cv = _verdict(report, "routine checks")
    assert cv is not None and cv.verdict == FAIL_CLASS_SOFT
    assert cv.reason == "judge_prior_read_conflict"


async def test_hard_fail_5_disruption_status_lane_hormuz_anti_update() -> None:
    """The desk updates on four fresh attack signals; its own superseded BLUF
    is quoted back at it.

    RESOLVED by V-G1. Traced in the adjudication to
    7eb5a4f6-fb82-4a8f-b8cd-b384dae0f77e.
    """
    prior = (
        "PRIOR READ (this target's previous verified read): Hormuz Strait – "
        "Interdiction risk – Holding\nNo material change in the physical flow "
        "through the Strait of Hormuz; interdiction risk remains the dominant "
        "vector and the situation is holding steady."
    )
    body = (
        "BLUF: transit conditions through the Strait of Hormuz have DEGRADED "
        "from the prior holding assessment.\n"
        "A second LNG tanker was struck and charterers declared force majeure "
        "on twenty-four cargoes [13].\n"
    )
    citations = [
        {
            "marker": "[13]",
            "signal_id": str(uuid4()),
            "title": "Second LNG tanker struck off Bandar Abbas; force majeure on 24 cargoes",
        },
        {
            "marker": "[44]",
            "ref_kind": "prior_read",
            "title": "Hormuz Strait – Interdiction risk – Holding",
            "evidence_text": prior,
        },
    ]
    judge = _Judge(
        {
            "verdicts": ["contradicted", "supported"],
            "quotes": [
                "No material change in the physical flow through the Strait of Hormuz",
                "",
            ],
        }
    )
    report = await verify_finding_faithfulness(
        body=body, citations=citations, judge_llm=judge, target_id="lane_hormuz"
    )
    cv = _verdict(report, "DEGRADED")
    assert cv is not None and cv.verdict == FAIL_CLASS_SOFT
    assert report.counters["hardfail_demoted_prior_read"] == 1


async def test_hard_fail_8_economic_coercion_ar_quote_confirms_the_claim() -> None:
    """The quote CONFIRMS the claim's own second clause — it does not refute it.

    RESOLVED by V-H4. The claim ENUMERATES what it denies and the quote names
    none of those things in full: business and mortgage defaults are not
    sovereign default pressure, and inflation is not a currency crisis.

    The claim text is the LIVE row's, not the adjudication's shorthand — the
    enumeration IS the mechanism, so a paraphrase that drops it would test
    nothing. Traced to the `a9ff92e5` row of the counter audit's flag 5.
    """
    claim = (
        "- There are no reports of FX-reserve depletion, currency crises, SWIFT "
        "bans, or sovereign default pressures affecting Argentina; the economic "
        "commentary focuses on domestic growth challenges and reforms rather "
        "than external financial coercion [1]."
    )
    wire = (
        "The rest of Argentina continues to struggle with sluggish growth, high "
        "inflation, weak consumer spending, and business and mortgage defaults"
    )
    judge = _Judge(
        {"verdicts": ["contradicted"], "quotes": [wire]}
    )
    report = await verify_finding_faithfulness(
        body=f"{claim}\n",
        citations=_signal_citations(wire),
        judge_llm=judge,
        target_id="country_g20_ar",
    )
    cv = _verdict(report, "sovereign default")
    assert cv is not None and cv.verdict == FAIL_CLASS_SOFT
    assert cv.reason == "judge_contradicted_off_scope"
    assert report.counters["hardfail_demoted_off_denied_scope"] == 1



async def test_hard_fail_9_world_assessor_continuity_through_v_b() -> None:
    """A continuity DIFF decided against a slice row that is one of the finding's
    own agreeing citations.

    RESOLVED by V-G2: the claim leaves the slice route entirely.
    """
    claim = (
        "No material change since the prior world read of "
        "2026-08-03T00:00:15.278605+00:00 [[ref:7]]."
    )
    judge = _Judge(
        slice_payload={
            "verdicts": ["contradicted"],
            "quotes": ["Escalation composition — three hotspots remain active"],
        }
    )
    report = await verify_finding_faithfulness(
        body=f"{claim}\n",
        citations=_signal_citations("world read input"),
        judge_llm=judge,
        slice_conn=_SliceConn(
            [
                {
                    "title": "Escalation composition — global",
                    "body": "Escalation composition — three hotspots remain active",
                    "row_kind": "output",
                }
            ]
        ),
        run_id=uuid4(),
    )
    assert report.counters["absence_slice_route_excluded_continuity_claim"] == 1
    assert judge.slice_calls == 0


# ===========================================================================
# SOFT STRATUM — 5 disagreements
# ===========================================================================


async def test_soft_fail_2_narrative_coordination_id_outlet_attribution() -> None:
    """Six named outlets, all correct, graded unsupported: the judge could not see
    the ``source_id`` field that would let it check any of them.

    RESOLVED by V-H1: the outlet ref reaches the judge's evidence view on its own
    ``OUTLET:`` line, OUTSIDE the evidence cap, and the citation builder carries
    ``signals.source_id`` onto the citation so the field exists in production.
    """
    claim = (
        "Near-identical framing appeared across CBC, Hindustan Times, NPR, Al "
        "Jazeera, the BBC and ABC Australia [1][4][16][33][54][52]."
    )
    citations = [
        {
            "marker": f"[{n}]",
            "signal_id": str(uuid4()),
            "source_id": sid,
            "title": "Indonesia protest coverage",
        }
        for n, sid in (
            (1, "source.cbc.world"),
            (4, "source.hindustantimes.world"),
            (16, "source.npr.world"),
            (33, "source.aljazeera.world"),
            (54, "source.bbc.world"),
            (52, "source.abc_au.justin"),
        )
    ]
    evidence = V._marker_to_evidence(citations)
    assert any("source.cbc.world" in v for v in evidence.values()), (
        "the outlet must be visible in the judge's evidence view"
    )


async def test_soft_fail_4_energy_security_ml_watch_bullet_graded() -> None:
    """An "Indicators to watch" bullet, graded on citation support it cannot have.

    RESOLVED by V-H2, and the mechanism is narrower than the xfail supposed: the
    bullet did not "leak outside a heading" — its heading was the UNDECORATED
    ``Indicators to watch:`` line, which the producer half of the system has
    always treated as a heading and the verify half required markdown for. The
    body below is the live finding's own shape (``00cb3cb0``), so this asserts on
    the real leak and not a reconstruction of it.

    Deliberately NOT taken: a per-claim "verbless markerless bullet is a watch
    item" classifier. ``_is_forward_looking`` feeds the FLOOR as well as the
    judge, so widening it would stop an uncited present-fact bullet from being
    counted at all — the H1 regression the anchoring comments exist to prevent.
    """
    claim = (
        "- Escalated interdiction risk on a maritime chokepoint carrying Mali's "
        "energy imports."
    )
    body = (
        "**BLUF:** The desk faces elevated energy-security pressure [1].\n"
        "\n"
        "Key points:\n"
        "- Fuel-excise relief ends at midnight, removing a price cap [102].\n"
        "\n"
        "Indicators to watch:\n"
        "- Unplanned outage at an LNG terminal serving the country.\n"
        f"{claim}\n"
    )
    spans = V._segment_claims(body)
    assert not any("interdiction risk" in s for s in spans), spans
    assert not any("Unplanned outage" in s for s in spans), spans
    # The KEY POINTS bullet above it is untouched — the skip is the watch section,
    # not "everything after the first plain label".
    assert any("Fuel-excise relief" in s for s in spans), spans


async def test_soft_fail_6_country_composition_kr_metadata_dominance() -> None:
    """The metadata leg verifies and the residual IS the cited output's title.

    RESOLVED by V-H3. The gate stays shut on the residual ALONE — that is the
    anti-laundering rule and it is unchanged — and opens when the cited evidence
    COVERS the residual, which is the case the panel found
    (metadata_verified_not_dominant=18 vs metadata_verified=7).
    """
    claim = (
        "South Korea is neither target nor wielder of coercive economic "
        "pressure (effective_confidence 0.68) [[ref:6]]."
    )
    matched = "effective_confidence 0.68"
    title = "South Korea – No coercive economic pressure – neither target nor wielder."
    assert V._metadata_dominant(claim, matched, residual_evidence=title) is True
    # The anti-laundering asymmetry is intact: without the covering citation the
    # column still does not certify the prose around it.
    assert V._metadata_dominant(claim, matched) is False
    # …and coverage alone is not enough, because bag-of-words is blind to
    # negation: the OPPOSITE claim is word-for-word "covered" by the same title.
    opposite = (
        "South Korea is the target of coercive economic pressure "
        "(effective_confidence 0.68) [[ref:6]]."
    )
    assert V._metadata_dominant(opposite, matched, residual_evidence=title) is False


@pytest.mark.xfail(
    strict=True,
    reason=(
        "NOT FIXABLE STRUCTURALLY, and recorded rather than closed. The "
        "adjudication's own words: 'Clean judge error, no structural excuse.' "
        "The cited evidence states the claim verbatim — thrust 2,500 kN, "
        "U.S.-mainland-capable — and the judge marked it unsupported anyway. "
        "There is no scope bug to fix; it is judge quality, which spec section 4 "
        "puts out of scope for this train. It stays here so the next panel sees "
        "it counted rather than quietly dropped."
    ),
)
async def test_soft_fail_7_proliferation_watch_kp_clean_judge_error() -> None:
    claim = (
        "The engine's thrust increased to 2,500 kN, making it U.S.-mainland-"
        "capable [4]."
    )
    wire = (
        "the engine's maximum thrust is 2,500 kilonewtons, up from about 1,970 — "
        "an engine for weapons capable of reaching the U.S. mainland"
    )
    judge = _Judge({"verdicts": ["unsupported"], "quotes": [""]})
    report = await verify_finding_faithfulness(
        body=f"{claim}\n", citations=_signal_citations(wire), judge_llm=judge
    )
    cv = _verdict(report, "2,500 kN")
    assert cv is not None and cv.verdict == VERDICT_SUPPORTED


async def test_soft_fail_10_region_composition_europe_evidence_window() -> None:
    """A near-verbatim paraphrase of the cited BLUF, graded unsupported.

    RESOLVED by F-D: the cited sub-claim's body is captured at the UNIT judge's
    whole-evidence width (3,600) instead of 600, which bound on essentially every
    composition citation in production.
    """
    from legba.data.analysts import meta_findings_synthesizer as M

    bluf = (
        "Energy-security pressure on Russia has risen sharply due to recent "
        "Ukrainian drone strikes on oil refineries, adding a new high-impact "
        "pressure vector alongside a steady but modest military buildup."
    )
    body = ("Preamble the composition does not cite. " * 20) + bluf
    assert len(body) > 600, "the old cap would have cut before the BLUF"
    citation = M._build_composition_citation(
        2, {"id": uuid4(), "title": "Russia – energy security", "body": body}
    )
    assert citation is not None
    assert bluf in V._ordinal_evidence_map([citation])[2]


# ===========================================================================
# SUPPORTED STRATUM — the pass-side miss (gate = ZERO)
# ===========================================================================


async def test_supported_7_internal_stability_ar_pass_side_miss() -> None:
    """Two uncited world-knowledge premises, markers=[], passed clean.

    RESOLVED by V-G5 — and, as importantly, decided the SAME WAY as its
    byte-similar Indonesian sibling, which the same judge soft-failed 13 hours
    earlier on the same analyst. The panel's sensitivity note is that it could
    not credit both calls; there is now only one call to credit.
    """
    claim = (
        "Given Argentina's historical propensity for coups and its ongoing "
        "economic challenges, the combination of elite discord and nascent "
        "protest activity pushes the near-term trajectory toward destabilizing."
    )
    sibling = (
        "Indonesia's historical low coup incidence and the absence of broader "
        "elite fracture keep the near-term trajectory steady."
    )
    for text, target in ((claim, "country_g20_ar"), (sibling, "country_g20_id")):
        report = await verify_finding_faithfulness(
            body=f"{text}\n",
            citations=_signal_citations("Milei and Villarruel rupture deepens"),
            judge_llm=_Judge(),
            target_id=target,
        )
        cv = _verdict(report, "historical")
        assert cv is not None, text
        assert cv.verdict == FAIL_CLASS_SOFT, text
        assert cv.reason == "uncited_world_knowledge", text


# ===========================================================================
# The ledger's own shape
# ===========================================================================


def test_every_adjudicated_row_has_an_entry() -> None:
    """The panel adjudicated 10 disagreements plus one pass-side miss.

    TEN now resolve. ONE remains, and it is not a structural defect: the panel's
    own words on `soft_fail#7` are "clean judge error, no structural excuse".
    (The eleventh disagreement, hard_fail#1, the panel itself scored as an
    AGREEMENT on the fail/pass axis — "verdict right, reason wrong" — and is
    covered by its §7 sensitivity note rather than by a row of its own.) If a row
    is ever added to or dropped from the adjudication, this count is where the
    ledger says so.
    """
    import inspect
    import sys

    tests = [
        name
        for name in dir(sys.modules[__name__])
        if name.startswith("test_")
        and (name.startswith(("test_hard_fail_", "test_soft_fail_", "test_supported_")))
    ]
    assert len(tests) == 11, sorted(tests)  # 10 disagreements + the pass-side miss
    owed = [
        n
        for n in tests
        if any(
            m.name == "xfail"
            for m in getattr(
                getattr(sys.modules[__name__], n), "pytestmark", []
            )
        )
    ]
    assert len(owed) == 1, sorted(owed)
    assert inspect.getdoc(sys.modules[__name__])
