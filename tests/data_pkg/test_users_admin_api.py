# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The console's Users page routes (``/api/v1/auth/admin/*``).

Through the REAL app on the migrated test DB, sign-in switch ON:
  * a reader and a contributor are refused (403) on every route, the same
    way for an id that exists and one that does not; a service credential is
    refused as a person-only route;
  * the list carries no token and no hash; the invite link is in the create
    answer only, stored as sha256, expires, and works once;
  * withdrawing an invite stops its link at once;
  * a role change, a disable, an enable and an end-sessions each write a
    signed audit row naming the admin; disabling ends the sessions at once;
  * the owner cannot be demoted or disabled, and no body can carry the
    owner flag;
  * the only active admin cannot demote or disable themself.
OFF: every route answers 409 ``signin_not_enabled``.

The pure half (no database): the last-admin rule, the person-only rule and
the registry-slim import.
"""
from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import textwrap
import uuid
from types import SimpleNamespace
from typing import Any

import asyncpg
import httpx
import pytest
import pytest_asyncio

from legba.data import users as U
from legba.data.config import NatsConfig, PostgresConfig
from legba.data.registry import _deps
from legba.data.registry import route_roles as rr
from legba.data.registry import session_auth as sa
from legba.shared import auth_signin as S

_REPO = pathlib.Path(__file__).resolve().parents[2]
_SRC = _REPO / "src"
_DOMAIN = "usersadmin.example"
ADMIN_TOK = "ua-admin-token-" + "6" * 24
RUNTIME_TOK = "ua-runtime-token-" + "7" * 22
_BASE = "/api/v1/auth/admin"


def _email(name: str) -> str:
    return f"{name}@{_DOMAIN}"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def pool(migrated_pg: PostgresConfig):
    p = await asyncpg.create_pool(migrated_pg.dsn, min_size=1, max_size=4)
    await _scrub(p)
    yield p
    await _scrub(p)
    await p.close()


async def _scrub(p: asyncpg.Pool) -> None:
    async with p.acquire() as conn:
        await conn.execute("DELETE FROM users WHERE email LIKE $1", f"%@{_DOMAIN}")
    U.local_password_provider().failures.clear()


def _build_app(migrated_pg: PostgresConfig, monkeypatch):
    from legba.data.registry.credentials import MASTER_KEY_ENV
    from legba.data.registry.server import create_app

    monkeypatch.setenv("LEGBA_REGISTRY_API_TOKEN", ADMIN_TOK)
    monkeypatch.delenv("LEGBA_REGISTRY_SERVICE_TOKEN", raising=False)
    monkeypatch.setenv(S.service_token_env("runtime"), RUNTIME_TOK)
    monkeypatch.delenv("LEGBA_DEV_MODE", raising=False)
    monkeypatch.delenv(rr.ROLE_OVERRIDE_ENV, raising=False)
    monkeypatch.delenv(sa.COOKIE_INSECURE_ENV, raising=False)
    if not os.environ.get(MASTER_KEY_ENV):
        monkeypatch.setenv(MASTER_KEY_ENV, "00112233445566778899" * 3 + "0011")
    if not os.environ.get("LEGBA_REGISTRY_SIGNING_KEY"):
        monkeypatch.setenv("LEGBA_REGISTRY_SIGNING_KEY", "22" * 32)
    return create_app(
        pg_config=migrated_pg,
        nats_config=NatsConfig.from_env(),
        enable_healthcheck_loop=False,
        enable_vocabulary_subscription=False,
    )


async def _client(migrated_pg, monkeypatch):
    app = _build_app(migrated_pg, monkeypatch)
    store = app.state.registry_deps.descriptor_registry.pg
    await store.connect()
    try:
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="https://t") as c:
            yield c
    finally:
        await store.close()


@pytest_asyncio.fixture
async def client_on(migrated_pg, pool, monkeypatch):
    monkeypatch.setenv(S.SIGNIN_ENV, "1")
    async for c in _client(migrated_pg, monkeypatch):
        yield c


@pytest_asyncio.fixture
async def client_off(migrated_pg, pool, monkeypatch):
    monkeypatch.delenv(S.SIGNIN_ENV, raising=False)
    async for c in _client(migrated_pg, monkeypatch):
        yield c


async def _person(
    pool: asyncpg.Pool, name: str, role: str, *, owner: bool = False,
    password: str | None = None,
) -> tuple[str, dict[str, str]]:
    """An active account and a signed-in browser for it → (id, headers)."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "INSERT INTO users (email, display_name, role, status, is_owner) "
            "VALUES ($1, $2, $3, 'active', $4) RETURNING id",
            _email(name), name, role, owner,
        )
        uid = str(row["id"])
        if password:
            await U.local_password_provider().set_secret(conn, uid, password)
        user = await U.get_user(conn, uid)
        session = await U.create_session(conn, user)
    return uid, {
        "Cookie": f"{sa.SESSION_COOKIE_NAME}={session.token}",
        "Sec-Fetch-Site": "same-origin",
    }


async def _audit_rows(pool: asyncpg.Pool, user_id: str) -> list[Any]:
    async with pool.acquire() as conn:
        return await conn.fetch(
            "SELECT actor_id, actor_role, action, change_summary FROM descriptor_audit_log "
            "WHERE namespace = 'user' AND descriptor_id = $1 ORDER BY occurred_at, chain_seq",
            user_id,
        )


async def _other_active_admins(pool: asyncpg.Pool) -> int:
    async with pool.acquire() as conn:
        return int(await conn.fetchval(
            "SELECT count(*) FROM users WHERE status = 'active' "
            "AND role = ANY($1::text[]) AND email NOT LIKE $2",
            list(U.ADMIN_ROLE_NAMES), f"%@{_DOMAIN}",
        ))


def _routes(uid: str) -> list[tuple[str, str, dict[str, Any] | None]]:
    return [
        ("GET", f"{_BASE}/users", None),
        ("POST", f"{_BASE}/invites", {"email": _email("someone"), "role": "reader"}),
        ("DELETE", f"{_BASE}/invites/{uid}", None),
        ("POST", f"{_BASE}/users/{uid}/role", {"role": "admin"}),
        ("POST", f"{_BASE}/users/{uid}/disable", None),
        ("POST", f"{_BASE}/users/{uid}/enable", None),
        ("POST", f"{_BASE}/users/{uid}/end-sessions", None),
    ]


# ---------------------------------------------------------------------------
# Switch ON
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_readers_contributors_and_services_are_refused(client_on, pool) -> None:
    c = client_on
    admin_id, _ = await _person(pool, "ada", "admin")
    _, reader = await _person(pool, "rhea", "reader")
    _, contributor = await _person(pool, "cato", "contributor")
    for headers in (reader, contributor):
        for uid in (admin_id, str(uuid.uuid4())):
            for method, path, body in _routes(uid):
                r = await c.request(method, path, headers=headers, json=body)
                assert r.status_code == 403, (method, path, r.text)
                assert r.json()["detail"]["required_role"] == "admin"
                assert _email("ada") not in r.text
    service = {"Authorization": f"Bearer {RUNTIME_TOK}"}
    for method, path, body in _routes(admin_id):
        r = await c.request(method, path, headers=service, json=body)
        assert r.status_code == 403, (method, path, r.text)
        assert "service_principal_refused" in r.text
    # Nothing was changed by any of the refused calls.
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT role, status FROM users WHERE id = $1",
                                  uuid.UUID(admin_id))
        assert (row["role"], row["status"]) == ("admin", "active")
        assert await conn.fetchval("SELECT 1 FROM users WHERE email = $1",
                                   _email("someone")) is None


@pytest.mark.asyncio
async def test_invite_link_shown_once_hashed_expiring_single_use(client_on, pool) -> None:
    c = client_on
    admin_id, admin = await _person(pool, "ada", "admin")
    r = await c.post(f"{_BASE}/invites", headers=admin, json={
        "email": _email("Nell").upper(), "role": "contributor", "display_name": "Nell",
    })
    assert r.status_code == 201, r.text
    assert r.headers.get("cache-control") == "no-store"
    body = r.json()
    path = body["invite_path"]
    assert path.startswith("/#invite=")
    token = path.split("=", 1)[1]
    invited_id = body["invite"]["user_id"]
    assert body["invite"]["role"] == "contributor"
    assert body["invite"]["email"] == _email("nell")

    async with pool.acquire() as conn:
        stored = await conn.fetchrow(
            "SELECT token_hash, expires_at > now() AS live, "
            "expires_at < now() + interval '8 days' AS bounded "
            "FROM user_invites WHERE user_id = $1", uuid.UUID(invited_id),
        )
    assert stored["token_hash"] == U.hash_token(token) and token not in stored["token_hash"]
    assert stored["live"] and stored["bounded"]

    # The list names the pending invite but carries neither token nor hash.
    r = await c.get(f"{_BASE}/users", headers=admin)
    assert r.status_code == 200, r.text
    assert r.headers.get("cache-control") == "no-store"
    listed = r.json()
    assert listed["signin"] is True and listed["you"] == admin_id
    assert token not in r.text and stored["token_hash"] not in r.text
    pending = {i["user_id"]: i for i in listed["invites"]}
    assert pending[invited_id]["role"] == "contributor"
    assert pending[invited_id]["expired"] is False
    assert pending[invited_id]["invited_by"] == f"user:{admin_id}"
    assert invited_id not in {u["id"] for u in listed["users"]}
    me = {u["id"]: u for u in listed["users"]}[admin_id]
    assert {"email", "display_name", "role", "is_owner", "status", "created_at",
            "last_sign_in_at", "last_seen_at", "live_sessions"} <= set(me)
    assert me["live_sessions"] == 1 and me["role"] == "admin"

    # Single use.
    r = await c.post("/api/v1/auth/accept", headers={"Sec-Fetch-Site": "same-origin"},
                     json={"token": token, "password": "nell passphrase long"})
    assert r.status_code == 200, r.text
    r = await c.post("/api/v1/auth/accept", headers={"Sec-Fetch-Site": "same-origin"},
                     json={"token": token, "password": "nell passphrase long"})
    assert r.status_code == 401 and r.json()["detail"]["error"] == "invite_used"

    rows = await _audit_rows(pool, invited_id)
    assert [r["action"] for r in rows] == ["user_invite"]
    assert rows[0]["actor_id"] == f"user:{admin_id}" and rows[0]["actor_role"] == "admin"
    assert token not in str(rows[0]["change_summary"])

    # The owner flag cannot ride in on an invite; nor can an unknown role.
    r = await c.post(f"{_BASE}/invites", headers=admin,
                     json={"email": _email("x"), "role": "reader", "is_owner": True})
    assert r.status_code == 422, r.text
    r = await c.post(f"{_BASE}/invites", headers=admin,
                     json={"email": _email("x"), "role": "owner"})
    assert r.status_code == 422, r.text


@pytest.mark.asyncio
async def test_withdrawn_invite_stops_working(client_on, pool) -> None:
    c = client_on
    admin_id, admin = await _person(pool, "ada", "admin")
    r = await c.post(f"{_BASE}/invites", headers=admin,
                     json={"email": _email("wes"), "role": "reader"})
    token = r.json()["invite_path"].split("=", 1)[1]
    invited_id = r.json()["invite"]["user_id"]
    r = await c.delete(f"{_BASE}/invites/{invited_id}", headers=admin)
    assert r.status_code == 200 and r.json()["revoked"] is True, r.text
    r = await c.post("/api/v1/auth/accept", headers={"Sec-Fetch-Site": "same-origin"},
                     json={"token": token, "password": "wes passphrase long"})
    assert r.status_code == 401 and r.json()["detail"]["error"] == "invite_invalid"
    r = await c.delete(f"{_BASE}/invites/{invited_id}", headers=admin)
    assert r.status_code == 404
    # An account past its invite is not "withdrawn".
    r = await c.delete(f"{_BASE}/invites/{admin_id}", headers=admin)
    assert r.status_code == 404
    rows = await _audit_rows(pool, invited_id)
    assert [r["action"] for r in rows] == ["user_invite", "user_invite_revoke"]
    # The same email can be invited again.
    r = await c.post(f"{_BASE}/invites", headers=admin,
                     json={"email": _email("wes"), "role": "reader"})
    assert r.status_code == 201, r.text


@pytest.mark.asyncio
async def test_role_disable_enable_end_sessions_are_audited(client_on, pool) -> None:
    c = client_on
    admin_id, admin = await _person(pool, "ada", "admin")
    pw = "cato passphrase long"
    cato_id, cato = await _person(pool, "cato", "contributor", password=pw)

    r = await c.post(f"{_BASE}/users/{cato_id}/role", headers=admin, json={"role": "reader"})
    assert r.status_code == 200 and r.json()["user"]["role"] == "reader", r.text
    r = await c.post(f"{_BASE}/users/{cato_id}/role", headers=admin, json={"role": "reader"})
    assert r.status_code == 200 and r.json()["changed"] is False
    r = await c.post(f"{_BASE}/users/{cato_id}/role", headers=admin,
                     json={"role": "admin", "is_owner": True})
    assert r.status_code == 422, r.text

    # Disabling ends the session at once.
    assert (await c.get("/api/v1/auth/me", headers=cato)).status_code == 200
    r = await c.post(f"{_BASE}/users/{cato_id}/disable", headers=admin)
    assert r.status_code == 200 and r.json()["user"]["status"] == "disabled", r.text
    assert (await c.get("/api/v1/auth/me", headers=cato)).status_code == 401
    r = await c.post("/api/v1/auth/sign-in", headers={"Sec-Fetch-Site": "same-origin"},
                     json={"email": _email("cato"), "password": pw})
    assert r.status_code == 403 and r.json()["detail"]["error"] == "user_disabled"

    r = await c.post(f"{_BASE}/users/{cato_id}/enable", headers=admin)
    assert r.status_code == 200 and r.json()["user"]["status"] == "active", r.text
    r = await c.post("/api/v1/auth/sign-in", headers={"Sec-Fetch-Site": "same-origin"},
                     json={"email": _email("cato"), "password": pw})
    assert r.status_code == 200, r.text
    fresh = {"Cookie": r.headers["set-cookie"].split(";", 1)[0], "Sec-Fetch-Site": "same-origin"}
    assert (await c.get("/api/v1/auth/me", headers=fresh)).status_code == 200

    r = await c.post(f"{_BASE}/users/{cato_id}/end-sessions", headers=admin)
    assert r.status_code == 200 and r.json()["sessions_ended"] == 1, r.text
    assert (await c.get("/api/v1/auth/me", headers=fresh)).status_code == 401

    rows = await _audit_rows(pool, cato_id)
    assert [r["action"] for r in rows] == [
        "user_role_change", "user_disable", "user_enable", "user_sessions_end",
    ]
    assert {r["actor_id"] for r in rows} == {f"user:{admin_id}"}
    import json
    first = rows[0]["change_summary"]
    first = json.loads(first) if isinstance(first, str) else first
    assert (first["from_role"], first["to_role"]) == ("contributor", "reader")

    r = await c.post(f"{_BASE}/users/{uuid.uuid4()}/disable", headers=admin)
    assert r.status_code == 404
    r = await c.post(f"{_BASE}/users/not-a-uuid/disable", headers=admin)
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_owner_is_protected(client_on, pool) -> None:
    c = client_on
    owner_id, _ = await _person(pool, "olga", "admin", owner=True)
    _, admin = await _person(pool, "ada", "admin")
    for path, body in (
        (f"{_BASE}/users/{owner_id}/role", {"role": "reader"}),
        (f"{_BASE}/users/{owner_id}/role", {"role": "contributor"}),
        (f"{_BASE}/users/{owner_id}/disable", None),
    ):
        r = await c.post(path, headers=admin, json=body)
        assert r.status_code == 409 and r.json()["detail"]["error"] == "owner_protected", r.text
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT role, status, is_owner FROM users WHERE id = $1",
                                  uuid.UUID(owner_id))
    assert (row["role"], row["status"], row["is_owner"]) == ("admin", "active", True)
    assert await _audit_rows(pool, owner_id) == []


@pytest.mark.asyncio
async def test_the_only_admin_cannot_leave_no_admin(client_on, pool) -> None:
    if await _other_active_admins(pool):
        pytest.skip("the shared test DB holds another active admin")
    c = client_on
    ada_id, ada = await _person(pool, "ada", "admin")
    for path, body in (
        (f"{_BASE}/users/{ada_id}/role", {"role": "reader"}),
        (f"{_BASE}/users/{ada_id}/disable", None),
    ):
        r = await c.post(path, headers=ada, json=body)
        assert r.status_code == 409 and r.json()["detail"]["error"] == "last_admin", r.text
    # With a second admin, stepping down is allowed — and then the second
    # one is the last.
    bo_id, bo = await _person(pool, "bo", "admin")
    r = await c.post(f"{_BASE}/users/{ada_id}/role", headers=ada, json={"role": "reader"})
    assert r.status_code == 200, r.text
    r = await c.post(f"{_BASE}/users/{bo_id}/disable", headers=bo)
    assert r.status_code == 409 and r.json()["detail"]["error"] == "last_admin", r.text


# ---------------------------------------------------------------------------
# Switch OFF
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_signin_off_says_so_and_touches_nothing(client_off, pool) -> None:
    c = client_off
    op = {"Authorization": f"Bearer {ADMIN_TOK}"}
    for method, path, body in _routes(str(uuid.uuid4())):
        r = await c.request(method, path, headers=op, json=body)
        assert r.status_code == 409, (method, path, r.text)
        detail = r.json()["detail"]
        assert detail["error"] == "signin_not_enabled"
        assert "LEGBA_AUTH_SIGNIN=1" in detail["message"]
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT 1 FROM users WHERE email = $1",
                                   _email("someone")) is None


# ---------------------------------------------------------------------------
# Pure
# ---------------------------------------------------------------------------


class _FakeConn:
    def __init__(self, admins: list[str]) -> None:
        self.admins = admins
        self.queries: list[str] = []

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, str]]:
        self.queries.append(sql)
        return [{"id": a} for a in self.admins]


def _user(uid: str, role: str = "admin", status: str = "active") -> U.User:
    return U.User(id=uid, email=None, display_name="", role=role, status=status,
                  auth_provider="local_password")


@pytest.mark.asyncio
async def test_last_admin_rule_pure() -> None:
    a, b = str(uuid.uuid4()), str(uuid.uuid4())
    alone = _FakeConn([a])
    for kw in ({"role": "reader", "status": None}, {"role": "contributor", "status": None},
               {"role": None, "status": "disabled"}):
        with pytest.raises(U.AuthRefused) as exc:
            await U._refuse_last_admin(alone, _user(a), **kw)
        assert exc.value.code == "last_admin" and exc.value.status == 409
    assert "FOR UPDATE" in alone.queries[0]
    # Another admin exists: allowed.
    await U._refuse_last_admin(_FakeConn([a, b]), _user(a), role="reader", status=None)
    # Staying an active admin, or not an admin to begin with: never read.
    quiet = _FakeConn([])
    await U._refuse_last_admin(quiet, _user(a), role="admin", status="active")
    await U._refuse_last_admin(quiet, _user(a, role="reader"), role="reader", status="disabled")
    await U._refuse_last_admin(quiet, _user(a, status="disabled"), role="reader", status=None)
    # The October name for admin counts as admin.
    with pytest.raises(U.AuthRefused):
        await U._refuse_last_admin(_FakeConn([a]), _user(a, role="operator"),
                                   role="reader", status=None)
    assert quiet.queries == []
    assert set(U.ADMIN_ROLE_NAMES) == {"admin", "operator"}


def test_account_routes_are_person_only_and_admin_only() -> None:
    from legba.data.registry import route_policy as rp

    uid = str(uuid.uuid4())
    for method, path, _ in _routes(uid):
        assert _deps.human_only_reason(method, path) == "managing accounts", path
    for method, template in [
        ("GET", "/api/v1/auth/admin/users"),
        ("POST", "/api/v1/auth/admin/invites"),
        ("DELETE", "/api/v1/auth/admin/invites/{user_id}"),
        ("POST", "/api/v1/auth/admin/users/{user_id}/role"),
        ("POST", "/api/v1/auth/admin/users/{user_id}/disable"),
        ("POST", "/api/v1/auth/admin/users/{user_id}/enable"),
        ("POST", "/api/v1/auth/admin/users/{user_id}/end-sessions"),
    ]:
        assert rp.ROUTE_MIN_ROLES[(method, template)] == "admin"
        assert rr.ROUTE_ROLES[(method, template)] == rr.ROLE_OPERATOR


def test_pending_invite_shape_has_no_secret() -> None:
    inv = U.PendingInvite(
        user_id="u", email="e@x.y", display_name="", role="operator", invited_by=None,
        created_at=None, expires_at=None, expired=True,
    )
    out = inv.public()
    assert out["role"] == "admin"
    assert not {k for k in out if "token" in k or "hash" in k}


def test_handler_refuses_below_admin_even_without_the_gate(monkeypatch) -> None:
    from fastapi import HTTPException

    from legba.data.registry import users_admin_api as ua

    monkeypatch.setenv(S.SIGNIN_ENV, "1")
    monkeypatch.delenv(rr.ROLE_OVERRIDE_ENV, raising=False)
    req = SimpleNamespace(
        state=SimpleNamespace(legba_role="contributor", legba_subject="u1"),
        headers={}, scope={"type": "http", "method": "GET", "path": "/x"},
    )
    with pytest.raises(HTTPException) as exc:
        ua._require_admin_and_signin(req)  # type: ignore[arg-type]
    assert exc.value.status_code == 403
    req.state.legba_role = "admin"
    monkeypatch.delenv(S.SIGNIN_ENV, raising=False)
    with pytest.raises(HTTPException) as exc:
        ua._require_admin_and_signin(req)  # type: ignore[arg-type]
    assert exc.value.status_code == 409
    assert exc.value.detail["error"] == "signin_not_enabled"


def test_users_admin_module_imports_registry_slim() -> None:
    probe = textwrap.dedent(
        """
        import sys
        for blocked in ("feedparser", "telethon", "warcio", "aiobotocore",
                        "pycountry", "networkx", "qdrant_client", "dspy"):
            sys.modules[blocked] = None
        import legba.data.registry.users_admin_api as uaa
        leaked = sorted(m for m in sys.modules
                        if m.startswith("legba.data.analysts") or m.startswith("legba.runtime"))
        assert not leaked, leaked
        assert "legba.data.registry.api" not in sys.modules, "users_admin_api pulled the api hub"
        assert uaa.build_users_admin_router
        print("OK")
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, timeout=120,
        env={**os.environ, "PYTHONPATH": os.pathsep.join(
            p for p in (str(_SRC), os.environ.get("PYTHONPATH", "")) if p)},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "OK" in result.stdout
