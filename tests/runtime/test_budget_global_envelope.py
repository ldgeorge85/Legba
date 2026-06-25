# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the system-wide budget envelope (Phase 5 hardening item 4).

The global envelope sits in ``global_budget_envelope(bucket, tokens_cap,
usd_cap, on_exceeded, ...)`` — one row per bucket. When the sum of all
analysts' ``budget_ledger.tokens_used`` crosses the cap,
``BudgetEnforcer.precall_check`` returns ``global_exhausted`` with
``cause="global"``. The runtime then flips the process-global
``_GLOBAL_DEMOTED_UNTIL`` flag for the remainder of the bucket window
so EVERY analyst auto-demotes.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import asyncpg
import pytest
import pytest_asyncio

from legba.runtime.budget import BudgetEnforcer
from legba.runtime.dapr_actors import (
    _bucket_end_iso,
    _is_actor_demoted,
    _set_global_demoted,
    clear_analyst_demotion,
)


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


@pytest.fixture(autouse=True)
def _cleanup_demotion_state():
    clear_analyst_demotion(None)
    yield
    clear_analyst_demotion(None)


@pytest_asyncio.fixture(autouse=True)
async def _cleanup_global_envelope(pg_pool):
    """Drop the global_budget_envelope + budget_ledger rows this module's
    tests create, BEFORE and AFTER each test.

    Test isolation (B4): these tests share the session-scoped ``migrated_pg``
    DB with the rest of the budget suite. A ``global_budget_envelope`` row
    seeded here for ``bucket=today`` leaks into sibling tests
    (e.g. ``test_budget_auto_demote::test_per_analyst_exhausted_cause``),
    which then see the global cap and report ``global_exhausted`` instead of
    the per-analyst ``exhausted`` they assert. Scrubbing the bucket-`today`
    rows at the test boundary keeps each test's view of the budget tables
    to exactly what it sets up, without changing any assertion.
    """
    async def _scrub() -> None:
        bucket = _today_utc()
        async with pg_pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM global_budget_envelope WHERE bucket = $1", bucket
            )
            await conn.execute(
                "DELETE FROM budget_ledger WHERE bucket = $1", bucket
            )

    await _scrub()
    yield
    await _scrub()


async def _set_global_cap(
    pool: asyncpg.Pool,
    *,
    bucket: date,
    tokens_cap: int | None,
    usd_cap: Decimal | None = None,
    on_exceeded: str = "demote_all",
) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO global_budget_envelope (bucket, tokens_cap, usd_cap, on_exceeded)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (bucket) DO UPDATE
            SET tokens_cap = EXCLUDED.tokens_cap,
                usd_cap = EXCLUDED.usd_cap,
                on_exceeded = EXCLUDED.on_exceeded,
                last_updated = NOW()
            """,
            bucket,
            tokens_cap,
            usd_cap,
            on_exceeded,
        )


def _today_utc() -> date:
    return datetime.now(tz=timezone.utc).date()


# ---------------------------------------------------------------------------
# Global envelope reads correctly when set
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_global_envelope_unset_passes(pg_pool) -> None:
    """No envelope row → decision is ok (per-analyst checks still apply)."""
    # Ensure no row exists.
    async with pg_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM global_budget_envelope WHERE bucket = $1",
            _today_utc(),
        )
    enf = BudgetEnforcer(
        analyst_id="test_global_unset",
        analyst_version="11" * 8,
        budget_tokens_per_day=10_000,
        provider="openai",
        model="gpt-oss-120b",
    )
    async with pg_pool.acquire() as conn:
        decision = await enf.precall_check(conn)
    assert decision.outcome == "ok"
    assert decision.global_tokens_cap is None


@pytest.mark.asyncio
async def test_global_envelope_set_and_under(pg_pool) -> None:
    """Envelope set but unfilled — decision is ok, but cap is reported."""
    bucket = _today_utc()
    await _set_global_cap(pg_pool, bucket=bucket, tokens_cap=1_000_000)

    # Clean any prior usage to keep the global counter low.
    async with pg_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM budget_ledger WHERE bucket = $1", bucket
        )

    enf = BudgetEnforcer(
        analyst_id="test_global_under",
        analyst_version="22" * 8,
        budget_tokens_per_day=100_000,
        provider="openai",
        model="gpt-oss-120b",
    )
    async with pg_pool.acquire() as conn:
        await enf.record(conn, prompt_tokens=100, completion_tokens=100)
        decision = await enf.precall_check(conn)
    assert decision.outcome == "ok"
    assert decision.global_tokens_cap == 1_000_000


# ---------------------------------------------------------------------------
# Crossing the global cap — outcome is global_exhausted
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_global_envelope_exhausted(pg_pool) -> None:
    """Sum across analysts crosses the global cap → outcome=global_exhausted."""
    bucket = _today_utc()
    # Reset the bucket to known state.
    async with pg_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM budget_ledger WHERE bucket = $1", bucket
        )

    await _set_global_cap(pg_pool, bucket=bucket, tokens_cap=2_000)

    # Two analysts together push global past the cap.
    enf_a = BudgetEnforcer(
        analyst_id="test_global_a",
        analyst_version="33" * 8,
        budget_tokens_per_day=10_000,  # well under per-analyst cap
        provider="openai",
        model="gpt-oss-120b",
    )
    enf_b = BudgetEnforcer(
        analyst_id="test_global_b",
        analyst_version="44" * 8,
        budget_tokens_per_day=10_000,
        provider="openai",
        model="gpt-oss-120b",
    )
    async with pg_pool.acquire() as conn:
        await enf_a.record(conn, prompt_tokens=600, completion_tokens=600)
        await enf_b.record(conn, prompt_tokens=500, completion_tokens=500)

    async with pg_pool.acquire() as conn:
        # Third analyst's pre-call check sees the global cap exhausted
        # even though its own ledger row is empty.
        enf_c = BudgetEnforcer(
            analyst_id="test_global_c",
            analyst_version="55" * 8,
            budget_tokens_per_day=10_000,
            provider="openai",
            model="gpt-oss-120b",
        )
        decision = await enf_c.precall_check(conn)

    assert decision.outcome == "global_exhausted"
    assert decision.cause == "global"
    assert decision.global_tokens_cap == 2_000
    # Global usage = 1200 (A) + 1000 (B) = 2200, which is >= 2000.
    assert decision.global_tokens_used is not None
    assert decision.global_tokens_used >= 2_000


# ---------------------------------------------------------------------------
# Global-demote flag affects every actor
# ---------------------------------------------------------------------------


def test_global_demote_flag_demotes_all_actors() -> None:
    """When _GLOBAL_DEMOTED_UNTIL is set, every actor reads as demoted."""
    now = datetime.now(tz=timezone.utc)
    until = _bucket_end_iso(now)
    _set_global_demoted(until)
    # Two arbitrary actor_ids — both should read as demoted via the
    # global flag (no per-actor mark needed).
    assert _is_actor_demoted("AnalystActor::analyst_a::00112233", now) is True
    assert _is_actor_demoted("AnalystActor::analyst_b::44556677", now) is True


def test_global_demote_clears_when_None() -> None:
    now = datetime.now(tz=timezone.utc)
    until = _bucket_end_iso(now)
    _set_global_demoted(until)
    assert _is_actor_demoted("AnalystActor::analyst_a::00112233", now) is True
    _set_global_demoted(None)
    assert _is_actor_demoted("AnalystActor::analyst_a::00112233", now) is False


def test_global_demote_expires_at_boundary() -> None:
    now = datetime.now(tz=timezone.utc)
    past = (now - timedelta(hours=1)).isoformat()
    _set_global_demoted(past)
    # Past boundary — not demoted anymore.
    assert _is_actor_demoted("AnalystActor::analyst_a::00112233", now) is False


# ---------------------------------------------------------------------------
# Global throttle (projected over) still surfaces as outcome=throttle
# but with cause=global so the runtime can differentiate.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_global_projected_throttle(pg_pool) -> None:
    bucket = _today_utc()
    async with pg_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM budget_ledger WHERE bucket = $1", bucket
        )

    await _set_global_cap(pg_pool, bucket=bucket, tokens_cap=2_000)

    enf = BudgetEnforcer(
        analyst_id="test_global_throttle",
        analyst_version="66" * 8,
        budget_tokens_per_day=10_000,
        provider="openai",
        model="gpt-oss-120b",
    )
    async with pg_pool.acquire() as conn:
        # Record 1900 of 2000 — under, but estimate=200 would push over.
        await enf.record(conn, prompt_tokens=1000, completion_tokens=900)
        decision = await enf.precall_check(conn, estimated_tokens=200)

    # 1900 used (under 2000 cap), 200 estimate would push to 2100 → throttle.
    assert decision.outcome == "throttle"
    assert decision.cause == "global"
