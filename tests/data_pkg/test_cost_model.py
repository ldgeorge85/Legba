# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""L-245 cost-model tests.

Covers:
  * Unit: `compute_cost_usd` dispatches per-provider against PRICE_TABLE and
    returns Decimal('0') for self-hosted vLLM (empty table) + unknown
    providers.
  * Integration: migration 0015 applies (idempotently) and the
    `cost_estimate_usd` column is present on `budget_ledger`.
  * Integration: `record_budget` populates `cost_estimate_usd` correctly
    for Anthropic (non-zero) and vLLM (0) without breaking the existing
    `cost_usd` column or other budget_ledger columns.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import asyncpg
import pytest

from legba.data.config import PostgresConfig
from legba.data.migrate import apply_primary_migrations
from legba.data.provenance import (
    BudgetLedgerRow,
    compute_cost_usd,
    record_budget,
)
from legba.data.stack.llm.anthropic import AnthropicProviderHandler
from legba.data.stack.llm.openai import OpenAIProviderHandler


# ---------------------------------------------------------------------------
# Unit — compute_cost_usd
# ---------------------------------------------------------------------------


def test_compute_cost_anthropic_priced_model_non_zero():
    # Anthropic claude-opus-4-7: input 15 / output 75 per 1M tokens.
    # 1M prompt + 1M completion = $15 + $75 = $90.
    cost = compute_cost_usd(
        provider="anthropic",
        model="claude-opus-4-7",
        prompt_tokens=1_000_000,
        completion_tokens=1_000_000,
    )
    assert isinstance(cost, Decimal)
    assert cost == Decimal("90.000000")


def test_compute_cost_anthropic_small_token_count_quantized():
    # 1000 prompt tokens of claude-opus-4-7 @ $15/M = $0.015.
    cost = compute_cost_usd(
        provider="anthropic",
        model="claude-opus-4-7",
        prompt_tokens=1000,
        completion_tokens=0,
    )
    assert cost == Decimal("0.015000")


def test_compute_cost_anthropic_prefix_match():
    # claude-opus-4-7-20260301 should resolve via prefix-match to the
    # claude-opus-4-7 entry (estimate_cost uses the same logic).
    cost = compute_cost_usd(
        provider="anthropic",
        model="claude-opus-4-7-20260301",
        prompt_tokens=1_000_000,
        completion_tokens=0,
    )
    assert cost == Decimal("15.000000")


def test_compute_cost_anthropic_cache_tokens():
    # cache_read 1.50 per 1M, cache_write 18.75 per 1M for claude-opus-4-7.
    cost = compute_cost_usd(
        provider="anthropic",
        model="claude-opus-4-7",
        prompt_tokens=0,
        completion_tokens=0,
        cache_read_tokens=2_000_000,
        cache_write_tokens=1_000_000,
    )
    # 2 * 1.50 + 1 * 18.75 = 3.00 + 18.75 = 21.75
    assert cost == Decimal("21.750000")


def test_compute_cost_openai_reasoning_tokens():
    # OpenAI o3: input 30 / output 120 / reasoning 120 per 1M tokens.
    # 100k prompt + 100k completion + 100k reasoning =
    #   0.1 * 30 + 0.1 * 120 + 0.1 * 120 = 3 + 12 + 12 = 27
    cost = compute_cost_usd(
        provider="openai",
        model="o3",
        prompt_tokens=100_000,
        completion_tokens=100_000,
        reasoning_tokens=100_000,
    )
    assert cost == Decimal("27.000000")


def test_compute_cost_vllm_zero_priced_returns_zero():
    # vLLM PRICE_TABLE is empty by design (self-hosted).
    cost = compute_cost_usd(
        provider="vllm",
        model="meta-llama/Llama-3.1-70B-Instruct",
        prompt_tokens=10_000_000,
        completion_tokens=10_000_000,
    )
    assert cost == Decimal("0")


def test_compute_cost_unknown_provider_returns_zero():
    cost = compute_cost_usd(
        provider="not-a-real-provider",
        model="claude-opus-4-7",
        prompt_tokens=1_000_000,
        completion_tokens=1_000_000,
    )
    assert cost == Decimal("0")


def test_compute_cost_unpriced_model_returns_zero():
    # Anthropic provider but a model not in its PRICE_TABLE (and no prefix
    # match) — falls through to Decimal('0').
    cost = compute_cost_usd(
        provider="anthropic",
        model="totally-fake-model-xyz",
        prompt_tokens=1_000_000,
        completion_tokens=1_000_000,
    )
    assert cost == Decimal("0")


def test_anthropic_price_table_populated():
    """Sanity: PRICE_TABLE keys we depend on for tests exist."""
    assert "claude-opus-4-7" in AnthropicProviderHandler.PRICE_TABLE
    assert "claude-sonnet-4-6" in AnthropicProviderHandler.PRICE_TABLE


def test_openai_price_table_populated():
    assert "gpt-5" in OpenAIProviderHandler.PRICE_TABLE
    assert "gpt-4.1" in OpenAIProviderHandler.PRICE_TABLE
    assert "o3" in OpenAIProviderHandler.PRICE_TABLE


# ---------------------------------------------------------------------------
# Integration — migration shape
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_budget_ledger_has_cost_estimate_column(migrated_pg: PostgresConfig):
    """Migration 0015 added `cost_estimate_usd` to `budget_ledger`."""
    conn = await asyncpg.connect(migrated_pg.dsn)
    try:
        rows = await conn.fetch(
            """
            SELECT column_name, data_type, is_nullable, column_default,
                   numeric_precision, numeric_scale
            FROM information_schema.columns
            WHERE table_name = 'budget_ledger'
            ORDER BY ordinal_position
            """
        )
        cols = {r["column_name"]: r for r in rows}
        assert "cost_estimate_usd" in cols, (
            f"cost_estimate_usd missing from budget_ledger; "
            f"present cols: {list(cols)}"
        )
        col = cols["cost_estimate_usd"]
        assert col["data_type"] == "numeric"
        assert col["numeric_precision"] == 12
        assert col["numeric_scale"] == 6
        assert col["is_nullable"] == "NO"
        # Default = 0 (different PG versions render it slightly differently;
        # just assert non-null).
        assert col["column_default"] is not None
    finally:
        await conn.close()


# test_migration_0015_recorded_in_ledger removed: the 30-migration chain was
# flattened to 0001_baseline.sql (clean-slate release). The cost_model schema is
# verified by test_migration_0015 above; baseline-in-ledger by test_migrations.py.


@pytest.mark.integration
async def test_migration_0015_idempotent(migrated_pg: PostgresConfig):
    """Re-running apply_primary_migrations is a no-op for 0015 — the ledger
    skips already-applied files and the ALTER TABLE IF NOT EXISTS is a
    belt-and-braces second line of defense."""
    applied = await apply_primary_migrations(migrated_pg)
    # Should be []; the session-scoped migrated_pg fixture already ran them.
    assert "0015_cost_model.sql" not in applied

    # Column still present, single instance.
    conn = await asyncpg.connect(migrated_pg.dsn)
    try:
        count = await conn.fetchval(
            """
            SELECT COUNT(*) FROM information_schema.columns
            WHERE table_name = 'budget_ledger'
              AND column_name = 'cost_estimate_usd'
            """
        )
        assert count == 1
    finally:
        await conn.close()


# ---------------------------------------------------------------------------
# Integration — record_budget end-to-end
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_record_budget_anthropic_populates_cost_estimate(
    migrated_pg: PostgresConfig,
):
    """Anthropic call with a priced model → cost_estimate_usd > 0."""
    conn = await asyncpg.connect(migrated_pg.dsn)
    try:
        row: BudgetLedgerRow = await record_budget(
            conn,
            analyst_id="test_l245_anthropic",
            analyst_version="v1",
            provider="anthropic",
            model="claude-sonnet-4-6",
            # sonnet 4-6: $3/M input, $15/M output
            # 200k input + 50k output = 0.6 + 0.75 = 1.35
            prompt_tokens=200_000,
            completion_tokens=50_000,
            bucket=date(2026, 5, 20),
        )
        assert row.tokens_used == 250_000
        assert row.runs == 1
        assert row.cost_estimate_usd == Decimal("1.350000")
        assert row.cost_usd == Decimal("0.000000")  # default — operator stamps later
        assert row.bucket == date(2026, 5, 20)
    finally:
        await conn.execute(
            "DELETE FROM budget_ledger WHERE analyst_id = $1",
            "test_l245_anthropic",
        )
        await conn.close()


@pytest.mark.integration
async def test_record_budget_vllm_zero_cost_not_null(migrated_pg: PostgresConfig):
    """vLLM (self-hosted, empty PRICE_TABLE) → cost_estimate_usd = 0 (NOT NULL)."""
    conn = await asyncpg.connect(migrated_pg.dsn)
    try:
        row = await record_budget(
            conn,
            analyst_id="test_l245_vllm",
            analyst_version="v1",
            provider="vllm",
            model="gpt-oss-120b",
            prompt_tokens=500_000,
            completion_tokens=200_000,
            bucket=date(2026, 5, 20),
        )
        assert row.tokens_used == 700_000
        assert row.cost_estimate_usd == Decimal("0.000000")
        # Verify the DB stored 0 (not NULL) — fetch directly and ensure
        # asyncpg got a Decimal, not None.
        raw = await conn.fetchval(
            """
            SELECT cost_estimate_usd FROM budget_ledger
            WHERE analyst_id = $1
            """,
            "test_l245_vllm",
        )
        assert raw is not None
        assert raw == Decimal("0")
    finally:
        await conn.execute(
            "DELETE FROM budget_ledger WHERE analyst_id = $1", "test_l245_vllm",
        )
        await conn.close()


@pytest.mark.integration
async def test_record_budget_accumulates_across_calls(migrated_pg: PostgresConfig):
    """Repeated record_budget calls against the same (analyst, version,
    bucket) add to the running totals."""
    conn = await asyncpg.connect(migrated_pg.dsn)
    try:
        first = await record_budget(
            conn,
            analyst_id="test_l245_accum",
            analyst_version="v1",
            provider="anthropic",
            model="claude-sonnet-4-6",
            prompt_tokens=100_000,
            completion_tokens=50_000,
            bucket=date(2026, 5, 20),
        )
        # 100k * 3/M + 50k * 15/M = 0.3 + 0.75 = 1.05
        assert first.cost_estimate_usd == Decimal("1.050000")
        assert first.runs == 1

        second = await record_budget(
            conn,
            analyst_id="test_l245_accum",
            analyst_version="v1",
            provider="anthropic",
            model="claude-sonnet-4-6",
            prompt_tokens=100_000,
            completion_tokens=50_000,
            bucket=date(2026, 5, 20),
        )
        assert second.tokens_used == 300_000
        assert second.runs == 2
        assert second.cost_estimate_usd == Decimal("2.100000")
    finally:
        await conn.execute(
            "DELETE FROM budget_ledger WHERE analyst_id = $1", "test_l245_accum",
        )
        await conn.close()


@pytest.mark.integration
async def test_record_budget_preserves_existing_columns(
    migrated_pg: PostgresConfig,
):
    """The new write helper doesn't break the pre-existing
    `tokens_used` / `runs` / `cost_usd` / `last_updated` columns."""
    conn = await asyncpg.connect(migrated_pg.dsn)
    try:
        before = datetime.now(tz=timezone.utc)
        await record_budget(
            conn,
            analyst_id="test_l245_cols",
            analyst_version="v1",
            provider="vllm",
            model="gpt-oss-120b",
            prompt_tokens=10_000,
            completion_tokens=5_000,
        )
        row = await conn.fetchrow(
            """
            SELECT analyst_id, analyst_version, bucket, tokens_used,
                   runs, cost_usd, cost_estimate_usd, last_updated
            FROM budget_ledger
            WHERE analyst_id = $1
            """,
            "test_l245_cols",
        )
        assert row is not None
        assert row["analyst_id"] == "test_l245_cols"
        assert row["analyst_version"] == "v1"
        assert row["tokens_used"] == 15_000
        assert row["runs"] == 1
        # cost_usd untouched (operator-stamped later); cost_estimate_usd
        # is the L-245 derived column.
        assert row["cost_usd"] == Decimal("0")
        assert row["cost_estimate_usd"] == Decimal("0")
        assert row["last_updated"] >= before
    finally:
        await conn.execute(
            "DELETE FROM budget_ledger WHERE analyst_id = $1", "test_l245_cols",
        )
        await conn.close()


@pytest.mark.integration
async def test_record_budget_runs_increment_zero(migrated_pg: PostgresConfig):
    """runs_increment=0 for accounting adjustments that shouldn't bump the
    run counter (e.g. correcting a miscounted prior write)."""
    conn = await asyncpg.connect(migrated_pg.dsn)
    try:
        first = await record_budget(
            conn,
            analyst_id="test_l245_runs_zero",
            analyst_version="v1",
            provider="anthropic",
            model="claude-sonnet-4-6",
            prompt_tokens=10_000,
            completion_tokens=0,
            bucket=date(2026, 5, 20),
        )
        assert first.runs == 1

        # Adjustment: more tokens, no run bump.
        second = await record_budget(
            conn,
            analyst_id="test_l245_runs_zero",
            analyst_version="v1",
            provider="anthropic",
            model="claude-sonnet-4-6",
            prompt_tokens=5_000,
            completion_tokens=0,
            bucket=date(2026, 5, 20),
            runs_increment=0,
        )
        assert second.runs == 1
        assert second.tokens_used == 15_000
    finally:
        await conn.execute(
            "DELETE FROM budget_ledger WHERE analyst_id = $1",
            "test_l245_runs_zero",
        )
        await conn.close()
