# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Fixtures for the job-plane integration tests (P-07).

Reuses the runtime/data_pkg ``migrated_pg`` fixture (a fresh DB with the full
0001-0024 migration chain, including the source-first ``signals`` table) and
adds:

  * ``job_pg``     — a connected :class:`PostgresStore` on the migrated test DB
                     with the job idempotency ledger ensured, and the ledger
                     rows the test writes RETIRED on teardown (scoped to
                     exactly those rows — see :func:`job_store_scope`, which
                     is the rooted fix for two nightly allowlist entries).
  * ``job_nats``   — a connected :class:`NatsStore` on the dev-rig NATS.
  * ``job_queue``  — a :class:`JobQueue` with a UNIQUE per-test stream/durable
                     so concurrent test runs don't collide on the shared rig.
"""

from __future__ import annotations

import os
import socket
from contextlib import asynccontextmanager
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


@asynccontextmanager
async def job_store_scope(cfg: PostgresConfig):
    """A connected store on the migrated DB, with the ledger rows the caller
    writes RETIRED on exit — scoped to exactly those rows.

    THE ROOTED CAUSE OF TWO NIGHTLY ALLOWLIST ENTRIES (task #23).
    ``public.legba_jobs`` is ONE table shared by the whole single-process
    suite: ``migrated_pg`` is session-scoped, so every test in this package
    reads and writes the same ledger. Several tests here deliberately end with
    a live ``claimed`` row — ``test_reaper_leaves_fresh_claims_alone`` (that IS
    its subject), ``test_nak_uses_delay_when_sibling_holds_claim``,
    ``test_claim_reclaims_a_released_row_in_place``,
    ``test_reaper_releases_stale_claim_with_budget_remaining`` and
    ``test_idempotent_claim_is_concurrency_safe``. Nothing removed them again.

    ``JobStore.reap_stale_claims`` is table-wide by design (a reaper that only
    swept its own worker's rows would not be a reaper), so a leaked ``claimed``
    row becomes reapable the moment it is older than one lease —
    ``ack_wait x max_deliver`` = 10 x 4 = **40 seconds**. Two tests state their
    result as a GLOBAL count over that sweep:

        test_jobs_plane_hardening::test_failed_reap_is_not_reenqueued
        test_worker_reaper_backoff::test_worker_loop_reaps_due_stale_claim

    ``worker.reaped == 1`` and ``queue.enqueued == []`` are true only while the
    ledger holds nothing but the test's own row.

    WHY IT WAS A LONG-PERIOD FAILURE. Every test in this package runs in ~20 ms
    (``--durations=0``), so in file order the leak is never 40 s old by the
    time a victim sweeps — the ordered phase is always green. pytest-randomly
    sorts MODULES by ``crc32(f"{seed}::{module.__name__}")`` with no regard for
    package or directory, so under a shuffled seed these five modules are
    scattered through a ~16-minute, 10k-test session and minutes separate a
    leaking module from a victim. That is why the entries went quiet for three
    nights in the 2026-08-09 sweep and refired on 08-11/14/15.

    Reproduce the pre-fix failure (a spacer stands in for the shuffle's gap)::

        # tests/runtime/jobs/spacer.py: def test_gap(): time.sleep(45)
        bash scripts/run_tests_in_container.sh \\
          tests/runtime/jobs/test_worker_reaper_backoff.py::test_reaper_leaves_fresh_claims_alone \\
          tests/runtime/jobs/spacer.py \\
          tests/runtime/jobs/test_jobs_plane_hardening.py::test_failed_reap_is_not_reenqueued \\
          -p no:randomly
        # -> assert 2 == 1, and the captured log names both reaped keys.

    SCOPED, NOT A WIPE: the snapshot is taken at setup and only keys absent
    from it are removed, so a row written outside this scope survives. The
    regression pin is ``test_ledger_scope_isolation.py``, which drives this
    context manager twice in one test and asserts both halves of that.

    The retirement runs on the FAILURE path too. A test that dies half way
    through leaves the messiest ledger of all, and letting that cascade into
    the next module's reap count is how one real failure becomes three
    mysterious ones.
    """
    store = PostgresStore(cfg)
    await store.connect()
    try:
        async with store.acquire() as conn:
            await JobStore.ensure_schema(conn)
            before = [
                r["idempotency_key"]
                for r in await conn.fetch(
                    "SELECT idempotency_key FROM public.legba_jobs"
                )
            ]
        try:
            yield store
        finally:
            async with store.acquire() as conn:
                await conn.execute(
                    "DELETE FROM public.legba_jobs "
                    "WHERE idempotency_key <> ALL($1::text[])",
                    before,
                )
    finally:
        await store.close()


@pytest_asyncio.fixture()
async def job_pg(migrated_pg: PostgresConfig):
    async with job_store_scope(migrated_pg) as store:
        yield store


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
