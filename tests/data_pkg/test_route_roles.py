# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""X2-B2 — the registry route-role gate (``route_roles.ROUTE_ROLES``).

The done test:

  * every POST/PUT/PATCH/DELETE route the REAL app (``server.create_app``)
    mounts has an explicit role in the table, and every table entry is
    mounted (no stale rows);
  * a ``read`` principal is refused — 403, the body names the role — on every
    operator route, on the vault (its GET included), and on every own-rows
    route; a ``reader-own-rows`` principal is refused on every operator route
    and ADMITTED by the gate on every own-rows route;
  * own rows, end to end over the real driver: a ``reader-own-rows``
    principal may create / change / delete ITS watch and is refused on
    another user's watch (fixture ids); the persisted ``created_by`` is read
    back off the row;
  * the ``X-Legba-Role`` override is a test affordance only — outside pytest
    the flag is ignored and logged;
  * docs/API_ROLES.md is the rendering of the table (drift test);
  * the gate module + the v3 split's leaves import under the registry-slim
    rule.

Principals come from the test override (``LEGBA_ROLE_OVERRIDE_FOR_TESTS=1``
+ ``X-Legba-Role`` / ``X-Legba-Subject``), the one path the gate honours
for a non-operator class until B3 stamps users on the request state.
"""
from __future__ import annotations

import os
import pathlib
import re
import subprocess
import sys
import textwrap
from typing import Any
from uuid import UUID

import asyncpg
import httpx
import pytest
import pytest_asyncio
from fastapi import Depends, FastAPI

from legba.data.config import NatsConfig, PostgresConfig
from legba.data.registry import route_roles as rr
from legba.data.registry.watchlist_api import build_watchlist_router

_REPO = pathlib.Path(__file__).resolve().parents[2]
_SRC = _REPO / "src"

_ID = "x1"  # stands in for every path parameter


def _concrete(template: str) -> str:
    return re.sub(r"\{[^}]+\}", _ID, template)


def _hdr(role: str, subject: str | None = None) -> dict[str, str]:
    h = {"X-Legba-Role": role}
    if subject:
        h["X-Legba-Subject"] = subject
    return h


@pytest.fixture
def override_on(monkeypatch):
    monkeypatch.setenv(rr.ROLE_OVERRIDE_ENV, "1")
    monkeypatch.delenv("LEGBA_REGISTRY_API_TOKEN", raising=False)
    monkeypatch.setenv("LEGBA_DEV_MODE", "1")


@pytest.fixture
def real_app(migrated_pg: PostgresConfig, monkeypatch) -> FastAPI:
    """The app ``create_app`` builds (lifespan NOT run — no pool, no NATS:
    the gate refuses before any handler touches either)."""
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


def _mounted_write_routes(app: FastAPI) -> set[tuple[str, str]]:
    """Every write route, read two ways (the OpenAPI spec — public API on
    every FastAPI shape — and the effective route walk, which also sees
    include_in_schema=False routes)."""
    from_spec = {
        (m.upper(), path)
        for path, ops in app.openapi()["paths"].items()
        for m in ops
        if m.upper() in rr.WRITE_METHODS
    }
    from_walk = {
        (m, p) for m, p in rr.iter_mounted_routes(app) if m in rr.WRITE_METHODS
    }
    assert from_spec, "the OpenAPI spec lists no write routes — vacuous"
    assert from_spec <= from_walk, sorted(from_spec - from_walk)
    return from_spec | from_walk


# ---------------------------------------------------------------------------
# The table vs the real app
# ---------------------------------------------------------------------------


def test_every_mounted_write_route_has_a_role(real_app: FastAPI) -> None:
    mounted = _mounted_write_routes(real_app)
    unassigned = sorted(k for k in mounted if k not in rr.ROUTE_ROLES)
    assert not unassigned, (
        "write routes with NO role in route_roles.ROUTE_ROLES (they fail "
        f"closed to operator, but must be classified): {unassigned}"
    )
    # ...and the table names nothing the app does not mount.
    all_mounted = set(rr.iter_mounted_routes(real_app)) | {
        (m.upper(), p)
        for p, ops in real_app.openapi()["paths"].items()
        for m in ops
    }
    stale = sorted(k for k in rr.ROUTE_ROLES if k not in all_mounted)
    assert not stale, f"ROUTE_ROLES entries the app does not mount: {stale}"
    # The lane's own count, so a silently-shrunk table is visible.
    assert len(mounted) >= 42, len(mounted)  # 42 @ X2-B2 (2026-10-08)


def test_the_gate_is_installed_on_every_route_of_the_real_app(
    real_app: FastAPI,
) -> None:
    assert any(
        getattr(d, "dependency", None) is rr.enforce_route_role
        for d in real_app.router.dependencies
    ), "server.create_app must install enforce_route_role app-wide"


def test_required_role_defaults_fail_closed() -> None:
    assert rr.required_role_for("GET", "/api/v1/v3/anything") == rr.ROLE_READ
    assert rr.required_role_for("HEAD", "/api/v1/whatever") == rr.ROLE_READ
    for m in rr.WRITE_METHODS:
        assert rr.required_role_for(m, "/api/v1/not/in/the/table") == rr.ROLE_OPERATOR
    # Template matching on concrete paths, method-scoped.
    assert rr.required_role_for(
        "GET", "/api/v1/registry/vault/secrets/abc/exists"
    ) == rr.ROLE_OPERATOR
    assert rr.required_role_for("PUT", "/api/v1/v3/watchlist/abc") == (
        rr.ROLE_READER_OWN_ROWS
    )
    assert rr.required_role_for("GET", "/api/v1/v3/watchlist") == rr.ROLE_READ
    # A deeper path never matches a shallower template.
    assert rr.required_role_for("POST", "/api/v1/consult/x/y") == rr.ROLE_OPERATOR


def test_the_classes_are_the_three_the_brief_names() -> None:
    assert rr.ROLES == ("read", "reader-own-rows", "operator")
    own = sorted(k for k, v in rr.ROUTE_ROLES.items() if v == rr.ROLE_READER_OWN_ROWS)
    assert 8 <= len(own) <= 10, own
    # Every vault route is operator, the GET included.
    vault = [k for k in rr.ROUTE_ROLES if "/vault/" in k[1]]
    assert len(vault) == 3 and all(rr.ROUTE_ROLES[k] == rr.ROLE_OPERATOR for k in vault)


# ---------------------------------------------------------------------------
# Refusals through the real app
# ---------------------------------------------------------------------------


async def _call(app: FastAPI, method: str, path: str, headers: dict[str, str]):
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        kwargs: dict[str, Any] = {"headers": headers}
        if method in rr.WRITE_METHODS and method != "DELETE":
            kwargs["json"] = [1]  # never a valid object body → 422 if admitted
        return await c.request(method, path, **kwargs)


def _assert_refused(resp: httpx.Response, required: str, principal_role: str) -> None:
    assert resp.status_code == 403, (resp.status_code, resp.text[:300])
    detail = resp.json()["detail"]
    assert detail["error"] == "role_forbidden"
    assert detail["required_role"] == required
    assert detail["principal_role"] == principal_role
    assert required in detail["message"]


@pytest.mark.asyncio
async def test_read_principal_refused_on_every_operator_route_and_the_vault(
    real_app: FastAPI, override_on,
) -> None:
    operator_routes = [k for k, v in rr.ROUTE_ROLES.items() if v == rr.ROLE_OPERATOR]
    assert len(operator_routes) >= 34
    for method, template in operator_routes:
        resp = await _call(real_app, method, _concrete(template), _hdr("read"))
        _assert_refused(resp, rr.ROLE_OPERATOR, rr.ROLE_READ)
    # /vault explicitly — every verb.
    for method, path in [
        ("POST", "/api/v1/registry/vault/secrets"),
        ("DELETE", "/api/v1/registry/vault/secrets/s1"),
        ("GET", "/api/v1/registry/vault/secrets/s1/exists"),
    ]:
        resp = await _call(real_app, method, path, _hdr("read"))
        _assert_refused(resp, rr.ROLE_OPERATOR, rr.ROLE_READ)


@pytest.mark.asyncio
async def test_reader_own_rows_refused_on_operator_routes_admitted_on_its_own(
    real_app: FastAPI, override_on,
) -> None:
    for (method, template), role in rr.ROUTE_ROLES.items():
        resp = await _call(
            real_app, method, _concrete(template), _hdr("reader-own-rows", "alice"),
        )
        if role == rr.ROLE_OPERATOR:
            _assert_refused(resp, rr.ROLE_OPERATOR, rr.ROLE_READER_OWN_ROWS)
        elif (method, template) in rr.PUBLIC_ROUTES:
            # X3-B3: a public route authenticates itself; with no cookie,
            # sign-out answers 200 (nothing to revoke), the others 422.
            # X4 GATE: the public READS (healthz, metrics, docs, openapi)
            # answer their handler's own status — healthz is 503 here, with
            # no substrate connected — never the gate's 401/403.
            allowed = (200, 422) if method == "POST" else (200, 503)
            assert resp.status_code in allowed, (
                method, template, resp.status_code, resp.text[:300],
            )
        else:
            # Admitted by the gate: the request reaches validation / the
            # handler (422 for the deliberately-invalid body, 400 for the
            # non-UUID watch id) — anything but the gate's 403.
            assert resp.status_code != 403, (method, template, resp.text[:300])
            assert resp.status_code in (400, 404, 422), (
                method, template, resp.status_code, resp.text[:300],
            )


@pytest.mark.asyncio
async def test_read_principal_refused_on_own_rows_routes_and_reads_pass(
    real_app: FastAPI, override_on,
) -> None:
    for (method, template), role in rr.ROUTE_ROLES.items():
        if role != rr.ROLE_READER_OWN_ROWS:
            continue
        resp = await _call(real_app, method, _concrete(template), _hdr("read"))
        _assert_refused(resp, rr.ROLE_READER_OWN_ROWS, rr.ROLE_READ)
    # A plain read is admitted for `read` (the spec route needs no substrate).
    resp = await _call(real_app, "GET", "/api/v1/registry/openapi.json", _hdr("read"))
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_no_override_means_operator_nothing_refused(
    real_app: FastAPI, monkeypatch,
) -> None:
    """Production posture today: no B3 users, the header is ignored without
    the flag → the caller is operator, and the gate refuses nothing."""
    monkeypatch.delenv(rr.ROLE_OVERRIDE_ENV, raising=False)
    monkeypatch.delenv("LEGBA_REGISTRY_API_TOKEN", raising=False)
    monkeypatch.setenv("LEGBA_DEV_MODE", "1")
    resp = await _call(
        real_app, "POST", "/api/v1/registry/vault/secrets", _hdr("read"),
    )
    assert resp.status_code != 403
    assert resp.status_code == 422  # reached body validation


def test_unknown_override_role_is_a_400(override_on) -> None:
    from starlette.requests import Request

    scope = {
        "type": "http", "method": "GET", "path": "/x",
        "headers": [(b"x-legba-role", b"admin")],
    }
    with pytest.raises(Exception) as exc:
        rr.resolve_principal(Request(scope))
    assert getattr(exc.value, "status_code", None) == 400


def test_request_state_seams_resolve() -> None:
    from starlette.requests import Request

    def _req() -> Request:
        return Request({"type": "http", "method": "GET", "path": "/x", "headers": []})

    r = _req()
    r.state.legba_role = "read"
    r.state.legba_subject = "carol"
    p = rr.resolve_principal(r)
    assert (p.role, p.subject, p.source) == ("read", "carol", "request_state")
    r = _req()
    r.state.principal_kind = "service"
    assert rr.resolve_principal(r).role == rr.ROLE_OPERATOR
    # X4 GATE: an unstamped caller is UNRESOLVED and the lowest class — never
    # the operator role.
    p = rr.resolve_principal(_req())
    assert (p.role, p.source, p.resolved) == (rr.ROLE_READ, rr.SOURCE_UNRESOLVED, False)


def test_override_is_ignored_outside_pytest() -> None:
    probe = textwrap.dedent(
        """
        import logging, os, sys
        logging.basicConfig(level=logging.ERROR, stream=sys.stdout)
        os.environ["LEGBA_ROLE_OVERRIDE_FOR_TESTS"] = "1"
        assert "pytest" not in sys.modules
        from starlette.requests import Request
        from legba.data.registry import route_roles as rr
        req = Request({"type": "http", "method": "POST", "path": "/api/v1/registry/stack",
                       "headers": [(b"x-legba-role", b"read")]})
        p = rr.resolve_principal(req)
        # The header was IGNORED: no test_override principal, just an
        # unstamped (unresolved) caller, which the X4 gate refuses as such.
        assert p.source == "unresolved", p
        try:
            rr.enforce_route_role(req)
        except Exception as exc:
            assert exc.detail["error"] == "role_unresolved", exc.detail
        else:
            raise AssertionError("an unstamped caller passed the gate")
        print("OK")
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, timeout=120,
        env={**os.environ, "PYTHONPATH": os.pathsep.join(
            p for p in (str(_SRC), os.environ.get("PYTHONPATH", "")) if p)},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "override_ignored" in result.stdout, result.stdout


# ---------------------------------------------------------------------------
# Own rows over the real driver (watchlist.created_by)
# ---------------------------------------------------------------------------

_LABEL = "x2b2-own-rows"


class _Reg:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pg = pool


class _RouteDeps:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self.descriptor_registry = _Reg(pool)


@pytest_asyncio.fixture
async def own_pool(migrated_pg: PostgresConfig):
    pool = await asyncpg.create_pool(migrated_pg.dsn, min_size=1, max_size=4)
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM watchlist WHERE label LIKE $1", _LABEL + "%")
    yield pool
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM watchlist WHERE label LIKE $1", _LABEL + "%")
    await pool.close()


@pytest_asyncio.fixture
async def own_client(own_pool, override_on):
    # The watchlist router behind the SAME app-level gate create_app installs,
    # and (X4 GATE) the same bearer middleware in front of it — dev mode here,
    # so a request with no override header is the dev-mode operator.
    from legba.data.registry.bearer_auth import BearerPrincipalMiddleware

    app = FastAPI(dependencies=[Depends(rr.enforce_route_role)])
    app.add_middleware(BearerPrincipalMiddleware)
    app.include_router(build_watchlist_router(_RouteDeps(own_pool)), prefix="/api/v1/v3")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        yield c


async def _insert_watch(pool: asyncpg.Pool, owner: str, suffix: str) -> str:
    async with pool.acquire() as conn:
        wid = await conn.fetchval(
            """
            INSERT INTO watchlist (kind, pattern, label, created_by)
            VALUES ('text', '{"query": "Strait of Hormuz"}'::jsonb, $1, $2)
            RETURNING id
            """,
            f"{_LABEL}-{suffix}", owner,
        )
    return str(wid)


@pytest.mark.asyncio
async def test_reader_own_rows_owns_its_watches_and_only_those(
    own_client, own_pool,
) -> None:
    alice = _hdr("reader-own-rows", "alice")
    bobs = await _insert_watch(own_pool, "bob", "bob")      # fixture id: bob's row
    alices = await _insert_watch(own_pool, "alice", "alice")  # fixture id: alice's row

    # Own row: update + soft delete are allowed.
    r = await own_client.put(
        f"/api/v1/v3/watchlist/{alices}", json={"label": f"{_LABEL}-alice-2"},
        headers=alice,
    )
    assert r.status_code == 200, r.text
    assert r.json()["label"] == f"{_LABEL}-alice-2"
    r = await own_client.delete(f"/api/v1/v3/watchlist/{alices}", headers=alice)
    assert r.status_code == 200, r.text
    assert r.json()["active"] is False

    # Another user's row: refused, and untouched.
    r = await own_client.put(
        f"/api/v1/v3/watchlist/{bobs}", json={"label": "hijack"}, headers=alice,
    )
    assert r.status_code == 403, r.text
    assert r.json()["detail"]["error"] == "not_row_owner"
    assert r.json()["detail"]["required_role"] == "operator"
    r = await own_client.delete(f"/api/v1/v3/watchlist/{bobs}", headers=alice)
    assert r.status_code == 403, r.text
    async with own_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT label, active, created_by FROM watchlist WHERE id = $1", UUID(bobs),
        )
    assert (row["label"], row["active"], row["created_by"]) == (
        f"{_LABEL}-bob", True, "bob",
    )

    # Unknown id: still a 404 (not a 403 that would confirm or deny nothing).
    r = await own_client.delete(
        "/api/v1/v3/watchlist/00000000-0000-0000-0000-00000000b2b2", headers=alice,
    )
    assert r.status_code == 404

    # Create: stamped with the caller's subject — read back off the row.
    r = await own_client.post(
        "/api/v1/v3/watchlist",
        json={"kind": "text", "pattern": {"query": "Bab el-Mandeb"},
              "label": f"{_LABEL}-new"},
        headers=alice,
    )
    assert r.status_code == 201, r.text
    async with own_pool.acquire() as conn:
        owner = await conn.fetchval(
            "SELECT created_by FROM watchlist WHERE id = $1", UUID(r.json()["id"]),
        )
    assert owner == "alice"
    # Naming another owner is refused, never silently rewritten.
    r = await own_client.post(
        "/api/v1/v3/watchlist",
        json={"kind": "text", "pattern": {"query": "Bab el-Mandeb"},
              "label": f"{_LABEL}-forged", "created_by": "bob"},
        headers=alice,
    )
    assert r.status_code == 403, r.text
    assert r.json()["detail"]["error"] == "not_row_owner"


@pytest.mark.asyncio
async def test_read_refused_and_operator_unchanged_on_the_watchlist(
    own_client, own_pool,
) -> None:
    bobs = await _insert_watch(own_pool, "bob", "bob-op")
    r = await own_client.put(
        f"/api/v1/v3/watchlist/{bobs}", json={"label": "x"}, headers=_hdr("read"),
    )
    assert r.status_code == 403
    assert r.json()["detail"]["required_role"] == "reader-own-rows"
    r = await own_client.get("/api/v1/v3/watchlist", headers=_hdr("read"))
    assert r.status_code == 200
    # The operator may change anyone's row — the pre-B2 behaviour.
    r = await own_client.put(
        f"/api/v1/v3/watchlist/{bobs}", json={"label": f"{_LABEL}-by-op"},
        headers=_hdr("operator"),
    )
    assert r.status_code == 200, r.text
    r = await own_client.post(
        "/api/v1/v3/watchlist",
        json={"kind": "text", "pattern": {"query": "Hormuz"},
              "label": f"{_LABEL}-op-for-bob", "created_by": "bob"},
    )
    assert r.status_code == 201, r.text
    assert r.json()["created_by"] == "bob"


# ---------------------------------------------------------------------------
# The generated doc + the slim-import guard
# ---------------------------------------------------------------------------


def test_api_roles_doc_is_the_rendering_of_the_table() -> None:
    doc = (_REPO / "docs" / "API_ROLES.md").read_text()
    assert doc == rr.render_api_roles_md(), (
        "docs/API_ROLES.md drifted from route_roles.ROUTE_ROLES — run "
        "python3 scripts/gen_api_roles_doc.py"
    )


def test_gate_and_split_leaves_import_registry_slim() -> None:
    probe = textwrap.dedent(
        """
        import sys
        for blocked in ("feedparser", "telethon", "warcio", "aiobotocore",
                        "pycountry", "networkx", "qdrant_client", "dspy"):
            sys.modules[blocked] = None
        import legba.data.registry.route_roles as rr
        import legba.data.registry.optimizer_review as orv
        import legba.data.registry.optimizer_review_api as ora
        leaked = sorted(m for m in sys.modules
                        if m.startswith("legba.data.analysts") or m.startswith("legba.runtime"))
        assert not leaked, leaked
        assert rr.enforce_route_role and orv.apply_optimizer_review
        assert ora.register_optimizer_review_routes
        print("OK")
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, timeout=120,
        env={**os.environ, "PYTHONPATH": os.pathsep.join(
            p for p in (str(_SRC), os.environ.get("PYTHONPATH", "")) if p)},
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_route_roles_is_a_leaf() -> None:
    """The gate module never imports a router module (the _deps.py rule)."""
    src = (_SRC / "legba" / "data" / "registry" / "route_roles.py").read_text()
    imports = re.findall(r"^\s*from\s+(\.[.\w]*)\s+import", src, flags=re.M)
    routers = [i for i in imports if i.lstrip(".") == "api" or i.endswith("_api")]
    assert not routers, routers
    absolute = re.findall(r"^\s*(?:from|import)\s+(legba[.\w]*)", src, flags=re.M)
    assert not absolute, absolute
    assert not re.findall(r"^\s*from\s+\.\.", src, flags=re.M)


def test_b1_principal_attribute_counts_as_operator() -> None:
    """B1's gate stamps ``request.state.principal`` (not ``principal_kind``);
    the role gate must read that spelling too (merge follow-up, X2)."""
    from types import SimpleNamespace
    from legba.data.registry import route_roles as rr

    assert "principal" in rr.STATE_PRINCIPAL_KIND_ATTRS
    state = SimpleNamespace(principal="service")
    req = SimpleNamespace(state=state, headers={}, scope={"type": "http"})
    resolved = rr.current_principal(req) if callable(getattr(rr, "current_principal", None)) else None
    if resolved is not None:
        assert resolved.role == "operator"
