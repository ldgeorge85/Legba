# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""P4-1 + P4-2 — the ``narrative_mapper`` analyst, real-Postgres integration.

Drives the mapper against a MIGRATED ephemeral DB (the ``migrated_pg`` fixture
applies migration 0102) to prove end to end:

  * reification over the LIVE lineage — a contested `fact_contention` group ->
    a `narratives` row with its carrier sources, the publish-dated lead, and the
    per-source echo lags (via supporting_fact_ids -> facts.derived_from ->
    signals.source_id, publish time at signals.payload->>'published_at');
  * the directed source-echo graph — A leads B within the window across two
    narratives -> a systematic `narrative_echo_edges` A->B row;
  * the publish-dated-only honesty rule — a narrative whose signals carry NO
    published_at yields no lead and no echo edge (fetch order is not publish order);
  * the NEVER-MUTATE-FACTS invariant — facts / fact_contention /
    fact_contention_values are byte-for-byte unchanged across a `handle` run;
  * wholesale refresh — idempotent re-run (no dup rows) + prune (a family that
    disappears leaves no stale narrative / edge);
  * the /api/v1/v3/narratives read route (list + echo + detail).
"""
from __future__ import annotations

import json
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

from legba.data.analysts.deterministic_handlers import narrative_mapper as nm
from legba.data.config import PostgresConfig
from legba.data.postgres import PostgresStore
from legba.data.registry.narratives_api import build_narratives_router

NOW = datetime(2026, 7, 24, 12, tzinfo=timezone.utc)
T0 = datetime(2026, 7, 1, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Substrate-seeding helpers.
# ---------------------------------------------------------------------------


async def _source(conn: Any, source_id: str, name: str) -> None:
    await conn.execute(
        """
        INSERT INTO source_descriptors
            (descriptor_id, version, schema_uri, is_head, kind, state, owner, name, body)
        VALUES ($1, 'v1', 'legba/source/1.0.0', true, 'rss', 'active', 'test', $2, '{}'::jsonb)
        ON CONFLICT DO NOTHING
        """,
        source_id, name,
    )


async def _signal(conn: Any, source_id: str, *, published_at: str | None,
                  fetched_at: datetime = NOW) -> UUID:
    sid = uuid4()
    payload = json.dumps({"published_at": published_at} if published_at else {})
    await conn.execute(
        "INSERT INTO signals (id, source_id, payload, fetched_at) "
        "VALUES ($1, $2, $3::jsonb, $4)",
        sid, source_id, payload, fetched_at,
    )
    return sid


async def _fact(conn: Any, subject: str, predicate: str, value: str,
                signal_ids: list[UUID], *, seq: int = 0) -> UUID:
    fid = uuid4()
    await conn.execute(
        """
        INSERT INTO facts (id, subject, predicate, value, confidence,
                           valid_from, derived_from)
        VALUES ($1, $2, $3, $4, 0.7, $5, $6::uuid[])
        """,
        fid, subject, predicate, value, NOW - timedelta(minutes=seq), signal_ids,
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


async def _contention(conn: Any, *, subject_key: str, predicate_key: str,
                      status: str = "contested", surfaced_value: str | None = None,
                      surfaced_fact_id: UUID | None = None,
                      surfaced_at: datetime | None = None,
                      opened_at: datetime = T0) -> UUID:
    cid = uuid4()
    await conn.execute(
        """
        INSERT INTO fact_contention
            (id, subject_key, predicate_key, status, surfaced_value,
             surfaced_fact_id, surfaced_at, opened_at, value_count)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, 2)
        """,
        cid, subject_key, predicate_key, status, surfaced_value,
        surfaced_fact_id, surfaced_at, opened_at,
    )
    return cid


async def _seed_ab_narrative(conn: Any, subject: str, base: datetime) -> UUID:
    """A narrative with A leading (publish-dated ``base``), B echoing (+2h,
    publish-dated), and a THIRD carrier C that is fetch-only (no published_at,
    fetched +50h) — a distinct carrier that is deliberately excluded from the
    echo graph, so the only publish-dated echo edge is A->B."""
    sa = await _signal(conn, "src.A", published_at=base.isoformat())
    sb = await _signal(conn, "src.B", published_at=(base + timedelta(hours=2)).isoformat())
    sc = await _signal(conn, "src.C", published_at=None,
                       fetched_at=base + timedelta(hours=50))
    wa = await _fact(conn, subject, "status", "holding", [sa], seq=0)
    wb = await _fact(conn, subject, "status", "holding", [sb], seq=1)
    lc = await _fact(conn, subject, "status", "collapsed", [sc], seq=2)
    cid = await _contention(conn, subject_key=subject, predicate_key="status")
    await _fcv(conn, cid, "holding", [wa, wb], winner=False, dsc=2)
    await _fcv(conn, cid, "collapsed", [lc], winner=False, dsc=1)
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
        await conn.execute("DELETE FROM narrative_echo_edges")
        await conn.execute("DELETE FROM narratives")
        await conn.execute("DELETE FROM fact_contention_values")
        await conn.execute("DELETE FROM fact_contention")
        await conn.execute("DELETE FROM facts")
        await conn.execute("DELETE FROM signals")
    yield


_OPTS = {
    "sub_handler": "narrative_mapper",
    "echo_window_hours": 48,
    "min_co_carriage": 2,
    "systematic_floor": 2,
    "echo_ratio_floor": 0.6,
}


# ---------------------------------------------------------------------------
# Part A — reification + echo graph over live SQL.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reify_and_echo_graph_end_to_end(pg_pool, clean):
    async with pg_pool.acquire() as conn:
        await _source(conn, "src.A", "Alpha Wire")
        await _source(conn, "src.B", "Bravo Wire")
        c1 = await _seed_ab_narrative(conn, "gaza", T0)
        c2 = await _seed_ab_narrative(conn, "syria", T0 + timedelta(days=1))

    deps = SimpleNamespace(pg_pool=pg_pool)
    result = await nm.handle([], _OPTS, deps)
    assert result.finding.confidence == 1.0
    assert result.usage["prompt_tokens"] == 0
    assert result.finding.data["narratives_total"] == 2

    async with pg_pool.acquire() as conn:
        nars = await conn.fetch(
            "SELECT contention_id, subject_key, carrier_source_count, "
            "publish_dated_source_count, variant_count, lead_source_id, "
            "lead_first_seen_at, span_hours, carriers "
            "FROM narratives ORDER BY subject_key"
        )
        edges = await conn.fetch(
            "SELECT leader_source_id, follower_source_id, co_carried, lead_count, "
            "follow_within_count, echo_ratio, median_lag_hours, systematic "
            "FROM narrative_echo_edges ORDER BY leader_source_id, follower_source_id"
        )

    assert {r["contention_id"] for r in nars} == {c1, c2}
    gaza = next(r for r in nars if r["subject_key"] == "gaza")
    assert gaza["carrier_source_count"] == 3       # A, B, and the fetch-only C
    assert gaza["publish_dated_source_count"] == 2  # only A and B carry publish times
    assert gaza["variant_count"] == 2
    assert gaza["lead_source_id"] == "src.A"
    assert gaza["lead_first_seen_at"] == T0
    assert gaza["span_hours"] == pytest.approx(50.0)
    carriers = {c["source_id"]: c for c in json.loads(gaza["carriers"])}
    assert carriers["src.A"]["role"] == "lead"
    assert carriers["src.A"]["source_name"] == "Alpha Wire"
    assert carriers["src.B"]["echo_lag_hours"] == pytest.approx(2.0)

    # The echo graph: A -> B systematic (led B within 48h in both narratives).
    by = {(e["leader_source_id"], e["follower_source_id"]): e for e in edges}
    ab = by[("src.A", "src.B")]
    assert ab["co_carried"] == 2 and ab["lead_count"] == 2
    assert ab["follow_within_count"] == 2
    assert ab["echo_ratio"] == pytest.approx(1.0)
    assert ab["median_lag_hours"] == pytest.approx(2.0)
    assert ab["systematic"] is True
    # B never led A -> no reverse edge.
    assert ("src.B", "src.A") not in by


@pytest.mark.integration
@pytest.mark.asyncio
async def test_fetch_only_carriage_no_lead_no_edge(pg_pool, clean):
    """Signals with NO published_at -> the narrative is reified (fetched_at
    dates it) but yields no publish-dated lead and NO echo edge."""
    async with pg_pool.acquire() as conn:
        sa = await _signal(conn, "src.A", published_at=None, fetched_at=T0)
        sb = await _signal(conn, "src.B", published_at=None,
                           fetched_at=T0 + timedelta(hours=3))
        fa = await _fact(conn, "libya", "status", "up", [sa])
        fb = await _fact(conn, "libya", "status", "down", [sb])
        cid = await _contention(conn, subject_key="libya", predicate_key="status")
        await _fcv(conn, cid, "up", [fa], winner=False, dsc=1)
        await _fcv(conn, cid, "down", [fb], winner=False, dsc=1)

    deps = SimpleNamespace(pg_pool=pg_pool)
    await nm.handle([], _OPTS, deps)

    async with pg_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT carrier_source_count, publish_dated_source_count, "
            "lead_source_id, first_seen_at FROM narratives WHERE contention_id = $1",
            cid,
        )
        edge_count = await conn.fetchval("SELECT count(*) FROM narrative_echo_edges")

    assert row["carrier_source_count"] == 2
    assert row["publish_dated_source_count"] == 0
    assert row["lead_source_id"] is None
    assert row["first_seen_at"] == T0        # datable via fetched_at
    assert edge_count == 0                    # fetch order is not publish order


# ---------------------------------------------------------------------------
# Part B — the never-mutate-facts invariant.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_never_mutates_facts_or_contention(pg_pool, clean):
    """DETECT-ONLY: facts / fact_contention / fact_contention_values are
    unchanged across a mapper run (it reads them, writes only the sidecars)."""
    async with pg_pool.acquire() as conn:
        await _seed_ab_narrative(conn, "gaza", T0)

    async def _snapshot(conn) -> dict[str, Any]:
        return {
            "facts": await conn.fetch(
                "SELECT id, subject, predicate, value, confidence, contested, "
                "contention_id, surfaced_winner, valid_until, superseded_by "
                "FROM facts ORDER BY id"
            ),
            "contention": await conn.fetch(
                "SELECT id, status, surfaced_value, surfaced_fact_id, value_count "
                "FROM fact_contention ORDER BY id"
            ),
            "values": await conn.fetch(
                "SELECT id, contention_id, value_key, surfaced_winner, is_junk "
                "FROM fact_contention_values ORDER BY id"
            ),
        }

    async with pg_pool.acquire() as conn:
        before = await _snapshot(conn)

    await nm.handle([], _OPTS, SimpleNamespace(pg_pool=pg_pool))

    async with pg_pool.acquire() as conn:
        after = await _snapshot(conn)

    for key in ("facts", "contention", "values"):
        assert [dict(r) for r in before[key]] == [dict(r) for r in after[key]], key
    # And the sidecars WERE written (the run did do its real work).
    async with pg_pool.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM narratives") == 1


# ---------------------------------------------------------------------------
# Part C — wholesale refresh: idempotent + prune.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_idempotent_refresh_and_prune(pg_pool, clean):
    async with pg_pool.acquire() as conn:
        c1 = await _seed_ab_narrative(conn, "gaza", T0)
        c2 = await _seed_ab_narrative(conn, "syria", T0 + timedelta(days=1))

    deps = SimpleNamespace(pg_pool=pg_pool)
    await nm.handle([], _OPTS, deps)
    await nm.handle([], _OPTS, deps)  # second run must not duplicate

    async with pg_pool.acquire() as conn:
        n1 = await conn.fetchval("SELECT count(*) FROM narratives")
        e1 = await conn.fetchval("SELECT count(*) FROM narrative_echo_edges")
    assert n1 == 2                         # no dup rows (PK on contention_id)
    assert e1 == 1                         # the single A->B edge, not duplicated

    # c2's family disappears -> the next refresh prunes its narrative, and the
    # echo edge (now backed by a single narrative < floor) prunes too.
    async with pg_pool.acquire() as conn:
        await conn.execute("DELETE FROM fact_contention WHERE id = $1", c2)
    await nm.handle([], _OPTS, deps)

    async with pg_pool.acquire() as conn:
        remaining = await conn.fetch("SELECT contention_id FROM narratives")
        edges_after = await conn.fetchval("SELECT count(*) FROM narrative_echo_edges")
    assert [r["contention_id"] for r in remaining] == [c1]
    assert edges_after == 0                 # A->B now below the co-carriage floor


# ---------------------------------------------------------------------------
# Part D — the read route.
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def route_client(migrated_pg: PostgresConfig, pg_pool, clean):
    os.environ.pop("LEGBA_REGISTRY_API_TOKEN", None)
    os.environ["LEGBA_DEV_MODE"] = "1"
    pg_store = PostgresStore(migrated_pg)
    await pg_store.connect()
    deps = SimpleNamespace(descriptor_registry=SimpleNamespace(pg=pg_store))
    app = FastAPI()
    app.state.registry_deps = deps
    app.include_router(build_narratives_router(deps), prefix="/api/v1/v3")
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver",
    ) as c:
        yield c, pg_pool
    await pg_store.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_narratives_route(route_client):
    client, pg_pool = route_client
    async with pg_pool.acquire() as conn:
        await _source(conn, "src.A", "Alpha Wire")
        await _source(conn, "src.B", "Bravo Wire")
        c1 = await _seed_ab_narrative(conn, "gaza", T0)
        await _seed_ab_narrative(conn, "syria", T0 + timedelta(days=1))
    await nm.handle([], _OPTS, SimpleNamespace(pg_pool=pg_pool))

    # List.
    r = await client.get("/api/v1/v3/narratives")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["count"] == 2
    assert "not a causal or coordination claim" in body["honesty_note"].lower()
    subjects = {n["subject_key"] for n in body["narratives"]}
    assert subjects == {"gaza", "syria"}

    # Echo graph, systematic only.
    r = await client.get("/api/v1/v3/narratives/echo?systematic_only=1")
    assert r.status_code == 200, r.text
    edges = r.json()["edges"]
    assert len(edges) == 1
    assert edges[0]["leader_source_id"] == "src.A"
    assert edges[0]["follower_source_id"] == "src.B"
    assert edges[0]["systematic"] is True

    # Single narrative detail.
    r = await client.get(f"/api/v1/v3/narratives/{c1}")
    assert r.status_code == 200, r.text
    detail = r.json()
    assert detail["subject_key"] == "gaza"
    assert detail["lead_source_id"] == "src.A"
    assert any(c["role"] == "lead" for c in detail["carriers"])

    # Unknown id -> 404.
    r = await client.get(f"/api/v1/v3/narratives/{uuid4()}")
    assert r.status_code == 404
