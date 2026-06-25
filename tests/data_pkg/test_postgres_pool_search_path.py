# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Regression test for the 2026-05-21 ``actor_state`` schema-search-path bug.

The runtime's ``ActorStateStore`` runs ``CREATE TABLE IF NOT EXISTS
actor_state`` on the first connection acquired from the
``legba.data.postgres.PostgresStore`` pool. That pool registers an
``init`` callback that runs once per fresh connection — sets
``search_path = ag_catalog, "$user", public`` and registers the AGE
``agtype`` codec.

The bug: asyncpg internally issues ``RESET ALL`` (or equivalent) when a
connection is returned to the pool, which clears ``SET search_path``.
On the *first* checkout the path is what ``init`` set; on every
subsequent checkout the path falls back to the libpq default
(``"$user", public``). If ``CREATE TABLE actor_state`` happens to land
on a checkout where ``ag_catalog`` is first, the table is created in
``ag_catalog`` and later lookups (which see only ``public``) cannot
find it.

The fix adds a ``setup`` callback that re-applies ``search_path`` on
every acquire. This test pins that contract — without the callback,
the second+ acquire would observe ``"$user", public`` as the path.
"""

from __future__ import annotations

import pytest

from legba.data.config import PostgresConfig
from legba.data.postgres import PostgresStore


@pytest.mark.asyncio
async def test_search_path_persists_across_pool_reuse(migrated_pg: PostgresConfig):
    """Every checkout from the pool must see the configured search_path.

    Acquires 6 connections in succession (more than the pool's max_size
    so we exercise both fresh-init and reused-checkout paths) and
    asserts ``SHOW search_path`` returns the ag_catalog-first ordering
    every time.
    """
    store = PostgresStore(migrated_pg)
    await store.connect()
    try:
        observed: list[str] = []
        for _ in range(6):
            async with store.acquire() as conn:
                row = await conn.fetchrow("SHOW search_path")
                observed.append(row[0])

        # Every acquire must put ag_catalog first so unqualified Cypher
        # / ag_catalog references resolve correctly.
        for path in observed:
            assert path.startswith("ag_catalog"), (
                f"acquire observed search_path={path!r}; expected ag_catalog first"
            )
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_actor_state_table_reachable_from_every_acquire(
    migrated_pg: PostgresConfig,
):
    """Create actor_state on one acquire, read it from many others.

    Reproduces the runtime's failure mode: the table is created during
    bring-up on one checkout, then later (after the pool recycles the
    connection through other callers) reads happen on a different
    checkout. With the bug, reads fail with ``UndefinedTableError``.
    """
    store = PostgresStore(migrated_pg)
    await store.connect()
    try:
        # Bring up the schema on a fresh checkout.
        from legba.runtime.state import ActorStateStore

        actor_state = ActorStateStore(store.pool)
        await actor_state.ensure_schema()

        # Burn through several checkouts. With the bug the search_path
        # drifts and the unqualified `SELECT * FROM actor_state` lookup
        # raises UndefinedTableError on the second+ checkout.
        for _ in range(6):
            async with store.acquire() as conn:
                # Sanity: unqualified table reference must resolve.
                row = await conn.fetchrow(
                    "SELECT count(*) AS n FROM actor_state"
                )
                assert row["n"] == 0

                # And the qualified form must also work (catches the
                # case where the SCHEMA constant accidentally drops
                # the public.* prefix).
                row = await conn.fetchrow(
                    "SELECT count(*) AS n FROM public.actor_state"
                )
                assert row["n"] == 0
    finally:
        await store.close()
