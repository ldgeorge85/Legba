# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""C-2b-1/unit+comp (W6) — the write plane's UNIT and COMPOSITION classes, on the REAL driver.

Design C-2a §10 items 1–8 for the two analyst classes. Real migrated Postgres,
a real ``DescriptorRegistry`` with a real signing identity, the real
``consult_write`` pack descriptor from ``descriptors/`` (its pin list and
unbilled list as shipped), real unit / composition / subject descriptors from
``descriptors/`` under fresh ids. Every value claimed persisted is read back.
The Dapr actor is the one boundary double: it writes the run's trace through
the runtime's REAL receipt writer (``RuntimeReceiptChain.record``), stamped
with the trigger kind it was POSTED — so a "method" trace proves the body.
"""

from __future__ import annotations

import ast
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio
import yaml

from legba.data import consult_write_vocabulary as W
from legba.data.provenance.receipts import RuntimeReceiptChain
from legba.data.registry import consult_write_analyst as A
from legba.data.registry.audit import signed_payload_for_row
from legba.data.registry.descriptor import DescriptorRegistry, Family
from legba.data.registry.errors import DescriptorNotFound
from legba.data.registry.signing import verify_audit_payload
from legba.data.schemas.action_pack import ActionPack
from legba.data.schemas.analyst import AnalystDescriptor
from legba.data.schemas.target import TargetDescriptor
from tests.data_pkg.test_consult_write_frame import (  # noqa: F401 — the shared fixture + helpers
    World,
    _identity,
    _pack_body,
    world,
)
from tests.data_pkg.test_consult_write_gate import _gate, _ledger, _round, rig  # noqa: F401

_ROOT = Path(__file__).resolve().parents[2]
_DESCRIPTORS = _ROOT / "descriptors"


# ---------------------------------------------------------------------------
# fixtures: the shipped pack, fresh units / compositions / subjects
# ---------------------------------------------------------------------------


async def _yaml_pack(reg: DescriptorRegistry, *, tool_config: dict[str, dict[str, Any]] | None = None) -> None:
    """The consult_write pack AS SHIPPED (pins, unbilled list), optionally with
    one tool's config overridden."""
    body = _pack_body()
    for tool in body["tools"]:
        if tool_config and tool["name"] in tool_config:
            tool["config"] = tool_config[tool["name"]]
    pack = ActionPack.model_validate(body, strict=False)
    try:
        await reg.get(W.CONSULT_WRITE_PACK_ID, family=Family.ACTION_PACK)
    except DescriptorNotFound:
        await reg.register(pack, "test")
        return
    await reg.update(W.CONSULT_WRITE_PACK_ID, pack, "test")


def _body(fname: str, aid: str, state: str) -> dict[str, Any]:
    body = yaml.safe_load((_DESCRIPTORS / fname).read_text())
    body["identity"].update({"id": aid, "version": "0" * 16, "state": state})
    return body


async def _unit(reg: DescriptorRegistry, *, state: str = "active", mutate: Any = None) -> str:
    aid = f"cw_unit_{uuid4().hex[:10]}"
    body = _body("analyst_escalation.yaml", aid, state)
    if mutate is not None:
        mutate(body)
    await reg.register(AnalystDescriptor.model_validate(body, strict=False), "test")
    return aid


async def _composition(reg: DescriptorRegistry, *, state: str = "active") -> str:
    aid = f"cw_comp_{uuid4().hex[:10]}"
    await reg.register(AnalystDescriptor.model_validate(
        _body("analyst_world_assessor.yaml", aid, state), strict=False), "test")
    return aid


async def _subject(reg: DescriptorRegistry, *, tags: tuple[str, ...] = ("watch",)) -> str:
    tid = f"cw_subj_{uuid4().hex[:10]}"
    body = yaml.safe_load((_DESCRIPTORS / "target_lane_black_sea.yaml").read_text())
    body["identity"].update({"id": tid, "version": "0" * 16, "state": "active"})
    body["scope"]["tags"] = list(tags)
    await reg.register(TargetDescriptor.model_validate(body, strict=False), "test")
    return tid


async def _pinned_unit(reg: DescriptorRegistry) -> str:
    """A unit whose id IS in the shipped pin set (the flip kit's ``escalation``).

    The test OWNS this id: any ``escalation`` head already in the session-shared
    database is replaced by the shipped descriptor (as a draft), never trusted.
    A stub another file left there (``test_desk_reference`` seeds a bare
    ``method.bounded_question`` row under this very id) does not parse, and
    ``inspect`` then answers ``found: False`` -- the ``KeyError: 'prompt'`` /
    ``'version'`` of nightly 10-09 .. 10-13. ``world``'s teardown removes it again.
    """
    async with reg.pg.acquire() as conn:
        await conn.execute("DELETE FROM analyst_descriptors WHERE descriptor_id = 'escalation'")
    await reg.register(AnalystDescriptor.model_validate(
        _body("analyst_escalation.yaml", "escalation", "draft"), strict=False), "test")
    return "escalation"


async def _do(w: World, tool: str, cls: str | None = None, **args: Any) -> dict[str, Any]:
    full = {"class": cls, **args} if cls is not None else dict(args)
    return await w.ex.submit(w.armed, tool=tool, args=full, turn_cost_usd=0.0)


async def _see(w: World, cls: str, aid: str) -> dict[str, Any]:
    return await _do(w, "inspect_object", cls, id=aid)


async def _head(w: World, aid: str) -> Any:
    return await w.reg.get(aid, family=Family.ANALYST)


async def _trace(w: World, aid: str, version: str, trigger: str, target: str | None = None) -> None:
    now = datetime.now(timezone.utc)
    await RuntimeReceiptChain(w.store.pool).record(
        run_id=uuid4(), analyst_id=aid, analyst_version=version, cadence_trigger=trigger,
        target_id=target, input_row_refs=[], input_payload={}, prompt_module_hash=None,
        prompt_rendered=None, output_row_refs=[], output_payload={}, run_started_at=now, run_ended_at=now,
    )


def _actor_double(w: World, calls: list[tuple[str, dict[str, Any]]]) -> SimpleNamespace:
    """The Dapr actor at the HTTP boundary: records the PUT, then writes the run's
    trace through the runtime's real receipt writer with the trigger it was sent."""

    class _Resp:
        status_code = 200

        @staticmethod
        def json() -> dict[str, Any]:
            return {"outcome": "success"}

    class _Client:
        def __init__(self, *a: Any, **k: Any) -> None:
            pass

        async def __aenter__(self) -> "_Client":
            return self

        async def __aexit__(self, *exc: Any) -> bool:
            return False

        async def put(self, url: str, json: dict[str, Any] | None = None, headers: Any = None) -> _Resp:
            calls.append((url, dict(json or {})))
            actor_id = url.split("/actors/AnalystActor/")[1].split("/method/run")[0]
            aid = actor_id.split("::")[1]
            head = await w.reg.get(aid, family=Family.ANALYST)
            await _trace(w, aid, head.version, str((json or {}).get("trigger_kind")), (json or {}).get("target_filter"))
            return _Resp()

    return SimpleNamespace(AsyncClient=_Client)


@pytest_asyncio.fixture
async def uw(world: World) -> World:
    await _yaml_pack(world.reg)
    return world


async def _audit(w: World, audit_id: str) -> dict[str, Any]:
    async with w.store.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM descriptor_audit_log WHERE id = $1::uuid", audit_id)
    assert row is not None
    return dict(row)


# ---------------------------------------------------------------------------
# 1. built verbs execute, one transaction, one receipt — unit and composition
# ---------------------------------------------------------------------------


async def test_unit_inspect_lists_the_catalog_knobs_and_what_is_protected(uw: World):
    unit = await _unit(uw.reg)
    seen = await _see(uw, "unit", unit)
    assert seen["found"] and seen["state"] == "active" and seen["kind"] == "inline_target"
    assert seen["fields"]["subscription.targets.time_window"] == "72h"
    assert seen["fields"]["method.options.judge_sample_rate"] == 1.0
    assert "method.options.judge_sample_rate" in seen["writable"] and "eval.rubric" in seen["writable"]
    assert seen["options_declared"]["route"] == "inline_target"
    assert seen["prompt"] == "inline" and W.PROMPT_PATH in seen["writable"]
    assert seen["subject_bound"] is True and seen["rerun"] == {"offered": True, "needs_subject": True}
    pinned = await _see(uw, "unit", await _pinned_unit(uw.reg))
    assert pinned["prompt"] == "pinned" and W.PROMPT_PATH in pinned["protected"]
    assert W.PROMPT_PATH not in pinned["writable"]
    # the class is the KIND: a unit asked for as a composition is not found, and says why
    wrong = await _see(uw, "composition", unit)
    assert wrong["found"] is False and "is a unit" in wrong["detail"]


async def test_unit_window_update_executes_and_receipt_audit_ledger_agree(uw: World):
    unit = await _unit(uw.reg)
    seen = await _see(uw, "unit", unit)
    res = await _do(uw, "update_object", "unit", id=unit, expected_version=seen["version"],
                    patch={"subscription.targets.time_window": 48, "method.options.judge_sample_rate": 0.25},
                    reason="widen it")
    assert res["status"] == "executed", res
    head = await _head(uw, unit)
    assert head.version != seen["version"]
    assert head.body["subscription"]["targets"]["time_window"] == "48h"  # normalised to the runtime's form
    assert head.body["method"]["options"]["judge_sample_rate"] == 0.25
    row = await uw.row(res["write_id"])
    assert row["status"] == "executed" and row["tier"] == "A" and row["class"] == "unit"
    assert row["receipt"]["pre"] == {"version": seen["version"], "state": "active"}
    assert row["receipt"]["post"] == {"version": head.version, "state": "active"}
    assert row["receipt"]["reversal"] == {"kind": "rollback", "to_version": seen["version"]}
    audit = await _audit(uw, row["audit_id"])
    assert audit["namespace"] == "analyst" and audit["actor_id"] == f"consult_write:{res['write_id']}"
    assert audit["actor_role"] == "operator_via_consult"
    assert verify_audit_payload(_identity().verify_key, signed_payload_for_row(audit), bytes(audit["signed_payload"]))


async def test_composition_input_window_executes_tier_a(uw: World):
    comp = await _composition(uw.reg)
    seen = await _see(uw, "composition", comp)
    assert seen["fields"][W.MEMBERSHIP_PATH] == [
        "region_composition", "escalation_composition", "hybrid_threat_composition",
    ]
    assert seen["fields"]["subscription.other_analysts.time_window"] == "336h"
    assert seen["subject_bound"] is False
    res = await _do(uw, "update_object", "composition", id=comp, expected_version=seen["version"],
                    patch={"subscription.other_analysts.time_window": "168h"}, reason="narrow it")
    assert res["status"] == "executed", res
    members = (await _head(uw, comp)).body["subscription"]["other_analysts"]
    assert {m["time_window"] for m in members} == {"168h"}


# ---------------------------------------------------------------------------
# 2. refusals — nothing changes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("patch", "cause"), [
    ({"method.options.not_a_knob": 3}, "path_not_writable"),          # an undeclared knob: the catalog is the contract
    ({"method.options.analyst_id": "other"}, "protected_field"),       # a runtime-reserved key
    ({"identity.kind": "deterministic"}, "protected_field"),
    ({"method.llm": {}}, "path_not_writable"),
    ({"method.options.judge_sample_rate": 2.0}, "preflight_failed"),   # out of the declared range
    ({"subscription.targets.time_window": "3d"}, "preflight_failed"),  # the runtime reads only '<hours>h'
    ({"subscription.other_analysts": ["x"]}, "path_not_writable"),     # a composition path on a unit
    ({"method.options.judge_sample_rate": 1.0}, "no_change"),
])
async def test_unit_patch_refusals_write_nothing(uw: World, patch, cause):
    unit = await _unit(uw.reg)
    seen = await _see(uw, "unit", unit)
    res = await _do(uw, "update_object", "unit", id=unit, expected_version=seen["version"], patch=patch, reason="r")
    assert res["status"] == "refused" and res["cause"] == cause, res
    assert (await _head(uw, unit)).version == seen["version"]
    assert (await uw.row(res["write_id"]))["error"]["cause"] == cause


async def test_a_pinned_prompt_is_protected_and_an_unlisted_pack_pins_every_prompt(uw: World):
    pinned = await _pinned_unit(uw.reg)
    seen = await _see(uw, "unit", pinned)
    res = await _do(uw, "update_object", "unit", id=pinned, expected_version=seen["version"],
                    patch={W.PROMPT_PATH: "You are a different unit."}, reason="rewrite it")
    assert res["cause"] == "protected_field" and "byte-identity pin" in res["detail"], res
    assert (await _head(uw, pinned)).version == seen["version"]
    # fail closed: a pack without the protected_prompts key pins EVERY prompt
    await _yaml_pack(uw.reg, tool_config={"update_object": {"classes_enabled": ["source", "unit", "composition"]}})
    unit = await _unit(uw.reg)
    seen = await _see(uw, "unit", unit)
    res = await _do(uw, "update_object", "unit", id=unit, expected_version=seen["version"],
                    patch={W.PROMPT_PATH: "New words."}, reason="r")
    assert res["cause"] == "protected_field" and "no protected_prompts" in res["detail"]


async def test_stale_read_class_mismatch_and_the_consult_surface(uw: World):
    unit = await _unit(uw.reg)
    res = await _do(uw, "update_object", "unit", id=unit, expected_version="f" * 16,
                    patch={"eval.rubric": "x"}, reason="r")
    assert res["cause"] == "stale_read"
    seen = await _see(uw, "unit", unit)
    res = await _do(uw, "update_object", "composition", id=unit, expected_version=seen["version"],
                    patch={"subscription.other_analysts.time_window": "24h"}, reason="r")
    assert res["cause"] == "class_not_writable" and "is a unit" in res["detail"]
    consult = f"cw_consult_{uuid4().hex[:8]}"
    await uw.reg.register(AnalystDescriptor.model_validate(
        _body("analyst_consult_default.yaml", consult, "active"), strict=False), "test")
    assert (await _see(uw, "unit", consult))["found"] is False


async def test_composition_membership_refuses_an_invented_analyst(uw: World):
    comp = await _composition(uw.reg)
    seen = await _see(uw, "composition", comp)
    res = await _do(uw, "update_object", "composition", id=comp, expected_version=seen["version"],
                    patch={W.MEMBERSHIP_PATH: ["region_composition", "no_such_analyst"]}, reason="r")
    assert res["cause"] == "ref_not_read" and "no_such_analyst" in res["detail"]
    res = await _do(uw, "update_object", "composition", id=comp, expected_version=seen["version"],
                    patch={W.MEMBERSHIP_PATH: [comp]}, reason="r")
    assert res["cause"] == "preflight_failed"


# ---------------------------------------------------------------------------
# 3. Tier B — a prompt edit, a membership edit, a pause: staged, then confirmed
# ---------------------------------------------------------------------------


async def test_a_prompt_edit_stages_and_the_typed_code_executes_it(uw: World):
    unit = await _unit(uw.reg)
    seen = await _see(uw, "unit", unit)
    res = await _do(uw, "update_object", "unit", id=unit, expected_version=seen["version"],
                    patch={W.PROMPT_PATH: "WHO YOU ARE. A narrower unit."}, reason="tighten it")
    assert res["status"] == "pending_confirmation" and "prompt edit" in res["card"], res
    assert (await _head(uw, unit)).version == seen["version"]  # nothing changed
    code = uw.ex._codes[res["write_id"]]
    done = await uw.ex.confirm(res["write_id"], via="typed_code", code=code)
    assert done["status"] == "executed", done
    head = await _head(uw, unit)
    assert head.body["method"]["system_prompt"] == "WHO YOU ARE. A narrower unit."
    assert (await uw.row(res["write_id"]))["confirmed_via"] == "typed_code"


async def test_a_membership_edit_stages_and_the_click_executes_it(uw: World):
    comp = await _composition(uw.reg)
    extra = await _unit(uw.reg)
    seen = await _see(uw, "composition", comp)
    res = await _do(uw, "update_object", "composition", id=comp, expected_version=seen["version"],
                    patch={W.MEMBERSHIP_PATH: ["region_composition", extra]}, reason="add the unit")
    assert res["status"] == "pending_confirmation" and f"+{extra}" in res["card"] and "−escalation_composition" in res["card"]
    done = await uw.ex.confirm(res["write_id"], via="click")
    assert done["status"] == "executed", done
    members = (await _head(uw, comp)).body["subscription"]["other_analysts"]
    assert [m["id"] for m in members] == ["region_composition", extra]
    assert members[1] == {"id": extra, "data_types": ["finding"], "time_window": "336h"}


async def test_pause_stages_confirms_and_reverts_and_retire_stages(uw: World):
    unit = await _unit(uw.reg)
    seen = await _see(uw, "unit", unit)
    res = await _do(uw, "transition_object", "unit", id=unit, expected_version=seen["version"],
                    to_state="paused", reason="pause it")
    assert res["status"] == "pending_confirmation"
    assert (await uw.ex.confirm(res["write_id"], via="click"))["status"] == "executed"
    assert (await _head(uw, unit)).state == "paused"
    rev = await uw.ex.revert_button(res["write_id"])
    assert rev["status"] == "executed", rev
    head = await _head(uw, unit)
    assert head.state == "active" and head.version == seen["version"]
    comp = await _composition(uw.reg)
    seen = await _see(uw, "composition", comp)
    res = await _do(uw, "transition_object", "composition", id=comp, expected_version=seen["version"],
                    to_state="retired", reason="r")
    assert res["status"] == "pending_confirmation" and "cannot be undone" in res["card"]
    assert (await _head(uw, comp)).state == "active"


# ---------------------------------------------------------------------------
# 4. reversal on the stored pre-image; superseded_since
# ---------------------------------------------------------------------------


async def test_revert_restores_the_pre_image_and_a_later_write_supersedes(uw: World):
    unit = await _unit(uw.reg)
    seen = await _see(uw, "unit", unit)
    before = (await _head(uw, unit)).body
    a = await _do(uw, "update_object", "unit", id=unit, expected_version=seen["version"],
                  patch={"eval.rubric": "Grade the window."}, reason="r")
    rev = await uw.ex.revert_button(a["write_id"])
    assert rev["status"] == "executed", rev
    head = await _head(uw, unit)
    assert head.version == seen["version"] and head.body == before
    mid = await _see(uw, "unit", unit)
    b = await _do(uw, "update_object", "unit", id=unit, expected_version=mid["version"],
                  patch={"method.options.offer_events": False}, reason="r")
    mid2 = await _see(uw, "unit", unit)
    c = await _do(uw, "update_object", "unit", id=unit, expected_version=mid2["version"],
                  patch={"method.options.offer_events": True}, reason="r")
    out = await uw.ex.revert_button(b["write_id"])
    assert out["cause"] == "superseded_since" and c["write_id"] in out["detail"]


# ---------------------------------------------------------------------------
# 5. the activation probe (D10) on the durable runtime signal
# ---------------------------------------------------------------------------


async def test_a_forced_parse_failed_rolls_the_unit_back(uw: World, monkeypatch):
    unit = await _unit(uw.reg)
    seen = await _see(uw, "unit", unit)
    res = await _do(uw, "update_object", "unit", id=unit, expected_version=seen["version"],
                    patch={"method.options.judge_sample_rate": 0.5}, reason="r")
    assert res["status"] == "executed"
    monkeypatch.setattr(A, "runtime_parse", lambda body: "forced: the runtime cannot parse it")
    verdict = await uw.ex.probe_once(res["write_id"])
    assert verdict["status"] == "parse_failed" and verdict["rolled_back_to"] == seen["version"], verdict
    assert (await _head(uw, unit)).version == seen["version"]
    row = await uw.row(res["write_id"])
    assert row["activation"]["status"] == "parse_failed"
    assert row["receipt"]["activation"]["rolled_back_to"] == seen["version"]
    async with uw.store.acquire() as conn:
        rb = await conn.fetchrow(
            "SELECT action, actor_role, namespace FROM descriptor_audit_log WHERE descriptor_id = $1 "
            "ORDER BY occurred_at DESC LIMIT 1", unit)
    assert dict(rb) == {"action": "rollback", "actor_role": "operator_via_consult", "namespace": "analyst"}


async def test_the_probe_verifies_on_the_first_trace_at_the_new_version_and_is_honest_when_paused(uw: World):
    unit = await _unit(uw.reg)
    seen = await _see(uw, "unit", unit)
    res = await _do(uw, "update_object", "unit", id=unit, expected_version=seen["version"],
                    patch={"method.options.offer_history": True}, reason="r")
    await _trace(uw, unit, seen["version"], "cadence")  # a run of the OLD head proves nothing
    assert await uw.ex.probe_once(res["write_id"]) is None
    await _trace(uw, unit, (await _head(uw, unit)).version, "cadence")
    verdict = await uw.ex.probe_once(res["write_id"])
    assert verdict["status"] == "verified" and verdict["trigger"] == "cadence"
    paused = await _unit(uw.reg, state="paused")
    seen = await _see(uw, "unit", paused)
    res = await _do(uw, "update_object", "unit", id=paused, expected_version=seen["version"],
                    patch={"eval.rubric": "x"}, reason="r")
    assert (await uw.ex.probe_once(res["write_id"]))["status"] == "not_applicable"


# ---------------------------------------------------------------------------
# 6. rerun — a forced run with trigger_kind "method", no undo
# ---------------------------------------------------------------------------


async def test_rerun_of_a_composition_lands_a_method_triggered_run(uw: World, monkeypatch):
    calls: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(A, "httpx", _actor_double(uw, calls))
    comp = await _composition(uw.reg)
    await _see(uw, "composition", comp)
    res = await _do(uw, "rerun_object", "composition", id=comp, reason="run it now")
    assert res["status"] == "executed", res
    await A.drain_reruns()
    head = await _head(uw, comp)
    assert calls == [(f"{A._sidecar_url()}/v1.0/actors/AnalystActor/analyst::{comp}::{head.version[:16]}/method/run",
                      {"trigger_kind": "method"})]
    row = await uw.row(res["write_id"])
    assert row["status"] == "executed" and row["verb"] == "rerun"
    assert row["receipt"]["reversal"]["kind"] == "none"
    assert row["activation"]["status"] == "run_landed" and row["activation"]["trace_status"] == "success"
    async with uw.store.acquire() as conn:
        trig = await conn.fetchval(
            "SELECT cadence_trigger FROM analyst_traces WHERE run_id = $1::uuid", row["activation"]["run_id"])
    assert trig == "method"
    audit = await _audit(uw, row["audit_id"])
    assert audit["action"] == "rerun" and audit["actor_role"] == "operator_via_consult"
    assert audit["from_version"] == audit["to_version"] == head.version
    assert verify_audit_payload(_identity().verify_key, signed_payload_for_row(audit), bytes(audit["signed_payload"]))
    undo = await uw.ex.revert_button(res["write_id"])
    assert undo["status"] == "refused" and "cannot be undone" in undo["detail"]


async def test_rerun_of_a_subject_bound_unit_needs_a_matching_subject(uw: World, monkeypatch):
    calls: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(A, "httpx", _actor_double(uw, calls))
    unit = await _unit(uw.reg)
    subj = await _subject(uw.reg, tags=("watch",))
    off = await _subject(uw.reg, tags=())
    assert (await _do(uw, "rerun_object", "unit", id=unit, reason="r"))["cause"] == "preflight_failed"
    out = await _do(uw, "rerun_object", "unit", id=unit, subject=off, reason="r")
    assert out["cause"] == "preflight_failed" and "not one of" in out["detail"]
    out = await _do(uw, "rerun_object", "unit", id=unit, subject="no_such_subject", reason="r")
    assert out["cause"] == "ref_not_read"
    res = await _do(uw, "rerun_object", "unit", id=unit, subject=subj, reason="r")
    assert res["status"] == "executed", res
    await A.drain_reruns()
    assert calls[-1] == (f"{A._sidecar_url()}/v1.0/actors/AnalystActor/analyst::{unit}::{subj}/method/run",
                         {"trigger_kind": "method", "target_filter": subj})
    act = (await uw.row(res["write_id"]))["activation"]
    assert act["status"] == "run_landed" and act["subject"] == subj


async def test_rerun_is_refused_on_a_billed_plane_and_on_a_paused_head(uw: World):
    def _billed(body: dict[str, Any]) -> None:
        body["method"]["llm"]["primary"]["raw"] = "llm.judge.openrouter_mistral_large.openai_compat"

    billed = await _unit(uw.reg, mutate=_billed)
    out = await _do(uw, "rerun_object", "unit", id=billed, subject=await _subject(uw.reg), reason="r")
    assert out["cause"] == "billed_rerun_not_offered" and "openrouter" in out["detail"]
    assert (await _see(uw, "unit", billed))["rerun"]["offered"] is False
    paused = await _unit(uw.reg, state="paused")
    assert (await _do(uw, "rerun_object", "unit", id=paused, reason="r"))["cause"] == "preflight_failed"


async def test_a_dead_sidecar_is_recorded_never_a_fabricated_landing(uw: World, monkeypatch):
    class _Down:
        def __init__(self, *a: Any, **k: Any) -> None:
            raise ConnectionError("sidecar down")

    monkeypatch.setattr(A, "httpx", SimpleNamespace(AsyncClient=_Down))
    comp = await _composition(uw.reg)
    res = await _do(uw, "rerun_object", "composition", id=comp, reason="r")
    await A.drain_reruns()
    act = (await uw.row(res["write_id"]))["activation"]
    assert act["status"] == "run_failed" and "sidecar down" in act["error"]


# ---------------------------------------------------------------------------
# 7. switches and seams
# ---------------------------------------------------------------------------


async def test_class_switch_and_the_unit_create_seam(uw: World):
    unit = await _unit(uw.reg)
    seen = await _see(uw, "unit", unit)
    await _yaml_pack(uw.reg, tool_config={"update_object": {"classes_enabled": ["source"], "protected_prompts": []}})
    res = await _do(uw, "update_object", "unit", id=unit, expected_version=seen["version"],
                    patch={"eval.rubric": "x"}, reason="r")
    assert res["cause"] == "class_disabled"
    with pytest.raises(W.WriteKindNotBuilt) as err:
        await _do(uw, "create_object", "unit", body={}, reason="r")
    assert err.value.seam == 64


# ---------------------------------------------------------------------------
# 8. through the REAL binding: read set, the rerun's subject, the ledger
# ---------------------------------------------------------------------------


async def test_unit_writes_through_the_real_binding_and_the_read_set(rig, monkeypatch):
    world, _front, _substrate, binding = rig
    await _yaml_pack(world.reg)
    calls: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(A, "httpx", _actor_double(world, calls))
    unit = await _unit(world.reg)
    subj = await _subject(world.reg)
    gate, rec = _gate(world, binding)
    insp = {"tool": "inspect_object", "args": {"class": "unit", "id": unit}}
    seen = await gate.run("inspect_object", insp["args"], 1)
    _round(gate, 1, [insp], [seen])
    upd, meta = await gate.run("update_object", {
        "class": "unit", "id": unit, "expected_version": seen[0]["version"],
        "patch": {"subscription.targets.time_window": "72h", "method.options.history_series_limit": 4},
        "reason": "the operator asked"}, 2)
    assert meta["admitted"] and upd["status"] == "executed", upd
    rerun = {"class": "unit", "id": unit, "subject": subj, "reason": "run it"}
    out, meta = await gate.run("rerun_object", rerun, 3)
    assert out["error"].startswith("tool_blocked: ref_not_read: ") and subj in out["error"]
    listed = {"tool": "list_targets", "args": {}}
    _round(gate, 3, [listed], [({"rows": [{"target_id": subj}]}, {})])  # the subject arrives through a read
    out, meta = await gate.run("rerun_object", rerun, 4)
    assert meta["admitted"] and out["status"] == "executed", out
    await A.drain_reruns()
    assert calls and calls[-1][1] == {"trigger_kind": "method", "target_filter": subj}
    inv, events = await _ledger(world, gate.binding.requested_by)
    assert [t for t, _ in inv] == ["inspect_object", "update_object", "rerun_object"]
    assert [(d, t) for d, _c, t in events] == [
        ("allow", "inspect_object"), ("allow", "update_object"), ("block", "rerun_object"), ("allow", "rerun_object")]
    assert events[2][1] == "ref_not_read"
    assert gate.truth_line() == "writes: executed 2 · staged 0 · refused 1"


# ---------------------------------------------------------------------------
# guards
# ---------------------------------------------------------------------------


def test_the_shipped_pin_list_is_the_flip_kits_set():
    """D9: the pin list is instance data in the pack; it must equal the flip kit's set."""
    tree = ast.parse((_ROOT / "scripts" / "voice4_flip" / "_flip_common.py").read_text())
    consts: dict[str, Any] = {}
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            target = node.targets[0] if isinstance(node, ast.Assign) else node.target
            if isinstance(target, ast.Name) and target.id in {"UNITS", "HELD_UNIT"}:
                consts[target.id] = ast.literal_eval(node.value)
    kit = set(consts["UNITS"]) | {consts["HELD_UNIT"]}
    update = [t for t in _pack_body()["tools"] if t["name"] == "update_object"][0]
    assert set(update["config"]["protected_prompts"]) == kit


def test_the_mirrored_constants_and_the_vocabulary_words():
    from legba.data.registry import consult_api

    assert (A.DAPR_SIDECAR_URL_ENV, A.DAPR_SIDECAR_URL_DEFAULT, A.ACTOR_TYPE) == (
        consult_api.DAPR_SIDECAR_URL_ENV, consult_api.DAPR_SIDECAR_URL_DEFAULT, consult_api.ACTOR_TYPE)
    assert W.analyst_tier("transition", from_state="active", to_state="paused") == "B"
    assert W.analyst_tier("transition", from_state="paused", to_state="active") == "A"
    assert W.analyst_tier("retire", from_state="draft", to_state="retired") == "B"
    assert W.analyst_tier("update", from_state="active", to_state=None, paths={W.PROMPT_PATH: "x"}) == "B"
    assert W.analyst_tier("update", from_state="active", to_state=None, paths=[W.MEMBERSHIP_PATH]) == "B"
    assert W.analyst_tier("update", from_state="active", to_state=None, paths=["eval.rubric"]) == "A"
    assert W.seam_for("unit", "create") == 64 and W.seam_for("unit", "rerun") is None
    rerun = W.TOOL_SCHEMAS["rerun_object"][1]
    assert rerun["properties"]["class"]["enum"] == ["unit", "composition"] and "subject" not in rerun["required"]


def test_the_prompt_block_names_unit_create_as_the_seam():
    from legba.data.analysts.consult_write_gate import write_prompt_block

    class _Binding:
        pack = ActionPack.model_validate({**_pack_body()}, strict=False)

    text = write_prompt_block(_Binding())
    assert "unit create" in text and "rerun_object(" in text
    assert json.dumps(W.BUILT_CLASSES)  # the enum the tools offer


async def test_the_new_audit_action_reads_back_on_the_registry_history(uw: World, monkeypatch):
    monkeypatch.setattr(A, "httpx", _actor_double(uw, []))
    comp = await _composition(uw.reg)
    await _do(uw, "rerun_object", "composition", id=comp, reason="r")
    await A.drain_reruns()
    async with uw.store.acquire() as conn:
        actions = [r["action"] for r in await conn.fetch(
            "SELECT action FROM descriptor_audit_log WHERE descriptor_id = $1 ORDER BY occurred_at", comp)]
    assert actions == ["register", "rerun"]


async def test_the_pinned_unit_is_owned_even_when_another_file_left_a_stub_under_its_id(uw: World):
    """X4 NIGHTLY: ``test_desk_reference`` seeded a bare ``method.bounded_question``
    row under ``escalation``; inspect answered found:false and these tests died on
    ``KeyError``. The helper now replaces whatever it finds."""
    async with uw.reg.pg.acquire() as conn:
        await conn.execute("DELETE FROM analyst_descriptors WHERE descriptor_id = 'escalation'")
        await conn.execute(
            "INSERT INTO analyst_descriptors (descriptor_id, version, is_head, kind, state, owner, name, "
            "schema_uri, type_signature, inherits, body) VALUES ('escalation', $1, TRUE, 'inline_target', "
            "'active', 'test_a1', 'escalation', 'legba/analyst/1.0.0', '{}'::jsonb, '{}', $2::jsonb)",
            "0" * 16, json.dumps({"method": {"bounded_question": "A stub?"}}),
        )
    assert (await _see(uw, "unit", "escalation"))["found"] is False  # the stub, as the nightly saw it
    seen = await _see(uw, "unit", await _pinned_unit(uw.reg))
    assert seen["found"] and seen["prompt"] == "pinned" and seen["version"]
