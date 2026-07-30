# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""C4 — the ``fact_decay_scan`` deterministic decay-readout stamper.

Registry wiring + pure ``compute_readouts`` (sighting derivation, non-mutation
shape) + the honest summary finding (zero-state + revoke listing). Ephemeral-DB
(``migrated_pg``): the sidecar stamp + idempotency (a second identical run
leaves the row content byte-stable), the CLOSED-fact prune, and — the C4 HARD
RULE — that a scan NEVER mutates ``facts.confidence``.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from legba.data.analysts.deterministic import (
    OUTPUT_KIND_BY_SUB_HANDLER,
    SUB_HANDLERS,
)
from legba.data.analysts.deterministic_handlers import fact_decay_scan as fds
from legba.data.config import PostgresConfig
from legba.data.facts.decay import default_decay_config
from legba.data.provenance.kinds import (
    STRUCTURAL_VERIFY_EXEMPT_ANALYSTS,
    OutputKind,
)
from legba.runtime.analyst_method import AnalystMethodResult

_NOW = datetime(2026, 7, 24, 12, 0, 0, tzinfo=timezone.utc)


def _days_ago(days: float) -> datetime:
    return _NOW - timedelta(days=days)


# ---------------------------------------------------------------------------
# Registry wiring
# ---------------------------------------------------------------------------


def test_registered_as_finding_sub_handler_and_structural_exempt():
    assert SUB_HANDLERS["fact_decay_scan"] is fds.handle
    assert OUTPUT_KIND_BY_SUB_HANDLER["fact_decay_scan"] is OutputKind.FINDING
    assert "fact_decay_scan" in STRUCTURAL_VERIFY_EXEMPT_ANALYSTS


async def test_refuses_loud_without_pool():
    with pytest.raises(RuntimeError, match="pg_pool"):
        await fds.handle([], {"sub_handler": "fact_decay_scan"}, None)


# ---------------------------------------------------------------------------
# Pure — compute_readouts (sighting derivation)
# ---------------------------------------------------------------------------


def test_signal_sighting_overrides_created_at_when_newer():
    rows = [
        {
            "id": uuid4(), "subject": "Iran", "predicate": "hostile to",
            "value": "Israel", "confidence": 0.9, "source_type": "ingestion",
            "created_at": _days_ago(300),
            "last_signal_at": _days_ago(1),  # a fresh corroborating sighting
        }
    ]
    out = fds.compute_readouts(rows, now=_NOW, config=default_decay_config())
    assert out[0]["sighting_source"] == "signal"
    assert out[0]["decay_state"] == "fresh"


def test_created_at_is_the_fallback_when_no_signal():
    rows = [
        {
            "id": uuid4(), "subject": "France", "predicate": "leader of",
            "value": "X", "confidence": 0.9, "source_type": "seed",
            "created_at": _days_ago(10),
            "last_signal_at": None,  # seed fact, signals purged
        }
    ]
    out = fds.compute_readouts(rows, now=_NOW, config=default_decay_config())
    assert out[0]["sighting_source"] == "created_at"
    assert out[0]["decay_state"] == "fresh"


def test_signal_older_than_birth_never_pins_newer_than_created_at():
    """A signal observed BEFORE the row was born is the first sighting, not a
    reset — created_at wins."""
    rows = [
        {
            "id": uuid4(), "subject": "X", "predicate": "member of",
            "value": "Y", "confidence": 0.9, "source_type": "ingestion",
            "created_at": _days_ago(5),
            "last_signal_at": _days_ago(400),  # stale backing signal
        }
    ]
    out = fds.compute_readouts(rows, now=_NOW, config=default_decay_config())
    assert out[0]["sighting_source"] == "created_at"


# ---------------------------------------------------------------------------
# Pure — the honest summary finding
# ---------------------------------------------------------------------------


def test_summary_zero_state_is_honest():
    finding = fds._build_finding(
        readouts=[], pruned=0, top_candidates=10, config=default_decay_config()
    )
    assert "0 open facts" in finding.title
    assert finding.data["open_facts_examined"] == 0
    assert finding.data["counts_per_state"] == {
        "fresh": 0, "aging": 0, "stale": 0, "revoke_candidate": 0
    }
    assert "honest zero" in finding.body


def test_summary_lists_top_revoke_candidates_lowest_first():
    readouts = [
        {
            "fact_id": uuid4(), "subject": "A", "predicate": "reported",
            "value": "v1", "stored_confidence": 0.5, "decayed_confidence": 0.05,
            "decay_state": "revoke_candidate", "decay_class": "event",
            "retention": 0.1, "lifetime_days": 45, "elapsed_days": 40,
            "last_sighting_at": _days_ago(40), "sighting_source": "signal",
        },
        {
            "fact_id": uuid4(), "subject": "B", "predicate": "targets",
            "value": "v2", "stored_confidence": 0.6, "decayed_confidence": 0.15,
            "decay_state": "revoke_candidate", "decay_class": "event",
            "retention": 0.25, "lifetime_days": 45, "elapsed_days": 30,
            "last_sighting_at": _days_ago(30), "sighting_source": "signal",
        },
        {
            "fact_id": uuid4(), "subject": "C", "predicate": "leader of",
            "value": "v3", "stored_confidence": 0.9, "decayed_confidence": 0.85,
            "decay_state": "fresh", "decay_class": "officeholder",
            "retention": 0.95, "lifetime_days": 730, "elapsed_days": 10,
            "last_sighting_at": _days_ago(10), "sighting_source": "signal",
        },
    ]
    finding = fds._build_finding(
        readouts=readouts, pruned=3, top_candidates=10,
        config=default_decay_config(),
    )
    top = finding.data["top_revoke_candidates"]
    assert [t["subject"] for t in top] == ["A", "B"]  # lowest decayed first
    assert finding.data["counts_per_state"]["revoke_candidate"] == 2
    assert finding.data["counts_per_state"]["fresh"] == 1
    assert finding.data["sidecar_rows_pruned"] == 3
    assert "no facts.confidence was mutated" in finding.body.lower()


# ---------------------------------------------------------------------------
# Ephemeral-DB rig
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def pg_pool(migrated_pg: PostgresConfig):
    pool = await asyncpg.create_pool(migrated_pg.dsn, min_size=1, max_size=4)
    yield pool
    await pool.close()


@pytest_asyncio.fixture
async def clean_slate(pg_pool):
    async with pg_pool.acquire() as conn:
        await conn.execute("DELETE FROM fact_decay_states")
        await conn.execute("DELETE FROM facts WHERE subject LIKE 'DKTEST_%'")
        await conn.execute(
            "DELETE FROM analyst_outputs WHERE analyst_id = 'fact_decay_scan'"
        )
    yield


class _Deps:
    def __init__(self, pool):
        self.pg_pool = pool


async def _insert_fact(conn, *, subject, predicate, value, confidence,
                       source_type="ingestion", created_at, valid_until=None,
                       superseded_by=None):
    fid = uuid4()
    await conn.execute(
        """
        INSERT INTO facts (id, subject, predicate, value, confidence,
                           source_type, data, created_at, updated_at,
                           valid_until, superseded_by)
        VALUES ($1, $2, $3, $4, $5, $6, '{}'::jsonb, $7, $7, $8, $9)
        """,
        fid, subject, predicate, value, confidence, source_type,
        created_at, valid_until, superseded_by,
    )
    return fid


async def _run(pool, **opts):
    result = await fds.handle(
        [],
        {"sub_handler": "fact_decay_scan", "run_id": str(uuid4()), **opts},
        _Deps(pool),
    )
    assert isinstance(result, AnalystMethodResult)
    return result


@pytest.mark.integration
@pytest.mark.asyncio
async def test_sidecar_migration_present(pg_pool):
    async with pg_pool.acquire() as conn:
        cols = {
            r["column_name"]
            for r in await conn.fetch(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema='public' AND table_name='fact_decay_states'"
            )
        }
    assert {"fact_id", "decayed_confidence", "decay_state", "last_sighting_at",
            "stored_confidence"} <= cols


@pytest.mark.integration
@pytest.mark.asyncio
async def test_scan_stamps_sidecar_without_mutating_facts(pg_pool, clean_slate):
    async with pg_pool.acquire() as conn:
        fresh = await _insert_fact(
            conn, subject="DKTEST_fresh", predicate="leader of", value="P",
            confidence=0.9, source_type="seed",
            created_at=datetime.now(timezone.utc) - timedelta(days=5),
        )
        stale = await _insert_fact(
            conn, subject="DKTEST_stale", predicate="involved in conflict event",
            value="E", confidence=0.8, source_type="ingestion",
            created_at=datetime.now(timezone.utc) - timedelta(days=200),
        )

    await _run(pg_pool)

    async with pg_pool.acquire() as conn:
        # facts.confidence is UNCHANGED — the C4 hard rule.
        assert await conn.fetchval(
            "SELECT confidence FROM facts WHERE id=$1", fresh
        ) == pytest.approx(0.9)
        assert await conn.fetchval(
            "SELECT confidence FROM facts WHERE id=$1", stale
        ) == pytest.approx(0.8)
        # facts.updated_at is untouched by the scan (no marker/mutation).
        f_row = await conn.fetchrow(
            "SELECT created_at, updated_at FROM facts WHERE id=$1", fresh
        )
        assert f_row["created_at"] == f_row["updated_at"]
        # The sidecar carries the readouts.
        fresh_state = await conn.fetchrow(
            "SELECT decay_state, stored_confidence, decayed_confidence "
            "FROM fact_decay_states WHERE fact_id=$1", fresh
        )
        stale_state = await conn.fetchval(
            "SELECT decay_state FROM fact_decay_states WHERE fact_id=$1", stale
        )
    assert fresh_state["decay_state"] == "fresh"
    assert fresh_state["stored_confidence"] == pytest.approx(0.9)
    assert fresh_state["decayed_confidence"] <= 0.9
    assert stale_state in ("stale", "revoke_candidate")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_scan_is_idempotent(pg_pool, clean_slate):
    async with pg_pool.acquire() as conn:
        fid = await _insert_fact(
            conn, subject="DKTEST_idem", predicate="member of", value="Org",
            confidence=0.7, source_type="ingestion",
            created_at=datetime.now(timezone.utc) - timedelta(days=30),
        )

    # ISOLATION NOTE: the scan stamps EVERY open fact in the session-shared
    # migrated DB, so global count(*) assertions are order-fragile (any earlier
    # test file that leaves an open fact behind adds a sidecar row). Scope the
    # duplicate-accumulation check to THIS test's fact — the per-fact claim is
    # the idempotency contract anyway.
    await _run(pg_pool)
    async with pg_pool.acquire() as conn:
        first = await conn.fetchrow(
            "SELECT decay_state, decayed_confidence, last_sighting_at "
            "FROM fact_decay_states WHERE fact_id=$1", fid
        )
        rows_after_first = await conn.fetchval(
            "SELECT count(*) FROM fact_decay_states WHERE fact_id=$1", fid
        )

    await _run(pg_pool)
    async with pg_pool.acquire() as conn:
        second = await conn.fetchrow(
            "SELECT decay_state, decayed_confidence, last_sighting_at "
            "FROM fact_decay_states WHERE fact_id=$1", fid
        )
        rows_after_second = await conn.fetchval(
            "SELECT count(*) FROM fact_decay_states WHERE fact_id=$1", fid
        )
    # One row per fact — no duplicate accumulation; content stable.
    assert rows_after_first == rows_after_second == 1
    assert second["decay_state"] == first["decay_state"]
    assert second["decayed_confidence"] == pytest.approx(
        first["decayed_confidence"], abs=1e-3
    )
    assert second["last_sighting_at"] == first["last_sighting_at"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_closed_fact_readout_is_pruned(pg_pool, clean_slate):
    async with pg_pool.acquire() as conn:
        keeper = await _insert_fact(
            conn, subject="DKTEST_open", predicate="leader of", value="P",
            confidence=0.9, source_type="seed",
            created_at=datetime.now(timezone.utc) - timedelta(days=3),
        )
        closing = await _insert_fact(
            conn, subject="DKTEST_toclose", predicate="leader of", value="Q",
            confidence=0.9, source_type="seed",
            created_at=datetime.now(timezone.utc) - timedelta(days=3),
        )
    # First scan stamps both. ISOLATION NOTE: scope every count to THIS test's
    # two facts — the scan stamps every open fact in the session-shared DB, so
    # a global count(*) is order-fragile (see test_scan_is_idempotent).
    _mine = "SELECT count(*) FROM fact_decay_states WHERE fact_id = ANY($1::uuid[])"
    await _run(pg_pool)
    async with pg_pool.acquire() as conn:
        assert await conn.fetchval(_mine, [keeper, closing]) == 2
        # Close one fact (supersede it).
        await conn.execute(
            "UPDATE facts SET valid_until = now(), superseded_by = $1 "
            "WHERE id = $2", keeper, closing,
        )
    # Second scan prunes the closed fact's readout.
    result = await _run(pg_pool)
    async with pg_pool.acquire() as conn:
        remaining = await conn.fetchval(_mine, [keeper, closing])
        survivor = await conn.fetchval(
            "SELECT fact_id FROM fact_decay_states "
            "WHERE fact_id = ANY($1::uuid[])", [keeper, closing],
        )
    assert remaining == 1
    assert survivor == keeper
    assert result.finding.data["sidecar_rows_pruned"] >= 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_scan_finding_is_finding_and_reports_distribution(pg_pool, clean_slate):
    async with pg_pool.acquire() as conn:
        await _insert_fact(
            conn, subject="DKTEST_a", predicate="located in", value="Region",
            confidence=0.95, source_type="seed",
            created_at=datetime.now(timezone.utc) - timedelta(days=1),
        )
    result = await _run(pg_pool)
    data = result.finding.data
    assert data["sub_handler"] == "fact_decay_scan"
    assert data["open_facts_examined"] >= 1
    assert sum(data["counts_per_state"].values()) == data["open_facts_examined"]
    assert result.usage["prompt_tokens"] == 0
