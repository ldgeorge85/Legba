# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for budget enforcement (per legba_runtime_spec.md §5)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
import pytest_asyncio

from legba.runtime.budget import BudgetDecision, BudgetEnforcer


# These tests need the substrate (real budget_ledger table). They share the
# session-scoped migrated_pg fixture from tests/data_pkg/conftest.py via the
# rootdir-level conftest below.


@pytest_asyncio.fixture
async def pg_pool(migrated_pg):
    import asyncpg

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


@pytest.mark.asyncio
async def test_no_budget_configured_passes(pg_pool) -> None:
    enf = BudgetEnforcer(
        analyst_id="test_no_budget",
        analyst_version="ff" * 8,
        budget_tokens_per_day=None,
        provider="openai",
        model="gpt-oss-120b",
    )
    async with pg_pool.acquire() as conn:
        decision = await enf.precall_check(conn, estimated_tokens=1000)
    assert decision.outcome == "ok"
    assert decision.tokens_used_today == 0


@pytest.mark.asyncio
async def test_under_budget_passes(pg_pool) -> None:
    enf = BudgetEnforcer(
        analyst_id="test_under_budget",
        analyst_version="ff" * 8,
        budget_tokens_per_day=10_000,
        provider="openai",
        model="gpt-oss-120b",
    )
    async with pg_pool.acquire() as conn:
        decision = await enf.precall_check(conn, estimated_tokens=500)
    assert decision.outcome == "ok"


@pytest.mark.asyncio
async def test_over_budget_throttles(pg_pool) -> None:
    enf = BudgetEnforcer(
        analyst_id="test_over_budget",
        analyst_version="ff" * 8,
        budget_tokens_per_day=1_000,
        provider="openai",
        model="gpt-oss-120b",
    )
    # Record some usage first.
    async with pg_pool.acquire() as conn:
        await enf.record(conn, prompt_tokens=500, completion_tokens=200)
        decision = await enf.precall_check(conn, estimated_tokens=500)
    # 700 used + 500 estimated = 1200, over budget => throttle.
    assert decision.outcome == "throttle"
    assert decision.tokens_used_today == 700


@pytest.mark.asyncio
async def test_exhausted_returns_exhausted(pg_pool) -> None:
    enf = BudgetEnforcer(
        analyst_id="test_exhausted",
        analyst_version="ff" * 8,
        budget_tokens_per_day=500,
        provider="openai",
        model="gpt-oss-120b",
    )
    async with pg_pool.acquire() as conn:
        # Push usage over the budget.
        await enf.record(conn, prompt_tokens=400, completion_tokens=200)
        decision = await enf.precall_check(conn, estimated_tokens=100)
    assert decision.outcome == "exhausted"


@pytest.mark.asyncio
async def test_record_accumulates(pg_pool) -> None:
    enf = BudgetEnforcer(
        analyst_id="test_accum",
        analyst_version="ff" * 8,
        budget_tokens_per_day=10_000,
        provider="openai",
        model="gpt-oss-120b",
    )
    async with pg_pool.acquire() as conn:
        row1 = await enf.record(conn, prompt_tokens=100, completion_tokens=50)
        row2 = await enf.record(conn, prompt_tokens=200, completion_tokens=80)
    assert row1.tokens_used == 150
    assert row2.tokens_used == 150 + 280
    assert row2.runs == 2
