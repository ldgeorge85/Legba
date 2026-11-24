# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""X2-B1 (W11-F16) — the fleet runs on a SERVICE token, the operator on their own.

Pins, in order:

  * THE GATE — either credential authenticates; the returned audit actor is
    ``service:bearer:<sha12>`` or ``operator:bearer:<sha12>`` (never the
    token); ``request.state.principal`` is stamped; both credentials are
    compared constant-time; neither set + no dev flag stays 503.
  * HUMAN-ONLY ROUTES — every entry of ``_deps.HUMAN_ONLY_ROUTES`` names a
    real route (the list cannot drift), the service token is refused 403 on
    each, the operator token is not.
  * THE FLEET WITH THE HUMAN TOKEN REVOKED — the registry app configured with
    the service token ONLY, the runtime env carrying the service token ONLY,
    and the runtime's real registry call paths (``RegistryHTTPClient``, the
    NLP factory's direct fallback, the source-first boot wiring) driven over
    ASGI: every call 2xx, every one carrying the service bearer.
  * NO SILENT FALLBACK — service token unset → the operator token, counted
    and logged ``principal_fallback``; the startup check logs ERROR.
  * A static guard — no fleet module reads the operator token directly.
  * ON THE REAL DRIVER — an audit row the walker writes reads ``service``; one
    written through the operator's curl reads ``operator``; one written with
    the service token reads ``service``.

Every token here is fake and generated per test.
"""
from __future__ import annotations

import ast
import hmac as hmac_mod
import importlib.util
import logging
import os
import sys
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from starlette.testclient import TestClient

from legba.data.registry import _deps
from legba.data.registry._deps import (
    API_TOKEN_ENV,
    DEV_MODE_ENV,
    HUMAN_ONLY_ROUTES,
    MISCONFIGURED_AUTH_DETAIL,
    SERVICE_TOKEN_ENV,
    _authorize_ws_token,
    human_only_reason,
    principal_actor,
    principal_label,
    require_bearer,
)
from legba.shared import registry_principal as rp

from .test_registry_api_unit import _build_app, _make_row, _sample_target_body

_REPO = Path(__file__).resolve().parents[2]
_BASE = "http://registry.test"


def _fake_token() -> str:
    return uuid4().hex + uuid4().hex


@pytest.fixture
def two_tokens(monkeypatch):
    op, svc = _fake_token(), _fake_token()
    monkeypatch.setenv(API_TOKEN_ENV, op)
    monkeypatch.setenv(SERVICE_TOKEN_ENV, svc)
    monkeypatch.delenv(DEV_MODE_ENV, raising=False)
    return op, svc


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


def test_each_credential_is_its_own_principal(two_tokens):
    op, svc = two_tokens
    a_op = require_bearer(authorization=f"Bearer {op}")
    a_svc = require_bearer(authorization=f"Bearer {svc}")
    assert a_op == principal_actor("operator", op) == f"operator:{principal_label(op)}"
    assert a_svc == principal_actor("service", svc) == f"service:{principal_label(svc)}"
    assert op not in a_op and svc not in a_svc
    # WS / SSE gate: same answer.
    assert _authorize_ws_token(None, f"Bearer {svc}") == a_svc
    assert _authorize_ws_token(op, None, surface="sse") == a_op
    with pytest.raises(HTTPException) as exc:
        require_bearer(authorization=f"Bearer {_fake_token()}")
    assert exc.value.status_code == 403


def test_both_credentials_compared_constant_time(two_tokens, monkeypatch):
    op, svc = two_tokens
    calls: list[tuple[bytes, bytes]] = []
    real = hmac_mod.compare_digest

    def spy(a, b):
        calls.append((bytes(a), bytes(b)))
        return real(a, b)

    monkeypatch.setattr(hmac_mod, "compare_digest", spy)
    require_bearer(authorization=f"Bearer {op}")
    # No early exit: the operator match still compares against the service token.
    assert calls == [(op.encode(), op.encode()), (op.encode(), svc.encode())]


def test_operator_token_revoked_the_service_token_alone_configures_the_gate(monkeypatch):
    svc = _fake_token()
    monkeypatch.delenv(API_TOKEN_ENV, raising=False)
    monkeypatch.setenv(SERVICE_TOKEN_ENV, svc)
    monkeypatch.delenv(DEV_MODE_ENV, raising=False)
    assert require_bearer(authorization=f"Bearer {svc}") == principal_actor("service", svc)
    with pytest.raises(HTTPException) as exc:
        require_bearer(authorization=None)
    assert exc.value.status_code == 401  # configured → enforced, not dev, not 503


def test_neither_token_and_no_dev_flag_is_still_fail_closed(monkeypatch):
    monkeypatch.delenv(API_TOKEN_ENV, raising=False)
    monkeypatch.delenv(SERVICE_TOKEN_ENV, raising=False)
    monkeypatch.delenv(DEV_MODE_ENV, raising=False)
    with pytest.raises(HTTPException) as exc:
        require_bearer(authorization="Bearer anything")
    assert exc.value.status_code == 503
    assert exc.value.detail == MISCONFIGURED_AUTH_DETAIL
    assert SERVICE_TOKEN_ENV in MISCONFIGURED_AUTH_DETAIL


def test_route_stamps_the_principal_on_request_state_and_the_actor(monkeypatch):
    op, svc = _fake_token(), _fake_token()
    monkeypatch.delenv(DEV_MODE_ENV, raising=False)
    app, deps = _build_app(token=op, service_token=svc)
    seen: list[tuple[str, str]] = []

    @app.middleware("http")
    async def _capture(request, call_next):
        resp = await call_next(request)
        seen.append((
            getattr(request.state, "principal", None),
            getattr(request.state, "principal_actor", None),
        ))
        return resp

    client = TestClient(app)
    for tok in (op, svc):
        r = client.post(
            "/api/v1/registry/descriptors/target",
            json=_sample_target_body(f"t_b1_{uuid4().hex[:6]}"),
            headers={"Authorization": f"Bearer {tok}"},
        )
        assert r.status_code == 201, r.text
    actors = [c[1][1] for c in deps.descriptor_registry.calls if c[0] == "register"]
    assert actors == [principal_actor("operator", op), principal_actor("service", svc)]
    assert seen == [
        ("operator", principal_actor("operator", op)),
        ("service", principal_actor("service", svc)),
    ]


# ---------------------------------------------------------------------------
# Human-only routes
# ---------------------------------------------------------------------------

#: One concrete request per HUMAN_ONLY_ROUTES entry (body irrelevant: the gate
#: refuses before validation).
_HUMAN_ONLY_REQUESTS = (
    ("POST", "/api/v1/registry/vault/secrets", {"secret_id": "x", "plaintext": "y"}),
    ("GET", "/api/v1/registry/vault/secrets/x/exists", None),
    ("DELETE", "/api/v1/registry/vault/secrets/x", None),
    ("POST", "/api/v1/registry/descriptors/target/t1/transition", {"to_state": "active"}),
    ("POST", "/api/v1/registry/descriptors/target/t1/retire", {}),
    ("POST", "/api/v1/registry/descriptors/target/t1/promote", {"candidate_version": "a" * 64}),
    ("POST", "/api/v1/registry/descriptors/target/t1/rollback", {"target_version": "a" * 64}),
)


def test_every_human_only_entry_names_a_real_route(monkeypatch):
    from legba.data.registry.api import build_router

    _app, deps = _build_app(token=_fake_token())
    real = {
        (m, "/api/v1/registry" + r.path)
        for r in build_router(deps).routes for m in getattr(r, "methods", None) or ()
    }
    # S-2: acknowledging / snoozing an alert (alert_ack_api, under /api/v1/v3).
    from legba.data.registry.alert_ack_api import build_alert_ack_router

    real |= {
        (m, "/api/v1/v3" + r.path)
        for r in build_alert_ack_router(deps).routes
        for m in getattr(r, "methods", None) or ()
    }
    for method, template, _why in HUMAN_ONLY_ROUTES:
        if template.endswith("/"):
            hits = [p for (m, p) in real if p.startswith(template)]
        else:
            hits = [p for (m, p) in real if p == template and (method == "*" or m == method)]
        assert hits, f"HUMAN_ONLY_ROUTES names {method} {template}, which is no route"
        # The matcher agrees on the template itself.
        assert all(human_only_reason(method if method != "*" else "GET", p) for p in hits)
    # Every concrete request below is covered; near-misses are not.
    for method, path, _ in _HUMAN_ONLY_REQUESTS:
        assert human_only_reason(method, path), (method, path)
    for method, path in (
        ("GET", "/api/v1/registry/descriptors/target/t1"),
        ("PUT", "/api/v1/registry/descriptors/target/t1"),
        ("POST", "/api/v1/registry/descriptors/target"),
        ("GET", "/api/v1/registry/descriptors/target/t1/retire"),
        ("POST", "/api/v1/registry/stack/llm.primary/retire"),
        ("GET", "/api/v1/registry/vaultish"),
    ):
        assert human_only_reason(method, path) is None, (method, path)


def test_service_token_refused_on_human_only_routes_operator_is_not(monkeypatch):
    op, svc = _fake_token(), _fake_token()
    monkeypatch.delenv(DEV_MODE_ENV, raising=False)
    app, _ = _build_app(token=op, service_token=svc)
    # The stubs do not implement every lifecycle verb: past the gate an
    # operator request may 4xx/5xx on the stub — only "not refused" matters.
    client = TestClient(app, raise_server_exceptions=False)
    for method, path, body in _HUMAN_ONLY_REQUESTS:
        r = client.request(method, path, json=body, headers={"Authorization": f"Bearer {svc}"})
        assert r.status_code == 403, (method, path, r.status_code, r.text)
        assert "service_principal_refused" in r.text, (method, path)
        r2 = client.request(method, path, json=body, headers={"Authorization": f"Bearer {op}"})
        assert "service_principal_refused" not in r2.text, (method, path)
        assert r2.status_code not in (401, 403), (method, path, r2.status_code, r2.text)


def test_service_token_refused_on_vault_store(monkeypatch):
    """The brief's named case, end to end: nothing reaches the vault."""
    op, svc = _fake_token(), _fake_token()
    monkeypatch.delenv(DEV_MODE_ENV, raising=False)
    app, deps = _build_app(token=op, service_token=svc)
    client = TestClient(app)
    r = client.post(
        "/api/v1/registry/vault/secrets",
        json={"secret_id": "b1_svc", "plaintext": "nope"},
        headers={"Authorization": f"Bearer {svc}"},
    )
    assert r.status_code == 403
    assert "b1_svc" not in deps.vault._db
    r = client.post(
        "/api/v1/registry/vault/secrets",
        json={"secret_id": "b1_op", "plaintext": "yes"},
        headers={"Authorization": f"Bearer {op}"},
    )
    assert r.status_code == 201, r.text
    assert "b1_op" in deps.vault._db


# ---------------------------------------------------------------------------
# The fleet with the HUMAN token revoked
# ---------------------------------------------------------------------------


class _Recording(httpx.AsyncBaseTransport):
    def __init__(self, inner: httpx.AsyncBaseTransport, log: list) -> None:
        self._inner = inner
        self._log = log

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        resp = await self._inner.handle_async_request(request)
        self._log.append((
            request.method, request.url.path, resp.status_code,
            request.headers.get("authorization"),
        ))
        return resp


@pytest.fixture
def fleet_on_service_only(monkeypatch):
    """Registry app + runtime env with the operator token REVOKED everywhere."""
    svc = _fake_token()
    monkeypatch.delenv(DEV_MODE_ENV, raising=False)
    app, deps = _build_app(token=None, service_token=svc)  # operator token unset
    monkeypatch.delenv(API_TOKEN_ENV, raising=False)
    monkeypatch.delenv("LEGBA_REGISTRY_TOKEN", raising=False)
    monkeypatch.setenv(SERVICE_TOKEN_ENV, svc)
    monkeypatch.setenv("LEGBA_REGISTRY_API_URL", _BASE)
    log: list[tuple[str, str, int, str | None]] = []
    transport = _Recording(httpx.ASGITransport(app=app), log)
    real_client = httpx.AsyncClient

    class _Routed(real_client):  # type: ignore[misc, valid-type]
        def __init__(self, *a: Any, **kw: Any) -> None:
            kw["transport"] = transport
            super().__init__(*a, **kw)

    monkeypatch.setattr(httpx, "AsyncClient", _Routed)
    rp.reset_principal_fallback_count()
    return svc, app, deps, log


async def test_fleet_paths_succeed_with_the_human_token_revoked(fleet_on_service_only):
    svc, _app, deps, log = fleet_on_service_only
    from legba.runtime.nlp_client_factory import _fetch_stack_component
    from legba.runtime.registry_client import RegistryHTTPClient
    from legba.runtime.source_first_runtime import _wire_targets_and_triggers

    # Make the source-first wiring walk deeper: active heads → typed fetches.
    async def _active_list(predicate):
        fam = predicate.family
        return [_make_row(descriptor_id=f"b1_{fam.value}", family=fam, state="active")]

    deps.descriptor_registry.list = _active_list  # type: ignore[method-assign]

    client = RegistryHTTPClient()  # env-resolved: the service token
    try:
        assert await client.get_descriptor("t1", family="target") is not None
        assert await client.get_descriptor_typed("a1", family="analyst") is not None
        assert await client.get_stack_component("llm.primary") is not None
        code, _ = await client.request_json("GET", "/api/v1/registry/descriptors",
                                            params={"family": "target"})
        assert code == 200
        # NLP factory — the direct-httpx fallback (a client with no getter).
        assert await _fetch_stack_component(object(), "nlp.primary") is not None  # type: ignore[arg-type]
        # Source-first boot wiring: lists targets + analysts, fetches typed heads.
        await _wire_targets_and_triggers(
            subscription_engine=None, trigger_engine=None,
            registry_client=client, policies={},
        )
        # The resync lister + the A2A typed fetch in dapr_host are closures over
        # the runtime's boot; they send THIS header to THESE two shapes.
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(f"{_BASE}/api/v1/registry/descriptors",
                            params={"family": "source", "head_only": "true", "limit": 500},
                            headers=rp.service_auth_headers())
            assert r.status_code == 200
            r = await c.get(f"{_BASE}/api/v1/registry/descriptors/analyst/a1/typed",
                            headers=rp.service_auth_headers())
            assert r.status_code == 200
    finally:
        await client.close()

    paths = {p for (_m, p, _s, _a) in log}
    assert "/api/v1/registry/stack/nlp.primary" in paths
    assert "/api/v1/registry/descriptors/target/b1_target/typed" in paths
    assert len(log) >= 8, log
    failed = [entry for entry in log if entry[2] >= 400]
    assert not failed, f"fleet calls refused with the human token revoked: {failed}"
    assert {a for (_m, _p, _s, a) in log} == {f"Bearer {svc}"}
    assert rp.principal_fallback_count() == 0


# ---------------------------------------------------------------------------
# No silent fallback
# ---------------------------------------------------------------------------


def test_unset_service_token_falls_back_loudly_and_counts(monkeypatch, caplog):
    op = _fake_token()
    monkeypatch.setenv(API_TOKEN_ENV, op)
    monkeypatch.delenv(SERVICE_TOKEN_ENV, raising=False)
    rp.reset_principal_fallback_count()
    caplog.set_level(logging.WARNING, logger=rp.__name__)
    assert rp.service_token() == op
    assert rp.service_auth_headers() == {"Authorization": f"Bearer {op}"}
    assert rp.principal_fallback_count() == 2
    assert any(rp.FALLBACK_EVENT in r.getMessage() for r in caplog.records)
    assert all(op not in r.getMessage() for r in caplog.records)

    caplog.clear()
    caplog.set_level(logging.INFO, logger=rp.__name__)
    assert rp.check_service_principal("runtime") == "principal_fallback"
    errs = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert errs and SERVICE_TOKEN_ENV in errs[0].getMessage()

    monkeypatch.setenv(SERVICE_TOKEN_ENV, op)
    assert rp.check_service_principal("runtime") == "indistinct"
    monkeypatch.setenv(SERVICE_TOKEN_ENV, _fake_token())
    assert rp.check_service_principal("runtime") == "service"
    monkeypatch.delenv(SERVICE_TOKEN_ENV)
    monkeypatch.delenv(API_TOKEN_ENV)
    assert rp.check_service_principal("runtime") == "unset"
    assert rp.service_token() is None and rp.service_auth_headers() == {}
    rp.reset_principal_fallback_count()


def _load_script(name: str):
    sys.path.insert(0, str(_REPO / "scripts"))
    try:
        spec = importlib.util.spec_from_file_location(name, _REPO / "scripts" / f"{name}.py")
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        sys.path.pop(0)


def test_bringup_scripts_resolve_the_service_token(monkeypatch, capsys):
    tok = _load_script("_token")
    monkeypatch.setattr(tok, "_env_file_value", lambda key: None)
    op, svc = _fake_token(), _fake_token()
    monkeypatch.delenv("LEGBA_REGISTRY_TOKEN", raising=False)
    monkeypatch.setenv(API_TOKEN_ENV, op)
    monkeypatch.setenv(SERVICE_TOKEN_ENV, svc)
    assert tok.resolve_token() == svc
    assert tok.resolve_token(principal="operator") == op
    monkeypatch.delenv(SERVICE_TOKEN_ENV)
    assert tok.resolve_token() == op
    assert "principal_fallback" in capsys.readouterr().err
    with pytest.raises(ValueError):
        tok.resolve_token(principal="root")


def test_the_vault_loaders_present_the_operator_token():
    for name in ("bringup_vault_load.py", "bringup_register_stack.py"):
        src = (_REPO / "scripts" / name).read_text()
        assert 'resolve_token(principal="operator")' in src, name


# ---------------------------------------------------------------------------
# Static guard — the fleet never reads the operator token itself
# ---------------------------------------------------------------------------

#: The only modules under src/legba that may name the operator token's env key.
_OPERATOR_TOKEN_READERS = {
    "shared/registry_principal.py": "THE resolver — service first, loud fallback",
    "data/registry/_deps.py": "the gate itself",
    "ui/mcp_builtin_tools.py": "the MCP surface acts for the operator's client (consult needs operator)",
}


def test_no_fleet_module_reads_the_operator_token():
    offenders: list[str] = []
    src_root = _REPO / "src" / "legba"
    for path in sorted(src_root.rglob("*.py")):
        rel = path.relative_to(src_root).as_posix()
        if rel in _OPERATOR_TOKEN_READERS:
            continue
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and node.value in (
                "LEGBA_REGISTRY_API_TOKEN", "LEGBA_REGISTRY_TOKEN",
            ):
                offenders.append(f"{rel}:{node.lineno}")
    assert not offenders, (
        "a module reads the OPERATOR registry token directly — fleet code must "
        "resolve its bearer through legba.shared.registry_principal "
        f"(service first): {offenders}"
    )


def test_env_example_documents_both_tokens():
    body = (_REPO / ".env.example").read_text()
    assert "\nLEGBA_REGISTRY_API_TOKEN=\n" in body
    assert "\nLEGBA_REGISTRY_SERVICE_TOKEN=\n" in body


# ---------------------------------------------------------------------------
# On the real driver — the audit rows say who
# ---------------------------------------------------------------------------


async def _audit_rows(pg, descriptor_id: str) -> list[dict]:
    async with pg.acquire() as conn:
        rows = await conn.fetch(
            "SELECT actor_id, actor_role, action FROM descriptor_audit_log "
            "WHERE descriptor_id = $1 ORDER BY occurred_at",
            descriptor_id,
        )
    return [dict(r) for r in rows]


@pytest.fixture
async def real_registry(migrated_pg):
    from nacl.signing import SigningKey

    from legba.data.postgres import PostgresStore
    from legba.data.registry.descriptor import DescriptorRegistry
    from legba.data.registry.signing import SigningIdentity
    from legba.data.schemas import ANALYST_KIND_REGISTRY

    snapshot = ANALYST_KIND_REGISTRY.extension_values()
    pg = PostgresStore(migrated_pg)
    await pg.connect()
    reg = DescriptorRegistry(pg, signing_identity=SigningIdentity(
        signing_key=SigningKey.generate(), signer_did="did:legba:registry:b1-test",
    ))
    await reg.start()
    try:
        yield pg, reg
    finally:
        await reg.stop()
        await pg.close()
        ANALYST_KIND_REGISTRY.replace_extensions(snapshot)


async def test_walker_audit_row_reads_service(real_registry, monkeypatch):
    pg, reg = real_registry
    saved = os.environ.get("LEGBA_DATA_PG_DB")
    sys.path.insert(0, str(_REPO / "scripts"))
    try:
        import _p17_registrar as walker
    finally:
        sys.path.pop(0)
        if saved is None:
            os.environ.pop("LEGBA_DATA_PG_DB", None)
        else:
            os.environ["LEGBA_DATA_PG_DB"] = saved
    from legba.data.registry.descriptor import Family
    from legba.data.schemas import TargetDescriptor

    did = f"t_b1_walker_{uuid4().hex[:8]}"
    model = TargetDescriptor.model_validate(_sample_target_body(did), strict=False)
    res = await walker.register_descriptor(pg, reg, family=Family.TARGET, descriptor=model)
    assert res.action == "registered", res
    rows = await _audit_rows(pg, did)
    assert rows, "the walker wrote no audit row"
    for r in rows:
        assert r["actor_id"].startswith("service:"), r
        assert r["actor_role"] == "service", r


async def test_curl_audit_rows_read_operator_and_service(real_registry, monkeypatch):
    pg, reg = real_registry
    from legba.data.registry._deps import RegistryAPIDeps
    from legba.data.registry.api import build_router

    from .test_registry_api_unit import (
        _StubDLQ,
        _StubStackRegistry,
        _StubVault,
        _StubVocabularyCache,
    )

    op, svc = _fake_token(), _fake_token()
    monkeypatch.setenv(API_TOKEN_ENV, op)
    monkeypatch.setenv(SERVICE_TOKEN_ENV, svc)
    monkeypatch.delenv(DEV_MODE_ENV, raising=False)
    deps = RegistryAPIDeps(
        descriptor_registry=reg,
        stack_registry=_StubStackRegistry(),  # type: ignore[arg-type]
        vault=_StubVault(),  # type: ignore[arg-type]
        dlq=_StubDLQ(),  # type: ignore[arg-type]
        audit_logger=reg._audit,
        vocabulary_cache=_StubVocabularyCache(),  # type: ignore[arg-type]
    )
    app = FastAPI()
    app.state.registry_deps = deps
    app.include_router(build_router(deps), prefix="/api/v1/registry")

    ids = {"operator": f"t_b1_op_{uuid4().hex[:8]}", "service": f"t_b1_svc_{uuid4().hex[:8]}"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=_BASE) as c:
        for who, tok in (("operator", op), ("service", svc)):
            r = await c.post(
                "/api/v1/registry/descriptors/target",
                json=_sample_target_body(ids[who]),
                headers={"Authorization": f"Bearer {tok}"},
            )
            assert r.status_code == 201, r.text
    op_rows = await _audit_rows(pg, ids["operator"])
    svc_rows = await _audit_rows(pg, ids["service"])
    assert [r["actor_id"] for r in op_rows] == [principal_actor("operator", op)]
    assert [r["actor_id"] for r in svc_rows] == [principal_actor("service", svc)]
    assert op not in op_rows[0]["actor_id"] and svc not in svc_rows[0]["actor_id"]


def test_deps_module_stays_a_leaf():
    """`_deps` may import the stdlib-only principal leaf — and never `api`."""
    src = Path(_deps.__file__).read_text()
    assert "from .api import" not in src and "import api" not in src
    leaf = Path(rp.__file__).read_text()
    tree = ast.parse(leaf)
    imported = {
        (n.module or "") if isinstance(n, ast.ImportFrom) else a.name
        for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))
        for a in (n.names if isinstance(n, ast.Import) else [None])
    }
    assert imported <= {"__future__", "logging", "os", "threading", "typing"}, imported
