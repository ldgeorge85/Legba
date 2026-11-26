# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The audit ``actor_role`` follows the authenticated principal.

Two sites hard-coded ``actor_role="operator"``: ``descriptor.py``'s method
defaults (so a service-token write read actor_role=operator beside
actor_id=service:…) and the optimizer-review route (formerly ``v3_api.py``,
split out by X2-B2). Each is proven on the real driver: the row is read back
from ``descriptor_audit_log`` after a request made with each credential.
"""

from __future__ import annotations

import json
from uuid import uuid4

import httpx
import pytest
from fastapi import APIRouter, FastAPI

from legba.data.registry._deps import (
    API_TOKEN_ENV,
    DEV_MODE_ENV,
    SERVICE_TOKEN_ENV,
    RegistryAPIDeps,
    principal_actor,
)
from legba.shared.registry_principal import role_for_actor

from .test_registry_api_unit import _sample_target_body

_BASE = "http://registry.test"


def _token() -> str:
    return uuid4().hex + uuid4().hex


def test_role_for_actor_reads_the_principal_label() -> None:
    assert role_for_actor(principal_actor("service", "tok")) == "service"
    assert role_for_actor(principal_actor("operator", "tok")) == "operator"
    # Legacy / unlabelled actors keep the role they always had.
    for legacy in ("bearer:abc123def456", "anonymous", "lewis", "", None,
                   "servicex:bearer:1", "service"):
        assert role_for_actor(legacy) == "operator", legacy


@pytest.fixture
async def real_deps(migrated_pg, monkeypatch):
    from nacl.signing import SigningKey

    from legba.data.postgres import PostgresStore
    from legba.data.registry.descriptor import DescriptorRegistry
    from legba.data.registry.signing import SigningIdentity
    from legba.data.schemas import ANALYST_KIND_REGISTRY

    from .test_registry_api_unit import (
        _StubDLQ,
        _StubStackRegistry,
        _StubVault,
        _StubVocabularyCache,
    )

    op, svc = _token(), _token()
    monkeypatch.setenv(API_TOKEN_ENV, op)
    monkeypatch.setenv(SERVICE_TOKEN_ENV, svc)
    monkeypatch.delenv(DEV_MODE_ENV, raising=False)
    snapshot = ANALYST_KIND_REGISTRY.extension_values()
    pg = PostgresStore(migrated_pg)
    await pg.connect()
    reg = DescriptorRegistry(pg, signing_identity=SigningIdentity(
        signing_key=SigningKey.generate(), signer_did="did:legba:registry:f62-test",
    ))
    await reg.start()
    deps = RegistryAPIDeps(
        descriptor_registry=reg,
        stack_registry=_StubStackRegistry(),  # type: ignore[arg-type]
        vault=_StubVault(),  # type: ignore[arg-type]
        dlq=_StubDLQ(),  # type: ignore[arg-type]
        audit_logger=reg._audit,
        vocabulary_cache=_StubVocabularyCache(),  # type: ignore[arg-type]
    )
    try:
        yield {"pg": pg, "reg": reg, "deps": deps, "op": op, "svc": svc}
    finally:
        await reg.stop()
        await pg.close()
        ANALYST_KIND_REGISTRY.replace_extensions(snapshot)


async def _audit(pg, descriptor_id: str, action: str | None = None) -> list[dict]:
    async with pg.pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT actor_id, actor_role, action FROM descriptor_audit_log "
            "WHERE descriptor_id = $1 ORDER BY occurred_at", descriptor_id,
        )
    return [dict(r) for r in rows if action is None or r["action"] == action]


async def test_descriptor_write_role_follows_the_credential(real_deps) -> None:
    """Site 1 — descriptor.py's default: POST /descriptors/target."""
    from legba.data.registry.api import build_router

    d = real_deps
    app = FastAPI()
    app.state.registry_deps = d["deps"]
    app.include_router(build_router(d["deps"]), prefix="/api/v1/registry")
    ids = {"operator": f"t_f62_op_{uuid4().hex[:8]}",
           "service": f"t_f62_svc_{uuid4().hex[:8]}"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=_BASE,
    ) as c:
        for who, tok in (("operator", d["op"]), ("service", d["svc"])):
            r = await c.post(
                "/api/v1/registry/descriptors/target",
                json=_sample_target_body(ids[who]),
                headers={"Authorization": f"Bearer {tok}"},
            )
            assert r.status_code == 201, r.text
    for who, tok in (("operator", d["op"]), ("service", d["svc"])):
        rows = await _audit(d["pg"], ids[who])
        assert rows
        for row in rows:
            assert row["actor_id"] == principal_actor(who, tok)
            assert row["actor_role"] == who, row


async def test_explicit_actor_role_still_wins(real_deps) -> None:
    """The walker (and anything else naming a role) is not re-derived."""
    from legba.data.registry.descriptor import Family
    from legba.data.schemas import TargetDescriptor

    d = real_deps
    did = f"t_f62_explicit_{uuid4().hex[:8]}"
    model = TargetDescriptor.model_validate(_sample_target_body(did), strict=False)
    await d["reg"].register(
        model, actor="operator:bearer:abcdef123456", actor_role="service",
    )
    assert (await _audit(d["pg"], did))[0]["actor_role"] == "service"
    assert Family.TARGET  # the family enum is the one the audit row names


async def test_optimizer_review_role_follows_the_credential(real_deps) -> None:
    """Site 2 — the optimizer-review route (was v3_api.py:826)."""
    from legba.data.registry.optimizer_review_api import (
        register_optimizer_review_routes,
    )

    d = real_deps
    router = APIRouter(prefix="/api/v1/v3")
    register_optimizer_review_routes(router, d["deps"])
    app = FastAPI()
    app.state.registry_deps = d["deps"]
    app.include_router(router)

    analyst = f"analyst.f62.{uuid4().hex[:8]}"
    cand = {who: uuid4() for who in ("operator", "service")}
    try:
        async with d["pg"].pool.acquire() as conn:
            for cid in cand.values():
                await conn.execute(
                    """
                    INSERT INTO analyst_outputs (id, kind, title, body, data,
                                                 analyst_id, schema_uri)
                    VALUES ($1, 'prompt_module_candidate', 'f62 candidate', '',
                            $2::jsonb, $3, 'iglu:legba/finding/jsonschema/1-0-0')
                    """,
                    cid,
                    json.dumps({"analyst_id": analyst,
                                "promotion_gate": "human_gated"}),
                    analyst,
                )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=_BASE,
        ) as c:
            for who, tok in (("operator", d["op"]), ("service", d["svc"])):
                r = await c.post(
                    f"/api/v1/v3/optimizer/candidates/{cand[who]}/review",
                    json={"action": "reject", "reviewer": "f62-reviewer",
                          "note": "f62"},
                    headers={"Authorization": f"Bearer {tok}"},
                )
                assert r.status_code == 200, r.text
        rows = await _audit(d["pg"], analyst, action="optimizer_reject")
        assert sorted(r["actor_role"] for r in rows) == ["operator", "service"], rows
    finally:
        async with d["pg"].pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM analyst_outputs WHERE id = ANY($1::uuid[])",
                list(cand.values()),
            )
