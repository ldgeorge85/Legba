# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""PIECE 4 — registry deep-consult submit + status route tests.

Covers (per PLAN_DEEP_CONSULT_WORKFLOW.md §9):

  * Submit returns 202 + task_id IMMEDIATELY (the actor schedules + returns;
    no blocking on completion) and the invoke body carries the question +
    run_id options.
  * Status mapping: a produced finding row → ``completed`` with finding_id +
    lineage; absent row → ``running``.
  * Auth: with a configured token, a missing bearer → 401.

The Dapr sidecar HTTP call is stubbed (no real sidecar); the status read is a
typed pg stub.
"""

from __future__ import annotations

import json
import os
from typing import Any
from uuid import uuid4

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

import legba.data.registry.deep_consult_api as deep_api
from legba.data.registry.api import RegistryAPIDeps
from legba.data.registry.deep_consult_api import build_deep_consult_router


API_TOKEN_ENV = "LEGBA_REGISTRY_API_TOKEN"


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


class _DescRow:
    version = "v" + "a" * 16


class _DescriptorRegistry:
    def __init__(self, pg: Any) -> None:
        self.pg = pg

    async def get(self, descriptor_id, *, family, version=None):
        return _DescRow()


class _StatusPg:
    """pg pool whose acquire() yields a connection returning a canned row."""

    def __init__(self, row: dict[str, Any] | None) -> None:
        self._row = row
        self.queries: list[tuple[str, tuple[Any, ...]]] = []

    def acquire(self):
        outer = self

        class _Ctx:
            async def __aenter__(self_inner):
                class _Conn:
                    async def fetchrow(self, sql, *args):
                        outer.queries.append((sql, args))
                        return outer._row

                return _Conn()

            async def __aexit__(self_inner, *exc):
                return False

        return _Ctx()


def _stub_dapr(actor_envelope: dict[str, Any], captured: dict[str, Any],
               *, status_code: int = 200):
    class _Resp:
        def __init__(self) -> None:
            self.status_code = status_code

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
    os.environ.pop(API_TOKEN_ENV, None)
    os.environ["LEGBA_DEV_MODE"] = "1"
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
    app.include_router(build_deep_consult_router(deps), prefix="/api/v1")
    return app


# ---------------------------------------------------------------------------
# Submit
# ---------------------------------------------------------------------------


def test_submit_returns_202_and_task_id(monkeypatch):
    envelope = {
        "outcome": "success",
        "mode": "deep_consult",
        "task_id": "deep_consult.global.abcd1234",
        "status": "running",
        "run_id": "abcd1234-0000-0000-0000-000000000000",
    }
    captured: dict[str, Any] = {}
    monkeypatch.setattr(deep_api.httpx, "AsyncClient", _stub_dapr(envelope, captured))

    app = _build_app(_StatusPg(None))
    client = TestClient(app)
    r = client.post(
        "/api/v1/deep_consult",
        json={"question": "What is the state of Brazil's grid?"},
    )
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["task_id"] == "deep_consult.global.abcd1234"
    assert body["status"] == "running"

    # The invoke body carried the question + a run_id option.
    sent = captured["body"]
    assert sent["inputs"][0]["question"].startswith("What is the state")
    assert sent["options"]["run_id"]


def test_submit_404_when_descriptor_absent(monkeypatch):
    from legba.data.registry.errors import DescriptorNotFound

    class _MissingRegistry(_DescriptorRegistry):
        async def get(self, descriptor_id, *, family, version=None):
            raise DescriptorNotFound("deep_consult", "head")

    envelope: dict[str, Any] = {}
    captured: dict[str, Any] = {}
    monkeypatch.setattr(deep_api.httpx, "AsyncClient", _stub_dapr(envelope, captured))

    os.environ.pop(API_TOKEN_ENV, None)
    os.environ["LEGBA_DEV_MODE"] = "1"
    deps = RegistryAPIDeps(
        descriptor_registry=_MissingRegistry(_StatusPg(None)),  # type: ignore[arg-type]
        stack_registry=None, vault=None, dlq=None, audit_logger=None,  # type: ignore[arg-type]
        vocabulary_cache=None, nats_store=None,  # type: ignore[arg-type]
    )
    app = FastAPI()
    app.include_router(build_deep_consult_router(deps), prefix="/api/v1")
    r = TestClient(app).post("/api/v1/deep_consult", json={"question": "q"})
    assert r.status_code == 404, r.text


# ---------------------------------------------------------------------------
# Status mapping
# ---------------------------------------------------------------------------


def test_status_running_when_no_row():
    pg = _StatusPg(None)
    app = _build_app(pg)
    client = TestClient(app)
    r = client.get("/api/v1/deep_consult/deep_consult.global.abcd1234")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "running"
    assert body["finding_id"] is None


def test_status_completed_with_finding_and_lineage():
    finding_id = str(uuid4())
    ref = str(uuid4())
    row = {
        "id": finding_id,
        "body": "Brazil's grid is strained.",
        "data": {"deep_consult": {"uncertainty": 0.3}},
        "derived_from": [ref],
    }
    pg = _StatusPg(row)
    app = _build_app(pg)
    client = TestClient(app)
    r = client.get("/api/v1/deep_consult/deep_consult.global.abcd1234")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "completed"
    assert body["finding_id"] == finding_id
    assert body["answer"].startswith("Brazil")
    assert body["uncertainty"] == pytest.approx(0.3)
    assert body["cited_refs"] == [ref]
    # The status query matched on the run_id prefix (run8) parsed from task_id.
    assert pg.queries
    _sql, args = pg.queries[0]
    assert args[1] == "abcd1234%"


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def test_submit_requires_bearer_when_token_configured(monkeypatch):
    monkeypatch.setenv(API_TOKEN_ENV, "s3cret")
    monkeypatch.delenv("LEGBA_DEV_MODE", raising=False)
    deps = RegistryAPIDeps(
        descriptor_registry=_DescriptorRegistry(_StatusPg(None)),  # type: ignore[arg-type]
        stack_registry=None, vault=None, dlq=None, audit_logger=None,  # type: ignore[arg-type]
        vocabulary_cache=None, nats_store=None,  # type: ignore[arg-type]
    )
    app = FastAPI()
    app.include_router(build_deep_consult_router(deps), prefix="/api/v1")
    r = TestClient(app).post("/api/v1/deep_consult", json={"question": "q"})
    assert r.status_code == 401, r.text
