# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""R-2 — the operator review surface: ``/api/v1/v3/collection-requirements``.

List (status/desk filter, priority order), get-one, and the disposition-only
PATCH (status + reviewed_by + note) over rows the ``collection_gap`` analyst
wrote (migration 0113). Asserts the route can ONLY ever move the small
disposition sidecar — content columns (topic/rationale/evidence/candidate
sources) never change — and that there is no create/delete surface here (the
analyst is the sole content writer; this route never touches
``source_descriptors``).
"""
from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

import asyncpg
import httpx
import pytest_asyncio
from fastapi import FastAPI

from legba.data.config import PostgresConfig
from legba.data.registry.collection_requirements_api import (
    STATUSES,
    build_collection_requirements_router,
)

_ANALYST = "collection_gap_r2_api_test"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def pg_pool(migrated_pg: PostgresConfig):
    pool = await asyncpg.create_pool(migrated_pg.dsn, min_size=1, max_size=4)
    yield pool
    await pool.close()


@pytest_asyncio.fixture
async def clean_slate(pg_pool):
    async with pg_pool.acquire() as conn:
        await conn.execute("DELETE FROM collection_requirements")
    yield


class _Reg:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pg = pool


class _RouteDeps:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self.descriptor_registry = _Reg(pool)


@pytest_asyncio.fixture
async def client(pg_pool, monkeypatch):
    monkeypatch.delenv("LEGBA_REGISTRY_API_TOKEN", raising=False)
    monkeypatch.setenv("LEGBA_DEV_MODE", "1")
    app = FastAPI()
    app.include_router(
        build_collection_requirements_router(_RouteDeps(pg_pool)),
        prefix="/api/v1/v3",
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        yield c


async def _insert_requirement(
    conn: Any,
    *,
    natural_key: str,
    desk: str | None = "country_g20_ml",
    dimension: str | None = "economic_coercion",
    topic: str = "economic_coercion coverage for country_g20_ml",
    fillable: bool = True,
    unfillable_reason: str | None = None,
    priority_rank: int = 0,
    candidate_sources: list[dict] | None = None,
) -> str:
    rid = uuid4()
    await conn.execute(
        "INSERT INTO collection_requirements "
        "  (id, natural_key, origin, desk, dimension, topic, rationale, "
        "   evidence_kind, evidence_id, source_classes_wanted, "
        "   candidate_sources, fillable, unfillable_reason, priority_rank) "
        "VALUES ($1, $2, 'collection_gap', $3, $4, $5, 'starved cell', "
        "        'analyst_output', $6, $7::text[], $8::jsonb, $9, $10, $11)",
        rid,
        natural_key,
        desk,
        dimension,
        topic,
        uuid4(),
        ["official", "reporting"],
        json.dumps(candidate_sources or []),
        fillable,
        unfillable_reason,
        priority_rank,
    )
    return str(rid)


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


async def test_list_orders_by_priority_rank_then_recency(client, pg_pool, clean_slate):
    async with pg_pool.acquire() as conn:
        await _insert_requirement(conn, natural_key="k1", priority_rank=2)
        await _insert_requirement(conn, natural_key="k2", priority_rank=0)
        await _insert_requirement(conn, natural_key="k3", priority_rank=1)

    r = await client.get("/api/v1/v3/collection-requirements")
    assert r.status_code == 200, r.text
    keys = [row["natural_key"] for row in r.json()]
    assert keys == ["k2", "k3", "k1"]


async def test_list_filters_by_status_and_desk(client, pg_pool, clean_slate):
    async with pg_pool.acquire() as conn:
        await _insert_requirement(conn, natural_key="k1", desk="country_g20_ml")
        await _insert_requirement(conn, natural_key="k2", desk="country_g20_fr")
        rid3 = await _insert_requirement(conn, natural_key="k3", desk="country_g20_ml")
        await conn.execute(
            "UPDATE collection_requirements SET status = 'dismissed' WHERE id = $1",
            rid3,
        )

    r = await client.get("/api/v1/v3/collection-requirements", params={"desk": "country_g20_ml"})
    assert {row["natural_key"] for row in r.json()} == {"k1", "k3"}

    r = await client.get("/api/v1/v3/collection-requirements", params={"status": "proposed"})
    assert {row["natural_key"] for row in r.json()} == {"k1", "k2"}

    r = await client.get(
        "/api/v1/v3/collection-requirements", params={"status": "not-a-status"}
    )
    assert r.status_code == 422


async def test_list_carries_full_reviewable_shape(client, pg_pool, clean_slate):
    """The list read must carry WHAT/WHY/EVIDENCE + honesty fields — everything
    an operator needs without a second query."""
    async with pg_pool.acquire() as conn:
        await _insert_requirement(
            conn,
            natural_key="k1",
            fillable=False,
            unfillable_reason="no_known_feed",
            candidate_sources=[],
        )

    r = await client.get("/api/v1/v3/collection-requirements")
    row = r.json()[0]
    assert row["topic"]
    assert row["rationale"] == "starved cell"
    assert row["evidence_kind"] == "analyst_output"
    assert row["evidence_id"]
    assert row["fillable"] is False
    assert row["unfillable_reason"] == "no_known_feed"
    assert row["candidate_sources"] == []
    assert row["status"] == "proposed"
    assert row["reviewed_by"] is None


# ---------------------------------------------------------------------------
# Get one
# ---------------------------------------------------------------------------


async def test_get_one_not_found(client, pg_pool, clean_slate):
    r = await client.get(f"/api/v1/v3/collection-requirements/{uuid4()}")
    assert r.status_code == 404


async def test_get_one_bad_uuid(client, pg_pool, clean_slate):
    r = await client.get("/api/v1/v3/collection-requirements/not-a-uuid")
    assert r.status_code == 400


async def test_get_one_roundtrip(client, pg_pool, clean_slate):
    async with pg_pool.acquire() as conn:
        rid = await _insert_requirement(conn, natural_key="k1")
    r = await client.get(f"/api/v1/v3/collection-requirements/{rid}")
    assert r.status_code == 200
    assert r.json()["natural_key"] == "k1"


# ---------------------------------------------------------------------------
# Disposition PATCH — the ONLY write
# ---------------------------------------------------------------------------


async def test_patch_advances_status_and_stamps_reviewer(client, pg_pool, clean_slate):
    async with pg_pool.acquire() as conn:
        rid = await _insert_requirement(conn, natural_key="k1")

    r = await client.patch(
        f"/api/v1/v3/collection-requirements/{rid}",
        json={
            "status": "registered",
            "reviewed_by": "lewis",
            "disposition_note": "added source.example.feed and activated it",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "registered"
    assert body["reviewed_by"] == "lewis"
    assert body["disposition_note"] == "added source.example.feed and activated it"
    assert body["reviewed_at"] is not None
    # Content untouched.
    assert body["natural_key"] == "k1"
    assert body["topic"]


async def test_patch_defaults_reviewed_by_to_operator(client, pg_pool, clean_slate):
    async with pg_pool.acquire() as conn:
        rid = await _insert_requirement(conn, natural_key="k1")
    r = await client.patch(
        f"/api/v1/v3/collection-requirements/{rid}", json={"status": "dismissed"}
    )
    assert r.status_code == 200
    assert r.json()["reviewed_by"] == "operator"


async def test_patch_rejects_unknown_status(client, pg_pool, clean_slate):
    async with pg_pool.acquire() as conn:
        rid = await _insert_requirement(conn, natural_key="k1")
    r = await client.patch(
        f"/api/v1/v3/collection-requirements/{rid}",
        json={"status": "auto_registered_lol"},
    )
    assert r.status_code == 422


async def test_patch_not_found(client, pg_pool, clean_slate):
    r = await client.patch(
        f"/api/v1/v3/collection-requirements/{uuid4()}",
        json={"status": "dismissed"},
    )
    assert r.status_code == 404


async def test_no_create_or_delete_route(client, pg_pool, clean_slate):
    """The analyst is the sole content writer — this surface never mints or
    removes a requirement row, only dispositions an existing one."""
    r = await client.post(
        "/api/v1/v3/collection-requirements", json={"topic": "operator-authored"}
    )
    assert r.status_code in (404, 405)

    async with pg_pool.acquire() as conn:
        rid = await _insert_requirement(conn, natural_key="k1")
    r = await client.delete(f"/api/v1/v3/collection-requirements/{rid}")
    assert r.status_code in (404, 405)


def test_statuses_are_the_closed_vocabulary():
    assert set(STATUSES) == {"proposed", "reviewed", "registered", "dismissed"}
