# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Judge TRANSPORT — the retry shim and PARTITION-PRESERVE (``2026-09-08/1``).

Every test here drives the REAL verify path — ``verify_finding_faithfulness`` →
``_maybe_llm_judge`` → ``_run_judge`` → ``_judge_claim_partition`` →
``judge_transport.call_judge_with_retry`` — and stubs only the HTTP layer, i.e.
the handler's ``chat_complete``. Nothing patches a verify internal, so a test
that passes proves the wiring and not a mock.

The shape being reproduced is the live one, recorded 2026-09-08: OpenRouter
answers **HTTP 200** and puts the upstream failure in the BODY
(``{"error": {"message": "Upstream error from Nvidia: Service temporarily
overloaded", "code": 502}}``, no ``choices``), which is why
``stack/llm/base._call_chat``'s status-code retry never fired and why the row
landed as ``judge_empty``. ``_parse_response`` maps that to
``LLMResponse(content="", finish_reason="error", raw_response=<the body>)``, so
the stub returns exactly that object shape.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from legba.data.provenance.judge_assessability import (
    PROVISIONAL_SCORE_CEILING,
    gate_score,
    is_provisional,
)
from legba.data.provenance.judge_transport import (
    JUDGE_RETRY_ATTEMPTS,
    JUDGE_RETRY_CAP_S,
    JUDGE_STATUS_PARTIAL,
    JudgeTransportTelemetry,
    backoff_delay,
    call_judge_with_retry,
    classify_response,
    resolve_partition_outcome,
    status_is_retryable,
)
from legba.data.provenance.verify import (
    _ABSENCE_JUDGE_SYSTEM,
    verify_finding_faithfulness,
)
from legba.data.stack.llm.base import HardLLMFailure, TransientLLMFailure


# ---------------------------------------------------------------------------
# Stub HTTP layer — the three response shapes the live route actually produces
# ---------------------------------------------------------------------------


class _Usage:
    prompt_tokens = 0
    completion_tokens = 0
    reasoning_tokens = 0
    total_tokens = 0
    cost_estimate_usd = 0.0


class _Response:
    """What ``_parse_response`` hands back for one provider body."""

    def __init__(self, content: str, raw: dict[str, Any] | None = None) -> None:
        self.content = content
        self.finish_reason = "stop" if content else "error"
        self.raw_response = raw if raw is not None else {"choices": [{}]}
        self.usage = _Usage()


def _ok(content: str) -> _Response:
    return _Response(content, {"provider": "Nvidia", "choices": [{}]})


def _upstream_502() -> _Response:
    """The live envelope, verbatim: HTTP 200, no ``choices``, a 502 inside."""
    return _Response(
        "",
        {
            "id": "gen-stub",
            "error": {
                "message": "Upstream error from Nvidia: Service temporarily overloaded",
                "code": 502,
            },
        },
    )


def _empty_200() -> _Response:
    """A genuinely empty answer — HTTP 200, well-formed, the judge said nothing."""
    return _Response("", {"provider": "Nvidia", "choices": [{"message": {}}]})


class _ScriptedJudge:
    """Replays a scripted sequence of responses/exceptions per judge ROUTE.

    Routes are keyed on prompt IDENTITY (the absence rubric is a constant, not a
    phrase) exactly as ``test_verify_absence_v3._PartitionJudge`` does, so a
    rewrite of either rubric cannot silently mis-route a partition here.
    """

    subprovider = "stub"

    def __init__(
        self,
        *,
        shared: list[Any] | None = None,
        absence: list[Any] | None = None,
        survey: list[Any] | None = None,
    ) -> None:
        self._scripts = {
            "shared": list(shared or []),
            "absence": list(absence or []),
            "survey": list(survey or []),
        }
        self.calls: list[str] = []

    def _route(self, system: str) -> str:
        if system == _ABSENCE_JUDGE_SYSTEM:
            return "absence"
        return "survey" if "NULL-RESULT" in system else "shared"

    def count(self, route: str) -> int:
        return self.calls.count(route)

    async def chat_complete(self, messages, *, system=None, **kw):
        route = self._route(system or "")
        self.calls.append(route)
        script = self._scripts[route]
        step = script.pop(0) if script else _empty_200()
        if isinstance(step, BaseException):
            raise step
        return step


async def _no_sleep(_delay: float) -> None:
    """Backoff is a policy under test, not a thing to actually wait for."""


@pytest.fixture(autouse=True)
def _instant_backoff(monkeypatch):
    """Never really sleep. The DELAYS are asserted directly on ``backoff_delay``
    and on the recorded arguments, so no test buys its coverage with wall time."""
    import legba.data.provenance.judge_transport as jt

    monkeypatch.setattr(jt.asyncio, "sleep", _no_sleep)


@pytest.fixture(autouse=True)
def _judge_on(monkeypatch):
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "1")


def _mixed_body(sid: str) -> tuple[str, list[dict[str, Any]]]:
    """A fact-rich finding with ONE embedded absence claim → the V3 two-partition
    route (two positive claims keep ``_is_null_result_finding`` False). Copied in
    shape from ``test_verify_absence_v3._mixed_body_one_absence``, which is the
    fixture that pins this routing."""
    body = (
        "The lira fell three percent today [1].\n"
        "The central bank spent two billion dollars defending the peg [1].\n"
        "No evidence of capital-flight controls appears in the reviewed signals.\n"
    )
    citations = [
        {"marker": "[1]", "signal_id": sid, "title": "Lira drops 3% on the day"}
    ]
    return body, citations


# ---------------------------------------------------------------------------
# 1. Classification — telling a 200-wrapped 502 from an empty answer
# ---------------------------------------------------------------------------


def test_classify_upstream_envelope_names_the_inner_status():
    token, content, retry_after = classify_response(_upstream_502())
    assert token == "200/502"
    assert content == ""
    assert retry_after is None
    assert status_is_retryable(token)


def test_classify_empty_and_ok_are_distinct_from_the_envelope():
    assert classify_response(_empty_200())[0] == "empty"
    assert classify_response(_ok('{"verdicts": []}'))[0] == "200"
    # An empty ANSWER is still worth one more ask (the judge may be flaky), but
    # it is not the same event and the row must be able to say which it was.
    assert status_is_retryable("empty")
    # A clean 200 never reaches the retry test — the loop returns on it — and it
    # is not retryable if it ever did.
    assert not status_is_retryable("200")


def test_a_policy_4xx_inside_a_200_is_not_retried():
    """A router that answers 200 and names a 403/404 is stating a configuration
    verdict; asking again cannot change it."""
    assert not status_is_retryable("200/404")
    assert not status_is_retryable("404")
    assert status_is_retryable("429")
    assert status_is_retryable("503")


def test_backoff_is_bounded_and_jittered_and_retry_after_wins():
    for attempt in range(6):
        assert 0 < backoff_delay(attempt) <= JUDGE_RETRY_CAP_S * 1.25
    # 2 s → 4 s → capped at 8 s, ±25%.
    assert 1.5 <= backoff_delay(0) <= 2.5
    assert 3.0 <= backoff_delay(1) <= 5.0
    assert 6.0 <= backoff_delay(2) <= 10.0
    # A provider instruction is honoured verbatim, never jittered.
    assert backoff_delay(0, retry_after=7.0) == 7.0
    # …and still capped: a 10-minute Retry-After is a later run, not a sleep.
    assert backoff_delay(0, retry_after=600.0) == 30.0


# ---------------------------------------------------------------------------
# 2. The retry loop — 502 → 200
# ---------------------------------------------------------------------------


async def test_upstream_502_then_200_recovers_the_verdicts():
    """The exact live sequence. Before this train the FIRST 200/502 ended the
    partition and the finding was graded on the deterministic floor."""
    sid = str(uuid4())
    body, citations = _mixed_body(sid)
    judge = _ScriptedJudge(
        shared=[_upstream_502(), _ok('{"verdicts": ["supported", "supported"]}')],
        absence=[_ok('{"verdicts": ["supported"]}')],
    )
    rep = await verify_finding_faithfulness(
        body=body, citations=citations, judge_llm=judge
    )
    assert rep.judge_status == "llm"
    assert rep.judge_unavailable_reason is None
    assert judge.count("shared") == 2  # the retry actually fired
    assert judge.count("absence") == 1
    assert rep.faithfulness_score == pytest.approx(1.0)
    # The receipts say what happened, in order.
    assert rep.judge_attempts == 3
    assert rep.judge_http_statuses == ["200/502", "200", "200"]
    assert rep.as_dict()["judge_http_statuses"] == ["200/502", "200", "200"]


async def test_retry_is_bounded_and_exhaustion_still_floors():
    """Three attempts, then the partition soft-fails exactly as it always did —
    the retry buys attempts, never an invented verdict."""
    sid = str(uuid4())
    body, citations = _mixed_body(sid)
    judge = _ScriptedJudge(
        shared=[_upstream_502()] * 10,
        absence=[_upstream_502()] * 10,
    )
    rep = await verify_finding_faithfulness(
        body=body, citations=citations, judge_llm=judge
    )
    assert judge.count("shared") == JUDGE_RETRY_ATTEMPTS
    assert judge.count("absence") == JUDGE_RETRY_ATTEMPTS
    assert rep.judge_status == "deterministic"
    assert rep.judge_unavailable_reason == "judge_empty"
    assert rep.judge_attempts == 2 * JUDGE_RETRY_ATTEMPTS
    assert set(rep.judge_http_statuses) == {"200/502"}


async def test_429_with_retry_after_is_retried_and_the_header_is_obeyed():
    """A real 429 arrives as ``TransientLLMFailure(status=429, retry_after=...)``
    once ``base._call_chat`` has exhausted its own retries. The shim retries it
    and sleeps for exactly what the provider asked."""
    slept: list[float] = []

    async def _record(delay: float) -> None:
        slept.append(delay)

    telem = JudgeTransportTelemetry()
    judge = _ScriptedJudge(
        shared=[
            TransientLLMFailure("stub 429", status=429, retry_after=3.0),
            _ok('{"verdicts": ["supported"]}'),
        ]
    )
    content = await call_judge_with_retry(
        judge,
        evidence_prompt="claims",
        system="generic",
        telemetry=telem,
        sleeper=_record,
    )
    assert content == '{"verdicts": ["supported"]}'
    assert telem.http_statuses == ["429", "200"]
    assert slept == [3.0]


async def test_a_hard_4xx_is_not_retried_and_still_reads_judge_error():
    """A 404 (the router does not serve this model id) is a configuration
    verdict. It must NOT burn retries, and it must keep landing as
    ``judge_error`` rather than being laundered into ``judge_empty``."""
    sid = str(uuid4())
    body, citations = _mixed_body(sid)
    judge = _ScriptedJudge(shared=[HardLLMFailure("stub 404", status=404)] * 5)
    rep = await verify_finding_faithfulness(
        body=body, citations=citations, judge_llm=judge
    )
    assert judge.count("shared") == 1
    assert rep.judge_status == "deterministic"
    assert rep.judge_unavailable_reason == "judge_error"
    assert rep.judge_http_statuses == ["404"]


async def test_retry_budget_stops_the_loop_before_it_can_run_away():
    """The budget bounds ADDED SLEEP and is checked BEFORE sleeping, so an
    exhausted budget stops immediately instead of after one more long wait."""
    telem = JudgeTransportTelemetry()
    judge = _ScriptedJudge(shared=[_upstream_502()] * 10)
    content = await call_judge_with_retry(
        judge,
        evidence_prompt="claims",
        system="generic",
        telemetry=telem,
        attempts=5,
        budget_s=0.0,
        sleeper=_no_sleep,
    )
    assert content == ""
    assert telem.attempts == 1  # no sleep fit in the budget, so no retry ran


# ---------------------------------------------------------------------------
# 3. PARTITION-PRESERVE — and the ceiling rule
# ---------------------------------------------------------------------------


async def test_both_partitions_judged_is_unchanged():
    sid = str(uuid4())
    body, citations = _mixed_body(sid)
    judge = _ScriptedJudge(
        shared=[_ok('{"verdicts": ["supported", "unsupported"]}')],
        absence=[_ok('{"verdicts": ["supported"]}')],
    )
    rep = await verify_finding_faithfulness(
        body=body, citations=citations, judge_llm=judge
    )
    assert rep.judge_status == "llm"
    assert rep.judge_unavailable_reason is None
    assert rep.checkable_claims == 3
    assert rep.supported_claims == 2
    assert not is_provisional(rep.judge_status)


async def test_empty_absence_partition_keeps_the_shared_verdicts():
    """THE REPAIR. One partition empty used to discard BOTH — ``return [], {}``
    at two call sites — so a finding with one absence claim lost every verdict
    about half the time at a ~30% transport failure rate."""
    sid = str(uuid4())
    body, citations = _mixed_body(sid)
    judge = _ScriptedJudge(
        shared=[_ok('{"verdicts": ["supported", "supported"]}')],
        absence=[_upstream_502()] * 5,
    )
    rep = await verify_finding_faithfulness(
        body=body, citations=citations, judge_llm=judge
    )
    assert rep.judge_status == JUDGE_STATUS_PARTIAL
    assert rep.judge_unavailable_reason == "judge_empty_partition:absence"
    # The judged partition's two verdicts SURVIVED.
    assert rep.supported_claims == 2
    assert not any(s.reason.startswith("judge_") for s in rep.unsupported_spans)
    # The floored partition's claim is not in the judged branch scores — no
    # verdict was invented for a claim nobody graded.
    assert "absence" not in rep.branch_scores
    assert rep.branch_scores["citation_support"]["checkable"] == 2
    # The absence partition burned its full retry budget before being floored.
    assert judge.count("absence") == JUDGE_RETRY_ATTEMPTS


async def test_empty_shared_partition_keeps_the_absence_verdicts():
    """Symmetric: the load-bearing partition can be the one that fails, and the
    absence verdicts must survive it just the same."""
    sid = str(uuid4())
    body, citations = _mixed_body(sid)
    judge = _ScriptedJudge(
        shared=[_empty_200()] * 5,
        absence=[_ok('{"verdicts": ["contradicted"]}')],
    )
    rep = await verify_finding_faithfulness(
        body=body, citations=citations, judge_llm=judge
    )
    assert rep.judge_status == JUDGE_STATUS_PARTIAL
    assert rep.judge_unavailable_reason == "judge_empty_partition:citation_support"
    assert any(s.reason.startswith("judge_") for s in rep.unsupported_spans)
    assert set(rep.judge_http_statuses) == {"empty", "200"}


async def test_every_partition_empty_is_the_old_deterministic_floor():
    """Unchanged, deliberately: with nothing graded there is no partial to
    preserve, and ``judge_empty`` keeps meaning what it meant."""
    sid = str(uuid4())
    body, citations = _mixed_body(sid)
    judge = _ScriptedJudge(shared=[_empty_200()] * 5, absence=[_empty_200()] * 5)
    rep = await verify_finding_faithfulness(
        body=body, citations=citations, judge_llm=judge
    )
    assert rep.judge_status == "deterministic"
    assert rep.judge_unavailable_reason == "judge_empty"
    assert rep.provisional is True


# ---------------------------------------------------------------------------
# 4. THE CEILING RULE, written down and pinned
# ---------------------------------------------------------------------------


def test_the_ceiling_rule_partial_is_adjudicated_deterministic_is_not():
    """0.85 marks a verdict NO grader adjudicated, so it applies iff the report
    carries ZERO judged verdicts."""
    assert is_provisional("deterministic") is True
    assert is_provisional("unsampled") is True
    assert is_provisional(None) is True
    assert is_provisional("") is True
    assert is_provisional(JUDGE_STATUS_PARTIAL) is False
    assert is_provisional("llm") is False

    high = 0.95
    # A floor-only verdict is capped, as it has been since Q-1(c).
    assert gate_score(
        score=high, ceiling=None, score_state="scored", provisional=True
    ) == pytest.approx(PROVISIONAL_SCORE_CEILING)
    # A partial one is not — the floored partition is already CHARGED in the
    # denominator, so capping would charge the finding twice for one outage.
    assert gate_score(
        score=high,
        ceiling=None,
        score_state="scored",
        provisional=is_provisional(JUDGE_STATUS_PARTIAL),
    ) == pytest.approx(high)


def test_resolve_partition_outcome_is_the_whole_policy():
    assert resolve_partition_outcome(judged=True, floored=[]) == ("llm", None)
    assert resolve_partition_outcome(judged=False, floored=[]) == (
        "deterministic",
        None,
    )
    assert resolve_partition_outcome(judged=False, floored=["absence"]) == (
        "deterministic",
        None,
    )
    assert resolve_partition_outcome(judged=True, floored=["absence"]) == (
        JUDGE_STATUS_PARTIAL,
        "judge_empty_partition:absence",
    )


async def test_a_partial_row_publishes_an_uncapped_overall_score():
    """End to end: the 0.85 cap does not land on a partial row's published gate
    number, and the row still SAYS which partition it lost."""
    sid = str(uuid4())
    body, citations = _mixed_body(sid)
    judge = _ScriptedJudge(
        shared=[_ok('{"verdicts": ["supported", "supported"]}')],
        absence=[_upstream_502()] * 5,
    )
    rep = await verify_finding_faithfulness(
        body=body, citations=citations, judge_llm=judge
    )
    published = rep.as_dict()
    assert published["judge_status"] == JUDGE_STATUS_PARTIAL
    assert published["provisional"] is False
    assert published["overall_score"] > PROVISIONAL_SCORE_CEILING
    assert published["judge_unavailable_reason"] == "judge_empty_partition:absence"
    assert published["judge_attempts"] == 1 + JUDGE_RETRY_ATTEMPTS


# ---------------------------------------------------------------------------
# 5. The receipts are SPARSE — no judge call, no keys
# ---------------------------------------------------------------------------


async def test_no_judge_call_leaves_the_verification_dict_untouched():
    """Flag off ⇒ no transport ⇒ neither receipt key appears, so every
    pre-2026-09-08 floor-path dict is byte-identical."""
    sid = str(uuid4())
    body, citations = _mixed_body(sid)
    rep = await verify_finding_faithfulness(
        body=body, citations=citations, judge_llm=None
    )
    published = rep.as_dict()
    assert "judge_attempts" not in published
    assert "judge_http_statuses" not in published
    assert rep.judge_attempts is None


async def test_a_non_transport_exception_is_not_retried():
    """A ``BudgetExhausted``, or any exception that is not a transport failure,
    must propagate on the FIRST attempt. Burning two backoff sleeps on a budget
    that is already spent delays an honest ``judge_error`` and buys nothing."""
    from legba.data.stack.llm.base import BudgetExhausted

    sid = str(uuid4())
    body, citations = _mixed_body(sid)
    judge = _ScriptedJudge(shared=[BudgetExhausted("envelope exhausted")] * 5)
    rep = await verify_finding_faithfulness(
        body=body, citations=citations, judge_llm=judge
    )
    assert judge.count("shared") == 1
    assert rep.judge_status == "deterministic"
    assert rep.judge_unavailable_reason == "judge_error"
    assert rep.judge_http_statuses == ["error"]


def test_every_report_rebuild_carries_the_transport_receipts():
    """A fold that adds ONE span rebuilds the whole report field by field. Five
    such sites exist (``composition_integrity``, ``assembly_arms``,
    ``judge_input_checks``, and two in ``verify`` itself) and every one of them
    runs AFTER the judge — so a rebuild that forgot these fields would erase the
    evidence that the judge had to be asked three times, silently, on exactly the
    composition rows this outage hurt most.

    Pinned structurally rather than by exercising all five paths: any
    ``FaithfulnessReport`` rebuild in the tree that carries ``judge_status`` from
    a source report must carry the receipts too."""
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[2] / "src" / "legba"
    missing: list[str] = []
    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for m in re.finditer(
            r"judge_status=(report|floor|rep)\.judge_status,", text
        ):
            src = m.group(1)
            window = text[m.end() : m.end() + 600]
            carried = (
                f"judge_attempts={src}.judge_attempts" in window
                or f'judge_attempts=getattr({src}, "judge_attempts"' in window
            )
            if not carried:
                line = text[: m.start()].count("\n") + 1
                missing.append(f"{path.relative_to(root)}:{line}")
    assert not missing, (
        "these report rebuilds drop judge_attempts/judge_http_statuses: "
        + ", ".join(missing)
    )


# ---------------------------------------------------------------------------
# 6. THE PERSISTED ROW — the serializer that actually writes to the DB
# ---------------------------------------------------------------------------
#
# 2026-09-08 20:53Z the stamp deployed; 21:02Z the first three live rows under it
# came back judge_status='llm' with NO receipt anywhere in the row text. The
# cause was not the transport and not the sparse rule: ``data.verification`` is
# built by a HAND-WRITTEN dict in ``build_faithfulness_critique_payload``, which
# never consulted ``FaithfulnessReport.as_dict``. The receipts reached the TRACE
# envelope (``actor_critic`` returns ``{**report.as_dict(), ...}``) and nowhere
# else.
#
# Every test in this section asserts on the payload the writer PERSISTS, not on
# ``as_dict()``. All of them fail on 89c002ed.


def _persisted_verification(report) -> dict[str, Any]:
    """The ``data.verification`` block exactly as the critique row carries it."""
    from legba.data.provenance.judge_assessability import (
        build_faithfulness_critique_payload,
    )

    payload = build_faithfulness_critique_payload(report, analyzed_output_id=uuid4())
    return payload["data"]["verification"]


async def test_a_judged_row_carries_its_receipts_on_the_first_try():
    """THE REGRESSION. A clean single-attempt judged pass must still publish
    ``judge_attempts`` — otherwise the instrument cannot tell "judged first try"
    from "receipt lost", which is the exact ambiguity the live rows had."""
    sid = str(uuid4())
    body, citations = _mixed_body(sid)
    judge = _ScriptedJudge(
        shared=[_ok('{"verdicts": ["supported", "supported"]}')],
        absence=[_ok('{"verdicts": ["supported"]}')],
    )
    rep = await verify_finding_faithfulness(
        body=body, citations=citations, judge_llm=judge
    )
    assert rep.judge_status == "llm"

    v = _persisted_verification(rep)
    assert v["judge_attempts"] == 2  # one call per partition, no retry
    assert v["judge_http_statuses"] == ["200", "200"]


async def test_the_persisted_row_shows_the_retry_that_happened():
    sid = str(uuid4())
    body, citations = _mixed_body(sid)
    judge = _ScriptedJudge(
        shared=[_upstream_502(), _ok('{"verdicts": ["supported", "supported"]}')],
        absence=[_ok('{"verdicts": ["supported"]}')],
    )
    rep = await verify_finding_faithfulness(
        body=body, citations=citations, judge_llm=judge
    )
    v = _persisted_verification(rep)
    assert v["judge_attempts"] == 3
    assert v["judge_http_statuses"] == ["200/502", "200", "200"]


async def test_partition_preserve_is_reachable_on_the_persisted_row():
    """(4) ``partial`` is not a state that only exists in memory: the persisted
    block carries the status, the named partition, an UNCAPPED overall_score and
    ``provisional: false`` — so the ceiling rule engages where the gates read."""
    sid = str(uuid4())
    body, citations = _mixed_body(sid)
    judge = _ScriptedJudge(
        shared=[_ok('{"verdicts": ["supported", "supported"]}')],
        absence=[_upstream_502()] * 5,
    )
    rep = await verify_finding_faithfulness(
        body=body, citations=citations, judge_llm=judge
    )
    v = _persisted_verification(rep)
    assert v["judge_status"] == JUDGE_STATUS_PARTIAL
    assert v["judge_unavailable_reason"] == "judge_empty_partition:absence"
    assert v["provisional"] is False
    assert v["provisional_score_ceiling"] is None
    assert v["overall_score"] > PROVISIONAL_SCORE_CEILING
    # …and the receipt says the absence partition burned its whole budget.
    assert v["judge_attempts"] == 1 + JUDGE_RETRY_ATTEMPTS
    assert v["judge_http_statuses"].count("200/502") == JUDGE_RETRY_ATTEMPTS


async def test_a_floored_row_still_names_the_transport_that_floored_it():
    """``judge_empty`` used to be indistinguishable from "the judge said
    nothing". The persisted row now names the upstream status that caused it."""
    sid = str(uuid4())
    body, citations = _mixed_body(sid)
    judge = _ScriptedJudge(shared=[_upstream_502()] * 5, absence=[_upstream_502()] * 5)
    rep = await verify_finding_faithfulness(
        body=body, citations=citations, judge_llm=judge
    )
    v = _persisted_verification(rep)
    assert v["judge_status"] == "deterministic"
    assert v["judge_unavailable_reason"] == "judge_empty"
    assert v["provisional"] is True
    assert set(v["judge_http_statuses"]) == {"200/502"}


async def test_a_row_with_no_judge_call_carries_no_receipt_keys():
    """The other half of the rule: a row where the judge was never ASKED stays
    byte-identical to the pre-2026-09-08 shape."""
    sid = str(uuid4())
    body, citations = _mixed_body(sid)
    rep = await verify_finding_faithfulness(
        body=body, citations=citations, judge_llm=None
    )
    v = _persisted_verification(rep)
    assert "judge_attempts" not in v
    assert "judge_http_statuses" not in v


async def test_the_persisted_block_never_drifts_from_the_report_again():
    """THE CLASS, not the instance. Two serializers exist for one report —
    ``FaithfulnessReport.as_dict`` (the trace envelope) and the hand-written dict
    in ``build_faithfulness_critique_payload`` (the persisted row) — and they
    drift silently, which is how three judged rows shipped with no receipt. Every
    ``judge_*`` key the report publishes must reach the row written down."""
    sid = str(uuid4())
    body, citations = _mixed_body(sid)
    judge = _ScriptedJudge(
        shared=[_upstream_502(), _ok('{"verdicts": ["supported", "supported"]}')],
        absence=[_ok('{"verdicts": ["supported"]}')],
    )
    rep = await verify_finding_faithfulness(
        body=body, citations=citations, judge_llm=judge
    )
    envelope = {k for k in rep.as_dict() if k.startswith("judge_")}
    persisted = set(_persisted_verification(rep))
    missing = sorted(envelope - persisted)
    assert not missing, (
        "the persisted verification block drops judge_* keys the trace envelope "
        f"publishes: {missing} — build_faithfulness_critique_payload is "
        "hand-written and does not derive from FaithfulnessReport.as_dict"
    )
