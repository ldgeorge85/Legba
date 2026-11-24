# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""C-2b-1/frame — the consult write tools through the REAL binding path and the loop.

Every write tool runs: the write gate (``analysts/consult_write_gate.py``) →
``AgencyToolBinding.run_tool`` → ``Agency.run_pack_tool`` (grant, known tool,
closed grammar, governor, ``action_pack_invocations`` row + ALLOW event) → the
registered thin-client handler → the runtime's ``ConsultWritesClient`` (which
preflights through the runtime parser) → ``POST /api/v1/consult/writes`` on a
real registry router → the ONE executor → Postgres. The binding is built by
the real ``runtime/consult_bindings.build_pack_binding``. The LLM is the one
permitted boundary double; the registry is reached over ASGI.

Design C-2a §10 items 1–3, 6–8 on the binding path: every tool admitted and
ledgered; ``ref_not_read`` for an invented id and for a same-round id; a Tier B
stages and a model turn saying "yes, go ahead" executes nothing; unarmed =
byte-identical request; the pack paused → ``writes_disabled``; the spend
ceiling counts refused writes on the truth line.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

import httpx
import pytest_asyncio
import yaml
from fastapi import FastAPI
from httpx import ASGITransport

from legba.data import consult_write_vocabulary as W
from legba.data.analysts.agency import Agency, AgencyToolBinding, GLOBAL_SCOPE, ToolContext
from legba.data.analysts.consult_on_demand import ConsultOnDemandDeps, run_method
from legba.data.analysts.consult_write_gate import WriteGate
from legba.data.registry import consult_writes_api as front_mod
from legba.data.registry.descriptor import Family
from legba.data.schemas.action_pack import ActionPack, ActionPackRef
from legba.runtime.consult_bindings import build_pack_binding
from legba.runtime.registry_client import RegistryHTTPClient
from tests.data_pkg.test_consult_write_frame import (  # noqa: F401 — the shared fixture + helpers
    World,
    _pack_body,
    _set_pack,
    world,
)

_DESCRIPTORS = __import__("pathlib").Path(__file__).resolve().parents[2] / "descriptors"


def _body(fname: str) -> dict[str, Any]:
    body = yaml.safe_load((_DESCRIPTORS / fname).read_text())
    body["identity"]["version"] = "0" * 16
    return body


class _Registry:
    """``fetch_action_pack``'s registry client: serves the real pack bodies."""

    def __init__(self, rc: RegistryHTTPClient) -> None:
        self._rc = rc

    async def get_descriptor(self, pack_id: str, *, family: str) -> dict[str, Any] | None:
        assert family == "action_pack"
        return {"body": _pack_body() if pack_id == W.CONSULT_WRITE_PACK_ID else _body(f"action_pack_{pack_id}.yaml")}

    async def request_json(self, *a, **k):
        return await self._rc.request_json(*a, **k)

    async def get_descriptor_typed(self, *a, **k):
        return await self._rc.get_descriptor_typed(*a, **k)


class _Port:
    async def list_sources(self, *, active_only: bool = True, silent_only: bool = False, silent_hours: int = 48):
        return {"rows": [], "refs": [], "count": 0}


@pytest_asyncio.fixture
async def rig(world: World, monkeypatch):
    """The registry app (write router + the typed read the runtime preflights
    against) and the consult_write binding, re-pointed at an armed request."""
    # The rig's registry runs in dev mode. The main checkout's .env carries the
    # live LEGBA_REGISTRY_API_TOKEN into the test container while the rig's
    # client is built with token="" — the registry answered 401 (w5-int,
    # 2026-10-04); a lane worktree has no .env, so it never saw this.
    monkeypatch.delenv("LEGBA_REGISTRY_API_TOKEN", raising=False)
    front = front_mod.WritesFront(
        type("Deps", (), {"descriptor_registry": world.reg})(), is_live=lambda rid: True, executor=world.ex,
    )
    front.armed[world.armed.request_id] = world.armed
    app = FastAPI()
    app.include_router(front_mod.build_router(front), prefix="/api/v1")

    @app.get("/api/v1/registry/descriptors/source/{descriptor_id}/typed")
    async def _typed(descriptor_id: str) -> dict[str, Any]:
        typed = await world.reg.get_typed(descriptor_id, family=Family.SOURCE)
        return typed.model_dump(mode="json", by_alias=True)

    http = httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://registry")
    rc = RegistryHTTPClient(base_url="http://registry", token="", client=http)
    who = f"analyst::cw_{uuid4().hex[:8]}"
    grants = [ActionPackRef(pack_id="substrate_read"), ActionPackRef(pack_id=W.CONSULT_WRITE_PACK_ID)]
    substrate = AgencyToolBinding(
        agency=Agency(),
        pack=ActionPack.model_validate(_body("action_pack_substrate_read.yaml"), strict=False),
        pg_pool=world.store.pool, tool_context=ToolContext(substrate=_Port()),
        analyst_grants=grants, target_allows=[ActionPackRef(pack_id="substrate_read")],
        scope=GLOBAL_SCOPE, requested_by=who, budget_account=who,
    )
    binding = await build_pack_binding(substrate, W.CONSULT_WRITE_PACK_ID,
                                       registry_client=_Registry(rc), analyst=None)
    assert binding is not None and binding.tool_context.consult_writes is not None
    yield world, front, substrate, binding
    await http.aclose()


class _Rec:
    def __init__(self) -> None:
        self.steps: list[dict[str, Any]] = []

    async def __call__(self, step: dict[str, Any]) -> None:
        self.steps.append(step)


def _gate(world: World, binding: Any, spend: Any = None) -> tuple[WriteGate, _Rec]:
    rec = _Rec()
    per_request = binding.for_request(session_id=world.armed.session_id, request_id=world.armed.request_id)
    return WriteGate.open(per_request, record=rec, first={}, spend=spend), rec


async def _ledger(world: World, requested_by: str) -> tuple[list[tuple[str, str]], list[tuple[str, str, str]]]:
    async with world.store.acquire() as conn:
        inv = await conn.fetch(
            "SELECT tool_name, outcome FROM action_pack_invocations WHERE requested_by = $1 "
            "ORDER BY occurred_at, id", requested_by)
        ev = await conn.fetch(
            "SELECT decision, cause, tool_name FROM governor_events WHERE requested_by = $1 "
            "ORDER BY occurred_at, id", requested_by)
    return ([(r["tool_name"], r["outcome"]) for r in inv], [(r["decision"], r["cause"], r["tool_name"]) for r in ev])


def _round(gate: WriteGate, round_no: int, calls: list[dict[str, Any]], results: list[Any]) -> None:
    gate.harvest(round_no, calls, results)


async def test_every_tool_through_the_real_binding(rig):
    world, _front, _substrate, binding = rig
    gate, rec = _gate(world, binding)
    src = world.src
    insp = {"tool": "inspect_object", "args": {"class": "source", "id": src}}
    seen = await gate.run("inspect_object", insp["args"], 1)
    assert seen[1]["admitted"] and seen[0]["found"]
    _round(gate, 1, [insp], [seen])
    version = seen[0]["version"]

    upd, meta = await gate.run("update_object", {
        "class": "source", "id": src, "expected_version": version,
        "patch": {"cadence.cooldown_seconds": 300}, "reason": "the operator asked"}, 2)
    assert meta["admitted"] and upd["status"] == "executed", upd
    body = yaml.safe_load((_DESCRIPTORS / "source_38north.yaml").read_text())
    body["identity"]["id"] = f"source.test.cw_g_{uuid4().hex[:8]}"
    made, meta = await gate.run("create_object", {"class": "source", "body": body, "reason": "add it"}, 2)
    assert made["status"] == "executed", made

    seen2 = await gate.run("inspect_object", insp["args"], 3)
    _round(gate, 3, [insp], [seen2])
    assert seen2[0]["writes"][0]["write_id"] == upd["write_id"]
    rev, meta = await gate.run("revert_write", {"write_id": upd["write_id"], "reason": "undo"}, 4)
    assert rev["status"] == "executed", rev
    assert (await world.head()).version == version

    seen3 = await gate.run("inspect_object", insp["args"], 5)
    _round(gate, 5, [insp], [seen3])
    staged, meta = await gate.run("transition_object", {
        "class": "source", "id": src, "expected_version": seen3[0]["version"],
        "to_state": "paused", "reason": "pause it"}, 6)
    assert staged["write_staged"] and "confirmation required" in staged["detail"]
    assert (await world.head()).state == "active"

    who = gate.binding.requested_by
    inv, events = await _ledger(world, who)
    assert [t for t, _ in inv] == [
        "inspect_object", "update_object", "create_object", "inspect_object", "revert_write",
        "inspect_object", "transition_object"]
    assert all(o == "completed" for _, o in inv)
    assert all(d == "allow" for d, _, _ in events)
    receipt = (await world.row(upd["write_id"]))["receipt"]
    async with world.store.acquire() as conn:
        inv_id = await conn.fetchval(
            "SELECT id FROM action_pack_invocations WHERE requested_by = $1 AND tool_name = 'update_object'", who)
    assert receipt["invocation_id"] == str(inv_id)
    assert gate.truth_line() == "writes: executed 3 · staged 1 · refused 0"
    assert [s["kind"] for s in rec.steps] == ["write_executed", "write_executed", "write_executed", "write_staged"]


async def test_ref_not_read_for_an_invented_and_a_same_round_id(rig):
    world, _front, _substrate, binding = rig
    gate, _rec = _gate(world, binding)
    out, meta = await gate.run("update_object", {
        "class": "source", "id": world.src, "expected_version": "a" * 16,
        "patch": {"cadence.cooldown_seconds": 5}, "reason": "r"}, 1)
    assert not meta["admitted"] and out["error"].startswith("tool_blocked: ref_not_read: ")
    _inv, events = await _ledger(world, gate.binding.requested_by)
    assert events == [("block", "ref_not_read", "update_object")]

    # Same round: [inspect, update] — the update was written before the model saw the version.
    from legba.data.analysts import consult_tool_routing as route

    seen = await world.inspect()
    deps = ConsultOnDemandDeps(llm=None, substrate=_Port(), provider_bindings={W.CONSULT_WRITE_PACK_ID: binding})
    calls = [
        {"tool": "inspect_object", "args": {"class": "source", "id": world.src}},
        {"tool": "update_object", "args": {"class": "source", "id": world.src,
                                          "expected_version": seen["version"],
                                          "patch": {"cadence.cooldown_seconds": 5}, "reason": "r"}},
    ]
    results = await route.run_round(deps, calls, scope_predicate=None, analyst_id=None, round_no=2,
                                    gates={W.CONSULT_WRITE_PACK_ID: gate})
    assert results[0][0]["found"]
    assert results[1][0]["error"].startswith("tool_blocked: ref_not_read: ")
    assert (await world.head()).version == seen["version"]


async def test_spend_ceiling_and_paused_pack_refuse_and_the_truth_line_counts_them(rig):
    world, _front, _substrate, binding = rig

    class _Spent:
        cost_usd = 3.2

        def exhausted_reason(self):
            return "the run reached its estimated-cost ceiling ($3.20 of $3.00)"

    gate, _rec = _gate(world, binding, spend=_Spent())
    insp = {"tool": "inspect_object", "args": {"class": "source", "id": world.src}}
    seen = await gate.run("inspect_object", insp["args"], 1)
    _round(gate, 1, [insp], [seen])
    args = {"class": "source", "id": world.src, "expected_version": seen[0]["version"],
            "patch": {"cadence.cooldown_seconds": 8}, "reason": "r"}
    out, _meta = await gate.run("update_object", args, 2)
    assert out["error"].startswith("tool_blocked: spend_ceiling_reached: ")
    assert gate.truth_line() == "writes: executed 0 · staged 0 · refused 1"

    gate2, _rec2 = _gate(world, binding)
    _round(gate2, 1, [insp], [seen])
    await _set_pack(world.reg, state="paused")
    out, meta = await gate2.run("update_object", args, 2)
    assert meta["admitted"] is False and out["error"].startswith("tool_blocked: writes_disabled: ")
    await _set_pack(world.reg)
    out, _meta = await gate2.run("update_object", args, 3)
    assert out["status"] == "executed"
    assert gate2.truth_line() == "writes: executed 1 · staged 0 · refused 1"


async def test_the_runtime_preflight_refuses_what_the_runtime_cannot_parse(rig):
    world, _front, _substrate, binding = rig
    gate, _rec = _gate(world, binding)
    insp = {"tool": "inspect_object", "args": {"class": "source", "id": world.src}}
    seen = await gate.run("inspect_object", insp["args"], 1)
    _round(gate, 1, [insp], [seen])
    out, _meta = await gate.run("update_object", {
        "class": "source", "id": world.src, "expected_version": seen[0]["version"],
        "patch": {"cadence.jitter_seconds": "often"}, "reason": "r"}, 2)
    assert out["error"].startswith("tool_blocked: preflight_failed: ")
    assert (await world.head()).version == seen[0]["version"]


# ---------------------------------------------------------------------------
# The loop: armed vs unarmed, the truth line, "yes, go ahead"
# ---------------------------------------------------------------------------


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


def _final(text: str) -> str:
    return json.dumps({"final": True, "answer": text, "uncertainty": 0.4,
                       "cited_refs": [], "unanswered_aspects": []})


async def test_an_armed_turn_stages_and_a_yes_turn_executes_nothing(rig):
    world, _front, substrate, binding = rig
    seen = await world.inspect()
    llm = _scripted([
        json.dumps({"tool": "inspect_object", "args": {"class": "source", "id": world.src}}),
        json.dumps({"tool": "transition_object", "args": {
            "class": "source", "id": world.src, "expected_version": seen["version"],
            "to_state": "paused", "reason": "the operator asked"}}),
        _final("Paused. Done."),
    ])
    deps = ConsultOnDemandDeps(llm=llm, substrate=_Port(), agency_binding=substrate,
                               provider_bindings={W.CONSULT_WRITE_PACK_ID: binding})
    first = {"question": "pause it", "allow_writes": True, "request_id": world.armed.request_id,
             "session_id": world.armed.session_id}
    result = await run_method([first], {"analyst_id": "consult_default"}, deps)
    answer = result.consult_response.answer
    assert answer.endswith("writes: executed 0 · staged 1 · refused 0"), answer
    writes = result.consult_response.data["writes"]
    assert [w["status"] for w in writes] == ["pending_confirmation"]
    assert (await world.head()).state == "active"

    yes = _scripted([_final("Yes — it is paused now.")])
    deps = ConsultOnDemandDeps(llm=yes, substrate=_Port(), agency_binding=substrate,
                               provider_bindings={W.CONSULT_WRITE_PACK_ID: binding})
    result = await run_method([{**first, "question": "yes, go ahead"}], {}, deps)
    assert result.consult_response.answer.endswith("writes: none")
    assert result.consult_response.data["writes"] == []
    assert (await world.head()).state == "active"
    rows = await world.ex.list(session_id=world.armed.session_id)
    assert [r["status"] for r in rows] == ["pending_confirmation"]


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
        return _NativeResp("<<<FINAL>>>\nuncertainty: 0.4\n\nnothing to change")


async def test_unarmed_is_byte_identical_and_armed_offers_the_five(rig):
    world, _front, substrate, binding = rig

    async def _one(first: dict[str, Any], **kw):
        llm = _NativeLLM()
        deps = ConsultOnDemandDeps(llm=llm, substrate=_Port(), agency_binding=substrate, **kw)
        result = await run_method([first], {}, deps)
        return llm.calls[0], result

    before, plain = await _one({"question": "q", "request_id": "r"})
    unarmed, plain2 = await _one({"question": "q", "request_id": "r"},
                                 provider_bindings={W.CONSULT_WRITE_PACK_ID: binding})
    assert json.dumps(unarmed, sort_keys=True, default=str) == json.dumps(before, sort_keys=True, default=str)
    assert "writes" not in plain2.consult_response.data
    assert not plain2.consult_response.answer.rstrip().endswith("writes: none")

    armed, result = await _one({"question": "q", "request_id": world.armed.request_id, "allow_writes": True},
                               provider_bindings={W.CONSULT_WRITE_PACK_ID: binding})
    names = [t["name"] for t in armed["tools"]]
    n = len(W.WRITE_TOOL_NAMES)
    assert names[-n:] == list(W.WRITE_TOOL_NAMES) and names[:-n] == [t["name"] for t in before["tools"]]
    assert "Changes (call them" in armed["system"] and "SEAMS #64" in armed["system"]
    assert result.consult_response.answer.endswith("writes: none")
    assert result.consult_response.data["writes"] == []


async def test_a_write_call_on_an_unarmed_turn_is_writes_not_armed(rig):
    world, _front, substrate, binding = rig
    llm = _scripted([
        json.dumps({"tool": "inspect_object", "args": {"class": "source", "id": world.src}}),
        _final("nothing"),
    ])
    deps = ConsultOnDemandDeps(llm=llm, substrate=_Port(), agency_binding=substrate,
                               provider_bindings={W.CONSULT_WRITE_PACK_ID: binding})
    result = await run_method([{"question": "q"}], {}, deps)
    step = [s for s in result.intermediate_steps if s.get("kind") == "tool_call"][0]
    assert step["ok"] is False and step["result"].startswith("tool_blocked: writes_not_armed: ")
    assert "writes" not in result.consult_response.data
    _inv, events = await _ledger(world, binding.requested_by)
    assert events == [("block", "writes_not_armed", "inspect_object")]


async def test_armed_without_the_binding_fails_loud():
    import pytest

    llm = _NativeLLM()
    deps = ConsultOnDemandDeps(llm=llm, substrate=_Port())
    with pytest.raises(ValueError, match="writes_unavailable: consult_write not bound"):
        await run_method([{"question": "q", "allow_writes": True}], {}, deps)
    assert llm.calls == []
