# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Integration tests for the lineage walk endpoint (L-204 P-5).

Hits the FastAPI router built by ``build_lineage_router`` against a real
substrate (``migrated_pg`` fixture). No mocks for the BFS or for any of
the substrate-table queries — the whole point is the cross-table walk.

Covered:
  * Single-row root (no derived_from) → root-only report.
  * Two-hop chain (signal → finding):
      - upstream from finding returns [signal].
      - downstream from signal returns [finding].
  * Three-hop multi-table chain (signal → finding → situation):
      - depth=1 truncates with ``truncated_at_depth=true``.
      - depth=3 walks full.
  * Cycle protection: row in derived_from references itself / an
    earlier ancestor; walker must terminate.
  * Bad row_kind → 400.
  * Unknown row_id → 404.
  * ``direction=both`` walks both ways from a middle row.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from uuid import UUID, uuid4

import asyncpg
import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from legba.data.config import PostgresConfig
from legba.data.postgres import PostgresStore
from legba.data.registry.api import API_TOKEN_ENV, RegistryAPIDeps
from legba.data.registry.lineage_api import build_lineage_router


# ---------------------------------------------------------------------------
# Minimal wiring — we only need .descriptor_registry.pg.acquire() to work,
# so a thin stand-in around the real PostgresStore is fine.
# ---------------------------------------------------------------------------


class _MinimalDescriptorRegistry:
    """Just enough surface for build_lineage_router. We are NOT mocking the
    SQL — every query hits real Postgres. We only avoid spinning the full
    DescriptorRegistry + NATS + vocabulary cache because none of that is
    on the lineage code path."""

    def __init__(self, pg_store: PostgresStore) -> None:
        self.pg = pg_store


@pytest_asyncio.fixture
async def lineage_app(migrated_pg: PostgresConfig):
    """Build a FastAPI app with just the lineage router and a real PG pool."""
    os.environ.pop(API_TOKEN_ENV, None)

    pg_store = PostgresStore(migrated_pg)
    await pg_store.connect()

    deps = RegistryAPIDeps(
        descriptor_registry=_MinimalDescriptorRegistry(pg_store),  # type: ignore[arg-type]
        stack_registry=None,  # type: ignore[arg-type]
        vault=None,  # type: ignore[arg-type]
        dlq=None,  # type: ignore[arg-type]
        audit_logger=None,  # type: ignore[arg-type]
        vocabulary_cache=None,  # type: ignore[arg-type]
        nats_store=None,
        conversion_registry=None,
    )

    app = FastAPI()
    app.state.registry_deps = deps
    app.include_router(build_lineage_router(deps), prefix="/api/v1")

    yield app, pg_store

    await pg_store.close()


@pytest_asyncio.fixture
async def client(lineage_app):
    app, _ = lineage_app
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver",
    ) as c:
        yield c


# ---------------------------------------------------------------------------
# Insert helpers — sidestep the writes.py routing layer so we can craft
# exact derived_from edges (including cycles).
# ---------------------------------------------------------------------------


async def _insert_signal(
    conn: asyncpg.Connection,
    *,
    title: str = "sig",
    derived_from: list[UUID] | None = None,
    source_id: str = "rss_main",
) -> UUID:
    # Source-first pivot (migration 0024): signals are target-agnostic +
    # modality-first. ``title`` lives in ``payload``; per-target columns are
    # gone; the lineage walker projects the title via ``payload->>'title'``
    # and uses ``fetched_at`` as the timestamp.
    sid = uuid4()
    await conn.execute(
        """
        INSERT INTO signals
            (id, source_id, source_version, produced_by_kind, fetched_at,
             modality, payload, content_hash, derived_from, schema_uri)
        VALUES ($1, $2, '', 'source', NOW(),
                'text', $3::jsonb, '', $4::uuid[],
                'iglu:legba/signal/jsonschema/3-0-0')
        """,
        sid, source_id, json.dumps({"title": title}), derived_from or [],
    )
    return sid


async def _insert_analyst_output(
    conn: asyncpg.Connection,
    *,
    kind: str,
    title: str,
    derived_from: list[UUID] | None = None,
    analyst_id: str = "an_x",
) -> UUID:
    aid = uuid4()
    await conn.execute(
        """
        INSERT INTO analyst_outputs
            (id, kind, title, body, analyst_id, analyst_version,
             produced_at, derived_from, schema_uri)
        VALUES ($1, $2, $3, '', $4, 'av', NOW(), $5::uuid[], $6)
        """,
        aid, kind, title, analyst_id, derived_from or [],
        f"iglu:legba/{kind}/jsonschema/1-0-0",
    )
    return aid


async def _insert_situation(
    conn: asyncpg.Connection,
    *,
    name: str,
    derived_from: list[UUID] | None = None,
) -> UUID:
    sid = uuid4()
    await conn.execute(
        """
        INSERT INTO situations
            (id, data, name, target_id, target_version, analyst_id,
             analyst_version, produced_at, derived_from, schema_uri)
        VALUES ($1, '{}'::jsonb, $2, 'br_energy', 'tv', 'an_situ', 'av',
                NOW(), $3::uuid[],
                'iglu:legba/situation/jsonschema/2-0-0')
        """,
        sid, name, derived_from or [],
    )
    return sid


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_single_row_root_only(lineage_app, client: AsyncClient):
    _, pg_store = lineage_app
    async with pg_store.acquire() as conn:
        sid = await _insert_signal(conn, title="lonely_signal")

    r = await client.get(
        f"/api/v1/lineage/signal/{sid}",
        params={"direction": "upstream", "depth": 3},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["root"]["id"] == str(sid)
    assert body["root"]["row_kind"] == "signal"
    assert body["root"]["title"] == "lonely_signal"
    assert body["root"]["depth"] == 0
    assert body["nodes"] == []
    assert body["edges"] == []
    assert body["truncated_at_depth"] is False


@pytest.mark.integration
@pytest.mark.asyncio
async def test_upstream_two_hop_chain(lineage_app, client: AsyncClient):
    """signal A → finding B; upstream from B returns [A]."""
    _, pg_store = lineage_app
    async with pg_store.acquire() as conn:
        a_id = await _insert_signal(conn, title="sig_A")
        b_id = await _insert_analyst_output(
            conn, kind="finding", title="finding_B", derived_from=[a_id],
        )

    r = await client.get(
        f"/api/v1/lineage/finding/{b_id}",
        params={"direction": "upstream", "depth": 3},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["root"]["id"] == str(b_id)
    assert body["root"]["row_kind"] == "finding"
    assert len(body["nodes"]) == 1
    assert body["nodes"][0]["id"] == str(a_id)
    assert body["nodes"][0]["row_kind"] == "signal"
    assert body["nodes"][0]["depth"] == 1
    assert body["edges"] == [{"parent": str(a_id), "child": str(b_id)}]
    assert body["truncated_at_depth"] is False


@pytest.mark.integration
@pytest.mark.asyncio
async def test_downstream_two_hop_chain(lineage_app, client: AsyncClient):
    """signal A → finding B; downstream from A returns [B]."""
    _, pg_store = lineage_app
    async with pg_store.acquire() as conn:
        a_id = await _insert_signal(conn, title="sig_A_dn")
        b_id = await _insert_analyst_output(
            conn, kind="finding", title="finding_B_dn", derived_from=[a_id],
        )

    r = await client.get(
        f"/api/v1/lineage/signal/{a_id}",
        params={"direction": "downstream", "depth": 3},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["root"]["id"] == str(a_id)
    assert len(body["nodes"]) == 1
    assert body["nodes"][0]["id"] == str(b_id)
    assert body["nodes"][0]["row_kind"] == "finding"
    assert body["nodes"][0]["depth"] == 1
    assert body["edges"] == [{"parent": str(a_id), "child": str(b_id)}]
    assert body["truncated_at_depth"] is False


@pytest.mark.integration
@pytest.mark.asyncio
async def test_multi_hop_depth_truncation(lineage_app, client: AsyncClient):
    """3-hop signal → finding → situation chain.

    depth=1 walks one hop and truncates; depth=3 walks fully and does
    not truncate.
    """
    _, pg_store = lineage_app
    async with pg_store.acquire() as conn:
        a_id = await _insert_signal(conn, title="sig_root")
        b_id = await _insert_analyst_output(
            conn, kind="finding", title="finding_mid", derived_from=[a_id],
        )
        c_id = await _insert_situation(
            conn, name="situ_leaf", derived_from=[b_id],
        )

    # depth=1 from situ_leaf → should reach finding_mid but NOT sig_root.
    r = await client.get(
        f"/api/v1/lineage/situation/{c_id}",
        params={"direction": "upstream", "depth": 1},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    ids = {n["id"] for n in body["nodes"]}
    assert str(b_id) in ids
    assert str(a_id) not in ids
    assert body["truncated_at_depth"] is True

    # depth=3 from situ_leaf → walks the full chain.
    r = await client.get(
        f"/api/v1/lineage/situation/{c_id}",
        params={"direction": "upstream", "depth": 3},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    ids = {n["id"] for n in body["nodes"]}
    assert str(b_id) in ids
    assert str(a_id) in ids
    assert body["truncated_at_depth"] is False

    # depth values per row.
    by_id = {n["id"]: n for n in body["nodes"]}
    assert by_id[str(b_id)]["depth"] == 1
    assert by_id[str(a_id)]["depth"] == 2


@pytest.mark.integration
@pytest.mark.asyncio
async def test_cycle_protection(lineage_app, client: AsyncClient):
    """A finding's derived_from references an earlier ancestor.

    Construct: signal A → finding B (derived_from=[A]) →
    finding C (derived_from=[B, A]). Walking upstream from C must not
    loop infinitely on the convergent A.
    """
    _, pg_store = lineage_app
    async with pg_store.acquire() as conn:
        a_id = await _insert_signal(conn, title="cy_A")
        b_id = await _insert_analyst_output(
            conn, kind="finding", title="cy_B", derived_from=[a_id],
        )
        c_id = await _insert_analyst_output(
            conn, kind="finding", title="cy_C", derived_from=[b_id, a_id],
        )

    r = await client.get(
        f"/api/v1/lineage/finding/{c_id}",
        params={"direction": "upstream", "depth": 5},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    # Each unique row appears at most once in nodes.
    node_ids = [n["id"] for n in body["nodes"]]
    assert len(node_ids) == len(set(node_ids)), node_ids
    assert set(node_ids) == {str(a_id), str(b_id)}
    # Both edges into A are recorded.
    edge_tuples = {(e["parent"], e["child"]) for e in body["edges"]}
    assert (str(a_id), str(c_id)) in edge_tuples
    assert (str(b_id), str(c_id)) in edge_tuples
    assert (str(a_id), str(b_id)) in edge_tuples


@pytest.mark.integration
@pytest.mark.asyncio
async def test_self_referential_cycle(lineage_app, client: AsyncClient):
    """A row whose derived_from contains its own id must not infinite loop."""
    _, pg_store = lineage_app
    async with pg_store.acquire() as conn:
        # Insert with empty derived_from, then patch to self-ref.
        a_id = await _insert_signal(conn, title="self_ref")
        await conn.execute(
            "UPDATE signals SET derived_from = $2::uuid[] WHERE id = $1",
            a_id, [a_id],
        )

    r = await client.get(
        f"/api/v1/lineage/signal/{a_id}",
        params={"direction": "upstream", "depth": 3},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    # Self-edge present, no extra nodes (visited check prevents re-walk).
    assert body["nodes"] == []
    assert body["edges"] == [{"parent": str(a_id), "child": str(a_id)}]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_unknown_row_kind_400(lineage_app, client: AsyncClient):
    r = await client.get(
        f"/api/v1/lineage/sandwich/{uuid4()}",
        params={"direction": "upstream", "depth": 3},
    )
    assert r.status_code == 400, r.text
    assert "sandwich" in r.json()["detail"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_row_not_found_404(lineage_app, client: AsyncClient):
    missing = uuid4()
    r = await client.get(
        f"/api/v1/lineage/signal/{missing}",
        params={"direction": "upstream", "depth": 3},
    )
    assert r.status_code == 404, r.text


@pytest.mark.integration
@pytest.mark.asyncio
async def test_direction_both_from_middle(lineage_app, client: AsyncClient):
    """signal → finding → situation, walk ``both`` from the middle finding.

    Should return the signal (upstream depth=1) AND the situation
    (downstream depth=1) in the same report.
    """
    _, pg_store = lineage_app
    async with pg_store.acquire() as conn:
        a_id = await _insert_signal(conn, title="both_A")
        b_id = await _insert_analyst_output(
            conn, kind="finding", title="both_B", derived_from=[a_id],
        )
        c_id = await _insert_situation(
            conn, name="both_C", derived_from=[b_id],
        )

    r = await client.get(
        f"/api/v1/lineage/finding/{b_id}",
        params={"direction": "both", "depth": 3},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    ids = {n["id"] for n in body["nodes"]}
    assert str(a_id) in ids
    assert str(c_id) in ids
    edge_tuples = {(e["parent"], e["child"]) for e in body["edges"]}
    assert (str(a_id), str(b_id)) in edge_tuples
    assert (str(b_id), str(c_id)) in edge_tuples


@pytest.mark.integration
@pytest.mark.asyncio
async def test_depth_above_cap_rejected(lineage_app, client: AsyncClient):
    """Pydantic Query validation enforces the depth=10 cap."""
    _, pg_store = lineage_app
    async with pg_store.acquire() as conn:
        sid = await _insert_signal(conn, title="cap")

    r = await client.get(
        f"/api/v1/lineage/signal/{sid}",
        params={"direction": "upstream", "depth": 11},
    )
    assert r.status_code == 422, r.text


@pytest.mark.integration
@pytest.mark.asyncio
async def test_polymorphic_kind_filter_on_analyst_outputs(
    lineage_app, client: AsyncClient,
):
    """A finding UUID must not resolve via the alert path (and v.v.).

    analyst_outputs is polymorphic on the ``kind`` column. The
    ``_fetch_root`` SQL filters by both id and kind so a stale
    row_kind URL parameter returns 404 instead of silently mis-typing
    the response.
    """
    _, pg_store = lineage_app
    async with pg_store.acquire() as conn:
        fid = await _insert_analyst_output(
            conn, kind="finding", title="polymorphic_finding",
        )

    # Correct row_kind → 200.
    ok = await client.get(f"/api/v1/lineage/finding/{fid}")
    assert ok.status_code == 200

    # Wrong row_kind (alert) for a finding UUID → 404.
    wrong = await client.get(f"/api/v1/lineage/alert/{fid}")
    assert wrong.status_code == 404
