# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Integration tests for migration 0086_entity_researcher_schema.sql.

The SCHEMA FOUNDATION for the `entity_researcher` (E2b + E5 of
planning/MASTER_PLAN_2026-07-10.md):

  * entity_alias / entity_judgement tables (write-time canonicalization surface
    + pairwise verdict cache) — constraints + UNIQUE-key dedupe hold.
  * entity_profiles.merged_into tombstone+redirect column.
  * resolve_entity(uuid) — a cycle-safe recursive redirect chaser: a 2-hop
    chain resolves to the terminal survivor, and a corrupt cycle terminates
    (no infinite loop) rather than hanging.

Runs against the freshly-migrated per-session test DB (see conftest.py's
`migrated_pg`, which globs + applies every `*.sql` migration — so 0086 is
already applied by the time these tests connect).
"""

from __future__ import annotations

from uuid import UUID

import asyncpg
import pytest

from legba.data.config import PostgresConfig


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


async def _insert_entity(conn: asyncpg.Connection, name: str) -> UUID:
    """Insert a minimal entity_profiles row, return its id (a UUID — matching
    what resolve_entity() and the FK columns return, so equality comparisons
    don't cross the str/UUID boundary)."""
    return await conn.fetchval(
        """
        INSERT INTO public.entity_profiles (data, canonical_name)
        VALUES ('{}'::jsonb, $1)
        RETURNING id
        """,
        name,
    )


# ---------------------------------------------------------------------------
# (a) the migration applies cleanly — the objects exist on the migrated DB
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_0086_objects_present(migrated_pg: PostgresConfig):
    """The two tables, the column, the function + view all exist post-migrate,
    and 0086 is recorded in the ledger."""
    conn = await asyncpg.connect(migrated_pg.dsn)
    try:
        # tables
        for tbl in ("entity_alias", "entity_judgement"):
            reg = await conn.fetchval("SELECT to_regclass($1)", f"public.{tbl}")
            assert reg is not None, f"missing table public.{tbl}"

        # tombstone column
        col = await conn.fetchval(
            """
            SELECT 1 FROM information_schema.columns
             WHERE table_schema='public' AND table_name='entity_profiles'
               AND column_name='merged_into'
            """
        )
        assert col == 1, "entity_profiles.merged_into column missing"

        # function
        fn = await conn.fetchval("SELECT to_regprocedure('public.resolve_entity(uuid)')")
        assert fn is not None, "resolve_entity(uuid) function missing"

        # convenience view
        view = await conn.fetchval("SELECT to_regclass('public.entity_profiles_resolved')")
        assert view is not None, "entity_profiles_resolved view missing"

        # ledger records the file
        applied = await conn.fetchval(
            "SELECT 1 FROM legba_data_migrations WHERE name = $1",
            "0086_entity_researcher_schema.sql",
        )
        assert applied == 1, "0086 not recorded in legba_data_migrations"
    finally:
        await conn.close()


# ---------------------------------------------------------------------------
# (b) resolve_entity — 2-hop redirect + cycle-safety
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_resolve_entity_two_hop(migrated_pg: PostgresConfig):
    """A -> B -> C (terminal) resolves every id in the chain to C; a
    non-tombstone id resolves to itself; an unknown id returns itself."""
    conn = await asyncpg.connect(migrated_pg.dsn)
    try:
        async with conn.transaction():
            a = await _insert_entity(conn, "hop-A")
            b = await _insert_entity(conn, "hop-B")
            c = await _insert_entity(conn, "hop-C")

            # A redirects to B, B redirects to C, C is terminal
            await conn.execute(
                "UPDATE public.entity_profiles SET merged_into=$1 WHERE id=$2", b, a
            )
            await conn.execute(
                "UPDATE public.entity_profiles SET merged_into=$1 WHERE id=$2", c, b
            )

            assert await conn.fetchval("SELECT public.resolve_entity($1)", a) == c
            assert await conn.fetchval("SELECT public.resolve_entity($1)", b) == c
            # terminal resolves to itself
            assert await conn.fetchval("SELECT public.resolve_entity($1)", c) == c

            # a brand-new (unknown) id resolves to itself, never NULL
            unknown = UUID("00000000-0000-0000-0000-0000000000ff")
            assert (
                await conn.fetchval("SELECT public.resolve_entity($1)", unknown)
                == unknown
            )

            # the convenience view agrees with the function for the chain rows
            resolved_a = await conn.fetchval(
                "SELECT resolved_id FROM public.entity_profiles_resolved WHERE id=$1", a
            )
            assert resolved_a == c

            raise asyncpg.PostgresError("_rollback_")  # keep the test DB pristine
    except asyncpg.PostgresError as exc:  # pragma: no cover - control flow
        if str(exc) != "_rollback_":
            raise
    finally:
        await conn.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_resolve_entity_cycle_safe(migrated_pg: PostgresConfig):
    """A corrupt A -> B -> A cycle must TERMINATE (bounded depth + CYCLE clause)
    and return a deterministic id, not hang."""
    conn = await asyncpg.connect(migrated_pg.dsn)
    try:
        async with conn.transaction():
            a = await _insert_entity(conn, "cyc-A")
            b = await _insert_entity(conn, "cyc-B")

            # deferred not needed — set both redirects, then close the loop
            await conn.execute(
                "UPDATE public.entity_profiles SET merged_into=$1 WHERE id=$2", b, a
            )
            await conn.execute(
                "UPDATE public.entity_profiles SET merged_into=$1 WHERE id=$2", a, b
            )

            # both calls must RETURN (any of the two ids is acceptable — the
            # contract is termination, not a designated survivor in a corrupt
            # cycle). A short statement_timeout proves it does not hang.
            await conn.execute("SET LOCAL statement_timeout = '5s'")
            res_a = await conn.fetchval("SELECT public.resolve_entity($1)", a)
            res_b = await conn.fetchval("SELECT public.resolve_entity($1)", b)
            assert res_a in (a, b)
            assert res_b in (a, b)

            raise asyncpg.PostgresError("_rollback_")
    except asyncpg.PostgresError as exc:  # pragma: no cover - control flow
        if str(exc) != "_rollback_":
            raise
    finally:
        await conn.close()


# ---------------------------------------------------------------------------
# (c) table constraints — CHECKs reject bad values; UNIQUE keys dedupe
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_entity_alias_constraints(migrated_pg: PostgresConfig):
    conn = await asyncpg.connect(migrated_pg.dsn)
    try:
        async with conn.transaction():
            ent = await _insert_entity(conn, "alias-owner")

            # a valid alias inserts
            await conn.execute(
                """
                INSERT INTO public.entity_alias (alias_norm, canonical_id, alias_kind, decided_by)
                VALUES ('snsc', $1, 'acronym', 'rule')
                """,
                ent,
            )

            # bad alias_kind rejected by CHECK
            with pytest.raises(asyncpg.CheckViolationError):
                async with conn.transaction():
                    await conn.execute(
                        """
                        INSERT INTO public.entity_alias (alias_norm, canonical_id, alias_kind)
                        VALUES ('x', $1, 'not_a_kind')
                        """,
                        ent,
                    )

            # bad decided_by rejected by CHECK
            with pytest.raises(asyncpg.CheckViolationError):
                async with conn.transaction():
                    await conn.execute(
                        """
                        INSERT INTO public.entity_alias (alias_norm, canonical_id, decided_by)
                        VALUES ('y', $1, 'guesswork')
                        """,
                        ent,
                    )

            # UNIQUE (alias_norm, canonical_id) dedupes a re-insert
            with pytest.raises(asyncpg.UniqueViolationError):
                async with conn.transaction():
                    await conn.execute(
                        """
                        INSERT INTO public.entity_alias (alias_norm, canonical_id, alias_kind)
                        VALUES ('snsc', $1, 'exact')
                        """,
                        ent,
                    )

            # ON DELETE CASCADE: deleting the entity removes its aliases
            await conn.execute("DELETE FROM public.entity_profiles WHERE id=$1", ent)
            remaining = await conn.fetchval(
                "SELECT count(*) FROM public.entity_alias WHERE canonical_id=$1", ent
            )
            assert remaining == 0

            raise asyncpg.PostgresError("_rollback_")
    except asyncpg.PostgresError as exc:  # pragma: no cover - control flow
        if str(exc) != "_rollback_":
            raise
    finally:
        await conn.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_entity_judgement_constraints(migrated_pg: PostgresConfig):
    conn = await asyncpg.connect(migrated_pg.dsn)
    try:
        async with conn.transaction():
            # a valid verdict inserts (nullable entity FKs left NULL)
            await conn.execute(
                """
                INSERT INTO public.entity_judgement (pair_key, verdict, decided_by)
                VALUES ('khamenei|khameneii', 'same', 'llm')
                """
            )

            # bad verdict rejected by CHECK
            with pytest.raises(asyncpg.CheckViolationError):
                async with conn.transaction():
                    await conn.execute(
                        """
                        INSERT INTO public.entity_judgement (pair_key, verdict)
                        VALUES ('a|b', 'maybe_same')
                        """
                    )

            # bad decided_by rejected by CHECK
            with pytest.raises(asyncpg.CheckViolationError):
                async with conn.transaction():
                    await conn.execute(
                        """
                        INSERT INTO public.entity_judgement (pair_key, verdict, decided_by)
                        VALUES ('c|d', 'unsure', 'vibes')
                        """
                    )

            # UNIQUE (pair_key) is the cache-dedupe invariant
            with pytest.raises(asyncpg.UniqueViolationError):
                async with conn.transaction():
                    await conn.execute(
                        """
                        INSERT INTO public.entity_judgement (pair_key, verdict)
                        VALUES ('khamenei|khameneii', 'not_same')
                        """
                    )

            raise asyncpg.PostgresError("_rollback_")
    except asyncpg.PostgresError as exc:  # pragma: no cover - control flow
        if str(exc) != "_rollback_":
            raise
    finally:
        await conn.close()
