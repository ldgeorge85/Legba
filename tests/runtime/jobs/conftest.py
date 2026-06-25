# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Fixtures for the job-plane integration tests (P-07).

Reuses the runtime/data_pkg ``migrated_pg`` fixture (a fresh DB with the full
0001-0024 migration chain, including the source-first ``signals`` table) and
adds:

  * ``job_pg``     — a connected :class:`PostgresStore` on the migrated test DB
                     with the job idempotency ledger ensured.
  * ``job_nats``   — a connected :class:`NatsStore` on the dev-rig NATS.
  * ``job_queue``  — a :class:`JobQueue` with a UNIQUE per-test stream/durable
                     so concurrent test runs don't collide on the shared rig.
"""

from __future__ import annotations

import os
import socket
from uuid import uuid4

import pytest
import pytest_asyncio

os.environ.setdefault("LEGBA_DATA_NATS_URL", "nats://127.0.0.1:4222")

from legba.data.config import NatsConfig, PostgresConfig
from legba.data.nats import NatsStore
from legba.data.postgres import PostgresStore
from legba.data.jobs.store import JobStore
from legba.runtime.jobs.queue import JobQueue


def _port_open(host: str, port: int, timeout: float = 1.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


@pytest_asyncio.fixture()
async def job_pg(migrated_pg: PostgresConfig):
    store = PostgresStore(migrated_pg)
    await store.connect()
    async with store.acquire() as conn:
        await JobStore.ensure_schema(conn)
    yield store
    await store.close()


@pytest_asyncio.fixture()
async def job_nats():
    if not _port_open("127.0.0.1", 4222):
        pytest.skip("dev-rig NATS not reachable on 127.0.0.1:4222")
    store = NatsStore(NatsConfig.from_env())
    await store.connect()
    yield store
    await store.close()


@pytest_asyncio.fixture()
async def job_queue(job_nats: NatsStore):
    suffix = uuid4().hex[:8]
    q = JobQueue(
        job_nats,
        stream=f"LEGBA_JOBS_TEST_{suffix}",
        durable=f"legba-job-workers-test-{suffix}",
        # Per-test subject space: the default ``jobs.>`` would overlap the
        # live runtime's LEGBA_JOBS stream on --network host.
        subject_prefix=f"jobs_test_{suffix}",
        ack_wait_seconds=10,
        max_deliver=4,
        max_age_seconds=600,
    )
    await q.ensure_topology()
    yield q
    # Teardown: delete the per-test stream so the rig stays clean.
    try:
        await job_nats.js.delete_stream(q.stream)
    except Exception:
        pass
