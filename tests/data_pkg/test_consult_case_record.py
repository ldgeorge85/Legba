# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""C-1b — the case RECORD: persisted, replayable without the model, stoppable.

Design C-1a §3 and acceptance 6, 8, 9. The record lives under the assistant
turn's 0223 response bag, key ``case``; these tests write it through the real
front door (``POST /api/v1/consult`` → the detached run → the stop route) onto
the REAL migrated Postgres and read it back through ``load_session`` — the
reload path the workstation's Replay button uses. The Dapr invoke and core
NATS are the documented doubles (the in-memory NATS replays publishes to the
run's own subscription exactly as core NATS would).
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import fields
from typing import Any
from uuid import uuid4

import asyncpg
import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

import legba.data.registry.consult_api as consult_api
from legba.data import workstation_vocabulary as V
from legba.data.analysts.consult_case_schema import (
    CaseActionFrame,
    CaseActionRecord,
    CaseRecord,
    Cite,
    RestsOn,
)
from legba.data.consult_case_record import CaseFrameAccumulator, compose_record
from legba.data.registry import consult_persistence
from legba.data.registry.api import RegistryAPIDeps
from legba.data.registry.consult_api import ConsultResponse, build_consult_router

pytestmark = [pytest.mark.asyncio]

API_TOKEN_ENV = "LEGBA_REGISTRY_API_TOKEN"

#: Captured before any test swaps ``consult_api.httpx.AsyncClient`` (which IS
#: ``httpx.AsyncClient``) for the Dapr double.
_RealAsyncClient = httpx.AsyncClient


@pytest_asyncio.fixture
async def pool(migrated_pg):
    p = await asyncpg.create_pool(
        host=migrated_pg.host, port=migrated_pg.port, user=migrated_pg.user,
        password=migrated_pg.password, database=migrated_pg.database,
        min_size=1, max_size=4,
    )
    yield p
    await p.close()


def _frame(seq: int, action: str = "narrate") -> dict[str, Any]:
    return CaseActionFrame(
        seq=seq, action=action, args={"text": f"step {seq}"},
        cites=[Cite("target", "subject_alpha")], rests_on=[RestsOn(1, "list_targets")],
        at="2026-10-03T12:00:00Z", round=2,
    ).to_step()


# ---------------------------------------------------------------------------
# The accumulator: contiguity, the stop, late frames
# ---------------------------------------------------------------------------


async def test_a_dropped_frame_yields_an_incomplete_record_that_stops_at_the_hole():
    acc = CaseFrameAccumulator(request_id="r", plane="core")
    for seq in (1, 2, 4, 5):
        acc.observe(_frame(seq))
    record = acc.compose()
    assert record["complete"] is False and record["gap_at_seq"] == 3
    assert [a["seq"] for a in record["actions"]] == [1, 2]
    assert record["status"] == "unsealed"


async def test_a_stop_truncates_the_record_and_late_frames_stay_out():
    acc = CaseFrameAccumulator(request_id="r", plane="core")
    for seq in (1, 2, 3, 4):
        acc.observe(_frame(seq))
    assert acc.stop(3) == 3
    late = _frame(5)
    acc.observe(late)
    assert late["after_stop"] is True  # the trace keeps it, marked
    record = acc.compose()
    assert record["status"] == "stopped" and record["stopped_after_seq"] == 3
    assert [a["seq"] for a in record["actions"]] == [1, 2, 3]
    assert record["complete"] is True and record["gap_at_seq"] is None


async def test_a_stop_without_a_count_lands_after_the_last_contiguous_frame():
    acc = CaseFrameAccumulator(request_id="r", plane=None)
    for seq in (1, 2, 4):
        acc.observe(_frame(seq))
    assert acc.stop(None) == 2


async def test_a_stop_claiming_past_what_the_registry_saw_is_clamped_to_it():
    """W5-F6 — the stop body is a claim; the record names only frames the registry holds."""
    none_seen = CaseFrameAccumulator(request_id="r", plane="core")
    assert none_seen.stop(3) == 0  # claims 3, no action frame arrived
    record = none_seen.compose()
    assert record["status"] == "stopped" and record["stopped_after_seq"] == 0
    assert record["actions"] == [] and record["complete"] is True

    two_seen = CaseFrameAccumulator(request_id="r", plane="core")
    for seq in (1, 2):
        two_seen.observe(_frame(seq))
    assert two_seen.stop(9) == 2  # clamped down to the highest seq seen
    assert [a["seq"] for a in two_seen.compose()["actions"]] == [1, 2]

    under = CaseFrameAccumulator(request_id="r", plane="core")
    for seq in (1, 2, 3):
        under.observe(_frame(seq))
    assert under.stop(1) == 1  # a claim BELOW what was seen is the workstation's word


async def test_w10_f10_frames_the_stop_claim_covers_land_even_after_the_ack():
    """W10-F10 — the workstation ran seq 1..2 (it executes only on a frame), but
    the frames reached the registry AFTER the stop's ack: the record still
    carries the two steps that ran; a frame past the claim stays out."""
    acc = CaseFrameAccumulator(request_id="r", plane="core")
    assert acc.stop(2) == 0  # nothing seen yet at the ack (the W5-F6 clamp)
    early = [_frame(1), _frame(2)]
    for f in early:
        acc.observe(f)
    assert all("after_stop" not in f for f in early)
    late = _frame(3)
    acc.observe(late)
    assert late["after_stop"] is True
    record = acc.compose()
    assert record["status"] == "stopped" and record["stopped_after_seq"] == 2
    assert [a["seq"] for a in record["actions"]] == [1, 2]
    assert record["complete"] is True

    # A stop with no claim keeps today's rule: nothing after the ack lands.
    bare = CaseFrameAccumulator(request_id="r", plane="core")
    assert bare.stop(None) == 0
    f1 = _frame(1)
    bare.observe(f1)
    assert f1["after_stop"] is True and bare.compose()["actions"] == []


async def test_the_record_is_exactly_the_contract_shape():
    record = compose_record(
        request_id="r", status="sealed", plane="core", refused=1, summary="s",
        actions=[{k: v for k, v in _frame(1).items() if k not in ("phase", "kind", "round")}],
    )
    assert set(record) == {f.name for f in fields(CaseRecord)}
    assert set(record["actions"][0]) == {f.name for f in fields(CaseActionRecord)}
    assert record["version"] == V.CASE_RECORD_VERSION
    assert record["vocabulary"] == V.vocabulary_hash()
    with pytest.raises(ValueError):
        compose_record(request_id=None, status="paused", plane=None, refused=0,
                       summary=None, actions=[])


# ---------------------------------------------------------------------------
# Persisted and reloaded (real Postgres)
# ---------------------------------------------------------------------------


async def test_a_full_case_persists_in_the_bag_and_the_reload_projects_it(pool):
    record = compose_record(
        request_id="req-1", status="sealed", plane="core", refused=0, summary="walk",
        actions=[{k: v for k, v in _frame(s).items() if k not in ("phase", "kind", "round")}
                 for s in (1, 2)],
    )
    session_id = await consult_persistence.create_session(pool, mode="chat", question="q")
    response = ConsultResponse(answer="a", uncertainty=0.4, model="core", case=record)
    turn_id = await consult_persistence.append_turn(
        pool, session_id=session_id, role="assistant", content="a",
        response=consult_persistence.response_bag(response),
    )
    assert turn_id is not None
    loaded = await consult_persistence.load_session(pool, session_id)
    turn = loaded["turns"][-1]
    assert turn["response_recorded"] is True
    assert turn["case"] == record
    # Replay needs nothing but the record: absolute args, every cite's read.
    assert [a["seq"] for a in turn["case"]["actions"]] == [1, 2]
    assert all(a["rests_on"] for a in turn["case"]["actions"])


async def test_a_plain_turn_bag_carries_case_null(pool):
    session_id = await consult_persistence.create_session(pool, mode="chat", question="q")
    await consult_persistence.append_turn(
        pool, session_id=session_id, role="assistant", content="a",
        response=consult_persistence.response_bag(ConsultResponse(answer="a")),
    )
    turn = (await consult_persistence.load_session(pool, session_id))["turns"][-1]
    assert turn["response_recorded"] is True and turn["case"] is None


# ---------------------------------------------------------------------------
# Through the front door: arm, 409, stop
# ---------------------------------------------------------------------------


class _Nc:
    def __init__(self) -> None:
        self.published: list[tuple[str, dict[str, Any]]] = []
        self._subs: dict[str, list[Any]] = {}

    async def publish(self, subject: str, payload: bytes) -> None:
        self.published.append((subject, json.loads(payload)))
        for cb in list(self._subs.get(subject, [])):
            await cb(type("_Msg", (), {"data": payload})())

    async def subscribe(self, subject: str, cb: Any = None) -> Any:
        self._subs.setdefault(subject, []).append(cb)
        nc = self

        class _Sub:
            async def unsubscribe(self) -> None:
                nc._subs.get(subject, []).remove(cb)

        return _Sub()


class _Nats:
    def __init__(self) -> None:
        self.nc = _Nc()


class _Row:
    def __init__(self, body: dict[str, Any]) -> None:
        self.version = "v" + "c" * 16
        self.body = body


class _Registry:
    def __init__(self, pg: Any, *, grants: bool = True, pack: bool = True) -> None:
        self.pg = pg
        self._grants = grants
        self._pack = pack

    async def get(self, descriptor_id: str, *, family: Any, version: Any = None) -> Any:
        from legba.data.registry.errors import DescriptorNotFound

        if descriptor_id == V.WORKSTATION_DRIVE_PACK_ID:
            if not self._pack:
                raise DescriptorNotFound("action_pack", descriptor_id)
            return _Row({})
        packs = [{"pack_id": "substrate_read"}]
        if self._grants:
            packs.append({"pack_id": V.WORKSTATION_DRIVE_PACK_ID})
        return _Row({"action_packs": packs})


def _app(pg: Any, nats: Any, **reg: Any) -> FastAPI:
    import os

    os.environ.pop(API_TOKEN_ENV, None)
    app = FastAPI()
    deps = RegistryAPIDeps(
        descriptor_registry=_Registry(pg, **reg),  # type: ignore[arg-type]
        stack_registry=None,  # type: ignore[arg-type]
        vault=None,  # type: ignore[arg-type]
        dlq=None,  # type: ignore[arg-type]
        audit_logger=None,  # type: ignore[arg-type]
        vocabulary_cache=None,  # type: ignore[arg-type]
        nats_store=nats,
    )
    app.include_router(build_consult_router(deps), prefix="/api/v1")
    return app


def _install_dapr(monkeypatch, *, nats: _Nats | None, captured: dict[str, Any],
                  envelope: dict[str, Any] | None = None, frames: int = 0,
                  frames_on_cancel: int = 0) -> None:
    class _Resp:
        status_code = 200

        def json(self) -> dict[str, Any]:
            return envelope or {}

        @property
        def text(self) -> str:
            return json.dumps(envelope or {})

    class _Client:
        def __init__(self, *a: Any, **k: Any) -> None:
            pass

        async def __aenter__(self) -> "_Client":
            return self

        async def __aexit__(self, *exc: Any) -> bool:
            return False

        async def put(self, url: str, json: Any = None, headers: Any = None) -> _Resp:
            captured["body"] = json
            if frames:
                rid = json["inputs"][0]["request_id"]
                for seq in range(1, frames + 1):
                    await nats.nc.publish(  # type: ignore[union-attr]
                        f"legba.consult.steps.{rid}",
                        __import__("json").dumps({"type": "step", **_frame(seq)}).encode(),
                    )
                await asyncio.sleep(3600)  # the run is still drilling
            if frames_on_cancel:
                rid = json["inputs"][0]["request_id"]
                try:
                    await asyncio.sleep(3600)  # drilling; the frames are in flight
                except asyncio.CancelledError:
                    # W10-F10 — the frames the workstation already ran reach the
                    # registry only after the stop's ack (core NATS is not
                    # ordered against the stop route).
                    for seq in range(1, frames_on_cancel + 1):
                        await nats.nc.publish(  # type: ignore[union-attr]
                            f"legba.consult.steps.{rid}",
                            __import__("json").dumps({"type": "step", **_frame(seq)}).encode(),
                        )
                    raise
            return _Resp()

    monkeypatch.setattr(consult_api.httpx, "AsyncClient", _Client)


def _client(app: FastAPI) -> httpx.AsyncClient:
    return _RealAsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


async def test_an_armed_request_threads_the_flag_and_a_plain_one_is_unchanged(pool, monkeypatch):
    captured: dict[str, Any] = {}
    envelope = {"outcome": "success", "mode": "chat", "derived_from": [],
                "consult_response": {"answer": "a", "data": {}}}
    _install_dapr(monkeypatch, nats=None, captured=captured, envelope=envelope)
    async with _client(_app(pool, None)) as client:
        r = await client.post("/api/v1/consult", json={
            "question": "q", "request_id": "fixed-1", "present_case": False, "session_id": "x"})
        assert r.status_code == 200, r.text
        assert captured["body"]["inputs"][0] == {
            "question": "q", "scope_predicate": None, "max_tool_rounds": 10,
            "mode": "chat", "request_id": "fixed-1", "messages": [], "session_id": "x",
        }
        assert r.json()["case"] is None
        r = await client.post("/api/v1/consult", json={
            "question": "q", "request_id": "fixed-2", "present_case": True, "session_id": "x"})
        assert r.status_code == 200, r.text
        row = captured["body"]["inputs"][0]
        assert row["present_case"] is True and row["max_tool_rounds"] == 20
        r = await client.post("/api/v1/consult", json={
            "question": "q", "present_case": True, "max_tool_rounds": 7, "session_id": "x"})
        assert captured["body"]["inputs"][0]["max_tool_rounds"] == 7


@pytest.mark.parametrize("reg", [{"grants": False}, {"pack": False}])
async def test_armed_without_the_pack_is_409_and_never_runs(pool, monkeypatch, reg):
    captured: dict[str, Any] = {}
    _install_dapr(monkeypatch, nats=None, captured=captured)
    async with _client(_app(pool, None, **reg)) as client:
        r = await client.post("/api/v1/consult", json={"question": "q", "present_case": True})
    assert r.status_code == 409
    assert r.json()["detail"] == "case_unavailable: workstation_drive not bound"
    assert "body" not in captured


async def test_a_completed_case_is_projected_with_its_plane_and_persisted(pool, monkeypatch):
    record = compose_record(request_id="r", status="sealed", plane=None, refused=0,
                            summary=None, actions=[])
    envelope = {"outcome": "success", "mode": "chat", "derived_from": [],
                "consult_response": {"answer": "a", "data": {"case": record}}}
    _install_dapr(monkeypatch, nats=None, captured={}, envelope=envelope)
    async with _client(_app(pool, None)) as client:
        r = await client.post("/api/v1/consult", json={"question": "q", "present_case": True})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["case"] == {**record, "plane": body["model"]}
    turn = (await consult_persistence.load_session(pool, body["session_id"]))["turns"][-1]
    assert turn["case"] == body["case"]


async def test_stop_mid_case_persists_a_stopped_record_and_nothing_after_it(pool, monkeypatch):
    monkeypatch.setenv("LEGBA_CONSULT_SYNC_WAIT_SECONDS", "0.3")
    nats = _Nats()
    _install_dapr(monkeypatch, nats=nats, captured={}, frames=3)
    rid = uuid4().hex
    async with _client(_app(pool, nats)) as client:
        r = await client.post("/api/v1/consult", json={
            "question": "walk me", "present_case": True, "request_id": rid})
        assert r.status_code == 202, r.text
        session_id, plane = r.json()["session_id"], r.json()["model"]
        r = await client.post(f"/api/v1/consult/runs/{rid}/stop",
                              json={"executed_through_seq": 2})
        assert r.status_code == 200, r.text
        assert r.json()["stopped_after_seq"] == 2
    subject = f"legba.consult.steps.{rid}"
    kinds = [f.get("kind") or f.get("type") for s, f in nats.nc.published if s == subject]
    assert kinds[:4] == ["case_action"] * 3 + ["case_stopped"]
    terminal = [f for s, f in nats.nc.published if f.get("type") == "final"][0]
    assert terminal["case"]["status"] == "stopped"

    turn = (await consult_persistence.load_session(pool, session_id))["turns"][-1]
    assert turn["role"] == "assistant" and turn["response_recorded"] is True
    case = turn["case"]
    assert case["status"] == "stopped" and case["stopped_after_seq"] == 2
    assert [a["seq"] for a in case["actions"]] == [1, 2]  # seq 3 arrived, never recorded
    assert case["complete"] is True and case["plane"] == plane and turn["model"] == plane
    # Every measurement key reads "not measured", never a zero.
    for key in ("uncertainty", "provenance_census", "hold_reason", "watch_minted"):
        assert turn[key] is None

    # The reload ROUTE the workstation's Replay reads carries it too.
    from legba.data.registry.consult_sessions_api import build_consult_sessions_router

    app = FastAPI()
    app.include_router(
        build_consult_sessions_router(RegistryAPIDeps(
            descriptor_registry=_Registry(pool),  # type: ignore[arg-type]
            stack_registry=None, vault=None, dlq=None,  # type: ignore[arg-type]
            audit_logger=None, vocabulary_cache=None, nats_store=None,  # type: ignore[arg-type]
        )),
        prefix="/api/v1",
    )
    async with _client(app) as client:
        r = await client.get(f"/api/v1/consult/sessions/{session_id}")
    assert r.status_code == 200, r.text
    assert r.json()["turns"][-1]["case"] == case


async def test_w10_f10_a_stopped_case_persists_the_steps_that_ran_before_the_stop(pool, monkeypatch):
    """W10-F10 on the real driver — the live row (10-04 04:35Z) read
    ``stopped_after_seq: 3, actions: []``. A case stopped after step 2 whose two
    frames reach the registry only after the stop's ack persists BOTH steps,
    read back from consult_turns; seq 3 (past the claim) never lands."""
    monkeypatch.setenv("LEGBA_CONSULT_SYNC_WAIT_SECONDS", "0.3")
    nats = _Nats()
    _install_dapr(monkeypatch, nats=nats, captured={}, frames_on_cancel=3)
    rid = uuid4().hex
    async with _client(_app(pool, nats)) as client:
        r = await client.post("/api/v1/consult", json={
            "question": "walk me", "present_case": True, "request_id": rid})
        assert r.status_code == 202, r.text
        session_id = r.json()["session_id"]
        r = await client.post(f"/api/v1/consult/runs/{rid}/stop",
                              json={"executed_through_seq": 2})
        assert r.status_code == 200, r.text
        # At the ack the registry held no frame: the W5-F6 clamp says 0.
        assert r.json()["stopped_after_seq"] == 0

    # Read the persisted row back on the real driver.
    async with pool.acquire() as conn:
        raw = await conn.fetchval(
            "SELECT response->'case' FROM consult_turns "
            "WHERE session_id = $1 AND role = 'assistant' "
            "ORDER BY created_at DESC LIMIT 1",
            session_id,
        )
    case = json.loads(raw) if isinstance(raw, str) else raw
    assert case["status"] == "stopped" and case["stopped_after_seq"] == 2
    assert [a["seq"] for a in case["actions"]] == [1, 2]
    assert case["complete"] is True and case["gap_at_seq"] is None
    # Replayable without the model: every action carries its args and reads.
    assert all(a["action"] and a["args"] and a["rests_on"] for a in case["actions"])
    # The trace keeps the frame past the claim, marked.
    turn = (await consult_persistence.load_session(pool, session_id))["turns"][-1]
    assert turn["case"] == case
    third = [s for s in turn["steps"] if s.get("kind") == "case_action" and s.get("seq") == 3]
    assert third and third[0]["after_stop"] is True
