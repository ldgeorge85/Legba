# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``/entities/graph`` renders the reified graph, never the candidate queue.

The panel this route feeds is titled "the world graph". It once drew straight
from ``proposed_edges`` with no status predicate at all — on the live substrate
that was 25,004 rejected and 26,373 orphaned candidates presented as reified
knowledge (graph-debate JUDGE_SYNTHESIS P0) — and was then filtered to
``status='promoted'``.

W3-A CUT THE READER OVER to ``entity_edges`` (migration 0143), which makes that
guarantee STRUCTURAL rather than a predicate one edit could drop: the candidate
queue is a different table, and only promoted rows were ever projected into the
edge store (0145). These tests therefore keep seeding the rejected and pending
candidates and assert they never appear — the guarantee is unchanged, its
enforcement moved — and add the three properties the id-keyed store buys that
the name-keyed reader could not offer:

  * an edge whose endpoint the GC merged away renders on the KEEPER instead of
    silently vanishing when the tombstone-hiding node join dropped it;
  * one name borne by two profiles of different `entity_class` no longer draws
    one actor as several nodes (the uniqueness index is
    ``(lower(canonical_name), entity_class)``, so a name is not a key);
  * ``edge_family`` travels on every edge, so a co-mention and an asserted
    relation stop rendering as the same claim.
"""

from __future__ import annotations

import os

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
from legba.data.registry.entities_api import build_entities_router
from legba.data.registry.signing import SigningIdentity
from legba.data.registry.stack import StackRegistry
from legba.data.registry.vocabulary_cache import VocabularyCache

_TEST_MASTER_KEY_HEX = "0011223344556677889900112233445566778899001122334455667788990011"
os.environ.setdefault(MASTER_KEY_ENV, _TEST_MASTER_KEY_HEX)
os.environ.setdefault("LEGBA_REGISTRY_SIGNING_KEY", "66" * 32)


@pytest_asyncio.fixture
async def api_app(migrated_pg: PostgresConfig):
    os.environ.pop(API_TOKEN_ENV, None)

    pg_store = PostgresStore(migrated_pg)
    await pg_store.connect()

    identity = SigningIdentity(
        signing_key=SigningKey(b"entities-graph-promoted-test-0001"[:32]),
        signer_did="did:legba:registry:entities-graph-test",
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

    async with pg_store.acquire() as conn:
        ids = {}
        for name, cls in (("EgpTestland", "geo"),
                          ("EgpFactionX", "organization"),
                          ("EgpRejectistan", "geo"),
                          # a merged loser, to prove the edge lands on the keeper
                          ("EgpOldName", "organization"),
                          # SAME lowered name as EgpFactionX at another class:
                          # the collision the name-keyed node join could not see
                          ("EgpFactionX", "person")):
            ids.setdefault(f"{name}/{cls}", await conn.fetchval(
                """INSERT INTO entity_profiles
                     (canonical_name, entity_class, entity_type, data)
                   VALUES ($1, $2, $2, '{}'::jsonb) RETURNING id""", name, cls))
        await conn.execute(
            "UPDATE entity_profiles SET merged_into=$2 WHERE id=$1",
            ids["EgpOldName/organization"], ids["EgpTestland/geo"])

        # The reified graph. Only promoted candidates were ever projected here.
        await conn.execute(
            """
            INSERT INTO entity_edges
                (src_id, dst_id, edge_type, edge_family, polarity, confidence,
                 observed_count, evidence_set)
            VALUES ($1, $2, 'conflict with', 'relation', -1, 0.9, 3,
                    '{"evidence_text": "promoted edge"}'::jsonb)
            """,
            ids["EgpTestland/geo"], ids["EgpFactionX/organization"])

        # The candidate queue, still holding a rejected row with the HIGHEST
        # confidence and a pending one. Neither may reach the panel.
        await conn.execute(
            """
            INSERT INTO proposed_edges
                (source_entity, target_entity, relationship_type, confidence,
                 evidence_text, status)
            VALUES
                ('EgpTestland', 'EgpRejectistan', 'member of', 0.99,
                 'rejected edge', 'rejected'),
                ('EgpFactionX', 'EgpRejectistan', 'controls', 0.95,
                 'still pending', 'pending')
            """
        )

    yield app, pg_store

    async with pg_store.acquire() as conn:
        await conn.execute(
            "DELETE FROM proposed_edges WHERE source_entity LIKE 'Egp%'")
        await conn.execute(
            """DELETE FROM entity_edges WHERE src_id IN
                 (SELECT id FROM entity_profiles
                   WHERE canonical_name LIKE 'Egp%')""")
        await conn.execute(
            "UPDATE entity_profiles SET merged_into=NULL "
            "WHERE canonical_name LIKE 'Egp%'")
        await conn.execute(
            "DELETE FROM entity_profiles WHERE canonical_name LIKE 'Egp%'")
    await descriptor_registry.stop()
    await pg_store.close()


@pytest_asyncio.fixture
async def client(api_app):
    app, _ = api_app
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver",
    ) as c:
        yield c


@pytest.mark.asyncio
async def test_top_n_graph_never_shows_the_candidate_queue(client):
    # The rejected candidate has the HIGHEST confidence (0.99) — under the
    # original no-filter query it ranked first in the "world graph".
    r = await client.get("/api/v1/registry/entities/graph", params={"limit": 300})
    assert r.status_code == 200
    kinds = {(e["source"], e["target"]) for e in r.json()["edges"]}
    assert ("EgpTestland", "EgpFactionX") in kinds
    assert ("EgpTestland", "EgpRejectistan") not in kinds
    assert ("EgpFactionX", "EgpRejectistan") not in kinds


@pytest.mark.asyncio
async def test_ego_graph_never_shows_the_candidate_queue(client):
    r = await client.get(
        "/api/v1/registry/entities/graph", params={"center": "EgpTestland"})
    assert r.status_code == 200
    body = r.json()
    assert [(e["source"], e["target"]) for e in body["edges"]] == [
        ("EgpTestland", "EgpFactionX")
    ]
    names = {n["canonical_name"] for n in body["nodes"]}
    assert "EgpRejectistan" not in names


@pytest.mark.asyncio
async def test_edges_carry_their_family_and_identity(client):
    """A co-mention and an asserted relation must not render as one claim, and
    the endpoints must travel as ids so the UI can stop keying on names."""
    r = await client.get(
        "/api/v1/registry/entities/graph", params={"center": "EgpTestland"})
    edge = r.json()["edges"][0]
    assert edge["edge_family"] == "relation"
    assert edge["polarity"] == -1
    assert edge["observed_count"] == 3
    assert edge["src_id"] and edge["dst_id"]


@pytest.mark.asyncio
async def test_an_edge_on_a_merged_endpoint_renders_on_the_keeper(client):
    """Centering on a merged loser's surviving keeper still finds the edge. The
    name-keyed reader dropped such an edge entirely: its node join hides
    tombstones, so the endpoint came back empty."""
    r = await client.get(
        "/api/v1/registry/entities/graph", params={"center": "EgpOldName"})
    assert r.status_code == 200
    # The tombstone name itself carries no edges — they live on the keeper.
    assert r.json()["edges"] == []

    r = await client.get(
        "/api/v1/registry/entities/graph", params={"center": "EgpTestland"})
    assert len(r.json()["edges"]) == 1


@pytest.mark.asyncio
async def test_one_name_at_two_classes_is_not_drawn_as_two_nodes(client):
    """`EgpFactionX` exists at both `organization` and `person`. The edge names
    exactly one of them, and only that one may render — the name-keyed node
    join pulled BOTH and drew one actor twice."""
    r = await client.get(
        "/api/v1/registry/entities/graph", params={"center": "EgpTestland"})
    nodes = r.json()["nodes"]
    faction = [n for n in nodes if n["canonical_name"] == "EgpFactionX"]
    assert len(faction) == 1, "one name, two profiles, ONE node"
    assert faction[0]["entity_class"] == "organization"


@pytest.mark.asyncio
async def test_entity_detail_relationships_come_from_the_edge_store(client):
    r = await client.get("/api/v1/registry/entities/EgpTestland")
    assert r.status_code == 200
    rels = r.json()["relationships"]
    assert len(rels) == 1
    assert rels[0]["other"] == "EgpFactionX"
    assert rels[0]["direction"] == "out"
    assert rels[0]["relationship_type"] == "conflict with"
    assert rels[0]["edge_family"] == "relation"
    assert rels[0]["evidence_text"] == "promoted edge"
    # the rejected candidate must not surface here either
    assert all(x["other"] != "EgpRejectistan" for x in rels)
