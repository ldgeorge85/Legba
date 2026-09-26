# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the V3/P3 entity-edge transition ledger (migration 0206,
spec §3.5) and its ``LEGBA_EDGE_TRANSITION_LEDGER`` gate.

The claims under test:

* FLAG OFF (the shipped default) — the write path is byte-identical: the
  nexus dual-write lands exactly as before and ``entity_edge_events`` stays
  empty.
* FLAG ON — the transitions land IN the same transaction as the edge write:
  ``observed`` on a minted edge, ``polarity_flip`` on each edge closed by a
  contradicting re-assert, and NOTHING for a same-polarity fold (transitions
  only, never re-observations — the ~2,900/week of noise spec §3.5 priced).
* APPEND-ONLY — UPDATE and DELETE fail at the database (the 0184
  situation_events contract, verbatim).
"""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from legba.data.provenance.entity_edge_writes import (
    edge_transition_ledger_enabled,
    write_entity_edge_for_nexus,
)


@pytest_asyncio.fixture
async def pg_pool(migrated_pg):
    pool = await asyncpg.create_pool(
        host=migrated_pg.host,
        port=migrated_pg.port,
        user=migrated_pg.user,
        password=migrated_pg.password,
        database=migrated_pg.database,
        min_size=1,
        max_size=4,
    )
    yield pool
    await pool.close()


@pytest_asyncio.fixture(autouse=True)
async def _clean(clean_tables):
    """The ledger assertions are exact-count, so each test starts empty."""
    await clean_tables(
        "entity_edge_events", "entity_edges", "entity_profiles")


async def _entity(pool, name: str):
    """Mint the entity profile an edge's endpoints resolve to."""
    async with pool.acquire() as conn:
        return await conn.fetchval(
            """INSERT INTO entity_profiles
                 (canonical_name, entity_class, entity_type, data)
               VALUES ($1, 'organization', 'organization', '{}'::jsonb)
               RETURNING id""",
            name)


async def _nexus_write(pool, *, subject: str, object_: str, polarity: int,
                       rel_type: str = "supports"):
    """One real dual-write call — the same path _insert_nexus wraps in its
    transaction (we open ours here so the ledger commits with the edge)."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            return await write_entity_edge_for_nexus(
                conn,
                edge_id=uuid4(),
                subject=subject,
                object_=object_,
                intermediary=None,
                rel_type=rel_type,
                polarity=polarity,
                intent="",
                channel="direct",
                confidence=0.9,
                valid_from=None,
                valid_until=None,
                produced_at=datetime.now(timezone.utc),
                source_signal_ids=[uuid4()],
                derived_from=[uuid4()],
                data=None,
                source_type="agent",
                seed_batch_id=None,
                analyst_id="ledger_test",
                analyst_version="1",
                run_id=None,
                target_id=None,
                target_version=None,
                nexus_id=uuid4(),
            )


async def _ledger_rows(pool, edge_id=None):
    async with pool.acquire() as conn:
        if edge_id is None:
            return await conn.fetch(
                "SELECT * FROM entity_edge_events ORDER BY occurred_at, id")
        return await conn.fetch(
            "SELECT * FROM entity_edge_events WHERE edge_id = $1 "
            "ORDER BY occurred_at, id", edge_id)


def test_flag_defaults_off(monkeypatch):
    monkeypatch.delenv("LEGBA_EDGE_TRANSITION_LEDGER", raising=False)
    assert edge_transition_ledger_enabled() is False
    monkeypatch.setenv("LEGBA_EDGE_TRANSITION_LEDGER", "1")
    assert edge_transition_ledger_enabled() is True
    monkeypatch.setenv("LEGBA_EDGE_TRANSITION_LEDGER", "off")
    assert edge_transition_ledger_enabled() is False


@pytest.mark.asyncio
async def test_flag_off_writes_no_ledger_rows(pg_pool, monkeypatch):
    """The byte-identity contract: flag off, the dual-write is unchanged and
    the ledger stays empty."""
    monkeypatch.delenv("LEGBA_EDGE_TRANSITION_LEDGER", raising=False)
    tag = uuid4().hex[:8]
    await _entity(pg_pool, f"LedgeOffA_{tag}")
    await _entity(pg_pool, f"LedgeOffB_{tag}")
    outcome = await _nexus_write(
        pg_pool, subject=f"LedgeOffA_{tag}", object_=f"LedgeOffB_{tag}",
        polarity=1)
    assert outcome == "written"
    assert await _ledger_rows(pg_pool) == []


@pytest.mark.asyncio
async def test_insert_lands_an_observed_row(pg_pool, monkeypatch):
    monkeypatch.setenv("LEGBA_EDGE_TRANSITION_LEDGER", "1")
    tag = uuid4().hex[:8]
    await _entity(pg_pool, f"LedgeInsA_{tag}")
    await _entity(pg_pool, f"LedgeInsB_{tag}")
    await _nexus_write(
        pg_pool, subject=f"LedgeInsA_{tag}", object_=f"LedgeInsB_{tag}",
        polarity=1)

    rows = await _ledger_rows(pg_pool)
    assert len(rows) == 1
    row = rows[0]
    assert row["transition"] == "observed"
    assert row["polarity_to"] == 1
    assert row["edge_type_to"] == "supports"
    assert row["analyst_id"] == "ledger_test"
    assert row["why"].strip() != ""
    # The evidence the write carried travels on the row.
    assert len(row["derived_from"]) >= 1


@pytest.mark.asyncio
async def test_fold_writes_no_ledger_rows(pg_pool, monkeypatch):
    """A same-polarity re-assert folds in place — a re-observation, NOT a
    transition (spec §3.5's priced distinction)."""
    monkeypatch.setenv("LEGBA_EDGE_TRANSITION_LEDGER", "1")
    tag = uuid4().hex[:8]
    await _entity(pg_pool, f"LedgeFoldA_{tag}")
    await _entity(pg_pool, f"LedgeFoldB_{tag}")
    await _nexus_write(
        pg_pool, subject=f"LedgeFoldA_{tag}", object_=f"LedgeFoldB_{tag}",
        polarity=1)
    await _nexus_write(  # the fold — same triple, same polarity
        pg_pool, subject=f"LedgeFoldA_{tag}", object_=f"LedgeFoldB_{tag}",
        polarity=1)

    rows = await _ledger_rows(pg_pool)
    assert [r["transition"] for r in rows] == ["observed"]


@pytest.mark.asyncio
async def test_polarity_flip_lands_a_transition_on_the_closed_edge(
        pg_pool, monkeypatch):
    """A contradicting re-assert closes the old edge and the ledger says so:
    'polarity_flip' on the CLOSED row, 'observed' on the replacement."""
    monkeypatch.setenv("LEGBA_EDGE_TRANSITION_LEDGER", "1")
    tag = uuid4().hex[:8]
    await _entity(pg_pool, f"LedgeFlipA_{tag}")
    await _entity(pg_pool, f"LedgeFlipB_{tag}")
    await _nexus_write(
        pg_pool, subject=f"LedgeFlipA_{tag}", object_=f"LedgeFlipB_{tag}",
        polarity=1)
    await _nexus_write(
        pg_pool, subject=f"LedgeFlipA_{tag}", object_=f"LedgeFlipB_{tag}",
        polarity=-1)

    rows = await _ledger_rows(pg_pool)
    transitions = sorted(r["transition"] for r in rows)
    # The first mint, the flip on the closed edge, the replacement's mint.
    assert transitions == ["observed", "observed", "polarity_flip"]

    flip = next(r for r in rows if r["transition"] == "polarity_flip")
    assert flip["polarity_from"] == 1
    assert flip["polarity_to"] == -1
    # The replacing edge leads the evidence — the CHECK's non-empty rule.
    assert len(flip["derived_from"]) >= 1
    # The flip row hangs off the CLOSED edge: it is the edge whose
    # valid_until just got stamped.
    async with pg_pool.acquire() as conn:
        closed_edge = await conn.fetchrow(
            "SELECT id, valid_until, superseded_by FROM entity_edges "
            " WHERE id = $1", flip["edge_id"])
    assert closed_edge["valid_until"] is not None
    assert closed_edge["superseded_by"] is not None


@pytest.mark.asyncio
async def test_ledger_is_append_only(pg_pool, monkeypatch):
    """The 0184 contract verbatim: UPDATE and DELETE fail loud at the db."""
    monkeypatch.setenv("LEGBA_EDGE_TRANSITION_LEDGER", "1")
    tag = uuid4().hex[:8]
    await _entity(pg_pool, f"LedgeApA_{tag}")
    await _entity(pg_pool, f"LedgeApB_{tag}")
    await _nexus_write(
        pg_pool, subject=f"LedgeApA_{tag}", object_=f"LedgeApB_{tag}",
        polarity=1)
    rows = await _ledger_rows(pg_pool)
    assert rows

    async with pg_pool.acquire() as conn:
        with pytest.raises(asyncpg.PostgresError):
            await conn.execute(
                "UPDATE entity_edge_events SET why = 'rewrite' WHERE id = $1",
                rows[0]["id"])
    async with pg_pool.acquire() as conn:
        with pytest.raises(asyncpg.PostgresError):
            await conn.execute(
                "DELETE FROM entity_edge_events WHERE id = $1",
                rows[0]["id"])
