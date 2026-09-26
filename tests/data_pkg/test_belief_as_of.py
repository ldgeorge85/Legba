# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Route-level tests for the V3/P3 temporal HTTP surface.

* ``GET /api/v1/v3/belief`` (:mod:`legba.data.registry.belief_api`) — the
  decision-time register: findings published and not yet superseded on
  ``as_of``, the verdict fold stamped, ``verdict_pending_at_as_of`` counted,
  and NO pooled score.
* ``GET /api/v1/situations?as_of=`` (:mod:`legba.data.registry.
  substrate_reads_api`) — the canonical validity-time read on the frame
  register: a situation closed today is visible at its historical date and
  absent from the current read.

Both go through the real binding path — the router factory mounted on a
FastAPI app with real deps carrying the migrated pg pool — never a mocked
handler call (the house v3-route test pattern, test_graph_triggers_api).

Auth: dev-mode (``LEGBA_DEV_MODE=1`` from tests/conftest.py), so
``require_bearer`` passes unauthenticated — the same bearer posture as the
rest of the v3 surface.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from legba.data.config import PostgresConfig
from legba.data.postgres import PostgresStore
from legba.data.registry.api import API_TOKEN_ENV
from legba.data.registry.belief_api import (
    BELIEF_READER_VERSION,
    build_belief_router,
)
from legba.data.registry.substrate_reads_api import (
    build_substrate_reads_router,
)
from legba.data.registry.situation_trajectory_api import (
    build_situation_trajectory_router,
)


# ---------------------------------------------------------------------------
# Fixtures — the real deps bundle + the real app
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def pg_store(migrated_pg: PostgresConfig):
    store = PostgresStore(migrated_pg)
    await store.connect()
    yield store
    await store.close()


@pytest_asyncio.fixture
async def belief_app(pg_store: PostgresStore):
    os.environ.pop(API_TOKEN_ENV, None)  # dev mode — any bearer accepted
    deps = SimpleNamespace(
        descriptor_registry=SimpleNamespace(pg=pg_store),
    )
    app = FastAPI()
    app.state.registry_deps = deps
    app.include_router(build_belief_router(deps), prefix="/api/v1/v3")
    yield app


@pytest_asyncio.fixture
async def client(belief_app):
    async with AsyncClient(
        transport=ASGITransport(app=belief_app), base_url="http://testserver",
    ) as c:
        yield c


@pytest_asyncio.fixture
async def reads_app(pg_store: PostgresStore):
    os.environ.pop(API_TOKEN_ENV, None)
    deps = SimpleNamespace(
        descriptor_registry=SimpleNamespace(pg=pg_store),
    )
    app = FastAPI()
    app.state.registry_deps = deps
    app.include_router(build_substrate_reads_router(deps), prefix="/api/v1")
    yield app


@pytest_asyncio.fixture
async def reads_client(reads_app):
    async with AsyncClient(
        transport=ASGITransport(app=reads_app), base_url="http://testserver",
    ) as c:
        yield c


@pytest_asyncio.fixture
async def v3_app(pg_store: PostgresStore):
    """The v3 situations surface — the trajectory module, which also mounts
    the /api/v1/v3/situations list the temporal acceptance proof curls."""
    os.environ.pop(API_TOKEN_ENV, None)
    deps = SimpleNamespace(
        descriptor_registry=SimpleNamespace(pg=pg_store),
    )
    app = FastAPI()
    app.state.registry_deps = deps
    app.include_router(
        build_situation_trajectory_router(deps), prefix="/api/v1/v3")
    yield app


@pytest_asyncio.fixture
async def v3_client(v3_app):
    async with AsyncClient(
        transport=ASGITransport(app=v3_app), base_url="http://testserver",
    ) as c:
        yield c


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


async def _finding(conn, *, title: str, confidence: float = 0.8,
                   produced_at: datetime,
                   superseded_at: datetime | None = None,
                   target_id: str | None = None):
    oid = uuid4()
    await conn.execute(
        """
        INSERT INTO analyst_outputs (
            id, kind, title, body, confidence, severity, data,
            target_id, produced_at, superseded_at,
            derived_from, schema_uri
        ) VALUES (
            $1, 'finding', $2, 'body', $3, 'medium', '{}'::jsonb,
            $4, $5, $6, '{}'::uuid[],
            'iglu:legba/finding/jsonschema/1-0-0'
        )
        """,
        oid, title, confidence, target_id, produced_at, superseded_at,
    )
    return oid


async def _critique(conn, *, analyzed_id, score: float, produced_at: datetime):
    cid = uuid4()
    await conn.execute(
        """
        INSERT INTO analyst_outputs (
            id, kind, title, body, confidence, data,
            produced_at, derived_from, schema_uri
        ) VALUES (
            $1, 'critique', 'Faithfulness verify', '', $4,
            $2::jsonb, $3, '{}'::uuid[],
            'iglu:legba/critique/jsonschema/1-0-0'
        )
        """,
        cid,
        json.dumps({
            "analyzed_output_id": str(analyzed_id),
            "overall_score": score,
        }),
        produced_at,
        score,
    )
    return cid


# ---------------------------------------------------------------------------
# /api/v1/v3/belief
# ---------------------------------------------------------------------------


def test_belief_route_registered_and_marker() -> None:
    router = build_belief_router(deps=object())  # type: ignore[arg-type]
    paths = {r.path for r in router.routes}  # type: ignore[attr-defined]
    assert "/belief" in paths
    assert BELIEF_READER_VERSION == "2026-09/p3"


@pytest.mark.asyncio
async def test_belief_as_of_returns_the_register(client, pg_store,
                                                 clean_tables):
    """fold='as_of': the graded finding carries its verdict; the ungraded one
    is pending — per-row, never pooled."""
    await clean_tables("analyst_outputs")
    t0 = datetime(2026, 8, 1, tzinfo=timezone.utc)
    async with pg_store.acquire() as conn:
        graded = await _finding(conn, title="graded", confidence=0.9,
                                produced_at=t0)
        pending = await _finding(conn, title="pending", confidence=0.7,
                                 produced_at=t0)
        await _critique(conn, analyzed_id=graded, score=0.6,
                        produced_at=datetime(2026, 8, 3, tzinfo=timezone.utc))
        # A finding superseded before D must not appear at all.
        await _finding(conn, title="superseded", produced_at=t0,
                       superseded_at=datetime(2026, 8, 4,
                                              tzinfo=timezone.utc))

    resp = await client.get(
        "/api/v1/v3/belief", params={"as_of": "2026-08-05"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["fold_verdicts"] == "as_of"
    by_title = {r["title"]: r for r in body["data"]}
    assert "superseded" not in by_title
    grow = by_title["graded"]
    assert grow["verdict_score"] == pytest.approx(0.6)
    assert grow["effective_confidence"] == pytest.approx(0.6)
    assert grow["verdict_pending_at_as_of"] is False
    prow = by_title["pending"]
    assert prow["verdict_score"] is None
    assert prow["effective_confidence"] is None
    assert prow["verdict_pending_at_as_of"] is True
    assert body["verdict_pending_at_as_of"] == 1
    assert "pooled" not in body


@pytest.mark.asyncio
async def test_belief_latest_fold_uses_todays_verdict(client, pg_store,
                                                    clean_tables):
    """fold='latest': a verdict landed AFTER D still scores the row — and
    pending_at_as_of still reports it was ungraded on D."""
    await clean_tables("analyst_outputs")
    t0 = datetime(2026, 8, 1, tzinfo=timezone.utc)
    async with pg_store.acquire() as conn:
        fid = await _finding(conn, title="late", confidence=0.9,
                             produced_at=t0)
        await _critique(conn, analyzed_id=fid, score=0.4,
                        produced_at=datetime(2026, 8, 10,
                                             tzinfo=timezone.utc))

    resp = await client.get(
        "/api/v1/v3/belief",
        params={"as_of": "2026-08-05", "fold_verdicts": "latest"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["fold_verdicts"] == "latest"
    row = body["data"][0]
    assert row["verdict_score"] == pytest.approx(0.4)
    assert row["effective_confidence"] == pytest.approx(0.4)
    assert row["verdict_pending_at_as_of"] is True


@pytest.mark.asyncio
async def test_belief_malformed_as_of_422s(client):
    """A malformed date refuses at the boundary — never a silent now()."""
    resp = await client.get(
        "/api/v1/v3/belief", params={"as_of": "last tuesday"})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_belief_requires_as_of(client):
    resp = await client.get("/api/v1/v3/belief")
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_belief_rejects_an_unknown_fold(client):
    resp = await client.get(
        "/api/v1/v3/belief",
        params={"as_of": "2026-08-05", "fold_verdicts": "best"})
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# /api/v1/situations?as_of=
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_situations_as_of_returns_a_frame_closed_today(
        reads_client, pg_store, clean_tables):
    """The acceptance shape: closed today, visible at its historical date,
    absent from the current read."""
    await clean_tables("situations")
    name = f"RouteSit_{uuid4().hex[:8]}"
    sid = uuid4()
    vf = datetime(2026, 8, 1, tzinfo=timezone.utc)
    vu = datetime(2026, 8, 10, tzinfo=timezone.utc)
    async with pg_store.acquire() as conn:
        await conn.execute(
            "INSERT INTO situations "
            "(id, data, name, status, category, intensity_score, "
            " situation_signature, valid_from, valid_until, analyst_id, "
            " produced_at) "
            "VALUES ($1, '{}'::jsonb, $2, 'closed', 'x', 1.7, $3, $4, $5, "
            "        'situation_clustering', $4)",
            sid, name, f"sig:{name}", vf, vu)

    resp = await reads_client.get(
        "/api/v1/situations", params={"as_of": "2026-08-05"})
    assert resp.status_code == 200, resp.text
    ids = {r["id"] for r in resp.json()["data"]}
    assert str(sid) in ids

    # After the close the same parameter read does not see it.
    resp2 = await reads_client.get(
        "/api/v1/situations", params={"as_of": "2026-08-20"})
    assert str(sid) not in {r["id"] for r in resp2.json()["data"]}


@pytest.mark.asyncio
async def test_situations_malformed_as_of_422s(reads_client):
    resp = await reads_client.get(
        "/api/v1/situations", params={"as_of": "back then"})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_v3_situations_alias_serves_the_same_page(
        v3_client, reads_client, pg_store, clean_tables):
    """The acceptance proof curls /api/v1/v3/situations — the v3 surface
    serves the identical page the v1 route does, through the same WHERE."""
    await clean_tables("situations")
    name = f"V3Sit_{uuid4().hex[:8]}"
    sid = uuid4()
    vf = datetime(2026, 8, 1, tzinfo=timezone.utc)
    vu = datetime(2026, 8, 10, tzinfo=timezone.utc)
    async with pg_store.acquire() as conn:
        await conn.execute(
            "INSERT INTO situations "
            "(id, data, name, status, category, intensity_score, "
            " situation_signature, valid_from, valid_until, analyst_id, "
            " produced_at) "
            "VALUES ($1, '{}'::jsonb, $2, 'closed', 'x', 1.7, $3, $4, $5, "
            "        'situation_clustering', $4)",
            sid, name, f"sig:{name}", vf, vu)

    v3 = await v3_client.get(
        "/api/v1/v3/situations", params={"as_of": "2026-08-05"})
    assert v3.status_code == 200, v3.text
    assert str(sid) in {r["id"] for r in v3.json()["data"]}

    # Same query, both prefixes — identical pages, one WHERE clause.
    v1 = await reads_client.get(
        "/api/v1/situations", params={"as_of": "2026-08-05"})
    assert v1.json() == v3.json()

    # A malformed date refuses on the v3 boundary too.
    bad = await v3_client.get(
        "/api/v1/v3/situations", params={"as_of": "back then"})
    assert bad.status_code == 422
