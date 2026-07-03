# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Piece 1 (T6) — registry consult endpoint chat-vs-deep branch.

Covers:
  * chat-mode actor envelope ⇒ endpoint returns the projected ConsultResponse
    with finding_id=None and DOES NOT touch the DB read-back (the stub pg
    raises if acquired).
  * deep-mode actor envelope ⇒ the existing read-back path runs and returns the
    persisted answer + finding_id.
  * the invoke body carries mode + request_id + messages.

The Dapr sidecar HTTP call is stubbed (no real sidecar); the substrate
read-back is a typed stub (chat must never reach it).
"""

from __future__ import annotations

import json
import os
from typing import Any

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

import legba.data.registry.consult_api as consult_api
from legba.data.registry.api import RegistryAPIDeps
from legba.data.registry.consult_api import build_consult_router


API_TOKEN_ENV = "LEGBA_REGISTRY_API_TOKEN"


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


class _Row(dict):
    """asyncpg-style row: supports __getitem__ by column name."""


class _NoReadBackPg:
    """A pg pool whose acquire() blows up — proves the chat path never reads
    back from analyst_outputs."""

    def acquire(self):  # noqa: D401 — must be awaitable-context, but never used
        raise AssertionError("chat path must not read back from the DB")


class _ReadBackPg:
    """A pg pool whose acquire() yields a connection returning a canned row."""

    def __init__(self, row: dict[str, Any] | None) -> None:
        self._row = row

    def acquire(self):
        row = self._row

        class _Ctx:
            async def __aenter__(self_inner):
                class _Conn:
                    async def fetchrow(self, *args, **kwargs):
                        return row

                return _Conn()

            async def __aexit__(self_inner, *exc):
                return False

        return _Ctx()


class _DescRow:
    version = "v" + "a" * 16


class _DescriptorRegistry:
    def __init__(self, pg: Any) -> None:
        self.pg = pg

    async def get(self, descriptor_id, *, family, version=None):
        return _DescRow()


def _stub_dapr(actor_envelope: dict[str, Any], captured: dict[str, Any]):
    """Patch consult_api.httpx.AsyncClient with a fake that records the invoke
    body and returns ``actor_envelope`` as the JSON response."""

    class _Resp:
        status_code = 200

        def json(self):
            return actor_envelope

        @property
        def text(self):
            return json.dumps(actor_envelope)

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def put(self, url, json=None, headers=None):
            captured["url"] = url
            captured["body"] = json
            return _Resp()

    return _Client


def _build_app(pg: Any) -> FastAPI:
    os.environ.pop(API_TOKEN_ENV, None)  # dev mode — token optional
    deps = RegistryAPIDeps(
        descriptor_registry=_DescriptorRegistry(pg),  # type: ignore[arg-type]
        stack_registry=None,  # type: ignore[arg-type]
        vault=None,  # type: ignore[arg-type]
        dlq=None,  # type: ignore[arg-type]
        audit_logger=None,  # type: ignore[arg-type]
        vocabulary_cache=None,  # type: ignore[arg-type]
        nats_store=None,
    )
    app = FastAPI()
    app.include_router(build_consult_router(deps), prefix="/api/v1")
    return app


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_chat_envelope_no_readback(monkeypatch):
    envelope = {
        "outcome": "success",
        "mode": "chat",
        "consult_response": {
            "question": "What touches Brazil?",
            "answer": "Two recent signals.",
            "uncertainty": 0.2,
            "cited_substrate_refs": [
                "11111111-1111-1111-1111-111111111111",
            ],
            "unanswered_aspects": [],
            "data": {"tool_calls": [{"tool": "search_signals", "args": {}}]},
        },
        "derived_from": ["11111111-1111-1111-1111-111111111111"],
    }
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        consult_api.httpx, "AsyncClient", _stub_dapr(envelope, captured)
    )

    app = _build_app(_NoReadBackPg())
    client = TestClient(app)
    r = client.post(
        "/api/v1/consult",
        json={
            "question": "What touches Brazil?",
            "mode": "chat",
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["finding_id"] is None
    assert body["answer"] == "Two recent signals."
    assert body["uncertainty"] == 0.2
    assert body["cited_refs"] == [
        {"kind": "signal", "id": "11111111-1111-1111-1111-111111111111",
         "description": None},
    ]
    assert body["tool_calls"][0]["tool"] == "search_signals"

    # The invoke body carried mode + a minted request_id + messages.
    sent = captured["body"]["inputs"][0]
    assert sent["mode"] == "chat"
    assert sent["request_id"]
    assert sent["messages"] == [{"role": "user", "content": "hi"}]


def test_chat_request_id_passthrough(monkeypatch):
    envelope = {
        "outcome": "success",
        "mode": "chat",
        "consult_response": {"question": "q", "answer": "a"},
        "derived_from": [],
    }
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        consult_api.httpx, "AsyncClient", _stub_dapr(envelope, captured)
    )
    app = _build_app(_NoReadBackPg())
    client = TestClient(app)
    r = client.post(
        "/api/v1/consult",
        json={"question": "q", "mode": "chat", "request_id": "fixed-123"},
    )
    assert r.status_code == 200
    assert captured["body"]["inputs"][0]["request_id"] == "fixed-123"


def test_deep_envelope_reads_back(monkeypatch):
    fid = "22222222-2222-2222-2222-222222222222"
    envelope = {
        "outcome": "success",
        "finding_id": fid,
        "output_id": fid,
        "kind": "finding",
        "derived_from": [],
        "receipt_hash": "rh",
    }
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        consult_api.httpx, "AsyncClient", _stub_dapr(envelope, captured)
    )

    row = _Row(
        id=fid,
        kind="finding",
        title="t",
        body="persisted answer",
        data=json.dumps(
            {
                "consult_response": {
                    "question": "q",
                    "answer": "persisted answer",
                    "uncertainty": 0.4,
                    "cited_substrate_refs": [],
                    "unanswered_aspects": [],
                    "data": {},
                }
            }
        ),
    )
    app = _build_app(_ReadBackPg(row))
    client = TestClient(app)
    r = client.post(
        "/api/v1/consult",
        json={"question": "q", "mode": "deep"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["finding_id"] == fid
    assert body["answer"] == "persisted answer"
    assert body["receipt_hash"] == "rh"
    # The invoke body carried mode=deep.
    assert captured["body"]["inputs"][0]["mode"] == "deep"


def test_default_mode_is_chat(monkeypatch):
    """Omitting ``mode`` at the HTTP boundary defaults to chat (safe: no row)."""
    envelope = {
        "outcome": "success",
        "mode": "chat",
        "consult_response": {"question": "q", "answer": "a"},
        "derived_from": [],
    }
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        consult_api.httpx, "AsyncClient", _stub_dapr(envelope, captured)
    )
    app = _build_app(_NoReadBackPg())
    client = TestClient(app)
    r = client.post("/api/v1/consult", json={"question": "q"})
    assert r.status_code == 200
    assert captured["body"]["inputs"][0]["mode"] == "chat"


# ---------------------------------------------------------------------------
# S8-T6 — the ReAct step trace threads into consult_turns.steps
# ---------------------------------------------------------------------------


def test_steps_from_payload_lifts_trace():
    """The full trace stashed on ``consult_response.data['steps']`` is lifted
    for persistence; a missing/malformed data bag yields an empty list."""
    steps = [{"phase": "plan", "kind": "render_prompt"}]
    assert consult_api._steps_from_payload({"data": {"steps": steps}}) == steps
    assert consult_api._steps_from_payload({"data": {}}) == []
    assert consult_api._steps_from_payload({}) == []
    assert consult_api._steps_from_payload({"data": {"steps": "nope"}}) == []


@pytest.mark.asyncio
async def test_persist_assistant_turn_threads_steps(monkeypatch):
    """``_persist_assistant_turn`` forwards the ReAct ``steps`` trace to
    ``consult_persistence.append_turn`` so ``consult_turns.steps`` is populated
    (previously it was always empty because steps were never threaded)."""
    captured: dict[str, Any] = {}

    async def _fake_append_turn(pg, **kwargs):
        captured.update(kwargs)
        return "turn-1"

    monkeypatch.setattr(
        consult_api.consult_persistence, "append_turn", _fake_append_turn,
    )
    resp = consult_api.ConsultResponse(
        answer="a",
        finding_id="f-1",
        tool_calls=[consult_api.ConsultToolCall(
            tool="search_signals", args={}, result={"count": 1},
        )],
        cited_refs=[consult_api.ConsultCitedRef(kind="signal", id="ref-1")],
    )
    steps = [
        {"phase": "plan", "kind": "render_prompt"},
        {"phase": "act", "kind": "tool_call", "tool": "search_signals"},
    ]
    await consult_api._persist_assistant_turn(
        object(), "sess-1", resp, steps=steps,
    )
    assert captured["session_id"] == "sess-1"
    assert captured["role"] == "assistant"
    assert captured["steps"] == steps
    assert captured["finding_id"] == "f-1"


@pytest.mark.asyncio
async def test_persist_assistant_turn_no_session_is_noop(monkeypatch):
    """No session id ⇒ no append_turn call (best-effort audit; the answer is
    unaffected)."""
    called = False

    async def _fake_append_turn(pg, **kwargs):
        nonlocal called
        called = True
        return None

    monkeypatch.setattr(
        consult_api.consult_persistence, "append_turn", _fake_append_turn,
    )
    resp = consult_api.ConsultResponse(answer="a")
    await consult_api._persist_assistant_turn(object(), None, resp, steps=[{"x": 1}])
    assert called is False
