# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for V3/P4b — the graph projection (build, read, refusal, parity).

Covers:

  * :mod:`legba.data.analysts.deterministic_handlers.graph_projector` — the
    whole-rebuild builder (flag gate, dry_run, the INSERT...SELECT sources,
    the transactional swap, the meta receipt);
  * :mod:`legba.data.registry.graph_arcs_api` — ``GET /api/v1/v3/graph/arcs``,
    the plane-filtered bounded ego/walk with ``as_of``, exercised through the
    REAL binding path (router factory + deps bundle + ASGI transport);
  * the three refusal states — ``projection_disabled`` / ``projection_empty``
    / ``projection_stale`` — as DISTINCT machine-readable states, never a
    quiet ``found=False``;
  * the S-1 parity loop in
    :mod:`legba.data.registry.production_gauge_projection`.

Auth: dev-mode (``LEGBA_DEV_MODE=1``, no ``LEGBA_REGISTRY_API_TOKEN``), so
``require_bearer`` returns ``"anonymous"`` — the same bearer path as the rest
of the v3 surface.
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import textwrap
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import UUID, uuid4

import asyncpg
import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from legba.data.analysts.deterministic_handlers import graph_projector
from legba.data.config import PostgresConfig
from legba.data.graph_projection import (
    GRAPH_ARC_SOURCE_TABLES,
    GRAPH_PROJECTION_ENV,
    GRAPH_PROJECTION_MAX_AGE_ENV,
)
from legba.data.registry.api import API_TOKEN_ENV
from legba.data.registry.graph_arcs_api import (
    PLANES,
    build_graph_arcs_router,
)
from legba.data.registry.production_gauge import GaugeConfig
from legba.data.registry.production_gauge_projection import (
    LOOP_GRAPH_PROJECTION,
    read_projection_loops,
)

_SRC = str(pathlib.Path(__file__).resolve().parents[2] / "src")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def pg_pool(migrated_pg: PostgresConfig, clean_tables, monkeypatch):
    """A clean projection substrate + a short-lived pool, flag ON."""
    # NOTE: graph_arcs_new / graph_arcs_old are NOT listed — they only exist
    # transiently inside a build, and a TRUNCATE over a nonexistent table
    # aborts the whole clean_tables statement (UndefinedTableError).
    await clean_tables(
        "graph_arcs",
        "graph_arcs_meta",
        "signals",
        "entity_profiles",
        "entity_edges",
        "analyst_outputs",
        "situations",
        "events",
        "signal_event_links",
        "event_entity_links",
        "event_edges",
        "situation_event_links",
        "situation_events",
        "signal_entity_links",
        "signal_aliases",
        "facts",
        "fact_contention",
        "fact_contention_values",
        "output_consumption",
        "bearing_edges",
        "narrative_echo_edges",
        "proposed_edges",
        "journal_entries",
    )
    monkeypatch.setenv(GRAPH_PROJECTION_ENV, "1")
    monkeypatch.delenv(GRAPH_PROJECTION_MAX_AGE_ENV, raising=False)
    pool = await asyncpg.create_pool(migrated_pg.dsn, min_size=1, max_size=3)
    yield pool
    await pool.close()


async def _entity(conn, name: str) -> UUID:
    """Insert one canonical entity profile."""
    return await conn.fetchval(
        "INSERT INTO entity_profiles (data, canonical_name) "
        "VALUES ('{}'::jsonb, $1) RETURNING id",
        name,
    )


async def _signal(conn, title: str) -> UUID:
    """Insert one minimal canonical signal."""
    return await conn.fetchval(
        """
        INSERT INTO signals (source_id, modality, payload, content_hash)
        VALUES ($1, 'text', $2::jsonb, $3) RETURNING id
        """,
        "src-a",
        json.dumps({"title": title}),
        f"p4b-{uuid4().hex}",
    )


async def _event(conn) -> UUID:
    """Insert one minimal event row (P0 writer shape, direct insert)."""
    return await conn.fetchval(
        """
        INSERT INTO events (event_signature, analyst_id, title, confidence)
        VALUES ($1, 'test', $2, 0.6) RETURNING id
        """,
        f"sig-{uuid4().hex}",
        f"event-{uuid4().hex[:8]}",
    )


async def _finding(conn, derived_from=None, kind: str = "alert") -> UUID:
    """Insert one analyst_outputs row (a finding node)."""
    return await conn.fetchval(
        """
        INSERT INTO analyst_outputs (kind, title, body, data, derived_from,
                                     schema_uri)
        VALUES ($1, $2, '', '{}'::jsonb, $3::uuid[], 'iglu:test/finding')
        RETURNING id
        """,
        kind,
        f"finding-{uuid4().hex[:8]}",
        list(derived_from or []),
    )


async def _all_arcs(conn) -> list[tuple]:
    """The whole projection as a sorted tuple list — the row-identity probe."""
    rows = await conn.fetch(
        "SELECT from_kind, from_id, to_kind, to_id, arc_type, plane, family, "
        "polarity, confidence, valid_from, valid_until, src_table, src_id "
        "FROM graph_arcs ORDER BY from_kind, from_id, to_kind, to_id, arc_type"
    )
    return [tuple(r) for r in rows]


def _deps(pool) -> SimpleNamespace:
    """The handler deps bundle — only what the projector touches."""
    return SimpleNamespace(pg_pool=pool)


# ---------------------------------------------------------------------------
# Pure tests — registration, vocabulary, the poisoned-import guard
# ---------------------------------------------------------------------------


def test_version_marker() -> None:
    """The deploy marker is the runbook's grep target."""
    assert graph_projector.GRAPH_PROJECTION_VERSION == "2026-09/p4b"


def test_source_table_vocabulary_matches_the_shared_leaf() -> None:
    """The projector's src_table keys == GRAPH_ARC_SOURCE_TABLES — the leaf
    the S-1 gauge reads, so a dropped leg or a renamed source fails here."""
    projected = {src for src, _sql in graph_projector._SOURCES}
    assert projected == set(GRAPH_ARC_SOURCE_TABLES)


def test_plane_vocabulary_matches_the_migration_check() -> None:
    """The API's plane list is the 0207 CHECK — drift would 400 valid reads."""
    assert PLANES == ("world", "evidence", "lineage")


def test_route_registered() -> None:
    """The route registers under the router factory."""
    router = build_graph_arcs_router(deps=object())  # type: ignore[arg-type]
    paths = {r.path for r in router.routes}  # type: ignore[attr-defined]
    assert "/graph/arcs" in paths


def test_no_substrate_writes_in_the_projection_path() -> None:
    """Spec §4.2 rule 1: authoritative store → projection, never the reverse.
    The projector writes ONLY to graph_arcs_new / graph_arcs_meta (and the
    swap's own renames); the read API writes nothing at all."""
    import re

    here = pathlib.Path(__file__).resolve().parents[2] / "src" / "legba"
    for rel in (
        "data/analysts/deterministic_handlers/graph_projector.py",
        "data/registry/graph_arcs_api.py",
        "data/graph_projection.py",
    ):
        src = (here / rel).read_text()
        for m in re.finditer(
            # ON CONFLICT ... DO UPDATE SET is the upsert tail, not a write.
            r"(?:INSERT\s+INTO|DELETE\s+FROM|(?<!DO\s)UPDATE)\s+(\w+)", src
        ):
            target = m.group(1)
            assert target in {
                "graph_arcs_new", "graph_arcs", "graph_arcs_meta",
                "graph_arcs_old", "public",
            }, f"{rel}: write against substrate table {target!r}"


def test_graph_arcs_route_imports_without_the_runtime_stack() -> None:
    """The slim-image import guard: the route must import with the heavy
    third-party stack poisoned and never reach legba.data.analysts /
    legba.runtime."""
    probe = textwrap.dedent(
        """
        import sys
        for blocked in ("feedparser", "telethon", "warcio", "aiobotocore",
                        "pycountry", "networkx", "qdrant_client"):
            sys.modules[blocked] = None

        import legba.data.registry.graph_arcs_api as api

        leaked = sorted(
            m for m in sys.modules
            if m.startswith("legba.data.analysts")
               or m.startswith("legba.runtime")
        )
        assert not leaked, "analyst/runtime reachable from route: %r" % leaked
        assert api.build_graph_arcs_router is not None
        print("OK")
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True, text=True, timeout=120,
        env={**os.environ, "PYTHONPATH": os.pathsep.join(
            p for p in (_SRC, os.environ.get("PYTHONPATH", "")) if p)},
    )
    assert result.returncode == 0, (
        f"slim-image import probe failed:\n{result.stdout}\n{result.stderr}"
    )
    assert "OK" in result.stdout


# ---------------------------------------------------------------------------
# The build — flag gate, dry_run, source coverage, row-identity
# ---------------------------------------------------------------------------


async def test_flag_off_never_builds(pg_pool, monkeypatch) -> None:
    """LEGBA_GRAPH_PROJECTION off → honest no-op, table untouched."""
    monkeypatch.setenv(GRAPH_PROJECTION_ENV, "0")
    res = await graph_projector.handle([], {}, _deps(pg_pool))
    assert res.finding.data["projection_enabled"] is False
    assert await pg_pool.fetchval("SELECT count(*) FROM graph_arcs") == 0
    assert await pg_pool.fetchval("SELECT count(*) FROM graph_arcs_meta") == 0


async def test_dry_run_builds_without_swapping(pg_pool) -> None:
    """dry_run builds + counts staging but installs nothing."""
    async with pg_pool.acquire() as conn:
        a = await _entity(conn, "Alpha")
        b = await _entity(conn, "Beta")
        await conn.execute(
            "INSERT INTO entity_edges (src_id, dst_id, edge_type, "
            "edge_family, polarity) VALUES ($1, $2, 'HostileTo', 'relation', -1)",
            a, b,
        )
    res = await graph_projector.handle([], {"dry_run": True}, _deps(pg_pool))
    d = res.finding.data
    assert d["dry_run"] is True
    assert d["source_counts"]["entity_edges"] == 1
    assert d["arc_count"] == 1
    # Nothing installed.
    assert await pg_pool.fetchval("SELECT count(*) FROM graph_arcs") == 0
    assert await pg_pool.fetchval("SELECT count(*) FROM graph_arcs_meta") == 0


async def test_build_twice_row_identical(pg_pool) -> None:
    """Two builds over the same snapshot produce ROW-IDENTICAL content —
    the disposable-projection determinism contract (spec §4.2 rule 2)."""
    async with pg_pool.acquire() as conn:
        a = await _entity(conn, "Alpha")
        b = await _entity(conn, "Beta")
        mid = await _entity(conn, "Hamas")
        sig = await _signal(conn, "signal one")
        ev = await _event(conn)
        f1 = await _finding(conn, derived_from=[sig])
        await conn.execute(
            "INSERT INTO entity_edges (src_id, dst_id, edge_type, "
            "edge_family, polarity, intermediary_id) "
            "VALUES ($1, $2, 'HostileTo', 'relation', -1, $3)",
            a, b, mid,
        )
        await conn.execute(
            "INSERT INTO signal_entity_links (signal_id, entity_id, role) "
            "VALUES ($1, $2, 'subject')",
            sig, a,
        )
        await conn.execute(
            "INSERT INTO signal_event_links "
            "(signal_id, event_id, relevance, source_class, linked_at) "
            "VALUES ($1, $2, 0.9, 'reporting', now())",
            sig, ev,
        )
        await conn.execute(
            "INSERT INTO event_entity_links (event_id, entity_id, role) "
            "VALUES ($1, $2, 'actor')",
            ev, a,
        )
        ev2 = await _event(conn)
        await conn.execute(
            # correlated_with is symmetric — stored canonically src < dst.
            "INSERT INTO event_edges (src_event_id, dst_event_id, edge_type) "
            "VALUES (LEAST($1::uuid, $2::uuid), GREATEST($1::uuid, $2::uuid), "
            "'correlated_with')",
            ev, ev2,
        )
    await graph_projector.handle([], {}, _deps(pg_pool))
    async with pg_pool.acquire() as conn:
        first = await _all_arcs(conn)
    await graph_projector.handle([], {}, _deps(pg_pool))
    async with pg_pool.acquire() as conn:
        second = await _all_arcs(conn)
    assert first == second
    assert len(first) > 0
    # The intermediated edge produced the main arc + both via hops.
    via = [r for r in second if r[4] == "via"]
    assert len(via) == 2
    # And the meta receipt landed.
    meta = await pg_pool.fetchrow(
        "SELECT arc_count, source_counts, build_seconds FROM graph_arcs_meta"
    )
    assert meta["arc_count"] == len(second)
    assert meta["build_seconds"] > 0
    counts = json.loads(meta["source_counts"])
    assert counts["entity_edges"] == 3  # 1 main + 2 via
    assert sum(counts.values()) == len(second)


async def test_plane_assignment_per_source(pg_pool) -> None:
    """Each source lands on its declared plane (spec §4.1)."""
    async with pg_pool.acquire() as conn:
        a = await _entity(conn, "Alpha")
        b = await _entity(conn, "Beta")
        sig = await _signal(conn, "s")
        ev = await _event(conn)
        sit = await conn.fetchval(
            "INSERT INTO situations (id, data, name) "
            "VALUES (gen_random_uuid(), '{}'::jsonb, 'sit') RETURNING id"
        )
        f1 = await _finding(conn, derived_from=[sig])
        await conn.execute(
            "INSERT INTO entity_edges (src_id, dst_id, edge_type, "
            "edge_family) VALUES ($1, $2, 'AlliedWith', 'relation')",
            a, b,
        )
        await conn.execute(
            "INSERT INTO signal_entity_links (signal_id, entity_id) "
            "VALUES ($1, $2)", sig, a,
        )
        await conn.execute(
            "INSERT INTO signal_event_links "
            "(signal_id, event_id, source_class, linked_at) "
            "VALUES ($1, $2, 'reporting', now())", sig, ev,
        )
        await conn.execute(
            "INSERT INTO situation_event_links (situation_id, event_id) "
            "VALUES ($1, $2)", sit, ev,
        )
        await conn.execute(
            "UPDATE situations SET derived_from = $2::uuid[] WHERE id = $1",
            sit, [f1],
        )
    await graph_projector.handle([], {}, _deps(pg_pool))
    planes = await pg_pool.fetch(
        "SELECT arc_type, plane FROM graph_arcs"
    )
    by_type = {r["arc_type"]: r["plane"] for r in planes}
    assert by_type["mentions"] == "evidence"          # signal_entity_links
    assert by_type["AlliedWith"] == "world"           # entity_edges
    assert by_type["evidences"] == "world"            # signal_event_links
    assert by_type["tracked_by"] == "world"           # situation_event_links
    assert by_type["member_of"] == "lineage"          # situations.derived_from
    assert by_type["cites"] == "lineage"              # derived_from


async def test_excluded_sources_never_project(pg_pool) -> None:
    """proposed_edges (candidate queue) and journal_entries (off-chain) can
    never produce arcs — spec §4.1's explicit exclusions."""
    async with pg_pool.acquire() as conn:
        a = await _entity(conn, "Alpha")
        b = await _entity(conn, "Beta")
        await conn.execute(
            "INSERT INTO entity_edges (src_id, dst_id, edge_type, "
            "edge_family) VALUES ($1, $2, 'AlliedWith', 'reference')",
            a, b,
        )
        await conn.execute(
            "INSERT INTO proposed_edges "
            "(source_entity, target_entity, relationship_type) "
            "VALUES ('Alpha', 'Beta', 'AlliedWith')",
        )
        await conn.execute(
            "INSERT INTO journal_entries "
            "(entry_kind, title, body, period_start, period_end, produced_at) "
            "VALUES ('entry', 'j', 'b', now(), now(), now())",
        )
    await graph_projector.handle([], {}, _deps(pg_pool))
    bad = await pg_pool.fetchval(
        "SELECT count(*) FROM graph_arcs "
        "WHERE src_table IN ('proposed_edges', 'journal_entries')"
    )
    assert bad == 0
    # The real edge did project — the exclusion is surgical, not a blanket.
    assert await pg_pool.fetchval("SELECT count(*) FROM graph_arcs") == 1


# ---------------------------------------------------------------------------
# The read — real binding path (router + deps + ASGI), refusal states, as_of
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def arcs_client(pg_pool):
    """A FastAPI app exposing the real router over the real pool."""
    os.environ.pop(API_TOKEN_ENV, None)  # dev mode — any bearer accepted
    deps = SimpleNamespace(
        descriptor_registry=SimpleNamespace(pg=pg_pool),
    )
    app = FastAPI()
    app.state.registry_deps = deps
    app.include_router(build_graph_arcs_router(deps), prefix="/api/v1/v3")
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver",
    ) as c:
        yield c


async def test_refusal_projection_disabled(arcs_client, monkeypatch) -> None:
    """Flag off → 409 with state=projection_disabled, not an empty result."""
    monkeypatch.setenv(GRAPH_PROJECTION_ENV, "0")
    r = await arcs_client.get(
        "/api/v1/v3/graph/arcs",
        params={"node_kind": "entity", "node_id": str(uuid4())},
    )
    assert r.status_code == 409
    assert r.json()["detail"]["state"] == "projection_disabled"


async def test_refusal_projection_empty(arcs_client) -> None:
    """Flag on, never built → 409 projection_empty (never found=False)."""
    r = await arcs_client.get(
        "/api/v1/v3/graph/arcs",
        params={"node_kind": "entity", "node_id": str(uuid4())},
    )
    assert r.status_code == 409
    assert r.json()["detail"]["state"] == "projection_empty"


async def test_refusal_projection_stale(arcs_client, pg_pool) -> None:
    """A built-but-old projection refuses with projection_stale."""
    await pg_pool.execute(
        "INSERT INTO graph_arcs (from_kind, from_id, to_kind, to_id, "
        "arc_type, plane, src_table) VALUES ('entity', $1, 'entity', $2, "
        "'AlliedWith', 'world', 'entity_edges')",
        uuid4(), uuid4(),
    )
    await pg_pool.execute(
        "INSERT INTO graph_arcs_meta (id, projected_at, arc_count, "
        "source_counts, build_seconds) VALUES (true, $1, 1, '{}', 1.0)",
        datetime.now(timezone.utc) - timedelta(hours=4),
    )
    r = await arcs_client.get(
        "/api/v1/v3/graph/arcs",
        params={"node_kind": "entity", "node_id": str(uuid4())},
    )
    assert r.status_code == 409
    assert r.json()["detail"]["state"] == "projection_stale"


async def test_temporal_walk_excludes_closed_arc(arcs_client, pg_pool) -> None:
    """An arc closed before as_of is excluded from the temporal read; the
    same arc is present at an earlier as_of."""
    async with pg_pool.acquire() as conn:
        a = await _entity(conn, "Alpha")
        b = await _entity(conn, "Beta")
        closed_at = datetime(2026, 9, 20, tzinfo=timezone.utc)
        await conn.execute(
            "INSERT INTO graph_arcs (from_kind, from_id, to_kind, to_id, "
            "arc_type, plane, src_table, valid_from, valid_until) "
            "VALUES ('entity', $1, 'entity', $2, 'AlliedWith', 'world', "
            "'entity_edges', $3, $4)",
            a, b,
            datetime(2026, 9, 1, tzinfo=timezone.utc), closed_at,
        )
        await conn.execute(
            "INSERT INTO graph_arcs_meta (id, projected_at, arc_count, "
            "source_counts, build_seconds) VALUES (true, now(), 1, '{}', 1.0)"
        )
    base = "/api/v1/v3/graph/arcs"
    params = {"node_kind": "entity", "node_id": str(a), "plane": "world"}

    # Open-now read: the closed arc is gone.
    r = await arcs_client.get(base, params=params)
    assert r.status_code == 200
    assert r.json()["arcs"] == []
    assert r.json()["projected_at"] is not None

    # as_of AFTER the close: still gone.
    r = await arcs_client.get(
        base, params={**params, "as_of": "2026-09-21T00:00:00Z"}
    )
    assert r.json()["arcs"] == []

    # as_of BEFORE the close: the arc is there.
    r = await arcs_client.get(
        base, params={**params, "as_of": "2026-09-10T00:00:00Z"}
    )
    body = r.json()
    assert len(body["arcs"]) == 1
    assert body["arcs"][0]["arc_type"] == "AlliedWith"
    assert body["arcs"][0]["to_id"] == str(b)


async def test_world_plane_is_the_default(arcs_client, pg_pool) -> None:
    """World-facing reads default to plane='world' — a lineage arc on the
    same node is invisible unless lineage is asked for."""
    async with pg_pool.acquire() as conn:
        a = await _entity(conn, "Alpha")
        f1 = await _finding(conn)
        await conn.execute(
            "INSERT INTO graph_arcs (from_kind, from_id, to_kind, to_id, "
            "arc_type, plane, src_table) VALUES "
            "('entity', $1, 'entity', $2, 'AlliedWith', 'world', 'entity_edges'),"
            "('finding', $3, 'finding', $4, 'supersedes', 'lineage', "
            "'analyst_outputs')",
            a, uuid4(), f1, uuid4(),
        )
        await conn.execute(
            "INSERT INTO graph_arcs_meta (id, projected_at, arc_count, "
            "source_counts, build_seconds) VALUES (true, now(), 2, '{}', 1.0)"
        )
    r = await arcs_client.get(
        "/api/v1/v3/graph/arcs",
        params={"node_kind": "entity", "node_id": str(a)},
    )
    body = r.json()
    assert r.status_code == 200
    assert body["plane"] == "world"
    assert len(body["arcs"]) == 1
    assert body["arcs"][0]["plane"] == "world"


async def test_empty_neighbourhood_is_200_not_a_refusal(
    arcs_client, pg_pool
) -> None:
    """A node with no arcs on a healthy projection is a 200 with arcs: [] —
    'this node has no arcs' and 'the projection is not there' are different
    answers."""
    await pg_pool.execute(
        "INSERT INTO graph_arcs_meta (id, projected_at, arc_count, "
        "source_counts, build_seconds) VALUES (true, now(), 1, '{}', 1.0)"
    )
    await pg_pool.execute(
        "INSERT INTO graph_arcs (from_kind, from_id, to_kind, to_id, "
        "arc_type, plane, src_table) VALUES ('entity', $1, 'entity', $2, "
        "'AlliedWith', 'world', 'entity_edges')",
        uuid4(), uuid4(),
    )
    r = await arcs_client.get(
        "/api/v1/v3/graph/arcs",
        params={"node_kind": "entity", "node_id": str(uuid4())},
    )
    assert r.status_code == 200
    assert r.json()["arcs"] == []
    assert r.json()["matched"] == 0


# ---------------------------------------------------------------------------
# The S-1 loop — parity / freshness gauge
# ---------------------------------------------------------------------------


async def test_gauge_flag_off_is_ungauged(pg_pool, monkeypatch) -> None:
    """A deliberately-off projection is a configuration, not a deficit."""
    monkeypatch.setenv(GRAPH_PROJECTION_ENV, "0")
    async with pg_pool.acquire() as conn:
        loops = await read_projection_loops(
            conn, now=datetime.now(timezone.utc), cfg=GaugeConfig()
        )
    assert len(loops) == 1
    assert loops[0].loop_class == LOOP_GRAPH_PROJECTION
    assert loops[0].state == "ungauged"
    assert loops[0].quiet_reason == "projection_disabled"


async def test_gauge_fresh_build_is_ok(pg_pool) -> None:
    """A fresh, consistent, complete build reads ok with parity evidence."""
    async with pg_pool.acquire() as conn:
        a = await _entity(conn, "Alpha")
        b = await _entity(conn, "Beta")
        await conn.execute(
            "INSERT INTO entity_edges (src_id, dst_id, edge_type, "
            "edge_family) VALUES ($1, $2, 'AlliedWith', 'relation')", a, b,
        )
    await graph_projector.handle([], {}, _deps(pg_pool))
    async with pg_pool.acquire() as conn:
        loops = await read_projection_loops(
            conn, now=datetime.now(timezone.utc), cfg=GaugeConfig()
        )
    (g,) = loops
    assert g.state == "ok"
    assert g.evidence["arc_count"] >= 1
    assert g.evidence["build_seconds"] > 0
    assert g.evidence["consistent"] is True
    assert g.evidence["missing_legs"] == []


async def test_gauge_no_receipt_is_a_deficit(pg_pool) -> None:
    """Flag on, no build ever landed → deficit (expected work, none done)."""
    async with pg_pool.acquire() as conn:
        loops = await read_projection_loops(
            conn, now=datetime.now(timezone.utc), cfg=GaugeConfig()
        )
    assert loops[0].state == "deficit"
    assert loops[0].evidence["reason"] == "projection_empty"


# ---------------------------------------------------------------------------
# The repointed readers' flag branch (review, 2026-09-23): flag OFF keeps the
# pre-P4b substrate read; flag ON with an empty/stale projection contributes
# nothing and says why. Pure unit level — a recording connection, no DB.
# ---------------------------------------------------------------------------


class _RecordingConn:
    def __init__(self):
        self.sql: list[str] = []

    async def fetch(self, sql, *args):
        self.sql.append(sql)
        return []


class _Pool:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        conn = self._conn

        class _Cm:
            async def __aenter__(self_inner):
                return conn

            async def __aexit__(self_inner, *exc):
                return False

        return _Cm()


class _Deps:
    def __init__(self, conn):
        self.pg_pool = _Pool(conn)


@pytest.mark.parametrize(
    "module_name,fn_name,empty",
    [
        ("graph_mining", "_augment_from_nexuses", 0),
        ("graph_mining", "_recent_hostile_edges", []),
        ("structural_balance", "_augment_from_nexuses", []),
    ],
)
@pytest.mark.asyncio
async def test_readers_keep_the_substrate_read_while_the_flag_is_off(
    monkeypatch, module_name, fn_name, empty
):
    import importlib
    import networkx as nx

    mod = importlib.import_module(
        f"legba.data.analysts.deterministic_handlers.{module_name}"
    )

    async def _disabled(conn):
        return "projection_disabled", None

    monkeypatch.setattr(mod, "graph_projection_state", _disabled)
    conn = _RecordingConn()
    fn = getattr(mod, fn_name)
    if fn_name == "_augment_from_nexuses" and module_name == "graph_mining":
        out = await fn(_Deps(conn), nx.MultiDiGraph())
    else:
        out = await fn(_Deps(conn))
    assert out == empty
    assert conn.sql and "FROM entity_edges e" in conn.sql[0], "flag off ⇒ the pre-P4b read"
    assert "FROM graph_arcs" not in conn.sql[0]


@pytest.mark.parametrize("state", ["projection_empty", "projection_stale"])
@pytest.mark.asyncio
async def test_readers_contribute_nothing_on_an_empty_or_stale_projection(
    monkeypatch, state
):
    import networkx as nx
    from legba.data.analysts.deterministic_handlers import graph_mining as gm

    async def _state(conn):
        return state, None

    monkeypatch.setattr(gm, "graph_projection_state", _state)
    conn = _RecordingConn()
    assert await gm._augment_from_nexuses(_Deps(conn), nx.MultiDiGraph()) == 0
    assert conn.sql == [], "flag on but nothing to read ⇒ no query, just the log line"


async def test_build_lands_in_public_under_the_age_first_search_path(
    migrated_pg, pg_pool
) -> None:
    """P4b-D1: the runtime pool puts ag_catalog FIRST on the search path, and
    an unqualified CREATE/RENAME landed the live projection in the AGE schema
    (2026-09-23). Build through a pool with the production search path and
    assert the table is in public and NOT in ag_catalog."""
    import asyncpg
    from legba.data.analysts.deterministic_handlers import graph_projector

    async with pg_pool.acquire() as conn:
        await conn.execute("CREATE SCHEMA IF NOT EXISTS ag_catalog")

    async def _init(c):
        await c.execute('SET search_path = ag_catalog, "$user", public')

    pool = await asyncpg.create_pool(migrated_pg.dsn, min_size=1, max_size=2, init=_init)
    try:
        await graph_projector.handle([], {}, _deps(pool))
        async with pool.acquire() as conn:
            assert await conn.fetchval("SELECT to_regclass('public.graph_arcs')") is not None
            assert await conn.fetchval("SELECT to_regclass('ag_catalog.graph_arcs')") is None
            assert await conn.fetchval("SELECT to_regclass('ag_catalog.graph_arcs_new')") is None
            assert await conn.fetchval("SELECT count(*) FROM public.graph_arcs_meta") == 1
    finally:
        await pool.close()

