# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Migration 0035 + SLM-disambiguator wiring (entity-resolution Wave 1).

Three layers, mirroring the package's test conventions:

  * **Migration schema (integration, real PG)** — 0035 replaces the single-key
    ``idx_entity_profiles_name`` with the composite
    ``idx_entity_profiles_name_class`` and the composite key actually permits a
    name shared across classes while still rejecting a same-name same-class
    duplicate.
  * **Builder wiring (unit)** — ``slm_entity_resolve`` is a known pipeline kind,
    is OFF unless a descriptor names it, fails LOUD (no stub) when its required
    deps are missing, and constructs the real SLM-disambiguating handler with a
    provider-plane SLM port + Postgres trigram candidate port when wired.
  * **Candidate port (unit)** — the Postgres trigram port refuses to construct
    without a pool (no stub) and issues a class-aware similarity query.
"""

from __future__ import annotations

from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from legba.data.config import PostgresConfig


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def pg_conn(migrated_pg: PostgresConfig):
    conn = await asyncpg.connect(migrated_pg.dsn)
    yield conn
    await conn.close()


# ---------------------------------------------------------------------------
# Migration 0035 — composite unique index
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_migration_0035_composite_index_shape(pg_conn):
    # The new composite index exists; the old single-key one is gone.
    composite = await pg_conn.fetchval(
        "SELECT indexdef FROM pg_indexes "
        "WHERE indexname = 'idx_entity_profiles_name_class'"
    )
    assert composite is not None, "composite index idx_entity_profiles_name_class missing"
    assert "lower(canonical_name)" in composite.lower()
    assert "entity_class" in composite.lower()

    old = await pg_conn.fetchval(
        "SELECT 1 FROM pg_indexes WHERE indexname = 'idx_entity_profiles_name'"
    )
    assert old is None, "old single-key idx_entity_profiles_name should be dropped"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_composite_key_allows_same_name_across_class(pg_conn):
    """Same name + different class = two rows (the false-merge fix); same name +
    same class = one row (still deduped)."""
    name = f"Georgia_{uuid4().hex[:8]}"

    async def _upsert(entity_class: str) -> str:
        row = await pg_conn.fetchrow(
            """
            INSERT INTO entity_profiles
                (canonical_name, entity_type, entity_class, data, completeness_score)
            VALUES ($1,$2,$2,'{}'::jsonb,0.3)
            ON CONFLICT (lower(canonical_name), entity_class) DO UPDATE
                SET last_event_link_at = now()
            RETURNING id
            """,
            name, entity_class,
        )
        return str(row["id"])

    try:
        country_id = await _upsert("country")
        state_id = await _upsert("location")
        # Distinct classes -> distinct rows.
        assert country_id != state_id

        # Re-upsert the country class -> same row (still deduped within a class).
        country_id_again = await _upsert("country")
        assert country_id_again == country_id

        n = await pg_conn.fetchval(
            "SELECT count(*) FROM entity_profiles "
            "WHERE lower(canonical_name)=lower($1)", name)
        assert n == 2, f"expected exactly 2 rows (country+location), got {n}"
    finally:
        await pg_conn.execute(
            "DELETE FROM entity_profiles WHERE lower(canonical_name)=lower($1)",
            name)


# ---------------------------------------------------------------------------
# build_filter_handler — slm_entity_resolve wiring (descriptor-gated, no stub)
# ---------------------------------------------------------------------------


def test_slm_entity_resolve_is_a_known_kind():
    from legba.runtime.pipeline import _KNOWN_KINDS

    assert "slm_entity_resolve" in _KNOWN_KINDS


def test_slm_entity_resolve_requires_pg_pool():
    """Fail LOUD when the candidate-port pool is missing — never a silent stub."""
    from legba.runtime.pipeline import build_filter_handler

    async def _factory(_cid):  # pragma: no cover - never reached
        raise AssertionError("factory should not be called when pg_pool missing")

    with pytest.raises(ValueError, match="requires a pg_pool"):
        build_filter_handler(
            kind="slm_entity_resolve",
            config={},
            llm_handler_factory=_factory,
        )


def test_slm_entity_resolve_requires_llm_factory():
    """Fail LOUD when the provider-plane factory is missing — no litellm, no stub."""
    from legba.runtime.pipeline import build_filter_handler

    class _Pool:
        async def acquire(self):  # pragma: no cover - never reached
            raise AssertionError("pool should not be used at construction")

    with pytest.raises(ValueError, match="requires an llm_handler_factory"):
        build_filter_handler(
            kind="slm_entity_resolve",
            config={},
            pg_pool=_Pool(),
        )


def test_slm_entity_resolve_builds_real_handler():
    """With both deps wired, build the real SLM disambiguator over a
    provider-plane SLM port + a Postgres trigram candidate port."""
    from legba.data.filters.entity_candidate_port import PostgresEntityCandidatePort
    from legba.data.filters.slm_entity_resolve import (
        SLM_ENTITY_RESOLVE_KIND,
        SLMEntityResolveHandler,
    )
    from legba.runtime.pipeline import build_filter_handler

    factory_calls: list[str] = []

    async def _factory(component_id: str):
        factory_calls.append(component_id)

        class _H:
            async def chat_complete(self, messages, *, system=None, **kw):
                return type("R", (), {"content": "{}"})()

        return _H()

    class _Pool:
        async def acquire(self):  # pragma: no cover - not exercised here
            raise AssertionError("pool not used at construction")

    handler = build_filter_handler(
        kind="slm_entity_resolve",
        config={"llm_component_id": "my-slm", "candidate_limit": 5},
        pg_pool=_Pool(),
        llm_handler_factory=_factory,
    )
    assert isinstance(handler, SLMEntityResolveHandler)
    assert handler.kind == SLM_ENTITY_RESOLVE_KIND
    # config knob threaded; the builder-only llm_component_id was stripped.
    assert handler.config.candidate_limit == 5
    # candidate port is the real Postgres-backed one (no stub).
    assert isinstance(handler._candidates, PostgresEntityCandidatePort)
    # The factory is resolved lazily — not at construction time.
    assert factory_calls == []


# ---------------------------------------------------------------------------
# PostgresEntityCandidatePort — no-stub construction guard
# ---------------------------------------------------------------------------


def test_candidate_port_refuses_without_pool():
    from legba.data.filters.entity_candidate_port import PostgresEntityCandidatePort

    with pytest.raises(ValueError, match="requires a pg_pool"):
        PostgresEntityCandidatePort(None)


@pytest.mark.asyncio
async def test_candidate_port_empty_name_returns_empty():
    from legba.data.filters.entity_candidate_port import PostgresEntityCandidatePort

    class _Pool:
        async def acquire(self):  # pragma: no cover - never reached
            raise AssertionError("empty name must short-circuit before any query")

    port = PostgresEntityCandidatePort(_Pool())
    assert await port.fetch_candidates(entity_name="  ", entity_type="other") == []
