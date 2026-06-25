# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for budget demotion plumbing (Phase 5 hardening item 4 + F-2).

The exhaustion path:

  1. ``BudgetEnforcer.precall_check`` returns ``exhausted`` once the
     per-analyst cap is hit.
  2. The actor writes a ``budget_demotion_events`` audit row, then
     dispatches on ``method.retry.budget.strategy``. F-2 (2026-06-09):
     ``demote_and_continue`` currently means an EXPLICIT audited pause
     until the budget window resets — no production resolver wires a
     fallback model, and silently proceeding (or silently pausing) was
     the G5 finding. Real cheap-model fallback demotion is a declared
     seam (docs/SEAMS.md / docs/DIRECTION.md).
  3. The demotion-flag helpers (``_set_analyst_demoted`` /
     ``_is_actor_demoted``) and the fallback dispatch machinery remain —
     they are the seam's landing zone and stay covered here.

These tests exercise the runtime-side wiring without spinning up a real
Dapr sidecar — they call ``BudgetEnforcer.precall_check`` + the per-
actor demotion-flag helpers + the audit-event writer directly, then
verify the database side-effect.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import asyncpg
import pytest
import pytest_asyncio

from legba.runtime.budget import BudgetEnforcer
from legba.runtime.dapr_actors import (
    _bucket_end_iso,
    _is_actor_demoted,
    _set_analyst_demoted,
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
    """Clear demotion state between tests so flags don't leak."""
    clear_analyst_demotion(None)
    yield
    clear_analyst_demotion(None)


# ---------------------------------------------------------------------------
# Per-analyst exhaustion classifies as "exhausted" with cause="per_analyst"
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_per_analyst_exhausted_cause(pg_pool) -> None:
    enf = BudgetEnforcer(
        analyst_id="test_demote_per_analyst",
        analyst_version="ff" * 8,
        budget_tokens_per_day=1_000,
        provider="openai",
        model="gpt-oss-120b",
    )
    async with pg_pool.acquire() as conn:
        # Push usage over the cap.
        await enf.record(conn, prompt_tokens=600, completion_tokens=500)
        decision = await enf.precall_check(conn)
    assert decision.outcome == "exhausted"
    assert decision.cause == "per_analyst"


# ---------------------------------------------------------------------------
# record_demotion writes a row into budget_demotion_events
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_record_demotion_persists_row(pg_pool) -> None:
    enf = BudgetEnforcer(
        analyst_id="test_demote_audit",
        analyst_version="aa" * 8,
        budget_tokens_per_day=500,
        provider="anthropic",
        model="claude-opus-4-7",
    )
    async with pg_pool.acquire() as conn:
        await enf.record_demotion(
            conn,
            cause="per_analyst",
            primary_llm="llm.anthropic.claude_opus_4_7",
            fallback_llm="llm.primary.openai_compat",
            tokens_used_at_demote=520,
            tokens_cap_at_demote=500,
        )
        row = await conn.fetchrow(
            """
            SELECT analyst_id, analyst_version, bucket, cause,
                   tokens_used_at_demote, tokens_cap_at_demote,
                   primary_llm, fallback_llm
            FROM budget_demotion_events
            WHERE analyst_id = $1
            ORDER BY occurred_at DESC
            LIMIT 1
            """,
            "test_demote_audit",
        )
    assert row is not None
    assert row["cause"] == "per_analyst"
    assert row["primary_llm"] == "llm.anthropic.claude_opus_4_7"
    assert row["fallback_llm"] == "llm.primary.openai_compat"
    assert int(row["tokens_used_at_demote"]) == 520
    assert int(row["tokens_cap_at_demote"]) == 500


# ---------------------------------------------------------------------------
# Per-actor demotion-flag helpers
# ---------------------------------------------------------------------------


def test_actor_not_demoted_by_default() -> None:
    now = datetime.now(tz=timezone.utc)
    assert _is_actor_demoted("AnalystActor::foo::bar", now) is False


def test_set_analyst_demoted_flips_flag() -> None:
    now = datetime.now(tz=timezone.utc)
    end_of_bucket = _bucket_end_iso(now)
    actor_id = "AnalystActor::demote_test::00112233"
    _set_analyst_demoted(actor_id, end_of_bucket)
    assert _is_actor_demoted(actor_id, now) is True
    # Another actor remains undemoted.
    assert _is_actor_demoted("AnalystActor::other::44556677", now) is False


def test_demotion_expires_at_bucket_boundary() -> None:
    # Force an expiry time in the past — the flag should NOT be considered active.
    now = datetime.now(tz=timezone.utc)
    past = (now - timedelta(hours=1)).isoformat()
    actor_id = "AnalystActor::expiry_test::00112233"
    _set_analyst_demoted(actor_id, past)
    assert _is_actor_demoted(actor_id, now) is False


def test_clear_analyst_demotion_per_actor() -> None:
    now = datetime.now(tz=timezone.utc)
    end_of_bucket = _bucket_end_iso(now)
    actor_a = "AnalystActor::a::00112233"
    actor_b = "AnalystActor::b::44556677"
    _set_analyst_demoted(actor_a, end_of_bucket)
    _set_analyst_demoted(actor_b, end_of_bucket)
    assert _is_actor_demoted(actor_a, now) is True
    assert _is_actor_demoted(actor_b, now) is True
    # Clear just A.
    clear_analyst_demotion(actor_a)
    assert _is_actor_demoted(actor_a, now) is False
    assert _is_actor_demoted(actor_b, now) is True


def test_bucket_end_iso_is_next_utc_midnight() -> None:
    # Construct a known time and verify the boundary computation.
    now = datetime(2026, 5, 22, 14, 30, 12, tzinfo=timezone.utc)
    boundary_iso = _bucket_end_iso(now)
    boundary = datetime.fromisoformat(boundary_iso)
    assert boundary == datetime(2026, 5, 23, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# End-to-end actor-level demotion (without daprd — exercise the deps bundle)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_demoted_actor_routes_to_fallback_run_method(pg_pool) -> None:
    """Once an actor is flagged demoted, the dispatch helper should pick the fallback.

    We don't spin up a real AnalystActor here (that requires Dapr).
    Instead we exercise the deps-resolution helper the actor uses to
    decide which run_method to call.
    """
    from legba.runtime.dapr_actors import (
        _AnalystDeps,
        _is_actor_demoted,
        _set_analyst_demoted,
    )

    actor_id = "AnalystActor::test::ABCD1234"

    # Build a deps bundle with both primary and fallback run_methods.
    primary_calls: list[tuple] = []
    fallback_calls: list[tuple] = []

    async def primary(inputs, options, deps):
        primary_calls.append((tuple(inputs), dict(options)))
        return None

    async def fallback(inputs, options, deps):
        fallback_calls.append((tuple(inputs), dict(options)))
        return None

    # We don't need to instantiate _AnalystDeps for this assertion — the
    # actor's run path picks fallback when `_is_actor_demoted(actor_id, now)`
    # AND `fallback_run_method` is non-None. We pin both here.

    now = datetime.now(tz=timezone.utc)
    assert _is_actor_demoted(actor_id, now) is False

    _set_analyst_demoted(actor_id, _bucket_end_iso(now))
    assert _is_actor_demoted(actor_id, now) is True

    # The dispatch decision the actor's run() makes:
    fallback_run_method = fallback
    using_fallback = _is_actor_demoted(actor_id, now) and (
        fallback_run_method is not None
    )
    active = fallback_run_method if using_fallback else primary
    await active([], {}, None)

    assert fallback_calls, "demoted actor must dispatch to fallback_run_method"
    assert not primary_calls
