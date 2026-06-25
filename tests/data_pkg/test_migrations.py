# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Integration tests for legba.data migrations.

Runs against a freshly-created test database (see conftest).
"""

from __future__ import annotations

import asyncpg
import pytest

from legba.data.config import PostgresConfig
from legba.data.postgres import PostgresStore
from legba.data.smoke import RETIRED_TABLES
from legba.data.vocabulary import ENTITY_CLASSES, RELATIONSHIP_TYPES, vertex_label

# NOTE (C-1): the retired pre-pivot tests that asserted the OLD substrate
# shape (legacy `sources`/`predictions` tables, analyst-provenance columns
# on `signals`) were DELETED. The post-pivot equivalents live in
# `legba.data.smoke` (EXPECTED_TABLES / SIGNAL_PROVENANCE_COLUMNS, already
# re-cut for migration 0024) and are exercised end-to-end by
# tests/data_pkg/test_smoke.py::test_smoke_passes.


@pytest.mark.integration
@pytest.mark.asyncio
async def test_retired_tables_absent(migrated_pg: PostgresConfig):
    store = PostgresStore(migrated_pg)
    await store.connect()
    try:
        present = set(await store.list_tables())
        unexpected = [t for t in RETIRED_TABLES if t in present]
        assert not unexpected, (
            f"retired tables found in fresh substrate: {unexpected} — "
            "these should not be created by migrations"
        )
    finally:
        await store.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_age_vocabulary_loaded(migrated_pg: PostgresConfig):
    store = PostgresStore(migrated_pg)
    await store.connect()
    try:
        labels = await store.graph_labels()
    finally:
        await store.close()

    vertices = set(labels["vertex"])
    edges = set(labels["edge"])

    expected_vertices = {vertex_label(ec) for ec in ENTITY_CLASSES}
    expected_edges = set(RELATIONSHIP_TYPES)

    missing_v = sorted(expected_vertices - vertices)
    missing_e = sorted(expected_edges - edges)
    assert not missing_v, f"missing AGE vertex labels: {missing_v}"
    assert not missing_e, f"missing AGE edge labels: {missing_e}"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_seed_vocabulary_inserted(migrated_pg: PostgresConfig):
    conn = await asyncpg.connect(migrated_pg.dsn)
    try:
        ec_rows = await conn.fetch(
            "SELECT value FROM vocabulary_entries WHERE family = 'entity_class'"
        )
        rt_rows = await conn.fetch(
            "SELECT value FROM vocabulary_entries WHERE family = 'relationship_type'"
        )
        ec_values = {r["value"] for r in ec_rows}
        rt_values = {r["value"] for r in rt_rows}

        # 0010 baseline vocab must be present; 0020 (L-200 geopolitical
        # extension) layers additional rows for the geopolitical template.
        # Subset (not equality) keeps the test stable as future migrations
        # extend the vocabulary.
        missing_ec = set(ENTITY_CLASSES) - ec_values
        missing_rt = set(RELATIONSHIP_TYPES) - rt_values
        assert not missing_ec, f"entity_class seed missing rows: {missing_ec}"
        assert not missing_rt, f"relationship_type seed missing rows: {missing_rt}"

        # 0020 extension rows (L-200 / Wave D).
        geopolitical_ec = {
            "military_unit", "political_party", "armed_group",
            "international_org", "media_outlet", "event_series",
            "commodity", "infrastructure",
        }
        geopolitical_rt = {
            "TradesWith", "BordersWith", "SignatoryTo", "SanctionsAgainst",
            "OccupiedBy", "SubsidiaryOf", "PartnersWith", "CompetesWith",
            "DiplomaticRelationsWith", "MilitaryPresenceIn",
        }
        missing_geo_ec = geopolitical_ec - ec_values
        missing_geo_rt = geopolitical_rt - rt_values
        assert not missing_geo_ec, f"0020 entity_class extension missing: {missing_geo_ec}"
        assert not missing_geo_rt, f"0020 relationship_type extension missing: {missing_geo_rt}"

        # Aliases preserved
        involved_in = await conn.fetchrow(
            "SELECT aliases FROM vocabulary_entries "
            "WHERE family = 'relationship_type' AND value = 'InvolvedIn'"
        )
        assert involved_in is not None
        aliases = set(involved_in["aliases"])
        assert "INVOLVED_IN" in aliases
        assert "TRACKED_BY" in aliases
    finally:
        await conn.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_migration_ledger_records(migrated_pg: PostgresConfig):
    conn = await asyncpg.connect(migrated_pg.dsn)
    try:
        rows = await conn.fetch(
            "SELECT name, sha256 FROM legba_data_migrations ORDER BY name"
        )
        names = [r["name"] for r in rows]
        # The 30-migration chain (0001..0031) was flattened to a single baseline
        # for the clean-slate release (no instances to upgrade), so the ledger
        # records just the baseline. Schema correctness is covered by the per-
        # feature schema tests, not by asserting historical migration filenames.
        assert "0001_baseline.sql" in names
        # Every recorded migration has a non-empty sha256.
        assert all(len(r["sha256"]) == 64 for r in rows)
    finally:
        await conn.close()
