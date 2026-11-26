# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A consult turn is judged after it SETTLES, inside the judge's own
budget, and a judge that cannot run says why.

2026-10-15: a capped consult turn printed a bare "not judged · judge
unavailable". These pin the fix (see ``analysts/consult_judging`` module
comment for the finding):

* a turn the per-run spend ceiling stopped is judged — the judge is exempt
  from the run's cap and never meters into it;
* the judge runs at the settle point (the last frame, after the mint, on the
  loop and on the recovery replay) and stamps a digest of what it graded; the
  front door withdraws a verdict whose text moved (``not_settled``);
* the judge's OWN input budget fits the claims from the head; only a budget
  too small for one claim is ``budget_exhausted``;
* every way the judge cannot run stamps ``judge_unavailable_reason`` from the
  closed vocabulary: budget_exhausted | route_down | timeout | not_settled.

Fakes only (the C1 idiom): the real verify pass over a stub judge, no model,
no database.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID, uuid4

import pytest

from legba.data.analysts import consult_judging
from legba.data.analysts.consult_on_demand import ConsultOnDemandDeps, run_method
from legba.data.registry import consult_turn_stamps as stamps

from .test_analyst_consult_on_demand import _ScriptedLLMHandler, _SubstrateStub
from .test_consult_c1_usage_judging import (  # noqa: F401 — _judge_on is a fixture
    _ANSWER,
    REF_A,
    REF_B,
    _judge_on,
    _Pool,
    _run,
    _StubJudge,
)

_ANSWER_A = f"The US withdrew twelve B-1 bombers from RAF Fairford [{REF_A}]."


def _final(answer: str, refs: list[str]) -> str:
    return json.dumps({
        "final": True, "answer": answer, "uncertainty": 0.2,
        "cited_refs": refs, "unanswered_aspects": [],
    })


def _deps(llm: Any, judge: Any, **kw: Any) -> ConsultOnDemandDeps:
    async def _resolver(ref: str) -> Any:
        return judge

    substrate = _SubstrateStub(
        signal_rows=[{"id": REF_A, "title": "Bombers leave Fairford"}],
        signal_refs=[UUID(REF_A)],
    )
    return ConsultOnDemandDeps(
        llm=llm, substrate=substrate, judge_resolver=_resolver, pg_pool=_Pool(), **kw,
    )


# ---------------------------------------------------------------------------
# The capped turn: judged, after it settles, outside the run's cap.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_capped_turn_is_judged_after_it_settles(_judge_on):
    llm = _ScriptedLLMHandler([
        json.dumps({"tool": "search_signals", "args": {"query": "Fairford"}}),
        _final(_ANSWER_A, [REF_A]),
    ])
    judge = _StubJudge(["supported"])
    # A ceiling the first round already exceeds: drilling stops, the forced
    # final synthesises — the capped shape of 2026-10-15.
    deps = _deps(llm, judge, max_input_tokens_per_run=1)
    result = await run_method(
        [{"question": "What happened at Fairford?"}],
        {"analyst_id": "consult_default", "run_id": uuid4()}, deps,
    )
    kinds = [s.get("kind") for s in result.intermediate_steps]
    assert "spend_ceiling_reached" in kinds and "forced_final" in kinds

    consult = result.consult_response
    ver = consult.data["verification"]
    assert ver["state"] == "judged" and ver["score"] == 1.0
    assert "judge_unavailable_reason" not in ver
    # The verdict is about the answer and refs the run RETURNS.
    assert ver["settled_sha256"] == stamps.settle_digest(
        consult.answer, [str(r) for r in consult.cited_substrate_refs],
    )
    # The settle point is the LAST frame — after the answer was wrapped.
    assert kinds[-1] == "judge_turn"
    assert kinds.index("wrap_finding") < kinds.index("judge_turn")
    # The finding carrier carries the same stamp.
    assert result.finding.data["consult_response"]["data"]["verification"] == ver
    # Exempt from the run's meter: the judge's call is not one of the turn's.
    assert judge.calls >= 1
    assert consult.data["usage"]["calls"] == len(llm.calls) == 2


@pytest.mark.asyncio
async def test_a_spent_run_never_reads_as_judge_unavailable(_judge_on):
    """The cap bites on BOTH ceilings at once; the judge still runs."""
    llm = _ScriptedLLMHandler([
        json.dumps({"tool": "search_signals", "args": {"query": "Fairford"}}),
        _final(_ANSWER_A, [REF_A]),
    ])
    deps = _deps(
        llm, _StubJudge(["supported"]),
        max_input_tokens_per_run=1, max_cost_usd_per_run=0.000001,
    )
    result = await run_method(
        [{"question": "q"}], {"analyst_id": "consult_default"}, deps,
    )
    ver = result.consult_response.data["verification"]
    assert ver["state"] == "judged"
    assert ver.get("reason") != "judge_unavailable"


@pytest.mark.asyncio
async def test_the_recovery_replay_is_judged_at_its_settle_point(_judge_on):
    from legba.data.analysts import consult_transcript

    llm = _ScriptedLLMHandler([_final(_ANSWER_A, [REF_A])])
    deps = _deps(llm, _StubJudge(["supported"]))
    transcript = consult_transcript.build_transcript(
        system="s", messages=[{"role": "user", "content": "q"}],
    )
    steps: list[dict[str, Any]] = []

    async def _publish(step: dict[str, Any]) -> None:
        steps.append(step)

    deps.step_publish = _publish
    result = await run_method(
        [{"question": "q", "synthesize_from": {
            "question": "q", "steps": [], "cited_refs": [REF_A],
            "replay_transcript": transcript,
        }}],
        {"analyst_id": "consult_default"}, deps,
    )
    ver = result.consult_response.data["verification"]
    assert ver["state"] == "judged" and ver["settled_sha256"]
    assert steps[-1]["kind"] == "judge_turn"
    assert [s["kind"] for s in steps].count("judge_turn") == 1


# ---------------------------------------------------------------------------
# The front door: a verdict about text the turn no longer carries is withdrawn.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_verdict_on_moved_text_is_withdrawn_as_not_settled(_judge_on):
    import legba.data.registry.consult_api as consult_api

    stamp = await _run(_StubJudge(["supported", "supported"]))
    assert stamp["state"] == "judged"
    payload = {
        "question": "q", "answer": _ANSWER, "uncertainty": 0.2,
        "cited_substrate_refs": [REF_A, REF_B], "unanswered_aspects": [],
        "data": {"synthesis_status": "complete", "verification": stamp},
    }
    kept = consult_api._project_consult_response(payload, finding_id=None, derived_from=[])
    assert kept.verification["state"] == "judged"
    assert kept.verification["score"] == stamp["score"]

    moved = consult_api._project_consult_response(
        {**payload, "answer": _ANSWER + " An edit after judging [" + REF_A + "]."},
        finding_id=None, derived_from=[],
    )
    assert moved.verification["state"] == "not_judged"
    assert moved.verification["score"] is None
    assert moved.verification["reason"] == "judged_text_changed"
    assert moved.verification["judge_unavailable_reason"] == "not_settled"

    # The cited SET counts too, but not its order.
    reordered = consult_api._project_consult_response(
        {**payload, "cited_substrate_refs": [REF_B, REF_A]},
        finding_id=None, derived_from=[],
    )
    assert reordered.verification["state"] == "judged"
    dropped = consult_api._project_consult_response(
        {**payload, "cited_substrate_refs": [REF_A]}, finding_id=None, derived_from=[],
    )
    assert dropped.verification["judge_unavailable_reason"] == "not_settled"


def test_a_stamp_without_a_digest_passes_through():
    """A runtime that predates the digest is not second-guessed."""
    out = stamps.verification_stamp(
        {"state": "judged", "score": 0.8}, reason_if_absent="x", answer="anything",
    )
    assert out["state"] == "judged" and out["score"] == 0.8


# ---------------------------------------------------------------------------
# The judge's own budget.
# ---------------------------------------------------------------------------


def _cite(n: int, chars: int) -> dict[str, Any]:
    return {"title": f"t{n}", "source": "desk", "source_text": "x" * chars}


def test_the_budget_keeps_claims_from_the_head_and_their_evidence_prefix():
    claims = ["One [1].", "Two [2].", "Three [3][1]."]
    cites = [_cite(1, 4000), _cite(2, 4000), _cite(3, 4000)]
    # 2,000 overhead + ~2,100 tokens for two 4,000-char records.
    fitted = consult_judging.fit_judge_input(claims, cites, max_input_tokens=4_100)
    assert fitted is not None
    kept, used, fit = fitted
    assert kept == ["One [1].", "Two [2]."] and len(used) == 2
    assert fit["claims_dropped_for_budget"] == 1 and fit["evidence_clipped"] is False
    # Everything fits under a generous budget.
    kept, used, fit = consult_judging.fit_judge_input(claims, cites, max_input_tokens=20_000)
    assert len(kept) == 3 and len(used) == 3 and fit["claims_dropped_for_budget"] == 0


def test_one_oversized_claim_is_clipped_not_refused():
    kept, used, fit = consult_judging.fit_judge_input(
        ["One [1][2]."], [_cite(1, 40_000), _cite(2, 40_000)], max_input_tokens=4_000,
    )
    assert kept == ["One [1][2]."] and fit["evidence_clipped"] is True
    assert all(len(c["source_text"]) <= 4_000 for c in used)


def test_a_budget_too_small_for_one_claim_fits_nothing():
    assert consult_judging.fit_judge_input(
        ["One [1]."], [_cite(1, 4000)], max_input_tokens=2_050,
    ) is None


@pytest.mark.asyncio
async def test_a_turn_over_the_judges_budget_is_judged_on_its_head(_judge_on, monkeypatch):
    # Two cited claims; a budget with room for one (each record ~70 chars of
    # evidence + frame, the claims ~60 chars): the first claim is judged.
    monkeypatch.setattr(consult_judging, "_JUDGE_PROMPT_OVERHEAD_TOKENS", 0)
    monkeypatch.setenv(consult_judging.CONSULT_JUDGE_MAX_INPUT_TOKENS_ENV, "60")
    stamp = await _run(_StubJudge(["supported"]))
    assert stamp["state"] == "judged"
    assert stamp["claims_judged"] == 1 and stamp["claims_dropped_for_budget"] == 1
    assert stamp["claims_truncated"] is True and stamp["cited_sentences"] == 2


# ---------------------------------------------------------------------------
# Every reason a judge cannot run is stamped, never a bare "unavailable".
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unavailable_budget_exhausted(_judge_on, monkeypatch):
    monkeypatch.setenv(consult_judging.CONSULT_JUDGE_MAX_INPUT_TOKENS_ENV, "2001")
    stamp = await _run(_StubJudge(["supported", "supported"]))
    assert stamp["state"] == "not_judged" and stamp["score"] is None
    assert stamp["reason"] == "judge_input_over_budget"
    assert stamp["judge_unavailable_reason"] == "budget_exhausted"


@pytest.mark.asyncio
async def test_unavailable_budget_exhausted_when_the_route_budget_is(_judge_on, monkeypatch):
    from legba.data.provenance import verify as verify_mod
    from legba.data.stack.llm.base import BudgetExhausted

    async def _verify(**_kw):
        raise BudgetExhausted("budget envelope exhausted")

    monkeypatch.setattr(verify_mod, "verify_finding_faithfulness", _verify)
    stamp = await _run(_StubJudge(["supported"]))
    assert stamp["reason"] == "judge_failed"
    assert stamp["judge_unavailable_reason"] == "budget_exhausted"


@pytest.mark.asyncio
async def test_unavailable_timeout(_judge_on):
    stamp = await _run(_StubJudge(["supported", "supported"], delay=2.0), timeout_s=0.05)
    assert stamp["reason"] == "judge_timeout"
    assert stamp["judge_unavailable_reason"] == "timeout"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("judge", "kw", "reason"),
    [
        (None, {}, "judge_route_unresolved"),
        ("stub", {"judge_resolver": None}, "no_judge_route"),
        ("stub", {"pg_pool": None}, "no_evidence_reader"),
        ("fail", {}, "judge_unavailable"),
    ],
)
async def test_unavailable_route_down(_judge_on, judge, kw, reason):
    j = {"stub": _StubJudge(["supported"]), "fail": _StubJudge(["x"], fail=True)}.get(judge)
    stamp = await _run(j, **kw)
    assert stamp["state"] == "not_judged" and stamp["reason"] == reason
    assert stamp["judge_unavailable_reason"] == "route_down"
    if reason == "judge_unavailable":
        # The verify pass's own reason rides beside the vocabulary.
        assert stamp["verify_unavailable_reason"] == "judge_error"


@pytest.mark.asyncio
async def test_unavailable_not_settled(_judge_on):
    stamp = await _run(_StubJudge(["supported"]), synthesis_status="partial")
    assert stamp["reason"] == "synthesis_incomplete"
    assert stamp["judge_unavailable_reason"] == "not_settled"


def test_a_deliberate_state_is_not_unavailability():
    assert "judge_unavailable_reason" not in consult_judging.not_judged("judge_disabled")
    deep = stamps.verification_stamp(None, reason_if_absent="deep_consult_not_judged")
    assert "judge_unavailable_reason" not in deep


def test_the_class_vocabulary():
    assert stamps.unavailable_reason_for_class("timeout") == "timeout"
    assert stamps.unavailable_reason_for_class("credit") == "budget_exhausted"
    for cls in ("auth", "retired", "rate_limited", "server_error", "empty", "error", None):
        assert stamps.unavailable_reason_for_class(cls) == "route_down"


def test_the_front_door_backfills_an_older_runtimes_stamp():
    """A stamp from before the field gets its why at the front door — the
    persisted partial turn, and an old floor-only stamp keyed by its class."""
    partial = stamps.verification_stamp(None, reason_if_absent="synthesis_incomplete")
    assert partial["judge_unavailable_reason"] == "not_settled"
    old = stamps.verification_stamp(
        {
            "state": "not_judged", "reason": "judge_unavailable",
            "judge_unavailable_reason": "judge_error",
            "unverified_reason": {"class": "timeout"},
        },
        reason_if_absent="x",
    )
    assert old["judge_unavailable_reason"] == "timeout"
    assert old["verify_unavailable_reason"] == "judge_error"
    for reason, why in (
        ("judge_timeout", "timeout"), ("judge_route_unresolved", "route_down"),
        ("judge_failed", "route_down"), ("judge_unavailable", "route_down"),
    ):
        out = stamps.verification_stamp(
            {"state": "not_judged", "reason": reason}, reason_if_absent="x",
        )
        assert out["judge_unavailable_reason"] == why
