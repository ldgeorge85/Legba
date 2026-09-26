# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Program 6 L0 migration 0214 — the ``source_layers`` / ``desk_apertures``
SCHEMA contract.

Asserts the invariants the migration exists to make representable/
unrepresentable, not the column list:

  * the migration lands in the ledger;
  * `layer` is the closed six-value vocabulary on BOTH tables, pinned equal to
    `legba.data.layers._vocab.LAYER_VOCAB`;
  * `declared` is the closed three-value vocabulary on `desk_apertures`
    (the `desk_apertures_declared_vocab` marker constraint), pinned equal to
    `APERTURE_DECLARED_VOCAB`;
  * `declared='absent'` with a blank reason is refused AT THE TABLE
    (`desk_apertures_absent_needs_reason`), not just by the pydantic model;
  * a blank `reason` on a `source_layers` row is refused
    (`source_layers_reason_nonblank`) — curation is the layer's bias;
  * `country` must be a two-letter uppercase code (`source_layers_country_iso2`);
  * at most one OPEN row per (source_id, country) on `source_layers` and per
    (target_id, layer) on `desk_apertures` — closed history is unconstrained,
    so a re-versioned assignment may recur.
"""

from __future__ import annotations

from typing import Any

import asyncpg
import pytest
import pytest_asyncio

from legba.data.config import PostgresConfig
from legba.data.layers._vocab import APERTURE_DECLARED_VOCAB, LAYER_VOCAB

MIGRATION_NAME = "0214_source_layers.sql"


@pytest_asyncio.fixture
async def pg_pool(migrated_pg: PostgresConfig):
    pool = await asyncpg.create_pool(migrated_pg.dsn, min_size=1, max_size=4)
    yield pool
    await pool.close()


async def _insert_layer(conn: Any, **kw: Any) -> None:
    defaults = dict(
        source_id="source.zztest.default", country="ZZ", layer="official",
        reason="test", map_version="v1",
    )
    defaults.update(kw)
    await conn.execute(
        """INSERT INTO source_layers (source_id, country, layer, reason, map_version)
           VALUES ($1, $2, $3, $4, $5)""",
        defaults["source_id"], defaults["country"], defaults["layer"],
        defaults["reason"], defaults["map_version"],
    )


async def _insert_aperture(conn: Any, **kw: Any) -> None:
    defaults = dict(
        target_id="country_watch_zz", layer="official", declared="unmeasured",
        reason="", map_version="v1",
    )
    defaults.update(kw)
    await conn.execute(
        """INSERT INTO desk_apertures (target_id, layer, declared, reason, map_version)
           VALUES ($1, $2, $3, $4, $5)""",
        defaults["target_id"], defaults["layer"], defaults["declared"],
        defaults["reason"], defaults["map_version"],
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_migration_0214_applies_on_a_fresh_substrate(pg_pool):
    async with pg_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT name, sha256 FROM legba_data_migrations WHERE name = $1",
            MIGRATION_NAME)
    assert row is not None, (
        f"{MIGRATION_NAME} is not in the ledger — the runner globs *.sql and "
        "applies in sorted filename order, so a misnamed file is skipped "
        "silently rather than failing")
    assert len(row["sha256"]) == 64


@pytest.mark.integration
@pytest.mark.asyncio
async def test_source_layers_layer_is_the_closed_vocab(pg_pool):
    async with pg_pool.acquire() as conn:
        for layer in LAYER_VOCAB:
            await _insert_layer(
                conn, source_id=f"source.zzvocab.{layer}", layer=layer)
        with pytest.raises(asyncpg.CheckViolationError):
            await _insert_layer(
                conn, source_id="source.zzvocab.bogus", layer="opinion")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_desk_apertures_layer_is_the_closed_vocab(pg_pool):
    async with pg_pool.acquire() as conn:
        for layer in LAYER_VOCAB:
            await _insert_aperture(
                conn, target_id=f"country_watch_zzv{layer[:3]}", layer=layer)
        with pytest.raises(asyncpg.CheckViolationError):
            await _insert_aperture(
                conn, target_id="country_watch_zzbogus", layer="opinion")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_desk_apertures_declared_is_the_closed_vocab(pg_pool):
    async with pg_pool.acquire() as conn:
        for i, declared in enumerate(APERTURE_DECLARED_VOCAB):
            reason = "reasoned absence" if declared == "absent" else ""
            await _insert_aperture(
                conn, target_id=f"country_watch_zzd{i}", declared=declared,
                reason=reason)
        with pytest.raises(asyncpg.CheckViolationError):
            await _insert_aperture(
                conn, target_id="country_watch_zzdbogus", declared="mostly")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_desk_apertures_absent_requires_a_reason(pg_pool):
    async with pg_pool.acquire() as conn:
        with pytest.raises(asyncpg.CheckViolationError):
            await _insert_aperture(
                conn, target_id="country_watch_zzabsent", declared="absent",
                reason="")
        with pytest.raises(asyncpg.CheckViolationError):
            await _insert_aperture(
                conn, target_id="country_watch_zzabsent2", declared="absent",
                reason="   ")
        # a reasoned absence is fine
        await _insert_aperture(
            conn, target_id="country_watch_zzabsent3", declared="absent",
            reason="no domestic press licensed to operate")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_source_layers_reason_must_be_nonblank(pg_pool):
    async with pg_pool.acquire() as conn:
        with pytest.raises(asyncpg.CheckViolationError):
            await _insert_layer(conn, source_id="source.zzblank.a", reason="")
        with pytest.raises(asyncpg.CheckViolationError):
            await _insert_layer(conn, source_id="source.zzblank.b", reason="  ")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_source_layers_country_must_be_iso2(pg_pool):
    async with pg_pool.acquire() as conn:
        with pytest.raises(asyncpg.CheckViolationError):
            await _insert_layer(conn, source_id="source.zziso.a", country="zzz")
        with pytest.raises(asyncpg.CheckViolationError):
            await _insert_layer(conn, source_id="source.zziso.b", country="z")
        await _insert_layer(conn, source_id="source.zziso.c", country="ZZ")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_one_open_source_layer_per_source_and_country(pg_pool):
    async with pg_pool.acquire() as conn:
        await _insert_layer(
            conn, source_id="source.zzopen.a", country="ZZ", layer="official")
        with pytest.raises(asyncpg.UniqueViolationError):
            await _insert_layer(
                conn, source_id="source.zzopen.a", country="ZZ",
                layer="domestic_press")
        # a different country is a different key
        await _insert_layer(
            conn, source_id="source.zzopen.a", country="YY", layer="official")
        # closing the first frees the key: it may recur under a new version
        await conn.execute(
            "UPDATE source_layers SET valid_until = now() "
            "WHERE source_id = 'source.zzopen.a' AND country = 'ZZ'")
        await _insert_layer(
            conn, source_id="source.zzopen.a", country="ZZ",
            layer="domestic_press", map_version="v2")
        assert await conn.fetchval(
            "SELECT count(*) FROM source_layers WHERE source_id = 'source.zzopen.a'"
        ) == 3


@pytest.mark.integration
@pytest.mark.asyncio
async def test_one_open_aperture_per_target_and_layer(pg_pool):
    async with pg_pool.acquire() as conn:
        await _insert_aperture(
            conn, target_id="country_watch_zzoa", layer="official")
        with pytest.raises(asyncpg.UniqueViolationError):
            await _insert_aperture(
                conn, target_id="country_watch_zzoa", layer="official")
        # a different layer is a different key
        await _insert_aperture(
            conn, target_id="country_watch_zzoa", layer="physical")
        # closing frees the key
        await conn.execute(
            "UPDATE desk_apertures SET valid_until = now() "
            "WHERE target_id = 'country_watch_zzoa' AND layer = 'official'")
        await _insert_aperture(
            conn, target_id="country_watch_zzoa", layer="official",
            declared="present", map_version="v2")
        assert await conn.fetchval(
            "SELECT count(*) FROM desk_apertures WHERE target_id = 'country_watch_zzoa'"
        ) == 3
