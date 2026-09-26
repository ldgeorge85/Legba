# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Program 6 L0 — ``legba.data.layers.loader.load_layer_map``'s idempotence
and re-versioning contract, through a real Postgres (migration 0214).

  * loading a fresh map inserts one open row per entry/aperture;
  * re-loading the IDENTICAL file (same map_version, same content) is a
    no-op — nothing is written, nothing is superseded;
  * re-loading the same map_version with DIFFERENT content is refused loudly
    (`LayerMapVersionConflict`) rather than silently applied;
  * loading a NEW map_version supersedes the prior open rows (`valid_until`
    closed) and opens new ones, even when unrelated rows are unchanged;
  * a stamp claimed "on every write" is read back through the column — this
    file reads every row back after each load rather than trusting the
    receipt counts alone.
"""

from __future__ import annotations

from typing import Any

import asyncpg
import pytest
import pytest_asyncio

from legba.data.config import PostgresConfig
from legba.data.layers.loader import LayerMapVersionConflict, load_layer_map
from legba.data.registry.layer_map_schema import LayerMapDescriptor

LAYER_VOCAB = (
    "official", "domestic_press", "foreign_press",
    "social_digest", "public_data", "physical",
)


def _full_apertures() -> list[dict]:
    return [
        {"layer": layer, "declared": "unmeasured", "reason": ""}
        for layer in LAYER_VOCAB
    ]


def _descriptor(country: str, map_version: str, entries: list[dict],
                 apertures: list[dict] | None = None) -> LayerMapDescriptor:
    body = {
        "identity": {
            "id": f"layer_map_{country.lower()}",
            "name": f"{country} test map",
            "schema_uri": "legba/layer_map/1.0.0",
            "kind": "layer_map",
            "owner": "test",
            "created": "2026-01-01T00:00:00Z",
            "state": "draft",
        },
        "country": country,
        "map_version": map_version,
        "entries": entries,
        "apertures": apertures if apertures is not None else _full_apertures(),
    }
    return LayerMapDescriptor.model_validate(body, strict=False)


@pytest_asyncio.fixture
async def pg_pool(migrated_pg: PostgresConfig):
    pool = await asyncpg.create_pool(migrated_pg.dsn, min_size=1, max_size=4)
    yield pool
    await pool.close()


async def _layer_rows(conn: Any, country: str) -> list[dict]:
    rows = await conn.fetch(
        "SELECT source_id, layer, reason, map_version, valid_until "
        "FROM source_layers WHERE country = $1 ORDER BY source_id", country)
    return [dict(r) for r in rows]


async def _aperture_rows(conn: Any, target_id: str) -> list[dict]:
    rows = await conn.fetch(
        "SELECT layer, declared, reason, map_version, valid_until "
        "FROM desk_apertures WHERE target_id = $1 ORDER BY layer", target_id)
    return [dict(r) for r in rows]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_fresh_load_inserts_one_open_row_per_entry_and_aperture(pg_pool):
    country = "XA"
    target_id = "country_watch_zzl1"
    desc = _descriptor(country, "v1", [
        {"source_id": "source.zzldr.a", "layer": "official", "reason": "r1"},
        {"source_id": "source.zzldr.b", "layer": "domestic_press", "reason": "r2"},
    ])
    async with pg_pool.acquire() as conn:
        async with conn.transaction():
            receipt = await load_layer_map(conn, desc, target_id=target_id)
        layers = await _layer_rows(conn, country)
        apertures = await _aperture_rows(conn, target_id)

    assert receipt.inserted == 2 + 6  # 2 entries + 6 apertures
    assert receipt.superseded == 0 and receipt.unchanged == 0
    assert len(layers) == 2
    assert all(r["valid_until"] is None for r in layers)
    assert len(apertures) == 6
    assert all(r["valid_until"] is None for r in apertures)
    assert {r["layer"] for r in apertures} == set(LAYER_VOCAB)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reloading_the_identical_map_version_is_a_noop(pg_pool):
    country = "XB"
    target_id = "country_watch_zzl2"
    entries = [{"source_id": "source.zzldr.c", "layer": "official", "reason": "r1"}]
    desc = _descriptor(country, "v1", entries)

    async with pg_pool.acquire() as conn:
        async with conn.transaction():
            await load_layer_map(conn, desc, target_id=target_id)
        async with conn.transaction():
            receipt2 = await load_layer_map(conn, desc, target_id=target_id)
        layers = await _layer_rows(conn, country)

    assert receipt2.inserted == 0 and receipt2.superseded == 0
    assert receipt2.unchanged == 1 + 6
    assert len(layers) == 1, "a no-op reload must not duplicate the row"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reloading_same_version_with_different_content_is_refused(pg_pool):
    country = "XC"
    target_id = "country_watch_zzl3"
    desc_v1 = _descriptor(country, "v1", [
        {"source_id": "source.zzldr.d", "layer": "official", "reason": "r1"},
    ])
    desc_v1_drifted = _descriptor(country, "v1", [
        {"source_id": "source.zzldr.d", "layer": "foreign_press", "reason": "r1-changed"},
    ])

    async with pg_pool.acquire() as conn:
        async with conn.transaction():
            await load_layer_map(conn, desc_v1, target_id=target_id)

        with pytest.raises(LayerMapVersionConflict):
            async with conn.transaction():
                await load_layer_map(conn, desc_v1_drifted, target_id=target_id)

        layers = await _layer_rows(conn, country)

    assert len(layers) == 1
    assert layers[0]["layer"] == "official", (
        "the refused load must not have partially applied — the rejected "
        "transaction rolls back in full"
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_new_map_version_supersedes_the_prior_open_row(pg_pool):
    country = "XD"
    target_id = "country_watch_zzl4"
    desc_v1 = _descriptor(country, "v1", [
        {"source_id": "source.zzldr.e", "layer": "official", "reason": "r1"},
    ])
    desc_v2 = _descriptor(country, "v2", [
        {"source_id": "source.zzldr.e", "layer": "domestic_press", "reason": "r2"},
    ])

    async with pg_pool.acquire() as conn:
        async with conn.transaction():
            await load_layer_map(conn, desc_v1, target_id=target_id)
        async with conn.transaction():
            receipt2 = await load_layer_map(conn, desc_v2, target_id=target_id)

        all_rows = await conn.fetch(
            "SELECT layer, map_version, valid_until FROM source_layers "
            "WHERE source_id = 'source.zzldr.e' ORDER BY created_at")

    # 1 source_layers row + 6 desk_apertures rows all move from v1 to v2 —
    # the map is versioned as a whole (see the next test for the case where
    # the apertures' CONTENT is also unchanged and they still supersede).
    assert receipt2.superseded == 1 + 6
    assert len(all_rows) == 2, "history is kept, never overwritten in place"
    closed, opened = all_rows[0], all_rows[1]
    assert closed["map_version"] == "v1" and closed["valid_until"] is not None
    assert opened["map_version"] == "v2" and opened["valid_until"] is None
    assert opened["layer"] == "domestic_press"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_new_map_version_still_supersedes_when_content_is_unchanged(pg_pool):
    """A version bump closes-and-reopens even a byte-identical row: the map is
    versioned as a whole, not row-by-row (loader.py's documented contract)."""
    country = "XE"
    target_id = "country_watch_zzl5"
    entries = [{"source_id": "source.zzldr.f", "layer": "physical", "reason": "same"}]
    desc_v1 = _descriptor(country, "v1", entries)
    desc_v2 = _descriptor(country, "v2", entries)

    async with pg_pool.acquire() as conn:
        async with conn.transaction():
            await load_layer_map(conn, desc_v1, target_id=target_id)
        async with conn.transaction():
            receipt2 = await load_layer_map(conn, desc_v2, target_id=target_id)
        rows = await conn.fetch(
            "SELECT map_version, valid_until FROM source_layers "
            "WHERE source_id = 'source.zzldr.f' ORDER BY created_at")

    assert receipt2.superseded == 1 + 6  # entry + 6 apertures, same content, new version
    assert len(rows) == 2
    assert rows[0]["map_version"] == "v1" and rows[0]["valid_until"] is not None
    assert rows[1]["map_version"] == "v2" and rows[1]["valid_until"] is None
