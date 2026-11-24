# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""JUDGE-2 (W11b) — the judge FAILURE CHAIN (``runtime/judge_fallback_chain.py``).

The 2026-10-02 → 10-05 OpenRouter 402 outage floored every critique for ~51 h
while a $0 in-house rung sat healthy. These tests pin the chain's contract:

  * a route-gone class (401/402/403/404, or an insufficient-credits body) MOVES
    to the next rung for that pass and stamps the rung actually used;
  * a 429 / 5xx / timeout does NOT move — it re-raises on the current rung and
    the REAL transport shim (``judge_transport.call_judge_with_retry``) retries
    it exactly as before;
  * every rung failing reaches the floor and stamps ``unverified_reason`` with
    the LAST rung's class;
  * the default chain wires byte-identically to today's single route when rung 1
    answers (same ref in → same client out; unchanged stamps; no extras);
  * ``method.llm.judge_chain`` parses through the runtime ``AnalystDescriptor``
    model (the 09-28 rule: a registry 200 is not a runtime parse).

No network, no DB, no paid call: the rungs are handler doubles raising the real
``HardLLMFailure`` / ``TransientLLMFailure`` the provider plane raises.
"""

from __future__ import annotations

import json
import re
from types import SimpleNamespace
from typing import Any

import pytest

from legba.data.provenance.judge_failure_class import (
    classify_exception,
    classify_status,
    is_route_gone,
)
from legba.data.provenance.judge_transport import (
    JudgeTransportTelemetry,
    call_judge_with_retry,
)
from legba.data.stack.llm.base import HardLLMFailure, RouterStatus, TransientLLMFailure
from legba.runtime import judge_fallback_chain as JC
from legba.runtime.analyst_deps_builder import JudgeRoute

R1 = "llm.judge.hosted_a"
R2 = "llm.judge.hosted_b"
R3 = "llm.inhouse.small"


@pytest.fixture(autouse=True)
def _no_chain_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(JC.JUDGE_CHAIN_DEFAULT_ENV, raising=False)
    monkeypatch.delenv(JC.JUDGE_WEAK_RUNGS_ENV, raising=False)


class _Rung:
    """A handler double: a scripted list of outcomes (Exception → raised;
    anything else → returned), recording every call's exact arguments."""

    def __init__(self, name: str, outcomes: list[Any], *, model: str = "") -> None:
        self.subprovider = f"sub-{name}"
        self._cfg = SimpleNamespace(model_name=SimpleNamespace(raw=model or f"model-{name}"))
        self._outcomes = list(outcomes)
        self.calls: list[tuple[Any, dict]] = []

    async def chat_complete(self, messages: Any, **kwargs: Any) -> Any:
        self.calls.append((messages, kwargs))
        out = self._outcomes.pop(0) if self._outcomes else self._outcomes_default()
        if isinstance(out, BaseException):
            raise out
        return out

    def _outcomes_default(self) -> Any:
        return _ok("{}")


def _ok(content: str) -> Any:
    return SimpleNamespace(content=content, raw_response={"choices": [{}]})


def _chain(*rungs: _Rung, refs: tuple[str, ...] = (R1, R2, R3)) -> tuple[JC.JudgeChain, list[str]]:
    built: list[str] = []
    by_ref = dict(zip(refs, rungs))

    async def factory(ref: str) -> Any:
        built.append(ref)
        return by_ref[ref]

    chain = JC.JudgeChain(refs[: len(rungs)], first_handler=rungs[0], factory=factory,
                          route_class="configured")
    return chain, built


def _report(status: str, reason: str | None = None, statuses: list[str] | None = None):
    return SimpleNamespace(
        judge_status=status, judge_unavailable_reason=reason,
        judge_http_statuses=statuses or [],
    )


# ---------------------------------------------------------------------------
# The failure class — one vocabulary for the chain, the watchdog, the route
# ---------------------------------------------------------------------------


def test_route_gone_classes_are_exactly_401_402_403_404() -> None:
    assert [classify_status(s) for s in (401, 402, 403, 404)] == [
        "auth", "credit", "auth", "retired",
    ]
    assert all(is_route_gone(classify_status(s)) for s in (401, 402, 403, 404))
    for busy in (429, 500, 502, 503, "timeout", "empty", 400):
        assert not is_route_gone(classify_status(busy)), busy
    # A router-wrapped 402 (HTTP 200, code 402 in the body) is still a 402.
    assert classify_status("200/402") == "credit"
    assert classify_exception(HardLLMFailure("x", status=RouterStatus(402))) == ("credit", 402)


def test_an_insufficient_credits_body_is_credit_whatever_the_status() -> None:
    exc = HardLLMFailure("bad request", status=400, body='{"error":"Insufficient credits"}')
    assert classify_exception(exc) == ("credit", 400)
    # …but never upgrades a retryable status (a 429 stays a throttle).
    exc429 = TransientLLMFailure("insufficient credits?", status=429)
    assert classify_exception(exc429)[0] == "rate_limited"


# ---------------------------------------------------------------------------
# Resolution — rung 1 is ALWAYS the ladder's route; the knob supplies the rest
# ---------------------------------------------------------------------------


def test_default_chain_is_resolved_route_then_the_free_inhouse_rung_only() -> None:
    """House rule: the DEFAULT fallback never moves spend — the $0 in-house rung
    only; a paid second rung is opt-in (``judge_chain`` or the env default)."""
    assert JC.DEFAULT_JUDGE_CHAIN_TAIL == ("llm.verify.slm_8b",)
    assert JC.resolve_judge_chain({}, R1) == (R1, "llm.verify.slm_8b")
    assert not any("openrouter" in r for r in JC.DEFAULT_JUDGE_CHAIN_TAIL)


def test_a_paid_second_rung_is_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    paid = "llm.judge.openrouter_mistral_large.openai_compat"
    llm = {"judge_chain": [paid, "llm.verify.slm_8b"]}
    assert JC.resolve_judge_chain(llm, R1) == (R1, paid, "llm.verify.slm_8b")
    monkeypatch.setenv(JC.JUDGE_CHAIN_DEFAULT_ENV, f"{paid},llm.verify.slm_8b")
    assert JC.resolve_judge_chain({}, R1) == (R1, paid, "llm.verify.slm_8b")


def test_declared_chain_follows_rung_one_deduplicated() -> None:
    llm = {"judge_chain": [R1, {"factory_kind": "stack_ref", "raw": R2}, R2, R3]}
    assert JC.resolve_judge_chain(llm, R1) == (R1, R2, R3)


def test_an_empty_declared_chain_is_todays_single_route() -> None:
    assert JC.resolve_judge_chain({"judge_chain": []}, R1) == (R1,)


def test_the_deployment_default_is_an_env_tunable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(JC.JUDGE_CHAIN_DEFAULT_ENV, f"{R3}, {R2}")
    assert JC.resolve_judge_chain({}, R1) == (R1, R3, R2)
    monkeypatch.setenv(JC.JUDGE_CHAIN_DEFAULT_ENV, "")
    assert JC.resolve_judge_chain({}, R1) == (R1,)


def test_a_malformed_chain_degrades_to_the_single_route() -> None:
    assert JC.resolve_judge_chain({"judge_chain": "llm.x"}, R1) == (R1,)


def test_judge_chain_alone_never_opts_a_descriptor_into_judging() -> None:
    """Rung 0 of the ladder (the judge/verify KEY) still gates judging — the
    chain knob re-routes failures, it never conscripts an analyst."""
    from legba.runtime.analyst_deps_builder import resolve_judge_route_from_llm_block

    assert resolve_judge_route_from_llm_block({"judge_chain": [R2]}) is None


@pytest.mark.parametrize("kind", ["inline_target", "meta_findings_synthesizer"])
def test_the_knob_parses_on_the_runtime_descriptor_model(kind: str) -> None:
    """The 09-28 rule: the runtime model must PARSE a descriptor carrying it —
    ``method.llm`` is the open route block, so no kind catalog can refuse it."""
    from legba.data.schemas.analyst import (
        AnalystDescriptor,
        AnalystIdentity,
        AnalystKind,
        CadenceBlock,
        MappingBlock,
        MethodBlock,
        SubscriptionBlock,
        TypeSignature,
    )
    from legba.data.schemas.lifecycle import LifecycleState

    desc = AnalystDescriptor(
        identity=AnalystIdentity(
            id="judge2_probe", name="probe", schema_uri="legba/analyst/1.0.0",
            version="0" * 64, kind=AnalystKind(kind),
            type_signature=TypeSignature(input_type="a.In", output_type="a.Out"),
            state=LifecycleState.ACTIVE, owner="test",
        ),
        subscription=SubscriptionBlock(),
        mapping=MappingBlock(),
        method=MethodBlock(
            kind="llm_single_turn", prompt_module="legba.prompts.inline_target.v1",
            llm={
                "verify": {"factory_kind": "stack_ref", "raw": R1},
                "judge_chain": [
                    {"factory_kind": "stack_ref", "raw": R2},
                    {"factory_kind": "stack_ref", "raw": R3},
                ],
            },
        ),
        cadence=CadenceBlock(fallback_schedule="0 0 1 1 *"),
    )
    again = AnalystDescriptor.model_validate(json.loads(desc.model_dump_json()))
    assert JC.resolve_judge_chain(again.method.llm, R1) == (R1, R2, R3)


# ---------------------------------------------------------------------------
# The session — 402 moves, 429 does not
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_402_moves_to_the_next_rung_and_stamps_the_rung_used() -> None:
    r1 = _Rung("a", [HardLLMFailure("payment required", status=402)])
    r2 = _Rung("b", [_ok("verdicts")], model="vendor/model-b")
    chain, built = _chain(r1, r2)
    session = chain.session()
    resp = await session.chat_complete([{"role": "user", "content": "x"}], max_tokens=7)
    assert resp.content == "verdicts"
    assert built == [R2]  # rung 2 built LAZILY, on the 402
    assert r2.calls == [([{"role": "user", "content": "x"}], {"max_tokens": 7})]
    model, ref, route, extras = JC.judge_stamps(
        judge=chain, session=session, judge_model="sub-a", judge_llm_ref=R1,
        judge_route="configured", report=_report("llm"),
    )
    assert (model, ref, route) == ("vendor/model-b", R2, f"chain:{R2}")
    assert extras["judge_chain"]["used"] == R2 and extras["judge_chain"]["rung"] == 2
    assert extras["judge_chain"]["tried"][0]["class"] == "credit"
    assert extras["judge_chain"]["tried"][0]["http_status"] == "402"
    assert extras["judge_chain"]["weak"] is False
    assert "unverified_reason" not in extras


@pytest.mark.asyncio
async def test_the_session_is_sticky_and_a_new_pass_starts_at_rung_one() -> None:
    r1 = _Rung("a", [HardLLMFailure("x", status=401), _ok("r1-back")])
    r2 = _Rung("b", [_ok("p1"), _ok("p2")])
    chain, _ = _chain(r1, r2)
    session = chain.session()
    assert (await session.chat_complete([])).content == "p1"
    # The second partition of the SAME pass does not re-ask the dead key.
    assert (await session.chat_complete([])).content == "p2"
    assert len(r1.calls) == 1
    # A new pass (the next critique) asks rung 1 first — recovery is immediate.
    assert (await chain.session().chat_complete([])).content == "r1-back"


@pytest.mark.asyncio
async def test_a_429_does_not_move_and_the_real_shim_retries_rung_one() -> None:
    """Driven through the REAL transport shim: three 429s are three attempts on
    rung 1, then the shim re-raises — rung 2 is never built, never asked."""
    r1 = _Rung("a", [TransientLLMFailure("busy", status=429)] * 3)
    r2 = _Rung("b", [_ok("never")])
    chain, built = _chain(r1, r2)
    session = chain.session()
    telem = JudgeTransportTelemetry()
    sleeps: list[float] = []

    async def _sleep(s: float) -> None:
        sleeps.append(s)

    with pytest.raises(TransientLLMFailure):
        await call_judge_with_retry(
            session, evidence_prompt="p", system="s", telemetry=telem, sleeper=_sleep,
        )
    assert len(r1.calls) == 3 and r2.calls == [] and built == []
    assert telem.http_statuses == ["429", "429", "429"]
    assert len(sleeps) == 2  # today's backoff, unchanged


@pytest.mark.asyncio
async def test_through_the_real_shim_a_402_is_one_answered_attempt() -> None:
    r1 = _Rung("a", [HardLLMFailure("x", status=402)])
    r2 = _Rung("b", [_ok('{"verdicts": ["supported"]}')])
    chain, _ = _chain(r1, r2)
    telem = JudgeTransportTelemetry()
    content = await call_judge_with_retry(
        chain.session(), evidence_prompt="p", system="s", telemetry=telem,
    )
    assert "supported" in content
    assert telem.http_statuses == ["200"]  # the shim saw an answer, not a 402


@pytest.mark.asyncio
async def test_every_rung_failing_reaches_the_floor_with_the_last_class() -> None:
    r1 = _Rung("a", [HardLLMFailure("x", status=402)])
    r2 = _Rung("b", [HardLLMFailure("x", status=402)])
    r3 = _Rung("c", [HardLLMFailure("x", status=404)])
    chain, built = _chain(r1, r2, r3)
    session = chain.session()
    with pytest.raises(HardLLMFailure):
        await session.chat_complete([])
    assert built == [R2, R3]
    model, ref, route, extras = JC.judge_stamps(
        judge=chain, session=session, judge_model="sub-a", judge_llm_ref=R1,
        judge_route="configured", report=_report("deterministic", "judge_error", ["404"]),
    )
    # The floor keeps today's stamps (rung 1's), and SAYS why it is the floor.
    assert (model, ref, route) == ("sub-a", R1, "configured")
    reason = extras["unverified_reason"]
    assert reason["class"] == "retired" and reason["http_status"] == "404"
    assert reason["component_id"] == R3 and reason["rungs_tried"] == 3
    assert reason["judge_unavailable_reason"] == "judge_error" and reason["at"]
    assert extras["judge_chain"]["used"] is None
    assert [t["rung"] for t in extras["judge_chain"]["tried"]] == [1, 2, 3]


@pytest.mark.asyncio
async def test_an_unbuildable_rung_is_skipped_not_fatal() -> None:
    r1 = _Rung("a", [HardLLMFailure("x", status=402)])
    r3 = _Rung("c", [_ok("small")])

    async def factory(ref: str) -> Any:
        if ref == R2:
            raise RuntimeError("component not registered")
        return r3

    chain = JC.JudgeChain((R1, R2, R3), first_handler=r1, factory=factory)
    session = chain.session()
    assert (await session.chat_complete([])).content == "small"
    assert [f["class"] for f in session.failures] == ["credit", "unresolvable"]
    *_, extras = JC.judge_stamps(
        judge=chain, session=session, judge_model="", judge_llm_ref=R1,
        judge_route="configured", report=_report("llm"),
    )
    assert extras["judge_chain"]["used"] == R3


@pytest.mark.asyncio
async def test_the_in_house_rung_is_labelled_weak(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(JC.JUDGE_WEAK_RUNGS_ENV, R3)
    r1 = _Rung("a", [HardLLMFailure("x", status=402)])
    r2 = _Rung("b", [HardLLMFailure("x", status=402)])
    r3 = _Rung("c", [_ok("v")], model="Small-8B-Instruct")
    chain, _ = _chain(r1, r2, r3)
    session = chain.session()
    await session.chat_complete([])
    model, _ref, route, extras = JC.judge_stamps(
        judge=chain, session=session, judge_model="sub-a", judge_llm_ref=R1,
        judge_route="configured", report=_report("llm"),
    )
    assert model == "Small-8B-Instruct" and route == f"chain:{R3}"
    assert extras["judge_chain"]["weak"] is True


def test_a_plain_handler_floor_still_says_why() -> None:
    """No chain wired (``judge_chain: []``): the floor reads the shim's receipt."""
    *_, extras = JC.judge_stamps(
        judge=object(), session=object(), judge_model="sub", judge_llm_ref=R1,
        judge_route="configured", report=_report("deterministic", "judge_error", ["402"]),
    )
    assert extras["unverified_reason"]["class"] == "credit"
    assert extras["unverified_reason"]["http_status"] == "402"
    assert "judge_chain" not in extras


def test_floors_that_never_asked_a_judge_carry_no_unverified_reason() -> None:
    for reason in ("flag_off", "no_judge_component", None):
        *_, extras = JC.judge_stamps(
            judge=object(), session=None, judge_model="", judge_llm_ref="",
            judge_route="", report=_report("deterministic", reason),
        )
        assert extras == {}
    *_, extras = JC.judge_stamps(
        judge=None, session=None, judge_model="", judge_llm_ref="", judge_route="",
        report=_report("deterministic", "judge_error"),
    )
    assert extras == {}


# ---------------------------------------------------------------------------
# Wiring — byte-identical to today's single route when rung 1 answers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wiring_same_ref_in_same_client_out() -> None:
    cache: dict[str, Any] = {}
    calls: list[str] = []

    async def factory(ref: str) -> Any:
        calls.append(ref)
        return cache.setdefault(ref, _Rung(ref, [_ok("rung-1 verdict")]))

    route = JudgeRoute(component_id=R1, source="env:LEGBA_JUDGE_STACK_REF")
    judge, ref, route_class = await JC.wire_judge_chain(
        route, {"verify": R1}, llm_handler_factory=factory,
    )
    assert calls == [R1]  # ONE ref resolved at wiring, as before
    assert (ref, route_class) == (R1, "configured")
    assert isinstance(judge, JC.JudgeChain)
    assert judge.first_handler is cache[R1]  # the SAME client object
    assert judge.subprovider == cache[R1].subprovider
    session = JC.open_session(judge)
    resp = await session.chat_complete([{"role": "user", "content": "q"}], temperature=0.0)
    assert resp.content == "rung-1 verdict"
    assert cache[R1].calls == [([{"role": "user", "content": "q"}], {"temperature": 0.0})]
    stamps = JC.judge_stamps(
        judge=judge, session=session, judge_model="sub-x", judge_llm_ref=R1,
        judge_route="configured", report=_report("llm"),
    )
    assert stamps == ("sub-x", R1, "configured", {})  # byte-identical stamps


@pytest.mark.asyncio
async def test_a_one_rung_chain_returns_the_bare_client() -> None:
    first = _Rung("a", [])

    async def factory(ref: str) -> Any:
        return first

    route = JudgeRoute(component_id=R1, source="method.llm.judge")
    judge, ref, cls = await JC.wire_judge_chain(
        route, {"judge": R1, "judge_chain": []}, llm_handler_factory=factory,
    )
    assert judge is first and ref == R1 and cls == "configured"
    assert JC.open_session(judge) is first


@pytest.mark.asyncio
async def test_wiring_soft_fails_to_the_floor() -> None:
    async def factory(ref: str) -> Any:
        raise RuntimeError("registry 500")

    route = JudgeRoute(component_id=R1, source="method.llm.verify")
    assert await JC.wire_judge_chain(route, {}, llm_handler_factory=factory) == (None, "", "")


def test_the_host_calls_the_leaf_not_an_inline_copy() -> None:
    """dapr_host's resolver delegates to ``wire_judge_chain`` (one wiring path)."""
    from pathlib import Path

    import legba.runtime.dapr_host as host

    src = Path(host.__file__).read_text(encoding="utf-8")
    assert "wire_judge_chain(" in src
    assert re.search(r"verify_judge = await _llm_handler_factory\(", src) is None
