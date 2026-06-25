# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for OutputKind.NEXUS + write_nexus + _insert_nexus (PIECE A — the
reified typed Nexus). Mirrors test_writes_fact.py (the facts plumbing this is
a faithful copy of).

Covers:
  * OutputKind.NEXUS resolves via spec_for_kind; KIND_REGISTRY[NEXUS].table=='nexuses'.
  * NexusPayload validation (required subject/object/rel_type; polarity bound).
  * the 0033 migration applied (nexuses table + indexes present).
  * write_nexus happy path → row in `nexuses` with analyst_id + polarity set.
  * bad payload → output_dead_letter.
  * _insert_nexus ON CONFLICT upsert (confidence=max, lineage unioned).
  * supersession: a polarity/label CHANGE for an existing typed triple closes
    the prior open row (valid_until + superseded_by); same-value re-assert does
    NOT supersede.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID, uuid4

import asyncpg
import pytest
import pytest_asyncio

from legba.data.config import PostgresConfig
from legba.data.provenance import (
    AnalystContext,
    KIND_REGISTRY,
    NexusPayload,
    OutputKind,
    spec_for_kind,
    write_nexus,
)
from legba.data.sources._contract import Signal
from legba.runtime.source_actor import write_canonical_signal


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def pg_conn(migrated_pg: PostgresConfig):
    conn = await asyncpg.connect(migrated_pg.dsn)
    yield conn
    await conn.close()


def _analyst_ctx(target_id: str | None = "br_energy_test") -> AnalystContext:
    return AnalystContext(
        analyst_id=f"analyst.reifier_{uuid4().hex[:8]}",
        analyst_version="v" + uuid4().hex[:8],
        run_id=uuid4(),
        target_id=target_id,
        target_version="abc123def456" if target_id else None,
    )


async def _seed_signal(conn, *, title: str = "root") -> UUID:
    signal = Signal(
        source_id="rss_main",
        modality="text",
        payload={"title": title},
        content_hash=f"hash-{uuid4().hex}",
        fetched_at=datetime.now(tz=timezone.utc),
    )
    return await write_canonical_signal(
        conn, signal, source_version="v1", owner_tenant="default"
    )


# ---------------------------------------------------------------------------
# Registry / payload unit tests (no DB)
# ---------------------------------------------------------------------------


def test_output_kind_nexus_registered():
    assert OutputKind.NEXUS in KIND_REGISTRY
    spec = spec_for_kind("nexus")
    assert spec is spec_for_kind(OutputKind.NEXUS)
    assert spec.table == "nexuses"
    assert spec.payload_model is NexusPayload
    assert spec.schema_uri == "iglu:legba/nexus/jsonschema/1-0-0"
    assert spec.nats_subject_pattern == "analyst.{analyst_id}.nexus"


def test_nexus_payload_validates():
    with pytest.raises(Exception):
        NexusPayload(subject="x", rel_type="HostileTo")  # object required
    with pytest.raises(Exception):
        NexusPayload(subject="A", object="B", rel_type="HostileTo", polarity=5)
    p = NexusPayload(
        subject="Iran", object="Israel", rel_type="SuppliesWeaponsTo",
        intermediary="Hamas", polarity=-1, intent="hostile", channel="proxy",
    )
    assert p.kind_marker == "nexus"
    assert p.polarity == -1
    assert p.intermediary == "Hamas"


# ---------------------------------------------------------------------------
# Migration applied (PIECE A — the nexuses table + indexes exist)
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_nexuses_table_and_indexes_present(pg_conn):
    table = await pg_conn.fetchval(
        "SELECT to_regclass('public.nexuses')"
    )
    assert table == "nexuses", "migration 0033 must create public.nexuses"
    idx = {
        r["indexname"]
        for r in await pg_conn.fetch(
            "SELECT indexname FROM pg_indexes WHERE tablename = 'nexuses'"
        )
    }
    assert "idx_nexuses_triple_open" in idx
    assert "idx_nexuses_decay_sweep" in idx


# ---------------------------------------------------------------------------
# write_nexus round-trip (DB)
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_write_nexus_routes_to_nexuses_table(pg_conn):
    actx = _analyst_ctx()
    s1 = await _seed_signal(pg_conn, title="root")
    output, dlq = await write_nexus(
        pg_conn,
        analyst_ctx=actx,
        payload=NexusPayload(
            subject="Iran", object="Israel", rel_type="SuppliesWeaponsTo",
            intermediary="Hamas", polarity=-1, intent="hostile",
            channel="proxy", confidence=0.85,
        ),
        derived_from=[s1],
    )
    assert dlq is None and output is not None
    row = await pg_conn.fetchrow(
        "SELECT subject, intermediary, object, rel_type, polarity, intent, "
        "channel, confidence, analyst_id, derived_from, valid_until, "
        "superseded_by FROM nexuses WHERE id = $1",
        output.id,
    )
    assert row["subject"] == "Iran"
    assert row["intermediary"] == "Hamas"
    assert row["object"] == "Israel"
    # Phase B item 5: rel_type converges on the canonical lowercase-spaced form
    # (was the raw CamelCase "SuppliesWeaponsTo").
    assert row["rel_type"] == "supplies weapons to"
    assert row["polarity"] == -1
    assert row["confidence"] == pytest.approx(0.85)
    assert row["analyst_id"] == actx.analyst_id
    assert s1 in row["derived_from"]
    assert row["valid_until"] is None and row["superseded_by"] is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_write_nexus_bad_payload_dead_letters(pg_conn):
    actx = _analyst_ctx()
    output, dlq = await write_nexus(
        pg_conn,
        analyst_ctx=actx,
        payload={"subject": "", "object": "B", "rel_type": "HostileTo"},
        derived_from=[],
    )
    assert output is None
    assert dlq is not None
    found = await pg_conn.fetchval(
        "SELECT count(*) FROM output_dead_letter WHERE id = $1", dlq.id
    )
    assert found == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_write_nexus_on_conflict_upserts(pg_conn):
    actx = _analyst_ctx()
    s1 = await _seed_signal(pg_conn, title="a")
    s2 = await _seed_signal(pg_conn, title="b")

    def _payload(conf: float) -> NexusPayload:
        return NexusPayload(
            subject="Russia", object="Syria", rel_type="AlliedWith",
            polarity=1, intent="supportive", confidence=conf,
        )

    out1, _ = await write_nexus(
        pg_conn, analyst_ctx=actx, payload=_payload(0.4), derived_from=[s1]
    )
    out2, _ = await write_nexus(
        pg_conn, analyst_ctx=actx, payload=_payload(0.9), derived_from=[s2]
    )
    assert out1 is not None and out2 is not None
    rows = await pg_conn.fetch(
        "SELECT id, confidence, derived_from FROM nexuses "
        "WHERE lower(subject)='russia' AND lower(object)='syria' "
        "AND rel_type='allied with' "  # Phase B item 5: canonical form
        "AND valid_until IS NULL AND superseded_by IS NULL"
    )
    assert len(rows) == 1, "same-value typed triple must upsert to one open row"
    assert rows[0]["confidence"] == pytest.approx(0.9)
    lineage = rows[0]["derived_from"]
    assert s1 in lineage and s2 in lineage


@pytest.mark.integration
@pytest.mark.asyncio
async def test_write_nexus_polarity_change_supersedes_prior(pg_conn):
    """A polarity/label CHANGE for an existing typed triple closes the prior
    open row (valid_until + superseded_by → new id) and opens the new one. A
    same-value re-assert does NOT supersede."""
    actx = _analyst_ctx()
    s1 = await _seed_signal(pg_conn, title="a")
    s2 = await _seed_signal(pg_conn, title="b")

    # First: Turkey AlliedWith US (supportive, +1).
    out1, _ = await write_nexus(
        pg_conn, analyst_ctx=actx,
        payload=NexusPayload(
            subject="Turkey", object="USA", rel_type="AlliedWith",
            polarity=1, label="Turkey AlliedWith USA",
        ),
        derived_from=[s1],
    )
    # Re-type the SAME triple with a DIFFERENT polarity (relationship soured).
    out2, _ = await write_nexus(
        pg_conn, analyst_ctx=actx,
        payload=NexusPayload(
            subject="Turkey", object="USA", rel_type="AlliedWith",
            polarity=-1, label="Turkey AlliedWith USA",
        ),
        derived_from=[s2],
    )
    assert out1 is not None and out2 is not None

    prior = await pg_conn.fetchrow(
        "SELECT valid_until, superseded_by FROM nexuses WHERE id=$1", out1.id
    )
    assert prior["valid_until"] is not None, "prior must be closed"
    assert prior["superseded_by"] == out2.id, "prior must chain to the new row"

    new = await pg_conn.fetchrow(
        "SELECT valid_until, superseded_by, polarity FROM nexuses WHERE id=$1",
        out2.id,
    )
    assert new["valid_until"] is None and new["superseded_by"] is None
    assert new["polarity"] == -1

    open_count = await pg_conn.fetchval(
        "SELECT count(*) FROM nexuses "
        "WHERE lower(subject)='turkey' AND lower(object)='usa' "
        "AND rel_type='allied with' "  # Phase B item 5: canonical form
        "AND valid_until IS NULL AND superseded_by IS NULL"
    )
    assert open_count == 1, "exactly one canonical (open) row per typed triple"
