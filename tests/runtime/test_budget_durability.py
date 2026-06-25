# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A-5 / review-G5 — budget durability.

Three pre-fix failures, each with a regression here:

  * The global envelope was a per-UTC-day row seeded once by a bringup
    script — on any later day the row was absent and the system-wide cap
    silently ceased to exist. Now the enforcer auto-rolls the most recent
    prior bucket forward (materialized, auditable, NULL-cap rows honored
    as an explicit operator "no cap").
  * ``precall_check`` was always called with ``estimated_tokens=0`` — the
    forward-looking throttle outcome was dead code. Now the enforcer
    carries ``estimated_tokens_per_run`` (resolved at deps-build time)
    and the actor passes it.
  * provider/model were a StackRef string + empty string — USD cost was
    always 0 (PRICE_TABLE dispatch never matched).
    :func:`resolve_llm_budget_params` resolves the real pair.

Far-future buckets keep these tests hermetic against the rest of the
session's budget_ledger / envelope rows (sums are per-bucket).
"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import asyncpg
import pytest
import pytest_asyncio

from legba.runtime.analyst_deps_builder import resolve_llm_budget_params
from legba.runtime.budget import BudgetEnforcer


@pytest_asyncio.fixture
async def pg_pool(migrated_pg):
    pool = await asyncpg.create_pool(
        host=migrated_pg.host,
        port=migrated_pg.port,
        user=migrated_pg.user,
        password=migrated_pg.password,
        database=migrated_pg.database,
        min_size=1,
        max_size=4,
    )
    yield pool
    await pool.close()


async def _cleanup(conn: asyncpg.Connection, *, buckets: list[date], analyst_ids: list[str]) -> None:
    await conn.execute(
        "DELETE FROM global_budget_envelope WHERE bucket = ANY($1::date[])",
        buckets,
    )
    if analyst_ids:
        await conn.execute(
            "DELETE FROM budget_ledger WHERE analyst_id = ANY($1::text[])",
            analyst_ids,
        )


# ---------------------------------------------------------------------------
# Envelope rollover
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_global_envelope_rolls_over_to_missing_bucket(pg_pool) -> None:
    seeded = date(2099, 1, 1)
    bucket = date(2099, 1, 2)
    enf = BudgetEnforcer(
        analyst_id="test_rollover_a5",
        analyst_version="aa" * 8,
        budget_tokens_per_day=None,  # isolate the GLOBAL dimension
        provider="openai",
        model="gpt-oss-120b",
    )
    async with pg_pool.acquire() as conn:
        try:
            await conn.execute(
                """
                INSERT INTO global_budget_envelope (bucket, tokens_cap, usd_cap, on_exceeded, note)
                VALUES ($1, 1000, NULL, 'demote_all', 'a5-test seed')
                ON CONFLICT (bucket) DO UPDATE SET tokens_cap = 1000
                """,
                seeded,
            )
            # Burn past the (inherited) cap in the MISSING bucket.
            await enf.record(
                conn, prompt_tokens=600, completion_tokens=500, bucket=bucket,
            )
            decision = await enf.precall_check(conn, bucket=bucket)
            assert decision.outcome == "global_exhausted", decision
            assert decision.global_tokens_cap == 1000

            # The rollover is MATERIALIZED — visible + auditable in the table.
            row = await conn.fetchrow(
                "SELECT tokens_cap, note FROM global_budget_envelope WHERE bucket = $1",
                bucket,
            )
            assert row is not None
            assert int(row["tokens_cap"]) == 1000
            assert "auto-rollover from 2099-01-01" in (row["note"] or "")
        finally:
            await _cleanup(
                conn, buckets=[seeded, bucket], analyst_ids=["test_rollover_a5"],
            )


@pytest.mark.asyncio
async def test_null_cap_row_is_an_explicit_no_cap_and_stops_inheritance(pg_pool) -> None:
    # An operator removes the cap by writing a NULL tokens_cap row: the
    # rollover must honor that (no inheritance reaching past it).
    seeded = date(2098, 1, 1)
    bucket = date(2098, 1, 2)
    enf = BudgetEnforcer(
        analyst_id="test_rollover_nullcap_a5",
        analyst_version="bb" * 8,
        budget_tokens_per_day=None,
        provider="openai",
        model="gpt-oss-120b",
    )
    async with pg_pool.acquire() as conn:
        try:
            await conn.execute(
                """
                INSERT INTO global_budget_envelope (bucket, tokens_cap, usd_cap, on_exceeded, note)
                VALUES ($1, NULL, NULL, 'demote_all', 'a5-test explicit no-cap')
                ON CONFLICT (bucket) DO UPDATE SET tokens_cap = NULL
                """,
                seeded,
            )
            await enf.record(
                conn, prompt_tokens=600, completion_tokens=500, bucket=bucket,
            )
            decision = await enf.precall_check(conn, bucket=bucket)
            assert decision.outcome == "ok", decision
            row = await conn.fetchrow(
                "SELECT 1 FROM global_budget_envelope WHERE bucket = $1", bucket,
            )
            assert row is None, "no row must be materialized past a NULL cap"
        finally:
            await _cleanup(
                conn,
                buckets=[seeded, bucket],
                analyst_ids=["test_rollover_nullcap_a5"],
            )


# ---------------------------------------------------------------------------
# Forward-looking throttle is reachable with a real estimate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_throttle_reachable_with_real_estimate(pg_pool) -> None:
    null_cap_guard = date(2097, 5, 4)   # explicit no-cap → hermetic global leg
    bucket = date(2097, 5, 5)
    enf = BudgetEnforcer(
        analyst_id="test_throttle_a5",
        analyst_version="cc" * 8,
        budget_tokens_per_day=1_000,
        provider="openai",
        model="gpt-oss-120b",
        estimated_tokens_per_run=500,
    )
    assert enf.estimated_tokens_per_run == 500
    async with pg_pool.acquire() as conn:
        try:
            await conn.execute(
                """
                INSERT INTO global_budget_envelope (bucket, tokens_cap, usd_cap, on_exceeded, note)
                VALUES ($1, NULL, NULL, 'demote_all', 'a5-test no-cap guard')
                ON CONFLICT (bucket) DO UPDATE SET tokens_cap = NULL
                """,
                null_cap_guard,
            )
            await enf.record(
                conn, prompt_tokens=400, completion_tokens=200, bucket=bucket,
            )
            # used=600 < 1000, but 600 + 500 projected > 1000 → throttle.
            decision = await enf.precall_check(
                conn,
                estimated_tokens=enf.estimated_tokens_per_run,
                bucket=bucket,
            )
            assert decision.outcome == "throttle", decision
            assert decision.cause == "per_analyst"
            # With no estimate the same state passes — which is exactly why
            # estimate=0 made throttle dead code.
            decision0 = await enf.precall_check(conn, bucket=bucket)
            assert decision0.outcome == "ok"
        finally:
            await _cleanup(
                conn,
                buckets=[null_cap_guard, bucket],
                analyst_ids=["test_throttle_a5"],
            )


# ---------------------------------------------------------------------------
# resolve_llm_budget_params — estimate + provider/model resolution
# ---------------------------------------------------------------------------


def _descriptor_stub(
    *,
    budget_tokens_per_run: int | None = None,
    llm: dict | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        method=SimpleNamespace(
            budget_tokens_per_run=budget_tokens_per_run,
            llm=llm or {},
        ),
    )


@pytest.mark.asyncio
async def test_resolve_budget_params_no_llm_uses_per_run_budget() -> None:
    desc = _descriptor_stub(budget_tokens_per_run=1234)
    provider, model, est = await resolve_llm_budget_params(
        desc, registry_client=None,  # never touched: no LLM ref
    )
    assert (provider, model, est) == ("", "", 1234)


@pytest.mark.asyncio
async def test_resolve_budget_params_falls_back_to_llm_max_tokens() -> None:
    desc = _descriptor_stub(llm={"max_tokens": {"raw": 9000}})
    provider, model, est = await resolve_llm_budget_params(
        desc, registry_client=None,
    )
    assert (provider, model, est) == ("", "", 9000)


@pytest.mark.asyncio
async def test_resolve_budget_params_registry_failure_degrades() -> None:
    class _Boom:
        async def _ensure_client(self):
            raise RuntimeError("registry down")

    desc = _descriptor_stub(
        budget_tokens_per_run=777,
        llm={"primary": {"raw": "llm.primary.openai_compat"}},
    )
    provider, model, est = await resolve_llm_budget_params(
        desc, registry_client=_Boom(),
    )
    assert (provider, model, est) == ("", "", 777)
