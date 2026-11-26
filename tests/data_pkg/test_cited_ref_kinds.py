# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Cited refs typed by the table their id is in.

A Consult citation once had a ref that said ``signal`` for a
situation id; the Record sheet asked ``/lineage/signal/<id>`` and got a 404.
These tests run without a database: the pool is a fake that answers the one
batched ``id = ANY`` lookup per table.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from legba.data.registry import cited_ref_kinds as ck
from legba.data.registry import consult_persistence
from legba.data.registry import consult_sessions_api
from legba.data.registry.api import RegistryAPIDeps
from legba.data.registry.consult_api import ConsultCitedRef

SIT = "328eb202-681a-4a39-a38e-ca387fb55e37"
SIG = "11111111-1111-1111-1111-111111111111"
HYP = "44444444-4444-4444-4444-444444444444"
GONE = "33333333-3333-3333-3333-333333333333"


class _Pool:
    def __init__(self, tables: dict[str, list[str]], *, fail: bool = False) -> None:
        self.tables = tables
        self.fail = fail
        self.queries: list[str] = []

    def acquire(self) -> Any:
        pool = self

        class _Conn:
            async def fetch(self, sql: str, ids: list[Any]) -> list[dict[str, Any]]:
                if pool.fail:
                    raise RuntimeError("db down")
                table = sql.split(" FROM ")[1].split()[0]
                pool.queries.append(table)
                kind = sql.split("SELECT id, ")[1].split(" AS k")[0].strip("'")
                held = set(pool.tables.get(table, []))
                return [{"id": i, "k": "finding" if kind == "kind" else kind} for i in ids if str(i) in held]

        class _Ctx:
            async def __aenter__(self) -> Any:
                return _Conn()

            async def __aexit__(self, *exc: Any) -> bool:
                return False

        return _Ctx()


TABLES = {"situations": [SIT], "signals": [SIG], "hypotheses": [HYP]}


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def test_placeholder_and_bare_refs_are_typed_by_their_table() -> None:
    pool = _Pool(TABLES)
    out = _run(ck.typed_refs(pool, [
        {"kind": "signal", "id": SIT, "description": None},
        SIG,  # the deep transport stores bare ids
        {"kind": "signal", "id": HYP, "description": "a thesis"},
        {"kind": "signal", "id": GONE, "description": None},
        {"kind": "finding", "id": GONE, "description": None},  # a writer's own kind is kept
        {"note": "no id"},  # not a ref: passed through, length kept
    ]))
    assert [(r.get("kind"), r.get("id")) for r in out] == [
        ("situation", SIT), ("signal", SIG), ("hypothesis", HYP),
        (ck.UNRESOLVED, GONE), ("finding", GONE), (None, None),
    ]
    assert out[2]["description"] == "a thesis"
    # one query per substrate table, whatever the number of refs
    assert len(pool.queries) == len(set(pool.queries))


def test_pydantic_refs_come_back_as_the_same_model() -> None:
    out = _run(ck.typed_refs(_Pool(TABLES), [ConsultCitedRef(kind="signal", id=SIT)]))
    assert isinstance(out[0], ConsultCitedRef) and out[0].kind == "situation"


def test_a_failed_lookup_leaves_the_refs_as_they_were() -> None:
    refs = [ConsultCitedRef(kind="signal", id=SIT)]
    assert _run(ck.typed_refs(_Pool(TABLES, fail=True), refs)) == refs


def test_no_lookup_when_every_ref_already_has_its_own_kind() -> None:
    pool = _Pool(TABLES)
    _run(ck.typed_refs(pool, [{"kind": "finding", "id": SIT}]))
    assert pool.queries == []


class _DescriptorRegistry:
    def __init__(self, pg: Any) -> None:
        self.pg = pg


def test_session_read_types_stored_refs_in_one_lookup(monkeypatch: Any) -> None:
    """Turns stored before the fix (all ``signal``, or bare ids from the deep
    transport) read back with their true kinds."""
    os.environ.pop("LEGBA_REGISTRY_API_TOKEN", None)
    os.environ["LEGBA_DEV_MODE"] = "1"
    session = {
        "id": "s1", "mode": "chat", "title": "t",
        "turns": [
            {"id": "u", "role": "user", "content": "q", "cited_refs": []},
            {"id": "a1", "role": "assistant", "content": "x",
             "cited_refs": [{"kind": "signal", "id": SIT, "description": None}]},
            {"id": "a2", "role": "assistant", "content": "y", "cited_refs": [SIG, GONE]},
        ],
    }

    async def _load(pg: Any, sid: str) -> dict[str, Any]:
        return session

    async def _owner(*a: Any, **k: Any) -> None:
        return None

    monkeypatch.setattr(consult_persistence, "load_session", _load)
    monkeypatch.setattr(consult_sessions_api._session_auth, "require_consult_session_owner", _owner)
    pool = _Pool(TABLES)
    deps = RegistryAPIDeps(
        descriptor_registry=_DescriptorRegistry(pool),  # type: ignore[arg-type]
        stack_registry=None,  # type: ignore[arg-type]
        vault=None,  # type: ignore[arg-type]
        dlq=None,  # type: ignore[arg-type]
        audit_logger=None,  # type: ignore[arg-type]
        vocabulary_cache=None,  # type: ignore[arg-type]
        nats_store=None,
    )
    app = FastAPI()
    app.include_router(consult_sessions_api.build_consult_sessions_router(deps), prefix="/api/v1")
    r = TestClient(app).get("/api/v1/consult/sessions/s1")
    assert r.status_code == 200, r.text
    turns = r.json()["turns"]
    assert turns[0]["cited_refs"] == []
    assert [(c["kind"], c["id"]) for c in turns[1]["cited_refs"]] == [("situation", SIT)]
    assert [(c["kind"], c["id"]) for c in turns[2]["cited_refs"]] == [("signal", SIG), (ck.UNRESOLVED, GONE)]
    assert len(pool.queries) == len(set(pool.queries))
