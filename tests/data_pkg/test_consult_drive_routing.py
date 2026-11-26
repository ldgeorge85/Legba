# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Consult presents a case: the drive tools through the REAL binding path.

Design C-1a §7, backend tests 1-7 and 10-11. Every drive tool runs through the
case gate and then ``AgencyToolBinding.run_tool`` → ``Agency.run_pack_tool`` →
the registered canonicalizer, so the ``action_pack_invocations`` ledger and
the ``governor_events`` ALLOW/BLOCK rows are the real ones, read back here.

Real migrated Postgres; the real ``workstation_drive`` and ``substrate_read``
pack descriptors from ``descriptors/``; the drive binding is built by the real
``runtime/consult_bindings.build_drive_binding``. The LLM is a scripted double
(the one permitted boundary double) and the substrate port an in-memory one.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio
import yaml

from legba.data import workstation_vocabulary as V
from legba.data.analysts.agency import Agency, AgencyToolBinding, GLOBAL_SCOPE, ToolContext
from legba.data.analysts.agency.workstation_drive import parse_instant
from legba.data.analysts.consult_case import CaseGate, harvest_refs
from legba.data.analysts.consult_case_schema import DRIVE_TOOLS
from legba.data.analysts.consult_on_demand import ConsultOnDemandDeps, run_method
from legba.data.schemas.action_pack import ActionPack, ActionPackRef
from legba.data.stack.llm.tool_rounds import WRITE_TOOLS
from legba.runtime.consult_bindings import build_drive_binding

pytestmark = [pytest.mark.asyncio]

_DESCRIPTORS = Path(__file__).resolve().parents[2] / "descriptors"

T1, T2 = "subject_alpha", "subject_beta"
F1 = "0b7a3c1e-0000-4000-8000-0000000000f1"
S1 = "0b7a3c1e-0000-4000-8000-0000000000a1"
SIG = "0b7a3c1e-0000-4000-8000-0000000000c1"
PINNED_REPORT = "0b7a3c1e-0000-4000-8000-0000000000e1"


@pytest_asyncio.fixture
async def pool(migrated_pg):
    p = await asyncpg.create_pool(
        host=migrated_pg.host, port=migrated_pg.port, user=migrated_pg.user,
        password=migrated_pg.password, database=migrated_pg.database,
        min_size=1, max_size=4,
    )
    yield p
    await p.close()


def _body(fname: str) -> dict[str, Any]:
    body = yaml.safe_load((_DESCRIPTORS / fname).read_text())
    body["identity"]["version"] = "0" * 16
    return body


class _Registry:
    """``fetch_action_pack``'s registry client: serves the real pack body."""

    async def get_descriptor(self, pack_id: str, *, family: str) -> dict[str, Any] | None:
        assert family == "action_pack"
        return {"body": _body(f"action_pack_{pack_id}.yaml")}


class _Port:
    """In-memory substrate: the reads the case harvest is typed over."""

    def __init__(self, order: list[str] | None = None) -> None:
        self.order = order if order is not None else []

    async def list_targets(self, *, active_only: bool = True) -> dict[str, Any]:
        self.order.append("list_targets")
        return {"rows": [{"target_id": T1, "name": "Alpha"}, {"target_id": T2, "name": "Beta"}],
                "refs": [], "count": 2}

    async def list_findings(self, **_: Any) -> dict[str, Any]:
        self.order.append("list_findings")
        return {"rows": [{"id": F1, "title": "f", "target_id": T1, "analyst_id": "desk_alpha"}],
                "refs": [F1], "count": 1}

    async def list_situations(self, **_: Any) -> dict[str, Any]:
        self.order.append("list_situations")
        return {"rows": [{"id": S1, "title": "s"}], "refs": [S1], "count": 1}

    async def search_signals(self, *, query: str, limit: int = 20, scope_predicate=None):
        self.order.append("search_signals")
        return {"rows": [{"id": SIG, "title": "a signal"}], "refs": [SIG], "count": 1}


async def _bindings(pool, port, who: str) -> tuple[AgencyToolBinding, AgencyToolBinding]:
    grants = [ActionPackRef(pack_id="substrate_read"), ActionPackRef(pack_id="workstation_drive")]
    substrate = AgencyToolBinding(
        agency=Agency(),
        pack=ActionPack.model_validate(_body("action_pack_substrate_read.yaml"), strict=False),
        pg_pool=pool,
        tool_context=ToolContext(substrate=port),
        analyst_grants=grants,
        target_allows=[ActionPackRef(pack_id="substrate_read")],
        scope=GLOBAL_SCOPE,
        requested_by=who,
        budget_account=who,
    )
    drive = await build_drive_binding(substrate, registry_client=_Registry(), analyst=None)
    assert drive is not None
    return substrate, drive


class _Recorder:
    def __init__(self) -> None:
        self.steps: list[dict[str, Any]] = []

    async def __call__(self, step: dict[str, Any]) -> None:
        self.steps.append(step)


def _read_round(gate: CaseGate, port_results: dict[str, dict[str, Any]], round_no: int = 1) -> None:
    calls = [{"tool": t, "args": {}} for t in port_results]
    gate.harvest(round_no, calls, [(r, {}) for r in port_results.values()])


async def _gate(pool, who: str, *, pins=None) -> tuple[CaseGate, _Recorder, str]:
    _substrate, drive = await _bindings(pool, _Port(), who)
    rid = uuid4().hex
    rec = _Recorder()
    gate = CaseGate.for_run(
        drive.for_request(session_id="sess", request_id=rid),
        record=rec, first={"request_id": rid, "pinned_context": pins or []}, plane=None,
    )
    port = _Port()
    _read_round(gate, {
        "list_targets": await port.list_targets(),
        "list_findings": await port.list_findings(),
        "list_situations": await port.list_situations(),
        "search_signals": await port.search_signals(query="x"),
    })
    return gate, rec, gate.binding.requested_by


async def _ledger(pool, requested_by: str) -> tuple[list[tuple[str, str]], list[tuple[str, str, str]]]:
    async with pool.acquire() as conn:
        inv = await conn.fetch(
            "SELECT tool_name, outcome FROM action_pack_invocations "
            "WHERE requested_by = $1 ORDER BY occurred_at, id", requested_by,
        )
        ev = await conn.fetch(
            "SELECT decision, cause, tool_name FROM governor_events "
            "WHERE requested_by = $1 ORDER BY occurred_at, id", requested_by,
        )
    return ([(r["tool_name"], r["outcome"]) for r in inv],
            [(r["decision"], r["cause"], r["tool_name"]) for r in ev])


C_T1 = [{"kind": "target", "id": T1}]

#: One valid call per tool, citing only refs the read round returned.
VALID: list[tuple[str, dict[str, Any]]] = [
    ("set_scope", {"kind": "situation", "id": S1, "cites": [{"kind": "situation", "id": S1}]}),
    ("timeline", {"window": {"span": "7d"}, "cites": [{"kind": "situation", "id": S1}]}),
    ("map", {"view": "fit_ref", "ref": {"kind": "signal", "id": SIG}, "layers": ["events", "signals"],
             "cites": [{"kind": "signal", "id": SIG}]}),
    ("focus", {"ref": {"kind": "finding", "id": F1}, "cites": [{"kind": "finding", "id": F1}]}),
    ("open_panel", {"kind": "subject.page", "params": {"subject": {"kind": "target", "id": T1}},
                    "cites": C_T1}),
    ("arrange", {"layout": "investigate", "cites": C_T1}),
    ("run_mission", {"name": "crisis", "cites": [{"kind": "situation", "id": S1}]}),
    ("narrate", {"text": "  The   situation  touches Alpha. ", "cites": C_T1 + C_T1}),
    ("done", {"summary": "Alpha, in one walk."}),
]


# ---------------------------------------------------------------------------
# 1. every tool admitted, ledgered, stamped
# ---------------------------------------------------------------------------


async def test_each_of_the_nine_tools_is_admitted_and_ledgered(pool):
    gate, rec, who = await _gate(pool, f"analyst::case_{uuid4().hex[:8]}")
    for name, args in VALID:
        result, meta = await gate.run(name, args, 2)
        assert meta["admitted"], (name, result)
    inv, events = await _ledger(pool, who)
    assert inv == [(name, "completed") for name, _ in VALID]
    assert [e for e in events if e[0] == "allow"] == [("allow", "ok", n) for n, _ in VALID]
    assert who.endswith(f"::request::{gate.request_id}")

    frames = [s for s in rec.steps if s["kind"] == "case_action"]
    assert [f["seq"] for f in frames] == list(range(1, 9))
    assert all(f["phase"] == "case" and f["round"] == 2 for f in frames)
    by = {f["action"]: f for f in frames}
    assert by["narrate"]["args"] == {"text": "The situation touches Alpha."}
    assert by["narrate"]["cites"] == C_T1  # de-duplicated
    assert by["map"]["args"]["layers"] == ["signals", "events"]  # vocabulary order
    assert by["arrange"]["args"] == {"layout": "investigate", "layout_type": "stance"}
    assert by["focus"]["rests_on"] == [{"round": 1, "tool": "list_findings"}]
    assert by["open_panel"]["rests_on"] == [{"round": 1, "tool": "list_targets"}]
    sealed = [s for s in rec.steps if s["kind"] == "case_sealed"]
    assert sealed == [{**sealed[0], "seq_through": 8, "summary": "Alpha, in one walk."}]
    record = gate.to_record()
    assert record["status"] == "sealed" and record["complete"] and record["gap_at_seq"] is None
    assert [a["seq"] for a in record["actions"]] == list(range(1, 9))
    assert record["vocabulary"] == V.vocabulary_hash()


# ---------------------------------------------------------------------------
# 2. grammar → invalid_arg / result_shaped_arg + a BLOCK event
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("name", "args", "cause"), [
    ("open_panel", {"kind": "system.settings", "cites": C_T1}, "invalid_arg"),
    ("open_panel", {"kind": "system.timeline", "params": {"tab": "lineage"}, "cites": C_T1}, "invalid_arg"),
    ("open_panel", {"kind": "system.wall", "params": {"zoom": 2}, "cites": C_T1}, "invalid_arg"),
    ("arrange", {"layout": "engine", "cites": C_T1}, "invalid_arg"),
    ("map", {"view": "reset", "layers": ["heat"], "cites": C_T1}, "invalid_arg"),
    ("run_mission", {"name": "release", "cites": C_T1}, "invalid_arg"),
    ("focus", {"ref": {"kind": "target", "id": T1}, "cites": C_T1, "colour": "red"}, "invalid_arg"),
    ("focus", {"ref": {"kind": "target", "id": T1}, "cites": C_T1, "result": [1]}, "result_shaped_arg"),
    ("focus", {"ref": {"kind": "absence", "id": "x"}, "cites": C_T1}, "invalid_arg"),
    ("open_panel", {"kind": "subject.page", "cites": C_T1}, "kind_not_drivable"),
])
async def test_grammar_refusals_are_blocked_and_ledgered(pool, name, args, cause):
    gate, rec, who = await _gate(pool, f"analyst::gram_{uuid4().hex[:8]}")
    result, meta = await gate.run(name, args, 2)
    assert not meta["admitted"]
    assert result["error"].startswith(f"tool_blocked: {cause}: ")
    inv, events = await _ledger(pool, who)
    assert inv == []  # never reached the agency gate's admit
    assert events == [("block", cause, name)]
    assert [s["kind"] for s in rec.steps] == ["case_refused"]
    assert gate.refused == 1 and gate.actions == []


# ---------------------------------------------------------------------------
# 3. the read-set rule
# ---------------------------------------------------------------------------


async def test_an_invented_ref_is_refused_ref_not_read(pool):
    gate, _rec, who = await _gate(pool, f"analyst::inv_{uuid4().hex[:8]}")
    invented = str(uuid4())
    result, meta = await gate.run(
        "focus", {"ref": {"kind": "finding", "id": invented},
                  "cites": [{"kind": "finding", "id": invented}]}, 2,
    )
    assert not meta["admitted"]
    assert result["error"].startswith("tool_blocked: ref_not_read: ")
    _inv, events = await _ledger(pool, who)
    assert events == [("block", "ref_not_read", "focus")]


async def test_a_pinned_ref_is_admitted_resting_on_the_pin(pool):
    pins = [{"kind": "report", "id": PINNED_REPORT, "title": "the read"}]
    gate, rec, _who = await _gate(pool, f"analyst::pin_{uuid4().hex[:8]}", pins=pins)
    _result, meta = await gate.run(
        "set_scope", {"kind": "report", "id": PINNED_REPORT,
                      "cites": [{"kind": "finding", "id": PINNED_REPORT}]}, 2,
    )
    assert meta["admitted"]
    frame = rec.steps[-1]
    assert frame["rests_on"] == [{"round": 0, "tool": "pinned"}]


def _scripted(replies: list[str]):
    class _LLM:
        subprovider = "vllm-test"

        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []
            self._replies = list(replies)

        async def chat_complete(self, messages, **kwargs):
            self.calls.append({"messages": list(messages), **kwargs})

            class _R:
                content = self._replies.pop(0)
                usage = None
            return _R()

    return _LLM()


FINAL = json.dumps({"final": True, "answer": "here is the walk", "uncertainty": 0.4,
                    "cited_refs": [], "unanswered_aspects": []})


async def test_same_batch_ref_refused_then_admitted_next_round_order_serial(pool):
    """Acceptance 3 + 5 through the real loop: a ref from the SAME batch is
    refused; the next round it is admitted resting on round 1; in a round of
    [read, action, action] the read runs first, then the actions in order."""
    order: list[str] = []
    port = _Port(order)
    who = f"analyst::loop_{uuid4().hex[:8]}"
    substrate, drive = await _bindings(pool, port, who)
    orig = AgencyToolBinding.run_tool

    async def _traced(self, tool_name, args, **kw):
        if tool_name in DRIVE_TOOLS:
            order.append(tool_name)
        return await orig(self, tool_name, args, **kw)

    open_t1 = {"tool": "open_panel", "args": {
        "kind": "subject.page", "params": {"subject": {"kind": "target", "id": T1}},
        "cites": C_T1}}
    llm = _scripted([
        json.dumps({"tools": [{"tool": "list_targets", "args": {}}, open_t1]}),
        json.dumps({"tools": [
            {"tool": "narrate", "args": {"text": "Alpha first.", "cites": C_T1}},
            {"tool": "list_findings", "args": {}},
            open_t1,
        ]}),
        json.dumps({"tool": "done", "args": {}}),
        FINAL,
    ])
    rec = _Recorder()
    deps = ConsultOnDemandDeps(
        llm=llm, substrate=port, agency_binding=substrate, drive_binding=drive,
        step_publish=rec,
    )
    rid = uuid4().hex
    try:
        AgencyToolBinding.run_tool = _traced  # type: ignore[method-assign]
        result = await run_method(
            [{"question": "walk me through Alpha", "present_case": True,
              "request_id": rid, "session_id": "s1"}],
            {"analyst_id": "consult_default"}, deps,
        )
    finally:
        AgencyToolBinding.run_tool = orig  # type: ignore[method-assign]

    # Round 1: the read ran, the same-batch open_panel was refused.
    # Round 2: list_findings (the read) BEFORE narrate and open_panel, which
    # ran in the order the model emitted them.
    # (The refused round-1 open_panel never reached the binding at all.)
    assert order == [
        "list_targets",
        "list_findings", "narrate", "open_panel",
        "done",
    ]
    refused = [s for s in rec.steps if s.get("kind") == "case_refused"]
    assert len(refused) == 1 and refused[0]["cause"] == "ref_not_read" and refused[0]["round"] == 1
    actions = [s for s in rec.steps if s.get("kind") == "case_action"]
    assert [(a["seq"], a["action"]) for a in actions] == [(1, "narrate"), (2, "open_panel")]
    assert actions[1]["rests_on"] == [{"round": 1, "tool": "list_targets"}]
    case = result.consult_response.data["case"]
    assert case["status"] == "sealed" and case["refused"] == 1
    assert [a["seq"] for a in case["actions"]] == [1, 2]
    # The ledger names the request: two admitted actions + the done.
    inv, _events = await _ledger(pool, f"{who}::session::s1::request::{rid}")
    assert [t for t, _ in inv] == ["narrate", "open_panel", "done"]
    # The first frame says the turn was armed; the round default is 20.
    plan = [s for s in rec.steps if s.get("kind") == "render_prompt"][0]
    assert plan["present_case"] is True and plan["max_rounds"] == 20


# ---------------------------------------------------------------------------
# 4. no_citation, the cap, the seal
# ---------------------------------------------------------------------------


async def test_no_citation_cap_and_seal(pool):
    gate, _rec, who = await _gate(pool, f"analyst::cap_{uuid4().hex[:8]}")
    gate.cap = 2
    narr = {"text": "x", "cites": C_T1}
    r, m = await gate.run("narrate", {"text": "uncited"}, 2)
    assert r["error"].startswith("tool_blocked: no_citation: ")
    r, m = await gate.run("narrate", {"text": "uncited", "cites": []}, 2)
    assert r["error"].startswith("tool_blocked: no_citation: ")
    for _ in range(2):
        _r, m = await gate.run("narrate", narr, 2)
        assert m["admitted"]
    r, m = await gate.run("narrate", narr, 2)  # cap + 1
    assert r["error"].startswith("tool_blocked: case_cap_reached: ")
    _r, m = await gate.run("done", {}, 2)  # done is not an action; it seals
    assert m["admitted"]
    r, m = await gate.run("narrate", narr, 3)
    assert r["error"].startswith("tool_blocked: case_sealed: ")
    _inv, events = await _ledger(pool, who)
    assert [e[1] for e in events if e[0] == "block"] == [
        "no_citation", "no_citation", "case_cap_reached", "case_sealed",
    ]
    assert gate.to_record()["refused"] == 4


async def test_the_pack_governor_carries_the_per_run_cap(pool):
    gate, _rec, _who = await _gate(pool, f"analyst::gov_{uuid4().hex[:8]}")
    assert gate.cap == 30 == gate.binding.pack.governor.max_invocations_per_run


# ---------------------------------------------------------------------------
# 7. the clock is stored absolute
# ---------------------------------------------------------------------------


async def test_span_is_stored_absolute_and_a_future_end_is_refused(pool):
    gate, rec, _who = await _gate(pool, f"analyst::clock_{uuid4().hex[:8]}")
    cites = [{"kind": "situation", "id": S1}]
    _r, m = await gate.run("timeline", {"window": {"span": "7d"}, "cites": cites}, 2)
    assert m["admitted"]
    args = rec.steps[-1]["args"]
    start, end = parse_instant(args["start"]), parse_instant(args["end"])
    assert args["span"] == "7d" and end - start == timedelta(days=7)
    assert abs((datetime.now(timezone.utc) - end).total_seconds()) < 60
    future = datetime.now(timezone.utc) + timedelta(minutes=30)
    r, m = await gate.run("timeline", {"window": {
        "start": "2026-01-01T00:00:00Z", "end": future.isoformat()}, "cites": cites}, 2)
    assert not m["admitted"] and r["error"].startswith("tool_blocked: invalid_arg: ")
    r, m = await gate.run("timeline", {"window": {"span": "7d", "start": "2026-01-01T00:00:00Z"},
                                       "cites": cites}, 2)
    assert not m["admitted"]


# ---------------------------------------------------------------------------
# 6. arming: unarmed is byte-identical; armed without the pack refuses
# ---------------------------------------------------------------------------


class _NativeResp:
    def __init__(self, content: str) -> None:
        self.content = content
        self.finish_reason = "stop"
        self.tool_calls: list[Any] = []
        self.usage = None
        self.raw_response = None


class _NativeLLM:
    subprovider = "anthropic"

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def chat_complete(self, messages, **kwargs):
        self.calls.append({"messages": list(messages), **kwargs})
        return _NativeResp("<<<FINAL>>>\nuncertainty: 0.4\n\nnothing to show")


async def test_unarmed_request_is_byte_identical_and_armed_offers_the_drive(pool):
    port = _Port()
    who = f"analyst::arm_{uuid4().hex[:8]}"
    substrate, drive = await _bindings(pool, port, who)

    async def _one(**kw):
        llm = _NativeLLM()
        deps = ConsultOnDemandDeps(llm=llm, substrate=port, agency_binding=substrate, **kw)
        await run_method([{"question": "q", "request_id": "r"}], {}, deps)
        return llm.calls[0]

    before = await _one()                      # no drive binding at all (yesterday)
    unarmed = await _one(drive_binding=drive)  # wired, not armed
    assert json.dumps(unarmed, sort_keys=True, default=str) == json.dumps(
        before, sort_keys=True, default=str)
    assert not {t["name"] for t in unarmed["tools"]} & DRIVE_TOOLS

    llm = _NativeLLM()
    deps = ConsultOnDemandDeps(llm=llm, substrate=port, agency_binding=substrate, drive_binding=drive)
    await run_method([{"question": "q", "request_id": "r", "present_case": True}], {}, deps)
    armed = llm.calls[0]
    names = [t["name"] for t in armed["tools"]]
    # The nine case/1 tools, then Q-1's panel_action, at the tail.
    assert names[-10:] == [*V.DRIVE_TOOL_NAMES, "panel_action"]
    assert names[:-10] == [t["name"] for t in before["tools"]]
    assert "Case actions" in armed["system"] and "Case actions" not in before["system"]


async def test_armed_without_the_binding_fails_loud():
    llm = _NativeLLM()
    deps = ConsultOnDemandDeps(llm=llm, substrate=_Port())
    with pytest.raises(ValueError, match="case_unavailable: workstation_drive not bound"):
        await run_method([{"question": "q", "present_case": True}], {}, deps)
    assert llm.calls == []


async def test_a_drive_call_on_an_unarmed_turn_is_drive_not_armed(pool):
    port = _Port()
    who = f"analyst::unarmed_{uuid4().hex[:8]}"
    substrate, drive = await _bindings(pool, port, who)
    llm = _scripted([
        json.dumps({"tool": "narrate", "args": {"text": "x", "cites": C_T1}}),
        FINAL,
    ])
    deps = ConsultOnDemandDeps(llm=llm, substrate=port, agency_binding=substrate, drive_binding=drive)
    result = await run_method([{"question": "q"}], {}, deps)
    step = [s for s in result.intermediate_steps if s.get("kind") == "tool_call"][0]
    assert step["ok"] is False and step["result"].startswith("tool_blocked: drive_not_armed: ")
    assert "case" not in result.consult_response.data
    inv, events = await _ledger(pool, who)
    assert inv == [] and events == [("block", "drive_not_armed", "narrate")]


# ---------------------------------------------------------------------------
# 10. the census union and the hold
# ---------------------------------------------------------------------------


class _ClassifyingPort(_Port):
    def __init__(self) -> None:
        super().__init__()
        self.asked: list[list[str]] = []

    async def classify_cited_refs(self, *, refs):
        self.asked.append(list(refs))
        return {"by_origin_class": {}, "by_kind": {}, "resolved": 0, "unresolved": 0,
                "fabricated": len(refs), "malformed": [], "asked": len(refs)}


async def test_case_cites_join_the_census_and_a_held_answer_keeps_its_case(pool):
    port = _ClassifyingPort()
    who = f"analyst::census_{uuid4().hex[:8]}"
    substrate, drive = await _bindings(pool, port, who)
    llm = _scripted([
        json.dumps({"tool": "list_findings", "args": {}}),
        json.dumps({"tool": "focus", "args": {"ref": {"kind": "finding", "id": F1},
                                              "cites": [{"kind": "finding", "id": F1}]}}),
        "<<<FINAL>>>\nuncertainty: 0.3\n\nThe finding says so.",
    ])
    deps = ConsultOnDemandDeps(llm=llm, substrate=port, agency_binding=substrate, drive_binding=drive)
    result = await run_method(
        [{"question": "q", "present_case": True, "request_id": uuid4().hex}], {}, deps,
    )
    assert port.asked and F1 in port.asked[0]
    data = result.consult_response.data
    assert data["hold_reason"] and data["case"]["actions"][0]["action"] == "focus"


# ---------------------------------------------------------------------------
# 11. guards
# ---------------------------------------------------------------------------


async def test_the_pack_is_read_only_and_matches_the_registered_specs():
    from legba.data.analysts.agency.tools import default_tool_registry

    body = _body("action_pack_workstation_drive.yaml")
    pack = ActionPack.model_validate(body, strict=False)
    names = [t.name for t in pack.tools]
    # The nine case/1 tools, then Q-1's panel_action (view actions only).
    assert names == [*V.DRIVE_TOOL_NAMES, "panel_action"]
    assert not set(names) & WRITE_TOOLS
    registry = default_tool_registry()
    assert all(registry.handler_for(n) is not None for n in names)
    assert pack.governor.max_invocations_per_run == V.LIMITS["case_cap_default"]
    assert pack.applies_to_tags == [] and pack.applicability_predicate is None


async def test_harvest_is_typed_per_tool():
    assert harvest_refs("list_targets", {"rows": [{"target_id": T1}]}) == [("target", T1)]
    assert harvest_refs("inspect_entity", {"found": True, "entity": "Acme",
                                           "profile": {"canonical_name": "Acme"}}) == [
        ("entity", "Acme"), ("entity", "Acme")]
    assert harvest_refs("list_targets", {"error": "boom"}) == []
    assert harvest_refs("query_facts", {"rows": [{"id": "x"}], "refs": ["y"]}) == []


# ---------------------------------------------------------------------------
# 9. stop: once the run is cancelled, no drive invocation follows
# ---------------------------------------------------------------------------


async def test_a_cancelled_case_run_issues_nothing_after_the_cancel(pool):
    import asyncio

    port = _Port()
    who = f"analyst::stop_{uuid4().hex[:8]}"
    substrate, drive = await _bindings(pool, port, who)
    narrate = json.dumps({"tool": "narrate", "args": {"text": "next", "cites": C_T1}})

    class _Reply:
        def __init__(self, content: str) -> None:
            self.content = content
            self.usage = None

    class _Endless:
        subprovider = "vllm-test"

        def __init__(self) -> None:
            self.n = 0

        async def chat_complete(self, messages, **kwargs):
            self.n += 1
            await asyncio.sleep(0.05)
            if self.n == 1:
                return _Reply(json.dumps({"tool": "list_targets", "args": {}}))
            return _Reply(narrate)

    rec = _Recorder()
    deps = ConsultOnDemandDeps(llm=_Endless(), substrate=port, agency_binding=substrate,
                               drive_binding=drive, step_publish=rec)
    rid = uuid4().hex
    task = asyncio.ensure_future(run_method(
        [{"question": "q", "present_case": True, "request_id": rid, "max_tool_rounds": 30}],
        {}, deps,
    ))
    for _ in range(200):
        if sum(1 for s in rec.steps if s.get("kind") == "case_action") >= 3:
            break
        await asyncio.sleep(0.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    requested_by = f"{who}::request::{rid}"
    at_stop, _ = await _ledger(pool, requested_by)
    await asyncio.sleep(0.4)
    after, _ = await _ledger(pool, requested_by)
    assert len(at_stop) >= 3
    assert after == at_stop, "a drive invocation landed after the run was cancelled"
