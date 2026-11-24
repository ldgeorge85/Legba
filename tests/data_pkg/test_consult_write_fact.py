# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""W11 C-2b-2/facts — the write plane's FACT record class on the REAL driver.

Design C-2a §1 row 6, §4 and §10 for ``fact``, on the frame's fixture: real
migrated Postgres, a real ``DescriptorRegistry`` with a real signing identity,
the real ``consult_write`` pack descriptor, real ``consult_sessions`` /
``consult_turns`` rows, real ``facts`` rows. Every value claimed persisted is
read back with SQL; every reversal is proven on the stored pre-image (the
pair's open set equals it again). The last test runs the tools through the
real ``AgencyToolBinding.run_tool`` and the runtime's thin client to the
registry over ASGI.

Every fact row and vocabulary family this file mints carries the ``cw_fact``
predicate / family prefix, which the shared ``world`` teardown deletes.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio

from legba.data import consult_write_vocabulary as W
from legba.data.provenance import writes_supersession as S
from legba.data.provenance.writes import supersede_prior_facts
from legba.data.registry.audit import signed_payload_for_row
from legba.data.registry.consult_write_exec import CLASS_BRANCHES, ConsultWriteExecutor
from legba.data.registry.signing import verify_audit_payload
from tests.data_pkg.test_consult_write_gate import rig  # noqa: F401 — the binding rig (shares ``world``)
from tests.data_pkg.test_consult_write_frame import (  # noqa: F401 — the shared fixture + helpers
    World,
    _arm,
    _audit_row,
    _identity,
    _set_pack,
    world,
)

WORDS = "Record that the subject's leader is now Jane Roe; the old value is wrong."
CLASSES = ["source", "fact"]


@dataclass
class FW:
    """The frame's world, re-armed with a fact instruction, and one fresh pair."""

    w: World
    subject: str
    predicate: str

    @property
    def ex(self) -> ConsultWriteExecutor:
        return self.w.ex

    @property
    def ref(self) -> str:
        return W.fact_ref(self.subject, self.predicate)

    async def inspect(self, ref: str | None = None) -> dict[str, Any]:
        return await self.ex.submit(self.w.armed, tool="inspect_object",
                                    args={"class": "fact", "id": ref or self.ref})

    async def call(self, tool: str, **args: Any) -> dict[str, Any]:
        return await self.ex.submit(self.w.armed, tool=tool, args=args, turn_cost_usd=0.0)

    async def assert_(self, value: str, supersede: Any = "none", **extra: Any) -> dict[str, Any]:
        return await self.call("assert_fact", subject=self.subject, predicate=self.predicate,
                               value=value, supersede=supersede, reason="the operator said so", **extra)

    async def fact(self, value: str, source_type: str = "ingestion", *, data: Any = None,
                   subject: str | None = None, predicate: str | None = None) -> str:
        """One OPEN facts row on the pair (or another), as a producer left it."""
        fid = uuid4()
        async with self.w.store.acquire() as conn:
            await conn.execute(
                "INSERT INTO facts (id, subject, predicate, value, confidence, source_type, "
                "produced_at, derived_from, schema_uri, data, origin_class) "
                "VALUES ($1, $2, $3, $4, 0.7, $5, now(), '{}', 'iglu:legba/fact/jsonschema/2-0-0', "
                "$6::jsonb, 'live')",
                fid, subject or self.subject, predicate or self.predicate, value, source_type,
                None if data is None else json.dumps(data),
            )
        return str(fid)

    async def row(self, fid: str) -> dict[str, Any]:
        async with self.w.store.acquire() as conn:
            r = await conn.fetchrow("SELECT * FROM facts WHERE id = $1::uuid", fid)
        assert r is not None
        out = dict(r)
        if isinstance(out.get("data"), str):
            out["data"] = json.loads(out["data"])
        return out

    async def open_ids(self) -> set[str]:
        async with self.w.store.acquire() as conn:
            rows = await S.read_open_fact_set(conn, subject=self.subject, predicate=self.predicate)
        return {str(r["id"]) for r in rows}

    async def write(self, wid: str) -> dict[str, Any]:
        return await self.w.row(wid)


@pytest_asyncio.fixture
async def fw(world: World) -> FW:
    await _set_pack(world.reg, classes=CLASSES)
    world.armed = await _arm(world.store, WORDS)
    tag = uuid4().hex[:10]
    return FW(w=world, subject=f"CW Fact Subject {tag}", predicate=f"cw_fact_{tag}")


async def _new_id(fw: FW, res: dict[str, Any]) -> str:
    return (await fw.write(res["write_id"]))["reversal"]["fact_id"]


# ---------------------------------------------------------------------------
# 1. inspect — the open rows with provenance, tier, window, chain
# ---------------------------------------------------------------------------


async def test_inspect_shows_the_open_set_its_provenance_and_the_chain(fw: FW):
    old = await fw.fact("Old Leader", "ingestion")
    seed = await fw.fact("Seeded Leader", "seed")
    mid = await fw.fact("Mid Leader", "agent")
    async with fw.w.store.acquire() as conn:  # mid supersedes old, as the producers do
        await conn.execute(
            "UPDATE facts SET valid_until = now(), superseded_by = $2::uuid, superseded_at = now() "
            "WHERE id = $1::uuid", old, mid)
    seen = await fw.inspect()
    assert seen["found"] and seen["class"] == "fact" and seen["id"] == fw.ref
    assert seen["predicate"] == fw.predicate and seen["state"] == "2 open"
    assert seen["version"] == S.open_set_digest([seed, mid])
    by_id = {r["id"]: r for r in seen["open"]}
    assert set(by_id) == {seed, mid}
    assert by_id[seed]["tier"] == 2 and by_id[seed]["authority"] == "authoritative"
    assert by_id[mid]["tier"] == 1 and by_id[mid]["authority"] == "machine"
    assert by_id[mid]["origin_class"] == "live" and by_id[mid]["recorded_at"]
    assert [s["id"] for s in by_id[mid]["supersedes"]] == [old]
    assert [c["id"] for c in seen["recent_closed"]] == [old]
    # by a fact id: the same pair, the id as focus
    focused = await fw.inspect(seed)
    assert focused["id"] == fw.ref and focused["focus"] == seed
    assert (await fw.inspect("no pair here"))["found"] is False
    assert (await fw.inspect(str(uuid4())))["found"] is False


# ---------------------------------------------------------------------------
# 2. assert — none / differing / ids; the receipt, the audit row, the revert
# ---------------------------------------------------------------------------


async def test_assert_none_writes_an_operator_row_closes_nothing_and_reverts(fw: FW):
    old = await fw.fact("Old Leader", "ingestion")
    res = await fw.assert_("Jane Roe", "none")
    assert res["status"] == "executed", res
    new = await _new_id(fw, res)
    row = await fw.row(new)
    assert row["source_type"] == "operator" and row["origin_class"] == "live"
    assert row["confidence"] == pytest.approx(1.0) and row["valid_until"] is None
    assert row["source_credibility"] == pytest.approx(0.9)
    assert row["data"]["write_id"] == res["write_id"]
    assert row["data"]["asserted_by"] == f"consult_write:{res['write_id']}"
    assert row["data"]["classifications"] == [
        {"family": "src", "value": fw.predicate, "status": "legacy_src"}]
    assert await fw.open_ids() == {old, new}  # none closed nothing

    led = await fw.write(res["write_id"])
    assert led["class"] == "fact" and led["verb"] == "assert" and led["object_ref"] == fw.ref
    assert led["tier"] == "A" and led["status"] == "executed" and led["args_sha256"]
    assert led["pre_image"]["closes"] == [] and led["pre_image"]["version"] == S.open_set_digest([old])
    assert led["post_image"] == {"version": S.open_set_digest([old, new]), "state": "2 open"}
    assert led["receipt"]["operator_words"] == WORDS and led["user_turn_id"] == fw.w.armed.user_turn_id
    assert led["reversal"] == {"kind": "close_and_reopen", "fact_id": new, "reopen": []}
    audit = await _audit_row(fw.w.store, led["audit_id"])
    assert audit["namespace"] == "fact" and audit["descriptor_id"] == fw.ref and audit["action"] == "assert"
    assert audit["actor_id"] == f"consult_write:{res['write_id']}"
    assert audit["actor_role"] == "operator_via_consult"
    assert verify_audit_payload(_identity().verify_key, signed_payload_for_row(audit), bytes(audit["signed_payload"]))

    rev = await fw.ex.revert_button(res["write_id"])
    assert rev["status"] == "executed", rev
    assert await fw.open_ids() == {old}  # the stored pre-image
    gone = await fw.row(new)
    assert gone["valid_until"] is not None and gone["superseded_by"] is None
    assert gone["data"]["closed_by"] == f"consult_write:{rev['write_id']}"
    assert (await fw.write(res["write_id"]))["reversed_by_write_id"] == rev["write_id"]
    rev_audit = await _audit_row(fw.w.store, (await fw.write(rev["write_id"]))["audit_id"])
    assert rev_audit["action"] == "revert" and rev_audit["namespace"] == "fact"


async def test_differing_closes_machine_rows_but_never_a_seed_row_and_reverts_exactly(fw: FW):
    machine = await fw.fact("Old Leader", "ingestion")
    seed = await fw.fact("Seeded Leader", "seed")
    res = await fw.assert_("Jane Roe", "differing")
    assert res["status"] == "executed" and (await fw.write(res["write_id"]))["tier"] == "A", res
    new = await _new_id(fw, res)
    assert await fw.open_ids() == {seed, new}  # the seed row stands: differing never closes curated
    closed = await fw.row(machine)
    assert str(closed["superseded_by"]) == new and closed["valid_until"] is not None
    assert closed["superseded_at"] is not None
    assert (await fw.write(res["write_id"]))["pre_image"]["closes"] == [machine]

    rev = await fw.ex.revert_button(res["write_id"])
    assert rev["status"] == "executed", rev
    assert await fw.open_ids() == {machine, seed}
    back = await fw.row(machine)
    assert back["valid_until"] is None and back["superseded_by"] is None and back["superseded_at"] is None


async def test_differing_over_two_rows_stages_and_the_click_executes(fw: FW):
    a = await fw.fact("Old A", "ingestion")
    b = await fw.fact("Old B", "agent")
    prior = await fw.fact("Prior operator word", "operator")
    res = await fw.assert_("Jane Roe", "differing")
    assert res["status"] == "pending_confirmation" and res["tier"] == "B", res
    assert "closes 3" in res["card"] and "Jane Roe" in res["card"]
    assert await fw.open_ids() == {a, b, prior}  # nothing changed
    done = await fw.ex.confirm(res["write_id"], via="click")
    assert done["status"] == "executed", done
    new = await _new_id(fw, res)
    assert await fw.open_ids() == {new}
    led = await fw.write(res["write_id"])
    assert sorted(led["pre_image"]["closes"]) == sorted([a, b, prior]) and led["confirmed_via"] == "click"
    assert led["confirm_code_hash"]  # the staged row carried its hashed code
    rev = await fw.ex.revert_button(res["write_id"])
    assert rev["status"] == "executed", rev
    assert await fw.open_ids() == {a, b, prior}


async def test_a_moved_open_set_before_confirm_is_stale_read(fw: FW):
    await fw.fact("Old A", "ingestion")
    await fw.fact("Old B", "ingestion")
    res = await fw.assert_("Jane Roe", "differing")
    assert res["status"] == "pending_confirmation"
    before = await fw.open_ids()
    late = await fw.fact("Late arrival", "ingestion")
    out = await fw.ex.confirm(res["write_id"], via="click")
    assert out["status"] == "refused" and out["cause"] == "stale_read", out
    assert await fw.open_ids() == before | {late}


async def test_ids_closes_a_seed_row_by_name_and_refuses_an_id_outside_the_pair(fw: FW):
    seed = await fw.fact("Seeded Leader", "seed")
    elsewhere = await fw.fact("x", "ingestion", subject=f"{fw.subject} other")
    out = await fw.assert_("Jane Roe", [elsewhere])
    assert out["status"] == "refused" and out["cause"] == "stale_read" and elsewhere in out["detail"], out
    out = await fw.assert_("Jane Roe", [str(uuid4())])
    assert out["cause"] == "stale_read"
    res = await fw.assert_("Jane Roe", [seed])
    assert res["status"] == "executed", res
    new = await _new_id(fw, res)
    assert await fw.open_ids() == {new} and str((await fw.row(seed))["superseded_by"]) == new
    assert (await fw.assert_("x", "sometimes"))["cause"] == "preflight_failed"


async def test_a_same_value_machine_row_is_replaced_and_an_authoritative_one_is_no_change(fw: FW):
    same = await fw.fact("Jane Roe", "ingestion")
    res = await fw.assert_("jane roe", "none")
    assert res["status"] == "executed", res
    new = await _new_id(fw, res)
    assert await fw.open_ids() == {new} and str((await fw.row(same))["superseded_by"]) == new
    again = await fw.assert_("Jane Roe", "none")
    assert again["status"] == "refused" and again["cause"] == "no_change" and new in again["detail"]


async def test_an_ingestion_fact_cannot_close_the_operators_fact(fw: FW):
    res = await fw.assert_("Jane Roe", "none")
    new = await _new_id(fw, res)
    async with fw.w.store.acquire() as conn:
        closed = await supersede_prior_facts(
            conn, subject=fw.subject, predicate=fw.predicate, value="Someone Else",
            new_fact_id=uuid4(), incoming_source_type="ingestion", returning_ids=True,
        )
    assert closed == 0 and closed.ids == ()
    assert new in await fw.open_ids()


# ---------------------------------------------------------------------------
# 3. close — and its revert; superseded_since when a later write intervenes
# ---------------------------------------------------------------------------


async def test_close_then_revert_restores_the_row_exactly(fw: FW):
    target = await fw.fact("Wrong Leader", "ingestion")  # data NULL
    seen = await fw.inspect()
    assert target in {r["id"] for r in seen["open"]}
    res = await fw.call("close_fact", fact_id=target, reason="it is wrong")
    assert res["status"] == "executed", res
    row = await fw.row(target)
    assert row["valid_until"] is not None and row["superseded_by"] is None
    assert row["data"] == {"closed_by": f"consult_write:{res['write_id']}", "closed_reason": "it is wrong"}
    led = await fw.write(res["write_id"])
    assert led["verb"] == "close" and led["object_ref"] == fw.ref and led["pre_image"]["closes"] == [target]
    assert (await fw.call("close_fact", fact_id=target, reason="again"))["cause"] == "stale_read"

    rev = await fw.ex.revert_button(res["write_id"])
    assert rev["status"] == "executed", rev
    back = await fw.row(target)
    assert back["valid_until"] is None and back["data"] is None  # the pre-image, data NULL again
    assert await fw.open_ids() == {target}


async def test_close_revert_is_refused_once_another_row_holds_the_triple(fw: FW):
    target = await fw.fact("Wrong Leader", "agent", data={"note": "kept"})
    res = await fw.call("close_fact", fact_id=target, reason="r")
    assert res["status"] == "executed"
    twin = await fw.assert_("Wrong Leader", "none")
    assert twin["status"] == "executed"
    out = await fw.ex.revert_button(res["write_id"])
    assert out["status"] == "refused" and out["cause"] == "superseded_since", out
    assert (await fw.row(target))["valid_until"] is not None
    assert (await fw.row(target))["data"]["note"] == "kept"


async def test_a_later_write_blocks_the_earlier_revert_until_it_is_reverted(fw: FW):
    machine = await fw.fact("Old Leader", "ingestion")
    first = await fw.assert_("Jane Roe", "differing")
    first_id = await _new_id(fw, first)
    second = await fw.assert_("John Doe", "differing")  # closes the operator's earlier word
    second_id = await _new_id(fw, second)
    assert await fw.open_ids() == {second_id}
    out = await fw.ex.revert_button(first["write_id"])
    assert out["status"] == "refused" and out["cause"] == "superseded_since", out
    assert second["write_id"] in out["detail"]
    assert await fw.open_ids() == {second_id}  # nothing changed
    assert (await fw.ex.revert_button(second["write_id"]))["status"] == "executed"
    assert await fw.open_ids() == {first_id}
    assert (await fw.ex.revert_button(first["write_id"]))["status"] == "executed"
    assert await fw.open_ids() == {machine}


# ---------------------------------------------------------------------------
# 4. the operator's fact GROUNDS; the predicate binds where a family exists
# ---------------------------------------------------------------------------


async def test_the_operators_fact_reaches_the_grounding_read(fw: FW, monkeypatch):
    from legba.runtime.grounding import SubstrateGroundingResolver

    monkeypatch.delenv("LEGBA_GROUNDING_TRUSTED_SOURCE_TYPES", raising=False)
    await fw.fact("A proposal", "proposed")
    res = await fw.assert_("Jane Roe", "none")
    assert res["status"] == "executed"
    facts, _ = await SubstrateGroundingResolver(pg_pool=fw.w.store.pool).resolve([fw.subject], max_facts=10)
    got = {(f.value, f.source_type) for f in facts}
    assert ("Jane Roe", "operator") in got
    assert not any(v == "A proposal" for v, _ in got)  # a proposed fact never grounds


async def test_a_registered_predicate_family_binds_and_an_unregistered_one_never_blocks(fw: FW):
    family = f"cw_fact.ax{uuid4().hex[:6]}"
    async with fw.w.store.acquire() as conn:
        await conn.execute(
            "INSERT INTO vocabulary_entries (id, family, value, schema_uri, introduced, aliases) "
            "VALUES ($1, $2, 'leads', 'legba/vocabulary/1.0.0', now(), ARRAY['heads'])",
            uuid4(), family)
    bound = await fw.call("assert_fact", subject=fw.subject, predicate=f"{family}:heads", value="Jane Roe",
                          supersede="none", reason="r")
    assert bound["status"] == "executed", bound
    row = await fw.row(await _new_id(fw, bound))
    assert row["data"]["classifications"] == [{"family": family, "value": "leads", "status": "bound"}]
    loose = await fw.call("assert_fact", subject=fw.subject, predicate="cw_fact.nope:x", value="v",
                          supersede="none", reason="r")
    assert loose["status"] == "executed", loose
    row = await fw.row(await _new_id(fw, loose))
    assert row["data"]["classifications"][0]["status"] == "unclassified"


# ---------------------------------------------------------------------------
# 5. supersede_prior_facts returns the IDS it closed (the count stays an int)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("contention", ["0", "1"])
async def test_supersede_prior_facts_returns_the_ids_it_closed(fw: FW, monkeypatch, contention):
    monkeypatch.setenv("LEGBA_FACT_CONTENTION", contention)
    a = await fw.fact("Alpha Value", "ingestion")
    b = await fw.fact("Zulu Other", "agent")
    async with fw.w.store.acquire() as conn:
        closed = await supersede_prior_facts(
            conn, subject=fw.subject, predicate=fw.predicate, value="Something Else Entirely",
            new_fact_id=uuid4(), incoming_source_type="seed", returning_ids=True,
        )
    assert isinstance(closed, int) and closed == 2
    assert sorted(str(i) for i in closed.ids) == sorted([a, b])
    c = await fw.fact("Charlie", "ingestion")
    async with fw.w.store.acquire() as conn:
        counted = await supersede_prior_facts(
            conn, subject=fw.subject, predicate=fw.predicate, value="Delta",
            new_fact_id=uuid4(), incoming_source_type="seed",
        )
    assert counted == 1 and counted.ids is None and isinstance(counted, S.ClosedFacts)
    assert c not in await fw.open_ids()


# ---------------------------------------------------------------------------
# 6. the words; the per-class switch
# ---------------------------------------------------------------------------


def test_the_fact_words():
    assert "fact" in W.BUILT_CLASSES
    built = {v for (c, v), e in W.VOCABULARY.items() if c == "fact" and e == "built"}
    assert built == CLASS_BRANCHES["fact"].VERBS == {"inspect", "assert", "close", "revert"}
    assert W.seam_for("entity", "edit") is None and W.seam_for("fact", "assert") is None  # SEAMS #65 resolved
    assert {t: c for t, c in W.TOOL_CLASS.items() if c == "fact"} == {"assert_fact": "fact", "close_fact": "fact"}
    assert W.TOOL_VERB["assert_fact"] == "assert" and W.TOOL_VERB["close_fact"] == "close"
    schema = W.TOOL_SCHEMAS["assert_fact"][1]
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {"subject", "predicate", "value", "supersede", "reason"}
    assert W.fact_tier(1) == "A" and W.fact_tier(2) == "B" and W.fact_tier(0) == "A"
    assert W.parse_fact_ref("Jane | leader | of") == ("Jane | leader", "of")
    assert W.parse_fact_ref("no separator") is None
    assert W.fact_ref(" Iran ", "Leader Of") == "iran | leader of"


async def test_dropping_fact_from_the_pack_stops_fact_writes_only(fw: FW):
    await _set_pack(fw.w.reg, classes=["source"])
    out = await fw.assert_("Jane Roe")
    assert out["status"] == "refused" and out["cause"] == "class_disabled"
    assert await fw.open_ids() == set()
    await _set_pack(fw.w.reg, classes=CLASSES)
    assert (await fw.assert_("Jane Roe"))["status"] == "executed"


# ---------------------------------------------------------------------------
# 7. the binding path: the real AgencyToolBinding → thin client →
#    POST /consult/writes → the executor; the read set holds fact ids
# ---------------------------------------------------------------------------


async def test_the_fact_tools_through_the_real_binding(rig):
    from tests.data_pkg.test_consult_write_gate import _gate, _ledger

    world, _front, _substrate, binding = rig
    await _set_pack(world.reg, classes=CLASSES)
    tag = uuid4().hex[:10]
    f = FW(w=world, subject=f"CW Fact Subject {tag}", predicate=f"cw_fact_{tag}")
    seed = await f.fact("Seeded Leader", "seed")
    gate, rec = _gate(world, binding)

    blind, meta = await gate.run("close_fact", {"fact_id": seed, "reason": "r"}, 1)
    assert not meta["admitted"] and blind["error"].startswith("tool_blocked: ref_not_read: ")
    assert seed in blind["error"]
    blind2, _meta = await gate.run("assert_fact", {
        "subject": f.subject, "predicate": f.predicate, "value": "Jane Roe",
        "supersede": [seed], "reason": "r"}, 1)
    assert blind2["error"].startswith("tool_blocked: ref_not_read: ") and seed in blind2["error"]

    insp = {"tool": "inspect_object", "args": {"class": "fact", "id": f.ref}}
    seen = await gate.run("inspect_object", insp["args"], 2)
    assert seen[1]["admitted"] and seen[0]["found"] and seen[0]["open"][0]["id"] == seed
    gate.harvest(2, [insp], [seen])
    done, meta = await gate.run("assert_fact", {
        "subject": f.subject, "predicate": f.predicate, "value": "Jane Roe",
        "supersede": [seed], "confidence": 0.95, "reason": "the operator named it"}, 3)
    assert meta["admitted"] and done["status"] == "executed", done
    new = await _new_id(f, done)
    assert await f.open_ids() == {new}
    assert (await f.row(new))["confidence"] == pytest.approx(0.95)

    seen2 = await gate.run("inspect_object", insp["args"], 4)
    gate.harvest(4, [insp], [seen2])
    closed, _meta = await gate.run("close_fact", {"fact_id": new, "reason": "on reflection"}, 5)
    assert closed["status"] == "executed", closed
    seen3 = await gate.run("inspect_object", insp["args"], 6)
    gate.harvest(6, [insp], [seen3])
    rev, _meta = await gate.run("revert_write", {"write_id": closed["write_id"], "reason": "undo"}, 7)
    assert rev["status"] == "executed", rev
    assert await f.open_ids() == {new}

    inv, events = await _ledger(world, gate.binding.requested_by)
    assert [t for t, _ in inv] == [
        "inspect_object", "assert_fact", "inspect_object", "close_fact", "inspect_object", "revert_write"]
    assert ("block", "ref_not_read", "close_fact") in events
    assert ("block", "ref_not_read", "assert_fact") in events
    for wid in (done["write_id"], closed["write_id"], rev["write_id"]):
        led = await f.write(wid)
        assert led["class"] == "fact" and led["verb"] in {"assert", "close", "revert"}
        assert led["pre_image"]["version"] and led["args_sha256"] and led["audit_id"]
        assert led["operator_words"] and led["receipt"]["invocation_id"]
    assert gate.truth_line() == "writes: executed 3 · staged 0 · refused 2"
    assert UUID(new)

