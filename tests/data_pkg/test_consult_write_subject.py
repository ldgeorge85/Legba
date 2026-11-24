# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""C-2b-1/subject (W6) — the write plane's SUBJECT class (code: targets) on the REAL driver.

Design C-2a §10 items 1–8 for ``subject``, on the frame's fixture: real migrated
Postgres (migration 0227), a real ``DescriptorRegistry`` with a real signing
identity and the seeded vocabulary, the real ``consult_write`` pack descriptor,
real ``consult_sessions`` / ``consult_turns`` rows. Every value claimed
persisted is read back with SQL. The last tests run the tools through the real
``AgencyToolBinding.run_tool`` and the runtime's thin client (its preflight reads
the target's ``/typed`` head) to the registry over ASGI. The Dapr sidecar is the
one boundary double (a confirm must never reach it).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from legba.data import consult_write_vocabulary as W
from legba.data.registry import consult_persistence
from legba.data.registry.audit import signed_payload_for_row
from legba.data.registry.consult_write_exec import CLASS_BRANCHES, ConsultWriteExecutor, executor_for
from legba.data.registry.descriptor import DescriptorRegistry, Family
from legba.data.registry.signing import verify_audit_payload
from legba.data.schemas.target import TargetDescriptor
from tests.data_pkg.test_consult_write_frame import (  # noqa: F401 — the shared fixture + helpers
    World,
    _arm,
    _audit_row,
    _identity,
    _no_dapr,
    _set_pack,
    world,
)

WORDS = "Rename the Brazil subject to Brazil Watch and add the energy tag."
CLASSES = ["source", "subject"]


def _subject_body(sid: str, *, state: str = "active", name: str = "Brazil") -> dict[str, Any]:
    return {
        "identity": {
            "id": sid, "name": name, "schema_uri": "legba/target/2.0.0", "version": "0" * 16,
            "abstraction_level": "L1", "state": state, "owner": "test", "created": "2026-10-01T00:00:00Z",
        },
        "scope": {
            "domain": "geo", "geo": ["BR"], "languages": ["pt-BR"],
            "entity_classes": ["organization", "country"], "relationship_types": [], "tags": ["news"],
        },
        "sources": [{"source_id": "rss_main"}] if state == "active" else [],
    }


async def _subject(reg: DescriptorRegistry, *, state: str = "active") -> str:
    sid = f"cw_subject_{uuid4().hex[:10]}"
    await reg.register(TargetDescriptor.model_validate(_subject_body(sid, state=state), strict=False), "test")
    return sid


@dataclass
class S:
    """The frame's world, re-armed with a subject instruction and a subject on it."""

    w: World
    sid: str

    @property
    def ex(self) -> ConsultWriteExecutor:
        return self.w.ex

    async def inspect(self, sid: str | None = None) -> dict[str, Any]:
        return await self.w.ex.submit(self.w.armed, tool="inspect_object",
                                      args={"class": "subject", "id": sid or self.sid})

    async def call(self, tool: str, **args: Any) -> dict[str, Any]:
        return await self.w.ex.submit(self.w.armed, tool=tool, args={"class": "subject", **args},
                                      turn_cost_usd=0.0)

    async def head(self, sid: str | None = None) -> Any:
        return await self.w.reg.get(sid or self.sid, family=Family.TARGET)

    async def row(self, wid: str) -> dict[str, Any]:
        return await self.w.row(wid)


@pytest_asyncio.fixture
async def subj(world: World) -> S:
    await _set_pack(world.reg, classes=CLASSES)
    world.armed = await _arm(world.store, WORDS)
    return S(w=world, sid=await _subject(world.reg))


async def _raw_body(s: S, sid: str, version: str) -> dict[str, Any]:
    async with s.w.store.acquire() as conn:
        body = await conn.fetchval(
            "SELECT body FROM target_descriptors WHERE descriptor_id = $1 AND version = $2", sid, version)
    return json.loads(body) if isinstance(body, str) else dict(body)


# ---------------------------------------------------------------------------
# 1. inspect → update (compare-and-set) → receipt + signed audit row → revert
# ---------------------------------------------------------------------------


async def test_inspect_shows_the_writable_fields_and_says_cadence_is_not_one(subj: S):
    seen = await subj.inspect()
    assert seen["found"] and seen["class"] == "subject" and seen["state"] == "active"
    assert seen["fields"] == {"identity.name": "Brazil", "scope.geo": ["BR"], "scope.tags": ["news"],
                              "scope.entity_classes": ["organization", "country"]}
    assert seen["writable"] == ["identity.name", "scope.geo", "scope.tags", "scope.entity_classes"]
    assert "identity.id" in seen["protected"] and "schema_uri" in seen["protected"]
    assert seen["read_only"]["scope.domain"] == "geo" and seen["read_only"]["sources"] == 1
    assert seen["cadence"].startswith("not writable")
    assert seen["versions"][0] == {**seen["versions"][0], "version": seen["version"], "head": True}
    missing = await subj.inspect("cw_subject_nope")
    assert missing == {"found": False, "class": "subject", "id": "cw_subject_nope"}


async def test_rename_executes_with_receipt_and_audit_row_and_revert_restores_the_pre_image(subj: S):
    seen = await subj.inspect()
    before = await subj.head()
    res = await subj.call("update_object", id=subj.sid, expected_version=seen["version"],
                          patch={"identity.name": "Brazil Watch", "scope.tags": ["news", "energy"]},
                          reason="the operator renamed it")
    assert res["status"] == "executed", res
    head = await subj.head()
    assert head.name == "Brazil Watch" and head.body["scope"]["tags"] == ["news", "energy"]
    assert head.version != seen["version"] and head.state == "active"

    row = await subj.row(res["write_id"])
    assert row["status"] == "executed" and row["tier"] == "A" and row["class"] == "subject"
    receipt = row["receipt"]
    assert receipt["operator_words"] == WORDS and receipt["class"] == "subject" and receipt["verb"] == "update"
    assert receipt["pre"] == {"version": seen["version"], "state": "active"}
    assert receipt["post"] == {"version": head.version, "state": "active"}
    assert receipt["reversal"] == {"kind": "rollback", "to_version": seen["version"]}

    audit = await _audit_row(subj.w.store, row["audit_id"])
    assert audit["namespace"] == "target" and audit["descriptor_id"] == subj.sid
    assert audit["actor_id"] == f"consult_write:{res['write_id']}" and audit["actor_role"] == "operator_via_consult"
    summary = audit["change_summary"]
    summary = json.loads(summary) if isinstance(summary, str) else summary
    assert summary["receipt"]["write_id"] == res["write_id"]
    assert verify_audit_payload(_identity().verify_key, signed_payload_for_row(audit), bytes(audit["signed_payload"]))

    # revert_write through the executor: the write id comes from a later inspect.
    listed = await subj.inspect()
    assert [w["write_id"] for w in listed["writes"]] == [res["write_id"]]
    rev = await subj.call("revert_write", write_id=res["write_id"], reason="undo it")
    assert rev["status"] == "executed", rev
    after = await subj.head()
    assert after.version == seen["version"] and after.body == before.body  # the pre-image hash and body
    assert (await subj.row(res["write_id"]))["reversed_by_write_id"] == rev["write_id"]
    rev_audit = await _audit_row(subj.w.store, (await subj.row(rev["write_id"]))["audit_id"])
    assert rev_audit["action"] == "rollback" and rev_audit["actor_role"] == "operator_via_consult"
    again = await subj.ex.revert_button(res["write_id"])
    assert again["cause"] == "superseded_since"


async def test_the_autowire_trailer_rides_onto_the_new_head_and_back(subj: S):
    seen = await subj.inspect()
    trailer = {"src_a": "2026-10-01T00:00:00+00:00"}
    async with subj.w.store.acquire() as conn:
        await conn.execute(
            "UPDATE target_descriptors SET body = body || jsonb_build_object('_auto_wired_sources', $2::jsonb) "
            "WHERE descriptor_id = $1 AND is_head", subj.sid, json.dumps(trailer))
    res = await subj.call("update_object", id=subj.sid, expected_version=seen["version"],
                          patch={"scope.geo": ["BR", "AR"]}, reason="r")
    assert res["status"] == "executed", res
    head = await subj.head()
    assert (await _raw_body(subj, subj.sid, head.version))["_auto_wired_sources"] == trailer
    # an autowire on the NEW head is kept by the revert too
    async with subj.w.store.acquire() as conn:
        await conn.execute(
            "UPDATE target_descriptors SET body = jsonb_set(body, '{_auto_wired_sources,src_b}', '\"t2\"') "
            "WHERE descriptor_id = $1 AND is_head", subj.sid)
    rev = await subj.ex.revert_button(res["write_id"])
    assert rev["status"] == "executed", rev
    restored = await _raw_body(subj, subj.sid, seen["version"])
    assert restored["_auto_wired_sources"] == {**trailer, "src_b": "t2"}
    assert (await subj.head()).version == seen["version"]


# ---------------------------------------------------------------------------
# 2. refusals: protected / not writable / preflight / vocabulary / stale — nothing changes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("patch", "cause"), [
    ({"identity.id": "cw_other"}, "protected_field"),
    ({"schema_uri": "legba/target/3.0.0"}, "protected_field"),
    ({"identity.version": "a" * 16}, "protected_field"),
    ({"scope.predicate": "geo_match([\"BR\"])"}, "path_not_writable"),
    ({"scope.languages": ["en"]}, "path_not_writable"),
    ({"scope.domain": "thematic"}, "path_not_writable"),
    ({"analyst.cadence.fallback_schedule": "*/5 * * * *"}, "path_not_writable"),
    ({"sources": []}, "path_not_writable"),
    ({"scope.geo": ["brazil"]}, "preflight_failed"),
    ({"scope.tags": "news"}, "preflight_failed"),
])
async def test_patch_refusals_write_nothing(subj: S, patch, cause):
    seen = await subj.inspect()
    res = await subj.call("update_object", id=subj.sid, expected_version=seen["version"], patch=patch, reason="r")
    assert res["status"] == "refused" and res["cause"] == cause, res
    assert (await subj.head()).version == seen["version"]
    assert (await subj.row(res["write_id"]))["error"]["cause"] == cause


async def test_an_unknown_entity_class_is_refused_at_plan_time_without_a_dead_letter(subj: S):
    seen = await subj.inspect()
    bogus = f"ufo_{uuid4().hex[:6]}"
    res = await subj.call("update_object", id=subj.sid, expected_version=seen["version"],
                          patch={"scope.entity_classes": ["organization", bogus]}, reason="r")
    assert res["cause"] == "preflight_failed" and bogus in res["detail"], res
    async with subj.w.store.acquire() as conn:
        dead = await conn.fetchval(
            "SELECT count(*) FROM descriptor_dead_letter WHERE attempted_payload->'identity'->>'id' = $1", subj.sid)
    assert dead == 0 and (await subj.head()).version == seen["version"]


async def test_a_stale_version_is_stale_read(subj: S):
    res = await subj.call("update_object", id=subj.sid, expected_version="f" * 16,
                          patch={"identity.name": "x"}, reason="r")
    assert res["cause"] == "stale_read"


async def test_a_restated_head_is_no_change(subj: S):
    seen = await subj.inspect()
    res = await subj.call("update_object", id=subj.sid, expected_version=seen["version"],
                          patch={"identity.name": "Brazil"}, reason="r")
    assert res["cause"] == "no_change" and (await subj.head()).version == seen["version"]


# ---------------------------------------------------------------------------
# 3. Tier B: pause an active subject; retire a draft by the typed code, zero LLM calls
# ---------------------------------------------------------------------------


async def test_pause_of_an_active_subject_stages_and_the_click_executes_and_revert_reactivates(subj: S):
    seen = await subj.inspect()
    res = await subj.call("transition_object", id=subj.sid, expected_version=seen["version"],
                          to_state="paused", reason="pause it")
    assert res["status"] == "pending_confirmation" and res["tier"] == "B", res
    assert (await subj.head()).state == "active"  # nothing changed
    done = await subj.ex.confirm(res["write_id"], via="click")
    assert done["status"] == "executed", done
    assert (await subj.head()).state == "paused"
    assert done["receipt"]["reversal"] == {"kind": "transition", "to_state": "active"}
    rev = await subj.ex.revert_button(res["write_id"])
    assert rev["status"] == "executed", rev
    head = await subj.head()
    assert head.state == "active" and head.version == seen["version"]


async def test_head_moved_before_confirm_is_stale_read(subj: S):
    seen = await subj.inspect()
    res = await subj.call("transition_object", id=subj.sid, expected_version=seen["version"],
                          to_state="paused", reason="r")
    body = (await subj.w.reg.get_typed(subj.sid, family=Family.TARGET)).model_dump(mode="json", by_alias=True)
    body["scope"]["tags"] = ["news", "someone_else"]
    await subj.w.reg.update(subj.sid, TargetDescriptor.model_validate(body, strict=False), "someone_else")
    out = await subj.ex.confirm(res["write_id"], via="click")
    assert out["status"] == "refused" and out["cause"] == "stale_read"
    assert (await subj.head()).state == "active"


async def test_activate_is_tier_a_and_an_illegal_move_is_refused(subj: S):
    draft = await _subject(subj.w.reg, state="draft")
    seen = await subj.inspect(draft)
    bad = await subj.call("transition_object", id=draft, expected_version=seen["version"],
                          to_state="active", reason="r")
    assert bad["cause"] == "preflight_failed" and "not a legal lifecycle move" in bad["detail"]
    ok = await subj.call("transition_object", id=draft, expected_version=seen["version"],
                         to_state="configured", reason="r")
    assert ok["status"] == "executed" and (await subj.head(draft)).state == "configured"
    seen = await subj.inspect(draft)
    # configured → active with no sources: the runtime parser refuses it before anything lands
    out = await subj.call("transition_object", id=draft, expected_version=seen["version"],
                          to_state="active", reason="r")
    assert out["cause"] == "preflight_failed" and "at least one source" in out["detail"], out
    # paused → active executes on the armed turn (Tier A: the design stages only pause and retire)
    body = (await subj.w.reg.get_typed(subj.sid, family=Family.TARGET)).model_dump(mode="json", by_alias=True)
    body["identity"]["state"] = "paused"
    await subj.w.reg.update(subj.sid, TargetDescriptor.model_validate(body, strict=False), "someone_else")
    seen = await subj.inspect()
    up = await subj.call("transition_object", id=subj.sid, expected_version=seen["version"],
                         to_state="active", reason="resume it")
    assert up["status"] == "executed" and (await subj.row(up["write_id"]))["tier"] == "A", up
    assert (await subj.head()).state == "active"


async def test_retire_a_draft_stages_and_the_typed_code_confirms_with_zero_model_invokes(subj: S, monkeypatch):
    import legba.data.registry.consult_api as consult_api
    from legba.data.registry.api import RegistryAPIDeps

    monkeypatch.delenv("LEGBA_REGISTRY_API_TOKEN", raising=False)
    invoked: list[Any] = []
    monkeypatch.setattr(consult_api.httpx, "AsyncClient", _no_dapr(invoked))
    ex = executor_for(subj.w.reg)  # THE executor the front door uses
    draft = await _subject(subj.w.reg, state="draft")
    seen = await ex.submit(subj.w.armed, tool="inspect_object", args={"class": "subject", "id": draft})
    staged = await ex.submit(subj.w.armed, tool="transition_object", args={
        "class": "subject", "id": draft, "expected_version": seen["version"],
        "to_state": "retired", "reason": "never used"})
    assert staged["status"] == "pending_confirmation" and "cannot be undone" in staged["card"], staged
    assert (await subj.head(draft)).state == "draft"
    code = ex._codes[staged["write_id"]]
    deps = RegistryAPIDeps(
        descriptor_registry=subj.w.reg, stack_registry=None, vault=None, dlq=None,  # type: ignore[arg-type]
        audit_logger=None, vocabulary_cache=None, nats_store=None,  # type: ignore[arg-type]
    )
    app = FastAPI()
    app.include_router(consult_api.build_consult_router(deps), prefix="/api/v1")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post("/api/v1/consult", json={"question": f"confirm {code}",
                                                  "session_id": subj.w.armed.session_id})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["answer"].startswith("Written · subject ") and body["usage"] == {}
    assert invoked == []  # zero actor invokes: zero LLM calls
    assert (await subj.head(draft)).state == "retired"
    row = await subj.row(staged["write_id"])
    assert row["confirmed_via"] == "typed_code" and row["receipt"]["reversal"] == {
        "kind": "none", "why": "retired is terminal"}
    undo = await ex.revert_button(staged["write_id"])
    assert undo["status"] == "refused" and "cannot be undone" in undo["detail"]


# ---------------------------------------------------------------------------
# 4. create lands draft; its revert retires the draft
# ---------------------------------------------------------------------------


async def test_create_lands_draft_and_its_revert_retires_the_draft(subj: S):
    new_id = f"cw_subject_new_{uuid4().hex[:8]}"
    res = await subj.call("create_object", body=_subject_body(new_id, state="active"), reason="watch it")
    assert res["status"] == "executed", res
    head = await subj.head(new_id)
    assert head.state == "draft" and head.owner == "test"
    rev = await subj.ex.revert_button(res["write_id"])
    assert rev["status"] == "executed" and (await subj.head(new_id)).state == "retired"
    taken = await subj.call("create_object", body=_subject_body(subj.sid), reason="r")
    assert taken["cause"] == "preflight_failed" and "already exists" in taken["detail"]
    bad = _subject_body(f"cw_subject_bad_{uuid4().hex[:6]}")
    bad["scope"]["entity_classes"] = ["not_a_class_at_all"]
    assert (await subj.call("create_object", body=bad, reason="r"))["cause"] == "preflight_failed"


# ---------------------------------------------------------------------------
# 5. the activation probe (D10): honest not_applicable; a forced parse_failed rolls back
# ---------------------------------------------------------------------------


async def test_the_probe_is_honest_and_a_forced_parse_failed_rolls_the_subject_back(subj: S):
    seen = await subj.inspect()
    res = await subj.call("update_object", id=subj.sid, expected_version=seen["version"],
                          patch={"identity.name": "Brazil (probe)"}, reason="r")
    verdict = await subj.ex.probe_once(res["write_id"])
    assert verdict["status"] == "not_applicable" and verdict["parsed"] is True, verdict
    assert "no durable runtime signal" in verdict["why"]
    assert (await subj.row(res["write_id"]))["receipt"]["activation"]["status"] == "not_applicable"

    ex = ConsultWriteExecutor(subj.w.reg, probe=False, parse_check=lambda body: "forced: unparseable")
    mid = await subj.inspect()
    res = await ex.submit(subj.w.armed, tool="update_object", args={
        "class": "subject", "id": subj.sid, "expected_version": mid["version"],
        "patch": {"identity.name": "Brazil (bad)"}, "reason": "r"})
    assert res["status"] == "executed"
    verdict = await ex.probe_once(res["write_id"])
    assert verdict["status"] == "parse_failed" and verdict["rolled_back_to"] == mid["version"], verdict
    assert (await subj.head()).version == mid["version"]


# ---------------------------------------------------------------------------
# 6–8. kill switch per class; the route; the vocabulary / drift words
# ---------------------------------------------------------------------------


async def test_dropping_subject_from_the_pack_stops_subject_writes_only(subj: S):
    seen = await subj.inspect()
    await _set_pack(subj.w.reg, classes=["source"])
    res = await subj.call("update_object", id=subj.sid, expected_version=seen["version"],
                          patch={"identity.name": "x"}, reason="r")
    assert res["cause"] == "class_disabled"
    await _set_pack(subj.w.reg, classes=CLASSES)
    res = await subj.call("update_object", id=subj.sid, expected_version=seen["version"],
                          patch={"identity.name": "x"}, reason="r")
    assert res["status"] == "executed"


async def test_the_route_admits_subject_and_a_seamed_verb_is_still_501(subj: S, monkeypatch):
    from legba.data.registry import consult_writes_api as front_mod

    monkeypatch.delenv("LEGBA_REGISTRY_API_TOKEN", raising=False)
    deps = type("Deps", (), {"descriptor_registry": subj.w.reg})()
    front = front_mod.WritesFront(deps, is_live=lambda rid: True, executor=subj.ex)
    front.armed[subj.w.armed.request_id] = subj.w.armed
    app = FastAPI()
    app.include_router(front_mod.build_router(front), prefix="/api/v1")
    rid = subj.w.armed.request_id
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        seen = (await c.post("/api/v1/consult/writes", json={
            "request_id": rid, "tool": "inspect_object", "args": {"class": "subject", "id": subj.sid}})).json()
        assert seen["found"] and seen["class"] == "subject"
        r = await c.post("/api/v1/consult/writes", json={
            "request_id": rid, "tool": "create_object",
            "args": {"class": "unit", "body": {"identity": {"id": "u"}}, "reason": "r"}})
        assert r.status_code == 501 and r.json()["detail"]["seam"] == 64
        r = await c.post("/api/v1/consult/writes", json={
            "request_id": rid, "tool": "update_object",
            "args": {"class": "subject", "id": subj.sid, "expected_version": seen["version"],
                     "patch": {"identity.name": "Brazil via route"}, "reason": "r"}})
        assert r.status_code == 200 and r.json()["status"] == "executed"
        listed = (await c.get("/api/v1/consult/writes", params={"request_id": rid, "class": "subject", "object": subj.sid})).json()
    assert [w["verb"] for w in listed["writes"]] == ["update"]


def test_the_subject_words():
    assert "subject" in W.BUILT_CLASSES
    assert {v for (c, v), e in W.VOCABULARY.items() if c == "subject" and e == "built"} == CLASS_BRANCHES[
        "subject"].VERBS
    assert CLASS_BRANCHES["subject"].FAMILY is Family.TARGET  # platform-neutral word → code family (C-2a §12)
    for name in ("inspect_object", "create_object", "update_object", "transition_object"):
        assert "subject" in W.TOOL_SCHEMAS[name][1]["properties"]["class"]["enum"]
    assert W.seam_for("unit", "create") == 64 and W.seam_for("proposal", "accept") == 64
    assert W.writable_paths("subject") == ("identity.name", "scope.geo", "scope.tags", "scope.entity_classes")
    assert W.subject_tier("transition", from_state="active", to_state="paused") == "B"
    assert W.subject_tier("transition", from_state="paused", to_state="active") == "A"
    assert W.subject_tier("transition", from_state="configured", to_state="active") == "A"
    assert W.subject_tier("retire", from_state="draft", to_state="retired") == "B"
    assert W.subject_tier("update", from_state="active", to_state=None) == "A"


# ---------------------------------------------------------------------------
# The binding path: the real AgencyToolBinding → thin client (runtime preflight
# over the target's /typed head) → POST /consult/writes → the executor
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def srig(subj: S, monkeypatch):
    from legba.data.analysts.agency import Agency, AgencyToolBinding, GLOBAL_SCOPE, ToolContext
    from legba.data.registry import consult_writes_api as front_mod
    from legba.data.schemas.action_pack import ActionPack, ActionPackRef
    from legba.runtime.consult_bindings import build_pack_binding
    from legba.runtime.registry_client import RegistryHTTPClient
    from tests.data_pkg.test_consult_write_gate import _body, _Port, _Registry

    monkeypatch.delenv("LEGBA_REGISTRY_API_TOKEN", raising=False)
    front = front_mod.WritesFront(
        type("Deps", (), {"descriptor_registry": subj.w.reg})(), is_live=lambda rid: True, executor=subj.ex,
    )
    front.armed[subj.w.armed.request_id] = subj.w.armed
    app = FastAPI()
    app.include_router(front_mod.build_router(front), prefix="/api/v1")
    typed_reads: list[str] = []

    @app.get("/api/v1/registry/descriptors/{family}/{descriptor_id}/typed")
    async def _typed(family: str, descriptor_id: str) -> dict[str, Any]:
        typed_reads.append(family)
        typed = await subj.w.reg.get_typed(descriptor_id, family=Family(family))
        return typed.model_dump(mode="json", by_alias=True)

    http = httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://registry")
    rc = RegistryHTTPClient(base_url="http://registry", token="", client=http)
    who = f"analyst::cw_{uuid4().hex[:8]}"
    grants = [ActionPackRef(pack_id="substrate_read"), ActionPackRef(pack_id=W.CONSULT_WRITE_PACK_ID)]
    substrate = AgencyToolBinding(
        agency=Agency(),
        pack=ActionPack.model_validate(_body("action_pack_substrate_read.yaml"), strict=False),
        pg_pool=subj.w.store.pool, tool_context=ToolContext(substrate=_Port()),
        analyst_grants=grants, target_allows=[ActionPackRef(pack_id="substrate_read")],
        scope=GLOBAL_SCOPE, requested_by=who, budget_account=who,
    )
    binding = await build_pack_binding(substrate, W.CONSULT_WRITE_PACK_ID,
                                       registry_client=_Registry(rc), analyst=None)
    assert binding is not None
    yield subj, binding, typed_reads
    await http.aclose()


async def test_rename_and_revert_through_the_real_binding(srig):
    from tests.data_pkg.test_consult_write_gate import _gate, _ledger

    subj, binding, typed_reads = srig
    gate, rec = _gate(subj.w, binding)
    insp = {"tool": "inspect_object", "args": {"class": "subject", "id": subj.sid}}

    blind, meta = await gate.run("update_object", {
        "class": "subject", "id": subj.sid, "expected_version": "a" * 16,
        "patch": {"identity.name": "x"}, "reason": "r"}, 1)
    assert not meta["admitted"] and blind["error"].startswith("tool_blocked: ref_not_read: ")

    seen = await gate.run("inspect_object", insp["args"], 2)
    assert seen[1]["admitted"] and seen[0]["found"]
    gate.harvest(2, [insp], [seen])
    version = seen[0]["version"]
    bad, _meta = await gate.run("update_object", {
        "class": "subject", "id": subj.sid, "expected_version": version,
        "patch": {"scope.geo": ["brazil"]}, "reason": "r"}, 3)
    assert bad["error"].startswith("tool_blocked: preflight_failed: ")
    assert typed_reads == ["target"]  # the runtime preflight read the subject's typed head
    upd, meta = await gate.run("update_object", {
        "class": "subject", "id": subj.sid, "expected_version": version,
        "patch": {"identity.name": "Brazil Watch"}, "reason": "the operator renamed it"}, 3)
    assert meta["admitted"] and upd["status"] == "executed", upd
    assert (await subj.head()).name == "Brazil Watch"

    seen2 = await gate.run("inspect_object", insp["args"], 4)
    gate.harvest(4, [insp], [seen2])
    rev, _meta = await gate.run("revert_write", {"write_id": upd["write_id"], "reason": "undo"}, 5)
    assert rev["status"] == "executed", rev
    assert (await subj.head()).version == version

    inv, events = await _ledger(subj.w, gate.binding.requested_by)
    assert [t for t, _ in inv] == ["inspect_object", "update_object", "update_object", "inspect_object", "revert_write"]
    assert ("block", "ref_not_read", "update_object") in events
    receipt = (await subj.row(upd["write_id"]))["receipt"]
    assert receipt["invocation_id"] and receipt["class"] == "subject"
    assert gate.truth_line() == "writes: executed 2 · staged 0 · refused 2"
    assert [s["kind"] for s in rec.steps] == ["write_refused", "write_refused", "write_executed", "write_executed"]
