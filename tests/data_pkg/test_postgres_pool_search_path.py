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

    2026-08-29 ROOT CAUSE / REWRITE NOTE: the original assertions here
    (``unqualified count == 0`` and ``qualified count == 0``) assumed a
    pristine, empty ``actor_state`` table — true only by accident of
    ``tests/data_pkg/``'s alphabetical default ordering (this file collects
    before ``test_runtime_telemetry_api.py``, whose ``_insert_actor_state``
    helper writes 3 rows across 3 call sites and never cleaned up). Under a
    pytest-randomly shuffle that runs the telemetry inserts first, this test
    saw ``assert 3 == 0`` and failed on a table it never claimed to own —
    the ACTUAL contract under test (per the docstring above and the
    "Sanity: unqualified table reference must resolve" comment this
    replaces) is reachability/consistency across acquires, not emptiness.
    ``test_runtime_telemetry_api.py`` now truncates ``actor_state`` at its
    own setup (see its ``_clean_actor_state`` autouse fixture), but this
    test no longer needs that guarantee to hold — it asserts something true
    regardless of how many rows are present.

    Restated as CONSISTENCY: on every acquire, the unqualified reference
    and the ``public.``-qualified reference must resolve to the SAME row
    count. This is STRICTLY AS DISCRIMINATING as the original for the
    historical bug shape (verified live, see
    planning/CAMPAIGN_2026-08-29/SHUFFLE_FIX_REPORT.md for the transcript):
    if search_path drifts on a later acquire while the table stays in
    `public` (the exact 2026-05-21 regression), the UNQUALIFIED read raises
    ``UndefinedTableError`` before any comparison runs — still a hard
    failure, not a silent pass. If instead ``ActorStateStore.SCHEMA`` ever
    regresses to an unqualified ``CREATE TABLE`` (so the table lands in
    ``ag_catalog`` instead, with search_path re-application otherwise
    intact), the QUALIFIED `public.actor_state` read is what raises — a
    class of regression the original ``== 0`` assertions covered no more
    directly than this does (both would have silently read whatever schema
    happened to be resolved). Order-proof either way: no assumption about
    who else has written to the table.
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
        for i in range(6):
            async with store.acquire() as conn:
                # Sanity: unqualified table reference must resolve, AND
                # must resolve to the exact same table `public.actor_state`
                # does — on EVERY acquire, whatever the row count actually
                # is (order-proof against any sibling test's writes to this
                # session-shared table).
                unqualified = await conn.fetchrow(
                    "SELECT count(*) AS n FROM actor_state"
                )
                qualified = await conn.fetchrow(
                    "SELECT count(*) AS n FROM public.actor_state"
                )
                assert unqualified["n"] == qualified["n"], (
                    f"acquire #{i}: unqualified count {unqualified['n']!r} != "
                    f"public.-qualified count {qualified['n']!r} — "
                    "search_path drifted (or actor_state landed outside "
                    "public) on this checkout"
                )
    finally:
        await store.close()
