# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""W11-F68a — the A2A fetcher returns JSON-safe rows.

``dapr_host._fetch_latest_outputs`` used to hand the route raw asyncpg values
(``uuid.UUID``, ``datetime``); ``JSONResponse`` could not serialise them, so
``GET/POST /a2a/skills/{id}`` 500'd on any real row. The fetch now lives in
``a2a_skill.fetch_latest_analyst_outputs`` (the closure delegates) and a
real-shaped row goes through the route on the real driver, the reader display
policy running before signing.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from nacl.signing import SigningKey

from legba.data.outputs.a2a_skill import (
    A2ASkillRegistration,
    A2ASkillRegistry,
    TrustedKeyDirectory,
    build_envelope,
    fetch_latest_analyst_outputs,
    json_safe,
    register_a2a_skill_route,
)
from legba.data.postgres import PostgresStore
from legba.data.registry.signing import SigningIdentity

SENTINEL = "SENTINEL-F68A-ARCHIVED-PUBLISHER-TEXT-0042"
ARTICLE = ("The ministry statement was brief. " * 30) + SENTINEL + " tail."


def test_json_safe_reduces_driver_values() -> None:
    u = uuid.uuid4()
    when = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)
    out = json_safe({"id": u, "t": when, "n": [u, {"k": when}], "x": None})
    assert out == {
        "id": str(u), "t": when.isoformat(),
        "n": [str(u), {"k": when.isoformat()}], "x": None,
    }
    json.dumps(out)  # plain json, no default=


def test_dapr_host_closure_delegates_to_the_shared_fetcher() -> None:
    src = (Path(__file__).resolve().parents[2]
           / "src/legba/runtime/dapr_host.py").read_text(encoding="utf-8")
    i = src.index("async def _fetch_latest_outputs(")
    body = src[i:i + 1500]
    assert "fetch_latest_analyst_outputs(" in body
    assert "[dict(r) for r in rows]" not in body


@pytest.mark.asyncio
async def test_a2a_route_serves_a_real_row_get_and_post(migrated_pg) -> None:
    store = PostgresStore(migrated_pg)
    await store.connect()
    tag = uuid.uuid4().hex[:10]
    analyst_id = f"analyst.f68a.{tag}"
    out_id = uuid.uuid4()
    run_id = uuid.uuid4()
    citation = {"marker": "[1]", "title": "t", "snippet": "short",
                "source_text": ARTICLE, "source_truncated": False}
    try:
        async with store.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO analyst_outputs (
                    id, kind, title, body, confidence, severity, data,
                    target_id, analyst_id, analyst_version, produced_at,
                    derived_from, schema_uri, run_id
                ) VALUES ($1,'finding','F68a finding','Closed [1].',0.7,'low',
                          $2::jsonb,$3,$4,'ab12',$5,$6::uuid[],
                          'iglu:legba/finding/jsonschema/1-0-0',$7)
                """,
                out_id, json.dumps({"citations": [citation]}),
                f"target.f68a.{tag}", analyst_id,
                datetime.now(tz=timezone.utc), [uuid.uuid4()], run_id,
            )

        # The raw fetch is JSON-native with no default= hook.
        rows = await fetch_latest_analyst_outputs(
            store.pool, analyst_ids=[analyst_id], limit=5, target_filter=None,
        )
        assert [r["id"] for r in rows] == [str(out_id)]
        assert isinstance(rows[0]["produced_at"], str)
        json.dumps(rows)
        scoped = await fetch_latest_analyst_outputs(
            store.pool, analyst_ids=[analyst_id], limit=5,
            target_filter="target.nope",
        )
        assert scoped == []

        async def _fetch(*, analyst_ids, limit, target_filter):
            return await fetch_latest_analyst_outputs(
                store.pool, analyst_ids=analyst_ids, limit=limit,
                target_filter=target_filter,
            )

        key = SigningKey(b"a2a-f68a-test-seed-0000000000001")
        identity = SigningIdentity(signing_key=key, signer_did="did:legba:test:f68a")
        registry = A2ASkillRegistry()
        registry.register(A2ASkillRegistration(
            skill_id="intelligence.f68a", analyst_id=analyst_id,
            analyst_version="ab12", input_schema={"type": "object"},
            response_schema={"type": "object"}, auth_required=False,
        ))
        app = FastAPI()
        register_a2a_skill_route(
            app, registry=registry, identity=identity,
            fetch_latest_outputs=_fetch, trusted_keys=TrustedKeyDirectory(),
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://a2a.test",
        ) as c:
            g = await c.get("/a2a/skills/intelligence.f68a")
            assert g.status_code == 200, g.text[:300]
            env = build_envelope(
                skill_id="intelligence.f68a", payload={"limit": 3},
                signer_did="did:legba:test:caller",
            )
            p = await c.post("/a2a/skills/intelligence.f68a",
                             json={"envelope": env, "signature": "x"})
            assert p.status_code == 200, p.text[:300]
        for resp in (g, p):
            findings = resp.json()["envelope"]["payload"]["findings"]
            assert [f["id"] for f in findings] == [str(out_id)]
            # display policy still runs before signing: the archived text
            # past the cited span never reaches the reader.
            assert SENTINEL not in resp.text
    finally:
        async with store.pool.acquire() as conn:
            await conn.execute("DELETE FROM analyst_outputs WHERE id = $1", out_id)
        await store.close()
