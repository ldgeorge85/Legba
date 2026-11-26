# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""X4 GATE — the bearer stamp moves BEFORE the route-role gate; unstamped is
refused; two owner checks; the sign-in lockout in Redis.

The lane's DONE test:

  * a route mounted WITHOUT ``require_bearer`` and without a stamp answers
    403 ``role_unresolved`` at the gate (fixture app), and with the bearer
    middleware in front the gate answers the middleware's own 401 — the
    handler never runs either way;
  * on the REAL app (``server.create_app``): the middleware order (bearer
    outermost, then session) and a probe route that forgot ``require_bearer``
    — unstamped 401 at the gate; the operator bearer, the service bearer and
    a signed-in session user each reach it stamped in their own class;
  * the TREE TEST: every mounted route of the real app, unstamped, is either
    public or refused by the gate, and every non-public route is stamped
    before the gate by the bearer middleware (operator, service, the WS
    subprotocol, the SSE ``?token=``) and by the session middleware; the
    census by stamp source is printed;
  * the events socket unstamped closes 1008 AT THE GATE (its handler is
    never called);
  * a second user on another's consult stream / deep-consult task gets 403;
    the owner, the operator and dev mode pass; a variant spelling of another
    user's task id is refused, not matched loosely;
  * the lockout survives a process restart and is shared across processes
    (fixture Redis: the isolated test stack's), expires with its window, and
    degrades to per-process — loudly — when the store fails;
  * the rate-limit key is the stamped actor, never the header;
  * the new modules import under the registry-slim rule.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import os
import pathlib
import re
import subprocess
import sys
import textwrap
import uuid
from typing import Any

import asyncpg
import httpx
import pytest
import pytest_asyncio
from fastapi import Depends, FastAPI, HTTPException, Request
from starlette.requests import HTTPConnection
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from legba.data import users as U
from legba.data.config import NatsConfig, PostgresConfig
from legba.data.registry import auth_state
from legba.data.registry import bearer_auth as ba
from legba.data.registry import route_roles as rr
from legba.data.registry import session_auth as sa
from legba.data.registry._deps import (
    HUMAN_ONLY_ROUTES,
    WS_BEARER_SUBPROTOCOL,
    human_only_reason,
    principal_actor,
    require_bearer,
)

_REPO = pathlib.Path(__file__).resolve().parents[2]
_SRC = _REPO / "src"

OP_TOKEN = "x4-gate-operator-" + "1" * 24
SVC_TOKEN = "x4-gate-service-" + "2" * 24
_DOMAIN = "x4gate.example"
_PW = "a long enough password"
_ID = "x1"


def _concrete(template: str) -> str:
    return re.sub(r"\{[^}]+\}", _ID, template)


@pytest.fixture
def tokens(monkeypatch):
    monkeypatch.setenv("LEGBA_REGISTRY_API_TOKEN", OP_TOKEN)
    monkeypatch.setenv("LEGBA_REGISTRY_SERVICE_TOKEN", SVC_TOKEN)
    monkeypatch.delenv("LEGBA_DEV_MODE", raising=False)
    monkeypatch.delenv(rr.ROLE_OVERRIDE_ENV, raising=False)
    monkeypatch.delenv(sa.COOKIE_INSECURE_ENV, raising=False)


# ---------------------------------------------------------------------------
# 1. The fixture app: a route that forgot require_bearer
# ---------------------------------------------------------------------------


def _forgetful_app(*, with_middleware: bool) -> tuple[FastAPI, list[dict[str, Any]]]:
    seen: list[dict[str, Any]] = []
    app = FastAPI(dependencies=[Depends(rr.enforce_route_role)])
    if with_middleware:
        app.add_middleware(ba.BearerPrincipalMiddleware)

    @app.get("/api/v1/x4/forgot-the-bearer")
    async def forgot(request: Request) -> dict[str, Any]:  # no require_bearer
        stamp = {
            k: getattr(request.state, k, None)
            for k in ("principal", "principal_actor", rr.STATE_ROLE_ATTR,
                      rr.STATE_SUBJECT_ATTR, rr.STATE_AUTH_SOURCE_ATTR)
        }
        seen.append(stamp)
        return stamp

    return app, seen


def test_unstamped_route_without_require_bearer_is_403_at_the_gate(tokens) -> None:
    app, seen = _forgetful_app(with_middleware=False)
    r = TestClient(app).get(
        "/api/v1/x4/forgot-the-bearer", headers={"Authorization": f"Bearer {OP_TOKEN}"},
    )
    # No middleware stamped it — even a valid bearer is unresolved here,
    # because nothing ran before the gate to resolve it.
    assert r.status_code == 403, r.text
    assert r.json()["detail"]["error"] == "role_unresolved"
    assert seen == []  # the handler never ran


def test_with_the_middleware_the_gate_answers_its_refusal_and_admits_the_stamp(tokens) -> None:
    app, seen = _forgetful_app(with_middleware=True)
    c = TestClient(app)
    r = c.get("/api/v1/x4/forgot-the-bearer")
    assert r.status_code == 401, r.text
    assert r.json()["detail"] == "missing Authorization: Bearer <token>"
    r = c.get("/api/v1/x4/forgot-the-bearer", headers={"Authorization": "Bearer nope"})
    assert (r.status_code, r.json()["detail"]) == (403, "invalid bearer token")
    assert seen == []
    for tok, kind in ((OP_TOKEN, "operator"), (SVC_TOKEN, "service")):
        r = c.get("/api/v1/x4/forgot-the-bearer", headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code == 200, r.text
        assert r.json() == {
            "principal": kind,
            "principal_actor": principal_actor(kind, tok),
            "legba_role": "operator",
            "legba_subject": kind,
            "legba_auth_source": "bearer",
        }
    assert len(seen) == 2


def test_misconfigured_is_still_503_and_dev_mode_still_the_operator(monkeypatch) -> None:
    monkeypatch.delenv("LEGBA_REGISTRY_API_TOKEN", raising=False)
    monkeypatch.delenv("LEGBA_REGISTRY_SERVICE_TOKEN", raising=False)
    monkeypatch.delenv("LEGBA_DEV_MODE", raising=False)
    app, seen = _forgetful_app(with_middleware=True)
    r = TestClient(app).get("/api/v1/x4/forgot-the-bearer")
    assert r.status_code == 503 and "misconfigured" in r.json()["detail"]
    assert seen == []
    monkeypatch.setenv("LEGBA_DEV_MODE", "1")
    r = TestClient(app).get("/api/v1/x4/forgot-the-bearer")
    assert r.status_code == 200, r.text
    assert r.json()["legba_role"] == "operator"
    assert r.json()["legba_auth_source"] == "dev"
    assert r.json()["principal"] is None  # /auth/me keeps saying "anonymous"
    assert r.json()["principal_actor"] == "anonymous"


def test_require_bearer_reads_the_stamp_and_rechecks_human_only(tokens) -> None:
    from starlette.requests import Request as SRequest

    def _req(path: str, method: str = "GET") -> SRequest:
        return SRequest({"type": "http", "method": method, "path": path, "headers": []})

    req = _req("/api/v1/registry/descriptors/target")
    req.state.legba_auth_source = "bearer"
    req.state.principal = "operator"
    req.state.principal_actor = "operator:bearer:stamped00001"
    # The stamp wins — no Authorization header needed (nor read).
    assert require_bearer(authorization=None, request=req) == "operator:bearer:stamped00001"
    method, template, _ = HUMAN_ONLY_ROUTES[1]
    req = _req(_concrete(template), method)
    req.state.legba_auth_source = "bearer"
    req.state.principal = "service"
    req.state.principal_actor = "service:bearer:stamped00002"
    with pytest.raises(HTTPException) as exc:
        require_bearer(authorization=None, request=req)
    assert exc.value.status_code == 403
    assert str(exc.value.detail).startswith("service_principal_refused")


# ---------------------------------------------------------------------------
# 2. The real app
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
        await conn.execute("DELETE FROM consult_sessions WHERE title LIKE 'x4gate-%'")
        await conn.execute("DELETE FROM users WHERE email LIKE $1", f"%@{_DOMAIN}")
    U.local_password_provider().failures.clear()
    auth_state._local.clear()


def _real_app(migrated_pg: PostgresConfig, monkeypatch) -> FastAPI:
    from legba.data.registry.credentials import MASTER_KEY_ENV
    from legba.data.registry.server import create_app

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
async def app(migrated_pg: PostgresConfig, pool, tokens, monkeypatch):
    """The real app, its Postgres store connected (lifespan not run: no NATS,
    no Redis — the run owners are the in-process fallback here)."""
    a = _real_app(migrated_pg, monkeypatch)
    probe_seen: list[dict[str, Any]] = []

    # A probe route that FORGOT require_bearer, mounted on the real app.
    @a.get("/api/v1/x4/probe")
    async def probe(request: Request) -> dict[str, Any]:
        stamp = {
            k: getattr(request.state, k, None)
            for k in ("principal", "principal_actor", rr.STATE_ROLE_ATTR,
                      rr.STATE_SUBJECT_ATTR, rr.STATE_AUTH_SOURCE_ATTR)
        }
        probe_seen.append(stamp)
        return stamp

    a.state.x4_probe_seen = probe_seen
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


async def _user(pool: asyncpg.Pool, name: str, role: str) -> tuple[str, str]:
    """→ (user id, session cookie value) — a real invited, accepted, signed-in user."""
    async with pool.acquire() as conn:
        invite = await U.create_invite(
            conn, email=f"{name}@{_DOMAIN}", role=role, invited_by="operator",
            display_name=name,
        )
        user = await U.accept_invite(conn, token=invite.token, password=_PW)
        session = await U.create_session(conn, user)
    return str(user.id), session.token


def _as(cookie: str) -> dict[str, str]:
    return {"Cookie": f"{sa.SESSION_COOKIE_NAME}={cookie}"}


OP = {"Authorization": f"Bearer {OP_TOKEN}"}
SVC = {"Authorization": f"Bearer {SVC_TOKEN}"}


def test_middleware_order_bearer_outermost_then_session(migrated_pg, tokens, monkeypatch) -> None:
    a = _real_app(migrated_pg, monkeypatch)
    order = [m.cls.__name__ for m in a.user_middleware]  # [0] = outermost
    assert order[:2] == ["BearerPrincipalMiddleware", "SessionCookieMiddleware"], order
    assert order.index("DisplayPolicyMiddleware") > 1, order


@pytest.mark.asyncio
async def test_real_app_probe_without_require_bearer(client, app, pool) -> None:
    seen = app.state.x4_probe_seen
    r = await client.get("/api/v1/x4/probe")
    assert r.status_code == 401, r.text  # the gate, answering the middleware
    r = await client.get("/api/v1/x4/probe", headers={"Authorization": "Bearer nope"})
    assert r.status_code == 403, r.text
    assert seen == []  # the handler never ran unstamped

    r = await client.get("/api/v1/x4/probe", headers=OP)
    assert r.status_code == 200 and r.json()["principal"] == "operator", r.text
    assert r.json()["legba_role"] == "operator"
    r = await client.get("/api/v1/x4/probe", headers=SVC)
    assert r.status_code == 200 and r.json()["principal"] == "service", r.text
    assert (r.json()["legba_role"], r.json()["legba_subject"]) == ("operator", "service")
    uid, cookie = await _user(pool, "rita", "read")
    r = await client.get("/api/v1/x4/probe", headers=_as(cookie))
    assert r.status_code == 200, r.text
    assert r.json() == {
        "principal": "user", "principal_actor": f"user:{uid}",
        "legba_role": "read", "legba_subject": uid, "legba_auth_source": "session",
    }
    assert len(seen) == 3


@pytest.mark.asyncio
async def test_three_principals_reach_their_classes_on_the_real_app(client, pool) -> None:
    r = await client.get("/api/v1/auth/me", headers=OP)
    assert (r.status_code, r.json()["kind"], r.json()["role"]) == (200, "operator", "operator")
    r = await client.get("/api/v1/auth/me", headers=SVC)
    assert (r.status_code, r.json()["kind"], r.json()["role"]) == (200, "service", "operator")
    uid, cookie = await _user(pool, "sam", "reader-own-rows")
    r = await client.get("/api/v1/auth/me", headers=_as(cookie))
    assert r.status_code == 200, r.text
    assert (r.json()["kind"], r.json()["role"], r.json()["actor"]) == (
        "user", "reader-own-rows", f"user:{uid}",
    )
    # Class, not just identity: the user is refused an operator route BY ROLE
    # (stamped), the service bearer on a human-only route BY THE MIDDLEWARE's
    # refusal at the gate — the same detail require_bearer gives.
    r = await client.post("/api/v1/registry/stack", headers=_as(cookie), json=[1])
    assert r.status_code == 403 and r.json()["detail"]["error"] == "role_forbidden"
    r = await client.post("/api/v1/registry/vault/secrets", headers=SVC, json=[1])
    assert r.status_code == 403
    assert str(r.json()["detail"]).startswith("service_principal_refused")
    # A bearer beside a cookie is the bearer (the session is not resolved).
    r = await client.get("/api/v1/auth/me", headers={**OP, **_as(cookie)})
    assert r.json()["kind"] == "operator"


# ---------------------------------------------------------------------------
# 3. The tree test — every mounted route, by stamp source
# ---------------------------------------------------------------------------


def _scope(method: str, path: str, headers: dict[str, str], *, query: str = "") -> dict:
    ws = method == "WEBSOCKET"
    return {
        "type": "websocket" if ws else "http",
        "method": None if ws else method,
        "path": path,
        "query_string": query.encode(),
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
    }


def _gate(scope: dict) -> tuple[str, Any]:
    """Run the bearer middleware's resolution, then the gate, on ``scope``."""
    state = scope.setdefault("state", {})
    ba.resolve_scope(scope, state)
    try:
        rr.enforce_route_role(HTTPConnection(scope))
    except HTTPException as exc:
        return "refused", (exc.status_code, exc.detail)
    except Exception as exc:  # WebSocketException
        return "refused", (getattr(exc, "code", None), getattr(exc, "reason", None))
    return "admitted", state


def _requires_bearer(route: Any) -> bool:
    def calls(dep: Any) -> bool:
        return dep.call is require_bearer or any(calls(d) for d in dep.dependencies)

    dep = getattr(getattr(route, "original_route", route), "dependant", None)
    return dep is not None and calls(dep)


def test_every_mounted_route_is_public_or_stamped_before_the_gate(
    migrated_pg, tokens, monkeypatch,
) -> None:
    from fastapi.routing import iter_route_contexts

    a = _real_app(migrated_pg, monkeypatch)
    census = {"public": 0, "route_dep_require_bearer": 0, "self_authenticating": 0}
    refused_unstamped = admitted_operator = admitted_service = 0
    service_human_only = session_read_admitted = session_read_refused_by_role = 0
    seen: set[tuple[str, str]] = set()
    contexts = list(iter_route_contexts(a.routes))
    for ctx in contexts:
        orig = getattr(ctx, "original_route", ctx)
        path = ctx.path_format or ctx.path
        if "WebSocket" in type(orig).__name__:
            methods = ["WEBSOCKET"]
            path = path if path.startswith("/api/") else "/api/v1/registry" + path
        else:
            methods = sorted(ctx.methods or [])
        for method in methods:
            seen.add((method, path))
            concrete = _concrete(path)
            public = rr.is_public_route(method, concrete)
            if public:
                census["public"] += 1
            elif _requires_bearer(ctx):
                census["route_dep_require_bearer"] += 1
            else:
                census["self_authenticating"] += 1

            # (a) unstamped: refused unless public.
            verdict, info = _gate(_scope(method, concrete, {}))
            if public:
                assert verdict == "admitted", (method, path, info)
                continue
            assert verdict == "refused", (method, path)
            refused_unstamped += 1

            # (b) the operator bearer — every carrier the route accepts.
            sse = concrete.startswith(ba.QUERY_TOKEN_HTTP_PREFIXES)
            if method == "WEBSOCKET":
                b64 = base64.urlsafe_b64encode(OP_TOKEN.encode()).decode().rstrip("=")
                op_scope = _scope(method, concrete, {
                    "Sec-WebSocket-Protocol": f"{WS_BEARER_SUBPROTOCOL}, {b64}",
                })
            elif sse:
                op_scope = _scope(method, concrete, {}, query=f"token={OP_TOKEN}")
            else:
                op_scope = _scope(method, concrete, OP)
            verdict, state = _gate(op_scope)
            assert verdict == "admitted", (method, path, state)
            assert state[rr.STATE_AUTH_SOURCE_ATTR] == "bearer"
            admitted_operator += 1

            # (c) the service bearer: operator class, refused only human-only.
            verdict, info = _gate(_scope(method, concrete, SVC))
            if method != "WEBSOCKET" and not sse and human_only_reason(method, concrete):
                assert verdict == "refused" and info[0] == 403, (method, path, info)
                assert str(info[1]).startswith("service_principal_refused")
                service_human_only += 1
            elif method != "WEBSOCKET" and not sse:
                assert verdict == "admitted", (method, path, info)
                admitted_service += 1

            # (d) a session user (the session middleware's stamp, no bearer):
            # stamped before the gate — admitted by class or refused BY ROLE.
            scope = _scope(method, concrete, {})
            scope["state"] = {}
            ba.resolve_scope(scope, scope["state"])
            assert scope["state"][rr.STATE_BEARER_PRESENTED_ATTR] is False
            sa.stamp_session(scope["state"], U.User(
                id="00000000-0000-0000-0000-0000000000a4", email=None, display_name="",
                role="read", status="active", auth_provider="local_password",
            ))
            try:
                rr.enforce_route_role(HTTPConnection(scope))
                session_read_admitted += 1
            except HTTPException as exc:
                assert exc.detail["error"] == "role_forbidden", (method, path)
                session_read_refused_by_role += 1
    total = sum(census.values())
    print(
        f"\nX4 GATE census: {total} mounted (method, route) pairs — "
        f"public {census['public']}, route-dependency require_bearer "
        f"{census['route_dep_require_bearer']}, self-authenticating (WS/SSE) "
        f"{census['self_authenticating']}; unstamped refused at the gate "
        f"{refused_unstamped}; bearer-stamped operator admitted {admitted_operator}; "
        f"service admitted {admitted_service} / refused human-only {service_human_only}; "
        f"session-stamped `read` admitted {session_read_admitted} / refused by role "
        f"{session_read_refused_by_role}"
    )
    assert refused_unstamped == total - census["public"]
    assert admitted_operator == refused_unstamped
    assert census["self_authenticating"] == 2, census  # events WS + the SSE relay
    assert service_human_only >= len(HUMAN_ONLY_ROUTES)
    assert total >= 160, total
    # Every public entry is mounted (a stale public row would be a silent hole).
    for (m, p) in rr.PUBLIC_ROUTES:
        assert (m, p) in seen, (m, p)


def test_events_socket_unstamped_closes_1008_at_the_gate(migrated_pg, tokens, monkeypatch) -> None:
    import legba.data.registry.api as api_mod

    calls: list[Any] = []

    def _recorder(*a: Any, **k: Any) -> str:
        calls.append((a, k))
        raise HTTPException(status_code=403, detail="recorder")

    monkeypatch.setattr(api_mod, "_authorize_ws_token", _recorder)
    a = _real_app(migrated_pg, monkeypatch)
    c = TestClient(a)
    with pytest.raises(WebSocketDisconnect) as exc:
        with c.websocket_connect("/api/v1/registry/events?token=wrong") as ws:
            ws.receive_text()
    assert exc.value.code == 1008
    with pytest.raises(WebSocketDisconnect):
        with c.websocket_connect("/api/v1/registry/events") as ws:
            ws.receive_text()
    assert calls == []  # the gate closed both; the handler was never called
    # A valid credential passes the gate and reaches the handler.
    with pytest.raises(WebSocketDisconnect):
        with c.websocket_connect(f"/api/v1/registry/events?token={OP_TOKEN}") as ws:
            ws.receive_text()
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# 4. Owner checks — the SSE relay and the deep-consult task read
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_second_user_is_refused_on_anothers_stream(client, app, pool) -> None:
    # No NATS in this fixture: a caller that PASSES the owner check gets the
    # relay's own 503 ("requires a connected NATS store"); one that fails it
    # gets 403 before any subscription.
    app.state.registry_deps.nats_store = None
    _, alice = await _user(pool, "alice", "reader-own-rows")
    _, bob = await _user(pool, "bob", "reader-own-rows")
    rid, rid_op = str(uuid.uuid4()), str(uuid.uuid4())
    s = "/api/v1/consult/stream/"

    r = await client.get(s + rid, headers=_as(alice))  # alice opens first: hers
    assert r.status_code == 503, r.text
    r = await client.get(s + rid, headers=_as(bob))
    assert r.status_code == 403, r.text
    assert r.json()["detail"]["error"] == "not_row_owner"
    r = await client.get(s + rid, headers=_as(alice))  # her reconnect still passes
    assert r.status_code == 503
    r = await client.get(s + rid + f"?token={OP_TOKEN}")  # the operator passes
    assert r.status_code == 503

    r = await client.get(s + rid_op + f"?token={OP_TOKEN}")  # the operator's run
    assert r.status_code == 503
    r = await client.get(s + rid_op, headers=_as(alice))
    assert r.status_code == 403

    # POST /consult naming another user's run id is refused up front (before
    # any descriptor lookup or invoke).
    r = await client.post(
        "/api/v1/consult", headers=_as(bob),
        json={"question": "x4 gate", "request_id": rid},
    )
    assert r.status_code == 403, r.text
    assert r.json()["detail"]["error"] == "not_row_owner"
    # Unstamped: refused at the gate, never reaching the owner check.
    r = await client.get(s + rid)
    assert r.status_code == 403 and r.json()["detail"] == "invalid token"


@pytest.mark.asyncio
async def test_a_second_user_is_refused_on_anothers_deep_task(client, pool) -> None:
    alice_id, alice = await _user(pool, "dalice", "reader-own-rows")
    _, bob = await _user(pool, "dbob", "reader-own-rows")
    run8 = uuid.uuid4().hex[:8]
    task = f"deep_consult.x4gate.{run8}"
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO consult_sessions (mode, title, principal, task_id) "
            "VALUES ('deep', 'x4gate-deep', $1, $2)",
            f"user:{alice_id}", task,
        )
    d = "/api/v1/deep_consult/"
    r = await client.get(d + task, headers=_as(alice))
    assert (r.status_code, r.json()["status"]) == (200, "running"), r.text
    r = await client.get(d + task, headers=_as(bob))
    assert r.status_code == 403 and r.json()["detail"]["error"] == "not_row_owner", r.text
    # The status read matches the finding on the 8-hex run prefix — a variant
    # spelling must not slip past the owner check on a looser match.
    for variant in (run8, f"other.{run8}"):
        r = await client.get(d + variant, headers=_as(bob))
        assert r.status_code == 403, (variant, r.text)
    r = await client.get(d + task, headers=OP)  # the operator passes
    assert r.status_code == 200, r.text


@pytest.mark.asyncio
async def test_run_owner_fallback_logs_when_redis_fails(caplog) -> None:
    class _Broken:
        async def set(self, *a: Any, **k: Any) -> Any:
            raise ConnectionError("redis down")

    auth_state._local.clear()
    auth_state.install(_Broken())
    try:
        with caplog.at_level(logging.WARNING):
            assert await auth_state.claim_run_owner("r-x4", "user:a") == "user:a"
            assert await auth_state.claim_run_owner("r-x4", "user:b") == "user:a"
        assert "run_owner_unavailable" in caplog.text
    finally:
        auth_state.install(None)
        auth_state._local.clear()


# ---------------------------------------------------------------------------
# 5. The lockout in Redis (the isolated test stack's Redis on 127.0.0.1:6379)
# ---------------------------------------------------------------------------

_REDIS_PROBE_ENV = {
    k: v for k, v in os.environ.items()
    if k not in ("LEGBA_DATA_REDIS_PASSWORD", "LEGBA_REDIS_PASSWORD", "REDIS_PASSWORD")
}


def _run_process(code: str) -> str:
    result = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code)], capture_output=True, text=True,
        timeout=120,
        env={**_REDIS_PROBE_ENV, "PYTHONPATH": os.pathsep.join(
            p for p in (str(_SRC), os.environ.get("PYTHONPATH", "")) if p)},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


def test_lockout_is_shared_across_processes_and_survives_a_restart() -> None:
    email = f"lock-{uuid.uuid4().hex[:8]}@{_DOMAIN}"
    common = f"""
        import asyncio
        from redis.asyncio import Redis
        from legba.data import users as U
        EMAIL = {email!r}
        def store():
            return U.RedisFailureStore(Redis(host="127.0.0.1", port=6379))
    """
    try:
        # Process 1: ten failed sign-ins, recorded — then the process EXITS.
        out = _run_process(common + """
        async def main():
            s = store()
            for _ in range(U.SIGN_IN_FAILURE_LIMIT):
                await s.record(EMAIL)
            print("recorded", await s.blocked(EMAIL))
        asyncio.run(main())
        """)
        assert "recorded True" in out, out
        # Process 2: a FRESH provider (empty in-process counter) with the
        # shared store installed refuses the email before any database read.
        out = _run_process(common + """
        class _NoDb:
            async def fetchrow(self, *a, **k):
                raise AssertionError("a locked email must not reach the database")
        async def main():
            p = U.LocalPasswordProvider()
            assert not p.failures.blocked(EMAIL)
            p.use_failure_store(store())
            try:
                await p.authenticate(_NoDb(), {"email": EMAIL, "password": "guess"})
            except U.AuthRefused as exc:
                print("refused", exc.code, exc.status)
        asyncio.run(main())
        """)
        assert "refused too_many_attempts 429" in out, out
    finally:
        _run_process(common + """
        asyncio.run(store().clear(EMAIL))
        """)


@pytest.mark.asyncio
async def test_lockout_window_expires_and_success_clears() -> None:
    from redis.asyncio import Redis

    client = Redis(host="127.0.0.1", port=6379)
    email = f"win-{uuid.uuid4().hex[:8]}@{_DOMAIN}"
    store = U.RedisFailureStore(client, limit=3, window_s=1.0)
    try:
        for _ in range(3):
            await store.record(email)
        assert await store.blocked(email)
        ttl = await client.ttl(store.key(email))
        assert 0 < ttl <= 2, ttl  # the key expires with its window
        assert email not in store.key(email)  # hashed: no address in Redis
        await asyncio.sleep(1.2)
        assert not await store.blocked(email)
        await store.record(email)
        await store.clear(email)
        assert await client.exists(store.key(email)) == 0
    finally:
        await store.clear(email)
        await client.aclose()


@pytest.mark.asyncio
async def test_lockout_degrades_to_per_process_loudly(caplog) -> None:
    class _Down:
        async def blocked(self, key: str) -> bool:
            raise ConnectionError("redis down")

        record = clear = blocked

    class _NoUser:
        async def fetchrow(self, *a: Any, **k: Any) -> None:
            return None

    p = U.LocalPasswordProvider()
    p.use_failure_store(_Down())
    email = f"down-{uuid.uuid4().hex[:8]}@{_DOMAIN}"
    with caplog.at_level(logging.WARNING):
        for _ in range(U.SIGN_IN_FAILURE_LIMIT):
            with pytest.raises(U.AuthRefused) as exc:
                await p.authenticate(_NoUser(), {"email": email, "password": "guess"})
            assert exc.value.code == "invalid_credentials"
        with pytest.raises(U.AuthRefused) as exc:
            await p.authenticate(_NoUser(), {"email": email, "password": "guess"})
    assert exc.value.code == "too_many_attempts"
    assert "auth.lockout.store_unavailable" in caplog.text


@pytest.mark.asyncio
async def test_the_lifespan_installs_redis_for_both(monkeypatch) -> None:
    for k in ("LEGBA_DATA_REDIS_PASSWORD", "LEGBA_REDIS_PASSWORD", "REDIS_PASSWORD"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("LEGBA_DATA_REDIS_HOST", "127.0.0.1")
    monkeypatch.setenv("LEGBA_DATA_REDIS_PORT", "6379")
    provider = U.local_password_provider()
    rid = f"x4-{uuid.uuid4().hex}"
    await auth_state.open_auth_state()
    try:
        assert isinstance(provider.failure_store, U.RedisFailureStore)
        assert auth_state.redis_client() is not None
        assert await auth_state.claim_run_owner(rid, "user:first") == "user:first"
        auth_state._local.clear()  # "another process": no in-process memory
        assert await auth_state.claim_run_owner(rid, "user:second") == "user:first"
        await auth_state.redis_client().delete(auth_state.RUN_OWNER_KEY_PREFIX + rid)
    finally:
        await auth_state.close_auth_state()
    assert provider.failure_store is None and auth_state.redis_client() is None


# ---------------------------------------------------------------------------
# 6. Rate-limit key + the registry-slim import guard
# ---------------------------------------------------------------------------


def test_rate_limit_keys_on_the_stamped_actor_never_the_header() -> None:
    from starlette.requests import Request as SRequest

    from legba.data.registry.rate_limit import _principal_key

    req = SRequest({
        "type": "http", "method": "POST", "path": "/api/v1/consult",
        "headers": [(b"authorization", b"Bearer some-secret-token")],
        "client": ("10.0.0.9", 1234),
    })
    assert _principal_key(req) == "10.0.0.9"  # unstamped: the header is NOT read
    req.state.principal_actor = "operator:bearer:abcdef012345"
    assert _principal_key(req) == "operator:bearer:abcdef012345"
    req.state.principal_actor = "user:42"
    assert _principal_key(req) == "user:42"


def test_gate_modules_import_registry_slim() -> None:
    probe = textwrap.dedent(
        """
        import sys
        for blocked in ("feedparser", "telethon", "warcio", "aiobotocore",
                        "pycountry", "networkx", "qdrant_client", "dspy"):
            sys.modules[blocked] = None
        import legba.data.registry.bearer_auth as ba
        import legba.data.registry.auth_state as st
        import legba.data.registry._deps as d
        leaked = sorted(m for m in sys.modules
                        if m.startswith("legba.data.analysts") or m.startswith("legba.runtime"))
        assert not leaked, leaked
        assert "legba.data.registry.api" not in sys.modules, "pulled the api hub"
        assert "legba.data.redis" not in sys.modules, "redis client imported eagerly"
        assert ba.BearerPrincipalMiddleware and st.require_run_owner and d.stamp_bearer
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
