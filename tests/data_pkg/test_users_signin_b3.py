# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""X3-B3 — local users + sign-in (migration 0236), the lane's DONE test.

Through the REAL app ``server.create_app`` builds (its middleware stack, the
route-role gate, the bearer gate, the users / consult / watchlist routers),
with its own Postgres store connected to the migrated test DB:

  * the operator (bearer, the perimeter path) invites two people; each accepts
    the invite with their own password and is signed in by an HttpOnly,
    Secure, SameSite=Lax cookie; the invite and session tokens are stored only
    as sha256, the password only as argon2id — read back off the rows;
  * a signed-in ``reader-own-rows`` user gets 403 (role_forbidden, naming
    ``operator``) on operator routes and 200/201 on their own-rows routes —
    B2's gate reading THIS lane's stamp (and a probe proves the stamp is on
    the request state when the gate runs: the middleware order);
  * two users see ONLY their own consult sessions (list + load; the
    cross-read is refused 403), their usage is keyed to ``user:<id>``, and
    the operator's bearer still sees everything (unchanged);
  * an unknown OIDC subject is refused by name; the OIDC exchange refuses
    naming SEAMS #73;
  * disabling a user revokes their sessions at once;
  * the session paths of the two non-HTTP gates (events socket, SSE relay);
  * the new modules import under the registry-slim rule.
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import subprocess
import sys
import textwrap
import uuid
from typing import Any

import asyncpg
import httpx
import pytest
import pytest_asyncio
from fastapi import HTTPException
from starlette.requests import Request

from legba.data import users as U
from legba.data.config import NatsConfig, PostgresConfig
from legba.data.registry import route_roles as rr
from legba.data.registry import session_auth as sa

_REPO = pathlib.Path(__file__).resolve().parents[2]
_SRC = _REPO / "src"

OP_TOKEN = "b3-operator-token-" + "0" * 24
_DOMAIN = "b3test.example"


def _email(name: str) -> str:
    return f"{name}@{_DOMAIN}"


OP = {"Authorization": f"Bearer {OP_TOKEN}"}


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
        await conn.execute(
            "DELETE FROM consult_sessions WHERE principal LIKE 'user:%' "
            "OR title LIKE 'b3-%'"
        )
        await conn.execute("DELETE FROM watchlist WHERE label LIKE 'b3-%'")
        await conn.execute("DELETE FROM users WHERE email LIKE $1", f"%@{_DOMAIN}")
        await conn.execute("DELETE FROM users WHERE external_subject LIKE 'b3-%'")
    U.local_password_provider().failures.clear()


@pytest_asyncio.fixture
async def app(migrated_pg: PostgresConfig, pool, monkeypatch):
    """The app create_app builds, its Postgres store connected (lifespan not
    run — no NATS: nothing under test publishes)."""
    from legba.data.registry.credentials import MASTER_KEY_ENV
    from legba.data.registry.server import create_app

    monkeypatch.setenv("LEGBA_REGISTRY_API_TOKEN", OP_TOKEN)
    monkeypatch.delenv("LEGBA_REGISTRY_SERVICE_TOKEN", raising=False)
    monkeypatch.delenv("LEGBA_DEV_MODE", raising=False)
    monkeypatch.delenv(rr.ROLE_OVERRIDE_ENV, raising=False)
    monkeypatch.delenv(sa.COOKIE_INSECURE_ENV, raising=False)
    if not os.environ.get(MASTER_KEY_ENV):
        monkeypatch.setenv(MASTER_KEY_ENV, "00112233445566778899" * 3 + "0011")
    if not os.environ.get("LEGBA_REGISTRY_SIGNING_KEY"):
        monkeypatch.setenv("LEGBA_REGISTRY_SIGNING_KEY", "22" * 32)
    a = create_app(
        pg_config=migrated_pg,
        nats_config=NatsConfig.from_env(),
        enable_healthcheck_loop=False,
        enable_vocabulary_subscription=False,
    )
    store = a.state.registry_deps.descriptor_registry.pg
    await store.connect()
    try:
        yield a
    finally:
        await store.close()


@pytest_asyncio.fixture
async def client(app):
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="https://t") as c:
        yield c


def _session_cookie(resp: httpx.Response) -> str:
    raw = resp.headers.get("set-cookie") or ""
    assert raw.startswith(f"{sa.SESSION_COOKIE_NAME}="), raw
    attrs = [a.strip().lower() for a in raw.split(";")]
    assert "httponly" in attrs, raw
    assert "secure" in attrs, raw
    assert "samesite=lax" in attrs, raw
    assert "path=/" in attrs, raw
    assert not any(a.startswith("domain=") for a in attrs), raw  # host-only
    return raw.split(";", 1)[0].split("=", 1)[1]


def _as(cookie: str) -> dict[str, str]:
    return {"Cookie": f"theme=dark; {sa.SESSION_COOKIE_NAME}={cookie}"}


async def _invite_and_accept(
    client: httpx.AsyncClient, name: str, role: str, password: str,
) -> tuple[str, str, str]:
    """→ (user id, invite token, session cookie)."""
    r = await client.post(
        "/api/v1/auth/invite", headers=OP,
        json={"email": _email(name).upper(), "role": role, "display_name": name},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["user"]["status"] == "invited"
    assert body["user"]["email"] == _email(name)  # case-folded
    assert body["invite_path"] == f"/#invite={body['invite_token']}"
    token = body["invite_token"]
    r = await client.post(
        "/api/v1/auth/accept", json={"token": token, "password": password},
    )
    assert r.status_code == 200, r.text
    assert r.json()["user"]["status"] == "active"
    assert r.json()["user"]["role"] == role
    return body["user"]["id"], token, _session_cookie(r)


# ---------------------------------------------------------------------------
# The schema
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_schema_has_no_password_column_and_refuses_plaintext(pool) -> None:
    async with pool.acquire() as conn:
        cols = {
            r["column_name"] for r in await conn.fetch(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = 'users'"
            )
        }
        assert {"id", "email", "display_name", "role", "status", "auth_provider",
                "external_subject", "created_at", "updated_at",
                "last_sign_in_at"} <= cols
        assert not {c for c in cols if "pass" in c or "secret" in c or "hash" in c}
        uid = await conn.fetchval(
            "INSERT INTO users (email, role, status) VALUES ($1, 'read', 'invited') "
            "RETURNING id", _email("schema"),
        )
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "INSERT INTO user_credentials (user_id, provider, secret_hash) "
                "VALUES ($1, 'local_password', 'hunter2hunter2')", uid,
            )
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                # 'admin' became a valid role name in migration 0255;
                # the CHECK still refuses a name in neither vocabulary.
                "UPDATE users SET role = 'superuser' WHERE id = $1", uid,
            )
        with pytest.raises(asyncpg.UniqueViolationError):
            await conn.execute(
                "INSERT INTO users (email) VALUES ($1)", _email("schema"),
            )
        idx = {
            r["indexname"] for r in await conn.fetch(
                "SELECT indexname FROM pg_indexes WHERE schemaname = 'public'"
            )
        }
    assert {"uq_users_email", "uq_users_external_subject",
            "idx_user_sessions_user_live",
            "idx_consult_sessions_principal_updated"} <= idx


def test_user_roles_are_the_route_gate_classes() -> None:
    assert U.USER_ROLES == rr.ROLES


# ---------------------------------------------------------------------------
# Invite → accept → sign in, own identity, B2's gate reading the stamp
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_second_person_signs_in_with_own_identity_and_class(
    client, pool, monkeypatch,
) -> None:
    bob_id, invite, cookie = await _invite_and_accept(
        client, "bob", "reader-own-rows", "correct horse battery",
    )

    # At rest: the invite + session tokens as sha256 only; the password as
    # argon2id only. Read back off the rows.
    async with pool.acquire() as conn:
        inv = await conn.fetchrow(
            "SELECT token_hash, used_at FROM user_invites WHERE user_id = $1",
            uuid.UUID(bob_id),
        )
        assert inv["token_hash"] == hashlib.sha256(invite.encode()).hexdigest()
        assert inv["used_at"] is not None
        sess = await conn.fetch(
            "SELECT id_hash FROM user_sessions WHERE user_id = $1", uuid.UUID(bob_id),
        )
        assert [s["id_hash"] for s in sess] == [hashlib.sha256(cookie.encode()).hexdigest()]
        secret = await conn.fetchval(
            "SELECT secret_hash FROM user_credentials WHERE user_id = $1",
            uuid.UUID(bob_id),
        )
        assert secret.startswith("$argon2id$") and "correct horse" not in secret
        row = await conn.fetchrow(
            "SELECT status, role, last_sign_in_at FROM users WHERE id = $1",
            uuid.UUID(bob_id),
        )
    assert (row["status"], row["role"]) == ("active", "reader-own-rows")
    assert row["last_sign_in_at"] is not None

    # Who am I — the actor the consult front door stamps is require_bearer's
    # return value, which is THIS actor.
    r = await client.get("/api/v1/auth/me", headers=_as(cookie))
    assert r.status_code == 200, r.text
    me = r.json()
    assert me["kind"] == "user"
    assert me["actor"] == f"user:{bob_id}"
    assert me["role"] == "reader-own-rows"
    assert me["user"]["email"] == _email("bob")

    # The order proof: the stamp is on the request state when B2's gate runs.
    seen: list[rr.Principal] = []
    real = rr.resolve_principal

    def _probe(conn):  # noqa: ANN001
        p = real(conn)
        seen.append(p)
        return p

    monkeypatch.setattr(rr, "resolve_principal", _probe)

    # Operator routes: 403 naming the role.
    for method, path, body in [
        ("GET", "/api/v1/auth/users", None),
        ("POST", "/api/v1/auth/invite", {"email": _email("eve"), "role": "operator"}),
        ("POST", "/api/v1/registry/stack", {"x": 1}),
        ("POST", "/api/v1/registry/vault/secrets", {"x": 1}),
    ]:
        r = await client.request(method, path, headers=_as(cookie), json=body)
        assert r.status_code == 403, (path, r.text)
        detail = r.json()["detail"]
        assert detail["error"] == "role_forbidden"
        assert detail["required_role"] == "operator"
        assert detail["principal_role"] == "reader-own-rows"
    assert seen and all(
        (p.role, p.subject, p.source) == ("reader-own-rows", bob_id, "request_state")
        for p in seen
    ), seen
    monkeypatch.setattr(rr, "resolve_principal", real)

    # Own-rows routes: admitted, and the row is stamped with the user's id.
    r = await client.post(
        "/api/v1/v3/watchlist", headers=_as(cookie),
        json={"kind": "text", "pattern": {"query": "Bab el-Mandeb"}, "label": "b3-bob"},
    )
    assert r.status_code == 201, r.text
    async with pool.acquire() as conn:
        owner = await conn.fetchval(
            "SELECT created_by FROM watchlist WHERE id = $1", uuid.UUID(r.json()["id"]),
        )
    assert owner == bob_id
    r = await client.get("/api/v1/consult/sessions", headers=_as(cookie))
    assert r.status_code == 200, r.text

    # Sign out → the cookie is dead; sign back in with the password.
    r = await client.post("/api/v1/auth/sign-out", headers=_as(cookie))
    assert r.status_code == 200 and r.json()["session_revoked"] is True
    r = await client.get("/api/v1/auth/me", headers=_as(cookie))
    assert r.status_code == 401, r.text  # no bearer, no live session
    r = await client.post(
        "/api/v1/auth/sign-in",
        json={"email": " Bob@B3Test.Example ", "password": "correct horse battery"},
    )
    assert r.status_code == 200, r.text
    cookie2 = _session_cookie(r)
    assert cookie2 != cookie
    r = await client.get("/api/v1/auth/me", headers=_as(cookie2))
    assert r.json()["actor"] == f"user:{bob_id}"

    # A wrong password and an unknown email answer the same.
    for email in (_email("bob"), _email("nobody")):
        r = await client.post(
            "/api/v1/auth/sign-in", json={"email": email, "password": "wrong password!"},
        )
        assert r.status_code == 401
        assert r.json()["detail"]["error"] == "invalid_credentials"

    # The invite is single-use.
    r = await client.post(
        "/api/v1/auth/accept", json={"token": invite, "password": "another password!"},
    )
    assert r.status_code == 401 and r.json()["detail"]["error"] == "invite_used"


@pytest.mark.asyncio
async def test_operator_bearer_path_is_unchanged(client) -> None:
    r = await client.get("/api/v1/auth/me", headers=OP)
    assert r.status_code == 200, r.text
    me = r.json()
    assert (me["kind"], me["role"], me["user"]) == ("operator", "operator", None)
    assert me["actor"].startswith("operator:bearer:")
    # No credential at all: 401, exactly as before.
    r = await client.get("/api/v1/auth/me")
    assert r.status_code == 401
    # A bad bearer: 403, exactly as before.
    r = await client.get("/api/v1/auth/me", headers={"Authorization": "Bearer nope"})
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_a_bearer_beside_a_cookie_is_the_bearer(client) -> None:
    _, _, cookie = await _invite_and_accept(client, "carol", "read", "carol's password!")
    r = await client.get("/api/v1/auth/me", headers={**OP, **_as(cookie)})
    assert r.json()["kind"] == "operator"
    # The cookie alone is carol, in the `read` class: refused an own-rows write.
    r = await client.post(
        "/api/v1/v3/watchlist", headers=_as(cookie),
        json={"kind": "text", "pattern": {"query": "x"}, "label": "b3-carol"},
    )
    assert r.status_code == 403
    assert r.json()["detail"]["required_role"] == "reader-own-rows"


@pytest.mark.asyncio
async def test_an_armed_consult_turn_stays_the_operators(client, pool) -> None:
    """POST /consult is own-rows, but ``allow_writes`` arms substrate CRUD
    (operator routes) — a signed-in reader is refused before anything runs,
    and cannot continue someone else's session either."""
    _, _, ivy = await _invite_and_accept(client, "ivy", "reader-own-rows", "ivy's own password")
    r = await client.post(
        "/api/v1/consult", headers=_as(ivy),
        json={"question": "pause source x", "allow_writes": True},
    )
    assert r.status_code == 403, r.text
    assert r.json()["detail"]["error"] == "role_forbidden"
    assert r.json()["detail"]["required_role"] == "operator"
    op_sid = await _seed_session(pool, "operator:bearer:b3b3b3b3b3b3", "b3-op-armed", 1)
    r = await client.post(
        "/api/v1/consult", headers=_as(ivy),
        json={"question": "confirm ABCD", "session_id": op_sid},
    )
    assert r.status_code == 403, r.text
    assert r.json()["detail"]["error"] == "not_row_owner"
    async with pool.acquire() as conn:
        turns = await conn.fetchval(
            "SELECT count(*) FROM consult_turns WHERE session_id = $1", uuid.UUID(op_sid),
        )
    assert turns == 2  # the seeded pair; nothing appended


@pytest.mark.asyncio
async def test_invite_refusals(client, pool) -> None:
    r = await client.post(
        "/api/v1/auth/invite", headers=OP, json={"email": _email("dan"), "role": "read"},
    )
    token = r.json()["invite_token"]
    r = await client.post("/api/v1/auth/accept", json={"token": token, "password": "short"})
    assert r.status_code == 400 and r.json()["detail"]["error"] == "password_too_short"
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE user_invites SET expires_at = now() - interval '1 minute' "
            "WHERE token_hash = $1", U.hash_token(token),
        )
    r = await client.post(
        "/api/v1/auth/accept", json={"token": token, "password": "long enough now"},
    )
    assert r.status_code == 401 and r.json()["detail"]["error"] == "invite_expired"
    r = await client.post(
        "/api/v1/auth/accept", json={"token": "not-a-real-token", "password": "long enough now"},
    )
    assert r.status_code == 401 and r.json()["detail"]["error"] == "invite_invalid"
    # An invited user cannot sign in before accepting.
    r = await client.post(
        "/api/v1/auth/sign-in", json={"email": _email("dan"), "password": "long enough now"},
    )
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# Two users, private consult sessions, usage per user
# ---------------------------------------------------------------------------


async def _seed_session(pool, principal: str, title: str, tokens: int) -> str:
    async with pool.acquire() as conn:
        sid = await conn.fetchval(
            "INSERT INTO consult_sessions (mode, title, principal) "
            "VALUES ('chat', $1, $2) RETURNING id", title, principal,
        )
        await conn.execute(
            "INSERT INTO consult_turns (session_id, role, content) VALUES ($1, 'user', $2)",
            sid, f"question of {title}",
        )
        await conn.execute(
            "INSERT INTO consult_turns (session_id, role, content, usage) "
            "VALUES ($1, 'assistant', $2, $3::jsonb)",
            sid, f"answer of {title}",
            json.dumps({"input_tokens": tokens, "output_tokens": tokens // 10}),
        )
    return str(sid)


@pytest.mark.asyncio
async def test_two_users_see_only_their_own_sessions_and_usage(
    client, pool, app,
) -> None:
    alice_id, _, alice = await _invite_and_accept(
        client, "alice", "reader-own-rows", "alice's own password",
    )
    bob_id, _, bob = await _invite_and_accept(
        client, "bob", "operator", "bob's own password!",
    )
    a_sid = await _seed_session(pool, f"user:{alice_id}", "b3-alice", 1000)
    b_sid = await _seed_session(pool, f"user:{bob_id}", "b3-bob", 50)
    op_sid = await _seed_session(pool, "operator:bearer:b3b3b3b3b3b3", "b3-operator", 7)

    async def _ids(headers: dict[str, str]) -> set[str]:
        r = await client.get("/api/v1/consult/sessions?limit=200", headers=headers)
        assert r.status_code == 200, r.text
        return {s["id"] for s in r.json()}

    assert await _ids(_as(alice)) == {a_sid}
    # bob is in the operator CLASS, and still sees only his own sessions:
    # privacy is per signed-in user, not per role.
    assert await _ids(_as(bob)) == {b_sid}
    # The operator's bearer: every session, as before.
    assert {a_sid, b_sid, op_sid} <= await _ids(OP)

    # Cross-read refused, both ways; own read admitted.
    r = await client.get(f"/api/v1/consult/sessions/{b_sid}", headers=_as(alice))
    assert r.status_code == 403 and r.json()["detail"]["error"] == "not_row_owner"
    r = await client.get(f"/api/v1/consult/sessions/{op_sid}", headers=_as(bob))
    assert r.status_code == 403
    r = await client.get(f"/api/v1/consult/sessions/{a_sid}", headers=_as(alice))
    assert r.status_code == 200 and r.json()["title"] == "b3-alice"
    r = await client.get(f"/api/v1/consult/sessions/{a_sid}", headers=OP)
    assert r.status_code == 200

    # Usage keyed to user:<id>.
    r = await client.get("/api/v1/consult/usage?days=2", headers=_as(alice))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["user"] == f"user:{alice_id}"
    assert body["totals"]["input_tokens"] == 1000
    assert body["totals"]["turns"] == 1
    r = await client.get(
        f"/api/v1/consult/usage?user=user:{bob_id}", headers=_as(alice),
    )
    assert r.status_code == 403 and r.json()["detail"]["error"] == "not_row_owner"
    r = await client.get(f"/api/v1/consult/usage?user=user:{alice_id}", headers=OP)
    assert r.status_code == 200 and r.json()["totals"]["input_tokens"] == 1000

    # The owner check the consult run routes use (continue / stop /
    # synthesize / run status), on the real driver.
    store = app.state.registry_deps.descriptor_registry.pg

    def _req(state: dict[str, Any]) -> Request:
        return Request({"type": "http", "method": "POST", "path": "/x",
                        "headers": [], "state": state})

    alice_state: dict[str, Any] = {}
    alice_user = await _fetch_user(pool, alice_id)
    sa.stamp_session(alice_state, alice_user)
    await sa.require_consult_session_owner(store, _req(alice_state), a_sid)
    with pytest.raises(HTTPException) as exc:
        await sa.require_consult_session_owner(store, _req(alice_state), b_sid)
    assert exc.value.status_code == 403
    await sa.require_consult_session_owner(store, _req({"principal": "operator"}), b_sid)


async def _fetch_user(pool, user_id: str) -> U.User:
    async with pool.acquire() as conn:
        user = await U.get_user(conn, user_id)
    assert user is not None
    return user


# ---------------------------------------------------------------------------
# OIDC — declared, invite-only, refuses an unknown subject by name
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unknown_oidc_subject_is_refused_by_name(client, pool) -> None:
    oidc = U.get_provider("oidc")
    assert oidc.built is False
    async with pool.acquire() as conn:
        with pytest.raises(U.AuthRefused) as exc:
            await oidc.authenticate(conn, {"subject": "b3-stranger-42"})
    assert exc.value.code == "oidc_subject_unknown"
    assert "b3-stranger-42" in exc.value.message
    assert "invite-only" in exc.value.message

    # The operator binds a subject to an invited user → the map resolves it.
    r = await client.post(
        "/api/v1/auth/invite", headers=OP, json={"email": _email("erin"), "role": "read"},
    )
    erin = r.json()["user"]["id"]
    r = await client.patch(
        f"/api/v1/auth/users/{erin}", headers=OP,
        json={"auth_provider": "oidc", "external_subject": "b3-erin-sub",
              "status": "active"},
    )
    assert r.status_code == 200, r.text
    async with pool.acquire() as conn:
        user = await oidc.authenticate(conn, {"subject": "b3-erin-sub"})
    assert user.id == erin and user.actor == f"user:{erin}"

    with pytest.raises(U.AuthRefused) as exc:
        await oidc.exchange_code("code", "https://t/cb")  # type: ignore[attr-defined]
    assert exc.value.code == "oidc_exchange_not_built"
    assert "SEAMS #73" in exc.value.message


# ---------------------------------------------------------------------------
# The operator's admin
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_disable_revokes_sessions_and_activation_needs_a_credential(
    client, pool,
) -> None:
    fay, _, cookie = await _invite_and_accept(client, "fay", "read", "fay's own password")
    r = await client.get("/api/v1/auth/users", headers=OP)
    assert r.status_code == 200 and fay in {u["id"] for u in r.json()["users"]}

    r = await client.patch(f"/api/v1/auth/users/{fay}", headers=OP, json={"status": "disabled"})
    assert r.status_code == 200 and r.json()["user"]["status"] == "disabled"
    async with pool.acquire() as conn:
        live = await conn.fetchval(
            "SELECT count(*) FROM user_sessions WHERE user_id = $1 AND revoked_at IS NULL",
            uuid.UUID(fay),
        )
    assert live == 0
    r = await client.get("/api/v1/auth/me", headers=_as(cookie))
    assert r.status_code == 401
    r = await client.post(
        "/api/v1/auth/sign-in", json={"email": _email("fay"), "password": "fay's own password"},
    )
    assert r.status_code == 403 and r.json()["detail"]["error"] == "user_disabled"

    # An invited user with no password cannot be activated by hand.
    r = await client.post(
        "/api/v1/auth/invite", headers=OP, json={"email": _email("gus"), "role": "read"},
    )
    gus = r.json()["user"]["id"]
    r = await client.patch(f"/api/v1/auth/users/{gus}", headers=OP, json={"status": "active"})
    assert r.status_code == 409 and r.json()["detail"]["error"] == "no_credential"
    # Re-inviting an active account is refused; re-inviting an invited one
    # replaces its token.
    r = await client.post(
        "/api/v1/auth/invite", headers=OP, json={"email": _email("fay"), "role": "read"},
    )
    assert r.status_code == 409


@pytest.mark.asyncio
async def test_repeated_failures_lock_the_email_for_a_window(client) -> None:
    await _invite_and_accept(client, "hal", "read", "hal's own password")
    for _ in range(U.SIGN_IN_FAILURE_LIMIT):
        r = await client.post(
            "/api/v1/auth/sign-in", json={"email": _email("hal"), "password": "guess guess guess"},
        )
        assert r.status_code == 401
    r = await client.post(
        "/api/v1/auth/sign-in", json={"email": _email("hal"), "password": "hal's own password"},
    )
    assert r.status_code == 429 and r.json()["detail"]["error"] == "too_many_attempts"


# ---------------------------------------------------------------------------
# The non-HTTP gates + the slim rule
# ---------------------------------------------------------------------------


def test_ws_and_sse_gate_accept_a_stamped_session(monkeypatch) -> None:
    from legba.data.registry._deps import _authorize_ws_token

    monkeypatch.setenv("LEGBA_REGISTRY_API_TOKEN", OP_TOKEN)
    monkeypatch.delenv("LEGBA_REGISTRY_SERVICE_TOKEN", raising=False)
    user = U.User(id="11111111-1111-1111-1111-111111111111", email=None,
                  display_name="", role="read", status="active",
                  auth_provider="local_password")
    state: dict[str, Any] = {}
    sa.stamp_session(state, user)
    assert _authorize_ws_token(None, None, state=state) == f"user:{user.id}"
    assert _authorize_ws_token(None, None, surface="sse", state=state) == f"user:{user.id}"
    # A presented bearer always takes the bearer path.
    assert _authorize_ws_token(OP_TOKEN, None, state=state).startswith("operator:")
    with pytest.raises(HTTPException) as exc:
        _authorize_ws_token(None, None, state={})
    assert exc.value.status_code == 403


def test_cookie_parse() -> None:
    assert sa.session_cookie_from_header("a=1; legba_session=abc; b=2") == "abc"
    assert sa.session_cookie_from_header("xlegba_session=abc") is None
    assert sa.session_cookie_from_header("") is None


def test_users_modules_import_registry_slim() -> None:
    probe = textwrap.dedent(
        """
        import sys
        for blocked in ("feedparser", "telethon", "warcio", "aiobotocore",
                        "pycountry", "networkx", "qdrant_client", "dspy"):
            sys.modules[blocked] = None
        import legba.data.users as u
        import legba.data.registry.session_auth as sa
        import legba.data.registry.users_api as ua
        import legba.data.registry.users_admin_api as uaa
        leaked = sorted(m for m in sys.modules
                        if m.startswith("legba.data.analysts") or m.startswith("legba.runtime"))
        assert not leaked, leaked
        assert "legba.data.registry.api" not in sys.modules, "users_api pulled the api hub"
        assert u.PROVIDERS and sa.SessionCookieMiddleware and ua.build_users_router
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
