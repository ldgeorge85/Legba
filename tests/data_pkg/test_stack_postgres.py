# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Integration tests for the L-125 AGE-aware Postgres stack handler.

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

    `migrated_pg` has migration 0004 applied, so AGE is loaded + the
    `legba_graph` and the 9-vertex / 14-edge label set already exist.
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


@pytest.mark.integration
@pytest.mark.asyncio
async def test_on_configure_fails_without_age(tmp_path):
    """On configure, a database without AGE must raise.

    We exercise this by pointing at the admin Postgres `postgres` database
    on the substrate container — that database doesn't have AGE installed
    via our migrations.
    """
    cfg = PostgresConfig(
        host="127.0.0.1", port=5432,
        user="legba", password="legba",
        database="postgres",  # no AGE here
        pool_min=1, pool_max=2,
    )
    h = PostgresClusterHandler(cfg, component_id="pg.noage.test")
    with pytest.raises(RuntimeError, match=r"AGE"):
        await h.on_configure()
    # The handler should have torn down on failure — state stays at draft.
    assert h.state == "draft"


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
# Cypher against the AGE graph — vertex round-trip with a real entity_class
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_cypher_vertex_round_trip(pg_handler: PostgresClusterHandler):
    """Create a vertex with one of the 9 entity_classes, match it, delete it."""
    marker = f"legba_test_{uuid4().hex[:10]}_country"
    graph = pg_handler.graph_name

    # CREATE — note `:Country` is one of the 9 retained entity_classes
    # (L-090 §4.5). The test uses {name} substitution via params= so the
    # template path is exercised too.
    created = await pg_handler.cypher(
        graph,
        "CREATE (n:Country {{name: '{name}'}}) RETURN n",
        params={"name": marker},
    )
    assert len(created) == 1
    node = created[0]["v"]
    assert isinstance(node, dict)
    assert node["label"] == "Country"
    assert node["properties"]["name"] == marker

    # MATCH — the just-created vertex must be findable.
    matched = await pg_handler.cypher(
        graph,
        "MATCH (n:Country {{name: '{name}'}}) RETURN n",
        params={"name": marker},
    )
    assert len(matched) == 1
    assert matched[0]["v"]["properties"]["name"] == marker

    # DELETE — cleanup; subsequent match returns empty.
    await pg_handler.cypher(
        graph,
        "MATCH (n:Country {{name: '{name}'}}) DELETE n",
        params={"name": marker},
    )
    after = await pg_handler.cypher(
        graph,
        "MATCH (n:Country {{name: '{name}'}}) RETURN n",
        params={"name": marker},
    )
    assert after == []


@pytest.mark.integration
@pytest.mark.asyncio
async def test_cypher_uses_default_cols(pg_handler: PostgresClusterHandler):
    """Default `cols='v agtype'` matches `RETURN x AS v` shape."""
    rows = await pg_handler.cypher(
        pg_handler.graph_name,
        "RETURN 42 AS v",
    )
    assert rows == [{"v": 42}]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_cypher_param_template_error(pg_handler: PostgresClusterHandler):
    with pytest.raises(ValueError, match=r"params do not satisfy template"):
        await pg_handler.cypher(
            pg_handler.graph_name,
            "RETURN '{missing}' AS v",
            params={"present": "x"},
        )


# ---------------------------------------------------------------------------
# AGE vocabulary helpers — idempotent label creation
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_register_vertex_label_idempotent(
    pg_handler: PostgresClusterHandler,
):
    # All 9 entity_classes are pre-created by migration 0004 — registration
    # of `Country` should return False (already present).
    created = await pg_handler.register_vertex_label("Country")
    assert created is False

    # A fresh, test-only label that nothing has created — first call
    # creates, second call returns False (idempotent).
    fresh = f"TestLabel{uuid4().hex[:8]}"
    assert await pg_handler.register_vertex_label(fresh) is True
    assert await pg_handler.register_vertex_label(fresh) is False


@pytest.mark.integration
@pytest.mark.asyncio
async def test_register_edge_label_normalizes_legacy(
    pg_handler: PostgresClusterHandler,
):
    # Legacy `INVOLVED_IN` normalizes to `InvolvedIn` (pre-created by 0004).
    created = await pg_handler.register_edge_label("INVOLVED_IN")
    assert created is False  # `InvolvedIn` already present


@pytest.mark.integration
@pytest.mark.asyncio
async def test_register_label_validates_identifier(
    pg_handler: PostgresClusterHandler,
):
    for bad in ("Bad Label", "1Country", "x; DROP TABLE x", ""):
        with pytest.raises(ValueError):
            await pg_handler.register_vertex_label(bad)
        with pytest.raises(ValueError):
            await pg_handler.register_edge_label(bad)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_ensure_seed_vocabulary(pg_handler: PostgresClusterHandler):
    """Migration 0004 already created the 9 vertices + 14 edges, so the
    seed helper should report zero new creations."""
    created = await pg_handler.ensure_seed_vocabulary()
    # No new labels — migration 0004 baked them in.
    assert created == {"vertex": [], "edge": []}
