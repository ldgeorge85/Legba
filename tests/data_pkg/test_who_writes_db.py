# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Who writes (data-model lane 9) against the migrated test database.

* ``legba_principal(handle)`` creates a principal once, on first use, links a
  person to their ``users`` row and splits a seat into descriptor + version;
* every stored kind is a ``principal_kind`` declaration;
* origins: the derived assignment, idempotence, an admin correction with its
  append-only log, the corrected row never re-derived, the read function;
* the origins route: an admin's correction names the request principal;
* write sites: a person's watch (sign-in off -> the shared credential, the
  route otherwise exactly as before), a seat's journal proposal, a rule's
  collection requirement.

Every name below is invented. Container only (``migrated_pg``).
"""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import asyncpg
import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from legba.data import origins as O
from legba.data.config import PostgresConfig
from legba.data.principals import declared_kinds

_DOMAIN = "who-writes.example"


@pytest_asyncio.fixture
async def pg_pool(migrated_pg: PostgresConfig):
    pool = await asyncpg.create_pool(migrated_pg.dsn, min_size=1, max_size=4)
    yield pool
    await pool.close()


@pytest.fixture
def env_signin_off(monkeypatch):
    """Nothing from the host's .env: sign-in off, no service tokens."""
    monkeypatch.setenv("LEGBA_AUTH_SIGNIN", "0")
    monkeypatch.setenv("LEGBA_REGISTRY_SERVICE_TOKEN", "")
    for name in ("REGISTRY", "RUNTIME", "WORKER", "CONSOLE"):
        monkeypatch.delenv(f"LEGBA_SERVICE_TOKEN_{name}", raising=False)
    monkeypatch.delenv("LEGBA_ROLE_OVERRIDE_FOR_TESTS", raising=False)


async def _handle_of(conn, pid) -> str | None:
    return await conn.fetchval("SELECT handle FROM principals WHERE id = $1", pid)


# ---------------------------------------------------------------------------
# legba_principal
# ---------------------------------------------------------------------------


async def test_a_principal_is_created_once_on_first_use(pg_pool):
    async with pg_pool.acquire() as conn:
        a = await conn.fetchval("SELECT legba_principal('rule:who_writes_test_rule')")
        b = await conn.fetchval("SELECT legba_principal('rule:who_writes_test_rule')")
        assert a == b
        row = await conn.fetchrow("SELECT kind, user_id FROM principals WHERE id = $1", a)
        assert row["kind"] == "rule" and row["user_id"] is None
        assert await conn.fetchval("SELECT legba_principal(NULL)") is None


async def test_a_person_is_linked_to_their_users_row(pg_pool):
    async with pg_pool.acquire() as conn:
        uid = await conn.fetchval(
            "INSERT INTO users (email, role) VALUES ($1, 'reader') RETURNING id",
            f"reader-{uuid.uuid4().hex[:8]}@{_DOMAIN}",
        )
        try:
            pid = await conn.fetchval("SELECT legba_principal($1)", f"user:{uid}")
            assert await conn.fetchval("SELECT user_id FROM principals WHERE id = $1", pid) == uid
        finally:
            await conn.execute("DELETE FROM users WHERE id = $1", uid)
        # The principal outlives the account (history keeps its writer).
        assert await conn.fetchval("SELECT user_id FROM principals WHERE id = $1", pid) is None


async def test_a_seat_is_split_into_descriptor_and_version(pg_pool):
    async with pg_pool.acquire() as conn:
        pid = await conn.fetchval("SELECT legba_principal('agent:desk_northaven@3f9a0c')")
        row = await conn.fetchrow(
            "SELECT kind, descriptor_id, descriptor_version FROM principals WHERE id = $1", pid)
        assert tuple(row) == ("agent", "desk_northaven", "3f9a0c")


async def test_a_malformed_handle_is_refused_by_the_database(pg_pool):
    async with pg_pool.acquire() as conn:
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.fetchval("SELECT legba_principal('no colon here')")


async def test_every_stored_kind_is_declared(pg_pool):
    async with pg_pool.acquire() as conn:
        await conn.fetchval("SELECT legba_principal('shared:admin-token')")
        kinds = {r["kind"] for r in await conn.fetch("SELECT DISTINCT kind FROM principals")}
    assert kinds <= set(declared_kinds())


# ---------------------------------------------------------------------------
# Origins
# ---------------------------------------------------------------------------


def _feed(did: str, url: str) -> O.Derivation:
    return O.derive_source(did, "rss", {"scope": {"source_class": "reporting"},
                                        "config": {"url": {"raw": url}}})


async def _wipe_origins(conn, keys: list[str]) -> None:
    # The log is append-only by trigger; a test database drops its own rows
    # by disabling the trigger for the cleanup only.
    await conn.execute("ALTER TABLE origin_assignment_log DISABLE TRIGGER origin_assignment_log_append_only")
    try:
        ids = [r["id"] for r in await conn.fetch("SELECT id FROM origins WHERE key = ANY($1::text[])", keys)]
        await conn.execute("DELETE FROM origin_assignment_log WHERE origin_id = ANY($1::uuid[])", ids)
        await conn.execute("DELETE FROM origins WHERE id = ANY($1::uuid[])", ids)
    finally:
        await conn.execute("ALTER TABLE origin_assignment_log ENABLE TRIGGER origin_assignment_log_append_only")


async def test_derive_correct_and_log(pg_pool):
    a = _feed("source.ww.port", "https://www.northaven-courier.example/port.xml")
    b = _feed("source.ww.pol", "https://feeds.northaven-courier.example/pol")
    keys = [a.origin_key, b.origin_key]
    async with pg_pool.acquire() as conn:
        await _wipe_origins(conn, keys)
        try:
            first = await O.sync_derived(conn, [a, b])
            assert first["inserted"] == 2
            again = await O.sync_derived(conn, [a, b])
            assert again == {"inserted": 0, "moved": 0, "unchanged": 2, "corrected_kept": 0}

            reads = await O.origin_groups_for_sources(conn, ["source.ww.port", "source.ww.pol", "source.ww.none"])
            assert reads["source.ww.port"].group_key == reads["source.ww.pol"].group_key == "host:northaven-courier.example"
            assert reads["source.ww.none"] == O.OriginGroupRead("source:source.ww.none", None, False)

            oid = await conn.fetchval("SELECT id FROM origins WHERE key = $1", b.origin_key)
            out = await O.correct_origin(conn, oid, group_key="publisher:northaven-politics-desk",
                                         group_label="Politics desk", reason="a separate newsroom",
                                         written_by="user:0b6c2f0e-1111-4c2a-9d3e-000000000001")
            assert out["group_key"] == "publisher:northaven-politics-desk"
            row = await conn.fetchrow(
                "SELECT o.rule, o.corrected, p.handle FROM origins o JOIN principals p ON p.id = o.written_by "
                "WHERE o.id = $1", oid)
            assert (row["rule"], row["corrected"]) == ("admin_correction", True)
            assert row["handle"] == "user:0b6c2f0e-1111-4c2a-9d3e-000000000001"

            # A corrected origin is never re-derived.
            kept = await O.sync_derived(conn, [a, b])
            assert kept["corrected_kept"] == 1 and kept["moved"] == 0

            log = await conn.fetch(
                "SELECT l.rule, l.reason, p.handle FROM origin_assignment_log l "
                "JOIN principals p ON p.id = l.written_by WHERE l.origin_id = $1 ORDER BY l.id", oid)
            assert [(r["rule"], r["handle"]) for r in log] == [
                ("publisher_host", "rule:origin_derivation"),
                ("admin_correction", "user:0b6c2f0e-1111-4c2a-9d3e-000000000001"),
            ]
            with pytest.raises(asyncpg.RaiseError, match="append-only"):
                await conn.execute("UPDATE origin_assignment_log SET reason = 'x' WHERE origin_id = $1", oid)
            with pytest.raises(asyncpg.RaiseError, match="append-only"):
                await conn.execute("DELETE FROM origin_assignment_log WHERE origin_id = $1", oid)
            with pytest.raises(O.OriginCorrectionError):
                await O.correct_origin(conn, oid, group_key="no-colon", group_label=None,
                                       reason="x", written_by="shared:admin-token")
        finally:
            await _wipe_origins(conn, keys)


class _Reg:
    def __init__(self, pool) -> None:
        self.pg = pool


@pytest_asyncio.fixture
async def origins_client(pg_pool, env_signin_off, monkeypatch):
    """A bearer-configured registry with sign-in OFF: the caller is the shared
    admin credential, exactly as before sign-in."""
    from legba.data.registry.origins_api import build_origins_router

    monkeypatch.setenv("LEGBA_REGISTRY_API_TOKEN", "who-writes-test-token")
    monkeypatch.delenv("LEGBA_DEV_MODE", raising=False)
    app = FastAPI()
    app.include_router(build_origins_router(SimpleNamespace(descriptor_registry=_Reg(pg_pool))),
                       prefix="/api/v1/registry")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t",
                                 headers={"Authorization": "Bearer who-writes-test-token"}) as c:
        yield c


async def test_the_route_correction_names_the_request_principal(origins_client, pg_pool):
    d = _feed("source.ww.route", "https://route.northaven-courier.example/x")
    async with pg_pool.acquire() as conn:
        await _wipe_origins(conn, [d.origin_key])
        await O.sync_derived(conn, [d])
        oid = await conn.fetchval("SELECT id::text FROM origins WHERE key = $1", d.origin_key)
    try:
        r = await origins_client.put(f"/api/v1/registry/origins/{oid}/group",
                                     json={"group_key": "publisher:route-desk", "reason": "own newsroom"})
        assert r.status_code == 200, r.text
        r = await origins_client.get(f"/api/v1/registry/origins/{oid}/log")
        assert r.status_code == 200, r.text
        assert [(e["rule"], e["written_by"]) for e in r.json()] == [
            ("publisher_host", "rule:origin_derivation"),
            ("admin_correction", "shared:admin-token"),
        ]
        r = await origins_client.put(f"/api/v1/registry/origins/{oid}/group",
                                     json={"group_key": "nocolon", "reason": "x"})
        assert r.status_code == 422
        r = await origins_client.put(f"/api/v1/registry/origins/{uuid.uuid4()}/group",
                                     json={"group_key": "publisher:x", "reason": "x"})
        assert r.status_code == 404
        r = await origins_client.get("/api/v1/registry/origins", params={"origin_kind": "source"})
        assert r.status_code == 200 and any(o["id"] == oid for o in r.json())
    finally:
        async with pg_pool.acquire() as conn:
            await _wipe_origins(conn, [d.origin_key])


# ---------------------------------------------------------------------------
# Write sites
# ---------------------------------------------------------------------------


async def test_a_watch_with_sign_in_off_names_the_shared_credential(pg_pool, env_signin_off, monkeypatch):
    from legba.data.registry.watchlist_api import build_watchlist_router

    monkeypatch.setenv("LEGBA_REGISTRY_API_TOKEN", "who-writes-test-token")
    monkeypatch.delenv("LEGBA_DEV_MODE", raising=False)
    app = FastAPI()
    app.include_router(build_watchlist_router(SimpleNamespace(descriptor_registry=_Reg(pg_pool))),
                       prefix="/api/v1/v3")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t",
                                 headers={"Authorization": "Bearer who-writes-test-token"}) as c:
        r = await c.post("/api/v1/v3/watchlist",
                         json={"kind": "text", "pattern": {"query": "northaven harbour"}, "label": "ww"})
    assert r.status_code == 201, r.text
    body = r.json()
    try:
        # The route's answer is what it always was: created_by stays the
        # October default; the writer is recorded beside it, not in it.
        assert "written_by" not in body
        async with pg_pool.acquire() as conn:
            handle = await conn.fetchval(
                "SELECT p.handle FROM watchlist w JOIN principals p ON p.id = w.written_by WHERE w.id = $1::uuid",
                body["id"])
        assert handle == "shared:admin-token"
    finally:
        async with pg_pool.acquire() as conn:
            await conn.execute("DELETE FROM watchlist WHERE id = $1::uuid", body["id"])


async def test_a_seat_proposal_names_the_seat(pg_pool):
    from legba.data.analysts.agency.journal_propose import _insert_proposal
    from legba.data.analysts.agency.tools import WritebackContext
    from legba.data.provenance import AnalystContext

    ctx = AnalystContext(analyst_id="ww_journal_seat", analyst_version="v-ww-1", run_id=uuid.uuid4())
    wb = WritebackContext(pg_pool=pg_pool, analyst_ctx=ctx)
    pid = await _insert_proposal(wb, proposal_kind="correction", rationale="invented",
                                 diff={"op": "noop"}, cited_refs=[], requested_by=None,
                                 pack_id="journal_propose", archive_reason="unapplyable_shape")
    try:
        async with pg_pool.acquire() as conn:
            handle = await conn.fetchval(
                "SELECT p.handle FROM journal_proposals j JOIN principals p ON p.id = j.written_by WHERE j.id = $1",
                pid)
        assert handle == "agent:ww_journal_seat@v-ww-1"
    finally:
        async with pg_pool.acquire() as conn:
            await conn.execute("DELETE FROM journal_proposals WHERE id = $1", pid)


async def test_a_rule_requirement_names_the_rule(pg_pool):
    from legba.data.analysts.deterministic_handlers.collection_gap import _INSERT_REQUIREMENT_SQL

    key = f"ww:{uuid.uuid4().hex}"
    async with pg_pool.acquire() as conn:
        await conn.execute(
            _INSERT_REQUIREMENT_SQL, key, "collection_gap", None, "geo", "ww topic", "invented",
            "analyst_output", uuid.uuid4(), ["reporting"], json.dumps([]), None, False, "none", 1,
        )
        try:
            handle = await conn.fetchval(
                "SELECT p.handle FROM collection_requirements c JOIN principals p ON p.id = c.written_by "
                "WHERE c.natural_key = $1", key)
            assert handle == "rule:collection_gap"
        finally:
            await conn.execute("DELETE FROM collection_requirements WHERE natural_key = $1", key)
