# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for L-152 SourceCredibilityHandler.

Three test groups:

  * Migration test — table + seed rows are present after `apply_primary_migrations`.
  * Unit tests on host extraction — `normalize_host` + `extract_lookup_hosts`
    cover the `www.`-prefix / subdomain-strip / IDN / IP-literal / bare-host
    cases without needing the DB.
  * Integration tests — wire a real asyncpg pool against the migrated test
    database; insert / override credibility rows; transform signals and
    verify the annotation behavior end-to-end.

The integration tests run against the conftest's `migrated_pg` fixture
(same fresh test DB the rest of `legba.data` integration tests use).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from legba.data.config import PostgresConfig
from legba.data.filters import (
    SourceCredibilityConfig,
    SourceCredibilityHandler,
    extract_lookup_hosts,
    normalize_host,
)
from legba.data.filters._contract import FilterContext
from legba.data.sources._contract import Signal


# ---------------------------------------------------------------------------
# Host-extraction unit tests (no DB)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        # Bare hostnames
        ("reuters.com", "reuters.com"),
        ("Reuters.COM", "reuters.com"),
        ("www.reuters.com", "reuters.com"),
        ("WWW.Reuters.com", "reuters.com"),
        # Full URLs
        ("https://www.reuters.com/world/article", "reuters.com"),
        ("http://news.bbc.co.uk/2/hi/123.stm", "news.bbc.co.uk"),
        ("https://www.bbc.co.uk/", "bbc.co.uk"),
        # User-info + port
        ("https://user:pass@example.com:8443/path", "example.com"),
        ("https://example.com:443", "example.com"),
        # IDN punycode
        ("https://xn--mller-kva.de/articles", "müller.de"),
        # IPv4 literal — IPs pass through normalization untrimmed at lookup time
        ("http://192.168.1.10:8080/feed", "192.168.1.10"),
        # IPv6 literal
        ("http://[::1]:8080/", "[::1]"),
        # Protocol-relative
        ("//cnn.com/world", "cnn.com"),
        # Empty / garbage
        ("", None),
        (None, None),
        ("   ", None),
    ],
)
def test_normalize_host(raw, expected):
    assert normalize_host(raw) == expected


def test_extract_lookup_hosts_three_label():
    assert extract_lookup_hosts("news.bbc.co.uk") == [
        "news.bbc.co.uk",
        "bbc.co.uk",
        "co.uk",
        "uk",
    ]


def test_extract_lookup_hosts_two_label():
    assert extract_lookup_hosts("reuters.com") == ["reuters.com", "com"]


def test_extract_lookup_hosts_single_label():
    assert extract_lookup_hosts("localhost") == ["localhost"]


def test_extract_lookup_hosts_empty():
    assert extract_lookup_hosts("") == []


def test_extract_lookup_hosts_ipv4_does_not_trim():
    """IPv4 literals should never be trimmed — `1.2.3.4` should not also
    probe `2.3.4`, `3.4`, `4`. That would produce nonsensical false hits."""
    assert extract_lookup_hosts("192.168.1.10") == ["192.168.1.10"]


def test_extract_lookup_hosts_ipv6_does_not_trim():
    assert extract_lookup_hosts("[::1]") == ["[::1]"]


# ---------------------------------------------------------------------------
# Migration tests (DB)
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_migration_creates_source_credibility_table(
    migrated_pg: PostgresConfig,
):
    conn = await asyncpg.connect(migrated_pg.dsn)
    try:
        row = await conn.fetchrow(
            """
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = 'public' AND table_name = 'source_credibility'
            """
        )
        assert row is not None, "source_credibility table missing"

        # Required columns
        cols = await conn.fetch(
            """
            SELECT column_name, data_type, is_nullable
            FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = 'source_credibility'
            """
        )
        col_names = {c["column_name"] for c in cols}
        assert {
            "source_host",
            "score",
            "score_rationale",
            "last_updated",
            "scored_by",
        } <= col_names, f"missing columns: have {col_names}"

        # CHECK constraint on score range
        ck = await conn.fetch(
            """
            SELECT conname FROM pg_constraint
            WHERE conrelid = 'public.source_credibility'::regclass
              AND contype = 'c'
            """
        )
        assert ck, "expected at least one CHECK constraint on source_credibility"
    finally:
        await conn.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_migration_seeds_canonical_baseline(migrated_pg: PostgresConfig):
    """The seed migration pre-populates a dozen wires high + known-low rows."""
    conn = await asyncpg.connect(migrated_pg.dsn)
    try:
        # Spot-check several high-credibility seeds.
        for host in (
            "reuters.com",
            "apnews.com",
            "afp.com",
            "bbc.com",
            "bbc.co.uk",
            "bloomberg.com",
            "theguardian.com",
        ):
            row = await conn.fetchrow(
                "SELECT score, score_rationale FROM source_credibility "
                "WHERE source_host = $1",
                host,
            )
            assert row is not None, f"{host} missing from seeds"
            assert row["score"] >= 0.85, f"{host} should be high credibility"
            assert row["score_rationale"], f"{host} missing rationale"

        # Spot-check a known-low seed.
        row = await conn.fetchrow(
            "SELECT score FROM source_credibility WHERE source_host = 'infowars.com'"
        )
        assert row is not None
        assert row["score"] <= 0.2

        # Count baseline rows.
        count = await conn.fetchval("SELECT COUNT(*) FROM source_credibility")
        assert count >= 12, f"expected at least 12 baseline rows, got {count}"
    finally:
        await conn.close()


# ---------------------------------------------------------------------------
# Integration tests (handler against the migrated DB)
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def credibility_pool(migrated_pg: PostgresConfig):
    """Bare asyncpg pool against the migrated test DB."""
    pool = await asyncpg.create_pool(migrated_pg.dsn, min_size=1, max_size=3)
    try:
        yield pool
    finally:
        await pool.close()


def _make_filter_ctx() -> FilterContext:
    return FilterContext(
        target_id="test.target.l152",
        target_version="0-0-1",
        filter_id="filter.source_credibility.l152",
    )


def _make_signal(*, canonical_url: str | None, payload: dict | None = None) -> Signal:
    # Source-first pivot: Signal is source-owned and target-agnostic — the
    # dropped ``target_id`` lives only on derived analyst outputs now
    # (see PIVOT_BUILD_PLAN; src/legba/data/sources/_contract.py Signal).
    return Signal(
        source_id="test.source",
        canonical_url=canonical_url,
        payload=payload or {},
        content_hash="deadbeef",
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_transform_high_credibility_seed(credibility_pool: asyncpg.Pool):
    """A signal from a seeded high-credibility host gets the seeded score."""
    handler = SourceCredibilityHandler(
        SourceCredibilityConfig(min_score=0.3),
        pool=credibility_pool,
    )
    sig = _make_signal(canonical_url="https://www.reuters.com/world/x")
    out = await handler.transform(sig, _make_filter_ctx())
    assert out is not None
    assert out.source_credibility == pytest.approx(0.90)
    assert out.source_credibility_rationale
    assert out.below_credibility_threshold is False


@pytest.mark.integration
@pytest.mark.asyncio
async def test_transform_subdomain_match(credibility_pool: asyncpg.Pool):
    """A signal from a sub-host of a registered domain matches via trimming."""
    handler = SourceCredibilityHandler(
        SourceCredibilityConfig(min_score=0.3),
        pool=credibility_pool,
    )
    sig = _make_signal(canonical_url="https://news.bbc.co.uk/2/hi/123.stm")
    out = await handler.transform(sig, _make_filter_ctx())
    # bbc.co.uk is a seed row at 0.90
    assert out.source_credibility == pytest.approx(0.90)
    assert out.below_credibility_threshold is False


@pytest.mark.integration
@pytest.mark.asyncio
async def test_transform_inserted_row_overrides(credibility_pool: asyncpg.Pool):
    """Operator-inserted row → handler returns that score."""
    host = f"l152test-{uuid4().hex[:8]}.example"
    async with credibility_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO source_credibility "
            "(source_host, score, score_rationale, scored_by) VALUES "
            "($1, $2, $3, $4)",
            host, 0.42, "L-152 integration test row", "test.operator",
        )

    try:
        handler = SourceCredibilityHandler(
            SourceCredibilityConfig(min_score=0.3),
            pool=credibility_pool,
        )
        sig = _make_signal(canonical_url=f"https://{host}/article")
        out = await handler.transform(sig, _make_filter_ctx())
        assert out.source_credibility == pytest.approx(0.42)
        assert out.source_credibility_rationale == "L-152 integration test row"
        assert out.below_credibility_threshold is False  # 0.42 >= 0.3
    finally:
        async with credibility_pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM source_credibility WHERE source_host = $1", host
            )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_transform_unknown_host_null(credibility_pool: asyncpg.Pool):
    """Unknown host with no default_score → null credibility, no flagging."""
    handler = SourceCredibilityHandler(
        SourceCredibilityConfig(min_score=0.3),
        pool=credibility_pool,
    )
    sig = _make_signal(canonical_url="https://unseen-domain-abc-xyz.invalid/x")
    out = await handler.transform(sig, _make_filter_ctx())
    assert out.source_credibility is None
    assert out.source_credibility_rationale is None
    assert out.below_credibility_threshold is None  # not flagged when score is null


@pytest.mark.integration
@pytest.mark.asyncio
async def test_transform_unknown_host_with_default_score(
    credibility_pool: asyncpg.Pool,
):
    """Unknown host with default_score below min_score → flagged."""
    handler = SourceCredibilityHandler(
        SourceCredibilityConfig(
            min_score=0.3,
            default_score=0.2,
            default_score_rationale="custom unknown-host default",
        ),
        pool=credibility_pool,
    )
    sig = _make_signal(canonical_url="https://still-unseen-domain.invalid/x")
    out = await handler.transform(sig, _make_filter_ctx())
    assert out.source_credibility == pytest.approx(0.2)
    assert out.source_credibility_rationale == "custom unknown-host default"
    assert out.below_credibility_threshold is True


@pytest.mark.integration
@pytest.mark.asyncio
async def test_transform_sub_threshold_flagging(credibility_pool: asyncpg.Pool):
    """A score below min_score is flagged but the signal is NOT dropped."""
    handler = SourceCredibilityHandler(
        SourceCredibilityConfig(min_score=0.5),
        pool=credibility_pool,
    )
    # infowars.com seeded at 0.10
    sig = _make_signal(canonical_url="https://www.infowars.com/article")
    out = await handler.transform(sig, _make_filter_ctx())
    assert out is not None, "handler must NOT drop sub-threshold signals"
    assert out.source_credibility == pytest.approx(0.10)
    assert out.below_credibility_threshold is True


@pytest.mark.integration
@pytest.mark.asyncio
async def test_transform_no_url_returns_null(credibility_pool: asyncpg.Pool):
    """A signal with no usable URL gets null credibility."""
    handler = SourceCredibilityHandler(
        SourceCredibilityConfig(min_score=0.3),
        pool=credibility_pool,
    )
    sig = _make_signal(canonical_url=None, payload={"title": "x"})
    out = await handler.transform(sig, _make_filter_ctx())
    assert out.source_credibility is None
    assert out.below_credibility_threshold is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_transform_fallback_to_payload_source_url(
    credibility_pool: asyncpg.Pool,
):
    """When canonical_url is None, `payload.source_url` is used."""
    handler = SourceCredibilityHandler(
        SourceCredibilityConfig(min_score=0.3),
        pool=credibility_pool,
    )
    sig = _make_signal(
        canonical_url=None,
        payload={"source_url": "https://feeds.reuters.com/world.xml"},
    )
    out = await handler.transform(sig, _make_filter_ctx())
    assert out.source_credibility == pytest.approx(0.90)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_caching_avoids_repeat_db_calls(credibility_pool: asyncpg.Pool):
    """Two calls for the same host hit the cache after the first miss."""
    handler = SourceCredibilityHandler(
        SourceCredibilityConfig(min_score=0.3, cache_ttl_seconds=60),
        pool=credibility_pool,
    )
    sig = _make_signal(canonical_url="https://www.bloomberg.com/x")
    out1 = await handler.transform(sig, _make_filter_ctx())
    assert handler.cache_size() == 1

    # Re-call; cache should be hit (size unchanged).
    out2 = await handler.transform(sig, _make_filter_ctx())
    assert out1.source_credibility == out2.source_credibility
    assert handler.cache_size() == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_caching_disabled_with_ttl_zero(credibility_pool: asyncpg.Pool):
    handler = SourceCredibilityHandler(
        SourceCredibilityConfig(min_score=0.3, cache_ttl_seconds=0),
        pool=credibility_pool,
    )
    sig = _make_signal(canonical_url="https://www.bloomberg.com/x")
    await handler.transform(sig, _make_filter_ctx())
    assert handler.cache_size() == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_health_check_healthy(credibility_pool: asyncpg.Pool):
    handler = SourceCredibilityHandler(
        SourceCredibilityConfig(),
        pool=credibility_pool,
    )
    h = await handler.health_check(_make_filter_ctx())
    assert h.state == "healthy"
    # registry_rows reflects the seeded baseline.
    assert h.detail["registry_rows"] >= 12


@pytest.mark.asyncio
async def test_health_check_without_pool_unhealthy():
    """Handler constructed without a pool reports unhealthy at probe time."""
    handler = SourceCredibilityHandler(SourceCredibilityConfig(), pool=None)
    h = await handler.health_check(_make_filter_ctx())
    assert h.state == "unhealthy"
    assert "pool" in (h.last_error or "").lower()


@pytest.mark.asyncio
async def test_transform_without_pool_is_pass_through():
    """No pool → handler returns the signal unmodified (never raises)."""
    handler = SourceCredibilityHandler(SourceCredibilityConfig(), pool=None)
    sig = _make_signal(canonical_url="https://www.reuters.com/x")
    out = await handler.transform(sig, _make_filter_ctx())
    assert out is not None
    assert out.source_credibility is None
    assert out.below_credibility_threshold is None


@pytest.mark.asyncio
async def test_transform_without_pool_with_default_score():
    """No pool but a default_score → default applies; below_threshold flagged when warranted."""
    handler = SourceCredibilityHandler(
        SourceCredibilityConfig(min_score=0.3, default_score=0.1),
        pool=None,
    )
    sig = _make_signal(canonical_url="https://anything.example/x")
    out = await handler.transform(sig, _make_filter_ctx())
    assert out.source_credibility == pytest.approx(0.1)
    assert out.below_credibility_threshold is True


def test_config_rejects_out_of_range():
    """Pydantic config validation refuses scores outside [0, 1]."""
    with pytest.raises(Exception):
        SourceCredibilityConfig(min_score=1.5)
    with pytest.raises(Exception):
        SourceCredibilityConfig(default_score=-0.1)


def test_handler_class_vars():
    """L-102 §1 conformance — required class vars are present and correct."""
    H = SourceCredibilityHandler
    assert H.kind == "source_credibility"
    assert H.family == "filter"
    assert H.schema_version.startswith("legba/filter.source_credibility/")
    assert H.config_schema is SourceCredibilityConfig
    assert "source_credibility" in H.output_contract
    assert "source_credibility_rationale" in H.output_contract
    assert "below_credibility_threshold" in H.output_contract


def test_signal_supports_credibility_fields():
    """The L-102 §3 'enrich the signal' shape.

    Source-first pivot (src/legba/data/sources/_contract.py): the Signal
    model declares ``source_credibility`` as a first-class field, but the
    per-target floor threshold is evaluated at subscription/read time, so
    ``source_credibility_rationale`` / ``below_credibility_threshold`` are
    no longer declared fields — the handler stamps them as transient
    enrichment via ``model_copy(update=...)``. This test exercises both
    shapes: the declared field is constructable, and the transient
    enrichment fields read back after a stamp.
    """
    sig = Signal(source_id="x", source_credibility=0.7)
    assert sig.source_credibility == 0.7

    stamped = sig.model_copy(
        update={
            "source_credibility_rationale": "r",
            "below_credibility_threshold": False,
        }
    )
    assert stamped.source_credibility == 0.7
    assert stamped.source_credibility_rationale == "r"
    assert stamped.below_credibility_threshold is False
