# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The two reads the workstation's SCOPE contract needs from ``/entities``.

Both were unavailable, and neither could be faked in the client:

  * **``/entities/graph?depth=2``** — ``_EGO_GRAPH_SQL`` was a single-hop query,
    so the second ring's edges were never fetched at any limit. v2 drew a
    depth-2 ego net on the selected entity; v3 could not, and no amount of
    client work could recover edges the server never sent.

  * **``/entities?finding_id=…``** — "which entities is THIS report about?" had
    no answer. The route took only ``q``/``entity_class``/``limit``, and
    ``entity`` is not a lineage ``row_kind``, so a caller holding a report's
    findings could only list the report's desk TARGETS and call them entities.
    The join was always in the substrate: ``analyst_outputs`` (findings are
    ``kind='finding'`` rows there) -> ``derived_from`` -> ``signals`` ->
    ``signal_entity_links`` -> ``entity_profiles``.

Every assertion below is about a BOUND as much as a result: the point of both
parameters is that they are the smallest additions that answer the question
without handing a caller an unbounded walk.
"""

from __future__ import annotations

import json
import os
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from nacl.signing import SigningKey

from legba.data.config import PostgresConfig
from legba.data.postgres import PostgresStore
from legba.data.registry.api import API_TOKEN_ENV, RegistryAPIDeps
from legba.data.registry.audit import AuditLogger
from legba.data.registry.credentials import MASTER_KEY_ENV, CredentialVault
from legba.data.registry.descriptor import DescriptorRegistry
from legba.data.registry.dlq import DescriptorDeadLetter
from legba.data.registry.entities_api import (
    MAX_EGO_DEPTH,
    MAX_FINDING_IDS,
    build_entities_router,
)
from legba.data.registry.signing import SigningIdentity
from legba.data.registry.stack import StackRegistry
from legba.data.registry.vocabulary_cache import VocabularyCache

_TEST_MASTER_KEY_HEX = "0011223344556677889900112233445566778899001122334455667788990011"
os.environ.setdefault(MASTER_KEY_ENV, _TEST_MASTER_KEY_HEX)
os.environ.setdefault("LEGBA_REGISTRY_SIGNING_KEY", "66" * 32)

#: A prefix nothing else in the suite uses, so teardown can be exact.
_P = "EscopeT"
_SOURCE = "source.escope.test"
_ANALYST = "escope_test_analyst"


async def _entity(conn, name: str, cls: str = "organization") -> UUID:
    return await conn.fetchval(
        """INSERT INTO entity_profiles (canonical_name, entity_class, entity_type, data)
           VALUES ($1, $2, $2, '{}'::jsonb) RETURNING id""",
        name,
        cls,
    )


async def _signal(conn, title: str) -> UUID:
    sid = uuid4()
    await conn.execute(
        "INSERT INTO signals (id, source_id, fetched_at, payload, content_hash) "
        "VALUES ($1, $2, now(), $3::jsonb, $4)",
        sid,
        _SOURCE,
        json.dumps({"title": title}),
        uuid4().hex,
    )
    return sid


async def _link(conn, signal_id: UUID, entity_id: UUID) -> None:
    await conn.execute(
        "INSERT INTO signal_entity_links (signal_id, entity_id, analyst_id) "
        "VALUES ($1, $2, $3) ON CONFLICT DO NOTHING",
        signal_id,
        entity_id,
        _ANALYST,
    )


async def _finding(conn, title: str, derived_from: list[UUID]) -> UUID:
    fid = uuid4()
    await conn.execute(
        """INSERT INTO analyst_outputs
             (id, kind, analyst_id, title, data, derived_from, schema_uri,
              produced_at)
           VALUES ($1, 'finding', $2, $3, '{}'::jsonb, $4::uuid[],
                   'legba://schema/finding/v1', now())""",
        fid,
        _ANALYST,
        title,
        derived_from,
    )
    return fid


@pytest_asyncio.fixture
async def seeded(migrated_pg: PostgresConfig):
    """A tiny world with a known shape.

    Graph (for `depth`), a straight chain so the rings are unambiguous::

        Hub --- Ring1 --- Ring2        Far (unreachable)

    Provenance (for `finding_id`)::

        world read --> desk finding --> signal S1 --> entity Alpha
                                    \\-> signal S2 --> entity Beta
        (unrelated) --------------------> signal S3 --> entity Gamma
    """
    os.environ.pop(API_TOKEN_ENV, None)

    pg_store = PostgresStore(migrated_pg)
    await pg_store.connect()

    identity = SigningIdentity(
        signing_key=SigningKey(b"entities-scope-reads-test-000001"[:32]),
        signer_did="did:legba:registry:entities-scope-test",
    )
    audit = AuditLogger(identity=identity)
    dlq = DescriptorDeadLetter(pg_store)
    vocab = VocabularyCache(pg_store)
    vault = CredentialVault(pg_store)
    descriptor_registry = DescriptorRegistry(
        pg_store,
        vocabulary_cache=vocab,
        signing_identity=identity,
        audit_logger=audit,
        dead_letter=dlq,
    )
    await descriptor_registry.start()
    deps = RegistryAPIDeps(
        descriptor_registry=descriptor_registry,
        stack_registry=StackRegistry(pg_store, vault, audit=audit, dlq=dlq),
        vault=vault,
        dlq=dlq,
        audit_logger=audit,
        vocabulary_cache=vocab,
        nats_store=None,
    )
    app = FastAPI()
    app.state.registry_deps = deps
    app.include_router(build_entities_router(deps), prefix="/api/v1/registry")

    ids: dict[str, UUID] = {}
    async with pg_store.acquire() as conn:
        for short in ("Hub", "Ring1", "Ring2", "Far", "Alpha", "Beta", "Gamma"):
            ids[short] = await _entity(conn, f"{_P}{short}")

        for src, dst in (("Hub", "Ring1"), ("Ring1", "Ring2")):
            await conn.execute(
                """INSERT INTO entity_edges
                     (src_id, dst_id, edge_type, edge_family, polarity,
                      confidence, observed_count, evidence_set)
                   VALUES ($1, $2, 'linked to', 'relation', 0, 0.9, 1,
                           '{"evidence_text": "seed"}'::jsonb)""",
                ids[src],
                ids[dst],
            )

        s1 = await _signal(conn, f"{_P} signal one")
        s2 = await _signal(conn, f"{_P} signal two")
        s3 = await _signal(conn, f"{_P} signal three")
        await _link(conn, s1, ids["Alpha"])
        await _link(conn, s2, ids["Beta"])
        await _link(conn, s3, ids["Gamma"])
        ids["s1"], ids["s2"], ids["s3"] = s1, s2, s3

        desk = await _finding(conn, f"{_P} desk finding", [s1, s2])
        world = await _finding(conn, f"{_P} world read", [desk])
        unrelated = await _finding(conn, f"{_P} unrelated", [s3])
        ids["desk"], ids["world"], ids["unrelated"] = desk, world, unrelated

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver",
    ) as client:
        yield client, ids

    async with pg_store.acquire() as conn:
        await conn.execute(
            "DELETE FROM analyst_outputs WHERE analyst_id = $1", _ANALYST)
        await conn.execute(
            "DELETE FROM signal_entity_links WHERE analyst_id = $1", _ANALYST)
        await conn.execute("DELETE FROM signals WHERE source_id = $1", _SOURCE)
        await conn.execute(
            """DELETE FROM entity_edges WHERE src_id IN
                 (SELECT id FROM entity_profiles WHERE canonical_name LIKE $1)""",
            f"{_P}%",
        )
        await conn.execute(
            "DELETE FROM entity_profiles WHERE canonical_name LIKE $1", f"{_P}%")
    await descriptor_registry.stop()
    await pg_store.close()


# ---------------------------------------------------------------------------
# /entities/graph?depth=
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_depth_defaults_to_one_and_is_the_query_it_always_was(seeded):
    """No `depth` ⇒ the single-hop ego net, unchanged. The parameter is additive."""
    client, _ = seeded
    r = await client.get(
        "/api/v1/registry/entities/graph", params={"center": f"{_P}Hub"})
    assert r.status_code == 200
    names = {n["canonical_name"] for n in r.json()["nodes"]}
    assert names == {f"{_P}Hub", f"{_P}Ring1"}
    assert f"{_P}Ring2" not in names


@pytest.mark.asyncio
async def test_depth_two_reaches_the_second_ring(seeded):
    """The whole point: an edge one hop further out than the centre's own."""
    client, _ = seeded
    r = await client.get(
        "/api/v1/registry/entities/graph",
        params={"center": f"{_P}Hub", "depth": 2},
    )
    assert r.status_code == 200
    body = r.json()
    names = {n["canonical_name"] for n in body["nodes"]}
    assert {f"{_P}Hub", f"{_P}Ring1", f"{_P}Ring2"} <= names
    # …and still not the unreachable node — a depth-2 ego net is not "everything".
    assert f"{_P}Far" not in names
    pairs = {(e["source"], e["target"]) for e in body["edges"]}
    assert (f"{_P}Hub", f"{_P}Ring1") in pairs
    assert (f"{_P}Ring1", f"{_P}Ring2") in pairs


@pytest.mark.asyncio
async def test_depth_two_never_duplicates_a_ring_one_edge(seeded):
    """The expansion re-reads ring 1's edges; they must land once, not twice."""
    client, _ = seeded
    r = await client.get(
        "/api/v1/registry/entities/graph",
        params={"center": f"{_P}Hub", "depth": 2},
    )
    pairs = [(e["source"], e["target"]) for e in r.json()["edges"]]
    assert len(pairs) == len(set(pairs))


@pytest.mark.asyncio
async def test_depth_is_bounded_at_the_declared_ceiling(seeded):
    """Three hops is a neighbourhood, not an ego net — and a different cost class."""
    client, _ = seeded
    r = await client.get(
        "/api/v1/registry/entities/graph",
        params={"center": f"{_P}Hub", "depth": MAX_EGO_DEPTH + 1},
    )
    assert r.status_code == 422
    r = await client.get(
        "/api/v1/registry/entities/graph",
        params={"center": f"{_P}Hub", "depth": 0},
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_depth_without_a_center_is_still_the_top_n_graph(seeded):
    """`depth` describes an EGO walk; with no centre there is no ego to walk."""
    client, _ = seeded
    r = await client.get(
        "/api/v1/registry/entities/graph", params={"limit": 300, "depth": 2})
    assert r.status_code == 200
    pairs = {(e["source"], e["target"]) for e in r.json()["edges"]}
    assert (f"{_P}Hub", f"{_P}Ring1") in pairs


# ---------------------------------------------------------------------------
# /entities?finding_id=
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_finding_id_returns_the_entities_that_findings_cited_signals_mention(seeded):
    """One hop: a desk finding cites its signals directly."""
    client, ids = seeded
    r = await client.get(
        "/api/v1/registry/entities",
        params={"finding_id": str(ids["desk"]), "limit": 100},
    )
    assert r.status_code == 200
    names = {n["canonical_name"] for n in r.json()["data"]}
    assert names == {f"{_P}Alpha", f"{_P}Beta"}


@pytest.mark.asyncio
async def test_finding_id_walks_two_provenance_hops_for_a_world_read(seeded):
    """Two hops: a world read cites desk findings, which cite the signals.

    This is the shape the Navigator actually sends — the read's block heads —
    and a one-hop walk would return NOTHING for it, which is the failure mode
    that would read as "this report is about no entities".
    """
    client, ids = seeded
    r = await client.get(
        "/api/v1/registry/entities",
        params={"finding_id": str(ids["world"]), "limit": 100},
    )
    assert r.status_code == 200
    names = {n["canonical_name"] for n in r.json()["data"]}
    assert names == {f"{_P}Alpha", f"{_P}Beta"}


@pytest.mark.asyncio
async def test_finding_id_EXCLUDES_entities_the_report_does_not_cite(seeded):
    """A filter, not a re-ranking: an unmentioned entity is absent, not a zero."""
    client, ids = seeded
    r = await client.get(
        "/api/v1/registry/entities",
        params={"finding_id": str(ids["desk"]), "limit": 100},
    )
    names = {n["canonical_name"] for n in r.json()["data"]}
    assert f"{_P}Gamma" not in names
    assert f"{_P}Hub" not in names


@pytest.mark.asyncio
async def test_repeatable_finding_id_unions_the_reports(seeded):
    client, ids = seeded
    r = await client.get(
        "/api/v1/registry/entities",
        params={
            "finding_id": [str(ids["desk"]), str(ids["unrelated"])],
            "limit": 100,
        },
    )
    names = {n["canonical_name"] for n in r.json()["data"]}
    assert names == {f"{_P}Alpha", f"{_P}Beta", f"{_P}Gamma"}


@pytest.mark.asyncio
async def test_finding_id_composes_with_q(seeded):
    client, ids = seeded
    r = await client.get(
        "/api/v1/registry/entities",
        params={"finding_id": str(ids["desk"]), "q": "Alpha", "limit": 100},
    )
    names = {n["canonical_name"] for n in r.json()["data"]}
    assert names == {f"{_P}Alpha"}


@pytest.mark.asyncio
async def test_mentions_are_counted_INSIDE_the_scope(seeded):
    """A corpus-wide count beside a scoped roster would rank the list by
    prominence somewhere else — the opposite of what the caller asked for."""
    client, ids = seeded
    r = await client.get(
        "/api/v1/registry/entities",
        params={"finding_id": str(ids["desk"]), "limit": 100},
    )
    by_name = {n["canonical_name"]: n for n in r.json()["data"]}
    assert by_name[f"{_P}Alpha"]["mentions"] == 1
    # `total` is still the CORPUS count, so a client can see how much of the
    # world it is looking at.
    assert r.json()["total"] > len(by_name)


@pytest.mark.asyncio
async def test_no_finding_id_leaves_the_response_unchanged_for_existing_callers(seeded):
    """The pre-existing read is untouched: an unscoped list still lists."""
    client, _ = seeded
    r = await client.get("/api/v1/registry/entities", params={"limit": 1000})
    assert r.status_code == 200
    body = r.json()
    assert set(body.keys()) == {"data", "total"}
    names = {n["canonical_name"] for n in body["data"]}
    # Entities with no signal link at all are still listed (the LEFT JOIN).
    assert f"{_P}Hub" in names
    assert f"{_P}Gamma" in names


@pytest.mark.asyncio
async def test_too_many_finding_ids_is_a_refusal_not_a_truncation(seeded):
    """A truncated scope that reports itself as the scope is worse than a 422."""
    client, ids = seeded
    r = await client.get(
        "/api/v1/registry/entities",
        params={"finding_id": [str(ids["desk"])] * (MAX_FINDING_IDS + 1)},
    )
    assert r.status_code == 422
    assert str(MAX_FINDING_IDS) in json.dumps(r.json())


@pytest.mark.asyncio
async def test_a_malformed_finding_id_is_a_refusal_not_a_silent_drop(seeded):
    """A mistyped desk head would otherwise yield a roster quietly missing a
    desk, with nothing to tell the caller a desk went missing."""
    client, ids = seeded
    r = await client.get(
        "/api/v1/registry/entities",
        params={"finding_id": [str(ids["desk"]), "not-a-uuid"]},
    )
    assert r.status_code == 422
