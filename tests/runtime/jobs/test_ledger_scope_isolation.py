# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Task #23 — the regression pin for the jobs-plane ledger's test isolation.

``public.legba_jobs`` is ONE table shared by the whole single-process suite
(``migrated_pg`` is session-scoped), and ``JobStore.reap_stale_claims`` sweeps
it table-wide. Two tests in this package state their result as a global count
over that sweep — ``test_failed_reap_is_not_reenqueued`` and
``test_worker_loop_reaps_due_stale_claim`` assert ``worker.reaped == 1`` — so a
sibling that leaves a live ``claimed`` row behind breaks them as soon as that
row is older than one lease (40 s). Five tests here leave exactly such a row on
purpose; ``job_store_scope`` retires them. Both of those entries sat on
``scripts/host_nightly_suite.sh``'s KNOWN_SHARED_STATE list until it did.

Why THIS pin and not an ordering-pinned pair: the leak only bites after 40 s of
wall clock, and every test in this package runs in ~20 ms — a two-test
invocation can never demonstrate it, which is precisely why the failure was
invisible in the nightly's ordered phase and surfaced only under seeds whose
global module shuffle put minutes between the modules. Driving
``job_store_scope`` twice inside ONE test removes the clock from the question
and pins the actual contract instead: a scope retires what it wrote, and only
what it wrote. It fails deterministically, in any order, on any seed.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from legba.data.config import PostgresConfig
from legba.data.jobs.envelope import JobEnvelope
from legba.data.jobs.store import JobStore
from legba.data.postgres import PostgresStore

from tests.runtime.jobs.conftest import job_store_scope


def _envelope() -> JobEnvelope:
    return JobEnvelope(
        job_kind="process_media",
        input_refs={"signal_id": str(uuid4()), "extraction": "transcribe"},
        idempotency_key=f"scope-pin-{uuid4().hex}",
        attempts=4,
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_job_scope_retires_its_own_claimed_rows_and_only_its_own(
    migrated_pg: PostgresConfig,
):
    """A ``job_store_scope`` retires the ledger rows written inside it — and
    leaves a row written outside it alone.

    The abandoned claim below is exactly what
    ``test_reaper_leaves_fresh_claims_alone`` (and four siblings) end with: the
    row IS the subject of those tests, so they cannot stop creating it. What
    they must not do is hand it to whichever module the shuffle runs next.
    """
    outsider = _envelope()   # stands in for a row this scope did not write
    leaked = _envelope()     # the abandoned claim a jobs test legitimately ends on

    probe = PostgresStore(migrated_pg)
    await probe.connect()
    try:
        # A ledger row that predates the scope under test.
        async with probe.acquire() as conn:
            await JobStore.ensure_schema(conn)
            assert (await JobStore.claim(conn, outsider)).acquired

        # One scope's worth of work: claim a key and walk away from it.
        async with job_store_scope(migrated_pg) as scoped:
            async with scoped.acquire() as conn:
                assert (await JobStore.claim(conn, leaked)).acquired
                inside = await conn.fetchval(
                    "SELECT status FROM public.legba_jobs "
                    "WHERE idempotency_key = $1",
                    leaked.idempotency_key,
                )
            assert inside == "claimed", (
                "the pin is only meaningful if the leak really happens inside "
                "the scope"
            )

        # On exit: the leak is gone, the outsider's row is untouched.
        async with probe.acquire() as conn:
            survivors = {
                r["idempotency_key"]
                for r in await conn.fetch(
                    "SELECT idempotency_key FROM public.legba_jobs "
                    "WHERE idempotency_key = ANY($1::text[])",
                    [outsider.idempotency_key, leaked.idempotency_key],
                )
            }
        assert leaked.idempotency_key not in survivors, (
            "a job_pg scope left a live claimed row in the session-shared "
            "ledger — reap_stale_claims sweeps table-wide, so the next module "
            "to assert `worker.reaped == 1` will read 2 (task #23)"
        )
        assert outsider.idempotency_key in survivors, (
            "the scope's cleanup is a WIPE, not a scoped retirement — it "
            "deleted a row it never wrote"
        )
    finally:
        async with probe.acquire() as conn:
            await conn.execute(
                "DELETE FROM public.legba_jobs WHERE idempotency_key = $1",
                outsider.idempotency_key,
            )
        await probe.close()
