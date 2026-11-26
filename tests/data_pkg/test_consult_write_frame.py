# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""C-2b-1/frame — the consult write EXECUTOR, its routes and the front door, on the REAL driver.

Design C-2a §10 items 1–8 for the ``source`` class. Real migrated Postgres
(migration 0227), a real ``DescriptorRegistry`` with a real signing identity,
the real ``consult_write`` pack descriptor from ``descriptors/``, real
``consult_sessions`` / ``consult_turns`` rows. Every value claimed persisted is
read back with SQL. The Dapr sidecar is the one boundary double (it is never
supposed to be reached by a confirm).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio
import yaml
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from nacl.signing import SigningKey

from legba.data import consult_write_vocabulary as W
from legba.data.config import PostgresConfig
from legba.data.postgres import PostgresStore
from legba.data.registry import consult_persistence
from legba.data.registry.audit import signed_payload_for_row
from legba.data.registry.consult_write_exec import Armed, ConsultWriteExecutor, executor_for
from legba.data.registry.descriptor import DescriptorRegistry, Family
from legba.data.registry.errors import DescriptorNotFound
from legba.data.registry.signing import SigningIdentity, verify_audit_payload
from legba.data.schemas.action_pack import ActionPack
from legba.data.schemas.source import SourceDescriptor

_ROOT = Path(__file__).resolve().parents[2]
_DESCRIPTORS = _ROOT / "descriptors"
_MIGRATION = _ROOT / "src" / "legba" / "data" / "migrations" / "0227_consult_writes.sql"
WORDS = "Slow the 38 North feed down to a ten-minute cooldown, and pause it."


def _identity() -> SigningIdentity:
    return SigningIdentity(
        signing_key=SigningKey(b"c2b1-frame-test-seed-0123456789ab"[:32]),
        signer_did="did:legba:registry:c2b1-test",
    )


def _pack_body() -> dict[str, Any]:
    body = yaml.safe_load((_DESCRIPTORS / "action_pack_consult_write.yaml").read_text())
    body["identity"]["version"] = "0" * 16
    return body


async def _set_pack(reg: DescriptorRegistry, *, state: str = "active", classes: list[str] | None = None) -> None:
    """The consult_write pack registered, in ``state``, with ``classes`` enabled."""
    body = _pack_body()
    body["identity"]["state"] = state
    for tool in body["tools"]:
        tool["config"] = {"classes_enabled": classes if classes is not None else ["source"]}
    pack = ActionPack.model_validate(body, strict=False)
    try:
        await reg.get(W.CONSULT_WRITE_PACK_ID, family=Family.ACTION_PACK)
    except DescriptorNotFound:
        await reg.register(pack, "test")
        return
    await reg.update(W.CONSULT_WRITE_PACK_ID, pack, "test")


async def _source(reg: DescriptorRegistry, *, state: str = "active") -> str:
    body = yaml.safe_load((_DESCRIPTORS / "source_38north.yaml").read_text())
    sid = f"source.test.cw_{uuid4().hex[:10]}"
    body["identity"].update({"id": sid, "version": "0" * 16, "state": state})
    await reg.register(SourceDescriptor.model_validate(body, strict=False), "test")
    return sid


@dataclass
class World:
    store: PostgresStore
    reg: DescriptorRegistry
    ex: ConsultWriteExecutor
    armed: Armed
    src: str

    async def inspect(self, src: str | None = None) -> dict[str, Any]:
        return await self.ex.submit(self.armed, tool="inspect_object",
                                    args={"class": "source", "id": src or self.src})

    async def call(self, tool: str, **args: Any) -> dict[str, Any]:
        return await self.ex.submit(self.armed, tool=tool, args={"class": "source", **args},
                                    turn_cost_usd=0.0)

    async def head(self, src: str | None = None) -> Any:
        return await self.reg.get(src or self.src, family=Family.SOURCE)

    async def row(self, wid: str) -> dict[str, Any]:
        row = await self.ex.get(wid)
        assert row is not None
        return row


async def _arm(store: PostgresStore, words: str = WORDS) -> Armed:
    sid = await consult_persistence.create_session(store, mode="chat", question=words, principal="bearer:0123456789ab")
    turn = await consult_persistence.append_turn(store, session_id=sid, role="user", content=words)
    assert sid and turn
    return Armed(request_id=uuid4().hex, session_id=sid, user_turn_id=turn,
                 plane="core", model_component="llm.primary.openai_compat")


@pytest_asyncio.fixture
async def world(migrated_pg: PostgresConfig, monkeypatch):
    monkeypatch.setenv("LEGBA_CONSULT_WRITES", "on")
    store = PostgresStore(migrated_pg)
    await store.connect()
    identity = _identity()
    from legba.data.registry.audit import AuditLogger

    reg = DescriptorRegistry(store, signing_identity=identity, audit_logger=AuditLogger(identity=identity))
    await reg.start()
    await _set_pack(reg)
    ex = ConsultWriteExecutor(reg, probe=False)
    w = World(store=store, reg=reg, ex=ex, armed=await _arm(store), src=await _source(reg))
    yield w
    await reg.stop()
    # Test isolation: everything this fixture and the four files that
    # share it register into the SESSION DB is deleted here — the real
    # ``consult_write`` pack (a tree descriptor: left behind it is "co-present"
    # with the shipped tree and the production gauge's descriptor_state_drift
    # loop gauges it on a "blank" engine) and the ``source.test.cw_*`` source
    # heads (class analysis: left behind they are a candidate feed, and
    # test_desk_reference's unfillable requirement read fillable). Ordered
    # suites failed on both; the files passed alone.
    async with store.acquire() as conn:
        await _scrub_registered(conn)
    await store.close()


async def _scrub_registered(conn: Any) -> None:
    """Delete every descriptor head the consult-write test files mint
    (frame / gate / subject / unitcomp share ``world``) plus the real pack."""
    await conn.execute(
        "DELETE FROM source_descriptors WHERE descriptor_id LIKE 'source.test.cw%'")
    await conn.execute(
        "DELETE FROM target_descriptors WHERE descriptor_id LIKE 'cw\\_subj%'")
    await conn.execute(
        "DELETE FROM analyst_descriptors "
        "WHERE descriptor_id LIKE 'cw\\_unit\\_%' OR descriptor_id LIKE 'cw\\_comp\\_%' "
        # test_consult_write_unitcomp's ``_pinned_unit`` registers the shipped
        # ``escalation`` unit; left behind it is a head other files do not expect.
        "OR descriptor_id = 'escalation'")
    await conn.execute(
        "DELETE FROM action_pack_descriptors WHERE descriptor_id = $1", W.CONSULT_WRITE_PACK_ID)
    # The binding rigs' governor ledger rows (requested_by ``analyst::cw_*``).
    # The pack's governor counts every consult_write call in the trailing
    # minute (``api_rate_per_minute: 30``); with the entity and fact binding
    # tests (W11) an ordered run crossed 30 and the next file's call was
    # refused ``over_rate`` — each test passed alone.
    await conn.execute(
        "DELETE FROM action_pack_invocations WHERE pack_id = $1 AND requested_by LIKE 'analyst::cw\\_%'",
        W.CONSULT_WRITE_PACK_ID)
    # Entity records (test_consult_write_entity mints ``cw_entity_*``
    # profiles): their judgements first (no FK), then the profiles — versions,
    # edges and links cascade.
    await conn.execute(
        "DELETE FROM entity_judgement WHERE entity_a IN (SELECT id FROM entity_profiles "
        "WHERE canonical_name LIKE 'cw\\_entity\\_%') OR entity_b IN (SELECT id FROM entity_profiles "
        "WHERE canonical_name LIKE 'cw\\_entity\\_%')")
    await conn.execute(
        "DELETE FROM facts WHERE subject LIKE 'cw\\_entity\\_%'")
    await conn.execute(
        "DELETE FROM entity_profiles WHERE canonical_name LIKE 'cw\\_entity\\_%'")
    # The fact rows (predicate ``cw_fact*``) and the
    # vocabulary family (``cw_fact.*``) test_consult_write_fact.py mints.
    await conn.execute("DELETE FROM facts WHERE predicate LIKE 'cw\\_fact%'")
    await conn.execute("DELETE FROM vocabulary_entries WHERE family LIKE 'cw\\_fact%'")


async def _audit_row(store: PostgresStore, audit_id: str) -> dict[str, Any]:
    async with store.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM descriptor_audit_log WHERE id = $1::uuid", audit_id)
    assert row is not None
    return dict(row)


# ---------------------------------------------------------------------------
# 1. a built verb: admitted, executed, read back — one transaction, one receipt
# ---------------------------------------------------------------------------


async def test_update_executes_and_the_receipt_audit_row_and_ledger_agree(world: World):
    seen = await world.inspect()
    assert seen["found"] and seen["fields"]["cadence.cooldown_seconds"] == 60
    res = await world.call("update_object", id=world.src, expected_version=seen["version"],
                           patch={"cadence.cooldown_seconds": 600}, reason="slow it down")
    assert res["status"] == "executed", res
    head = await world.head()
    assert head.body["cadence"]["cooldown_seconds"] == 600 and head.version != seen["version"]

    row = await world.row(res["write_id"])
    assert row["status"] == "executed" and row["tier"] == "A" and row["audit_id"]
    receipt = row["receipt"]
    assert receipt["operator_words"] == WORDS
    assert receipt["pre"] == {"version": seen["version"], "state": "active"}
    assert receipt["post"] == {"version": head.version, "state": "active"}
    assert receipt["reversal"] == {"kind": "rollback", "to_version": seen["version"]}
    assert receipt["plane"] == "core" and receipt["turn_index"] == 0
    assert row["user_turn_id"] == world.armed.user_turn_id

    audit = await _audit_row(world.store, row["audit_id"])
    assert audit["actor_id"] == f"consult_write:{res['write_id']}"
    assert audit["actor_role"] == "operator_via_consult"
    summary = json.loads(audit["change_summary"]) if isinstance(audit["change_summary"], str) else audit["change_summary"]
    assert summary["receipt"]["write_id"] == res["write_id"]
    assert summary["receipt"]["post"]["version"] == head.version
    assert "bearer" not in audit["actor_id"] and "token" not in audit["actor_id"]
    # The signed chain verifies over the stored row (what the Audit-Chain Browser does).
    assert verify_audit_payload(_identity().verify_key, signed_payload_for_row(audit), bytes(audit["signed_payload"]))


# ---------------------------------------------------------------------------
# 2. refusals: protected / not writable / stale / preflight — nothing changes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("patch", "cause"), [
    ({"identity.id": "source.other"}, "protected_field"),
    ({"config.api_key_secret": "x"}, "protected_field"),
    ({"config.url": "https://elsewhere.example/feed"}, "path_not_writable"),
    ({"pipeline.media": "eager"}, "path_not_writable"),
    ({"config.lookback_days": 3}, "path_not_writable"),
    ({"cadence.cooldown_seconds": "soon"}, "preflight_failed"),
])
async def test_patch_refusals_write_nothing(world: World, patch, cause):
    seen = await world.inspect()
    res = await world.call("update_object", id=world.src, expected_version=seen["version"],
                           patch=patch, reason="r")
    assert res["status"] == "refused" and res["cause"] == cause, res
    assert (await world.head()).version == seen["version"]
    row = await world.row(res["write_id"])
    assert row["status"] == "refused" and row["error"]["cause"] == cause


async def test_a_stale_version_is_stale_read(world: World):
    res = await world.call("update_object", id=world.src, expected_version="f" * 16,
                           patch={"cadence.cooldown_seconds": 90}, reason="r")
    assert res["cause"] == "stale_read"


# ---------------------------------------------------------------------------
# 3. Tier B: stages; confirm by click and by typed code; expiry; head moved
# ---------------------------------------------------------------------------


async def test_pause_stages_and_the_click_executes_it(world: World):
    seen = await world.inspect()
    res = await world.call("transition_object", id=world.src, expected_version=seen["version"],
                           to_state="paused", reason="pause it")
    assert res["status"] == "pending_confirmation" and "code" not in res
    assert (await world.head()).state == "active"  # nothing changed
    listed = await world.ex.list(session_id=world.armed.session_id)
    pending = [r for r in listed if r["id"] == res["write_id"]][0]
    assert len(pending["confirm_code"]) == 4 and set(pending["confirm_code"]) <= set(W.CODE_ALPHABET)
    async with world.store.acquire() as conn:
        stored = await conn.fetchval("SELECT confirm_code_hash FROM consult_writes WHERE id = $1::uuid", res["write_id"])
    assert stored == W.code_hash(res["write_id"], pending["confirm_code"]) and pending["confirm_code"] not in stored

    done = await world.ex.confirm(res["write_id"], via="click")
    assert done["status"] == "executed", done
    assert (await world.head()).state == "paused"
    row = await world.row(res["write_id"])
    assert row["confirmed_via"] == "click" and row["receipt"]["tier"] == "B"


async def test_typed_code_confirms_and_a_wrong_code_does_not(world: World):
    draft = await _source(world.reg, state="draft")
    seen = await world.inspect(draft)
    res = await world.call("transition_object", id=draft, expected_version=seen["version"],
                           to_state="retired", reason="dead since spring")
    assert res["status"] == "pending_confirmation"
    code = world.ex._codes[res["write_id"]]
    wrong = "".join(c for c in W.CODE_ALPHABET if c not in code)[:4]
    miss = await world.ex.confirm(res["write_id"], via="typed_code", code=wrong)
    assert miss["status"] == "pending_confirmation" and (await world.head(draft)).state == "draft"
    found = await world.ex.find_by_code(world.armed.session_id, code.lower())
    assert found is not None and found["id"] == res["write_id"]
    done = await world.ex.confirm(res["write_id"], via="typed_code", code=code)
    assert done["status"] == "executed"
    assert (await world.head(draft)).state == "retired"
    assert done["receipt"]["reversal"] == {"kind": "none", "why": "retired is terminal"}


async def test_an_expired_staged_write_cannot_be_confirmed(world: World):
    seen = await world.inspect()
    res = await world.call("transition_object", id=world.src, expected_version=seen["version"],
                           to_state="paused", reason="r")
    async with world.store.acquire() as conn:
        await conn.execute(
            "UPDATE consult_writes SET expires_at = now() - interval '1 minute' WHERE id = $1::uuid",
            res["write_id"],
        )
    out = await world.ex.confirm(res["write_id"], via="click")
    assert out["status"] == "expired"
    assert (await world.row(res["write_id"]))["status"] == "expired"
    assert (await world.head()).state == "active"


async def test_head_moved_before_confirm_is_stale_read(world: World):
    seen = await world.inspect()
    res = await world.call("transition_object", id=world.src, expected_version=seen["version"],
                           to_state="paused", reason="r")
    typed = await world.reg.get_typed(world.src, family=Family.SOURCE)
    body = typed.model_dump(mode="json", by_alias=True)
    body["cadence"]["jitter_seconds"] = 45
    await world.reg.update(world.src, SourceDescriptor.model_validate(body, strict=False), "someone_else")
    out = await world.ex.confirm(res["write_id"], via="click")
    assert out["status"] == "refused" and out["cause"] == "stale_read"
    assert (await world.head()).state == "active"


async def test_a_new_staged_write_on_the_same_object_cancels_the_older(world: World):
    seen = await world.inspect()
    first = await world.call("transition_object", id=world.src, expected_version=seen["version"],
                             to_state="paused", reason="r")
    second = await world.call("transition_object", id=world.src, expected_version=seen["version"],
                              to_state="retired", reason="r")
    assert (await world.row(first["write_id"]))["status"] == "cancelled"
    assert (await world.row(second["write_id"]))["status"] == "pending_confirmation"
    assert (await world.ex.cancel(second["write_id"]))["status"] == "cancelled"


# ---------------------------------------------------------------------------
# 4. reversal on the stored pre-image; superseded_since
# ---------------------------------------------------------------------------


async def test_revert_an_update_restores_the_pre_image(world: World):
    seen = await world.inspect()
    before = (await world.head()).body
    res = await world.call("update_object", id=world.src, expected_version=seen["version"],
                           patch={"identity.name": "38 North (slow)"}, reason="r")
    rev = await world.ex.revert_button(res["write_id"], session_id=world.armed.session_id)
    assert rev["status"] == "executed", rev
    head = await world.head()
    assert head.version == seen["version"] and head.body == before
    original = await world.row(res["write_id"])
    assert original["reversed_by_write_id"] == rev["write_id"]
    revert_row = await world.row(rev["write_id"])
    assert revert_row["reverses_write_id"] == res["write_id"] and revert_row["confirmed_via"] == "revert_button"
    again = await world.ex.revert_button(res["write_id"])
    assert again["cause"] == "superseded_since"


async def test_revert_after_a_later_write_is_superseded_since(world: World):
    seen = await world.inspect()
    a = await world.call("update_object", id=world.src, expected_version=seen["version"],
                         patch={"cadence.jitter_seconds": 10}, reason="r")
    mid = await world.inspect()
    b = await world.call("update_object", id=world.src, expected_version=mid["version"],
                         patch={"cadence.jitter_seconds": 20}, reason="r")
    out = await world.ex.revert_button(a["write_id"])
    assert out["status"] == "refused" and out["cause"] == "superseded_since"
    assert b["write_id"] in out["detail"]
    assert (await world.head()).body["cadence"]["jitter_seconds"] == 20


async def test_revert_a_confirmed_pause_reactivates(world: World):
    seen = await world.inspect()
    res = await world.call("transition_object", id=world.src, expected_version=seen["version"],
                           to_state="paused", reason="r")
    await world.ex.confirm(res["write_id"], via="click")
    rev = await world.ex.revert_button(res["write_id"])
    assert rev["status"] == "executed"
    head = await world.head()
    assert head.state == "active" and head.version == seen["version"]


async def test_create_lands_draft_and_its_revert_retires_the_draft(world: World):
    body = yaml.safe_load((_DESCRIPTORS / "source_38north.yaml").read_text())
    new_id = f"source.test.cw_new_{uuid4().hex[:8]}"
    body["identity"].update({"id": new_id, "state": "active"})
    res = await world.call("create_object", body=body, reason="add the feed")
    assert res["status"] == "executed", res
    assert (await world.head(new_id)).state == "draft"
    rev = await world.ex.revert_button(res["write_id"])
    assert rev["status"] == "executed" and (await world.head(new_id)).state == "retired"


async def test_preflight_refuses_an_unparseable_body(world: World):
    bad = {"identity": {"id": f"source.test.cw_bad_{uuid4().hex[:6]}", "name": "x", "kind": "rss",
                        "schema_uri": "legba/source/1.0.0"}, "acquisition": "stream"}
    res = await world.call("create_object", body=bad, reason="r")
    assert res["status"] == "refused" and res["cause"] == "preflight_failed"
    seen = await world.inspect()
    res = await world.ex.submit(
        world.armed, tool="update_object",
        args={"class": "source", "id": world.src, "expected_version": seen["version"],
              "patch": {"cadence.cooldown_seconds": 30}, "reason": "r"},
        runtime_preflight="the runtime parser refused it",
    )
    assert res["cause"] == "preflight_failed" and (await world.head()).version == seen["version"]


# ---------------------------------------------------------------------------
# 5. the activation probe (D10): a forced post-write parse_failed rolls back
# ---------------------------------------------------------------------------


async def test_a_forced_parse_failed_rolls_the_head_back(world: World):
    ex = ConsultWriteExecutor(world.reg, probe=False, parse_check=lambda body: "forced: unparseable")
    seen = await world.inspect()
    res = await ex.submit(world.armed, tool="update_object", args={
        "class": "source", "id": world.src, "expected_version": seen["version"],
        "patch": {"cadence.cooldown_seconds": 75}, "reason": "r"})
    assert res["status"] == "executed"
    verdict = await ex.probe_once(res["write_id"])
    assert verdict["status"] == "parse_failed" and verdict["rolled_back_to"] == seen["version"]
    assert (await world.head()).version == seen["version"]
    row = await world.row(res["write_id"])
    assert row["activation"]["status"] == "parse_failed"
    assert row["receipt"]["activation"]["rolled_back_to"] == seen["version"]


async def test_the_probe_verifies_on_the_first_poll_and_is_honest_when_paused(world: World):
    from legba.runtime.source_poll_outcome import write_poll_outcome

    seen = await world.inspect()
    res = await world.call("update_object", id=world.src, expected_version=seen["version"],
                           patch={"cadence.cooldown_seconds": 77}, reason="r")
    assert await world.ex.probe_once(res["write_id"]) is None  # nothing polled yet: keep watching
    head = await world.head()
    async with world.store.acquire() as conn:
        await write_poll_outcome(conn, source_id=world.src, source_version=head.version, owner_tenant="shared",
                                 outcome="empty", health_state=None, capped=False, signals_written=0, error=None)
    verdict = await world.ex.probe_once(res["write_id"])
    assert verdict["status"] == "verified"
    draft = await _source(world.reg, state="draft")
    seen = await world.inspect(draft)
    res = await world.call("update_object", id=draft, expected_version=seen["version"],
                           patch={"identity.name": "draft renamed"}, reason="r")
    assert (await world.ex.probe_once(res["write_id"]))["status"] == "not_applicable"


# ---------------------------------------------------------------------------
# 6. kill switches; no operator turn; caps; seams
# ---------------------------------------------------------------------------


async def test_kill_switches_each_stop_the_next_write(world: World, monkeypatch):
    seen = await world.inspect()
    args = {"id": world.src, "expected_version": seen["version"],
            "patch": {"cadence.cooldown_seconds": 61}, "reason": "r"}
    monkeypatch.setenv("LEGBA_CONSULT_WRITES", "off")
    assert (await world.call("update_object", **args))["cause"] == "writes_disabled"
    monkeypatch.setenv("LEGBA_CONSULT_WRITES", "on")
    await _set_pack(world.reg, state="paused")
    assert (await world.call("update_object", **args))["cause"] == "writes_disabled"
    await _set_pack(world.reg, classes=[])
    assert (await world.call("update_object", **args))["cause"] == "class_disabled"
    await _set_pack(world.reg)
    assert (await world.call("update_object", **args))["status"] == "executed"


async def test_a_run_without_a_durable_operator_turn_writes_nothing(world: World):
    armed = Armed(request_id="r", session_id=world.armed.session_id, user_turn_id=str(uuid4()),
                  plane="core", model_component=None)
    seen = await world.inspect()
    res = await world.ex.submit(armed, tool="update_object", args={
        "class": "source", "id": world.src, "expected_version": seen["version"],
        "patch": {"cadence.cooldown_seconds": 62}, "reason": "r"})
    assert res["cause"] == "no_operator_turn"


async def test_per_turn_caps(world: World):
    for i in range(10):
        seen = await world.inspect()
        res = await world.call("update_object", id=world.src, expected_version=seen["version"],
                               patch={"cadence.cooldown_seconds": 100 + i}, reason="r")
        assert res["status"] == "executed", (i, res)
    seen = await world.inspect()
    res = await world.call("update_object", id=world.src, expected_version=seen["version"],
                           patch={"cadence.cooldown_seconds": 999}, reason="r")
    assert res["cause"] == "write_cap_reached"
    world.armed = await _arm(world.store)
    for i in range(5):
        src = await _source(world.reg)
        seen = await world.inspect(src)
        res = await world.call("transition_object", id=src, expected_version=seen["version"],
                               to_state="paused", reason="r")
        assert res["status"] == "pending_confirmation"
    src = await _source(world.reg)
    seen = await world.inspect(src)
    res = await world.call("transition_object", id=src, expected_version=seen["version"],
                           to_state="paused", reason="r")
    assert res["cause"] == "write_cap_reached"


async def test_a_seamed_verb_raises_write_kind_not_built(world: World):
    with pytest.raises(W.WriteKindNotBuilt) as err:
        # unit create stays SEAMS #64 beyond v1 (subject is built since W6).
        await world.ex.submit(world.armed, tool="create_object", args={
            "class": "unit", "body": {"identity": {"id": "x"}}, "reason": "r"})
    assert err.value.seam == 64
    with pytest.raises(W.WriteKindNotBuilt) as err:
        # the proposal decision class is SEAMS #64 too, even for its read verb
        # (#65 is resolved: both record classes, entity and fact, are built in W11).
        await world.ex.submit(world.armed, tool="inspect_object", args={"class": "proposal", "id": "x"})
    assert err.value.seam == 64
    # Not a word of the plane at all: refused, never a 500.
    res = await world.ex.submit(world.armed, tool="create_object",
                                args={"class": "proposal", "body": {}, "reason": "r"})
    assert res["cause"] == "class_not_writable"
    res = await world.ex.submit(world.armed, tool="inspect_object", args={"class": "finding", "id": "x"})
    assert res["cause"] == "class_not_writable"


# ---------------------------------------------------------------------------
# 7. routes: POST /consult/writes (armed only; 501 for a seam), operator acts
# ---------------------------------------------------------------------------


def _writes_app(world: World, *, live: bool = True) -> tuple[FastAPI, Any]:
    from legba.data.registry import consult_writes_api as front_mod

    deps = type("Deps", (), {"descriptor_registry": world.reg})()
    front = front_mod.WritesFront(deps, is_live=lambda rid: live, executor=world.ex)
    front.armed[world.armed.request_id] = world.armed
    app = FastAPI()
    app.include_router(front_mod.build_router(front), prefix="/api/v1")
    return app, front


async def test_the_routes(world: World, monkeypatch):
    monkeypatch.delenv("LEGBA_REGISTRY_API_TOKEN", raising=False)
    app, _front = _writes_app(world)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post("/api/v1/consult/writes", json={
            "request_id": "not-armed", "tool": "inspect_object", "args": {"class": "source", "id": world.src}})
        assert r.status_code == 403 and "writes_not_armed" in r.text
        r = await c.post("/api/v1/consult/writes", json={
            "request_id": world.armed.request_id, "tool": "inspect_object",
            "args": {"class": "proposal", "id": "p"}})
        assert r.status_code == 501 and r.json()["detail"]["seam"] == 64
        seen = (await c.post("/api/v1/consult/writes", json={
            "request_id": world.armed.request_id, "tool": "inspect_object",
            "args": {"class": "source", "id": world.src}})).json()
        staged = (await c.post("/api/v1/consult/writes", json={
            "request_id": world.armed.request_id, "tool": "transition_object",
            "args": {"class": "source", "id": world.src, "expected_version": seen["version"],
                     "to_state": "paused", "reason": "r"}})).json()
        assert staged["status"] == "pending_confirmation"
        listed = (await c.get("/api/v1/consult/writes", params={"session_id": world.armed.session_id})).json()
        assert [w["status"] for w in listed["writes"]] == ["pending_confirmation"]
        assert "confirm_code_hash" not in listed["writes"][0] and listed["writes"][0]["confirm_code"]
        r = await c.post(f"/api/v1/consult/writes/{staged['write_id']}/confirm", json={})
        assert r.status_code == 200 and r.json()["status"] == "executed"
        assert r.json()["line"].startswith("Written · source ")
        r = await c.post(f"/api/v1/consult/writes/{staged['write_id']}/revert", json={})
        assert r.json()["status"] == "executed" and (await world.head()).state == "active"
        r = await c.post(f"/api/v1/consult/writes/{uuid4()}/cancel", json={})
        assert r.status_code == 404
        listed = (await c.get("/api/v1/consult/writes", params={
            "session_id": world.armed.session_id, "class": "source", "object": world.src})).json()
        assert [w["verb"] for w in listed["writes"]] == ["transition", "revert"]


async def test_the_list_is_scoped_and_the_whole_ledger_never_carries_a_code(world: World, monkeypatch):
    """A read names its scope; ``?all=1`` needs an explicit principal and never leaks a code."""
    monkeypatch.delenv("LEGBA_REGISTRY_API_TOKEN", raising=False)
    monkeypatch.setenv("LEGBA_DEV_MODE", "1")
    app, _front = _writes_app(world)
    ex = _front.executor
    seen = await world.inspect()
    staged = await ex.submit(world.armed, tool="transition_object", args={
        "class": "source", "id": world.src, "expected_version": seen["version"],
        "to_state": "paused", "reason": "r"})
    assert staged["status"] == "pending_confirmation" and staged["write_id"] in ex._codes
    bearer = {"Authorization": "Bearer operator-token"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        # no scope and no all=1 -> refused; an anonymous all=1 -> refused
        r = await c.get("/api/v1/consult/writes", headers=bearer)
        assert r.status_code == 400 and "scope_required" in r.text
        r = await c.get("/api/v1/consult/writes", params={"all": "1"})
        assert r.status_code == 403
        # the session scope is the caller's own conversation: it still carries the typed-code hint
        mine = (await c.get("/api/v1/consult/writes", params={"session_id": world.armed.session_id})).json()
        assert [w["status"] for w in mine["writes"]] == ["pending_confirmation"]
        assert len(mine["writes"][0]["confirm_code"]) == 4
        # another session sees none of it
        other = (await c.get("/api/v1/consult/writes", params={"session_id": str(uuid4())})).json()
        assert other == {"writes": [], "count": 0}
        # the whole ledger: explicit principal, and NEVER a code or a hash
        whole = (await c.get("/api/v1/consult/writes", params={"all": "1"}, headers=bearer)).json()
        pending = [w for w in whole["writes"] if w["status"] == "pending_confirmation"]
        assert pending and all("confirm_code" not in w and "confirm_code_hash" not in w for w in whole["writes"])
        assert ex._codes[staged["write_id"]] not in json.dumps(whole)


# ---------------------------------------------------------------------------
# 8. the front door: the typed code executes with ZERO model invokes
# ---------------------------------------------------------------------------


def _no_dapr(invoked: list[Any]) -> type:
    class _NoDapr:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def put(self, url, json=None, headers=None):
            invoked.append(json)
            raise AssertionError("a confirm must never invoke the actor")

    return _NoDapr


async def test_typed_code_at_the_front_door_executes_without_a_model(world: World, monkeypatch):
    import legba.data.registry.consult_api as consult_api
    from legba.data.registry.api import RegistryAPIDeps

    monkeypatch.delenv("LEGBA_REGISTRY_API_TOKEN", raising=False)
    invoked: list[Any] = []
    monkeypatch.setattr(consult_api.httpx, "AsyncClient", _no_dapr(invoked))
    ex = executor_for(world.reg)  # THE executor the front door uses
    seen = await world.inspect()
    staged = await ex.submit(world.armed, tool="transition_object", args={
        "class": "source", "id": world.src, "expected_version": seen["version"],
        "to_state": "paused", "reason": "r"})
    code = ex._codes[staged["write_id"]]
    deps = RegistryAPIDeps(
        descriptor_registry=world.reg, stack_registry=None, vault=None, dlq=None,  # type: ignore[arg-type]
        audit_logger=None, vocabulary_cache=None, nats_store=None,  # type: ignore[arg-type]
    )
    app = FastAPI()
    app.include_router(consult_api.build_consult_router(deps), prefix="/api/v1")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        miss = await c.post("/api/v1/consult", json={"question": "confirm 9999",
                                                     "session_id": world.armed.session_id})
        assert miss.status_code == 200 and miss.json()["answer"].startswith("No pending change")
        r = await c.post("/api/v1/consult", json={"question": f"  confirm {code.lower()} ",
                                                  "session_id": world.armed.session_id})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["answer"].startswith("Written · source ") and body["usage"] == {}
    assert body["writes"][0]["status"] == "executed"
    assert invoked == []  # zero actor invokes: zero LLM calls
    assert (await world.head()).state == "paused"
    row = await world.row(staged["write_id"])
    assert row["confirmed_via"] == "typed_code"
    loaded = await consult_persistence.load_session(world.store, world.armed.session_id)
    contents = [t["content"] for t in loaded["turns"]]
    assert f"Confirmed write w-{staged['write_id']}" in contents
    assert contents[-1].startswith("Written · source ")


def test_an_armed_request_without_the_grant_is_409_and_an_unrecorded_turn_is_503(monkeypatch):
    """Fail loud before anything runs: no grant → 409; no durable turn → 503."""
    import legba.data.registry.consult_api as consult_api
    from fastapi.testclient import TestClient
    from legba.data.registry.api import RegistryAPIDeps

    monkeypatch.delenv("LEGBA_REGISTRY_API_TOKEN", raising=False)

    class _Row:
        version = "v" * 16

        def __init__(self, grants):
            self.body = {"action_packs": [{"pack_id": g} for g in grants]}

    class _Pg:
        def acquire(self):
            raise AssertionError("no db in this test")

    class _Reg:
        pg = _Pg()

        def __init__(self, grants):
            self.grants = grants

        async def get(self, descriptor_id, *, family, version=None):
            return _Row(self.grants)

    class _Dapr:
        def __init__(self, *a, **k):
            raise AssertionError("never invoked")

    monkeypatch.setattr(consult_api.httpx, "AsyncClient", _Dapr)
    for grants, code, word in ((["substrate_read"], 409, "writes_unavailable"),
                               (["substrate_read", "consult_write"], 503, "operator turn")):
        deps = RegistryAPIDeps(
            descriptor_registry=_Reg(grants), stack_registry=None, vault=None, dlq=None,  # type: ignore[arg-type]
            audit_logger=None, vocabulary_cache=None, nats_store=None,  # type: ignore[arg-type]
        )
        app = FastAPI()
        app.include_router(consult_api.build_consult_router(deps), prefix="/api/v1")
        r = TestClient(app).post("/api/v1/consult", json={"question": "pause it", "allow_writes": True})
        assert r.status_code == code and word in r.text, r.text


@pytest.mark.parametrize("shape", ["bad_status", "envelope"])
async def test_an_actor_side_writes_unavailable_is_409_never_a_502(monkeypatch, shape):
    """The actor's own ``writes_unavailable`` refusal reaches the operator as 409."""
    import legba.data.registry.consult_api as consult_api
    from legba.data.registry.consult_runs import ConsultRunError

    words = "writes_unavailable: consult_write not bound"
    assert words == W.CONSULT_WRITE_PACK_ID.join(("writes_unavailable: ", " not bound"))

    class _Resp:
        def __init__(self) -> None:
            self.status_code = 500 if shape == "bad_status" else 200

        @property
        def text(self) -> str:
            return f"actor error: ValueError: {words}" if shape == "bad_status" else "{}"

        def json(self) -> dict[str, Any]:
            return {"outcome": "error", "error": f"ValueError: {words}", "reason": None, "detail": None}

    class _Dapr:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def put(self, url, json=None, headers=None):
            return _Resp()

    monkeypatch.setattr(consult_api.httpx, "AsyncClient", _Dapr)
    with pytest.raises(ConsultRunError) as err:
        await consult_api._invoke_and_project(
            None, invoke_url="http://x", sidecar_url="http://x", actor_id="a",  # type: ignore[arg-type]
            invoke_body={}, session_id=None, chosen_model="core", request_id="r",
        )
    assert err.value.status_code == 409 and err.value.detail == words


# ---------------------------------------------------------------------------
# 9. the ledger table: forward-only, idempotent migration, read-back
# ---------------------------------------------------------------------------


async def test_status_moves_forward_only_and_the_migration_is_idempotent(world: World):
    import asyncpg

    seen = await world.inspect()
    res = await world.call("update_object", id=world.src, expected_version=seen["version"],
                           patch={"cadence.cooldown_seconds": 63}, reason="r")
    async with world.store.acquire() as conn:
        for back in ("staged", "pending_confirmation", "cancelled"):
            with pytest.raises(asyncpg.exceptions.CheckViolationError):
                await conn.execute("UPDATE consult_writes SET status = $2 WHERE id = $1::uuid",
                                   res["write_id"], back)
        # a non-status column of an executed row may still be stamped
        await conn.execute("UPDATE consult_writes SET activation = '{\"status\":\"x\"}' WHERE id = $1::uuid",
                           res["write_id"])
        await conn.execute(_MIGRATION.read_text())
        idx = {r["indexname"] for r in await conn.fetch(
            "SELECT indexname FROM pg_indexes WHERE tablename = 'consult_writes'")}
        # No FK to the conversation (receipts outlive it). The one FK is
        # lane 9's written_by -> principals, whose rows are never deleted.
        fks = await conn.fetchval(
            "SELECT count(*) FROM pg_constraint WHERE conrelid = 'consult_writes'::regclass AND contype = 'f' "
            "AND confrelid <> 'principals'::regclass")
    assert {"idx_consult_writes_session", "idx_consult_writes_object", "idx_consult_writes_pending_expiry"} <= idx
    assert fks == 0


# ---------------------------------------------------------------------------
# 10. guards
# ---------------------------------------------------------------------------


def test_the_drift_guard_pack_specs_vocabulary_handlers_executor():
    from legba.data.analysts.agency.tools import ToolRegistry
    from legba.data.analysts.agency.consult_write import register_consult_write_tools
    from legba.data.analysts.consult_tool_providers import providers
    from legba.data.registry.consult_write_exec import CLASS_BRANCHES
    from legba.data.stack.llm.tool_rounds import ARG_BLOCK_CAUSES, TOOL_SCHEMAS, is_write_tool

    pack_tools = [t["name"] for t in _pack_body()["tools"]]
    provider = [p for p in providers() if p.pack_id == W.CONSULT_WRITE_PACK_ID][0]
    reg = ToolRegistry()
    register_consult_write_tools(reg)
    assert pack_tools == list(W.WRITE_TOOL_NAMES) == list(provider.tool_names)
    assert set(reg.names) == set(pack_tools)
    for name in pack_tools:
        schema = TOOL_SCHEMAS[name].json_schema
        assert schema["additionalProperties"] is False, name  # closed: validate_args checks it
        assert is_write_tool(name), name  # serial ordering
    built = {(c, v) for (c, v), e in W.VOCABULARY.items() if e == "built"}
    assert set(CLASS_BRANCHES) == set(W.BUILT_CLASSES) == {c for c, _ in built}
    for cls, branch in CLASS_BRANCHES.items():
        assert branch.VERBS == {v for c, v in built if c == cls}
    assert {W.TOOL_VERB[t] for t in pack_tools} | {"retire"} <= {v for _, v in built}
    assert not W.WRITE_BLOCK_CAUSES & ARG_BLOCK_CAUSES
    # SEAMS #65 (record writes) is resolved: entity and fact are both built (W11).
    assert {W.seam_for(c, v) for (c, v), e in W.VOCABULARY.items() if e != "built"} == {64}


def test_the_registry_write_modules_import_without_the_runtime_stack():
    probe = textwrap.dedent(
        """
        import sys
        for blocked in ("feedparser", "telethon", "warcio", "aiobotocore",
                        "pycountry", "networkx", "qdrant_client"):
            sys.modules[blocked] = None
        import legba.data.registry.consult_writes_api as api
        import legba.data.registry.consult_write_exec as ex
        import legba.data.consult_write_vocabulary as voc
        leaked = sorted(
            m for m in sys.modules
            if m.startswith("legba.data.analysts") or m.startswith("legba.runtime")
        )
        assert not leaked, "analyst/runtime package reachable: %r" % leaked
        assert api.build_router and ex.ConsultWriteExecutor and voc.WRITE_TOOL_NAMES
        print("OK")
        """
    )
    result = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True,
                            timeout=120, env={**os.environ})
    assert result.returncode == 0, result.stderr[-2000:]
    assert result.stdout.strip().endswith("OK")


def test_the_vocabulary_words():
    code = W.mint_code()
    assert len(code) == 4 and set(code) <= set(W.CODE_ALPHABET)
    assert not set("0O1IL2Z5S8B") & set(W.CODE_ALPHABET)
    assert W.source_tier("transition", from_state="active", to_state="paused") == "B"
    assert W.source_tier("transition", from_state="paused", to_state="active") == "B"
    assert W.source_tier("transition", from_state="draft", to_state="configured") == "A"
    assert W.source_tier("retire", from_state="draft", to_state="retired") == "B"
    assert W.source_tier("update", from_state="active", to_state=None) == "A"
