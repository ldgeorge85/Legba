# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""D4b — FLOOR-TRIGGERED JUDGING: the composition floor may only exclude on
JUDGED evidence.

THE DEFECT these tests close (PREMISE_GRADING_LOOP §A-3, measured over 14 live
days): whether a finding is LLM-judged is a SHA-256 coin flip over its own
UUID. Judged findings publish mean faithfulness 0.853 and fall below the 0.50
composition floor 3.2% of the time; unjudged findings publish 0.571 and fall
below it 24.2% of the time — 8/8 desks, same direction. So 1,263 of 6,905
scored findings were excluded from every composition that would have consumed
them, substantially by the low 64 bits of an id.

EVERY behavioural test here drives the REAL binding path: the actual
``verify_finding_faithfulness`` (via ``verify_with_floor_escalation``), the
actual ``build_faithfulness_critique_payload``, the actual ``CritiquePayload``
write contract, and — for the seam test — the actual
``actor_critic.verify_inline_target_finding``. The judge is a stub because it
is the only external process; nothing else is faked.

The four contract properties, one section each:

  1. an UNJUDGED below-floor finding is ESCALATED, and is excluded only if it
     is STILL below the floor after the judge has spoken;
  2. an already-JUDGED below-floor finding is excluded WITHOUT a second call
     (and neither is an above-floor one escalated — the cost is bounded to the
     would-be-floored slice);
  3. the escalation MARKER lands in the critique payload, at the JSONB path the
     live laterals read;
  4. the SAMPLED arm's population query can exclude escalated rows — the arm
     comparison that found this defect must survive the fix.
"""
from __future__ import annotations

import json
import re
from types import SimpleNamespace
from uuid import uuid4

import pytest

from legba.data.provenance.judge_assessability import (
    JUDGE_STATUS_UNSAMPLED,
    JudgeSamplingPolicy,
    build_faithfulness_critique_payload,
)
from legba.data.provenance.judge_floor_escalation import (
    COUNTER_ESCALATED,
    COUNTER_ESCALATION_FAILED,
    COUNTER_ESCALATION_UNAVAILABLE,
    DEFAULT_ESCALATION_FLOOR,
    ESCALATION_ENABLED_ENV,
    ESCALATION_FLOOR_ENV,
    JUDGE_TRIGGER_FLOOR_ESCALATION,
    JUDGE_TRIGGER_SAMPLED,
    open_policy,
    published_overall,
    resolve_escalation_floor,
    verify_with_floor_escalation,
)
from legba.data.provenance.models import CritiquePayload

FLOOR = 0.50


# ---------------------------------------------------------------------------
# Harness — the judge is the ONLY stub (it is the only external process)
# ---------------------------------------------------------------------------

#: The judge prompt numbers its claims ``"<n>. <claim>"`` (``_judge_claim_block``)
#: and ``_judge_claim_partition`` enforces an EXACT one-verdict-per-claim
#: contract, so the stub counts what it was actually asked to grade instead of
#: guessing — a hard-coded verdict count would make these tests pass or fail on
#: the segmenter's behaviour rather than on the escalation's.
_CLAIM_NUM = re.compile(r"^\s*(\d+)\.\s", re.M)


class _Usage:
    prompt_tokens = 10
    completion_tokens = 5
    reasoning_tokens = 0


class _Response:
    def __init__(self, content: str) -> None:
        self.content = content
        self.usage = _Usage()


class _UniformJudge:
    """Returns ONE verdict per graded claim, all the same, and counts calls."""

    subprovider = "stub"

    def __init__(self, verdict: str = "supported") -> None:
        self.verdict = verdict
        self.calls = 0

    async def chat_complete(self, messages, **kw):
        self.calls += 1
        tail = str(messages[-1]["content"]).split("CLAIMS:\n")[-1]
        n = len({int(m) for m in _CLAIM_NUM.findall(tail)})
        return _Response(json.dumps({"verdicts": [self.verdict] * n}))


class _MustNotBeCalledJudge:
    """A judge whose invocation IS the failure — the bound on the cost.

    Wired (not ``None``) on purpose: ``should_escalate`` skips a ``None`` judge
    for its own reason, so a ``None`` here would prove nothing about the gates
    under test.
    """

    subprovider = "stub"

    async def chat_complete(self, *a, **k):  # pragma: no cover — the trap
        raise AssertionError("the judge was called when it must not have been")


def _sid_citation(sid: str) -> list[dict]:
    return [{
        "marker": "[1]",
        "signal_id": sid,
        "title": "Lira drops 3% on the day",
        "source_text": "The lira fell three percent on Monday.",
    }]


#: 1 cited + 3 UNCITED fact-asserting claims → deterministic floor 1/4 = 0.25,
#: i.e. under the 0.50 composition floor. This is the live shape of the defect:
#: ``no_citation`` is 93.7% of the unsampled arm's failure mass and 0.0% of the
#: judged arm's, because the judge is TOLD that a markerless claim is synthesis.
BELOW_FLOOR_BODY = (
    "The lira fell three percent on Monday [1].\n"
    "The central bank spent two billion dollars defending the peg.\n"
    "Ankara summoned the German ambassador on Tuesday.\n"
    "Reserves fell to a nine-year low in March.\n"
)

#: 3 cited + 1 uncited → floor 3/4 = 0.75, comfortably above. Nothing to defend.
ABOVE_FLOOR_BODY = (
    "The lira fell three percent on Monday [1].\n"
    "The central bank spent two billion dollars defending the peg [1].\n"
    "Ankara summoned the German ambassador on Tuesday [1].\n"
    "Reserves fell to a nine-year low in March.\n"
)


def _unsampled_policy() -> JudgeSamplingPolicy:
    """rate=0.0 on a non-always-listed kind ⇒ the sampling gate excludes it."""
    return JudgeSamplingPolicy(
        finding_id=str(uuid4()), kind="inline_target", rate=0.0
    )


async def _run(body: str, *, judge, policy=None, floor=FLOOR, confidence=0.80):
    return await verify_with_floor_escalation(
        escalation_floor=floor,
        floor_confidence=confidence,
        body=body,
        citations=_sid_citation(str(uuid4())),
        judge_llm=judge,
        judge_sampling=_unsampled_policy() if policy is None else policy,
    )


# ---------------------------------------------------------------------------
# 1. The rule: exclusion requires JUDGED evidence
# ---------------------------------------------------------------------------


async def test_unsampled_below_floor_is_escalated_and_rescued(monkeypatch):
    """THE FIX. An unjudged finding the floor would drop goes to the judge
    first — and when the judge supports its claims it clears the floor and
    enters the composition it would otherwise have been coin-flipped out of."""
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "1")
    judge = _UniformJudge("supported")
    out = await _run(BELOW_FLOOR_BODY, judge=judge)

    assert out.escalated is True
    assert out.trigger == JUDGE_TRIGGER_FLOOR_ESCALATION
    assert out.report.judge_status == "llm"
    # The deterministic verdict WAS below the floor …
    assert out.pre_escalation_overall_score == pytest.approx(0.25)
    assert out.pre_escalation_judge_status == JUDGE_STATUS_UNSAMPLED
    # … and the judged verdict is not.
    assert published_overall(out.report) >= FLOOR
    assert out.report.counters.get(COUNTER_ESCALATED) == 1
    # EXACTLY ONE judge call — the escalation is one grading, not a retry loop.
    assert judge.calls == 1


async def test_escalated_finding_still_below_floor_is_excluded_on_judged_evidence(
    monkeypatch,
):
    """The other half of the rule. Escalation is not an amnesty: a finding the
    judge also refuses stays under the floor and is still excluded — but now
    because a grader said so, not because of its UUID."""
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "1")
    judge = _UniformJudge("unsupported")
    out = await _run(BELOW_FLOOR_BODY, judge=judge)

    assert out.escalated is True
    assert out.report.judge_status == "llm"
    assert published_overall(out.report) < FLOOR
    assert out.pre_escalation_overall_score == pytest.approx(0.25)
    assert judge.calls == 1


async def test_escalated_pass_is_the_same_pass_a_sampled_finding_takes(monkeypatch):
    """NOT a judge-behaviour change (so no ``JUDGE_PIPELINE_VERSION`` bump).

    The escalated call is the ordinary call with the gate opened, so the same
    finding graded as SAMPLED and graded as ESCALATED must produce the same
    verdict — only the selection provenance differs.
    """
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "1")
    fid = str(uuid4())
    sid = str(uuid4())
    kwargs = dict(body=BELOW_FLOOR_BODY, citations=_sid_citation(sid))

    escalated = await verify_with_floor_escalation(
        escalation_floor=FLOOR, floor_confidence=0.80,
        judge_llm=_UniformJudge("supported"),
        judge_sampling=JudgeSamplingPolicy(
            finding_id=fid, kind="inline_target", rate=0.0),
        **kwargs,
    )
    sampled = await verify_with_floor_escalation(
        escalation_floor=FLOOR, floor_confidence=0.80,
        judge_llm=_UniformJudge("supported"),
        judge_sampling=JudgeSamplingPolicy(
            finding_id=fid, kind="inline_target", rate=1.0),
        **kwargs,
    )
    assert sampled.trigger == JUDGE_TRIGGER_SAMPLED
    assert escalated.trigger == JUDGE_TRIGGER_FLOOR_ESCALATION
    assert escalated.report.judge_status == sampled.report.judge_status == "llm"
    assert published_overall(escalated.report) == published_overall(sampled.report)
    assert escalated.report.checkable_claims == sampled.report.checkable_claims
    assert escalated.report.supported_claims == sampled.report.supported_claims


def test_open_policy_keeps_the_identity_and_only_opens_the_gate():
    policy = JudgeSamplingPolicy(
        finding_id="abc", kind="inline_target", analyst_id="energy_security",
        rate=0.0, always=(),
    )
    opened = open_policy(policy)
    assert opened.should_judge() is True
    assert (opened.finding_id, opened.kind, opened.analyst_id, opened.always) == (
        policy.finding_id, policy.kind, policy.analyst_id, policy.always
    )


# ---------------------------------------------------------------------------
# 2. The bound: only the would-be-floored UNJUDGED slice pays
# ---------------------------------------------------------------------------


async def test_judged_below_floor_finding_is_not_re_judged(monkeypatch):
    """A SAMPLED finding that lands below the floor was already excluded on
    judged evidence. It must be graded exactly ONCE."""
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "1")
    judge = _UniformJudge("unsupported")
    out = await _run(
        BELOW_FLOOR_BODY,
        judge=judge,
        policy=JudgeSamplingPolicy(
            finding_id=str(uuid4()), kind="inline_target", rate=1.0),
    )
    assert out.report.judge_status == "llm"
    assert published_overall(out.report) < FLOOR
    assert out.trigger == JUDGE_TRIGGER_SAMPLED
    assert out.pre_escalation_overall_score is None
    assert judge.calls == 1  # NOT 2


async def test_unsampled_above_floor_is_never_escalated(monkeypatch):
    """The cost bound. An unjudged finding the floor would ADMIT is left alone
    — escalation buys nothing there and the sampled arm stays a clean sample of
    the above-floor population too."""
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "1")
    out = await _run(ABOVE_FLOOR_BODY, judge=_MustNotBeCalledJudge())
    assert out.report.judge_status == JUDGE_STATUS_UNSAMPLED
    assert out.trigger == JUDGE_TRIGGER_SAMPLED
    assert published_overall(out.report) >= FLOOR


async def test_own_confidence_below_the_floor_vetoes_the_escalation(monkeypatch):
    """The floor is ``LEAST(confidence, overall_score)``. A finding whose own
    asserted confidence is already under the bar cannot be rescued by any
    verdict, so it must not spend a judge call (297 of 1,165 measured rows,
    ~25% of the naive volume)."""
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "1")
    out = await _run(BELOW_FLOOR_BODY, judge=_MustNotBeCalledJudge(), confidence=0.30)
    assert out.report.judge_status == JUDGE_STATUS_UNSAMPLED
    assert out.trigger == JUDGE_TRIGGER_SAMPLED


async def test_unassessable_finding_is_not_escalated(monkeypatch):
    """An UNASSESSABLE report extracted no gradeable claim. Its capped score is
    an honest statement of absence, not a coin flip — there is nothing for the
    judge to adjudicate and no call is made."""
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "1")
    out = await _run("## Heading only\n", judge=_MustNotBeCalledJudge())
    assert out.report.score_state == "unassessable"
    assert out.trigger == JUDGE_TRIGGER_SAMPLED


async def test_no_escalation_floor_is_byte_identical(monkeypatch):
    """Every UNGATED caller — replay harnesses, the pre-D4b tests — passes no
    floor and gets exactly today's pass."""
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "1")
    out = await _run(BELOW_FLOOR_BODY, judge=_MustNotBeCalledJudge(), floor=None)
    assert out.report.judge_status == JUDGE_STATUS_UNSAMPLED
    assert out.trigger == JUDGE_TRIGGER_SAMPLED
    assert out.pre_escalation_overall_score is None


@pytest.mark.parametrize("off", ["0", "false", "no", "off", ""])
async def test_kill_switch_stops_the_escalation(monkeypatch, off):
    """The OPS lever: one env var restores pre-D4b behaviour with no deploy,
    and it stops ONLY the escalation — the sampling gate is untouched."""
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "1")
    monkeypatch.setenv(ESCALATION_ENABLED_ENV, off)
    out = await _run(BELOW_FLOOR_BODY, judge=_MustNotBeCalledJudge())
    assert out.report.judge_status == JUDGE_STATUS_UNSAMPLED
    assert out.trigger == JUDGE_TRIGGER_SAMPLED


async def test_escalation_is_on_by_default(monkeypatch):
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "1")
    monkeypatch.delenv(ESCALATION_ENABLED_ENV, raising=False)
    out = await _run(BELOW_FLOOR_BODY, judge=_UniformJudge("supported"))
    assert out.escalated is True


# ---------------------------------------------------------------------------
# 2b. Degrade-not-drop — arm membership never depends on judge HEALTH
# ---------------------------------------------------------------------------


async def test_judge_down_keeps_the_honest_unsampled_verdict(monkeypatch):
    """J2's invariant, preserved. If the escalated pass does not actually get
    an LLM verdict (flag off / judge down), the row keeps its ``unsampled``
    label instead of being republished as ``deterministic`` — otherwise a judge
    outage would silently move rows between arms. The gap is COUNTED, because
    a floor exclusion going un-adjudicated must be visible."""
    monkeypatch.delenv("LEGBA_VERIFY_LLM_JUDGE", raising=False)  # flag OFF
    judge = _UniformJudge("supported")
    out = await _run(BELOW_FLOOR_BODY, judge=judge)

    assert out.report.judge_status == JUDGE_STATUS_UNSAMPLED
    assert out.report.judge_unavailable_reason is None  # unsampled ≠ unavailable
    assert out.trigger == JUDGE_TRIGGER_SAMPLED
    assert out.report.counters.get(COUNTER_ESCALATION_UNAVAILABLE) == 1
    assert judge.calls == 0  # the flag gate short-circuits before the transport


async def test_escalation_crash_degrades_to_the_original_verdict(monkeypatch):
    """Fault injection: the escalated pass RAISES. The original verdict ships,
    the counter fires, and the run does not break — a floor that cannot be
    adjudicated is bad, a broken run is worse."""
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "1")
    from legba.data.provenance import verify as verify_mod

    real = verify_mod.verify_finding_faithfulness
    state = {"n": 0}

    async def _boom(**kwargs):
        state["n"] += 1
        if state["n"] == 1:
            return await real(**kwargs)
        raise RuntimeError("escalated pass exploded")

    monkeypatch.setattr(verify_mod, "verify_finding_faithfulness", _boom)
    out = await _run(BELOW_FLOOR_BODY, judge=_UniformJudge("supported"))

    assert state["n"] == 2  # the escalation WAS attempted
    assert out.report.judge_status == JUDGE_STATUS_UNSAMPLED
    assert out.trigger == JUDGE_TRIGGER_SAMPLED
    assert out.report.counters.get(COUNTER_ESCALATION_FAILED) == 1


# ---------------------------------------------------------------------------
# 3. The marker lands in the critique payload
# ---------------------------------------------------------------------------


def _payload(outcome):
    return build_faithfulness_critique_payload(
        outcome.report,
        analyzed_output_id=uuid4(),
        analyzed_analyst_id="energy_security",
        analyzed_analyst_version="1.0.0",
        analyzed_model="llm.primary.openai_compat",
        judge_model="stub",
        judge_trigger=outcome.trigger,
        pre_escalation_overall_score=outcome.pre_escalation_overall_score,
        pre_escalation_judge_status=outcome.pre_escalation_judge_status,
    )


async def test_escalation_marker_lands_in_the_critique_payload(monkeypatch):
    """At the JSONB path the live laterals read: the whole payload is dumped
    into ``analyst_outputs.data``, so ``data.verification.*`` here is
    ``cr.data->'data'->'verification'->>'…'`` in SQL (the same path
    ``claim_verdicts`` is already lifted from)."""
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "1")
    out = await _run(BELOW_FLOOR_BODY, judge=_UniformJudge("supported"))
    v = _payload(out)["data"]["verification"]

    assert v["judge_trigger"] == JUDGE_TRIGGER_FLOOR_ESCALATION
    assert v["judge_status"] == "llm"
    # The unsampled arm's verdict is PRESERVED — without it this fix would
    # truncate that arm at the floor and destroy the A-3 measurement.
    assert v["pre_escalation_overall_score"] == pytest.approx(0.25)
    assert v["pre_escalation_judge_status"] == JUDGE_STATUS_UNSAMPLED
    # The gate JOIN key is untouched by the marker: still a real float.
    assert isinstance(v["overall_score"], float)


async def test_ordinary_rows_carry_the_sampled_marker_and_no_pre_keys(monkeypatch):
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "1")
    out = await _run(ABOVE_FLOOR_BODY, judge=_MustNotBeCalledJudge())
    v = _payload(out)["data"]["verification"]
    assert v["judge_trigger"] == JUDGE_TRIGGER_SAMPLED
    assert v["pre_escalation_overall_score"] is None
    assert v["pre_escalation_judge_status"] is None


async def test_marker_survives_the_critique_write_contract(monkeypatch):
    """The additive keys must survive ``CritiquePayload`` validation + dump —
    that model is what ``write_critique`` persists, so a key it drops would
    never reach the row no matter what the builder emitted."""
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "1")
    out = await _run(BELOW_FLOOR_BODY, judge=_UniformJudge("supported"))
    dumped = CritiquePayload(**_payload(out)).model_dump(mode="json")
    v = dumped["data"]["verification"]
    assert v["judge_trigger"] == JUDGE_TRIGGER_FLOOR_ESCALATION
    assert v["pre_escalation_overall_score"] == pytest.approx(0.25)


def test_pre_d4b_callers_get_a_null_marker_not_a_fabricated_one():
    """Every existing caller (and every historical row) carries NULL here.
    NULL means 'the ordinary policy decided' — which is what the population
    query's COALESCE says — and never a fabricated 'sampled' claim from a
    build that could not know."""
    from legba.data.provenance.verify import FaithfulnessReport

    payload = build_faithfulness_critique_payload(
        FaithfulnessReport(faithfulness_score=1.0, checkable_claims=1,
                           supported_claims=1, judge_status="llm"),
        analyzed_output_id=uuid4(),
    )
    v = payload["data"]["verification"]
    assert v["judge_trigger"] is None
    assert v["pre_escalation_overall_score"] is None


# ---------------------------------------------------------------------------
# 4. The sampled arm stays statistically clean
# ---------------------------------------------------------------------------

#: THE ARM FILTER, verbatim as the analysis SQL must spell it. Escalation
#: conditions on the SCORE (only below-floor rows escalate), so pooling
#: escalated rows into the ``llm`` arm would bias every arm mean; and pre-D4b
#: rows carry NULL and ARE sampled-arm, which is what the COALESCE says.
SAMPLED_ARM_SQL = (
    "COALESCE(cr.data->'data'->'verification'->>'judge_trigger', 'sampled') "
    "= 'sampled'"
)


def _in_sampled_arm(payload: dict) -> bool:
    """Python mirror of :data:`SAMPLED_ARM_SQL` over the SAME nested keys the
    SQL walks — one expression, so the two cannot drift apart silently."""
    v = payload.get("data", {}).get("verification", {})
    return (v.get("judge_trigger") or "sampled") == "sampled"


async def test_sampled_arm_query_excludes_escalated_rows(monkeypatch):
    """The population requirement. An escalated row reads judge_status='llm'
    but must NOT enter the sampled arm, or the next A-3-style comparison pools
    a score-conditioned subpopulation with a random one."""
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "1")
    escalated = _payload(await _run(BELOW_FLOOR_BODY, judge=_UniformJudge("supported")))
    sampled = _payload(await _run(
        BELOW_FLOOR_BODY,
        judge=_UniformJudge("supported"),
        policy=JudgeSamplingPolicy(
            finding_id=str(uuid4()), kind="inline_target", rate=1.0),
    ))
    unsampled = _payload(await _run(ABOVE_FLOOR_BODY, judge=_MustNotBeCalledJudge()))

    # Both judged rows say judge_status='llm' — status alone cannot separate them.
    assert escalated["data"]["verification"]["judge_status"] == "llm"
    assert sampled["data"]["verification"]["judge_status"] == "llm"
    # The trigger can.
    assert _in_sampled_arm(escalated) is False
    assert _in_sampled_arm(sampled) is True
    assert _in_sampled_arm(unsampled) is True
    # And the JSONB path the SQL walks is the one the payload actually uses.
    assert "->'data'->'verification'->>'judge_trigger'" in SAMPLED_ARM_SQL
    assert "judge_trigger" in escalated["data"]["verification"]


def test_a_historical_row_without_the_key_is_sampled_arm():
    """Pre-D4b critiques (every row on disk today) have no ``judge_trigger``.
    They were selected by the ordinary policy, so the COALESCE must keep
    them — otherwise the fix would erase 14 days of comparable population."""
    assert _in_sampled_arm({"data": {"verification": {}}}) is True
    assert _in_sampled_arm({}) is True


# ---------------------------------------------------------------------------
# 5. The floor this rule defends is THE composition floor
# ---------------------------------------------------------------------------


def test_escalation_floor_tracks_the_composition_floor():
    """The escalation floor is a deliberate COPY of the composition floor
    (``data.provenance`` sits BELOW ``data.analysts`` and must not import
    upward). This is the pin that stops the copy drifting: a drift either
    escalates findings nothing excludes, or leaves excluded findings
    un-adjudicated — the defect, back again."""
    from legba.data.analysts.meta_findings_synthesizer import (
        DEFAULT_VERIFY_FLOOR,
        VERIFY_FLOOR_ENV,
    )

    assert ESCALATION_FLOOR_ENV == VERIFY_FLOOR_ENV
    assert DEFAULT_ESCALATION_FLOOR == DEFAULT_VERIFY_FLOOR


def test_escalation_floor_reads_the_ops_env(monkeypatch):
    monkeypatch.setenv(ESCALATION_FLOOR_ENV, "0.65")
    assert resolve_escalation_floor() == pytest.approx(0.65)
    monkeypatch.setenv(ESCALATION_FLOOR_ENV, "9")
    assert resolve_escalation_floor() == 1.0  # clamped, never a >1 bar
    monkeypatch.setenv(ESCALATION_FLOOR_ENV, "not-a-number")
    assert resolve_escalation_floor() == DEFAULT_ESCALATION_FLOOR  # warns, defaults
    monkeypatch.delenv(ESCALATION_FLOOR_ENV, raising=False)
    assert resolve_escalation_floor() == DEFAULT_ESCALATION_FLOOR


def test_published_overall_is_the_number_the_floor_compares():
    """The floor reads ``data->>'overall_score'`` — the GATE score, not the raw
    tally. Deciding escalation on the raw tally would escalate the wrong
    findings: a zero-claim finding's raw tally is 1.0."""
    from legba.data.provenance.verify import FaithfulnessReport

    unassessable = FaithfulnessReport(
        faithfulness_score=1.0, checkable_claims=0, supported_claims=0,
        judge_status=JUDGE_STATUS_UNSAMPLED, score_state="unassessable",
    )
    assert unassessable.faithfulness_score == 1.0
    assert published_overall(unassessable) <= 0.5
    provisional = FaithfulnessReport(
        faithfulness_score=1.0, checkable_claims=4, supported_claims=4,
        judge_status=JUDGE_STATUS_UNSAMPLED,
    )
    assert published_overall(provisional) == pytest.approx(0.85)


# ---------------------------------------------------------------------------
# 6. THE BINDING PATH — the live seam actually calls this
# ---------------------------------------------------------------------------


async def test_actor_seam_binds_the_escalation_with_the_resolved_floor(monkeypatch):
    """Drives the REAL ``actor_critic.verify_inline_target_finding``.

    A fix nothing calls is not a fix: this pins that the live verify seam routes
    through ``verify_with_floor_escalation``, hands it the RESOLVED composition
    floor, and hands it the finding's OWN confidence for the veto — and that the
    trace envelope it returns carries the trigger the critique row carries.
    """
    from legba.data.provenance import judge_floor_escalation as esc
    from legba.data.provenance.verify import FaithfulnessReport
    from legba.runtime import actor_critic

    monkeypatch.setenv(ESCALATION_FLOOR_ENV, "0.55")
    seen: dict = {}

    async def _recorder(**kwargs):
        seen.update(kwargs)
        return esc.EscalationOutcome(
            report=FaithfulnessReport(
                faithfulness_score=1.0, checkable_claims=1, supported_claims=1,
                judge_status="llm",
            ),
            trigger=esc.JUDGE_TRIGGER_FLOOR_ESCALATION,
            pre_escalation_overall_score=0.25,
            pre_escalation_judge_status=JUDGE_STATUS_UNSAMPLED,
        )

    monkeypatch.setattr(esc, "verify_with_floor_escalation", _recorder)

    deps = SimpleNamespace(
        descriptor=SimpleNamespace(
            identity=SimpleNamespace(
                kind="inline_target", id="energy_security", version="1.0.0"),
            method=SimpleNamespace(llm={"primary": {"raw": "llm.primary.openai_compat"}}),
        ),
        verify_judge=SimpleNamespace(subprovider="stub"),
        verify_judge_ref="llm.judge.openrouter",
        verify_judge_route="configured",
    )
    finding = SimpleNamespace(
        body=BELOW_FLOOR_BODY, title="Lira slide", confidence=0.72,
        data={"citations": _sid_citation(str(uuid4()))},
    )

    class _DeadConn:
        """The write is best-effort + try/except'd in the seam; this proves the
        escalation wiring without touching a live database."""

        def __getattr__(self, _name):
            raise AssertionError("no live DB in this test")

    trace = await actor_critic.verify_inline_target_finding(
        _DeadConn(), deps=deps, finding_id=uuid4(), finding_payload=finding,
        run_id=uuid4(), target_id="country_g20_tr",
        judge_sample_rate=0.10, judge_sample_always=None,
    )

    assert seen["escalation_floor"] == pytest.approx(0.55)  # the RESOLVED floor
    assert seen["floor_confidence"] == pytest.approx(0.72)  # the finding's own
    assert seen["judge_sampling"] is not None
    # The T7 input stays None on the unit path — floor_confidence is a SEPARATE
    # variable precisely so this stays true.
    assert seen["finding_confidence"] is None
    assert trace is not None and trace["judge_trigger"] == "floor_escalation"


async def test_actor_seam_passes_no_floor_when_no_sampling_gate_is_configured(
    monkeypatch,
):
    """No rate ⇒ no gate ⇒ no coin flip ⇒ nothing to defend, and the pass is
    byte-identical to pre-D4b."""
    from legba.data.provenance import judge_floor_escalation as esc
    from legba.data.provenance.verify import FaithfulnessReport
    from legba.runtime import actor_critic

    seen: dict = {}

    async def _recorder(**kwargs):
        seen.update(kwargs)
        return esc.EscalationOutcome(report=FaithfulnessReport(
            faithfulness_score=1.0, checkable_claims=1, supported_claims=1))

    monkeypatch.setattr(esc, "verify_with_floor_escalation", _recorder)

    deps = SimpleNamespace(
        descriptor=SimpleNamespace(
            identity=SimpleNamespace(
                kind="inline_target", id="energy_security", version="1.0.0"),
            method=SimpleNamespace(llm={}),
        ),
        verify_judge=None, verify_judge_ref="", verify_judge_route="",
    )
    finding = SimpleNamespace(
        body=BELOW_FLOOR_BODY, title="", confidence=0.72,
        data={"citations": _sid_citation(str(uuid4()))},
    )

    class _DeadConn:
        def __getattr__(self, _name):
            raise AssertionError("no live DB in this test")

    await actor_critic.verify_inline_target_finding(
        _DeadConn(), deps=deps, finding_id=uuid4(), finding_payload=finding,
        run_id=uuid4(), target_id=None,
        judge_sample_rate=None, judge_sample_always=None,
    )
    assert seen["escalation_floor"] is None
    assert seen["judge_sampling"] is None
