# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""W11-F105 — a stack PUT whose content equals an EARLIER version re-heads it;
`StackRegistry.rollback` + `POST /registry/stack/{component_id}/rollback`.

Live 2026-10-23: `PUT /registry/stack/{id}` with a body whose content hash
named a non-head historical row 500'd on `stack_components_pkey` — the
version is content-derived and `update` always INSERTed.

No database: the registry runs over an in-memory `stack_components` /
`descriptor_audit_log` fake that answers the registry's own SQL statements
(an unknown statement fails the test loudly), with all-or-nothing
transactions and the real primary-key collision. The real `_validate` runs
(a vector-store component carries no secret, so the vault is never asked).
The same paths against Postgres are `test_stack_registry.py`'s
`test_update_*` / `test_rollback_*` integration tests (container DB).
"""

from __future__ import annotations

import copy
import re
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any

import asyncpg
import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from legba.data.registry import NullEventEmitter, StackRegistry
from legba.data.registry import route_roles as rr
from legba.data.registry.api import RegistryAPIDeps, build_router
from legba.data.registry.errors import (
    DescriptorNotFound,
    IllegalLifecycleTransition,
    VersionConflict,
)
from legba.data.schemas import LifecycleState, Property, VectorStore, VectorStoreConfig

CID = "vector.qdrant.f105"
_T0 = datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc)

_COLS = ("component_id", "version", "schema_uri", "kind", "is_head",
         "state", "owner", "name", "body", "created_at")


def _norm(sql: str) -> str:
    return re.sub(r"\s+", " ", sql).strip()


# ---------------------------------------------------------------------------
# In-memory store answering exactly the registry's statements
# ---------------------------------------------------------------------------


class _FakeDB:
    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], dict[str, Any]] = {}
        self.audit: list[dict[str, Any]] = []
        self.tick = 0
        self.statements: list[str] = []

    def now(self) -> datetime:
        self.tick += 1
        return _T0 + timedelta(seconds=self.tick)


class _FakeConn:
    def __init__(self, db: _FakeDB) -> None:
        self.db = db

    def _ret(self, r: dict[str, Any] | None) -> dict[str, Any] | None:
        return None if r is None else {k: r[k] for k in _COLS}

    def _head(self, cid: str) -> dict[str, Any] | None:
        heads = [r for (c, _), r in self.db.rows.items() if c == cid and r["is_head"]]
        assert len(heads) <= 1, "stack_components_head_unique violated"
        return heads[0] if heads else None

    async def fetchrow(self, sql: str, *a: Any) -> dict[str, Any] | None:
        s = _norm(sql)
        self.db.statements.append(s)
        rows = self.db.rows
        if s == ("SELECT version, state FROM stack_components "
                 "WHERE component_id = $1 AND is_head FOR UPDATE"):
            h = self._head(a[0])
            return None if h is None else {"version": h["version"], "state": h["state"]}
        if s == ("SELECT version, state FROM stack_components "
                 "WHERE component_id = $1 AND version = $2"):
            r = rows.get((a[0], a[1]))
            return None if r is None else {"version": r["version"], "state": r["state"]}
        if s == "SELECT version FROM stack_components WHERE component_id = $1 AND is_head":
            h = self._head(a[0])
            return None if h is None else {"version": h["version"]}
        if s.startswith("SELECT component_id, version, schema_uri") and s.endswith(
            "WHERE component_id = $1 AND is_head"
        ):
            return self._ret(self._head(a[0]))
        if s.startswith("SELECT component_id, version, schema_uri") and s.endswith(
            "WHERE component_id = $1 AND version = $2"
        ):
            return self._ret(rows.get((a[0], a[1])))
        if s.startswith("INSERT INTO stack_components"):
            if (a[0], a[1]) in rows:
                raise asyncpg.exceptions.UniqueViolationError(
                    'duplicate key value violates unique constraint "stack_components_pkey"'
                )
            if self._head(a[0]) is not None:
                raise asyncpg.exceptions.UniqueViolationError(
                    'duplicate key value violates unique constraint "stack_components_head_unique"'
                )
            r = dict(zip(_COLS, (a[0], a[1], a[2], a[3], True, a[4], a[5], a[6],
                                 a[7], self.db.now())))
            rows[(a[0], a[1])] = r
            return self._ret(r)
        if s.startswith("UPDATE stack_components SET is_head = TRUE, state = $3, "
                        "owner = $4, name = $5, body = $6::jsonb "
                        "WHERE component_id = $1 AND version = $2 RETURNING"):
            r = rows.get((a[0], a[1]))
            if r is None:
                return None
            r.update(is_head=True, state=a[2], owner=a[3], name=a[4], body=a[5])
            return self._ret(r)
        if s.startswith("UPDATE stack_components SET is_head = TRUE "
                        "WHERE component_id = $1 AND version = $2 RETURNING"):
            r = rows.get((a[0], a[1]))
            if r is None:
                return None
            r["is_head"] = True
            return self._ret(r)
        if s.startswith("UPDATE stack_components SET state = $2 "
                        "WHERE component_id = $1 AND is_head RETURNING"):
            h = self._head(a[0])
            if h is None:
                return None
            h["state"] = a[1]
            return self._ret(h)
        raise AssertionError(f"unexpected fetchrow: {s}")

    async def execute(self, sql: str, *a: Any) -> str:
        s = _norm(sql)
        self.db.statements.append(s)
        if s == "UPDATE stack_components SET is_head = FALSE WHERE component_id = $1 AND is_head":
            n = 0
            for (c, _), r in self.db.rows.items():
                if c == a[0] and r["is_head"]:
                    r["is_head"] = False
                    n += 1
            return f"UPDATE {n}"
        raise AssertionError(f"unexpected execute: {s}")

    async def fetchval(self, sql: str, *a: Any) -> Any:
        s = _norm(sql)
        self.db.statements.append(s)
        if s.startswith("SELECT from_version FROM descriptor_audit_log"):
            assert "action <> 'rollback'" in s
            ns, cid, to_v = a
            hits = [
                e for e in self.db.audit
                if e["namespace"] == ns and e["descriptor_id"] == cid
                and e["to_version"] == to_v and e["action"] != "rollback"
                and e["from_version"] is not None and e["from_version"] != e["to_version"]
            ]
            hits.sort(key=lambda e: e["seq"], reverse=True)
            return hits[0]["from_version"] if hits else None
        raise AssertionError(f"unexpected fetchval: {s}")


class _FakeStore:
    def __init__(self) -> None:
        self.db = _FakeDB()

    @asynccontextmanager
    async def acquire(self):
        yield _FakeConn(self.db)

    @asynccontextmanager
    async def transaction(self):
        snapshot = (copy.deepcopy(self.db.rows), list(self.db.audit))
        try:
            yield _FakeConn(self.db)
        except BaseException:
            self.db.rows, self.db.audit = snapshot
            raise


class _FakeAudit:
    async def record(self, conn: _FakeConn, **kw: Any) -> None:
        kw["seq"] = len(conn.db.audit)
        conn.db.audit.append(kw)


class _NoVault:
    async def verify_exists(self, sid: str) -> bool:  # pragma: no cover
        raise AssertionError("a vector-store component carries no secret")


@pytest.fixture
def env():
    store = _FakeStore()
    emitter = NullEventEmitter()
    reg = StackRegistry(
        store=store, vault=_NoVault(),  # type: ignore[arg-type]
        audit=_FakeAudit(), emitter=emitter,  # type: ignore[arg-type]
        dlq=object(), health=object(),  # type: ignore[arg-type]
    )
    return reg, store.db, emitter


def _comp(prefix: str, *, state: LifecycleState = LifecycleState.DRAFT) -> VectorStore:
    return VectorStore(
        id=CID,
        name="Qdrant F105",
        schema_uri="legba/stack/vector_store/1.0.0",
        version="0" * 16,
        owner="ops@local",
        state=state,
        config=VectorStoreConfig(
            endpoint=Property.Text.of("http://127.0.0.1:6333"),
            collection_prefix=Property.Text.of(prefix),
        ),
    )


async def _history(reg: StackRegistry) -> list[str]:
    """Register a, update b, update c — returns [va, vb, vc]."""
    a = await reg.register(_comp("a"), actor="ops")
    b = await reg.update(CID, _comp("b"), actor="ops")
    c = await reg.update(CID, _comp("c"), actor="ops")
    assert len({a.version, b.version, c.version}) == 3
    return [a.version, b.version, c.version]


def _heads(db: _FakeDB) -> list[str]:
    return [v for (c, v), r in db.rows.items() if c == CID and r["is_head"]]


# ---------------------------------------------------------------------------
# update — re-head / conflict / insert
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_put_of_an_earlier_versions_content_reheads_that_row(env):
    reg, db, emitter = env
    va, vb, vc = await _history(reg)
    created_a = db.rows[(CID, va)]["created_at"]
    emitter.clear()

    row = await reg.update(CID, _comp("a"), actor="ops2")

    assert row.version == va and row.is_head
    assert _heads(db) == [va]
    assert len(db.rows) == 3, "no new row: the historical one is re-used"
    assert db.rows[(CID, va)]["created_at"] == created_a, "created_at is never rewritten"
    assert db.rows[(CID, vc)]["is_head"] is False
    # One audit entry, re_head, from the demoted head to the re-headed row.
    last = db.audit[-1]
    assert last["action"] == "re_head"
    assert (last["from_version"], last["to_version"]) == (vc, va)
    assert last["actor_id"] == "ops2" and last["namespace"] == "stack"
    assert last["change_summary"]["head_shift"] is True
    assert last["change_summary"]["kind"] == "vector_store"
    # Consumers see an update (the handler cache evicts on it).
    assert emitter.subjects() == [f"stack.component.updated.vector_store.{CID}"]
    assert emitter.published[0][1]["from_version"] == vc
    # The re-head is one transaction: lock head, probe, demote, promote, audit.
    tx = [s for s in db.statements if "stack_components" in s][-3:]
    assert tx[0].startswith("SELECT version, state FROM stack_components WHERE component_id = $1 AND version = $2")
    assert tx[1].startswith("UPDATE stack_components SET is_head = FALSE")
    assert tx[2].startswith("UPDATE stack_components SET is_head = TRUE, state = $3")


@pytest.mark.asyncio
async def test_the_old_path_would_have_hit_the_primary_key(env):
    """The fake models the live 500: inserting an existing (id, version)."""
    reg, db, _ = env
    va, _, _ = await _history(reg)
    conn = _FakeConn(db)
    with pytest.raises(asyncpg.exceptions.UniqueViolationError, match="stack_components_pkey"):
        await conn.fetchrow(
            "INSERT INTO stack_components (component_id, version) VALUES ($1, $2)",
            CID, va, "u", "k", "draft", "o", "n", "{}",
        )


@pytest.mark.asyncio
async def test_rehead_reasserts_state_a_retire_flipped_in_place(env):
    reg, db, _ = env
    a = await reg.register(_comp("a", state=LifecycleState.ACTIVE), actor="ops")
    await reg.retire(CID, actor="ops")  # state flips on the row, version kept
    await reg.update(CID, _comp("b", state=LifecycleState.ACTIVE), actor="ops")
    assert db.rows[(CID, a.version)]["state"] == "retired"

    row = await reg.update(CID, _comp("a", state=LifecycleState.ACTIVE), actor="ops")

    assert row.version == a.version and row.state == LifecycleState.ACTIVE
    assert db.audit[-1]["change_summary"]["state_reasserted_from"] == "retired"


@pytest.mark.asyncio
async def test_put_matching_the_current_head_is_still_a_version_conflict(env):
    reg, db, emitter = env
    _, _, vc = await _history(reg)
    n_audit = len(db.audit)
    emitter.clear()
    with pytest.raises(VersionConflict, match="matches head version"):
        await reg.update(CID, _comp("c"), actor="ops")
    assert _heads(db) == [vc] and len(db.audit) == n_audit and not emitter.published


@pytest.mark.asyncio
async def test_fresh_content_inserts_as_before(env):
    reg, db, emitter = env
    va, vb, vc = await _history(reg)
    emitter.clear()
    row = await reg.update(CID, _comp("d"), actor="ops")
    assert row.version not in (va, vb, vc) and row.is_head
    assert len(db.rows) == 4 and _heads(db) == [row.version]
    assert db.audit[-1]["action"] == "update"
    assert "head_shift" not in db.audit[-1]["change_summary"]
    assert emitter.subjects() == [f"stack.component.updated.vector_store.{CID}"]


@pytest.mark.asyncio
async def test_update_refuses_when_the_head_moved_under_it(env, monkeypatch):
    reg, db, _ = env
    va, vb, vc = await _history(reg)
    real_get = reg.get

    async def stale_get(component_id, version=None):
        row = await real_get(component_id, version=version)
        return row if version else await real_get(component_id, version=vb)

    monkeypatch.setattr(reg, "get", stale_get)
    with pytest.raises(VersionConflict, match="head moved"):
        await reg.update(CID, _comp("d"), actor="ops")
    assert _heads(db) == [vc] and len(db.rows) == 3


# ---------------------------------------------------------------------------
# rollback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rollback_to_previous_then_walks_back(env):
    reg, db, emitter = env
    va, vb, vc = await _history(reg)
    emitter.clear()

    row = await reg.rollback(CID, "ops", reason="regression")
    assert row.version == vb and _heads(db) == [vb]
    entry = db.audit[-1]
    assert entry["action"] == "rollback"
    assert (entry["from_version"], entry["to_version"]) == (vc, vb)
    assert entry["change_summary"] == {
        "from_version": vc, "to_version": vb, "target": "previous", "reason": "regression",
    }
    assert emitter.subjects() == [f"stack.component.rolled_back.vector_store.{CID}"]
    assert emitter.published[0][1]["from_version"] == vc

    # A second rollback walks back to a, not forth to c.
    row = await reg.rollback(CID, "ops")
    assert row.version == va and _heads(db) == [va]
    # a was only ever reached by register: nothing before it.
    with pytest.raises(VersionConflict, match="no previous version"):
        await reg.rollback(CID, "ops")
    assert _heads(db) == [va]


@pytest.mark.asyncio
async def test_rollback_after_a_rehead_undoes_the_put(env):
    reg, db, _ = env
    va, vb, vc = await _history(reg)
    await reg.update(CID, _comp("a"), actor="ops")  # re-head a over c
    row = await reg.rollback(CID, "ops")
    assert row.version == vc


@pytest.mark.asyncio
async def test_rollback_to_a_named_version(env):
    reg, db, _ = env
    va, vb, vc = await _history(reg)
    row = await reg.rollback(CID, "ops", target_version=va)
    assert row.version == va and row.is_head and _heads(db) == [va]
    assert db.audit[-1]["change_summary"]["target"] == "named"
    assert "reason" not in db.audit[-1]["change_summary"]


@pytest.mark.asyncio
async def test_rollback_errors(env):
    reg, db, emitter = env
    with pytest.raises(DescriptorNotFound):
        await reg.rollback("vector.nope", "ops")

    va, vb, vc = await _history(reg)
    n_audit = len(db.audit)
    emitter.clear()
    with pytest.raises(VersionConflict, match="already the head"):
        await reg.rollback(CID, "ops", target_version=vc)
    with pytest.raises(VersionConflict, match="does not exist"):
        await reg.rollback(CID, "ops", target_version="f" * 64)
    assert _heads(db) == [vc] and len(db.audit) == n_audit and not emitter.published


@pytest.mark.asyncio
async def test_rollback_refuses_a_retired_head_and_a_retired_target(env):
    reg, db, _ = env
    va, vb, vc = await _history(reg)
    await reg.retire(CID, actor="ops")
    with pytest.raises(IllegalLifecycleTransition):
        await reg.rollback(CID, "ops")
    assert _heads(db) == [vc]

    # A demoted row that a retire flipped in place is not a rollback target.
    await reg.update(CID, _comp("d"), actor="ops")
    assert db.rows[(CID, vc)]["state"] == "retired"
    with pytest.raises(IllegalLifecycleTransition):
        await reg.rollback(CID, "ops", target_version=vc)
    # ...while a live one is.
    assert (await reg.rollback(CID, "ops", target_version=vb)).version == vb


# ---------------------------------------------------------------------------
# The route + its role class
# ---------------------------------------------------------------------------


class _RouteStack:
    def __init__(self, exc: Exception | None = None) -> None:
        self.calls: list[tuple] = []
        self.exc = exc

    async def rollback(self, component_id, actor, *, target_version=None, reason=None):
        self.calls.append((component_id, target_version, reason))
        if self.exc is not None:
            raise self.exc
        from legba.data.registry.stack import StackComponentRow
        return StackComponentRow(
            component_id=component_id, version=target_version or "b" * 64,
            schema_uri="legba/stack/vector_store/1.0.0", kind="vector_store",
            is_head=True, state=LifecycleState.DRAFT, owner="o", name="n",
            body={}, created_at=_T0,
        )


def _client(stack: _RouteStack, monkeypatch) -> TestClient:
    monkeypatch.delenv("LEGBA_REGISTRY_API_TOKEN", raising=False)
    monkeypatch.delenv("LEGBA_REGISTRY_SERVICE_TOKEN", raising=False)
    deps = RegistryAPIDeps(
        descriptor_registry=None, stack_registry=stack,  # type: ignore[arg-type]
        vault=None, dlq=None, audit_logger=None,  # type: ignore[arg-type]
        vocabulary_cache=None, nats_store=None, conversion_registry=None,  # type: ignore[arg-type]
    )
    app = FastAPI()
    app.state.registry_deps = deps
    app.include_router(build_router(deps), prefix="/api/v1/registry")
    return TestClient(app)


def test_rollback_route_previous_named_and_alias(monkeypatch):
    stack = _RouteStack()
    c = _client(stack, monkeypatch)
    url = f"/api/v1/registry/stack/{CID}/rollback"
    r = c.post(url)
    assert r.status_code == 200 and r.json()["version"] == "b" * 64
    r = c.post(url, json={"version": "a" * 64, "reason": "regression"})
    assert r.status_code == 200 and r.json()["version"] == "a" * 64
    r = c.post(url, json={"target_version": "c" * 64})
    assert r.status_code == 200
    assert stack.calls == [
        (CID, None, None), (CID, "a" * 64, "regression"), (CID, "c" * 64, None),
    ]


@pytest.mark.parametrize("exc,code", [
    (DescriptorNotFound("stack", CID), 404),
    (VersionConflict("already the head"), 409),
    (IllegalLifecycleTransition("retired", "rollback"), 409),
])
def test_rollback_route_error_mapping(monkeypatch, exc, code):
    c = _client(_RouteStack(exc), monkeypatch)
    r = c.post(f"/api/v1/registry/stack/{CID}/rollback", json={})
    assert r.status_code == code


def test_rollback_route_is_classed_with_the_stack_put():
    put = ("PUT", "/api/v1/registry/stack/{component_id}")
    rollback = ("POST", "/api/v1/registry/stack/{component_id}/rollback")
    assert rr.ROUTE_ROLES[rollback] == rr.ROUTE_ROLES[put] == rr.ROLE_OPERATOR
    assert rr.required_role_for("POST", f"/api/v1/registry/stack/{CID}/rollback") == (
        rr.ROLE_OPERATOR
    )
