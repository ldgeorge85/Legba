# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Retention-policy config surface: ``/api/v1/v3/retention-policies``.

List, get-one, and the operator-tunable-only PATCH (ttl_days / keep_classes /
batch_size / enabled / description) over the seeded rows migration 0109
writes. Asserts the route can only ever move the operator-tunable columns —
``policy_name`` / ``table_name`` / ``env_fallback_var`` never change — and
that there is no create/delete surface here (every row is paired to a
specific Python retention adapter by name).
"""
from __future__ import annotations

from typing import Any

import asyncpg
import httpx
import pytest_asyncio
from fastapi import FastAPI

from legba.data.config import PostgresConfig
from legba.data.registry.retention_policies_api import (
    build_retention_policies_router,
)


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
        # Reset to the migration's own seed rather than deleting them — the
        # two rows are the migration's ON CONFLICT DO NOTHING seed, not
        # test-owned fixtures; restore their shipped defaults after any test
        # mutates them.
        await conn.execute(
            "UPDATE retention_policies SET ttl_days = 0, keep_classes = "
            "  CASE policy_name "
            "    WHEN 'signals_retention' THEN ARRAY['retain_always', 'evidence_hold']::text[] "
            "    ELSE '{}'::text[] "
            "  END, "
            "  batch_size = 5000, enabled = TRUE, "
            "  description = description "
            "WHERE policy_name IN ('signals_retention', 'analyst_traces_retention')"
        )
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
        build_retention_policies_router(_RouteDeps(pg_pool)),
        prefix="/api/v1/v3",
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        yield c


async def _policy_row(pg_pool, policy_name: str) -> dict[str, Any]:
    async with pg_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM retention_policies WHERE policy_name = $1", policy_name
        )
    assert row is not None
    return dict(row)


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


async def test_list_carries_the_seeded_rows(client, pg_pool, clean_slate):
    r = await client.get("/api/v1/v3/retention-policies")
    assert r.status_code == 200, r.text
    names = {row["policy_name"] for row in r.json()}
    assert {"signals_retention", "analyst_traces_retention"} <= names


async def test_list_is_alphabetical(client, pg_pool, clean_slate):
    r = await client.get("/api/v1/v3/retention-policies")
    names = [row["policy_name"] for row in r.json()]
    assert names == sorted(names)


async def test_list_seeded_rows_ship_disabled(client, pg_pool, clean_slate):
    """Every seeded policy ships ttl_days=0 (disables the sweep) — deleting
    substrate data stays an operator decision, per the 0109 migration."""
    r = await client.get("/api/v1/v3/retention-policies")
    by_name = {row["policy_name"]: row for row in r.json()}
    assert by_name["signals_retention"]["ttl_days"] == 0
    assert by_name["analyst_traces_retention"]["ttl_days"] == 0


# ---------------------------------------------------------------------------
# Get one
# ---------------------------------------------------------------------------


async def test_get_one_not_found(client, pg_pool, clean_slate):
    r = await client.get("/api/v1/v3/retention-policies/no_such_policy")
    assert r.status_code == 404


async def test_get_one_roundtrip(client, pg_pool, clean_slate):
    r = await client.get("/api/v1/v3/retention-policies/signals_retention")
    assert r.status_code == 200
    body = r.json()
    assert body["policy_name"] == "signals_retention"
    assert body["table_name"] == "signals"
    assert set(body["keep_classes"]) == {"retain_always", "evidence_hold"}
    assert body["env_fallback_var"] == "LEGBA_SIGNALS_RETENTION_TTL_DAYS"


# ---------------------------------------------------------------------------
# PATCH — operator-tunable fields only
# ---------------------------------------------------------------------------


async def test_patch_updates_ttl_days_and_enabled(client, pg_pool, clean_slate):
    r = await client.patch(
        "/api/v1/v3/retention-policies/analyst_traces_retention",
        json={"ttl_days": 90, "enabled": False},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ttl_days"] == 90
    assert body["enabled"] is False
    row = await _policy_row(pg_pool, "analyst_traces_retention")
    assert row["ttl_days"] == 90
    assert row["enabled"] is False


async def test_patch_is_partial_unset_fields_keep_current_value(client, pg_pool, clean_slate):
    before = await _policy_row(pg_pool, "signals_retention")
    r = await client.patch(
        "/api/v1/v3/retention-policies/signals_retention",
        json={"batch_size": 1234},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["batch_size"] == 1234
    # Untouched fields keep their prior value.
    assert body["ttl_days"] == before["ttl_days"]
    assert set(body["keep_classes"]) == set(before["keep_classes"])
    assert body["enabled"] == before["enabled"]


async def test_patch_updates_keep_classes_and_description(client, pg_pool, clean_slate):
    r = await client.patch(
        "/api/v1/v3/retention-policies/analyst_traces_retention",
        json={"keep_classes": ["retain_always"], "description": "widened for an incident review"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["keep_classes"] == ["retain_always"]
    assert body["description"] == "widened for an incident review"


async def test_patch_never_touches_identity_or_wiring_columns(client, pg_pool, clean_slate):
    """policy_name / table_name / env_fallback_var are not on the wire model
    at all — extra fields in the body are ignored by pydantic, not applied."""
    before = await _policy_row(pg_pool, "signals_retention")
    r = await client.patch(
        "/api/v1/v3/retention-policies/signals_retention",
        json={
            "policy_name": "renamed_policy",
            "table_name": "some_other_table",
            "env_fallback_var": "LEGBA_HACKED",
            "ttl_days": 30,
        },
    )
    assert r.status_code == 200, r.text
    row = await _policy_row(pg_pool, "signals_retention")
    assert row["policy_name"] == before["policy_name"]
    assert row["table_name"] == before["table_name"]
    assert row["env_fallback_var"] == before["env_fallback_var"]
    assert row["ttl_days"] == 30


async def test_patch_rejects_negative_ttl_days(client, pg_pool, clean_slate):
    r = await client.patch(
        "/api/v1/v3/retention-policies/signals_retention",
        json={"ttl_days": -1},
    )
    assert r.status_code == 422


async def test_patch_rejects_non_positive_batch_size(client, pg_pool, clean_slate):
    r = await client.patch(
        "/api/v1/v3/retention-policies/signals_retention",
        json={"batch_size": 0},
    )
    assert r.status_code == 422


async def test_patch_rejects_empty_keep_class_entry(client, pg_pool, clean_slate):
    r = await client.patch(
        "/api/v1/v3/retention-policies/signals_retention",
        json={"keep_classes": [""]},
    )
    assert r.status_code == 422


async def test_patch_not_found(client, pg_pool, clean_slate):
    r = await client.patch(
        "/api/v1/v3/retention-policies/no_such_policy",
        json={"ttl_days": 30},
    )
    assert r.status_code == 404


async def test_no_create_or_delete_route(client, pg_pool, clean_slate):
    """No row here is ever operator-created or operator-deleted through the
    API — every row is paired 1:1 with a Python retention adapter."""
    r = await client.post(
        "/api/v1/v3/retention-policies", json={"policy_name": "operator_authored"}
    )
    assert r.status_code in (404, 405)

    r = await client.delete("/api/v1/v3/retention-policies/signals_retention")
    assert r.status_code in (404, 405)
