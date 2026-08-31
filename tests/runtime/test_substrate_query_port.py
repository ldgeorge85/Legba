# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for :class:`PostgresQdrantSubstrateQueryPort`.

Covers each method against live ``legba-postgres`` (via the
``migrated_pg`` fixture) and live ``legba-qdrant`` (via the inline
``real_qdrant`` fixture below).  Insert known rows, query, assert results.
Per Lewis's no-mocks rule: real substrate, real schema, real client.

Coverage map per Protocol method:

  * ``search_signals``         — happy path, category filter, empty
                                 query, no-match query, limit clamp,
                                 scope_predicate echo.
  * ``query_facts``            — single-filter + combined filters, no-
                                 filter error path, empty-result path.
  * ``inspect_entity``         — entity found w/ versions + mentions;
                                 entity not found; empty-name guard.
  * ``vector_search``          — Protocol-shape (returns unavailable
                                 today since no embedder is wired
                                 through the port surface).
  * ``vector_search_by_embedding`` — happy path against a freshly-
                                 written collection; target_id payload
                                 filter; empty collection.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import asyncpg
import pytest
import pytest_asyncio

from legba.runtime.substrate_query_port import PostgresQdrantSubstrateQueryPort


# Source-first pivot (migration 0024) re-cut ``signals`` to the target-
# agnostic, modality-first shape: ``data`` -> ``payload`` (JSONB), and the
# scalar columns ``title`` / ``category`` / ``source_url`` / ``produced_at``
# / ``target_id`` were DROPPED. ``PostgresQdrantSubstrateQueryPort`` now reads
# its searchable text from ``payload->>'title'`` / ``payload->>'summary'`` and
# its category filter from ``payload->>'category'`` (with ``fetched_at`` /
# ``canonical_url`` standing in for the dropped ``produced_at`` / ``source_url``)
# in both ``search_signals`` and the ``inspect_entity`` recent-mentions JOIN.


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def pg_pool(migrated_pg):
    """asyncpg pool against the per-session migrated test database."""
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


_QDRANT_GATE = os.environ.get("LEGBA_TEST_QDRANT", "1") == "1"

skip_unless_qdrant_live = pytest.mark.skipif(
    not _QDRANT_GATE,
    reason="LEGBA_TEST_QDRANT=1 not set; skipping live Qdrant tests",
)


@pytest_asyncio.fixture
async def real_qdrant():
    """Live AsyncQdrantClient against the legba-qdrant container.

    Drops every collection whose name starts with ``legba_test_sqp__`` at
    teardown so concurrent test runs don't pile up artifacts.
    """
    from qdrant_client import AsyncQdrantClient

    client = AsyncQdrantClient(host="127.0.0.1", port=6333)
    yield client
    try:
        cols = await client.get_collections()
        for c in cols.collections:
            if c.name.startswith("legba_test_sqp__"):
                try:
                    await client.delete_collection(collection_name=c.name)
                except Exception:                                # pragma: no cover
                    pass
    finally:
        await client.close()


@pytest_asyncio.fixture
async def port(pg_pool, real_qdrant):
    return PostgresQdrantSubstrateQueryPort(
        pg_pool=pg_pool,
        qdrant_client=real_qdrant,
        signals_collection="legba_test_sqp__signals",
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _insert_signal(
    pool,
    *,
    title: str,
    summary: str = "",
    category: str = "other",
    produced_at: datetime | None = None,
) -> UUID:
    """Insert a source-first ``signals`` row (post-0024 schema).

    The pivot re-cut ``signals`` to the source-owned, modality-first shape:
    ``data`` -> ``payload`` (JSONB), and ``title`` / ``category`` /
    ``source_url`` / ``produced_at`` / ``target_id`` were DROPPED. The old
    per-row scalars that survived as substantive content now live inside
    ``payload`` (``title`` / ``summary``), with ``category`` carried as a
    structured-filter ``tag`` and the row's fetch time on ``fetched_at``.
    """
    sid = uuid4()
    now = produced_at or datetime.now(tz=timezone.utc)
    payload = {
        "title": title,
        "summary": summary,
        "category": category,
        "descriptor_source_id": "test",
    }
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO signals (
                id, source_id, source_version, produced_by_kind,
                fetched_at, modality, payload, canonical_url, language,
                tags, derived_from, schema_uri
            ) VALUES (
                $1, $2, '', 'source',
                $3, 'text', $4::jsonb, $5, 'en',
                $6::text[], '{}'::uuid[],
                'iglu:legba/signal/jsonschema/3-0-0'
            )
            """,
            sid,
            f"src-sqp-{sid}",
            now,
            json.dumps(payload),
            f"https://example.invalid/sqp/{sid}",
            [category],
        )
    return sid


async def _insert_fact(
    pool,
    *,
    subject: str,
    predicate: str,
    value: str,
    confidence: float = 0.85,
    superseded_by: UUID | None = None,
    valid_until: datetime | None = None,
) -> UUID:
    """Insert a ``facts`` row.

    ``superseded_by`` / ``valid_until`` default to NULL — the canonical
    "current / open" fact (migration 0032).  Pass either to mark a row as
    superseded by a successor / explicitly expired so the current-facts
    gate in ``query_facts`` + ``inspect_entity`` can be exercised.
    """
    fid = uuid4()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO facts (
                id, subject, predicate, value, confidence,
                source_cycle, source_type, data, evidence_set,
                valid_from, valid_until, superseded_by, geo_lat, geo_lon,
                target_id, target_version, analyst_id, analyst_version,
                produced_at, derived_from, schema_uri, run_id
            ) VALUES (
                $1, $2, $3, $4, $5,
                NULL, 'agent', NULL, NULL,
                NULL, $6, $7, NULL, NULL,
                NULL, NULL, NULL, NULL,
                NOW(), '{}'::uuid[],
                'iglu:legba/fact/jsonschema/2-0-0', NULL
            )
            """,
            fid, subject, predicate, value, confidence,
            valid_until, superseded_by,
        )
    return fid


async def _insert_entity(
    pool,
    *,
    canonical_name: str,
    entity_class: str = "organization",
) -> UUID:
    eid = uuid4()
    data = {"description": f"Test entity for {canonical_name}"}
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO entity_profiles (
                id, data, canonical_name, entity_type, entity_class,
                version, completeness_score,
                last_event_link_at, last_verified_at,
                geo_lat, geo_lon, geo_country, geo_region,
                target_id, target_version, analyst_id, analyst_version,
                produced_at, derived_from, schema_uri, run_id
            ) VALUES (
                $1, $2::jsonb, $3, $4, $4,
                1, 0.5,
                NULL, NULL,
                NULL, NULL, 'BR', NULL,
                NULL, NULL, NULL, NULL,
                NOW(), '{}'::uuid[],
                'iglu:legba/entity_profile/jsonschema/2-0-0', NULL
            )
            """,
            eid, json.dumps(data), canonical_name, entity_class,
        )
    return eid


async def _insert_entity_version(
    pool, *, entity_id: UUID, version: int, cycle_number: int | None = None,
) -> UUID:
    vid = uuid4()
    data = {"version": version, "snapshot": True}
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO entity_profile_versions (
                id, entity_id, version, data, cycle_number,
                analyst_id, analyst_version, run_id
            ) VALUES ($1, $2, $3, $4::jsonb, $5, NULL, NULL, NULL)
            """,
            vid, entity_id, version, json.dumps(data), cycle_number,
        )
    return vid


async def _link_signal_to_entity(
    pool, *, signal_id: UUID, entity_id: UUID, role: str = "mentioned",
) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO signal_entity_links (
                signal_id, entity_id, role, confidence,
                analyst_id, analyst_version, run_id
            ) VALUES ($1, $2, $3, 0.9, NULL, NULL, NULL)
            ON CONFLICT DO NOTHING
            """,
            signal_id, entity_id, role,
        )


async def _insert_nexus(
    pool,
    *,
    subject: str,
    object_: str,
    rel_type: str,
    intermediary: str | None = None,
    polarity: int = 0,
    intent: str = "",
    channel: str = "direct",
    confidence: float = 0.9,
    superseded_by: UUID | None = None,
    valid_until: datetime | None = None,
    target_id: str | None = None,
) -> UUID:
    """Insert a reified ``nexuses`` row (migration 0033).

    ``superseded_by`` / ``valid_until`` default to NULL — the canonical
    OPEN nexus ("what holds now").  Pass either to mark a row as
    superseded / explicitly expired so the open-nexus gate in
    ``query_nexuses`` can be exercised.
    """
    nid = uuid4()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO nexuses (
                id, subject, intermediary, object, rel_type, label,
                polarity, intent, channel, confidence,
                valid_from, valid_until, superseded_by,
                target_id, analyst_id, produced_at, run_id
            ) VALUES (
                $1, $2, $3, $4, $5, '',
                $6, $7, $8, $9,
                NOW(), $10, $11,
                $12, NULL, NOW(), NULL
            )
            """,
            nid, subject, intermediary, object_, rel_type,
            polarity, intent, channel, confidence,
            valid_until, superseded_by, target_id,
        )
    return nid


async def _insert_edge(
    pool,
    *,
    subject: str,
    object_: str,
    rel_type: str,
    intermediary: str | None = None,
    polarity: int = 0,
    confidence: float = 0.9,
    edge_family: str = "relation",
) -> UUID:
    """Insert an id-keyed ``entity_edges`` row (migration 0143), minting the
    endpoint ``entity_profiles`` on demand.

    W3-A: the path/broker walks read `entity_edges`, not `nexuses`. Seeding a
    bare nexus row no longer reaches them — which is the point of the cutover,
    since the walk now traverses foreign keys rather than name strings.
    """
    async def _entity(conn, name: str) -> UUID:
        eid = await conn.fetchval(
            "SELECT id FROM entity_profiles WHERE lower(canonical_name)=lower($1)"
            " AND merged_into IS NULL LIMIT 1", name)
        if eid is None:
            eid = await conn.fetchval(
                """INSERT INTO entity_profiles
                     (canonical_name, entity_class, entity_type, data)
                   VALUES ($1, 'organization', 'organization', '{}'::jsonb)
                   RETURNING id""", name)
        return eid

    async with pool.acquire() as conn:
        src = await _entity(conn, subject)
        dst = await _entity(conn, object_)
        via = await _entity(conn, intermediary) if intermediary else None
        return await conn.fetchval(
            """
            INSERT INTO entity_edges
                (src_id, dst_id, intermediary_id, edge_type, edge_family,
                 polarity, confidence)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            RETURNING id
            """,
            src, dst, via, rel_type, edge_family, int(polarity),
            float(confidence),
        )


async def _insert_hypothesis(
    pool,
    *,
    thesis: str,
    counter_thesis: str = "",
    evidence_balance: int = 0,
    status: str = "active",
    situation_id: UUID | None = None,
    supporting_signals: list[UUID] | None = None,
    refuting_signals: list[UUID] | None = None,
    resolved_outcome: int | None = None,
    resolved_by: str | None = None,
    target_id: str | None = None,
) -> UUID:
    """Insert an ACH ``hypotheses`` row (migration 0001 + 0038).

    ``resolved_outcome`` / ``resolved_by`` default to NULL — the
    unresolved hypothesis (no exogenous signal yet, migration 0038).
    Pass ``resolved_outcome`` (0/1) + ``resolved_by`` to mark a row
    exogenously resolved so ``query_hypotheses`` can surface it.
    """
    hid = uuid4()
    resolved_at = (
        datetime.now(tz=timezone.utc) if resolved_outcome is not None else None
    )
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO hypotheses (
                id, situation_id, thesis, counter_thesis,
                supporting_signals, refuting_signals, evidence_balance,
                status, resolved_outcome, resolved_at, resolved_by,
                target_id, produced_at, derived_from, run_id
            ) VALUES (
                $1, $2, $3, $4,
                $5::uuid[], $6::uuid[], $7,
                $8, $9, $10, $11,
                $12, NOW(), '{}'::uuid[], NULL
            )
            """,
            hid, situation_id, thesis, counter_thesis,
            supporting_signals or [], refuting_signals or [], evidence_balance,
            status, resolved_outcome, resolved_at, resolved_by, target_id,
        )
    return hid


async def _insert_finding(
    pool,
    *,
    title: str,
    target_id: str,
    confidence: float = 0.6,
    severity: str | None = None,
    superseded_by: UUID | None = None,
) -> UUID:
    """Insert an ``analyst_outputs`` row of kind ``finding`` for one target.

    ``superseded_by`` defaults to NULL (live finding); pass a successor id
    to mark it superseded so the ``compare_targets`` rollup (which counts
    only ``superseded_by IS NULL`` findings) can be exercised.
    """
    oid = uuid4()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO analyst_outputs (
                id, kind, title, body, confidence, severity, data,
                target_id, produced_at, derived_from, schema_uri, run_id,
                superseded_by
            ) VALUES (
                $1, 'finding', $2, '', $3, $4, '{}'::jsonb,
                $5, NOW(), '{}'::uuid[],
                'iglu:legba/finding/jsonschema/1-0-0', NULL,
                $6
            )
            """,
            oid, title, confidence, severity, target_id, superseded_by,
        )
    return oid


# ---------------------------------------------------------------------------
# search_signals
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_signals_happy_path(port, pg_pool):
    sid_match = await _insert_signal(
        pg_pool,
        title="Itaipu turbine upgrade complete",
        summary="Brazil's Itaipu Binacional finished its third-gen turbine refit on Tuesday.",
        category="energy",
    )
    # Non-matching noise row.
    await _insert_signal(
        pg_pool,
        title="Argentina wheat harvest projections",
        summary="Crop forecast unchanged.",
        category="agriculture",
    )

    result = await port.search_signals(query="Itaipu turbine", limit=10)

    assert result["backing"] == "postgres_fts"
    assert result["scope_predicate_applied"] is False
    refs = result["refs"]
    assert str(sid_match) in refs
    # The matching row should be the top hit.
    assert result["rows"][0]["id"] == str(sid_match)
    assert result["rows"][0]["category"] == "energy"
    assert result["rows"][0]["rank"] > 0.0


@pytest.mark.asyncio
async def test_search_signals_category_param_removed(port, pg_pool):
    # W2-T5 residual (2026-07): the dead ``category`` FILTER param is GONE —
    # 0 live signals carry the payload key, so any value turned every query
    # into an honest-looking empty result. Passing it must fail loudly, and
    # rows matching on text must come back regardless of their payload
    # category value (no hidden narrowing).
    sid_energy = await _insert_signal(
        pg_pool,
        title="Hydroelectric expansion plan filed",
        summary="Brazil hydropower",
        category="energy",
    )
    sid_env = await _insert_signal(
        pg_pool,
        title="Hydroelectric concerns flagged by NGO",
        summary="Brazil hydropower",
        category="environment",
    )

    with pytest.raises(TypeError):
        await port.search_signals(
            query="hydroelectric", category="energy", limit=10,
        )

    both = await port.search_signals(query="hydroelectric", limit=10)
    assert str(sid_energy) in both["refs"]
    assert str(sid_env) in both["refs"]
    assert "category" not in both  # no dead filter echo in the envelope


@pytest.mark.asyncio
async def test_search_signals_empty_query(port):
    result = await port.search_signals(query="   ", limit=5)
    assert result["rows"] == []
    assert result["refs"] == []
    assert result["note"] == "empty_query"


@pytest.mark.asyncio
async def test_search_signals_no_match(port, pg_pool):
    await _insert_signal(
        pg_pool,
        title="Routine maintenance log",
        summary="Nothing notable",
        category="other",
    )
    result = await port.search_signals(
        query="xyzzy_unmatched_token_zzzqqq", limit=5,
    )
    assert result["rows"] == []
    assert result["refs"] == []


@pytest.mark.asyncio
async def test_search_signals_limit_clamped(port, pg_pool):
    # Insert a few matching rows.
    for i in range(3):
        await _insert_signal(
            pg_pool,
            title=f"Solar farm update {i}",
            summary="renewable energy expansion",
            category="energy",
        )
    huge = await port.search_signals(query="solar farm", limit=10_000)
    # Just confirm the limit was clamped — we don't have 200 rows but
    # the SQL must have applied LIMIT _MAX_ROW_LIMIT (=200).
    assert len(huge["rows"]) <= 200


@pytest.mark.asyncio
async def test_search_signals_scope_predicate_echo(port, pg_pool):
    await _insert_signal(
        pg_pool, title="Diplomatic statement issued", summary="press release",
        category="politics",
    )
    result = await port.search_signals(
        query="diplomatic statement",
        scope_predicate='target_id == "br-energy"',
    )
    assert result["scope_predicate_applied"] is False
    assert "scope_predicate_note" in result


# ---------------------------------------------------------------------------
# query_facts
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_query_facts_by_predicate(port, pg_pool):
    fid = await _insert_fact(
        pg_pool, subject="Brazil", predicate="capital", value="Brasilia",
    )
    await _insert_fact(
        pg_pool, subject="Argentina", predicate="capital", value="Buenos Aires",
    )
    result = await port.query_facts(predicate="capital", limit=10)
    refs = result["refs"]
    assert str(fid) in refs
    assert len(result["rows"]) >= 2
    assert all(r["predicate"] == "capital" for r in result["rows"])


@pytest.mark.asyncio
async def test_query_facts_combined_filters(port, pg_pool):
    fid = await _insert_fact(
        pg_pool, subject="Itaipu", predicate="located_in", value="Brazil",
    )
    await _insert_fact(
        pg_pool, subject="Itaipu", predicate="kind", value="Hydroelectric",
    )
    result = await port.query_facts(
        subject="Itaipu", predicate="located_in", limit=10,
    )
    assert str(fid) in result["refs"]
    assert len(result["rows"]) == 1
    row = result["rows"][0]
    assert row["subject"] == "Itaipu"
    assert row["predicate"] == "located_in"
    assert row["value"] == "Brazil"


@pytest.mark.asyncio
async def test_query_facts_surfaces_source_type(port, pg_pool):
    """F1: every fact row carries source_type so a reader (consult LLM / agency
    tools / UI) can tell vetted ground truth from automated ingestion and
    discount the latter — labelled, not dropped."""
    await _insert_fact(
        pg_pool, subject="Provenanceland", predicate="capital", value="X",
    )
    result = await port.query_facts(subject="Provenanceland", limit=5)
    assert result["rows"]
    row = result["rows"][0]
    assert "source_type" in row
    assert row["source_type"] == "agent"  # _insert_fact stamps 'agent'


@pytest.mark.asyncio
async def test_query_facts_requires_filter(port):
    result = await port.query_facts(limit=5)
    assert result["rows"] == []
    assert "error" in result
    assert "at least one" in result["error"]


@pytest.mark.asyncio
async def test_query_facts_empty_result(port):
    result = await port.query_facts(
        predicate="nonexistent_predicate_zzqq", limit=5,
    )
    assert result["rows"] == []
    assert result["refs"] == []
    assert "error" not in result


@pytest.mark.asyncio
async def test_query_facts_excludes_superseded(port, pg_pool):
    """A fact whose ``superseded_by`` points at a successor must NOT be
    returned — query_facts surfaces only current (open) rows."""
    pred = "leader_superseded_test"
    successor = await _insert_fact(
        pg_pool, subject="Atlantis", predicate=pred, value="New Regent",
    )
    superseded = await _insert_fact(
        pg_pool, subject="Atlantis", predicate=pred, value="Old Regent",
        superseded_by=successor,
    )
    result = await port.query_facts(predicate=pred, limit=10)
    refs = result["refs"]
    assert str(successor) in refs
    assert str(superseded) not in refs
    assert all(r["value"] != "Old Regent" for r in result["rows"])


@pytest.mark.asyncio
async def test_query_facts_excludes_expired(port, pg_pool):
    """A fact with a non-NULL ``valid_until`` (explicitly expired) must NOT
    be returned even when no successor superseded it."""
    pred = "treaty_status_expired_test"
    expired = await _insert_fact(
        pg_pool, subject="Eldoria", predicate=pred, value="Signatory",
        valid_until=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )
    current = await _insert_fact(
        pg_pool, subject="Eldoria", predicate=pred, value="Withdrawn",
    )
    result = await port.query_facts(predicate=pred, limit=10)
    refs = result["refs"]
    assert str(current) in refs
    assert str(expired) not in refs


# ---------------------------------------------------------------------------
# inspect_entity
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inspect_entity_happy_path(port, pg_pool):
    eid = await _insert_entity(
        pg_pool, canonical_name="Itaipu Binacional",
        entity_class="organization",
    )
    v1 = await _insert_entity_version(
        pg_pool, entity_id=eid, version=1, cycle_number=10,
    )
    v2 = await _insert_entity_version(
        pg_pool, entity_id=eid, version=2, cycle_number=11,
    )
    sid = await _insert_signal(
        pg_pool,
        title="Itaipu announcement",
        summary="Operational update",
        category="energy",
    )
    await _link_signal_to_entity(pg_pool, signal_id=sid, entity_id=eid)

    result = await port.inspect_entity(name="Itaipu Binacional")
    assert result["found"] is True
    profile = result["profile"]
    assert profile["id"] == str(eid)
    assert profile["canonical_name"] == "Itaipu Binacional"
    assert profile["entity_class"] == "organization"

    version_ids = {v["id"] for v in result["versions"]}
    assert str(v1) in version_ids
    assert str(v2) in version_ids
    # Versions returned newest-first.
    assert result["versions"][0]["version"] >= result["versions"][-1]["version"]

    mention_ids = {m["signal_id"] for m in result["recent_signal_mentions"]}
    assert str(sid) in mention_ids

    # refs covers entity + versions + signals.
    assert str(eid) in result["refs"]
    assert str(v1) in result["refs"]
    assert str(sid) in result["refs"]


@pytest.mark.asyncio
async def test_inspect_entity_surfaces_current_facts_only(port, pg_pool):
    """inspect_entity attaches facts keyed by subject = canonical_name and
    gates them to current rows — superseded / expired facts are excluded."""
    name = "Republic of Inspectia"
    await _insert_entity(
        pg_pool, canonical_name=name, entity_class="country",
    )
    successor = await _insert_fact(
        pg_pool, subject=name, predicate="capital", value="New Capital",
    )
    superseded = await _insert_fact(
        pg_pool, subject=name, predicate="capital", value="Old Capital",
        superseded_by=successor,
    )
    expired = await _insert_fact(
        pg_pool, subject=name, predicate="alliance", value="Former Bloc",
        valid_until=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )

    result = await port.inspect_entity(name=name)
    assert result["found"] is True
    fact_ids = {f["id"] for f in result["facts"]}
    assert str(successor) in fact_ids
    assert str(superseded) not in fact_ids
    assert str(expired) not in fact_ids
    # The current fact's id also rides the refs list for citation.
    assert str(successor) in result["refs"]
    assert str(superseded) not in result["refs"]


@pytest.mark.asyncio
async def test_inspect_entity_case_insensitive(port, pg_pool):
    await _insert_entity(
        pg_pool, canonical_name="Ministry of Mines", entity_class="organization",
    )
    result = await port.inspect_entity(name="ministry of mines")
    assert result["found"] is True
    assert result["profile"]["canonical_name"] == "Ministry of Mines"


@pytest.mark.asyncio
async def test_inspect_entity_not_found(port):
    result = await port.inspect_entity(
        name="Nonexistent Org That Should Not Exist 12345",
    )
    assert result["found"] is False
    assert result["facts"] == []
    assert result["versions"] == []
    assert result["recent_signal_mentions"] == []
    assert result["refs"] == []


@pytest.mark.asyncio
async def test_inspect_entity_empty_name(port):
    result = await port.inspect_entity(name="   ")
    assert result["found"] is False
    assert "error" in result


# ---------------------------------------------------------------------------
# vector_search (Protocol-facing)
# ---------------------------------------------------------------------------


@skip_unless_qdrant_live
@pytest.mark.asyncio
async def test_vector_search_reports_unavailable(port):
    """Without an embedder wired through the port surface, vector_search
    must honestly report unavailable rather than fabricate vectors."""
    result = await port.vector_search(query="Itaipu hydroelectric", limit=5)
    assert result["unavailable"] is True
    assert result["rows"] == []
    assert "no_embedder_wired" in result["reason"]
    assert result["collection"] == "legba_test_sqp__signals"


# ---------------------------------------------------------------------------
# vector_search_by_embedding (helper for embedder-aware callers)
# ---------------------------------------------------------------------------


@skip_unless_qdrant_live
@pytest.mark.asyncio
async def test_vector_search_by_embedding_happy_path(port, real_qdrant):
    """Seed a small collection with two distinct vectors; assert the
    nearer one ranks first and that the target_id payload filter works."""
    from qdrant_client.http import models as qmodels

    collection = "legba_test_sqp__signals"
    # Recreate the collection deterministically so the test is hermetic.
    cols = await real_qdrant.get_collections()
    if any(c.name == collection for c in cols.collections):
        await real_qdrant.delete_collection(collection_name=collection)
    await real_qdrant.create_collection(
        collection_name=collection,
        vectors_config=qmodels.VectorParams(
            size=4, distance=qmodels.Distance.COSINE,
        ),
    )

    sid_a = str(uuid4())
    sid_b = str(uuid4())
    await real_qdrant.upsert(
        collection_name=collection,
        points=[
            qmodels.PointStruct(
                id=sid_a,
                vector=[1.0, 0.0, 0.0, 0.0],
                payload={
                    "external_id": "ext-a",
                    "source_id": "src-1",
                    "target_id": "br-energy",
                    "fetched_at": "2026-05-20T00:00:00+00:00",
                },
            ),
            qmodels.PointStruct(
                id=sid_b,
                vector=[0.0, 1.0, 0.0, 0.0],
                payload={
                    "external_id": "ext-b",
                    "source_id": "src-2",
                    "target_id": "ar-grain",
                    "fetched_at": "2026-05-20T00:00:00+00:00",
                },
            ),
        ],
    )

    # Query near point A; A should rank first.
    near_a = await port.vector_search_by_embedding(
        query_embedding=[0.9, 0.1, 0.0, 0.0], limit=5,
    )
    assert near_a["collection"] == collection
    assert near_a["rows"][0]["signal_id"] == sid_a
    assert near_a["rows"][0]["target_id"] == "br-energy"
    assert sid_a in near_a["refs"]
    assert sid_b in near_a["refs"]

    # target_id filter narrows to just point B.
    filtered = await port.vector_search_by_embedding(
        query_embedding=[0.0, 0.5, 0.5, 0.0],
        target_id="ar-grain",
        limit=5,
    )
    assert filtered["filtered_target_id"] == "ar-grain"
    assert len(filtered["rows"]) == 1
    assert filtered["rows"][0]["signal_id"] == sid_b


@skip_unless_qdrant_live
@pytest.mark.asyncio
async def test_vector_search_by_embedding_empty_collection(port, real_qdrant):
    """Empty / freshly-created collection returns zero rows, no error."""
    from qdrant_client.http import models as qmodels

    collection = "legba_test_sqp__signals"
    cols = await real_qdrant.get_collections()
    if any(c.name == collection for c in cols.collections):
        await real_qdrant.delete_collection(collection_name=collection)
    await real_qdrant.create_collection(
        collection_name=collection,
        vectors_config=qmodels.VectorParams(
            size=4, distance=qmodels.Distance.COSINE,
        ),
    )

    result = await port.vector_search_by_embedding(
        query_embedding=[1.0, 0.0, 0.0, 0.0], limit=5,
    )
    assert result["rows"] == []
    assert result["refs"] == []
    assert "error" not in result


# ---------------------------------------------------------------------------
# query_nexuses (S4-T6)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_query_nexuses_by_subject_and_object(port, pg_pool):
    nid = await _insert_nexus(
        pg_pool, subject="Atlantis", object_="Eldoria",
        rel_type="AlliedWith", polarity=1, intent="supportive",
    )
    await _insert_nexus(
        pg_pool, subject="Borealia", object_="Eldoria",
        rel_type="HostileTo", polarity=-1, intent="hostile",
    )
    result = await port.query_nexuses(subject="Atlantis", obj="Eldoria")
    refs = result["refs"]
    assert str(nid) in refs
    assert len(result["rows"]) == 1
    row = result["rows"][0]
    assert row["subject"] == "Atlantis"
    assert row["object"] == "Eldoria"
    assert row["rel_type"] == "AlliedWith"
    assert row["polarity"] == 1
    assert "source_type" in row  # F1: provenance rides on the read surface


@pytest.mark.asyncio
async def test_query_nexuses_polarity_filter(port, pg_pool):
    hostile = await _insert_nexus(
        pg_pool, subject="Carpathia", object_="Drava",
        rel_type="HostileTo", polarity=-1,
    )
    await _insert_nexus(
        pg_pool, subject="Carpathia", object_="Drava",
        rel_type="TradesWith", polarity=1,
    )
    result = await port.query_nexuses(subject="Carpathia", polarity=-1)
    refs = result["refs"]
    assert str(hostile) in refs
    assert all(r["polarity"] == -1 for r in result["rows"])


@pytest.mark.asyncio
async def test_query_nexuses_excludes_superseded_and_expired(port, pg_pool):
    """A superseded or explicitly-expired nexus must NOT be returned —
    query_nexuses honors the OPEN-nexus gate (valid_until IS NULL AND
    superseded_by IS NULL)."""
    rel = "SuppliesWeaponsTo"
    successor = await _insert_nexus(
        pg_pool, subject="Frosthold", object_="Gleam",
        rel_type=rel, polarity=-1,
    )
    superseded = await _insert_nexus(
        pg_pool, subject="Frosthold", object_="Gleam",
        rel_type=rel, polarity=-1, superseded_by=successor,
    )
    expired = await _insert_nexus(
        pg_pool, subject="Frosthold", object_="Hearth",
        rel_type=rel, polarity=-1,
        valid_until=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )
    result = await port.query_nexuses(subject="Frosthold")
    refs = result["refs"]
    assert str(successor) in refs
    assert str(superseded) not in refs
    assert str(expired) not in refs


@pytest.mark.asyncio
async def test_query_nexuses_limit_clamped(port, pg_pool):
    for i in range(3):
        await _insert_nexus(
            pg_pool, subject=f"Clampia-{i}", object_="Common",
            rel_type="ObservedNear", polarity=0,
        )
    result = await port.query_nexuses(obj="Common", limit=10_000)
    assert len(result["rows"]) <= 200


# ---------------------------------------------------------------------------
# query_hypotheses (S4-T6)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_query_hypotheses_by_target_and_status(port, pg_pool):
    tid = "hyp-target-alpha"
    confirmed = await _insert_hypothesis(
        pg_pool, thesis="Coup attempt will fail",
        counter_thesis="Coup will succeed",
        evidence_balance=3, status="confirmed", target_id=tid,
        supporting_signals=[uuid4(), uuid4()],
    )
    await _insert_hypothesis(
        pg_pool, thesis="Election will be delayed",
        status="active", target_id=tid,
    )
    result = await port.query_hypotheses(target_id=tid, status="confirmed")
    refs = result["refs"]
    assert str(confirmed) in refs
    assert len(result["rows"]) == 1
    row = result["rows"][0]
    assert row["status"] == "confirmed"
    assert row["evidence_balance"] == 3
    assert row["supporting_count"] == 2
    assert row["target_id"] == tid


@pytest.mark.asyncio
async def test_query_hypotheses_surfaces_exogenous_resolution(port, pg_pool):
    """The exogenous resolution columns (0038) ride the result so a consult
    can tell a world-resolved hypothesis from a self-consistent one."""
    tid = "hyp-target-resolved"
    resolved = await _insert_hypothesis(
        pg_pool, thesis="Treaty will be ratified",
        status="confirmed", evidence_balance=2, target_id=tid,
        resolved_outcome=1, resolved_by="subsequent_facts",
    )
    result = await port.query_hypotheses(target_id=tid)
    rows = {r["id"]: r for r in result["rows"]}
    assert str(resolved) in rows
    row = rows[str(resolved)]
    assert row["resolved_outcome"] == 1
    assert row["resolved_by"] == "subsequent_facts"
    assert row["resolved_at"] is not None


@pytest.mark.asyncio
async def test_query_hypotheses_no_filter_returns_recent(port, pg_pool):
    hid = await _insert_hypothesis(
        pg_pool, thesis="Baseline hypothesis nofilter test",
        target_id="hyp-target-nofilter",
    )
    result = await port.query_hypotheses(limit=200)
    assert str(hid) in result["refs"]


# ---------------------------------------------------------------------------
# get_timeline (S4-T6)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_timeline_merges_facts_and_signals_newest_first(port, pg_pool):
    """get_timeline merges current facts + FTS-matched signals into one
    newest-first stream, each anchored on its own timestamp."""
    subj = "Timelinia"
    newer = datetime(2026, 5, 1, tzinfo=timezone.utc)
    sig = await _insert_signal(
        pg_pool, title="Timelinia signs trade pact",
        summary="Timelinia diplomatic development", category="politics",
        produced_at=newer,
    )
    fact = await _insert_fact(
        pg_pool, subject=subj, predicate="capital", value="Centropolis",
    )
    result = await port.get_timeline(subject=subj, limit=20)
    item_ids = [i["id"] for i in result["items"]]
    assert str(sig) in item_ids
    assert str(fact) in item_ids
    # Every item carries a kind + a non-null anchor.
    assert all(i["at"] is not None for i in result["items"])
    assert all(i["kind"] in {"fact", "signal"} for i in result["items"])
    # Newest-first ordering across the merged stream.
    anchors = [i["at"] for i in result["items"]]
    assert anchors == sorted(anchors, reverse=True)
    # refs == the surfaced item ids.
    assert result["refs"] == item_ids


@pytest.mark.asyncio
async def test_get_timeline_includes_situation_frames(port, pg_pool):
    """get_timeline merges situation FRAMES alongside facts/signals — situations
    are the persistent-frame substitute for an events table (5b/5c S-8). The
    frame anchors on valid_from and carries its span end (`until`)."""
    subj = f"Framelandia_{datetime.now(tz=timezone.utc).timestamp():.0f}"
    vf = datetime(2026, 2, 28, tzinfo=timezone.utc)
    async with pg_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO situations "
            "(id, data, name, status, category, intensity_score, "
            " situation_signature, valid_from, valid_until, analyst_id) "
            "VALUES (gen_random_uuid(), '{}'::jsonb, $1, 'closed', 'x', 1.7, $2, "
            "$3, $4, 'situation_clustering')",
            f"{subj} war escalates", f"sig:{subj}",
            vf, datetime(2026, 3, 5, tzinfo=timezone.utc),
        )
    result = await port.get_timeline(subject=subj, limit=20)
    sit_items = [i for i in result["items"] if i["kind"] == "situation"]
    assert len(sit_items) == 1
    assert sit_items[0]["name"] == f"{subj} war escalates"
    assert sit_items[0]["at"] == vf.isoformat()
    assert sit_items[0]["until"] is not None        # closed frame has a span end
    assert result["counts"]["situations"] == 1


@pytest.mark.asyncio
async def test_get_timeline_skips_superseded_facts(port, pg_pool):
    """The fact stream gates to current rows — a superseded fact is absent
    from the timeline."""
    subj = "Supersedia"
    successor = await _insert_fact(
        pg_pool, subject=subj, predicate="leader", value="New Chief",
    )
    superseded = await _insert_fact(
        pg_pool, subject=subj, predicate="leader", value="Old Chief",
        superseded_by=successor,
    )
    result = await port.get_timeline(subject=subj, limit=20)
    item_ids = {i["id"] for i in result["items"]}
    assert str(successor) in item_ids
    assert str(superseded) not in item_ids


@pytest.mark.asyncio
async def test_get_timeline_per_kind_floor_keeps_sparse_facts(port, pg_pool):
    """DQ-#70/F5: a dense signal stream must NOT crowd the sparse facts out of
    the clamped window. The lone fact (older than the signal burst) would be
    excluded by pure newest-first recency at limit=6; the per-kind floor keeps
    it visible."""
    subj = f"Flooria_{datetime.now(tz=timezone.utc).timestamp():.0f}"
    fact = await _insert_fact(
        pg_pool, subject=subj, predicate="capital", value="Floortown",
    )
    # 12 signals dated in the FUTURE → strictly newer than the fact, so a pure
    # recency clamp at limit=6 would surface 6 signals and drop the fact.
    for i in range(12):
        await _insert_signal(
            pg_pool, title=f"{subj} update {i}", summary=f"{subj} news",
            category="x", produced_at=datetime(2027, 1, 1, i, tzinfo=timezone.utc),
        )
    result = await port.get_timeline(subject=subj, limit=6)
    item_ids = {i["id"] for i in result["items"]}
    assert len(result["items"]) == 6
    assert str(fact) in item_ids, "per-kind floor must keep the sparse fact visible"
    assert result["counts"]["facts"] >= 1
    assert result["counts"]["signals"] >= 1


@pytest.mark.asyncio
async def test_get_timeline_empty_subject_guard(port):
    result = await port.get_timeline(subject="   ", limit=5)
    assert result["items"] == []
    assert result["refs"] == []
    assert "error" in result


# ---------------------------------------------------------------------------
# compare_targets (S4-T6)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compare_targets_rolls_up_per_target(port, pg_pool):
    ta, tb = "cmp-target-a", "cmp-target-b"
    # Target A: one open nexus + one active hypothesis + one live finding,
    # all carrying target_id=ta (the _insert_fact helper leaves target_id
    # NULL, so the fact rollup is exercised separately in the port unit
    # suite — here we assert the nexus / hypothesis / finding rollups).
    await _insert_nexus(
        pg_pool, subject="EntityA", object_="EntityB",
        rel_type="AlliedWith", polarity=1, target_id=ta,
    )
    await _insert_hypothesis(
        pg_pool, thesis="A-thesis", status="active", target_id=ta,
    )
    await _insert_finding(
        pg_pool, title="Finding for A", target_id=ta, confidence=0.7,
    )
    # Target B: two hypotheses (one confirmed, one refuted) + one finding.
    await _insert_hypothesis(
        pg_pool, thesis="B-thesis-1", status="confirmed", target_id=tb,
    )
    await _insert_hypothesis(
        pg_pool, thesis="B-thesis-2", status="refuted", target_id=tb,
    )
    await _insert_finding(
        pg_pool, title="Finding for B", target_id=tb, confidence=0.55,
    )

    result = await port.compare_targets(target_ids=[ta, tb])
    assert result["compared"] == [ta, tb]
    by_id = {t["target_id"]: t for t in result["targets"]}

    a = by_id[ta]
    assert a["open_nexus_count"] == 1
    assert a["hypothesis_status_mix"].get("active") == 1
    assert any(f["title"] == "Finding for A" for f in a["recent_findings"])

    b = by_id[tb]
    assert b["hypothesis_status_mix"].get("confirmed") == 1
    assert b["hypothesis_status_mix"].get("refuted") == 1
    assert any(f["title"] == "Finding for B" for f in b["recent_findings"])


@pytest.mark.asyncio
async def test_compare_targets_excludes_superseded_findings(port, pg_pool):
    """A superseded finding is not counted in the per-target rollup."""
    ta, tb = "cmp-sup-a", "cmp-sup-b"
    successor = await _insert_finding(
        pg_pool, title="Live A finding", target_id=ta,
    )
    await _insert_finding(
        pg_pool, title="Stale A finding", target_id=ta,
        superseded_by=successor,
    )
    await _insert_finding(pg_pool, title="B finding", target_id=tb)

    result = await port.compare_targets(target_ids=[ta, tb])
    by_id = {t["target_id"]: t for t in result["targets"]}
    a_titles = {f["title"] for f in by_id[ta]["recent_findings"]}
    assert "Live A finding" in a_titles
    assert "Stale A finding" not in a_titles


@pytest.mark.asyncio
async def test_compare_targets_requires_two_distinct(port):
    # A single id (even repeated) is a degenerate comparison.
    result = await port.compare_targets(target_ids=["only-one", "only-one"])
    assert result["targets"] == []
    assert "error" in result
    assert "at least two" in result["error"]


# ---------------------------------------------------------------------------
# Palette expansion — finished-intelligence / navigation readers.
#
# These run the NEW SQL against the migrated schema with empty tables: an
# empty SELECT still parses + plans every column reference, so a bad column
# name surfaces as an asyncpg UndefinedColumnError here (the dominant risk
# for the hand-written list_situations / list_sources / query_predictions
# SQL). Shape + filter-branch coverage; row-content correctness is exercised
# for list_findings via the reused substrate_reads_api SQL elsewhere and
# live-verified by the deploying session.
# ---------------------------------------------------------------------------


def _assert_rows_refs_count(out: dict) -> None:
    assert set(out) >= {"rows", "refs", "count"}
    assert isinstance(out["rows"], list)
    assert isinstance(out["refs"], list)
    assert out["count"] == len(out["rows"])


@pytest.mark.asyncio
async def test_list_findings_sql_executes(port):
    # No filters, then every WHERE branch — exercises the critic LEFT JOIN
    # LATERAL + effective_confidence projection against the real schema.
    _assert_rows_refs_count(await port.list_findings(limit=5))
    _assert_rows_refs_count(
        await port.list_findings(
            target_id="country_g20_ir",
            analyst_id="country_assessor",
            severity="high",
            since_hours=48,
            include_superseded=True,
            limit=5,
        )
    )


@pytest.mark.asyncio
async def test_list_findings_excludes_superseded_by_default(port, pg_pool):
    """R1 / W2-T1 (read-truth): the shared list_findings handler (consult +
    journal_read + deep_consult all route here) serves only LIVE finding heads
    by default — a superseded revision must not double-count."""
    tid = "lf-superseded-gate"
    successor = await _insert_finding(pg_pool, title="Live head", target_id=tid)
    stale = await _insert_finding(
        pg_pool, title="Stale revision", target_id=tid, superseded_by=successor,
    )

    out = await port.list_findings(target_id=tid, limit=10)
    ids = {r["id"] for r in out["rows"]}
    assert str(successor) in ids
    assert str(stale) not in ids
    assert out["count"] == 1


@pytest.mark.asyncio
async def test_list_findings_include_superseded_flag_relaxes_gate(port, pg_pool):
    """include_superseded=True is the explicit history/audit opt-in — both the
    live head and its superseded ancestor surface."""
    tid = "lf-superseded-optin"
    successor = await _insert_finding(pg_pool, title="Live head", target_id=tid)
    stale = await _insert_finding(
        pg_pool, title="Stale revision", target_id=tid, superseded_by=successor,
    )

    out = await port.list_findings(
        target_id=tid, include_superseded=True, limit=10,
    )
    ids = {r["id"] for r in out["rows"]}
    assert {str(successor), str(stale)} <= ids
    assert out["count"] == 2


@pytest.mark.asyncio
async def test_list_situations_sql_executes(port):
    _assert_rows_refs_count(await port.list_situations(limit=5))
    _assert_rows_refs_count(
        await port.list_situations(
            status="open", target_id="country_g20_ir", since_hours=72, limit=5,
        )
    )


@pytest.mark.asyncio
async def test_query_predictions_sql_executes(port):
    _assert_rows_refs_count(await port.query_predictions(limit=5))
    _assert_rows_refs_count(
        await port.query_predictions(
            target_id="country_g20_ir", status="open", limit=5,
        )
    )


@pytest.mark.asyncio
async def test_query_predictions_maps_writer_extras(port, pg_pool):
    # STORED SHAPE (W2-T6 / M3): the emit path
    # (actor_payload._PAYLOAD_SELECTORS[OutputKind.PREDICTION]) UNWRAPS the
    # analyst-side finding.data["prediction"] blob, so the analyst_outputs
    # row's ``data`` IS the PredictionPayload dump at the TOP LEVEL — with
    # the writer's extras keys ci_lower / ci_upper / method (NOT
    # ci_low/ci_high/forecast_method). Insert a row exactly as it is STORED
    # and assert the reader surfaces those onto its ci_low/ci_high/
    # forecast_method fields. (The old nested data->'prediction' read path
    # matched zero live rows and was deleted.)
    oid = uuid4()
    blob = {
        "hypothesis": "event volume rises",
        "status": "open",
        "confidence": 0.6,
        "point_estimate": 12.5,
        "ci_lower": 8.0,
        "ci_upper": 17.0,
        "ci_level": 0.9,
        "horizon_days": 30,
        "method": "AutoARIMA",
        "narrative": "Trend up.",
    }
    async with pg_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO analyst_outputs (
                id, kind, title, body, confidence, severity, data,
                target_id, produced_at, derived_from, schema_uri, run_id
            ) VALUES (
                $1, 'prediction', '', '', 0.6, NULL, $2::jsonb,
                'country_g20_ir', NOW(), '{}'::uuid[],
                'iglu:legba/prediction/jsonschema/1-0-0', NULL
            )
            """,
            oid, json.dumps(blob),
        )

    # A RESOLVED prediction, written the way the resolver actually does it
    # (calibration_tracking): lifecycle status + outcome merged over the
    # stored top-level dump via jsonb ``||`` — status flips 'open' →
    # 'resolved' in place.
    oid_resolved = uuid4()
    resolved_blob = {
        "hypothesis": "h2",
        "point_estimate": 3.0,
        "status": "resolved",          # the resolver's merge overwrote 'open'
        "resolved_outcome": "hit",     # the resolver's merge
    }
    async with pg_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO analyst_outputs (
                id, kind, title, body, confidence, severity, data,
                target_id, produced_at, derived_from, schema_uri, run_id
            ) VALUES (
                $1, 'prediction', '', '', 0.6, NULL, $2::jsonb,
                'country_g20_ir', NOW(), '{}'::uuid[],
                'iglu:legba/prediction/jsonschema/1-0-0', NULL
            )
            """,
            oid_resolved, json.dumps(resolved_blob),
        )

    out = await port.query_predictions(target_id="country_g20_ir", limit=5)
    assert str(oid) in out["refs"]
    row = next(r for r in out["rows"] if r["id"] == str(oid))
    assert row["point_estimate"] == 12.5
    assert row["ci_low"] == 8.0          # mapped from ci_lower
    assert row["ci_high"] == 17.0        # mapped from ci_upper
    assert row["ci_level"] == 0.9
    assert row["horizon_days"] == 30
    assert row["forecast_method"] == "AutoARIMA"  # mapped from method
    assert row["narrative"] == "Trend up."
    assert row["status"] == "open"
    assert row["resolved_outcome"] is None

    # The graded row reports its TRUE lifecycle status + outcome.
    rrow = next(r for r in out["rows"] if r["id"] == str(oid_resolved))
    assert rrow["status"] == "resolved"
    assert rrow["resolved_outcome"] == "hit"

    # status filter reads the ONE canonical path (data->>'status'): 'open'
    # hits only the never-resolved row, 'resolved' only the graded row.
    open_refs = (await port.query_predictions(status="open", limit=5))["refs"]
    assert str(oid) in open_refs
    assert str(oid_resolved) not in open_refs
    resolved_refs = (await port.query_predictions(status="resolved", limit=5))["refs"]
    assert str(oid_resolved) in resolved_refs
    assert str(oid) not in resolved_refs

    # FEED HONESTY (W2-T6): the response states the feed is frozen and
    # carries the newest produced_at so a reader can refuse to present a
    # historical row as a current forecast.
    assert "FROZEN" in out["feed_note"]
    assert out["latest_produced_at"] is not None


# ---------------------------------------------------------------------------
# query_paths — polarity filter in SQL (W2-T6 / M5)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_query_paths_polarity_filter_in_sql(port, pg_pool):
    """W2-T6 / M5: the ``polarity_product`` filter is a SQL predicate BEFORE
    the LIMIT — a tight limit can no longer starve matching paths behind
    non-matching ones (the old Python post-filter only saw what survived the
    fetch cutoff). Graph: wt6m5_a -> wt6m5_b direct (+1, 1 hop — ranks
    first) and wt6m5_a -> wt6m5_c -> wt6m5_b (net -1, 2 hops)."""
    await _insert_edge(
        pg_pool, subject="wt6m5_a", object_="wt6m5_b",
        rel_type="supports", polarity=1,
    )
    await _insert_edge(
        pg_pool, subject="wt6m5_a", object_="wt6m5_c",
        rel_type="opposes", polarity=-1,
    )
    await _insert_edge(
        pg_pool, subject="wt6m5_c", object_="wt6m5_b",
        rel_type="supports", polarity=1,
    )

    unfiltered = await port.query_paths(subject="wt6m5_a", obj="wt6m5_b")
    assert len(unfiltered["paths"]) == 2

    # limit=1 + net -1: shortest-first ranking puts the +1 direct hop first,
    # so the LIMIT would eat it unless the polarity predicate runs in SQL
    # BEFORE the cutoff (the old Python post-filter only stayed correct via
    # a +100-row over-provision that a large path set could exhaust); the
    # matching 2-hop chain must surface.
    neg = await port.query_paths(
        subject="wt6m5_a", obj="wt6m5_b", polarity_product=-1, limit=1,
    )
    assert len(neg["paths"]) == 1
    assert neg["paths"][0]["polarity_product"] == -1
    assert neg["paths"][0]["hops"] == 2
    assert neg["polarity_product_filter"] == -1

    pos = await port.query_paths(
        subject="wt6m5_a", obj="wt6m5_b", polarity_product=1, limit=1,
    )
    assert len(pos["paths"]) == 1
    assert pos["paths"][0]["polarity_product"] == 1
    assert pos["paths"][0]["hops"] == 1


@pytest.mark.asyncio
async def test_query_paths_walk_advances_from_the_object(port, pg_pool):
    """The recursive seed must hand hop 2 the first edge's OBJECT. Seeding
    with the subject made every later hop re-expand from the origin — on live
    data that fabricated 64,136 "paths" where 1,517 exist. The W2-T6 test
    above never caught it because its triangle topology let origin-re-expansion
    produce a chain with the same node sequence and polarity product.

    Two topologies where the bug flips the ANSWER, not just the edge ids:

    * a chain ``qpw_a -> qpw_c -> qpw_b`` with NO direct edge: under the bug
      hop 2 re-expands from ``qpw_a`` (whose only edge is visited-blocked) and
      finds NOTHING — the real 2-hop path vanishes;
    * a fork ``qpf_a -> qpf_c`` + ``qpf_a -> qpf_b`` with no ``c -> b`` edge:
      under the bug hop 2 walks the second origin edge and reports a 2-hop
      ``a -> c -> b`` chain that NO edge sequence supports.
    """
    # Chain: the genuine 2-hop path must be found.
    await _insert_edge(
        pg_pool, subject="qpw_a", object_="qpw_c",
        rel_type="supports", polarity=1,
    )
    await _insert_edge(
        pg_pool, subject="qpw_c", object_="qpw_b",
        rel_type="supports", polarity=1,
    )
    chain = await port.query_paths(subject="qpw_a", obj="qpw_b")
    assert len(chain["paths"]) == 1
    assert chain["paths"][0]["hops"] == 2

    # Fork: no path a->..->b of length 2 exists beyond the direct edge; a
    # 2-hop result here is a fabricated chain.
    await _insert_edge(
        pg_pool, subject="qpf_a", object_="qpf_c",
        rel_type="supports", polarity=1,
    )
    await _insert_edge(
        pg_pool, subject="qpf_a", object_="qpf_b",
        rel_type="supports", polarity=1,
    )
    fork = await port.query_paths(subject="qpf_a", obj="qpf_b")
    assert [p["hops"] for p in fork["paths"]] == [1]


@pytest.mark.asyncio
async def test_list_targets_sql_executes(port):
    _assert_rows_refs_count(await port.list_targets(active_only=True))
    _assert_rows_refs_count(await port.list_targets(active_only=False))


@pytest.mark.asyncio
async def test_list_sources_sql_executes(port):
    _assert_rows_refs_count(await port.list_sources(active_only=True))
    _assert_rows_refs_count(
        await port.list_sources(
            active_only=False, silent_only=True, silent_hours=24,
        )
    )


# ---------------------------------------------------------------------------
# REGISTER-1h — ``new_situations`` counts NEW frames, not the clustering cron
# ---------------------------------------------------------------------------


async def _insert_situation(
    pg_pool, *, name: str, created_at: datetime, updated_at: datetime,
) -> UUID:
    """One ``situations`` row with its two clocks set INDEPENDENTLY.

    That independence is the whole point of the fixture: on the live substrate
    ``situation_clustering`` re-UPSERTs an existing frame every 20 minutes with
    ``updated_at=NOW()`` and never touches ``created_at``, so a frame opened
    weeks ago carries a fresh ``updated_at`` forever. The seed reproduces that
    shape directly rather than simulating the cron.
    """
    sid = uuid4()
    async with pg_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO situations (id, data, name, status, category, "
            "  event_count, intensity_score, analyst_id, produced_at, "
            "  derived_from, schema_uri, created_at, updated_at) "
            "VALUES ($1,'{}'::jsonb,$2,'active','test_register_1h',1,1.0,"
            "  'situation_clustering',$3,'{}'::uuid[],'u',$3,$4)",
            sid, name, created_at, updated_at,
        )
    return sid


@pytest.mark.asyncio
async def test_journal_delta_new_situations_counts_created_not_updated(
    pg_pool, port,
):
    """REGISTER-1h. The journal desks' "what changed since I last wrote" prompt
    carries ``delta.new_situations``. It read ``updated_at``, which on a MUTABLE
    row re-UPSERTed every 20 minutes is a count of the CRON: measured live on
    2026-08-29, 44 of 89 frames had an ``updated_at`` inside the last 20 minutes
    — the same 44 for the last hour and for the last 24 hours — while frames
    actually created in the last 24 hours numbered ZERO. The desk was told 44
    when the answer was 0.

    DISCRIMINATING BY CONSTRUCTION, and deliberately measured as a DIFFERENCE
    against a live baseline rather than as an absolute: the session DB is shared,
    so an absolute count would pin this file to its neighbours' rows. Insert the
    cron-shaped frame (old ``created_at``, fresh ``updated_at``) and the counter
    must not move; insert a genuinely new frame and it must move by exactly one.
    Under the old ``updated_at`` predicate the first assertion fails — the
    cron-shaped row is precisely the row that predicate counted.
    """
    now = datetime.now(timezone.utc)
    since = (now - timedelta(hours=24)).isoformat()

    baseline = (await port.get_journal_delta(since=since))["delta"]
    assert "new_situations" in baseline

    # (1) THE CRON ROW — opened a week ago, re-upserted one minute ago. This is
    # the live shape of 44 of 89 frames and it is NOT news.
    await _insert_situation(
        pg_pool,
        name="register-1h stale frame, freshly re-upserted",
        created_at=now - timedelta(days=7),
        updated_at=now - timedelta(minutes=1),
    )
    after_cron = (await port.get_journal_delta(since=since))["delta"]
    assert after_cron["new_situations"] == baseline["new_situations"], (
        "a frame merely RE-UPSERTED inside the window is not a new situation; "
        "counting it is counting the 20-minute clustering cadence"
    )

    # (2) THE GENUINELY NEW ROW — created inside the window. This is news.
    await _insert_situation(
        pg_pool,
        name="register-1h frame opened inside the window",
        created_at=now - timedelta(hours=1),
        updated_at=now - timedelta(minutes=1),
    )
    after_new = (await port.get_journal_delta(since=since))["delta"]
    assert after_new["new_situations"] == baseline["new_situations"] + 1, (
        "a frame CREATED inside the window must count exactly once"
    )

    # (3) The window still moves: widen the cursor past the cron row's opening
    # and it becomes a legitimately new frame for THAT window.
    wide = (now - timedelta(days=14)).isoformat()
    assert (
        (await port.get_journal_delta(since=wide))["delta"]["new_situations"]
        >= after_new["new_situations"] + 1
    )
