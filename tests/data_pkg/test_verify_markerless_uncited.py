# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""V-G5 (2026-08-03) — the MARKERLESS-UNCITED guard: the recurring pass-side miss.

The one acceptance gate that is supposed to be ZERO has failed on both runs, and
both misses share a shape the judge is *instructed* to wave through: the unit and
composition leads each say "A claim with NO [N] marker is a synthesis / framing /
severity / absence statement — mark it SUPPORTED unless it asserts a SPECIFIC
fact." Right for the 2,374 markerless claims a stamped day produces; a hole for
one class inside them.

``supported#7`` (08-03, internal_stability / country_g20_ar), ``markers=[]``::

    Given Argentina's historical propensity for coups and its ongoing economic
    challenges, the combination of elite discord and nascent protest activity
    pushes the near-term trajectory toward destabilizing.

Neither load-bearing premise appears in any cited row — the citations are a
Milei/Villarruel rupture, an indigenous land-bill protest and a week-in-review
digest. Both are uncited WORLD KNOWLEDGE injected as a premise: the
uncited-prior-leak shape that forced the world_context RAG rollback. It passed
clean, while thirteen hours earlier the same judge SOFT-FAILED the byte-similar
Indonesian shape on the same analyst. The panel's own sensitivity note is that the
judge scored the two oppositely and it could not credit both.

The guard is deterministic precisely so the two are decided the same way every
time. It is NOT "markerless claims fail": measured read-only over the stamped
day's 5,338 claim verdicts, 19 (0.36%) carry an uncited world baseline, and every
sibling of the two adjudicated rows is among them.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

import legba.data.provenance.verify as V
from legba.data.provenance.verify import (
    FAIL_CLASS_SOFT,
    VERDICT_SUPPORTED,
    verify_finding_faithfulness,
)


class _Response:
    def __init__(self, content: str) -> None:
        self.content = content
        self.usage = None


class _StubJudge:
    """Marks EVERYTHING supported — reproducing the pass-side miss exactly."""

    subprovider = "stub-judge"

    def __init__(self) -> None:
        self.calls = 0

    async def chat_complete(
        self, messages, *, max_tokens=None, temperature=None, system=None, **kw
    ):
        self.calls += 1
        import re

        n = len(re.findall(r"^\d+\.\s", messages[0]["content"], re.MULTILINE))
        return _Response(
            json.dumps({"verdicts": ["supported"] * max(n, 1), "quotes": [""] * max(n, 1)})
        )


def _citations() -> list[dict[str, Any]]:
    return [
        {
            "marker": "[3]",
            "signal_id": str(uuid4()),
            "title": "Milei and Villarruel rupture deepens over pension veto",
        },
        {
            "marker": "[7]",
            "signal_id": str(uuid4()),
            "title": "Indigenous groups protest land bill in Formosa",
        },
    ]


def _verdict(report, needle: str):
    return next((cv for cv in report.claim_verdicts if needle in cv.text), None)


_SUPPORTED_7 = (
    "Given Argentina's historical propensity for coups and its ongoing economic "
    "challenges, the combination of elite discord and nascent protest activity "
    "pushes the near-term trajectory toward destabilizing."
)
_SOFT_FAIL_9 = (
    "Indonesia's historical low coup incidence and the absence of broader elite "
    "fracture keep the near-term trajectory steady."
)


async def test_supported_7_the_argentina_shape_no_longer_passes_clean(
    monkeypatch,
) -> None:
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "1")
    judge = _StubJudge()
    report = await verify_finding_faithfulness(
        body=f"{_SUPPORTED_7}\n",
        citations=_citations(),
        judge_llm=judge,
        target_id="country_g20_ar",
    )
    assert judge.calls, "the judge still runs; the guard decides afterwards"
    cv = _verdict(report, "historical propensity")
    assert cv is not None
    assert cv.verdict == FAIL_CLASS_SOFT
    assert cv.reason == "uncited_world_knowledge"
    assert report.counters["claim_markerless_uncited"] == 1
    assert "no cited row supplies" in (cv.detail or "")


async def test_the_two_byte_similar_shapes_are_now_decided_the_same_way(
    monkeypatch,
) -> None:
    """The panel could not credit both calls; the guard makes them one call."""
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "1")
    for claim in (_SUPPORTED_7, _SOFT_FAIL_9):
        report = await verify_finding_faithfulness(
            body=f"{claim}\n", citations=_citations(), judge_llm=_StubJudge()
        )
        cv = _verdict(report, "historical")
        assert cv is not None and cv.reason == "uncited_world_knowledge", claim


async def test_a_cited_claim_is_left_to_the_judge(monkeypatch) -> None:
    """It cited something — the judge can grade THAT, and this guard steps out."""
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "1")
    claim = (
        "Given Argentina's historical propensity for coups [3], elite discord "
        "pushes the trajectory toward destabilizing [7]."
    )
    report = await verify_finding_faithfulness(
        body=f"{claim}\n", citations=_citations(), judge_llm=_StubJudge()
    )
    cv = _verdict(report, "historical propensity")
    assert cv is not None and cv.verdict == VERDICT_SUPPORTED
    assert "claim_markerless_uncited" not in report.counters


async def test_ordinary_markerless_synthesis_still_passes(monkeypatch) -> None:
    """The expensive error would be failing the 2,374 markerless claims a day.

    The 07-01 fabrication-vs-interpretation calibration is what these depend on;
    the guard must be the narrow premise class and nothing wider.
    """
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "1")
    body = (
        "BLUF: escalation risk is low and the trajectory is steady.\n"
        "The combination of elite discord and protest activity is worth "
        "watching, but neither is acute.\n"
        "Overall severity is assessed as moderate.\n"
    )
    report = await verify_finding_faithfulness(
        body=body, citations=_citations(), judge_llm=_StubJudge()
    )
    assert "claim_markerless_uncited" not in report.counters
    assert all(cv.verdict == VERDICT_SUPPORTED for cv in report.claim_verdicts)


def test_a_baseline_scoped_to_the_EVIDENCE_is_not_a_world_claim() -> None:
    """"Absent FROM THE EVIDENCE" is the absence machinery's question, not this one."""
    assert (
        V._is_uncited_world_baseline(
            "- Historical coup indicators (e.g., recent coups, contested "
            "succession) are absent from the evidence."
        )
        is False
    )
    assert (
        V._is_uncited_world_baseline(
            "No historical precedent appears in the collected reporting."
        )
        is False
    )
    assert V._is_uncited_world_baseline(_SUPPORTED_7) is True
    assert (
        V._is_uncited_world_baseline(
            "Structural coup risk remains low given Australia's longstanding "
            "democratic institutions and lack of acute triggers."
        )
        is True
    )
    assert (
        V._is_uncited_world_baseline("Escalation risk is assessed as low.") is False
    )


async def test_a_slice_pass_cannot_erase_the_premise_flag(monkeypatch) -> None:
    """The W31 coexistence rule, extended: the two defects are orthogonal.

    A scoped negative can verify perfectly against the input slice AND still rest
    on a baseline the analyst supplied from memory. Letting the content pass
    clear the premise flag would retire the pass-side guard the same way it once
    retired W31's phrasing flag.
    """
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "1")

    class _SliceConn:
        async def fetchrow(self, sql: str, *a):
            return {"input_row_refs": [uuid4()]} if "analyst_traces" in sql else None

        async def fetch(self, sql: str, *a):
            return [
                {
                    "title": "Buenos Aires transit strike enters second day",
                    "body": "",
                    "source_id": "source.wire",
                    "provenance_kind": "",
                    "row_kind": "signal",
                }
            ]

    claim = (
        "No new mass mobilisations were recorded, consistent with Argentina's "
        "historical low coup incidence."
    )
    report = await verify_finding_faithfulness(
        body=f"{claim}\n",
        citations=_citations(),
        judge_llm=_StubJudge(),
        target_id="country_g20_ar",
        slice_conn=_SliceConn(),
        run_id=uuid4(),
    )
    cv = _verdict(report, "historical low coup incidence")
    assert cv is not None and cv.reason == "uncited_world_knowledge"
    assert report.counters["absence_slice_premise_flagged"] == 1
    assert "absence_slice_verified" not in report.counters
