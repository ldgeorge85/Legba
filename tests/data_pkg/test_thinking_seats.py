# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Thinking per call role, the seats that think, and the safety net.

No model calls: the REAL vLLM handler over ``httpx.MockTransport`` (so the
alias on the wire is what is asserted) and scripted doubles.

* Per-block thinking: ``method.llm.thinking`` is the default for every primary
  call; ``method.gather.thinking`` decides the GATHER rounds and
  ``method.llm.narrate.thinking`` the VOICE. ``ThinkingPlan.for_role`` is the
  one decision; registration refuses a choice nothing reads.
* The shipped seats: which descriptors think, on which role — and the
  high-volume seats that must not.
* The safety net: a thinking call that fails for a reason the alias causes is
  retried ONCE on the plain alias; the retry's receipt carries
  ``thinking_fallback``; a run step, a WARNING and the logged-only
  ``thinking_fallback`` alert follow; anything else is not retried.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
from pathlib import Path
from typing import Any, Mapping
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
import yaml

from legba.data.registry.credentials import MissingSecretError  # noqa: F401 — cycle warm-up

from legba.data import run_accounting
from legba.data.alerts import kinds as alert_kinds
from legba.data.alerts import page_rule
from legba.data.alerts import topics as alert_topics
from legba.data.analysts.agency.agency import AgencyOutcome
from legba.data.analysts.agency.tools import ToolResult
from legba.data.schemas import LLMProviderConfig, Property
from legba.data.schemas.analyst import AnalystDescriptor, GatherBlock, register_analyst_kind
from legba.data.schemas.versioning import content_hash
from legba.data.stack.llm import HardLLMFailure, TransientLLMFailure, VLLMProviderHandler
from legba.data.stack.llm import thinking as th
from legba.runtime.analyst_deps_builder import build_analyst_run_method
from legba.runtime.deps import StandardDeps
from legba.runtime.thinking_fallback_alert import alert_fields, install_thinking_fallback_alerts, uninstall
from legba.shared.thinking_choice import (
    ROLE_DEFAULT,
    ROLE_GATHER,
    ROLE_NARRATE,
    thinking_declaration_errors,
    thinking_plan,
)

REPO = Path(__file__).resolve().parents[2]
DESCRIPTORS = REPO / "descriptors"
BASE_URL = "https://gw.example.test"
CORE_ID = "llm.primary.openai_compat"


# ---------------------------------------------------------------------------
# Doubles: the real handler on a scripted wire
# ---------------------------------------------------------------------------


def _handler(*, alias: str | None = "deep") -> VLLMProviderHandler:
    extra: dict[str, Any] = {}
    if alias is not None:
        extra["thinking_model_name"] = Property.Text.of(alias)
    h = VLLMProviderHandler()
    h._cfg = LLMProviderConfig(  # noqa: SLF001
        api_endpoint=Property.Text.of(f"{BASE_URL}/v1"),
        api_key=Property.Secret.of("llm.primary.key"),
        model_name=Property.Text.of("fast"),
        max_tokens=Property.Number.of(16384, minimum=1, maximum=200000),
        **extra,
    )
    h._api_key = "k"  # noqa: SLF001
    h._instance_id = CORE_ID  # noqa: SLF001
    return h


def _reply(content: str) -> httpx.Response:
    body = {
        "id": "c1", "object": "chat.completion", "created": 1, "model": "x",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }
    return httpx.Response(200, json=body)


class _Wire:
    """Answers each request from ``script(body)``; records every body."""

    def __init__(self, script: Any) -> None:
        self.script = script
        self.bodies: list[dict[str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.bodies.append(body)
        return self.script(body)

    @property
    def models(self) -> list[str]:
        return [b["model"] for b in self.bodies]


def _install(h: VLLMProviderHandler, wire: _Wire) -> VLLMProviderHandler:
    h._client = httpx.AsyncClient(  # noqa: SLF001
        base_url=BASE_URL, headers=h._auth_headers(),  # noqa: SLF001
        transport=httpx.MockTransport(wire), timeout=httpx.Timeout(10.0),
    )
    return h


def _descriptor(name: str, mutate: Any = None) -> AnalystDescriptor:
    for kind in ("journal_assessor", "inquiry", "inline_target"):
        register_analyst_kind(kind)
    body = yaml.safe_load((DESCRIPTORS / f"analyst_{name}.yaml").read_text())
    body.setdefault("identity", {})["version"] = "0" * 16
    if mutate is not None:
        mutate(body)
    return AnalystDescriptor.model_validate(body, strict=False)


async def _build(descriptor: AnalystDescriptor, handler: Any) -> tuple[Any, Any]:
    async def factory(_component_id: str) -> Any:
        return handler

    run_fn, kind_deps, _ok, _rc, _rs = await build_analyst_run_method(
        descriptor,
        deps=StandardDeps(pg_pool=None, nats_publish=None, secrets_resolve=None),
        registry_client=AsyncMock(), pg_pool=None, llm_handler_factory=factory,
    )
    return run_fn, kind_deps


class _FakeBinding:
    def __init__(self, pack_id: str = "substrate_read") -> None:
        self.pack_id = pack_id
        self.calls: list[str] = []

    async def run_tool(self, tool_name: str, args: dict[str, Any], **kwargs: Any) -> AgencyOutcome:
        self.calls.append(tool_name)
        return AgencyOutcome(
            admitted=True, pack_id=self.pack_id, tool_name=tool_name,
            tool_result=ToolResult(status="completed", output={"rows": []}),
        )


# ---------------------------------------------------------------------------
# The decision: ThinkingPlan.for_role
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("llm", "gather", "expected"), [
    ({}, None, {ROLE_DEFAULT: False, ROLE_GATHER: False, ROLE_NARRATE: False}),
    ({"thinking": "on"}, None, {ROLE_DEFAULT: True, ROLE_GATHER: True, ROLE_NARRATE: True}),
    ({"thinking": "on"}, {"thinking": "off"},
     {ROLE_DEFAULT: True, ROLE_GATHER: False, ROLE_NARRATE: True}),
    ({"narrate": {"raw": CORE_ID, "thinking": "on"}}, None,
     {ROLE_DEFAULT: False, ROLE_GATHER: False, ROLE_NARRATE: True}),
    ({"thinking": True, "narrate": {"thinking": False}}, {"thinking": False},
     {ROLE_DEFAULT: True, ROLE_GATHER: False, ROLE_NARRATE: False}),
])
def test_a_block_decides_its_role_and_inherits_the_seat_default(
    llm: dict[str, Any], gather: Any, expected: dict[str, bool],
) -> None:
    plan = thinking_plan(llm, gather)
    assert {r: plan.for_role(r) for r in expected} == expected


def test_registration_refuses_a_choice_nothing_reads() -> None:
    # verify / judge are not thinking blocks (the judge route can leave the plane).
    for block in ("verify", "judge", "primary"):
        errs = thinking_declaration_errors(
            "inline_target", {block: {"raw": CORE_ID, "thinking": "on"}})
        assert errs and "not a thinking block" in errs[0], block
    # narrate is read only by the voice kinds; gather only by the gathering kinds.
    assert thinking_declaration_errors("inline_target", {"narrate": {"thinking": "on"}})
    assert thinking_declaration_errors("meta_findings_synthesizer", {}, {"thinking": "off"})
    assert thinking_declaration_errors("consult_on_demand", {}, {"thinking": "on"})
    # ... and accepted where they are read.
    assert not thinking_declaration_errors("journal_assessor", {"narrate": {"thinking": "on"}},
                                           {"thinking": "off"})
    assert not thinking_declaration_errors("inline_target", {"thinking": "on"}, {"thinking": "off"})
    # a misspelt value is refused, never read as "off"
    assert "not a thinking choice" in thinking_declaration_errors(
        "inline_target", {}, {"thinking": "sometimes"})[0]


def test_the_descriptor_model_runs_the_refusals() -> None:
    def gather_on_a_synthesizer(body: dict[str, Any]) -> None:
        body["method"]["gather"] = {"thinking": "off"}

    with pytest.raises(ValueError, match="read only by the kinds"):
        _descriptor("world_assessment", gather_on_a_synthesizer)

    def verify_thinks(body: dict[str, Any]) -> None:
        body["method"]["llm"]["verify"]["thinking"] = "on"

    with pytest.raises(ValueError, match="not a thinking block"):
        _descriptor("journal_assessor", verify_thinks)

    with pytest.raises(ValueError, match="not a thinking choice"):
        GatherBlock(thinking="maybe")


def test_an_undeclared_gather_choice_leaves_every_content_hash_alone() -> None:
    assert GatherBlock().model_dump() == {"max_rounds": 1}
    assert GatherBlock(thinking="off").model_dump() == {"max_rounds": 1, "thinking": "off"}
    # A descriptor this change did not touch hashes exactly as before.
    d = _descriptor("relationship_reifier")
    assert "thinking" not in d.model_dump(mode="json")["method"]["gather"]
    assert content_hash(d) == content_hash(d.model_copy())


# ---------------------------------------------------------------------------
# The shipped seats
# ---------------------------------------------------------------------------

DESKS = (
    "narrative_coordination", "internal_stability", "leadership_transition", "economic_coercion",
    "energy_security", "escalation", "military_posture",
    "disruption_status", "cyber_watch",
)
SYNTHESIS_SEATS = ("world_assessment", "competing_hypotheses", "fact_contention_arbiter")
#: Switched off on the first live measurement (2026-11-15): three to four times
#: slower with the verifier score flat (proliferation_watch 8 runs 46.8 s vs
#: 13.4 s, 0.988 vs 0.979; country_assessment 32 runs 57.6 s vs 16.0 s, 0.725 vs 0.718).
MEASURED_OFF = ("proliferation_watch", "country_assessment")
VOICE_SEATS = ("journal_assessor", "chronicle_assessor", "inquiry_pilot", "crossroads",
               "lens_baserate", "lens_capability", "lens_diff", "lens_intent", "lens_trend")
NEVER = ("relationship_reifier", "correctness_grader", "signal_salience", "signal_summarizer",
         "reference_builder", "situation_tracker", "claim_watch", "entity_researcher",
         "contrary_evidence_pass")


def _plan(name: str) -> dict[str, bool]:
    d = _descriptor(name)
    plan = thinking_plan(d.method.llm, d.method.gather)
    return {r: plan.for_role(r) for r in (ROLE_DEFAULT, ROLE_GATHER, ROLE_NARRATE)}


@pytest.mark.parametrize("name", DESKS)
def test_a_desk_thinks_on_its_synthesis_and_not_on_its_gather(name: str) -> None:
    p = _plan(name)
    assert p[ROLE_DEFAULT] is True and p[ROLE_GATHER] is False


@pytest.mark.parametrize("name", SYNTHESIS_SEATS)
def test_the_low_volume_synthesis_seats_think(name: str) -> None:
    assert _plan(name)[ROLE_DEFAULT] is True


@pytest.mark.parametrize("name", MEASURED_OFF)
def test_a_seat_measured_slower_and_no_better_does_not_think(name: str) -> None:
    assert not any(_plan(name).values())


@pytest.mark.parametrize("name", VOICE_SEATS)
def test_a_voice_seat_thinks_on_its_voice_only(name: str) -> None:
    assert _plan(name) == {ROLE_DEFAULT: False, ROLE_GATHER: False, ROLE_NARRATE: True}


@pytest.mark.parametrize("name", NEVER)
def test_the_high_volume_seats_do_not_think(name: str) -> None:
    assert (DESCRIPTORS / f"analyst_{name}.yaml").exists(), name
    assert not any(_plan(name).values())


# ---------------------------------------------------------------------------
# Which calls carry the thinking alias — on the wire
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_desk_sends_its_gather_rounds_fast_and_its_synthesis_deep() -> None:
    """energy_security as shipped: one GATHER round (tool call, then done) on
    the plain alias, the synthesis on the thinking alias."""
    final = ('{"title": "T", "body": "b", "confidence": 0.6, "evidence": [], "tags": []}')
    replies = iter([
        '{"tool": "query_facts", "args": {"subject": "X"}}',
        '{"done": true}',
        final,
    ])
    wire = _Wire(lambda body: _reply(next(replies)))
    run_fn, _ = await _build(_descriptor("energy_security"), _install(_handler(), wire))
    inner = dataclasses.replace(run_fn._deps, max_rounds=2)  # noqa: SLF001
    binding = _FakeBinding()
    from legba.data.analysts.inline_target import run_method

    row = {"id": uuid4(), "title": "t", "produced_at": "2026-11-14T00:00:00+00:00",
           "source_url": "https://e.test/a", "data": {"summary": "s"}}
    await run_method([row], {"target_id": "t1", "agency_binding": binding}, inner)
    assert binding.calls == ["query_facts"]
    assert wire.models == ["fast", "fast", "deep"]


@pytest.mark.asyncio
async def test_a_journal_voice_thinks_and_its_gather_does_not() -> None:
    """journal_assessor as shipped: GATHER on the plain alias, the field notes
    and the narrate on the thinking alias."""
    def script(body: dict[str, Any]) -> httpx.Response:
        system = body["messages"][0]["content"] if body["messages"][0]["role"] == "system" else ""
        if "Reasoning: high" in system:  # the GATHER prompt carries the directive
            return _reply('{"done": true}')
        return _reply("# Entry\n\nThe window was quiet.\n\nI wonder what we are missing.")

    wire = _Wire(script)
    _run, deps = await _build(_descriptor("journal_assessor"), _install(_handler(), wire))
    binding = _FakeBinding("journal_read")
    from legba.data.analysts.journal_assessor import run_method

    deps = dataclasses.replace(deps, max_rounds=1, agency_binding=binding)
    await run_method([{"title": "seed"}], {"analyst_id": "journal_assessor",
                                           "agency_binding": binding,
                                           "gather_tool_bindings": {}}, deps)
    gather = [b["model"] for b in wire.bodies
              if "Reasoning: high" in str(b["messages"][0].get("content"))]
    voice = [b["model"] for b in wire.bodies
             if "Reasoning: high" not in str(b["messages"][0].get("content"))]
    assert gather and set(gather) == {"fast"}
    assert len(voice) >= 2 and set(voice) == {"deep"}


@pytest.mark.asyncio
async def test_a_seat_with_no_choice_is_byte_identical() -> None:
    wire = _Wire(lambda body: _reply('{"title": "T", "body": "b", "confidence": 0.5}'))
    run_fn, _ = await _build(_descriptor("energy_security", _undeclare), _install(_handler(), wire))
    assert run_fn._deps.llm_gather is None  # noqa: SLF001
    assert isinstance(run_fn._deps.llm, VLLMProviderHandler)  # noqa: SLF001 — not wrapped


def _undeclare(body: dict[str, Any]) -> None:
    body["method"]["llm"].pop("thinking", None)
    body["method"].pop("gather", None)


# ---------------------------------------------------------------------------
# The safety net
# ---------------------------------------------------------------------------


def _error(status: int, message: str) -> httpx.Response:
    return httpx.Response(status, json={"error": {"message": message}})


@pytest.fixture
def account() -> Any:
    token = run_accounting.bind_run_accounting()
    steps = run_accounting.bind_run_steps()
    yield steps
    run_accounting.reset_run_accounting(token)


@pytest.fixture
def sink_events() -> Any:
    events: list[Mapping[str, Any]] = []
    sink = events.append
    th.add_thinking_fallback_sink(sink)
    yield events
    th.remove_thinking_fallback_sink(sink)


@pytest.mark.asyncio
@pytest.mark.parametrize(("status", "message", "tools", "reason"), [
    (400, "tool_choice is not supported with this model", True, "tools_refused"),
    (400, "Unrecognized request argument supplied: thinking_budget", False,
     "unsupported_parameter"),
    (404, "The model `deep` does not exist", False, "alias_unknown"),
    (422, "bad request", False, "request_rejected"),
])
async def test_a_thinking_failure_retries_once_on_the_plain_alias(
    account: Any, sink_events: Any, caplog: Any,
    status: int, message: str, tools: bool, reason: str,
) -> None:
    replies = iter([_error(status, message), _reply("ok")])
    wire = _Wire(lambda body: next(replies))
    h = _install(_handler(), wire)
    tool_spec = [{"type": "function", "function": {"name": "f", "parameters": {}}}]
    resp = await h.chat_complete([{"role": "user", "content": "q"}], thinking=True,
                                 tools=tool_spec if tools else None)
    assert resp.content == "ok"
    assert wire.models == ["deep", "fast"]
    failed, retried = run_accounting.current_llm_calls()
    assert failed["thinking"] is True and failed["status"] == "hard_fail"
    assert "thinking_fallback" not in failed
    assert retried["thinking_fallback"] == reason and "thinking" not in retried
    assert retried["status"] == "success"
    assert [s["reason"] for s in account if s.get("kind") == "thinking_fallback"] == [reason]
    assert [e["reason"] for e in sink_events] == [reason]
    assert "llm.thinking_fallback" in caplog.text


@pytest.mark.asyncio
async def test_a_reasoning_body_that_does_not_parse_falls_back(account: Any, sink_events: Any) -> None:
    replies = iter([httpx.Response(200, text="data: {broken", headers={"content-type": "text/plain"}),
                    _reply("ok")])
    wire = _Wire(lambda body: next(replies))
    resp = await _install(_handler(), wire).chat_complete(
        [{"role": "user", "content": "q"}], thinking=True)
    assert resp.content == "ok" and wire.models == ["deep", "fast"]
    assert [e["reason"] for e in sink_events] == ["reasoning_parse"]


@pytest.mark.asyncio
@pytest.mark.parametrize(("status", "message"), [
    (401, "invalid api key"),                                   # the route, not the alias
    (400, "This model's maximum context length is 8192 tokens"),  # too long on either alias
])
async def test_a_failure_the_alias_did_not_cause_is_not_retried(
    account: Any, sink_events: Any, status: int, message: str,
) -> None:
    wire = _Wire(lambda body: _error(status, message))
    with pytest.raises(HardLLMFailure):
        await _install(_handler(), wire).chat_complete(
            [{"role": "user", "content": "q"}], thinking=True)
    assert wire.models == ["deep"] and sink_events == []


@pytest.mark.asyncio
async def test_a_thinking_off_call_and_a_missing_alias_are_never_retried(sink_events: Any) -> None:
    wire = _Wire(lambda body: _error(400, "tool_choice is not supported"))
    with pytest.raises(HardLLMFailure):
        await _install(_handler(), wire).chat_complete([{"role": "user", "content": "q"}])
    assert wire.models == ["fast"]
    wire2 = _Wire(lambda body: _reply("never"))
    with pytest.raises(HardLLMFailure):
        await _install(_handler(alias=None), wire2).chat_complete(
            [{"role": "user", "content": "q"}], thinking=True)
    assert wire2.bodies == [] and sink_events == []


@pytest.mark.asyncio
async def test_the_retry_fails_too_and_that_failure_propagates(sink_events: Any) -> None:
    wire = _Wire(lambda body: _error(400, "unsupported parameter"))
    with pytest.raises(HardLLMFailure):
        await _install(_handler(), wire).chat_complete(
            [{"role": "user", "content": "q"}], thinking=True)
    assert wire.models == ["deep", "fast"]  # once, never twice
    assert len(sink_events) == 1


def test_transient_and_cancellation_are_never_thinking_failures() -> None:
    assert th.thinking_failure_reason(TransientLLMFailure("503", status=503)) is None
    assert th.thinking_failure_reason(asyncio.CancelledError()) is None
    assert th.thinking_failure_reason(TimeoutError()) is None
    unavailable = HardLLMFailure("no alias")
    unavailable.__cause__ = th.ThinkingUnavailable("no alias")
    assert th.thinking_failure_reason(unavailable) is None
    assert th.thinking_failure_reason(HardLLMFailure("x", status=403)) is None


@pytest.mark.asyncio
async def test_a_bound_seat_handler_falls_back_through_its_binding(sink_events: Any) -> None:
    replies = iter([_error(400, "unsupported"), _reply("ok")])
    wire = _Wire(lambda body: next(replies))
    bound = th.bind_thinking(_install(_handler(), wire), True, analyst_id="desk")
    assert (await bound.chat_complete([{"role": "user", "content": "q"}])).content == "ok"
    assert wire.models == ["deep", "fast"]


# ---------------------------------------------------------------------------
# The alert: in the closed list, engine topic, never pages by default
# ---------------------------------------------------------------------------


def test_the_alert_kind_is_closed_listed_and_never_pages() -> None:
    kind = alert_kinds.require_kind("thinking_fallback")
    assert kind.subject_kind == "route" and kind.lifecycle == "event"
    assert alert_topics.TOPIC_BY_KIND["thinking_fallback"] == alert_topics.TOPIC_ENGINE
    rule = page_rule.load_rule()
    for sev in ("info", "low", "medium", "high", "critical"):
        assert rule.match("thinking_fallback", sev, CORE_ID) is None


@pytest.mark.asyncio
async def test_the_runtime_sink_raises_one_alert_per_fallback_on_the_pool_loop() -> None:
    raised: list[dict[str, Any]] = []

    async def fake_raise(pool: Any, **kwargs: Any) -> None:
        raised.append({"pool": pool, **kwargs})

    sink = install_thinking_fallback_alerts("POOL", raise_fn=fake_raise)
    try:
        th.note_thinking_fallback(component_id=CORE_ID, reason="tools_refused",
                                  error=HardLLMFailure("x", status=400))
        for _ in range(20):
            if raised:
                break
            await asyncio.sleep(0.01)
    finally:
        uninstall(sink)
    (alert,) = raised
    assert alert["pool"] == "POOL" and alert["kind"] == "thinking_fallback"
    assert alert["dedupe_key"] == f"thinking_fallback:{CORE_ID}:tools_refused"
    assert alert == {"pool": "POOL", **alert_fields({
        "component_id": CORE_ID, "reason": "tools_refused", "error": "HardLLMFailure",
        "http_status": 400})}


# ---------------------------------------------------------------------------
# The staged switch-on: an id-limited walk
# ---------------------------------------------------------------------------


def test_the_walker_limits_a_plan_to_named_ids_and_refuses_a_typo() -> None:
    import os
    import sys

    had = "LEGBA_DATA_PG_DB" in os.environ
    sys.path.insert(0, str(REPO / "scripts"))
    try:
        import bringup_register_descriptors as walker
    finally:
        sys.path.remove(str(REPO / "scripts"))
        if not had:
            os.environ.pop("LEGBA_DATA_PG_DB", None)
    planned, excluded = walker.plan()
    half = {"energy_security", "escalation"}
    got, gone = walker.limit_to_ids(planned, excluded, half)
    assert {p.descriptor_id for p in got} == half and gone == []
    with pytest.raises(walker.PlanError, match="does not plan"):
        walker.limit_to_ids(planned, excluded, {"energy_securty"})
