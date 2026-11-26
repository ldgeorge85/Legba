# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Integration tests for the AGE-aware Postgres stack handler.

Real Postgres+AGE container. The session fixtures in
`tests/data_pkg/conftest.py` create a fresh per-session test database;
this module mounts a `PostgresClusterHandler` against it, then drives the
lifecycle hooks, multi-tenant schema flow, AGE label registration, and
Cypher round-trips through the handler's public surface.

No mocks. Real DDL, real Cypher.
"""

from __future__ import annotations

from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from legba.data.config import PostgresConfig
from legba.data.schemas import PostgresClusterConfig, Property
from legba.data.schemas.properties import Secret, Text
from legba.data.stack.postgres import PostgresClusterHandler, handler


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def pg_handler(migrated_pg: PostgresConfig):
    """Build a handler against the migrated session test DB and bring it up.

    `migrated_pg` has every migration applied; the AGE extension is still
    installed but the (empty) `legba_graph` was dropped.
    """
    h = PostgresClusterHandler(
        migrated_pg,
        component_id=f"pg.test.{uuid4().hex[:8]}",
    )
    await h.on_configure()
    await h.on_activate()
    try:
        yield h
    finally:
        await h.on_retire()


# ---------------------------------------------------------------------------
# Identity / registration
# ---------------------------------------------------------------------------


def test_handler_factory_returns_class():
    cls = handler()
    assert cls is PostgresClusterHandler
    assert cls.kind == "postgres"
    assert cls.family == "stack"
    assert cls.schema_version.startswith("legba/stack/postgres/")
    assert cls.handler_version


# ---------------------------------------------------------------------------
# Descriptor-shape parsing
# ---------------------------------------------------------------------------


def test_descriptor_config_requires_password_override(migrated_pg: PostgresConfig):
    """A PostgresClusterConfig holds a Secret reference, not cleartext —
    constructing the handler without `password_override` must fail loudly."""
    desc_cfg = PostgresClusterConfig(
        host=Text.of(migrated_pg.host),
        port=Property.Number.of(migrated_pg.port, minimum=1, maximum=65535),
        database=Text.of(migrated_pg.database),
        user=Text.of(migrated_pg.user),
        password=Secret.of("legba.pg.test.password"),
    )
    with pytest.raises(ValueError, match=r"Secret reference"):
        PostgresClusterHandler(desc_cfg)


def test_descriptor_config_with_override(migrated_pg: PostgresConfig):
    desc_cfg = PostgresClusterConfig(
        host=Text.of(migrated_pg.host),
        port=Property.Number.of(migrated_pg.port, minimum=1, maximum=65535),
        database=Text.of(migrated_pg.database),
        user=Text.of(migrated_pg.user),
        password=Secret.of("legba.pg.test.password"),
        pool_size=Property.Number.of(7, minimum=1, maximum=200),
    )
    h = PostgresClusterHandler(desc_cfg, password_override=migrated_pg.password)
    assert h.pg_config.host == migrated_pg.host
    assert h.pg_config.pool_max == 7
    assert h.pg_config.database == migrated_pg.database


# ---------------------------------------------------------------------------
# Lifecycle + healthcheck
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_lifecycle_and_health(migrated_pg: PostgresConfig):
    h = PostgresClusterHandler(migrated_pg, component_id="pg.lifecycle.test")
    assert h.state == "draft"

    await h.on_configure()
    assert h.state == "configured"

    await h.on_activate()
    assert h.state == "active"

    health = await h.health_check()
    assert health.state == "healthy"
    assert health.age_version, "expected AGE extversion populated"
    assert health.pool_size == migrated_pg.pool_max
    assert health.last_success_at is not None

    await h.on_pause()
    assert h.state == "paused"

    await h.on_resume()
    assert h.state == "active"

    await h.on_retire()
    assert h.state == "retired"
    # After retire, healthcheck reports unhealthy / not connected.
    h2_health = await h.health_check()
    assert h2_health.state == "unhealthy"


# (The old "configure refuses a database without the AGE graph" test was
# removed with the graph: PostgresStore.connect installs the extension itself,
# so the only thing that ever made configure refuse was the graph probe.)


# ---------------------------------------------------------------------------
# Query wrappers
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_query_wrappers(pg_handler: PostgresClusterHandler):
    val = await pg_handler.fetchval("SELECT 1")
    assert val == 1

    row = await pg_handler.fetchrow("SELECT $1::int AS x, $2::text AS y", 7, "ok")
    assert row["x"] == 7
    assert row["y"] == "ok"

    rows = await pg_handler.fetch(
        "SELECT generate_series(1, $1) AS n", 3
    )
    assert [r["n"] for r in rows] == [1, 2, 3]

    # execute returns the CommandComplete tag.
    tag = await pg_handler.execute("SELECT 1")
    assert tag.startswith("SELECT")


# ---------------------------------------------------------------------------
# Multi-tenant schema flow
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_create_and_drop_user_schema(pg_handler: PostgresClusterHandler):
    schema = f"legba_test_{uuid4().hex[:10]}_pg"
    assert not await pg_handler.schema_exists(schema)
    await pg_handler.create_user_schema(schema)
    assert await pg_handler.schema_exists(schema)
    # Idempotent.
    await pg_handler.create_user_schema(schema)
    assert await pg_handler.schema_exists(schema)
    # Drop + idempotent.
    await pg_handler.drop_user_schema(schema)
    assert not await pg_handler.schema_exists(schema)
    await pg_handler.drop_user_schema(schema)  # no-op


@pytest.mark.integration
@pytest.mark.asyncio
async def test_acquire_with_schema_switches_search_path(
    pg_handler: PostgresClusterHandler,
):
    schema = f"legba_test_{uuid4().hex[:10]}_pg"
    await pg_handler.create_user_schema(schema)
    try:
        # Create a table inside the user schema, then verify lookup via
        # search_path resolves to the tenant-scoped object.
        async with pg_handler.acquire(schema=schema) as conn:
            await conn.execute(
                'CREATE TABLE "t_tenant" (id int primary key, payload text)'
            )
            await conn.execute(
                'INSERT INTO "t_tenant" (id, payload) VALUES (1, $1)',
                "hello-tenant",
            )
            val = await conn.fetchval('SELECT payload FROM "t_tenant" WHERE id=1')
            assert val == "hello-tenant"

        # A new acquire WITHOUT specifying the schema should NOT see the
        # tenant table (search_path reset on release).
        async with pg_handler.acquire() as conn:
            val = await conn.fetchval(
                "SELECT to_regclass($1)::text", "t_tenant"
            )
            assert val is None, (
                "tenant table should not be visible under default search_path"
            )
            # But fully qualifying the name should still resolve.
            qualified = await conn.fetchval(
                f'SELECT payload FROM "{schema}"."t_tenant" WHERE id=1'
            )
            assert qualified == "hello-tenant"

        # set_schema on a held connection should also work.
        async with pg_handler.acquire() as conn:
            await pg_handler.set_schema(conn, schema)
            val = await conn.fetchval('SELECT payload FROM "t_tenant" WHERE id=1')
            assert val == "hello-tenant"
    finally:
        await pg_handler.drop_user_schema(schema, cascade=True)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_schema_identifier_validation(
    pg_handler: PostgresClusterHandler,
):
    for bad in ("a; DROP DATABASE legba", "", "1abc", "x" * 64, "with space"):
        with pytest.raises(ValueError):
            await pg_handler.create_user_schema(bad)


# ---------------------------------------------------------------------------
# The AGE graph is gone (2026-11-15): the handler still comes up without it,
# and no longer carries the cypher / label helpers.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_configures_and_is_healthy_without_the_age_graph(
    pg_handler: PostgresClusterHandler,
):
    health = await pg_handler.health_check()
    assert health.state == "healthy"
    assert health.age_version  # the extension is still installed
    assert "graph_name" not in health.detail


def test_the_cypher_and_label_helpers_are_gone():
    for name in ("cypher", "register_vertex_label", "register_edge_label", "ensure_seed_vocabulary"):
        assert not hasattr(PostgresClusterHandler, name), name
