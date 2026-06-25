# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the SIPRI arms-transfer seed adapter (flavor-b, SIPRI tier).

Covers (the seeding-sipri stream deliverable):

  * the adapter is registered in ``ADAPTERS`` with source_type 'seed' and is
    visible to ``scripts/seed.py --list``;
  * it maps the curated YAML → typed SIGNED ``ArmsTransferTo`` nexuses
    (subject=supplier, object=recipient, polarity=+1) with a real valid_from,
    skipping malformed / self-loop rows;
  * end-to-end through the driver: fetched-from-YAML → entities resolved →
    written idempotently with the batch marker (no dup open triples on re-run).
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio

from legba.data.config import PostgresConfig
from legba.data.seed import (
    SeedContext,
    SeedNexus,
    get_adapter,
    list_adapters,
    run_seed_source,
)
from legba.data.seed.adapters.sipri_arms_transfers import (
    SIPRIArmsTransfersSeedSource,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def pg_pool(migrated_pg: PostgresConfig):
    pool = await asyncpg.create_pool(migrated_pg.dsn, min_size=1, max_size=4)
    yield pool
    await pool.close()


_FIXTURE_YAML = """
meta:
  curated_at: "2026-01-01"
transfers:
  - { supplier: "United States", recipient: "Saudi Arabia", valid_from: "2000-01-01", tiv_rank: 1 }
  - { supplier: "Russia",        recipient: "India",        valid_from: "2000-01-01", tiv_rank: 1 }
  # malformed (no recipient) -> skipped
  - { supplier: "France",        recipient: "",             valid_from: "2016-01-01" }
  # self-loop -> skipped
  - { supplier: "China",         recipient: "China",        valid_from: "2000-01-01" }
  # no valid_from -> skipped
  - { supplier: "Germany",       recipient: "Egypt" }
"""


@pytest.fixture
def fixture_yaml(tmp_path: Path) -> Path:
    p = tmp_path / "sipri_fixture.yaml"
    p.write_text(_FIXTURE_YAML, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# Registry / CLI surface
# ---------------------------------------------------------------------------


def test_sipri_registered():
    names = dict(list_adapters())
    assert names.get("sipri_arms_transfers") == "seed"
    adapter = get_adapter("sipri_arms_transfers")
    assert adapter.name == "sipri_arms_transfers"
    assert adapter.source_type == "seed"


# ---------------------------------------------------------------------------
# Mapping (no DB) — fixture YAML
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sipri_maps_signed_arms_nexuses(fixture_yaml: Path):
    adapter = SIPRIArmsTransfersSeedSource()
    raw = await adapter.fetch(SeedContext(options={"yaml_path": str(fixture_yaml)}))
    payloads = list(adapter.map(raw))

    nexuses = [p for p in payloads if isinstance(p, SeedNexus)]
    # Only the two well-formed, non-self-loop rows survive.
    assert len(nexuses) == 2
    pairs = {(n.subject, n.object) for n in nexuses}
    assert pairs == {
        ("United States", "Saudi Arabia"),
        ("Russia", "India"),
    }
    for n in nexuses:
        assert n.rel_type == "ArmsTransferTo"
        assert n.polarity == 1, "an arms transfer is a supportive (+1) tie"
        assert isinstance(n.valid_from, datetime)
        assert n.confidence == pytest.approx(0.90)
        assert n.data["seed_adapter"] == "sipri_arms_transfers"


@pytest.mark.asyncio
async def test_sipri_real_curated_yaml_maps():
    """The shipped seeds/sipri_arms_transfers.yaml parses + maps to many
    signed +1 ArmsTransferTo nexuses (no fixture override)."""
    adapter = SIPRIArmsTransfersSeedSource()
    raw = await adapter.fetch(SeedContext(dry_run=True))
    nexuses = [p for p in adapter.map(raw) if isinstance(p, SeedNexus)]
    assert len(nexuses) >= 12, "a couple dozen curated transfers"
    assert all(n.rel_type == "ArmsTransferTo" and n.polarity == 1 for n in nexuses)
    assert all(isinstance(n.valid_from, datetime) for n in nexuses)


# ---------------------------------------------------------------------------
# End-to-end through the driver + idempotency
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_sipri_driver_writes_and_is_idempotent(pg_pool, fixture_yaml: Path):
    adapter = SIPRIArmsTransfersSeedSource()
    opts = {"yaml_path": str(fixture_yaml)}

    # First run — writes signed nexuses + the batch row.
    r1 = await run_seed_source(pg_pool, adapter, dry_run=False, options=opts)
    assert not r1.errors, f"unexpected errors: {r1.errors}"
    assert r1.seed_batch_id is not None
    assert r1.source_type == "seed"
    assert r1.counts["nexuses"] == 2

    async with pg_pool.acquire() as conn:
        batch = await conn.fetchrow(
            "SELECT source, source_type FROM seed_batches WHERE id=$1",
            r1.seed_batch_id,
        )
        assert batch["source"] == "sipri_arms_transfers"
        assert batch["source_type"] == "seed"

        # A known transfer nexus is present, typed + signed + stamped.
        nx = await conn.fetchrow(
            "SELECT source_type, seed_batch_id, polarity, valid_from FROM nexuses "
            "WHERE lower(subject)='united states' AND rel_type='arms transfer to' "
            "AND lower(object)='saudi arabia' "
            "AND valid_until IS NULL AND superseded_by IS NULL"
        )
        assert nx is not None, "US ArmsTransferTo Saudi Arabia seeded"
        assert nx["source_type"] == "seed"
        assert nx["polarity"] == 1
        assert nx["valid_from"] is not None

        # Supplier + recipient resolved to exactly one country profile each.
        for name in ("united states", "saudi arabia", "russia", "india"):
            n = await conn.fetchval(
                "SELECT count(*) FROM entity_profiles "
                "WHERE lower(canonical_name)=$1 AND entity_class='country'",
                name,
            )
            assert n == 1, f"exactly one {name} country profile"

        open_nexus_1 = await conn.fetchval(
            "SELECT count(*) FROM nexuses WHERE source_type='seed' "
            "AND rel_type='arms transfer to' "
            "AND valid_until IS NULL AND superseded_by IS NULL"
        )

    # Second run — idempotent: NO new open nexus rows (upsert no-op).
    r2 = await run_seed_source(pg_pool, adapter, dry_run=False, options=opts)
    assert not r2.errors
    assert r2.seed_batch_id == r1.seed_batch_id, (
        "an identical re-import dedupes onto the same batch row (P3-3 ledger "
        "idempotency) rather than minting a duplicate"
    )

    async with pg_pool.acquire() as conn:
        open_nexus_2 = await conn.fetchval(
            "SELECT count(*) FROM nexuses WHERE source_type='seed' "
            "AND rel_type='arms transfer to' "
            "AND valid_until IS NULL AND superseded_by IS NULL"
        )
        us_n = await conn.fetchval(
            "SELECT count(*) FROM entity_profiles "
            "WHERE lower(canonical_name)='united states' AND entity_class='country'"
        )

    assert open_nexus_2 == open_nexus_1, "re-run must not add open nexus rows"
    assert us_n == 1, "re-run must not spawn a duplicate entity"
