# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Realistic lineage walk benchmark.

Not auto-collected by pytest (underscore prefix). Run with:

    pytest tests/data_pkg/_bench_lineage_walk.py::test_bench_walk -s

Inserts a 4-table chain (signal → 10 findings → 5 situations) and times
upstream + downstream depth=3 walks end-to-end through the FastAPI app.
"""
from __future__ import annotations

import os
import time
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from legba.data.config import PostgresConfig
from legba.data.postgres import PostgresStore
from legba.data.registry.api import API_TOKEN_ENV, RegistryAPIDeps
from legba.data.registry.lineage_api import build_lineage_router


class _MinimalDescriptorRegistry:
    def __init__(self, pg): self.pg = pg


@pytest.mark.integration
@pytest.mark.asyncio
async def test_bench_walk(migrated_pg: PostgresConfig, capsys):
    os.environ.pop(API_TOKEN_ENV, None)
    pg = PostgresStore(migrated_pg)
    await pg.connect()
    try:
        async with pg.acquire() as conn:
            sig_root = uuid4()
            await conn.execute(
                """INSERT INTO signals (id, data, title, target_id,
                       target_version, produced_at, derived_from, schema_uri)
                   VALUES ($1, '{}'::jsonb, 'root_sig', 'br_energy', 'tv',
                       NOW(), '{}'::uuid[],
                       'iglu:legba/signal/jsonschema/2-0-0')""",
                sig_root,
            )
            findings: list[UUID] = []
            for i in range(10):
                fid = uuid4()
                await conn.execute(
                    """INSERT INTO analyst_outputs
                       (id, kind, title, body, analyst_id, analyst_version,
                        produced_at, derived_from, schema_uri)
                       VALUES ($1, 'finding', $2, '', 'an_x', 'av',
                           NOW(), $3::uuid[],
                           'iglu:legba/finding/jsonschema/1-0-0')""",
                    fid, f"finding_{i}", [sig_root],
                )
                findings.append(fid)
            situations: list[UUID] = []
            for j, fid in enumerate(findings[:5]):
                sid = uuid4()
                await conn.execute(
                    """INSERT INTO situations
                         (id, data, name, target_id, target_version,
                          analyst_id, analyst_version, produced_at,
                          derived_from, schema_uri)
                       VALUES ($1, '{}'::jsonb, $2, 'br_energy', 'tv',
                           'an_situ', 'av', NOW(), $3::uuid[],
                           'iglu:legba/situation/jsonschema/2-0-0')""",
                    sid, f"situ_{j}", [fid],
                )
                situations.append(sid)
        deps = RegistryAPIDeps(
            descriptor_registry=_MinimalDescriptorRegistry(pg),  # type: ignore[arg-type]
            stack_registry=None, vault=None, dlq=None,  # type: ignore[arg-type]
            audit_logger=None, vocabulary_cache=None,  # type: ignore[arg-type]
            nats_store=None, conversion_registry=None,
        )
        app = FastAPI()
        app.state.registry_deps = deps
        app.include_router(build_lineage_router(deps), prefix="/api/v1")
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://x",
        ) as cli:
            # Warm up.
            await cli.get(
                f"/api/v1/lineage/signal/{sig_root}",
                params={"direction": "downstream", "depth": 3},
            )
            # downstream from root
            samples = []
            for _ in range(7):
                t0 = time.perf_counter()
                r = await cli.get(
                    f"/api/v1/lineage/signal/{sig_root}",
                    params={"direction": "downstream", "depth": 3},
                )
                samples.append((time.perf_counter() - t0) * 1000)
                assert r.status_code == 200
            body = r.json()
            with capsys.disabled():
                print(
                    f"\n[bench] downstream depth=3 from sig_root: "
                    f"nodes={len(body['nodes'])} "
                    f"edges={len(body['edges'])} "
                    f"truncated={body['truncated_at_depth']}"
                )
                print(
                    f"[bench] samples_ms="
                    f"{[f'{s:.1f}' for s in samples]} "
                    f"median={sorted(samples)[len(samples)//2]:.1f}ms"
                )
            # upstream from a situation leaf
            leaf = situations[0]
            samples = []
            for _ in range(7):
                t0 = time.perf_counter()
                r = await cli.get(
                    f"/api/v1/lineage/situation/{leaf}",
                    params={"direction": "upstream", "depth": 3},
                )
                samples.append((time.perf_counter() - t0) * 1000)
                assert r.status_code == 200
            body = r.json()
            with capsys.disabled():
                print(
                    f"[bench] upstream depth=3 from situ_leaf: "
                    f"nodes={len(body['nodes'])} "
                    f"edges={len(body['edges'])} "
                    f"truncated={body['truncated_at_depth']}"
                )
                print(
                    f"[bench] samples_ms="
                    f"{[f'{s:.1f}' for s in samples]} "
                    f"median={sorted(samples)[len(samples)//2]:.1f}ms"
                )
    finally:
        await pg.close()
