# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``GET /v3/counts``: the World KPI strip's 24-hour counts.

The route counts what the list routes would list in the window: canonical
signals (``/signals``' own clause) by ``fetched_at`` and rows of kind
``finding`` (``/findings``' base clause) by ``produced_at`` — never an alias,
never a critique, never a row older than the window. Held against a real
migrated Postgres, plus the slim-image import probe every registry route
carries.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from tests.data_pkg.test_collection_slim_imports import _assert_ok, _probe


def test_the_counts_route_imports_in_the_slim_image() -> None:
    _assert_ok(_probe(
        """
        import legba.data.registry.server as server
        import legba.data.registry.window_counts_api as wc

        leaked = sorted(
            m for m in sys.modules
            if m.startswith("legba.data.analysts") or m.startswith("legba.runtime")
        )
        assert not leaked, "analyst/runtime reachable from the counts route: %r" % leaked
        assert wc.build_window_counts_router is not None
        print("OK")
        """
    ))


def test_the_counts_mirror_the_list_routes_filters() -> None:
    from legba.data.registry import window_counts_api as wc
    from legba.data.signal_slice_scope import CANONICAL_ONLY_SQL

    assert CANONICAL_ONLY_SQL in wc._SIGNALS_SQL and "fetched_at >= $1" in wc._SIGNALS_SQL
    assert "kind = 'finding'" in wc._FINDINGS_SQL and "produced_at >= $1" in wc._FINDINGS_SQL


@pytest.fixture
async def pool(migrated_pg):
    import asyncpg

    p = await asyncpg.create_pool(migrated_pg.dsn, min_size=1, max_size=2)
    yield p
    await p.close()


async def _signal(conn, source_id: str, *, fetched: datetime, canonical=None):
    u = uuid4()
    await conn.execute(
        "INSERT INTO signals (id, source_id, owner_tenant, modality, payload, content_hash, "
        "fetched_at, created_at, canonical_signal_id) "
        "VALUES ($1,$2,'default','text',$3::jsonb,$4,$5,$5,$6)",
        u, source_id, json.dumps({"title": "t"}), f"h_{uuid4().hex}", fetched, canonical)
    return u


async def _output(conn, analyst_id: str, kind: str, *, produced: datetime) -> None:
    await conn.execute(
        "INSERT INTO analyst_outputs (kind, title, body, confidence, data, analyst_id, "
        "schema_uri, produced_at) VALUES ($1, 't', '', 0.5, '{}'::jsonb, $2, 'legba/finding/1.0.0', $3)",
        kind, analyst_id, produced)


async def _get(app) -> dict:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        r = await client.get("/api/v1/v3/counts?window_hours=24")
    assert r.status_code == 200, r.text
    return r.json()


@pytest.mark.asyncio
async def test_the_route_counts_the_window_canonical_signals_and_findings(pool) -> None:
    from fastapi import FastAPI

    from legba.data.registry import window_counts_api as wc
    from legba.data.registry.api import require_bearer

    app = FastAPI()
    app.include_router(wc.build_window_counts_router(
        SimpleNamespace(descriptor_registry=SimpleNamespace(pg=pool))), prefix="/api/v1/v3")
    app.dependency_overrides[require_bearer] = lambda: "test"

    wc._CACHE.clear()
    before = await _get(app)
    assert before["window_hours"] == 24

    src = f"source.wctest_{uuid4().hex[:8]}"
    aid = f"wctest_{uuid4().hex[:8]}"
    now = datetime.now(timezone.utc)
    async with pool.acquire() as c:
        keep = await _signal(c, src, fetched=now - timedelta(hours=1))
        await _signal(c, src, fetched=now - timedelta(hours=2))
        await _signal(c, src, fetched=now - timedelta(hours=2), canonical=keep)   # alias: not counted
        await _signal(c, src, fetched=now - timedelta(hours=30))                  # outside the window
        await _output(c, aid, "finding", produced=now - timedelta(hours=3))
        await _output(c, aid, "finding", produced=now - timedelta(hours=40))      # outside
        await _output(c, aid, "critique", produced=now - timedelta(hours=1))      # not a finding

    cached = await _get(app)
    assert cached == before, "an answer is reused inside the TTL"
    wc._CACHE.clear()
    after = await _get(app)
    try:
        assert after["signals"] - before["signals"] == 2
        assert after["findings"] - before["findings"] == 1
    finally:
        async with pool.acquire() as c:
            await c.execute("DELETE FROM signals WHERE source_id = $1", src)
            await c.execute("DELETE FROM analyst_outputs WHERE analyst_id = $1", aid)
        wc._CACHE.clear()


@pytest.mark.asyncio
async def test_the_window_is_bounded() -> None:
    from fastapi import FastAPI

    from legba.data.registry import window_counts_api as wc
    from legba.data.registry.api import require_bearer

    app = FastAPI()
    app.include_router(wc.build_window_counts_router(SimpleNamespace()), prefix="/api/v1/v3")
    app.dependency_overrides[require_bearer] = lambda: "test"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        assert (await client.get("/api/v1/v3/counts?window_hours=0")).status_code == 422
        assert (await client.get("/api/v1/v3/counts?window_hours=169")).status_code == 422
