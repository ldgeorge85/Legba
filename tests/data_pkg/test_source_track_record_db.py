# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A6 P3-3 — EARNED source track record, real-Postgres integration (layer 3).

Drives the measurement against a MIGRATED ephemeral DB (the ``migrated_pg``
fixture applies migration 0099) to prove end to end:

  * win/loss detection over a RESOLVED contention via the fact->signal->source
    lineage — a source on the surfaced-winner cluster scores a WIN, a source on
    only losing clusters scores a LOSS, one outcome per (contention, source);
  * corroboration counting (a carried cluster with >= 2 distinct sources);
  * the CIRCULARITY-GUARD lag — a contention surfaced INSIDE the lag window does
    NOT contribute (guard (a)); and the acyclicity exclusion drops the very
    contention being decided (guard (c));
  * ``store_source_records`` round-trip + wholesale prune (a source that ages
    out of the window leaves no stale row);
  * the ``handle`` entry point refreshes the table + returns an honest FINDING;
  * the assurance route's ``earned`` section + the ``/sources``
    ``earned_win_rate`` projection (additive, display-only).
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import asyncpg
import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from nacl.signing import SigningKey

from legba.data.analysts.deterministic_handlers import source_track_record as strk
from legba.data.config import PostgresConfig
from legba.data.postgres import PostgresStore
from legba.data.registry.api import (
    API_TOKEN_ENV,
    RegistryAPIDeps,
    build_router,
    load_earned_win_rates,
)
from legba.data.registry.audit import AuditLogger
from legba.data.registry.credentials import CredentialVault, MASTER_KEY_ENV
from legba.data.registry.descriptor import DescriptorRegistry
from legba.data.registry.dlq import DescriptorDeadLetter
from legba.data.registry.signing import SigningIdentity
from legba.data.registry.source_assurance_api import (
    build_source_assurance_router,
    load_earned_record,
)
from legba.data.registry.stack import StackRegistry
from legba.data.registry.vocabulary_cache import VocabularyCache

_TEST_MASTER_KEY_HEX = "0011223344556677889900112233445566778899001122334455667788990011"
os.environ.setdefault(MASTER_KEY_ENV, _TEST_MASTER_KEY_HEX)
os.environ.setdefault("LEGBA_REGISTRY_SIGNING_KEY", "44" * 32)

NOW = datetime(2026, 7, 24, 12, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Substrate-seeding helpers.
# ---------------------------------------------------------------------------


async def _signal(conn: Any, source_id: str) -> UUID:
    sid = uuid4()
    await conn.execute(
        "INSERT INTO signals (id, source_id) VALUES ($1, $2)", sid, source_id,
    )
    return sid


async def _fact(conn: Any, subject: str, predicate: str, value: str,
                signal_ids: list[UUID], *, seq: int = 0) -> UUID:
    fid = uuid4()
    # Distinct valid_from per row so same-value rows from different sources
    # legitimately COEXIST open (the open-triple unique index keys on
    # (subject, predicate, value, COALESCE(valid_from, epoch))) — the shape of a
    # real N-sources-agree-on-one-value cluster.
    valid_from = NOW - timedelta(minutes=seq)
    await conn.execute(
        """
        INSERT INTO facts (id, subject, predicate, value, confidence,
                           valid_from, derived_from)
        VALUES ($1, $2, $3, $4, 0.7, $5, $6::uuid[])
        """,
        fid, subject, predicate, value, valid_from, signal_ids,
    )
    return fid


async def _fcv(conn: Any, cid: UUID, value_key: str, fact_ids: list[UUID],
               *, winner: bool, dsc: int, junk: bool = False) -> None:
    await conn.execute(
        """
        INSERT INTO fact_contention_values
            (contention_id, value_key, representative_fact_id,
             distinct_source_count, supporting_fact_ids, surfaced_winner, is_junk)
        VALUES ($1, $2, $3, $4, $5::uuid[], $6, $7)
        """,
        cid, value_key, fact_ids[0], dsc, fact_ids, winner, junk,
    )


async def _resolved_contention(
    conn: Any, *, subject_key: str, predicate_key: str,
    winner_value: str, winner_facts: list[UUID], winner_dsc: int,
    loser_value: str, loser_facts: list[UUID], loser_dsc: int,
    surfaced_at: datetime,
) -> UUID:
    cid = uuid4()
    await conn.execute(
        """
        INSERT INTO fact_contention
            (id, subject_key, predicate_key, status, surfaced_value,
             surfaced_fact_id, surfaced_by, surfaced_at, opened_at)
        VALUES ($1, $2, $3, 'surfaced', $4, $5, 'deterministic', $6, $6)
        """,
        cid, subject_key, predicate_key, winner_value, winner_facts[0], surfaced_at,
    )
    await _fcv(conn, cid, winner_value, winner_facts, winner=True, dsc=winner_dsc)
    await _fcv(conn, cid, loser_value, loser_facts, winner=False, dsc=loser_dsc)
    return cid


# ---------------------------------------------------------------------------
# Fixtures.
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def pg_pool(migrated_pg: PostgresConfig):
    pool = await asyncpg.create_pool(migrated_pg.dsn, min_size=1, max_size=4)
    yield pool
    await pool.close()


@pytest_asyncio.fixture
async def clean(pg_pool):
    async with pg_pool.acquire() as conn:
        await conn.execute("DELETE FROM source_track_records")
        await conn.execute("DELETE FROM fact_contention_values")
        await conn.execute("DELETE FROM fact_contention")
        await conn.execute("DELETE FROM facts")
        await conn.execute("DELETE FROM signals")
    yield


# ---------------------------------------------------------------------------
# Part A — the measurement.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_win_loss_and_corroboration_over_resolved_contention(pg_pool, clean):
    async with pg_pool.acquire() as conn:
        sw = await _signal(conn, "source.winner")
        sc = await _signal(conn, "source.corrob")
        sl = await _signal(conn, "source.loser")
        w1 = await _fact(conn, "India", "border status", "de-escalating", [sw], seq=0)
        w2 = await _fact(conn, "India", "border status", "de-escalating", [sc], seq=1)
        l1 = await _fact(conn, "India", "border status", "clashes ongoing", [sl], seq=2)
        await _resolved_contention(
            conn,
            subject_key="india", predicate_key="border status",
            winner_value="de-escalating", winner_facts=[w1, w2], winner_dsc=2,
            loser_value="clashes ongoing", loser_facts=[l1], loser_dsc=1,
            surfaced_at=NOW - timedelta(hours=100),   # settled (> 72h lag)
        )
        records = await strk.compute_source_records(conn, now=NOW, lag_hours=72.0)

    by = {r.source_id: r for r in records}
    assert by["source.winner"].wins == 1 and by["source.winner"].losses == 0
    assert by["source.corrob"].wins == 1 and by["source.corrob"].losses == 0
    assert by["source.loser"].wins == 0 and by["source.loser"].losses == 1
    # Corroboration: the winning cluster had 2 distinct sources -> corroborated
    # for both its carriers; the 1-source loser cluster is not corroborated.
    assert by["source.winner"].corroborated == 1
    assert by["source.winner"].corroboration_total == 1
    assert by["source.loser"].corroborated == 0
    assert by["source.loser"].corroboration_total == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_lag_window_excludes_recent_contention(pg_pool, clean):
    """A contention resolved INSIDE the lag window contributes NOTHING (guard a):
    the record is a function only of SETTLED disputes."""
    async with pg_pool.acquire() as conn:
        sw = await _signal(conn, "source.winner")
        sl = await _signal(conn, "source.loser")
        w = await _fact(conn, "Gaza", "ceasefire", "holding", [sw])
        l = await _fact(conn, "Gaza", "ceasefire", "collapsed", [sl])
        await _resolved_contention(
            conn,
            subject_key="gaza", predicate_key="ceasefire",
            winner_value="holding", winner_facts=[w], winner_dsc=1,
            loser_value="collapsed", loser_facts=[l], loser_dsc=1,
            surfaced_at=NOW - timedelta(hours=1),   # INSIDE the 72h lag window
        )
        recent = await strk.compute_source_records(conn, now=NOW, lag_hours=72.0)
        # Same data, lag shrunk below its age -> now it counts.
        settled = await strk.compute_source_records(conn, now=NOW, lag_hours=0.5)

    assert recent == []                       # nothing settled yet
    assert {r.source_id for r in settled} == {"source.winner", "source.loser"}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_acyclicity_exclusion_drops_the_current_contention(pg_pool, clean):
    """Guard (c): the contention being decided is excluded from its own carriers'
    records — a contention's outcome can never feed its own re-decision."""
    async with pg_pool.acquire() as conn:
        sw = await _signal(conn, "source.winner")
        sl = await _signal(conn, "source.loser")
        w = await _fact(conn, "Syria", "govt control", "consolidating", [sw])
        l = await _fact(conn, "Syria", "govt control", "fragmenting", [sl])
        cid = await _resolved_contention(
            conn,
            subject_key="syria", predicate_key="govt control",
            winner_value="consolidating", winner_facts=[w], winner_dsc=1,
            loser_value="fragmenting", loser_facts=[l], loser_dsc=1,
            surfaced_at=NOW - timedelta(hours=100),
        )
        included = await strk.compute_source_records(conn, now=NOW, lag_hours=72.0)
        excluded = await strk.compute_source_records(
            conn, now=NOW, lag_hours=72.0, exclude_contention=cid,
        )
        # The arbiter helper, excluding the same contention, yields no bonus.
        weights = await strk.earned_weights_for_sources(
            conn, ["source.winner"], now=NOW, exclude_contention=cid, lag_hours=72.0,
        )

    assert {r.source_id for r in included} == {"source.winner", "source.loser"}
    assert excluded == []                     # the only contention was excluded
    assert weights == {}                      # -> no earned signal to feed back


@pytest.mark.integration
@pytest.mark.asyncio
async def test_store_round_trip_and_wholesale_prune(pg_pool, clean):
    async with pg_pool.acquire() as conn:
        sw = await _signal(conn, "source.a")
        sl = await _signal(conn, "source.b")
        w = await _fact(conn, "X", "p", "yes", [sw])
        l = await _fact(conn, "X", "p", "no", [sl])
        await _resolved_contention(
            conn, subject_key="x", predicate_key="p",
            winner_value="yes", winner_facts=[w], winner_dsc=1,
            loser_value="no", loser_facts=[l], loser_dsc=1,
            surfaced_at=NOW - timedelta(hours=100),
        )
        records = await strk.compute_source_records(conn, now=NOW, lag_hours=72.0)
        await strk.store_source_records(conn, records)
        stored = await conn.fetch(
            "SELECT source_id, wins, losses, contested_total, win_rate_smoothed, "
            "low_sample FROM source_track_records ORDER BY source_id",
        )
        assert {r["source_id"] for r in stored} == {"source.a", "source.b"}
        a = next(r for r in stored if r["source_id"] == "source.a")
        assert a["wins"] == 1 and a["losses"] == 0 and a["contested_total"] == 1
        assert a["low_sample"] is True   # 1 < floor
        assert a["win_rate_smoothed"] == pytest.approx((1 + 2) / (1 + 4), abs=1e-6)

        # Wholesale refresh with an EMPTY set prunes every stale row.
        await strk.store_source_records(conn, [])
        remaining = await conn.fetchval("SELECT count(*) FROM source_track_records")
    assert remaining == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_handle_refreshes_table_and_returns_finding(pg_pool, clean):
    async with pg_pool.acquire() as conn:
        sw = await _signal(conn, "source.win")
        sl = await _signal(conn, "source.lose")
        w = await _fact(conn, "Y", "q", "up", [sw])
        l = await _fact(conn, "Y", "q", "down", [sl])
        await _resolved_contention(
            conn, subject_key="y", predicate_key="q",
            winner_value="up", winner_facts=[w], winner_dsc=1,
            loser_value="down", loser_facts=[l], loser_dsc=1,
            surfaced_at=NOW - timedelta(hours=100),
        )

    deps = SimpleNamespace(pg_pool=pg_pool)
    result = await strk.handle([], {"sub_handler": "source_track_record"}, deps)
    assert result.finding is not None
    assert result.finding.confidence == 1.0
    assert result.usage["prompt_tokens"] == 0
    assert result.finding.data["contested_sources"] == 2

    async with pg_pool.acquire() as conn:
        n = await conn.fetchval("SELECT count(*) FROM source_track_records")
    assert n == 2


@pytest.mark.integration
@pytest.mark.asyncio
async def test_handle_refuses_without_pool():
    with pytest.raises(RuntimeError, match="requires a live deps.pg_pool"):
        await strk.handle([], {"sub_handler": "source_track_record"}, SimpleNamespace())


# ---------------------------------------------------------------------------
# Part B — the route + projection surfaces.
# ---------------------------------------------------------------------------


def _fixed_identity() -> SigningIdentity:
    seed = b"p3-3-earned-record-test-seedXXXX"[:32]
    return SigningIdentity(
        signing_key=SigningKey(seed),
        signer_did="did:legba:registry:p3-3-earned-test",
    )


def _source_body(descriptor_id: str) -> dict[str, Any]:
    return {
        "identity": {
            "id": descriptor_id,
            "name": "Earned Test Wire",
            "kind": "rss",
            "schema_uri": "legba/source/1.0.0",
            "version": "0" * 16,
            "abstraction_level": "L1",
            "state": "draft",
            "owner": "lewis@local",
            "created": "2026-07-24T10:00:00+00:00",
        },
        "scope": {
            "owner_tenant": "default", "geo": ["US"],
            "languages": ["en"], "tags": ["news"],
        },
        "acquisition": "poll",
        "cadence": {"schedule": {"raw": "*/15 * * * *"}},
        "subscription_policy": "open",
        "output": {"delivery": "lossy"},
    }


@pytest_asyncio.fixture
async def api_app(migrated_pg: PostgresConfig):
    os.environ.pop(API_TOKEN_ENV, None)
    pg_store = PostgresStore(migrated_pg)
    await pg_store.connect()

    identity = _fixed_identity()
    audit = AuditLogger(identity=identity)
    dlq = DescriptorDeadLetter(pg_store)
    vocab = VocabularyCache(pg_store)
    vault = CredentialVault(pg_store)
    descriptor_registry = DescriptorRegistry(
        pg_store, vocabulary_cache=vocab, signing_identity=identity,
        audit_logger=audit, dead_letter=dlq,
    )
    await descriptor_registry.start()
    stack_registry = StackRegistry(pg_store, vault, audit=audit, dlq=dlq)
    deps = RegistryAPIDeps(
        descriptor_registry=descriptor_registry, stack_registry=stack_registry,
        vault=vault, dlq=dlq, audit_logger=audit, vocabulary_cache=vocab,
        nats_store=None,
    )
    app = FastAPI()
    app.state.registry_deps = deps
    app.include_router(build_router(deps), prefix="/api/v1/registry")
    app.include_router(build_source_assurance_router(deps), prefix="/api/v1/v3")
    yield app, pg_store
    await descriptor_registry.stop()
    await pg_store.close()


@pytest_asyncio.fixture
async def client(api_app):
    app, _ = api_app
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver",
    ) as c:
        yield c


@pytest.mark.integration
@pytest.mark.asyncio
async def test_assurance_route_earned_section(api_app, client):
    _, pg_store = api_app
    source_id = f"source.rss.earned_{uuid4().hex[:8]}"
    async with pg_store.acquire() as conn:
        # store_source_records prunes to only the passed set, so insert directly
        # to avoid nuking other tests' rows on the shared session DB.
        rec = strk.SourceRecord(
            source_id=source_id, wins=8, losses=2, corroborated=6,
            corroboration_total=10, lag_hours=72.0,
            sample_as_of=NOW - timedelta(hours=72), computed_at=NOW,
        )
        await conn.execute(
            """
            INSERT INTO source_track_records
                (source_id, wins, losses, contested_total, win_rate_raw,
                 win_rate_smoothed, win_rate_lower, low_sample, corroborated,
                 corroboration_total, corroboration_rate, lag_hours,
                 sample_as_of, computed_at)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)
            ON CONFLICT (source_id) DO NOTHING
            """,
            source_id, rec.wins, rec.losses, rec.contested_total, rec.win_rate_raw,
            rec.win_rate_smoothed, rec.win_rate_lower, rec.low_sample,
            rec.corroborated, rec.corroboration_total, rec.corroboration_rate,
            rec.lag_hours, rec.sample_as_of, rec.computed_at,
        )

    r = await client.get(f"/api/v1/v3/sources/{source_id}/assurance")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["source_id"] == source_id
    earned = body["earned"]
    assert earned is not None
    assert earned["wins"] == 8 and earned["losses"] == 2
    assert earned["contested_total"] == 10
    assert earned["win_rate_raw"] == pytest.approx(0.8)
    assert earned["win_rate_smoothed"] == pytest.approx((8 + 2) / (10 + 4))
    assert earned["low_sample"] is False
    assert earned["corroboration_rate"] == pytest.approx(0.6)
    assert earned["lag_hours"] == pytest.approx(72.0)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_source_list_projection_earned_win_rate(api_app, client):
    _, pg_store = api_app
    desc_id = f"source.rss.earned_{uuid4().hex[:8]}"
    r = await client.post(
        "/api/v1/registry/descriptors/source", json=_source_body(desc_id),
    )
    assert r.status_code == 201, r.text

    def _row(payload: list[dict[str, Any]]) -> dict[str, Any]:
        m = [x for x in payload if x["descriptor_id"] == desc_id]
        assert len(m) == 1
        return m[0]

    # Ungraded/unmeasured: the additive field is present and null.
    r = await client.get("/api/v1/registry/sources")
    assert r.status_code == 200, r.text
    row = _row(r.json())
    assert row["earned_win_rate"] is None
    assert row["assurance_grade"] is None       # sibling projection intact

    # A measured record surfaces as the smoothed rate.
    async with pg_store.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO source_track_records
                (source_id, wins, losses, contested_total, win_rate_smoothed,
                 win_rate_lower, low_sample)
            VALUES ($1, 9, 1, 10, $2, 0.6, false)
            ON CONFLICT (source_id) DO NOTHING
            """,
            desc_id, (9 + 2) / (10 + 4),
        )
    r = await client.get("/api/v1/registry/sources")
    assert _row(r.json())["earned_win_rate"] == pytest.approx((9 + 2) / (10 + 4))

    # Detail view carries the same stamp.
    r = await client.get(f"/api/v1/registry/sources/{desc_id}")
    assert r.status_code == 200, r.text
    assert r.json()["earned_win_rate"] == pytest.approx((9 + 2) / (10 + 4))


@pytest.mark.integration
@pytest.mark.asyncio
async def test_loaders_degrade_when_records_absent(api_app):
    """A source with no record yields null (loader helpers return empty/None)."""
    _, pg_store = api_app
    missing = f"source.rss.none_{uuid4().hex[:8]}"
    assert await load_earned_record(pg_store, missing) is None
    assert await load_earned_win_rates(pg_store, [missing]) == {}
