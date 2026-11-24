# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""C-2b-2/entities (W11) — the write plane's ENTITY record class on the REAL driver.

Design C-2a §1 row 5, §4, §10 for ``entity``, on the frame's ``world`` fixture
(real migrated Postgres, a real ``DescriptorRegistry`` with a real signing
identity and the seeded vocabulary — the ``entity_class`` family included —,
the real ``consult_write`` pack descriptor, real ``consult_sessions`` /
``consult_turns`` rows). Every value claimed persisted is read back with SQL:
the profile, its ``entity_profile_versions`` row and the provenance on it, the
``consult_writes`` ledger row, the signed audit row, the ``entity_judgement``
row. The last test runs every entity tool through the real
``AgencyToolBinding.run_tool`` and the runtime's thin client to the registry
over ASGI. Every profile minted here is named ``cw_entity_*`` and scrubbed by
the shared fixture's teardown (``_scrub_registered``).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport

from legba.data import consult_write_vocabulary as W
from legba.data.registry.audit import signed_payload_for_row
from legba.data.registry.consult_write_exec import CLASS_BRANCHES, ConsultWriteExecutor
from legba.data.registry.signing import verify_audit_payload
from tests.data_pkg.test_consult_write_frame import (  # noqa: F401 — the shared fixture + helpers
    World,
    _arm,
    _audit_row,
    _identity,
    _set_pack,
    world,
)

WORDS = "Merge Acme Corp into Acme, and rename Acme to its full name."
CLASSES = ["source", "subject", "entity"]


def _j(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


async def _mint(w: World, label: str, *, cls: str = "organization", aliases: list[str] | None = None,
                extra: dict[str, Any] | None = None) -> tuple[str, str]:
    """One profile the way the lifecycle writes it (version 1 + its created row)."""
    name = f"cw_entity_{uuid4().hex[:8]}_{label}"
    data = {"source": "entity_resolution", "merged_aliases": list(aliases or []), **(extra or {})}
    async with w.store.acquire() as conn:
        eid = await conn.fetchval(
            "INSERT INTO entity_profiles (canonical_name, entity_type, entity_class, data) "
            "VALUES ($1, $2, $2, $3::jsonb) RETURNING id", name, cls, json.dumps(data))
        await conn.execute(
            "INSERT INTO entity_profile_versions (entity_id, version, data) VALUES ($1, 1, $2::jsonb)",
            eid, json.dumps({"event": "created", "canonical_name": name, "entity_class": cls}))
    return str(eid), name


@dataclass
class Ent:
    w: World
    keeper: str
    keeper_name: str
    loser: str
    loser_name: str
    fact_id: str

    @property
    def ex(self) -> ConsultWriteExecutor:
        return self.w.ex

    async def inspect(self, ref: str) -> dict[str, Any]:
        return await self.ex.submit(self.w.armed, tool="inspect_object", args={"class": "entity", "id": ref})

    async def call(self, tool: str, **args: Any) -> dict[str, Any]:
        return await self.ex.submit(self.w.armed, tool=tool, args=args, turn_cost_usd=0.0)

    async def row(self, wid: str) -> dict[str, Any]:
        return await self.w.row(wid)

    async def profile(self, eid: str) -> dict[str, Any]:
        async with self.w.store.acquire() as conn:
            r = await conn.fetchrow("SELECT * FROM entity_profiles WHERE id = $1::uuid", eid)
        out = dict(r)
        out["data"] = _j(out["data"])
        return out

    async def versions(self, eid: str) -> list[dict[str, Any]]:
        async with self.w.store.acquire() as conn:
            rows = await conn.fetch(
                "SELECT version, data, analyst_id, run_id FROM entity_profile_versions "
                "WHERE entity_id = $1::uuid ORDER BY version", eid)
        return [{**dict(r), "data": _j(r["data"])} for r in rows]

    async def judgement(self) -> dict[str, Any] | None:
        lo, hi = sorted((self.keeper, self.loser))
        async with self.w.store.acquire() as conn:
            r = await conn.fetchrow("SELECT * FROM entity_judgement WHERE pair_key = $1", f"{lo}::{hi}")
        return dict(r) if r else None

    async def stage_and_confirm(self, tool: str, *, via: str = "click", **args: Any) -> dict[str, Any]:
        staged = await self.call(tool, **args)
        assert staged["status"] == "pending_confirmation", staged
        if via == "typed_code":
            code = self.ex._codes[staged["write_id"]]  # what the operator's card shows
            found = await self.ex.find_by_code(self.w.armed.session_id, code)
            assert found is not None and found["id"] == staged["write_id"]
            return await self.ex.confirm(staged["write_id"], via="typed_code", code=code)
        return await self.ex.confirm(staged["write_id"], via="click")


@pytest_asyncio.fixture
async def ent(world: World) -> Ent:
    await _set_pack(world.reg, classes=CLASSES)
    world.armed = await _arm(world.store, WORDS)
    keeper, kname = await _mint(world, "acme", aliases=["ACME Inc"])
    loser, lname = await _mint(world, "acme_corp", aliases=["Acme Corporation"], extra={"gc_status": "dormant"})
    async with world.store.acquire() as conn:
        fact = await conn.fetchval(
            "INSERT INTO facts (subject, predicate, value, confidence, source_type, schema_uri) "
            "VALUES ($1, 'cw_pred', 'x', 0.9, 'agent', 'iglu:legba/fact/jsonschema/1-0-0') RETURNING id", lname)
    return Ent(w=world, keeper=keeper, keeper_name=kname, loser=loser, loser_name=lname, fact_id=str(fact))


# ---------------------------------------------------------------------------
# inspect
# ---------------------------------------------------------------------------


async def test_inspect_returns_the_record_by_id_and_by_name(ent: Ent):
    seen = await ent.inspect(ent.loser)
    assert seen["found"] and seen["class"] == "entity" and seen["id"] == ent.loser
    assert seen["version"] == "1" and seen["state"] == "live" and seen["merged_into"] is None
    assert seen["fields"] == {"canonical_name": ent.loser_name, "entity_class": "organization",
                              "aliases": ["Acme Corporation"], "geo_country": None, "geo_region": None,
                              "geo_lat": None, "geo_lon": None}
    assert seen["open_facts"]["naming_this_name"] == 1 and seen["open_facts"]["as_of"]
    assert seen["writable"] == list(W.ENTITY_WRITABLE)
    assert {"organization", "person"} <= set(seen["entity_classes"])
    assert seen["versions"][0]["version"] == "1" and seen["writes"] == []
    by_name = await ent.inspect(ent.keeper_name)
    assert by_name["found"] and by_name["id"] == ent.keeper
    nobody = str(uuid4())
    assert (await ent.inspect(nobody)) == {"found": False, "class": "entity", "id": nobody}
    # Two live profiles share a name in different classes → ambiguous, inspect by id.
    async with ent.w.store.acquire() as conn:
        await conn.execute(
            "INSERT INTO entity_profiles (canonical_name, entity_type, entity_class, data) "
            "VALUES ($1, 'person', 'person', '{}'::jsonb)", ent.keeper_name)
    amb = await ent.inspect(ent.keeper_name)
    assert amb["found"] is False and {c["entity_class"] for c in amb["ambiguous"]} == {"organization", "person"}


# ---------------------------------------------------------------------------
# edit — Tier B, compare-and-set, provenance on the version row, receipt + audit
# ---------------------------------------------------------------------------


async def test_edit_stages_then_the_confirm_writes_a_version_with_provenance(ent: Ent):
    seen = await ent.inspect(ent.keeper)
    new_name = f"{ent.keeper_name}_holdings"
    staged = await ent.call("edit_entity", entity_id=ent.keeper, expected_version=seen["version"],
                            patch={"canonical_name": new_name, "aliases_add": ["Acme Group"],
                                   "geo_country": "US", "geo_lat": 40.7, "geo_lon": -74.0},
                            reason="the operator gave its full name")
    assert staged["status"] == "pending_confirmation" and staged["tier"] == "B", staged
    before = await ent.profile(ent.keeper)
    assert before["canonical_name"] == ent.keeper_name and before["version"] == 1  # nothing changed yet

    done = await ent.ex.confirm(staged["write_id"], via="click")
    assert done["status"] == "executed", done
    after = await ent.profile(ent.keeper)
    assert after["canonical_name"] == new_name and after["version"] == 2
    assert after["data"]["merged_aliases"] == ["ACME Inc", "Acme Group"]
    assert (after["geo_country"], after["geo_lat"], after["geo_lon"]) == ("US", 40.7, -74.0)
    assert after["data"]["source"] == "entity_resolution"  # the rest of data is untouched

    v2 = (await ent.versions(ent.keeper))[-1]
    assert v2["version"] == 2 and v2["data"]["event"] == "operator_edit"
    prov = v2["data"]["provenance"]
    assert prov["actor"] == f"consult_write:{staged['write_id']}" and prov["actor_role"] == "operator_via_consult"
    assert prov["author"] == "operator" and prov["origin_class"] == "live"
    assert prov["reason"] == "the operator gave its full name" and prov["at"] and prov["write_id"] == staged["write_id"]
    assert v2["analyst_id"] == prov["actor"] and str(v2["run_id"]) == staged["write_id"]
    assert v2["data"]["canonical_name"] == new_name and v2["data"]["merged_aliases"] == ["ACME Inc", "Acme Group"]

    row = await ent.row(staged["write_id"])
    assert (row["class"], row["verb"], row["tier"], row["status"]) == ("entity", "edit", "B", "executed")
    assert row["confirm_code_hash"] and row["confirmed_via"] == "click" and len(row["args_sha256"]) == 64
    assert row["pre_image"]["sha256"] and row["pre_image"]["version"] == "1"
    assert row["pre_image"]["fields"]["canonical_name"] == ent.keeper_name
    assert row["receipt"]["operator_words"] == WORDS and row["receipt"]["post"] == {"version": "2", "state": "live"}
    assert row["reversal"]["kind"] == "restore_fields"

    audit = await _audit_row(ent.w.store, row["audit_id"])
    assert (audit["namespace"], audit["descriptor_id"], audit["action"]) == ("entity", ent.keeper, "edit")
    assert (audit["from_version"], audit["to_version"]) == ("1", "2")
    assert audit["actor_id"] == f"consult_write:{staged['write_id']}" and audit["actor_role"] == "operator_via_consult"
    assert _j(audit["change_summary"])["receipt"]["write_id"] == staged["write_id"]
    assert verify_audit_payload(_identity().verify_key, signed_payload_for_row(audit), bytes(audit["signed_payload"]))


async def test_a_stale_version_is_refused_and_a_head_moved_before_confirm_too(ent: Ent):
    res = await ent.call("edit_entity", entity_id=ent.keeper, expected_version="7",
                         patch={"geo_country": "US"}, reason="r")
    assert res["status"] == "refused" and res["cause"] == "stale_read", res
    assert (await ent.row(res["write_id"]))["status"] == "refused"
    staged = await ent.call("edit_entity", entity_id=ent.keeper, expected_version="1",
                            patch={"geo_country": "US"}, reason="r")
    assert staged["status"] == "pending_confirmation"
    async with ent.w.store.acquire() as conn:  # the lifecycle folds an alias meanwhile (no version bump)
        await conn.execute(
            "UPDATE entity_profiles SET data = jsonb_set(data, '{merged_aliases}', '[\"ACME Inc\", \"A.C.M.E.\"]') "
            "WHERE id = $1::uuid", ent.keeper)
    moved = await ent.ex.confirm(staged["write_id"], via="click")
    assert moved["status"] == "refused" and moved["cause"] == "stale_read", moved
    assert (await ent.profile(ent.keeper))["geo_country"] is None


async def test_a_class_outside_the_family_is_refused_by_name_and_a_member_binds(ent: Ent):
    res = await ent.call("edit_entity", entity_id=ent.keeper, expected_version="1",
                         patch={"entity_class": "spaceship"}, reason="r")
    assert res["cause"] == "preflight_failed" and "entity_class family" in res["detail"], res
    for patch, cause in (({"derived_from": []}, "path_not_writable"),
                         ({"aliases_remove": ["never an alias"]}, "preflight_failed"),
                         ({"geo_lat": 91}, "preflight_failed"),
                         ({"aliases_add": ["ACME Inc"]}, "no_change")):
        out = await ent.call("edit_entity", entity_id=ent.keeper, expected_version="1", patch=patch, reason="r")
        assert out["cause"] == cause, (patch, out)
    done = await ent.stage_and_confirm("edit_entity", entity_id=ent.keeper, expected_version="1",
                                       patch={"entity_class": "person"}, reason="it is a person")
    assert done["status"] == "executed", done
    prof = await ent.profile(ent.keeper)
    assert prof["entity_class"] == "person" and prof["entity_type"] == "person"  # TAX-4: bound, never 'entity'
    assert any(c.get("family") == "entity_class" and c.get("value") == "person"
               for c in prof["data"]["classifications"]), prof["data"]["classifications"]


async def test_revert_of_an_edit_restores_the_prior_data_as_a_new_version(ent: Ent):
    pre = await ent.profile(ent.keeper)
    done = await ent.stage_and_confirm("edit_entity", entity_id=ent.keeper, expected_version="1",
                                       patch={"aliases_remove": ["ACME Inc"], "geo_region": "Northeast"},
                                       reason="r")
    assert done["status"] == "executed"
    listed = await ent.inspect(ent.keeper)
    assert [w["write_id"] for w in listed["writes"]] == [done["write_id"]]
    rev = await ent.call("revert_write", write_id=done["write_id"], reason="undo it")
    assert rev["status"] == "executed", rev  # Tier A: a revert of an edit restores the stored pre-image
    after = await ent.profile(ent.keeper)
    assert after["version"] == 3
    for key in ("canonical_name", "entity_class", "entity_type", "geo_country", "geo_region", "geo_lat", "geo_lon"):
        assert after[key] == pre[key], key
    assert after["data"] == pre["data"]
    assert (await ent.versions(ent.keeper))[-1]["data"]["event"] == "operator_revert"
    assert (await ent.row(done["write_id"]))["reversed_by_write_id"] == rev["write_id"]
    rrow = await ent.row(rev["write_id"])
    assert rrow["tier"] == "A" and rrow["verb"] == "revert" and rrow["confirm_code_hash"] is None
    again = await ent.ex.revert_button(done["write_id"])
    assert again["cause"] == "superseded_since"


async def test_revert_after_a_later_edit_is_superseded_since(ent: Ent):
    first = await ent.stage_and_confirm("edit_entity", entity_id=ent.keeper, expected_version="1",
                                        patch={"geo_country": "US"}, reason="r")
    second = await ent.stage_and_confirm("edit_entity", entity_id=ent.keeper, expected_version="2",
                                         patch={"geo_country": "CA"}, reason="r")
    assert first["status"] == second["status"] == "executed"
    res = await ent.ex.revert_button(first["write_id"])
    assert res["cause"] == "superseded_since" and second["write_id"] in res["detail"], res
    assert (await ent.profile(ent.keeper))["geo_country"] == "CA"


# ---------------------------------------------------------------------------
# merge / unmerge — the pre-image round trip, the distinct judgement, the seam
# ---------------------------------------------------------------------------


async def test_merge_then_unmerge_restores_the_pre_image_exactly(ent: Ent):
    k0, l0 = await ent.profile(ent.keeper), await ent.profile(ent.loser)
    merged = await ent.stage_and_confirm("merge_entities", keeper_id=ent.keeper, loser_id=ent.loser,
                                         reason="the same company")
    assert merged["status"] == "executed", merged
    k1, l1 = await ent.profile(ent.keeper), await ent.profile(ent.loser)
    assert str(l1["merged_into"]) == ent.keeper and l1["data"]["gc_status"] == "merged"
    assert l1["data"]["merge"]["edges"] == "deferred_to_compaction"
    assert l1["data"]["merge"]["added_aliases"] == [ent.loser_name, "Acme Corporation"]
    assert l1["data"]["merge"]["pre"] == {"gc_status": "dormant", "merge": None}
    assert k1["data"]["merged_aliases"] == ["ACME Inc", ent.loser_name, "Acme Corporation"]
    assert (k1["version"], l1["version"]) == (2, 2)
    row = await ent.row(merged["write_id"])
    assert (row["class"], row["verb"], row["object_ref"], row["tier"]) == ("entity", "merge", ent.loser, "B")
    pre = row["pre_image"]
    assert pre["sha256"] and pre["loser"]["gc_status"] == "dormant" and pre["keeper"]["version"] == "1"
    assert pre["facts"]["open_naming"] == 1 and pre["facts"]["ids"] == [ent.fact_id]  # compaction would fold it
    assert [v["data"]["event"] for v in await ent.versions(ent.loser)][-1] == "operator_merge_loser"
    # the fact still names the loser: compaction is the lifecycle's, not the merge's
    async with ent.w.store.acquire() as conn:
        assert await conn.fetchval("SELECT subject FROM facts WHERE id = $1::uuid", ent.fact_id) == ent.loser_name

    seen = await ent.inspect(ent.loser)
    assert seen["state"] == "merged" and seen["merge"]["unmerge"].startswith("possible")
    un = await ent.stage_and_confirm("unmerge_entity", via="typed_code", loser_id=ent.loser,
                                     reason="they are different companies")
    assert un["status"] == "executed", un
    k2, l2 = await ent.profile(ent.keeper), await ent.profile(ent.loser)
    assert l2["merged_into"] is None and l2["data"] == l0["data"]  # gc_status 'dormant' back, no merge block
    assert k2["data"] == k0["data"] and k2["canonical_name"] == k0["canonical_name"]
    assert (k2["version"], l2["version"]) == (3, 3)
    judged = await ent.judgement()
    assert judged["verdict"] == "not_same" and judged["decided_by"] == "human" and judged["confidence"] == 1.0
    assert judged["justification"].startswith(f"consult_write:{un['write_id']}")
    urow = await ent.row(un["write_id"])
    assert urow["confirmed_via"] == "typed_code" and urow["pre_image"]["judgement"] is None
    audit = await _audit_row(ent.w.store, urow["audit_id"])
    assert (audit["namespace"], audit["action"], audit["descriptor_id"]) == ("entity", "unmerge", ent.loser)

    # revert of the unmerge = the re-merge, from the stored pre-image (judgement back to none)
    redo = await ent.ex.revert_button(un["write_id"])
    assert redo["status"] == "executed", redo
    k3, l3 = await ent.profile(ent.keeper), await ent.profile(ent.loser)
    assert str(l3["merged_into"]) == ent.keeper and l3["data"]["merge"] == l1["data"]["merge"]
    assert k3["data"]["merged_aliases"] == k1["data"]["merged_aliases"]
    assert await ent.judgement() is None


async def test_revert_of_a_merge_is_the_unmerge(ent: Ent):
    k0, l0 = await ent.profile(ent.keeper), await ent.profile(ent.loser)
    merged = await ent.stage_and_confirm("merge_entities", keeper_id=ent.keeper, loser_id=ent.loser, reason="r")
    rev = await ent.ex.revert_button(merged["write_id"])
    assert rev["status"] == "executed", rev
    assert (await ent.profile(ent.loser))["data"] == l0["data"]
    assert (await ent.profile(ent.keeper))["data"] == k0["data"]
    assert (await ent.judgement())["verdict"] == "not_same"
    assert (await ent.row(merged["write_id"]))["reversed_by_write_id"] == rev["write_id"]


async def test_a_merge_after_a_later_keeper_change_cannot_be_reverted(ent: Ent):
    merged = await ent.stage_and_confirm("merge_entities", keeper_id=ent.keeper, loser_id=ent.loser, reason="r")
    edit = await ent.stage_and_confirm("edit_entity", entity_id=ent.keeper, expected_version="2",
                                       patch={"geo_country": "US"}, reason="r")
    assert edit["status"] == "executed"
    res = await ent.ex.revert_button(merged["write_id"])
    assert res["cause"] == "superseded_since", res
    assert str((await ent.profile(ent.loser))["merged_into"]) == ent.keeper


async def test_a_compacted_loser_is_refused_by_name_the_seam(ent: Ent):
    merged = await ent.stage_and_confirm("merge_entities", keeper_id=ent.keeper, loser_id=ent.loser, reason="r")
    assert merged["status"] == "executed"
    async with ent.w.store.acquire() as conn:  # the lifecycle compacts it (entity_gc's mark)
        await conn.execute(
            "UPDATE entity_profiles SET data = jsonb_set(data, '{merge,compacted_at}', '\"2026-10-05T00:00:00\"') "
            "WHERE id = $1::uuid", ent.loser)
    seen = await ent.inspect(ent.loser)
    assert seen["state"] == "merged_compacted" and "SEAMS #68" in seen["merge"]["unmerge"]
    other, _ = await _mint(ent.w, "third")
    for tool, args in (("unmerge_entity", {"loser_id": ent.loser}),
                       ("merge_entities", {"keeper_id": other, "loser_id": ent.loser})):
        res = await ent.call(tool, reason="r", **args)
        assert res["cause"] == "kind_not_built" and "SEAMS #68" in res["detail"], res
    res = await ent.ex.revert_button(merged["write_id"])
    assert res["cause"] == "kind_not_built" and "SEAMS #68" in res["detail"], res


async def test_merge_refusals_name_the_reason(ent: Ent):
    res = await ent.call("merge_entities", keeper_id=ent.keeper, loser_id=ent.keeper, reason="r")
    assert res["cause"] == "preflight_failed"
    res = await ent.call("merge_entities", keeper_id=ent.keeper, loser_id=str(uuid4()), reason="r")
    assert res["cause"] == "ref_not_read"
    # a researcher merge (merge_pair: no recorded pre-image, edges folded at merge time) is not unmergeable here
    lone, _ = await _mint(ent.w, "lone", extra={"gc_status": "merged", "merge": {"into": ent.keeper,
                                                                                 "decided_by": "llm"}})
    async with ent.w.store.acquire() as conn:
        await conn.execute("UPDATE entity_profiles SET merged_into = $2::uuid WHERE id = $1::uuid", lone, ent.keeper)
    res = await ent.call("unmerge_entity", loser_id=lone, reason="r")
    assert res["cause"] == "kind_not_built" and "decided_by llm" in res["detail"], res
    res = await ent.call("edit_entity", entity_id=lone, expected_version="1", patch={"geo_country": "US"},
                         reason="r")
    assert res["cause"] == "preflight_failed" and "merged into" in res["detail"]


async def test_compaction_folds_the_id_edges_a_consult_merge_deferred(ent: Ent):
    from legba.data.analysts.deterministic_handlers import entity_gc

    third, _ = await _mint(ent.w, "third")
    async with ent.w.store.acquire() as conn:
        edge = await conn.fetchval(
            "INSERT INTO entity_edges (src_id, dst_id, edge_type, edge_family) "
            "VALUES ($1::uuid, $2::uuid, 'cw_partner_of', 'relation') RETURNING id", ent.loser, third)
    merged = await ent.stage_and_confirm("merge_entities", keeper_id=ent.keeper, loser_id=ent.loser, reason="r")
    assert merged["status"] == "executed"
    async with ent.w.store.acquire() as conn:
        assert str(await conn.fetchval("SELECT src_id FROM entity_edges WHERE id = $1", edge)) == ent.loser
        assert entity_gc.MERGE_EDGES_DEFERRED == "deferred_to_compaction"
        touched = await entity_gc._fold_deferred_id_edges(conn, UUID(ent.loser))
        assert touched >= 1
        assert str(await conn.fetchval("SELECT src_id FROM entity_edges WHERE id = $1", edge)) == ent.keeper


# ---------------------------------------------------------------------------
# The journal accept of merge_entities writes through the apply layer or refuses
# ---------------------------------------------------------------------------


async def _proposal(conn: Any, diff: dict[str, Any]) -> UUID:
    return await conn.fetchval(
        "INSERT INTO journal_proposals (proposal_kind, proposed_by_analyst_id, run_id, rationale, diff, status) "
        "VALUES ('correction', 'journal_assessor', $1, 'cw_entity test', $2::jsonb, 'accepted') RETURNING id",
        uuid4(), json.dumps(diff))


async def test_the_journal_merge_writes_through_the_apply_layer_or_refuses_by_name(ent: Ent):
    from legba.data.registry.journal_proposals_apply import (
        CORRECT_SITUATION_REFUSAL,
        ProposalApplyError,
        apply_accepted_proposal,
    )

    deps = type("Deps", (), {"descriptor_registry": ent.w.reg})()
    async with ent.w.store.acquire() as conn:
        pids = []
        try:
            diff = {"op": "merge_entities", "from": ent.loser_name, "into": ent.keeper}
            pid = await _proposal(conn, diff)
            pids.append(pid)
            applied = await apply_accepted_proposal(conn, deps, proposal_id=pid, proposal_kind="correction",
                                                    diff=diff, actor="bearer:0123456789ab")
            assert applied["applied"] == "merged" and applied["loser_id"] == ent.loser, applied
            assert applied["open_facts_naming_loser"] == 1 and applied["edges"] == "deferred_to_compaction"
            loser = await ent.profile(ent.loser)
            assert str(loser["merged_into"]) == ent.keeper
            prov = (await ent.versions(ent.loser))[-1]["data"]["provenance"]
            assert prov["actor"] == f"journal_proposal:{pid}" and prov["principal"] == "bearer:0123456789ab"
            assert prov["actor_role"] == "operator_via_journal" and prov["author"] == "operator"
            audit = await conn.fetchrow(
                "SELECT * FROM descriptor_audit_log WHERE namespace = 'entity' AND descriptor_id = $1 "
                "AND action = 'merge' ORDER BY occurred_at DESC LIMIT 1", ent.loser)
            assert audit["actor_role"] == "operator_via_journal" and audit["actor_id"] == "bearer:0123456789ab"
            # the consult unmerge undoes a journal merge (its pre-image was recorded)
            seen = await ent.inspect(ent.loser)
            assert seen["merge"]["unmerge"].startswith("possible")

            for bad, words in (({"op": "merge_entities", "from": str(uuid4()), "into": ent.keeper},
                                "merge_entities refused: ref_not_read"),
                               ({"op": "merge_entities", "from": ent.loser, "into": ent.keeper},
                                "merge_entities refused: preflight_failed"),
                               ({"op": "correct_situation", "situation_id": str(uuid4()), "patch": {}},
                                CORRECT_SITUATION_REFUSAL)):
                pid = await _proposal(conn, bad)
                pids.append(pid)
                with pytest.raises(ProposalApplyError) as err:
                    await apply_accepted_proposal(conn, deps, proposal_id=pid, proposal_kind="correction",
                                                  diff=bad, actor="bearer:0123456789ab")
                assert words in str(err.value)
        finally:
            await conn.execute("DELETE FROM journal_proposals WHERE id = ANY($1::uuid[])", pids)


# ---------------------------------------------------------------------------
# The words, and every tool through the real binding
# ---------------------------------------------------------------------------


def test_the_entity_words():
    assert "entity" in W.BUILT_CLASSES
    assert {v for (c, v), e in W.VOCABULARY.items() if c == "entity" and e == "built"} == CLASS_BRANCHES[
        "entity"].VERBS == {"inspect", "edit", "merge", "unmerge", "revert"}
    assert {t: c for t, c in W.TOOL_CLASS.items() if c == "entity"} == {
        "edit_entity": "entity", "merge_entities": "entity", "unmerge_entity": "entity"}
    assert "entity" in W.TOOL_SCHEMAS["inspect_object"][1]["properties"]["class"]["enum"]
    for head in ("create_object", "update_object", "transition_object"):
        assert "entity" not in W.TOOL_SCHEMAS[head][1]["properties"]["class"]["enum"], head
    for tool in ("edit_entity", "merge_entities", "unmerge_entity"):
        assert W.TOOL_SCHEMAS[tool][1]["additionalProperties"] is False
    assert [W.entity_tier(v) for v in ("edit", "merge", "unmerge")] == ["B", "B", "B"]
    assert W.entity_tier("revert", reverses="edit") == "A" and W.entity_tier("revert", reverses="merge") == "B"


@pytest_asyncio.fixture
async def erig(ent: Ent, monkeypatch):
    from legba.data.analysts.agency import Agency, AgencyToolBinding, GLOBAL_SCOPE, ToolContext
    from legba.data.registry import consult_writes_api as front_mod
    from legba.data.schemas.action_pack import ActionPack, ActionPackRef
    from legba.runtime.consult_bindings import build_pack_binding
    from legba.runtime.registry_client import RegistryHTTPClient
    from tests.data_pkg.test_consult_write_gate import _body, _Port, _Registry

    monkeypatch.delenv("LEGBA_REGISTRY_API_TOKEN", raising=False)
    front = front_mod.WritesFront(
        type("Deps", (), {"descriptor_registry": ent.w.reg})(), is_live=lambda rid: True, executor=ent.ex,
    )
    front.armed[ent.w.armed.request_id] = ent.w.armed
    app = FastAPI()
    app.include_router(front_mod.build_router(front), prefix="/api/v1")
    http = httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://registry")
    rc = RegistryHTTPClient(base_url="http://registry", token="", client=http)
    who = f"analyst::cw_{uuid4().hex[:8]}"
    grants = [ActionPackRef(pack_id="substrate_read"), ActionPackRef(pack_id=W.CONSULT_WRITE_PACK_ID)]
    substrate = AgencyToolBinding(
        agency=Agency(),
        pack=ActionPack.model_validate(_body("action_pack_substrate_read.yaml"), strict=False),
        pg_pool=ent.w.store.pool, tool_context=ToolContext(substrate=_Port()),
        analyst_grants=grants, target_allows=[ActionPackRef(pack_id="substrate_read")],
        scope=GLOBAL_SCOPE, requested_by=who, budget_account=who,
    )
    binding = await build_pack_binding(substrate, W.CONSULT_WRITE_PACK_ID,
                                       registry_client=_Registry(rc), analyst=None)
    assert binding is not None
    yield ent, binding
    await http.aclose()


async def test_every_entity_tool_through_the_real_binding(erig):
    from tests.data_pkg.test_consult_write_gate import _gate, _ledger

    ent, binding = erig
    gate, rec = _gate(ent.w, binding)

    blind, meta = await gate.run("edit_entity", {"entity_id": ent.keeper, "expected_version": "1",
                                                 "patch": {"geo_country": "US"}, "reason": "r"}, 1)
    assert not meta["admitted"] and blind["error"].startswith("tool_blocked: ref_not_read: ")
    blind, _meta = await gate.run("merge_entities", {"keeper_id": ent.keeper, "loser_id": ent.loser,
                                                     "reason": "r"}, 1)
    assert blind["error"].startswith("tool_blocked: ref_not_read: ")

    calls = [{"tool": "inspect_object", "args": {"class": "entity", "id": i}} for i in (ent.keeper, ent.loser)]
    seen = [await gate.run("inspect_object", c["args"], 2) for c in calls]
    assert all(s[1]["admitted"] and s[0]["found"] for s in seen)
    gate.harvest(2, calls, seen)

    edit, meta = await gate.run("edit_entity", {"entity_id": ent.keeper, "expected_version": "1",
                                                "patch": {"geo_country": "US"}, "reason": "r"}, 3)
    assert meta["admitted"] and edit.get("write_staged"), edit
    assert (await ent.profile(ent.keeper))["geo_country"] is None  # staged: nothing changed
    assert (await ent.ex.confirm(edit["write_staged"], via="click"))["status"] == "executed"
    assert (await ent.profile(ent.keeper))["geo_country"] == "US"

    merge, meta = await gate.run("merge_entities", {"keeper_id": ent.keeper, "loser_id": ent.loser,
                                                    "reason": "same company"}, 3)
    assert meta["admitted"] and merge.get("write_staged"), merge
    assert (await ent.ex.confirm(merge["write_staged"], via="click"))["status"] == "executed"
    assert str((await ent.profile(ent.loser))["merged_into"]) == ent.keeper

    again = [{"tool": "inspect_object", "args": {"class": "entity", "id": ent.loser}}]
    seen2 = [await gate.run("inspect_object", again[0]["args"], 4)]
    gate.harvest(4, again, seen2)
    un, meta = await gate.run("unmerge_entity", {"loser_id": ent.loser, "reason": "not the same"}, 5)
    assert meta["admitted"] and un.get("write_staged"), un
    assert (await ent.ex.confirm(un["write_staged"], via="click"))["status"] == "executed"
    assert (await ent.profile(ent.loser))["merged_into"] is None

    inv, events = await _ledger(ent.w, gate.binding.requested_by)
    assert [t for t, _ in inv] == ["inspect_object", "inspect_object", "edit_entity", "merge_entities",
                                   "inspect_object", "unmerge_entity"]
    assert ("block", "ref_not_read", "edit_entity") in events and ("block", "ref_not_read", "merge_entities") in events
    for wid in (edit["write_staged"], merge["write_staged"], un["write_staged"]):
        row = await ent.row(wid)
        assert row["class"] == "entity" and row["invocation_id"] and row["pre_image"]["sha256"]
        assert row["confirm_code_hash"] and row["receipt"]["operator_words"] == WORDS
    assert gate.truth_line() == "writes: executed 0 · staged 3 · refused 2"
    assert [s["kind"] for s in rec.steps] == ["write_refused", "write_refused", "write_staged", "write_staged",
                                              "write_staged"]
