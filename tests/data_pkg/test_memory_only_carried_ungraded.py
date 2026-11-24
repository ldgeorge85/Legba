# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""SW-9 — a memory-only unit finding is CARRIED, UNGRADED, never a 1.0.

THE GOLDEN. ``energy_security`` on ``country_watch_mm``, finding 809fdee1
(2026-09-28 04:02Z, confidence 0.7). Its ONLY citations are ``[16]`` (the
PRIOR READ) and ``[17]`` (the WINDOW LEDGER); its critique scored
``overall_score 1.0``, 4/4 supported, ``judge=llm``. The judge was not handed
an empty map — QW1-B folds both blocks into the evidence it grades against —
so "the pressure stays moderate as recorded in the prior read [16]" was
checked against the prior read, agreed with it, and passed. That is a
consistency check against what the desk already said, published as a
faithfulness score about this cycle's evidence. bc997fb0 (2026-09-26, Israel,
one ``[121]`` prior-read citation) is the same shape on the absence route.

DESIGN (b) is what is pinned here: the finding publishes the score state
``carried_ungraded`` — NULL ``faithfulness_score``, the gate capped at 0.5,
its own title/tag/body line/counter — and every verdict, span and reason is
byte-identical to the scored run (which is what licenses the lineage's
``none`` on the severity and census families).
"""

from __future__ import annotations

import json
import re
from typing import Any
from uuid import UUID, uuid4

import pytest

import legba.data.provenance.verify as verify
from legba.data.alerts.sinks import CARRIED_UNGRADED_STATE, verify_state_from_score
from legba.data.analysts.deterministic_handlers import unit_correctness_scorer as ucs
from legba.data.provenance.grounding_backstops import memory_only_citations
from legba.data.provenance.judge_assessability import (
    CARRIED_UNGRADED_GATE_SCORE,
    NON_SCORE_STATES,
    SCORE_STATE_CARRIED_UNGRADED,
    SCORE_STATE_SCORED,
    SCORE_STATE_UNASSESSABLE,
    build_faithfulness_critique_payload,
    gate_score,
    resolve_score_state,
)
from legba.data.provenance.judge_pipeline_version import (
    JUDGE_PIPELINE_VERSION,
    METRIC_FAITHFULNESS_SCORE,
    METRIC_REASON_CENSUS,
    METRIC_SEVERITY_SPLIT,
    SHIFT_MOVES,
    SHIFT_NONE,
    STAMP_EXPECTED_SHIFTS,
    STAMP_LINEAGE,
)
from legba.data.provenance.verify import verify_finding_faithfulness
from legba.data.registry.export_api import _finding_verify

# ---------------------------------------------------------------------------
# The 809fdee1 fixture (planning/waves/R1/evidence/memory_only_findings.json),
# evidence_text trimmed to the header + the BLUF the carry rests on.
# ---------------------------------------------------------------------------

GOLDEN_ID = UUID("809fdee1-8514-435f-830d-3a21d33f94f7")

GOLDEN_BODY = (
    "*As of 2026-09-28; slice covers the trailing 72h to that date; 15 signals.*\n\n"
    "**BLUF:** Myanmar’s energy‑security pressure stays moderate with no "
    "new supply, price or infrastructure disruptions reported in the latest "
    "three‑day collection.\n\n"
    "## What changed\n"
    "- No material change; the pressure stays moderate as recorded in the prior "
    "read on 27 September 2026 [16] and reaffirmed by the latest "
    "window‑ledger entry for 27 September 2026 [17].\n\n"
    "## Why it matters\n"
    "- A moderate rating indicates that Myanmar’s electricity generation, "
    "fuel imports and transport networks continue operating without acute "
    "outages, plant shutdowns, pipeline cuts or sharp price spikes. The absence "
    "of any reported outage, price shock, or attack on power plants, pipelines "
    "or ports in the current slice suggests that existing supply arrangements "
    "remain functional and that the energy system is not under heightened "
    "stress at this time.\n\n"
    "## What would change this read\n"
    "- A credible field report of a power‑plant outage, a forced "
    "fuel‑import curtailment, a sharp rise in domestic fuel prices, or an "
    "attack on an energy‑critical facility would immediately raise the "
    "assessment.\n\n"
    "## Indicators\n"
    "- LNG terminal outage: not observed\n"
    "- Pipeline disruption: not observed\n"
)


def _golden_citations() -> list[dict[str, Any]]:
    return [
        {
            "title": "Myanmar’s energy‑security pressure stays moderate, "
            "no new disruptions reported",
            "marker": "[16]",
            "ref_id": "e604ed9a-04bc-4009-b550-8ceadb7cf7e7",
            "ordinal": 16,
            "ref_kind": "prior_read",
            "grounding": "prior_read",
            "produced_at": "2026-09-27T16:01:56.034174+00:00",
            "marker_class": "desk_grounding",
            "evidence_text": (
                "[16] PRIOR READ — this unit's previous verified read of this "
                "target: Myanmar’s energy‑security pressure stays "
                "moderate, no new disruptions reported\n"
                "    body: **BLUF:** Myanmar’s energy‑security pressure "
                "remains moderate with no new supply, price or infrastructure "
                "disruptions reported in the latest collection."
            ),
            "resolves_against": "data.citations",
        },
        {
            "title": "Window ledger (this unit's trailing 14-day record)",
            "marker": "[17]",
            "ordinal": 17,
            "ref_kind": "window_ledger",
            "grounding": "window_ledger",
            "marker_class": "desk_grounding",
            "evidence_text": (
                "[17] WINDOW LEDGER — THIS UNIT's own verified reads of the "
                "trailing 14 days (2 line(s), OLDEST FIRST).\n"
                "    - 26 September 2026 :: energy_security [moderate] Myanmar’s "
                "energy‑security pressure stays moderate with no new "
                "disruptions\n"
                "    - 27 September 2026 :: energy_security [moderate] Myanmar’s "
                "energy‑security pressure stays moderate, no new "
                "disruptions reported"
            ),
            "resolves_against": "data.citations",
            "ledger_finding_ids": [str(uuid4()), str(uuid4())],
        },
    ]


def _golden_indicators() -> list[dict[str, Any]]:
    return [
        {
            "id": "lng-terminal-outage",
            "status": "not_observed",
            "citations": [],
            "statement": "Unplanned outage at an LNG import/export terminal",
            "first_seen": "2026-09-28",
            "horizon_date": "2026-10-12",
        },
    ]


SIGNAL_ORD = 3


def _signal_citation() -> dict[str, Any]:
    return {
        "marker": f"[{SIGNAL_ORD}]",
        "ordinal": SIGNAL_ORD,
        "signal_id": str(uuid4()),
        "title": "Yangon grid holds through the week",
        "source_text": (
            "Myanmar's national grid operator reported no load-shedding this "
            "week and fuel imports arrived on schedule."
        ),
    }


# ---------------------------------------------------------------------------
# The stub judge — one verdict per claim it was actually asked to grade
# ---------------------------------------------------------------------------

_CLAIM_NUM = re.compile(r"^\s*(\d+)\.\s", re.M)


class _Usage:
    prompt_tokens = 10
    completion_tokens = 5
    reasoning_tokens = 0
    total_tokens = 15
    cost_estimate_usd = 0.0


class _Response:
    def __init__(self, content: str) -> None:
        self.content = content
        self.finish_reason = "stop"
        self.raw_response = {"provider": "stub", "choices": [{}]}
        self.usage = _Usage()


class _UniformJudge:
    """Marks every graded claim with ONE verdict — the live 4/4 'supported'."""

    subprovider = "stub"

    def __init__(self, verdict: str = "supported") -> None:
        self.verdict = verdict

    async def chat_complete(self, messages, **kw):
        tail = str(messages[-1]["content"]).split("CLAIMS:\n")[-1]
        n = len({int(m) for m in _CLAIM_NUM.findall(tail)})
        return _Response(json.dumps({"verdicts": [self.verdict] * n}))


@pytest.fixture
def judge_on(monkeypatch):
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "1")


async def _verify(body: str, citations: Any, *, judge: Any = None, indicators=None):
    return await verify_finding_faithfulness(
        body=body,
        citations=citations,
        judge_llm=judge,
        indicators=indicators,
        title="Myanmar’s energy‑security pressure stays moderate",
        target_id="country_watch_mm",
    )


def _payload(report) -> dict[str, Any]:
    return build_faithfulness_critique_payload(
        report, analyzed_output_id=GOLDEN_ID, analyzed_analyst_id="energy_security"
    )


# ---------------------------------------------------------------------------
# 1. The golden: 809fdee1 is no longer 1.0
# ---------------------------------------------------------------------------


async def test_the_golden_is_carried_ungraded_not_a_perfect_score(judge_on) -> None:
    report = await _verify(
        GOLDEN_BODY, _golden_citations(), judge=_UniformJudge(),
        indicators=_golden_indicators(),
    )
    # The judge ran and agreed with every claim — exactly as it did live.
    assert report.judge_status == "llm"
    assert report.checkable_claims >= 2
    assert report.supported_claims == report.checkable_claims
    # ...and that agreement is no longer published as a faithfulness score.
    assert report.score_state == SCORE_STATE_CARRIED_UNGRADED
    assert report.score_state_reason == "memory_only_citations"
    assert report.counters.get("carried_ungraded") == 1
    block = report.as_dict()
    assert block["faithfulness_score"] is None
    assert block["score_state"] == "carried_ungraded"
    assert block["overall_score"] == CARRIED_UNGRADED_GATE_SCORE == 0.5

    payload = _payload(report)
    assert payload["title"] == "Faithfulness verify (carried, ungraded)"
    assert "carried_ungraded" in payload["tags"]
    assert payload["scores"] == {}
    assert payload["overall_score"] == 0.5 and payload["confidence"] == 0.5
    v = payload["data"]["verification"]
    assert v["faithfulness_score"] is None
    assert v["overall_score"] == 0.5
    assert v["score_state"] == "carried_ungraded"
    assert v["score_state_reason"] == "memory_only_citations"
    assert v["judge_pipeline_version"] == JUDGE_PIPELINE_VERSION
    assert "CARRIED, UNGRADED (memory_only_citations)" in payload["body"]
    assert "faithfulness_score=carried_ungraded" in payload["body"]


async def test_the_ledger_is_byte_identical_with_the_state_on_or_off(
    judge_on, monkeypatch
) -> None:
    """What licenses ``none`` on severity + census: the state touches no span,
    verdict, reason or tally — only what the tally is CALLED. With the
    memory-only verdict forced off, the same run is the pre-SW-9 1.0."""
    on = await _verify(
        GOLDEN_BODY, _golden_citations(), judge=_UniformJudge(),
        indicators=_golden_indicators(),
    )
    monkeypatch.setattr(verify, "memory_only_citations", lambda *a, **k: False)
    off = await _verify(
        GOLDEN_BODY, _golden_citations(), judge=_UniformJudge(),
        indicators=_golden_indicators(),
    )
    assert off.score_state == SCORE_STATE_SCORED
    assert off.as_dict()["faithfulness_score"] == 1.0  # the live 1.0, reproduced
    assert off.as_dict()["overall_score"] == 1.0

    a, b = on.as_dict(), off.as_dict()
    for key in (
        "claim_verdicts", "unsupported_spans", "checkable_claims",
        "supported_claims", "branch_scores", "judge_status",
    ):
        assert a[key] == b[key], key
    assert {k: n for k, n in on.counters.items() if k != "carried_ungraded"} == (
        off.counters
    )
    assert on.faithfulness_score == off.faithfulness_score  # the raw tally


async def test_bc997fb0_prior_read_only_on_the_floor_path_is_carried_too() -> None:
    """The second specimen's shape (one ``[121]`` prior read), judge OFF: the
    state is decided from the citations, never from which grader ran."""
    body = (
        "**BLUF:** Israel’s energy‑security pressure remains moderate.\n\n"
        "## What changed\n"
        "The prior 72‑hour read already judged the pressure moderate and "
        "noted no new shocks, so the assessment is unchanged [121].\n\n"
        "## Why it matters\n"
        "A moderate pressure level indicates that the grid and fuel imports "
        "continue operating without interruption.\n"
    )
    cits = [dict(_golden_citations()[0], marker="[121]", ordinal=121)]
    report = await _verify(body, cits)
    assert report.provisional
    assert report.score_state == SCORE_STATE_CARRIED_UNGRADED
    assert report.as_dict()["faithfulness_score"] is None
    assert report.as_dict()["overall_score"] <= 0.5


# ---------------------------------------------------------------------------
# 2. Negative controls — one real signal and the finding is graded as before
# ---------------------------------------------------------------------------


async def test_one_real_signal_citation_keeps_the_finding_scored(judge_on) -> None:
    body = GOLDEN_BODY.replace(
        "stress at this time.", f"stress at this time [{SIGNAL_ORD}]."
    )
    assert f"[{SIGNAL_ORD}]" in body
    cits = [_signal_citation(), *_golden_citations()]
    report = await _verify(
        body, cits, judge=_UniformJudge(), indicators=_golden_indicators()
    )
    assert report.score_state == SCORE_STATE_SCORED
    assert "carried_ungraded" not in report.counters
    assert report.as_dict()["faithfulness_score"] == 1.0
    payload = _payload(report)
    assert payload["title"] == "Faithfulness verify (score 1.00)"
    assert payload["scores"] == {"faithfulness": 1.0}


def test_a_signal_cited_only_by_an_indicator_is_still_real_evidence() -> None:
    inds = [dict(_golden_indicators()[0], status="triggered", citations=[SIGNAL_ORD])]
    cits = [_signal_citation(), *_golden_citations()]
    assert memory_only_citations(GOLDEN_BODY, cits, inds) is False
    assert memory_only_citations(GOLDEN_BODY, cits, _golden_indicators()) is True


def test_an_uncited_signal_entry_does_not_rescue_a_memory_only_body() -> None:
    """The question is what the PROSE rests on, not what the list carries."""
    cits = [_signal_citation(), *_golden_citations()]
    assert memory_only_citations(GOLDEN_BODY, cits) is True


def test_inert_shapes() -> None:
    assert memory_only_citations("", _golden_citations()) is False
    assert memory_only_citations(GOLDEN_BODY, None) is False
    assert memory_only_citations(GOLDEN_BODY, "not a list") is False
    assert memory_only_citations(GOLDEN_BODY, [None, 3, {"ordinal": "x"}]) is False
    # A body citing nothing that resolves is the FLOOR's business, not this one.
    assert memory_only_citations("Nothing cited here.\n", _golden_citations()) is False
    # A memory entry with no captured text never reaches the judge: not resolvable.
    bare = [{k: v for k, v in c.items() if k != "evidence_text"}
            for c in _golden_citations()]
    assert memory_only_citations(GOLDEN_BODY, bare) is False
    # A non-memory grounding block (the situation register) is not memory.
    register = [dict(_golden_citations()[0], ref_kind="situation_register")]
    assert memory_only_citations("Steady [16].\n", register) is False


def test_a_composition_is_never_carried_ungraded() -> None:
    """Unit convention only: a composition cites desk reads as ``[[ref:N]]``
    sub-claims and its region/world bytes are pinned by the flip kit."""
    cits = [{
        "ordinal": 2, "marker": "[[ref:2]]", "ref_kind": "finding",
        "continuity": "prior_read", "produced_at": "2026-09-27T00:00:00+00:00",
        "evidence_text": "The prior country read.",
    }]
    assert memory_only_citations("Steady since the prior read [[ref:2]].\n", cits) is False


# ---------------------------------------------------------------------------
# 3. The policy functions
# ---------------------------------------------------------------------------


def test_unassessable_wins_over_carried() -> None:
    assert resolve_score_state(checkable_claims=0, body="x", memory_only=True) == (
        SCORE_STATE_UNASSESSABLE, "no_checkable_claims",
    )
    assert resolve_score_state(checkable_claims=3, body="x", memory_only=True) == (
        SCORE_STATE_CARRIED_UNGRADED, "memory_only_citations",
    )
    assert resolve_score_state(checkable_claims=3, body="x") == (SCORE_STATE_SCORED, None)
    assert NON_SCORE_STATES == {SCORE_STATE_UNASSESSABLE, SCORE_STATE_CARRIED_UNGRADED}


def test_the_cap_only_lowers() -> None:
    kw = {"ceiling": None, "score_state": SCORE_STATE_CARRIED_UNGRADED,
          "provisional": False}
    assert gate_score(score=1.0, **kw) == 0.5
    # A carry the judge FAILED keeps its failure — the cap never raises.
    assert gate_score(score=0.25, **kw) == 0.25


def test_the_stamp_is_bumped_once_with_a_registry_row() -> None:
    # SW-9's OWN stamp, by literal: W4 F-3 (``2026-10-03/2``, the world floor)
    # has since become the head, so this no longer reads JUDGE_PIPELINE_VERSION.
    row = STAMP_EXPECTED_SHIFTS["2026-10-03/1"]
    assert "2026-10-03/1" in STAMP_LINEAGE
    assert row == {
        METRIC_FAITHFULNESS_SCORE: SHIFT_MOVES,
        METRIC_SEVERITY_SPLIT: SHIFT_NONE,
        METRIC_REASON_CENSUS: SHIFT_NONE,
    }
    # carried_ungraded is a STATE, never a reason: the fail-class table is
    # untouched (the census family's ``none``).
    assert "carried_ungraded" not in verify._FAIL_CLASS_BY_REASON


# ---------------------------------------------------------------------------
# 4. The readers — the desk brief's verify line and the correctness mean
# ---------------------------------------------------------------------------


def test_the_operator_facing_state_names_the_class() -> None:
    assert verify_state_from_score(None, score_state="carried_ungraded") == (
        CARRIED_UNGRADED_STATE
    )
    assert CARRIED_UNGRADED_STATE.startswith("carried, ungraded")
    # the export (desk brief) reads the block, not a bare score
    state, _flags = _finding_verify(
        {"faithfulness_score": None, "score_state": "carried_ungraded"},
        "energy_security",
    )
    assert state == CARRIED_UNGRADED_STATE


def test_the_correctness_scorer_counts_carried_rows_beside_its_mean() -> None:
    assert "score_state" in ucs._FAITHFULNESS_SQL
    assert ucs._carried_ungraded({"confidence": 0.5, "score_state": "carried_ungraded"})
    assert not ucs._carried_ungraded({"confidence": 0.9, "score_state": "scored"})
    assert not ucs._carried_ungraded({"confidence": 0.9})  # pre-SW-9 / fakes
