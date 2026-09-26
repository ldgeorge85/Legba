# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the V3/P4a graph-engine trigger gauges.

Covers :mod:`legba.data.registry.graph_triggers_api`
(``GET /api/v1/v3/graph-triggers``) — the five pre-registered triggers from
JUDGE_SYNTHESIS §4.2 that the 2026-11-03 sitting reads — plus the
``duration_ms`` stamp that migration 0208 adds to ``action_pack_invocations``
and ``agency.run_pack_tool`` writes at settle.

Two layers, per the house v3-route pattern:

  * PURE tests (no DB): route registration, the threshold constants pinned to
    the judge's table, and each gauge builder's measured / unreadable /
    declared behaviour — including the load-bearing honesty rule that an
    unreadable gauge reports ``reading=None``, never 0.
  * INTEGRATION tests over the ephemeral ``migrated_pg`` database + real HTTP
    (the ``test_v3_since_api`` fixture shape — through the router factory and
    the deps bundle, not the handler function directly), plus the settle-path
    stamp exercised through the REAL binding path
    (``Agency.run_pack_tool`` → governor settle → the ledger row).

Auth: tests run in dev-mode (``LEGBA_DEV_MODE=1`` from tests/conftest.py, no
``LEGBA_REGISTRY_API_TOKEN``), so ``require_bearer`` returns ``"anonymous"``
and unauthenticated requests pass — the same bearer path as the rest of the
v3 surface.

The deps bundle is a ``SimpleNamespace`` carrying only ``descriptor_registry.pg``
because that is the whole of what this route touches; building a real
``DescriptorRegistry`` would pull in a NATS connect the gauge never uses.
"""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from legba.data.config import PostgresConfig
from legba.data.postgres import PostgresStore
from legba.data.registry.api import API_TOKEN_ENV
from legba.data.registry.graph_triggers_api import (
    DECISION_RULE,
    E1_OPEN_RELATION_EDGES,
    E2_MIN_INVOCATIONS,
    E2_P95_MS,
    E2_WINDOW_DAYS,
    E3_INVOCATIONS_PER_DAY,
    E3_SHAPE_SHARE,
    E3_WINDOW_DAYS,
    E4_REBUILD_SECONDS,
    GRAPH_TOOLS,
    SITTING_DATE,
    WALK_TOOLS,
    build_graph_triggers_router,
    gauge_e1,
    gauge_e2,
    gauge_e3,
    gauge_e4,
    gauge_e5,
)

_SRC = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..", "src"
)


# ---------------------------------------------------------------------------
# Pure tests — registration, threshold pins, gauge builders (no DB)
# ---------------------------------------------------------------------------


def test_route_registered_and_no_v3_collision() -> None:
    """The route registers under /api/v1/v3 and shadows nothing."""
    router = build_graph_triggers_router(deps=object())  # type: ignore[arg-type]
    paths = {r.path for r in router.routes}  # type: ignore[attr-defined]
    assert "/graph-triggers" in paths

    from legba.data.registry.v3_api import build_v3_router

    v3_paths = {
        r.path
        for r in build_v3_router(deps=object()).routes  # type: ignore[arg-type]
    }
    assert not (paths & v3_paths)


def test_thresholds_match_judge_synthesis() -> None:
    """The constants are the judge's §4.2 table, verbatim — a gauge that reads
    against a different bar than the sitting expects is worse than no gauge."""
    assert E1_OPEN_RELATION_EDGES == 250_000
    assert E2_P95_MS == 2_000
    assert E2_MIN_INVOCATIONS == 100
    assert E2_WINDOW_DAYS == 30
    assert E3_INVOCATIONS_PER_DAY == 20.0
    assert E3_WINDOW_DAYS == 14
    assert E3_SHAPE_SHARE == 0.20
    assert E4_REBUILD_SECONDS == 60.0
    assert SITTING_DATE == "2026-11-03"
    assert "TWO" in DECISION_RULE
    # E3's numerator is the deep-walk subset of the graph-tool set.
    assert set(WALK_TOOLS) <= set(GRAPH_TOOLS)
    assert set(WALK_TOOLS) == {
        "query_paths", "find_proxy_chains", "query_brokers",
    }


def test_e1_pure() -> None:
    g = gauge_e1(16_860)
    assert g.id == "E1" and g.state == "measured"
    assert g.reading == 16_860 and g.reading_unit == "edges"
    assert g.fired is False and "250,000" in g.threshold
    assert gauge_e1(250_001).fired is True


def test_e2_unreadable_below_100_never_zero() -> None:
    """n < 100 timed invocations → unreadable WITH the n, reading=None."""
    g = gauge_e2({"n_timed": 6, "n_total": 9, "p95_ms": 41.0}, window_days=30)
    assert g.state == "unreadable"
    assert g.reading is None                       # never a 0 reading
    assert g.fired is None
    assert "insufficient invocations" in (g.unreadable_reason or "")
    assert "n=6" in (g.unreadable_reason or "")


def test_e2_measured_at_threshold() -> None:
    stats = {"n_timed": 120, "n_total": 130, "p95_ms": 426.0, "max_ms": 900}
    g = gauge_e2(stats, window_days=30)
    assert g.state == "measured" and g.reading == 426.0
    assert g.fired is False
    assert gauge_e2(
        {"n_timed": 100, "n_total": 100, "p95_ms": 2_001.0, "max_ms": 3_000},
        window_days=30,
    ).fired is True


def test_e3_demand_leg_and_unreadable_shape_leg() -> None:
    # 6 lifetime-style invocations over the 14d window → single-digit rate,
    # demand leg under threshold → cannot fire regardless of the dark shape leg.
    g = gauge_e3({"query_paths": 4, "find_proxy_chains": 1,
                  "query_brokers": 1}, window_days=14)
    assert g.state == "measured"
    assert g.reading == pytest.approx(6 / 14)
    assert g.fired is False
    shape = g.evidence["shape_share"]
    assert shape["state"] == "unreadable"
    assert shape["reason"] == "no shape classifier"

    # If demand ever crosses with the shape leg still dark, the AND cannot
    # be evaluated — the verdict is honestly null, not a guess.
    hot = gauge_e3({"query_paths": 400}, window_days=14)
    assert hot.fired is None


def test_e4_projector_not_deployed_until_a_build_row() -> None:
    missing = gauge_e4(None, table_present=False)
    assert missing.state == "unreadable"
    assert missing.unreadable_reason == "projector not deployed"
    assert missing.reading is None
    # Table exists but no build has run — still unreadable, same reason.
    empty = gauge_e4(None, table_present=True)
    assert empty.state == "unreadable"
    # A real receipt → measured.
    built = gauge_e4(
        {"build_seconds": 31.7, "arc_count": 4_000_000,
         "projected_at": datetime(2026, 9, 22, tzinfo=timezone.utc)},
        table_present=True,
    )
    assert built.state == "measured" and built.reading == 31.7
    assert built.fired is False
    assert gauge_e4(
        {"build_seconds": 61.0, "arc_count": 8_000_000, "projected_at": None},
        table_present=True,
    ).fired is True


def test_e5_is_a_position_not_a_number() -> None:
    g = gauge_e5()
    assert g.id == "E5" and g.state == "declared"
    assert g.reading is None and g.fired is None
    assert g.position and "interactive" in g.position


# ---------------------------------------------------------------------------
# Integration — migrated scratch DB + real HTTP through the router factory
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def pg_store(migrated_pg: PostgresConfig):
    store = PostgresStore(migrated_pg)
    await store.connect()
    yield store
    await store.close()


@pytest_asyncio.fixture
async def triggers_app(pg_store: PostgresStore):
    """A FastAPI app exposing only the triggers router, deps carrying the real
    pg pool — the real binding path minus the NATS-backed registries this
    read-only gauge never touches."""
    os.environ.pop(API_TOKEN_ENV, None)  # dev mode — any bearer accepted
    deps = SimpleNamespace(
        descriptor_registry=SimpleNamespace(pg=pg_store),
    )
    app = FastAPI()
    app.state.registry_deps = deps
    app.include_router(build_graph_triggers_router(deps), prefix="/api/v1/v3")
    yield app


@pytest_asyncio.fixture
async def client(triggers_app):
    async with AsyncClient(
        transport=ASGITransport(app=triggers_app), base_url="http://testserver",
    ) as c:
        yield c


async def _entity(conn, name: str) -> object:
    return await conn.fetchval(
        """INSERT INTO entity_profiles (canonical_name, entity_class,
             entity_type, data) VALUES ($1, 'organization', 'organization',
             '{}'::jsonb) RETURNING id""",
        name,
    )


async def _edge(conn, src, dst, *, family="relation", open_: bool = True) -> None:
    await conn.execute(
        """INSERT INTO entity_edges
             (src_id, dst_id, edge_type, edge_family, polarity, confidence,
              valid_until)
           VALUES ($1, $2, 'supports', $3, 1, 0.9, $4)""",
        src, dst, family, None if open_ else datetime.now(timezone.utc),
    )


async def _invocation(
    conn, tool: str, *, duration_ms: int | None, minutes_ago: int = 5,
    pack_id: str = "substrate_read",
) -> None:
    await conn.execute(
        """INSERT INTO action_pack_invocations
             (pack_id, pack_version, tool_name, budget_account, requested_by,
              tenant_id, cost_usd, units, outcome, occurred_at, duration_ms)
           VALUES ($1,'v',$2,'acct','test','default',0,1,'completed',
                   now() - make_interval(mins => $3), $4)""",
        pack_id, tool, minutes_ago, duration_ms,
    )


def _gauge(payload: dict, gid: str) -> dict:
    return next(t for t in payload["triggers"] if t["id"] == gid)


@pytest.mark.asyncio
async def test_graph_triggers_end_to_end(client, pg_store, clean_tables):
    """The whole route over a seeded substrate: E1 reads real edges, E2/E4
    are honestly unreadable, E3 reports its demand leg, E5 its position."""
    await clean_tables("action_pack_invocations", "entity_edges",
                       "graph_arcs_meta")
    async with pg_store.acquire() as conn:
        a = await _entity(conn, "GT EntA")
        b = await _entity(conn, "GT EntB")
        c = await _entity(conn, "GT EntC")
        await _edge(conn, a, b)                                  # open relation
        await _edge(conn, a, c, open_=False)                     # closed — not counted
        await _edge(conn, b, c, family="cooccurrence")           # open, wrong family
        for _ in range(3):
            await _invocation(conn, "query_paths", duration_ms=120)
        await _invocation(conn, "query_brokers", duration_ms=80)

    resp = await client.get("/api/v1/v3/graph-triggers")
    assert resp.status_code == 200
    body = resp.json()
    assert body["measured"] is True
    assert body["sitting"] == "2026-11-03"
    assert {t["id"] for t in body["triggers"]} == {"E1", "E2", "E3", "E4", "E5"}

    e1 = _gauge(body, "E1")
    assert e1["state"] == "measured" and e1["reading"] == 1  # open+relation only
    assert e1["fired"] is False

    e2 = _gauge(body, "E2")
    assert e2["state"] == "unreadable"
    assert e2["reading"] is None
    assert "insufficient invocations" in e2["unreadable_reason"]
    assert "n=4" in e2["unreadable_reason"]

    e3 = _gauge(body, "E3")
    assert e3["state"] == "measured"
    assert e3["reading"] == pytest.approx(4 / 14)
    assert e3["evidence"]["tools"] == {"query_paths": 3, "query_brokers": 1}
    assert e3["evidence"]["shape_share"]["state"] == "unreadable"

    e4 = _gauge(body, "E4")
    assert e4["state"] == "unreadable"
    assert e4["unreadable_reason"] == "projector not deployed"
    assert e4["reading"] is None

    e5 = _gauge(body, "E5")
    assert e5["state"] == "declared"
    assert e5["reading"] is None and e5["position"]


@pytest.mark.asyncio
async def test_e2_measured_once_100_timed_invocations_exist(
    client, pg_store, clean_tables
):
    """At >=100 timed rows the p95 computes over duration_ms only — the
    pre-0208 NULL rows count in n_total but never in the percentile."""
    await clean_tables("action_pack_invocations")
    async with pg_store.acquire() as conn:
        for _ in range(120):
            await _invocation(conn, "query_paths", duration_ms=3_000)
        # Pre-0208 shape: admitted/complete rows with no stamp — excluded from n.
        for _ in range(10):
            await _invocation(conn, "inspect_entity", duration_ms=None)
        # A non-graph tool's latency never enters the sample.
        await _invocation(conn, "search_corpus", duration_ms=9_999)

    body = (await client.get("/api/v1/v3/graph-triggers")).json()
    e2 = _gauge(body, "E2")
    assert e2["state"] == "measured"
    assert e2["reading"] == 3_000
    assert e2["fired"] is True                    # 3,000 > the 2,000 ms bar
    assert e2["evidence"]["invocations_timed"] == 120
    assert e2["evidence"]["invocations_total"] == 130


@pytest.mark.asyncio
async def test_e4_reads_the_projector_receipt_when_it_exists(
    client, pg_store, clean_tables
):
    """The unreadable→measured flip is driven by graph_arcs_meta: the route
    must read the receipt the P4b projector writes. Since 0207 the table is
    real — seed the singleton row, never drop the table (a DROP on the shared
    session DB would poison every later test that reads it)."""
    await clean_tables("action_pack_invocations", "graph_arcs_meta")
    async with pg_store.acquire() as conn:
        await conn.execute(
            """INSERT INTO public.graph_arcs_meta
                 (projected_at, arc_count, build_seconds)
               VALUES (now(), 4000000, 31.7)
               ON CONFLICT (id) DO UPDATE SET build_seconds = 31.7"""
        )
    try:
        body = (await client.get("/api/v1/v3/graph-triggers")).json()
        e4 = _gauge(body, "E4")
        assert e4["state"] == "measured"
        assert e4["reading"] == pytest.approx(31.7)
        assert e4["fired"] is False
        assert e4["evidence"]["arc_count"] == 4_000_000
    finally:
        # Leave the receipt table EMPTY for whoever runs next — the shared
        # session DB makes teardown-visible state a cross-file hazard.
        await clean_tables("graph_arcs_meta")


@pytest.mark.asyncio
async def test_substrate_unreachable_is_honest_empty(triggers_app, monkeypatch):
    """A substrate read failure returns 200 with measured=false — the polled
    surface never hands a panel a 500 to hammer."""
    class _DeadAcquire:
        async def __aenter__(self):
            raise RuntimeError("pg down")
        async def __aexit__(self, *a):
            return False

    dead_pg = SimpleNamespace(acquire=lambda *a, **k: _DeadAcquire())
    triggers_app.state.registry_deps = SimpleNamespace(
        descriptor_registry=SimpleNamespace(pg=dead_pg),
    )
    async with AsyncClient(
        transport=ASGITransport(app=triggers_app), base_url="http://testserver",
    ) as c:
        body = (await c.get("/api/v1/v3/graph-triggers")).json()
    assert body["measured"] is False
    assert body["triggers"] == []


# ---------------------------------------------------------------------------
# The 0208 stamp — duration_ms lands on the settle row via the REAL path
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def pool(migrated_pg: PostgresConfig):
    p = await asyncpg.create_pool(
        host=migrated_pg.host, port=migrated_pg.port, user=migrated_pg.user,
        password=migrated_pg.password, database=migrated_pg.database,
        min_size=1, max_size=4,
    )
    yield p
    await p.close()


def _pack(pid: str, *, tools: list[str], tags: list[str] | None = None):
    from legba.data.schemas.action_pack import ActionPack

    return ActionPack.model_validate(
        {
            "identity": {
                "id": pid, "name": pid, "schema_uri": "legba/action_pack/1.0.0",
                "version": "a" * 16, "state": "active", "owner": "p11_agency",
                "created": datetime.now(timezone.utc).isoformat(),
            },
            "tools": [{"name": t} for t in tools],
            "applies_to_tags": tags or [],
        },
        strict=False,
    )


@pytest.mark.asyncio
async def test_settle_stamps_duration_ms_on_success_and_crash(pool, clean_tables):
    """run_pack_tool → settle writes duration_ms on BOTH settle paths —
    a completed call and a handler crash (E2 reads both; latency is latency)."""
    from legba.data.analysts.agency import (
        Agency, TargetScopeView, ToolCall, ToolContext, ToolRegistry,
        ToolResult,
    )
    from legba.data.schemas.action_pack import ActionPackRef

    await clean_tables("action_pack_invocations")

    async def _ok(call, pack, ctx):
        return ToolResult(status="emitted", output={"ok": True})

    async def _boom(call, pack, ctx):
        raise RuntimeError("handler exploded")

    registry = ToolRegistry()
    registry.register("query_paths", _ok)
    registry.register("query_brokers", _boom)
    agency = Agency(tool_registry=registry)
    pack = _pack(
        "substrate_read", tools=["query_paths", "query_brokers"], tags=[],
    )
    scope = TargetScopeView(target_id="t_gauge", tags=[])
    account = f"acct-{uuid4().hex[:8]}"

    async with pool.acquire() as conn:
        ok = await agency.run_pack_tool(
            conn, pack=pack,
            call=ToolCall(
                pack_id="substrate_read", tool_name="query_paths",
                budget_account=account, requested_by="t", args={},
            ),
            analyst_grants=[ActionPackRef(pack_id="substrate_read")],
            target_allows=[ActionPackRef(pack_id="substrate_read")],
            scope=scope, ctx=ToolContext(),
        )
        crashed = await agency.run_pack_tool(
            conn, pack=pack,
            call=ToolCall(
                pack_id="substrate_read", tool_name="query_brokers",
                budget_account=account, requested_by="t", args={},
            ),
            analyst_grants=[ActionPackRef(pack_id="substrate_read")],
            target_allows=[ActionPackRef(pack_id="substrate_read")],
            scope=scope, ctx=ToolContext(),
        )
        rows = await conn.fetch(
            """SELECT tool_name, outcome, duration_ms
                 FROM action_pack_invocations
                WHERE budget_account = $1 ORDER BY occurred_at""",
            account,
        )

    assert ok.admitted and crashed.admitted
    by_tool = {r["tool_name"]: r for r in rows}
    assert by_tool["query_paths"]["outcome"] == "completed"
    assert by_tool["query_brokers"]["outcome"] == "failed"
    for r in rows:
        assert r["duration_ms"] is not None and r["duration_ms"] >= 0
