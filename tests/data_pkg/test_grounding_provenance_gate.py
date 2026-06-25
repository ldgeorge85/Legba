# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Grounding-quality gate (PG behavioral): the "treat as ground truth" preamble
admits ONLY operator-vetted provenance.

A live audit found the ingestion path laundered current, grounding-eligible NER
hallucinations onto real subjects — ``Iran | capital of | US``,
``Iran | controls | Israel``, ``<person> | leader of | <geo>`` — at confidence
**1.0** (REBEL's synthetic floor leaks). Because the preamble header tells the
LLM to treat the block as ground truth that SUPERSEDES its training, this junk
poisoned every grounded assessor. The fix restricts the resolver to
``source_type IN ('seed','curated')`` for BOTH facts and signed nexuses.

The unit tests in ``tests/runtime/test_grounding.py`` assert the SQL filter +
bound param + env override; this proves the END-TO-END behavior against a real
Postgres — seed rows on a subject survive while ingestion/agent rows on the
SAME subject (even at conf 1.0) are dropped before they can render.
"""

from __future__ import annotations

from datetime import datetime, timezone

import asyncpg
import pytest
import pytest_asyncio

from legba.data.config import PostgresConfig
from legba.runtime.grounding import (
    SubstrateGroundingResolver,
    build_grounding_preamble,
    build_situations_block,
)


@pytest_asyncio.fixture
async def pg_pool(migrated_pg: PostgresConfig):
    pool = await asyncpg.create_pool(migrated_pg.dsn, min_size=1, max_size=4)
    yield pool
    await pool.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_grounding_preamble_excludes_ingestion_junk(pg_pool):
    # Unique synthetic subject so the test is isolated from any seeded G20 data
    # already present in the shared dev DB.
    subj = f"Groundland_{datetime.now(tz=timezone.utc).timestamp():.0f}"
    vf = datetime(2026, 3, 8, tzinfo=timezone.utc)

    async with pg_pool.acquire() as conn:
        # Operator-vetted ground truth (must survive the gate).
        await conn.execute(
            "INSERT INTO facts (subject, predicate, value, confidence, source_type, valid_from) "
            "VALUES ($1, 'head of state', 'Real Leader', 0.95, 'seed', $2)",
            subj, vf,
        )
        # Ingestion NER junk on the SAME subject, at confidence 1.0 — must be
        # dropped wholesale (confidence is NOT a trust signal for ingestion).
        await conn.execute(
            "INSERT INTO facts (subject, predicate, value, confidence, source_type, valid_from) "
            "VALUES ($1, 'capital of', 'US', 1.0, 'ingestion', $2), "
            "($1, 'controls', 'Israel', 1.0, 'ingestion', $2), "
            "($1, 'leader of', 'Some Junk', 1.0, 'ingestion', $2)",
            subj, vf,
        )
        # Signed nexuses: the seed conflict edge survives; the reified/promoted
        # 'agent' lane is an analysis product, excluded from ground truth.
        await conn.execute(
            "INSERT INTO nexuses (subject, rel_type, object, polarity, confidence, source_type, valid_from) "
            "VALUES ($1, 'in active conflict with', 'United States', -1, 0.95, 'seed', $2), "
            "($1, 'controls', 'Everything', 1, 1.0, 'agent', $2)",
            subj, vf,
        )

    resolver = SubstrateGroundingResolver(pg_pool=pg_pool)
    facts, nexuses = await resolver.resolve([subj], max_facts=30)

    fact_values = {f.value for f in facts}
    assert "Real Leader" in fact_values            # seed officeholder survives
    assert "US" not in fact_values                 # 'capital of US' dropped
    assert "Israel" not in fact_values             # 'controls Israel' dropped
    assert "Some Junk" not in fact_values          # ingestion 'leader of' dropped
    assert all(f.source_type in ("seed", "curated") for f in facts)

    nexus_objects = {n.object for n in nexuses}
    assert "United States" in nexus_objects        # seed active-conflict survives
    assert "Everything" not in nexus_objects       # agent nexus dropped

    preamble = build_grounding_preamble(
        facts, nexuses, now=datetime(2026, 6, 19, tzinfo=timezone.utc),
    )
    assert preamble is not None
    assert "head of state: Real Leader" in preamble
    assert "in active conflict with United States" in preamble
    # None of the laundered junk reaches the LLM context.
    assert "capital of" not in preamble
    assert "controls" not in preamble
    assert "Some Junk" not in preamble


@pytest.mark.integration
@pytest.mark.asyncio
async def test_grounding_env_override_admits_extra_provenance(pg_pool, monkeypatch):
    """An operator can widen the trusted set via env to admit a vetted lane
    (e.g. a distinct 'wikidata' source_type) without a code change — proving the
    gate is a real, configurable filter and not a hard-coded constant."""
    subj = f"Envland_{datetime.now(tz=timezone.utc).timestamp():.0f}"
    vf = datetime(2026, 3, 8, tzinfo=timezone.utc)

    async with pg_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO facts (subject, predicate, value, confidence, source_type, valid_from) "
            "VALUES ($1, 'head of state', 'Wiki Leader', 0.95, 'wikidata', $2), "
            "($1, 'controls', 'Junk', 1.0, 'ingestion', $2)",
            subj, vf,
        )

    resolver = SubstrateGroundingResolver(pg_pool=pg_pool)

    # Default gate excludes the 'wikidata' lane.
    monkeypatch.delenv("LEGBA_GROUNDING_TRUSTED_SOURCE_TYPES", raising=False)
    facts_default, _ = await resolver.resolve([subj], max_facts=30)
    assert "Wiki Leader" not in {f.value for f in facts_default}

    # Widened gate admits it; the ingestion junk still never qualifies.
    monkeypatch.setenv("LEGBA_GROUNDING_TRUSTED_SOURCE_TYPES", "seed,curated,wikidata")
    facts_wide, _ = await resolver.resolve([subj], max_facts=30)
    values = {f.value for f in facts_wide}
    assert "Wiki Leader" in values
    assert "Junk" not in values


@pytest.mark.integration
@pytest.mark.asyncio
async def test_situations_grounding_surfaces_open_frames_only(pg_pool):
    """Phase 5a.3: the ASSESSED SITUATIONS grounding block surfaces only OPEN
    (ongoing) frames for the scope — a CLOSED frame is excluded — and renders
    under its own analysis-derived header, never the ground-truth one."""
    cat = f"country_test_{datetime.now(tz=timezone.utc).timestamp():.0f}"
    vf = datetime(2026, 2, 28, tzinfo=timezone.utc)

    async with pg_pool.acquire() as conn:
        # An OPEN (ongoing) frame: valid_until NULL, status active. target_id is
        # the scope key (migration 0042); situations.id is NOT NULL with no DB
        # default, so supply it explicitly.
        await conn.execute(
            "INSERT INTO situations "
            "(id, data, name, status, category, target_id, intensity_score, "
            " situation_signature, valid_from, analyst_id) "
            "VALUES (gen_random_uuid(), '{}'::jsonb, 'Ongoing War', 'active', $1, $1, "
            "1.5, $2, $3, 'situation_clustering')",
            cat, f"sig:{cat}:open", vf,
        )
        # A CLOSED frame on the SAME target: valid_until stamped → excluded.
        await conn.execute(
            "INSERT INTO situations "
            "(id, data, name, status, category, target_id, intensity_score, "
            " situation_signature, valid_from, valid_until, analyst_id) "
            "VALUES (gen_random_uuid(), '{}'::jsonb, 'Settled Matter', 'closed', $1, $1, "
            "0.2, $2, $3, $4, 'situation_clustering')",
            cat, f"sig:{cat}:closed", vf,
            datetime(2026, 3, 1, tzinfo=timezone.utc),
        )

    resolver = SubstrateGroundingResolver(pg_pool=pg_pool)
    sits = await resolver.resolve_situations(scope_target_id=cat, limit=8)
    names = {s.name for s in sits}
    assert "Ongoing War" in names            # open frame surfaces
    assert "Settled Matter" not in names     # closed frame excluded

    block = build_situations_block(sits)
    assert block is not None
    assert "ASSESSED SITUATIONS" in block
    assert "Ongoing War" in block
    assert "Settled Matter" not in block
    # The situations block is fenced off from the ground-truth header.
    assert "AUTHORITATIVE CURRENT CONTEXT" not in block
