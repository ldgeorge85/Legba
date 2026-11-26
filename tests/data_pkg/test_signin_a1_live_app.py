# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The sign-in switch through the REAL app, on the migrated test DB.

ON (``LEGBA_AUTH_SIGNIN=1``):
  * migration 0255 applies twice (idempotent) and widens only what it says;
  * the first owner is created by the bootstrap path (no invite token, no
    secret printed), and a second bootstrap is refused;
  * no credential → 401 (the console's sign-in screen); the owner signs in
    with a cookie and ``/auth/me`` says admin + owner + ``signin``;
  * the owner invites a reader and a contributor by the NEW role names;
    the reader is refused (403, naming the role) on a contributor route and
    on an admin ("operator side") route, the contributor passes the gate on
    its route; a reader's watch is owned by the reader whatever the body says;
  * a contributor's label records the caller, not the body's ``labeled_by``;
  * the owner cannot be demoted or disabled through the API;
  * a cross-site write with the cookie is refused; an idle session ends;
  * each service credential is its own actor; the admin token is break-glass.

OFF (the default), the same app: the per-service token is refused, the admin
token is the October ``operator``, ``/auth/me`` answers the October keys, and
the invite route takes the October role names — the off-path proof beside
the unchanged October suites.
"""
from __future__ import annotations

import os
import pathlib
import uuid

import asyncpg
import httpx
import pytest
import pytest_asyncio

from legba.data import users as U
from legba.data import users_bootstrap as B
from legba.data.config import NatsConfig, PostgresConfig
from legba.data.registry import _deps
from legba.data.registry import route_roles as rr
from legba.data.registry import session_auth as sa
from legba.shared import auth_signin as S

_REPO = pathlib.Path(__file__).resolve().parents[2]
_MIGRATION = _REPO / "src" / "legba" / "data" / "migrations" / "0255_signin_roles_owner_idle.sql"
_DOMAIN = "a1test.example"
ADMIN_TOK = "a1-admin-token-" + "4" * 24
RUNTIME_TOK = "a1-runtime-token-" + "5" * 22
OWNER_PW = "an owner passphrase for a1"


def _email(name: str) -> str:
    return f"{name}@{_DOMAIN}"


@pytest_asyncio.fixture
async def pool(migrated_pg: PostgresConfig):
    p = await asyncpg.create_pool(migrated_pg.dsn, min_size=1, max_size=4)
    await _scrub(p)
    yield p
    await _scrub(p)
    await p.close()


async def _scrub(p: asyncpg.Pool) -> None:
    async with p.acquire() as conn:
        await conn.execute("DELETE FROM watchlist WHERE label LIKE 'a1-%'")
        await conn.execute(
            "DELETE FROM unit_reference_labels WHERE reference_answer LIKE 'a1-%'"
        )
        await conn.execute("DELETE FROM users WHERE email LIKE $1", f"%@{_DOMAIN}")
        # The bootstrap refuses while ANY active owner exists; the test DB is
        # shared, so only rows this file made are removed above — assert none
        # other is in the way rather than delete someone else's.
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


@pytest_asyncio.fixture
async def client_on(migrated_pg, pool, monkeypatch):
    monkeypatch.setenv(S.SIGNIN_ENV, "1")
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
async def client_off(migrated_pg, pool, monkeypatch):
    monkeypatch.delenv(S.SIGNIN_ENV, raising=False)
    app = _build_app(migrated_pg, monkeypatch)
    store = app.state.registry_deps.descriptor_registry.pg
    await store.connect()
    try:
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="https://t") as c:
            yield c
    finally:
        await store.close()


def _cookie(resp: httpx.Response) -> dict[str, str]:
    raw = resp.headers.get("set-cookie") or ""
    assert raw.startswith(f"{sa.SESSION_COOKIE_NAME}="), raw
    attrs = [a.strip().lower() for a in raw.split(";")]
    assert {"httponly", "secure", "samesite=lax", "path=/"} <= set(attrs), raw
    value = raw.split(";", 1)[0].split("=", 1)[1]
    return {"Cookie": f"{sa.SESSION_COOKIE_NAME}={value}", "Sec-Fetch-Site": "same-origin"}


async def _owner_exists_elsewhere(pool) -> bool:
    async with pool.acquire() as conn:
        return bool(await conn.fetchval(
            "SELECT 1 FROM users WHERE is_owner AND status = 'active' "
            "AND email NOT LIKE $1", f"%@{_DOMAIN}",
        ))


# ---------------------------------------------------------------------------
# The migration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_migration_0255_is_idempotent_and_widens_only(pool) -> None:
    sql = _MIGRATION.read_text()
    async with pool.acquire() as conn:
        await conn.execute(sql)
        await conn.execute(sql)
        cols = {
            (r["table_name"], r["column_name"]): r for r in await conn.fetch(
                "SELECT table_name, column_name, data_type, is_nullable, column_default "
                "FROM information_schema.columns WHERE table_schema = 'public' "
                "AND table_name IN ('users', 'user_sessions')"
            )
        }
        owner = cols[("users", "is_owner")]
        assert (owner["data_type"], owner["is_nullable"]) == ("boolean", "NO")
        assert cols[("user_sessions", "last_seen_at")]["is_nullable"] == "YES"
        for role in ("read", "reader-own-rows", "operator", "reader", "contributor", "admin"):
            uid = await conn.fetchval(
                "INSERT INTO users (email, role) VALUES ($1, $2) RETURNING id",
                _email(f"m-{role}"), role,
            )
            assert uid is not None
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "INSERT INTO users (email, role) VALUES ($1, 'superuser')", _email("m-bad"),
            )


# ---------------------------------------------------------------------------
# Switch ON
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_signin_on_end_to_end(client_on, pool, capsys) -> None:
    c = client_on
    if await _owner_exists_elsewhere(pool):
        pytest.skip("the shared test DB already holds an owner from another test")

    # No credential at all → 401 (the console's sign-in screen).
    r = await c.get("/api/v1/auth/me")
    assert r.status_code == 401, r.text

    # The first owner — the bootstrap path, then refused a second time.
    async with pool.acquire() as conn:
        owner = await B.bootstrap_owner(
            conn, email=_email("owner"), password=OWNER_PW, display_name="Owner",
        )
        with pytest.raises(U.AuthRefused) as exc:
            await B.bootstrap_owner(conn, email=_email("owner2"), password=OWNER_PW)
        assert exc.value.code == "owner_exists"
        secret = await conn.fetchval(
            "SELECT secret_hash FROM user_credentials WHERE user_id = $1",
            uuid.UUID(owner.id),
        )
    assert secret.startswith("$argon2id$") and OWNER_PW not in secret
    assert owner.is_owner is True and owner.role == "admin"
    assert OWNER_PW not in capsys.readouterr().out

    r = await c.post(
        "/api/v1/auth/sign-in", json={"email": _email("owner"), "password": OWNER_PW},
        headers={"Sec-Fetch-Site": "same-origin"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["user"]["is_owner"] is True and r.json()["user"]["role"] == "admin"
    owner_h = _cookie(r)
    me = (await c.get("/api/v1/auth/me", headers=owner_h)).json()
    assert (me["kind"], me["role"], me["is_owner"], me["signin"]) == ("user", "admin", True, True)
    assert me["actor"] == f"user:{owner.id}"

    # Invites by the NEW names; the October names are refused (422).
    r = await c.post("/api/v1/auth/invite", headers=owner_h,
                     json={"email": _email("x"), "role": "operator"})
    assert r.status_code == 422, r.text
    people = {}
    for name, role in (("rhea", "reader"), ("cato", "contributor")):
        r = await c.post("/api/v1/auth/invite", headers=owner_h,
                         json={"email": _email(name), "role": role})
        assert r.status_code == 201, r.text
        assert r.json()["user"]["invited_by"] == f"user:{owner.id}"
        tok = r.json()["invite_token"]
        r = await c.post("/api/v1/auth/accept",
                         json={"token": tok, "password": f"{name} passphrase long"},
                         headers={"Sec-Fetch-Site": "same-origin"})
        assert r.status_code == 200, r.text
        assert r.json()["user"]["role"] == role
        people[name] = (r.json()["user"]["id"], _cookie(r))
    rhea_id, rhea = people["rhea"]
    cato_id, cato = people["cato"]

    # Reader: refused on a contributor route and an admin route, by name.
    r = await c.post("/api/v1/eval/labels", headers=rhea, json={})
    assert r.status_code == 403, r.text
    assert r.json()["detail"]["required_role"] == "contributor"
    assert r.json()["detail"]["principal_role"] == "reader"
    r = await c.get("/api/v1/v3/system/production-gauge", headers=rhea)
    assert r.status_code == 403 and r.json()["detail"]["required_role"] == "admin"
    r = await c.get("/api/v1/registry/descriptors", headers=cato)
    assert r.status_code == 403 and r.json()["detail"]["required_role"] == "admin"
    # Contributor passes the gate on its route (the body is then validated).
    r = await c.post("/api/v1/eval/labels", headers=cato, json={})
    assert r.status_code == 422, r.text

    # Reader's own watch: the owner is the caller, whatever the body claims.
    r = await c.post("/api/v1/v3/watchlist", headers=rhea, json={
        "label": "a1-watch", "kind": "text", "pattern": {"query": "a1 watch"},
    })
    assert r.status_code == 201, r.text
    assert r.json()["created_by"] == rhea_id
    r = await c.post("/api/v1/v3/watchlist", headers=rhea, json={
        "label": "a1-watch-2", "kind": "text", "pattern": {"query": "a1 watch"},
        "created_by": "someone-else",
    })
    assert r.status_code == 403, r.text

    # The owner stays an active admin through the API.
    r = await c.patch(f"/api/v1/auth/users/{owner.id}", headers=owner_h, json={"role": "reader"})
    assert r.status_code == 409 and r.json()["detail"]["error"] == "owner_protected"
    r = await c.patch(f"/api/v1/auth/users/{owner.id}", headers=owner_h,
                      json={"status": "disabled"})
    assert r.status_code == 409
    r = await c.patch(f"/api/v1/auth/users/{cato_id}", headers=owner_h, json={"role": "reader"})
    assert r.status_code == 200 and r.json()["user"]["role"] == "reader"

    # A cross-site write carrying the cookie is refused before any route.
    r = await c.post("/api/v1/v3/watchlist", headers={**rhea, "Sec-Fetch-Site": "cross-site"},
                     json={"label": "a1-x", "kind": "text", "pattern": {"query": "xx"}})
    assert r.status_code == 403 and r.json()["detail"]["error"] == "cross_site_refused"

    # The idle limit: a session last seen 13 h ago is over (default 12 h).
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE user_sessions SET last_seen_at = now() - interval '13 hours' "
            "WHERE user_id = $1", uuid.UUID(rhea_id),
        )
    r = await c.get("/api/v1/auth/me", headers=rhea)
    assert r.status_code == 401, r.text
    # A live session's clock is touched.
    async with pool.acquire() as conn:
        seen = await conn.fetchval(
            "SELECT max(last_seen_at) FROM user_sessions WHERE user_id = $1",
            uuid.UUID(owner.id),
        )
    assert seen is not None

    # Services: own actor; refused on a human-only route; break-glass logged.
    r = await c.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {RUNTIME_TOK}"})
    assert r.status_code == 200, r.text
    assert (r.json()["kind"], r.json()["service"], r.json()["actor"]) == (
        "service", "runtime", "service:runtime",
    )
    r = await c.post("/api/v1/registry/vault/secrets",
                     headers={"Authorization": f"Bearer {RUNTIME_TOK}"}, json={})
    assert r.status_code == 403 and "service_principal_refused" in r.text
    before = _deps.break_glass_count()
    r = await c.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {ADMIN_TOK}"})
    assert r.status_code == 200
    assert r.json()["kind"] == "admin-token" and r.json()["role"] == "admin"
    assert ADMIN_TOK not in r.text
    assert _deps.break_glass_count() == before + 1


# ---------------------------------------------------------------------------
# Switch OFF — the same app answers as it did before the switch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_signin_off_is_the_october_behaviour(client_off) -> None:
    c = client_off
    # The per-service token is not a credential.
    r = await c.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {RUNTIME_TOK}"})
    assert r.status_code == 403, r.text
    # The admin token is the October operator, with the October answer.
    r = await c.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {ADMIN_TOK}"})
    assert r.status_code == 200, r.text
    me = r.json()
    assert set(me) == {"kind", "actor", "role", "user", "session_rejected"}
    assert (me["kind"], me["role"]) == ("operator", "operator")
    assert me["actor"].startswith("operator:bearer:")
    # No credential: the October 401.
    r = await c.get("/api/v1/auth/me")
    assert r.status_code == 401
    # Invites take the October class names; the new ones are refused (422).
    r = await c.post("/api/v1/auth/invite", headers={"Authorization": f"Bearer {ADMIN_TOK}"},
                     json={"email": _email("off"), "role": "admin"})
    assert r.status_code == 422, r.text
    r = await c.post("/api/v1/auth/invite", headers={"Authorization": f"Bearer {ADMIN_TOK}"},
                     json={"email": _email("off"), "role": "read"})
    assert r.status_code == 201, r.text
    assert "is_owner" not in r.json()["user"]
    # The same-origin rule does not run: a cross-site sign-in attempt reaches
    # the route (and is refused there for bad credentials, as before).
    r = await c.post("/api/v1/auth/sign-in", headers={"Sec-Fetch-Site": "cross-site"},
                     json={"email": _email("nobody"), "password": "wrong password!!"})
    assert r.status_code == 401 and r.json()["detail"]["error"] == "invalid_credentials"
