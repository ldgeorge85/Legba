"""
Integration tests for GraphStore (Apache AGE).

Requires a running Apache AGE container:
  docker compose up postgres -d
"""

import asyncio
import os
import pytest

from legba.agent.memory.graph import GraphStore
from legba.shared.schemas.memory import Entity

_PG_HOST = os.getenv("POSTGRES_HOST", "localhost")
DSN = f"postgresql://legba:legba@{_PG_HOST}:5432/legba"


@pytest.fixture
async def graph():
    """Fresh GraphStore per test (function-scoped, correct event loop)."""
    store = GraphStore(dsn=DSN)
    await store.connect()
    assert store._available, "GraphStore failed to connect — is apache/age running?"
    yield store
    await store.close()


# ------------------------------------------------------------------
# Connection + setup
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_connect_creates_graph(graph: GraphStore):
    """The graph and AGE extension should be set up after connect()."""
    assert graph._available
    async with graph._pool.acquire() as conn:
        exists = await conn.fetchval(
            "SELECT count(*) FROM ag_catalog.ag_graph WHERE name = $1",
            graph.GRAPH_NAME,
        )
        assert exists > 0


# ------------------------------------------------------------------
# Entity operations
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_upsert_and_find_entity(graph: GraphStore):
    entity = Entity(name="Russia", entity_type="country", properties={"capital": "Moscow"})
    ok = await graph.upsert_entity(entity)
    assert ok, "upsert_entity returned False"

    found = await graph.find_entity("Russia")
    assert found is not None
    assert found.name == "Russia"
    assert found.entity_type == "Country"  # CamelCase label
    assert found.properties.get("capital") == "Moscow"


@pytest.mark.asyncio
async def test_find_entity_case_insensitive(graph: GraphStore):
    entity = Entity(name="Ukraine", entity_type="country", properties={"capital": "Kyiv"})
    await graph.upsert_entity(entity)

    found = await graph.find_entity("ukraine")
    assert found is not None
    assert found.name == "Ukraine"

    found2 = await graph.find_entity("UKRAINE")
    assert found2 is not None


@pytest.mark.asyncio
async def test_upsert_merges_properties(graph: GraphStore):
    e1 = Entity(name="TestMerge", entity_type="concept", properties={"a": 1})
    await graph.upsert_entity(e1)

    e2 = Entity(name="TestMerge", entity_type="concept", properties={"b": 2})
    await graph.upsert_entity(e2)

    found = await graph.find_entity("TestMerge")
    assert found is not None
    assert found.properties.get("b") == 2


@pytest.mark.asyncio
async def test_search_entities_by_name(graph: GraphStore):
    await graph.upsert_entity(Entity(name="NATO", entity_type="organization"))
    await graph.upsert_entity(Entity(name="OTAN", entity_type="organization"))

    results = await graph.search_entities(query="nat")
    names = [e.name for e in results]
    assert "NATO" in names


@pytest.mark.asyncio
async def test_search_entities_by_type(graph: GraphStore):
    await graph.upsert_entity(Entity(name="SearchPerson1", entity_type="person"))
    await graph.upsert_entity(Entity(name="SearchOrg1", entity_type="organization"))

    results = await graph.search_entities(entity_type="person")
    types = [e.entity_type for e in results]
    assert len(types) >= 1
    assert all(t == "Person" for t in types)


# ------------------------------------------------------------------
# Relationship operations
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_add_and_get_relationship(graph: GraphStore):
    await graph.upsert_entity(Entity(name="CountryA", entity_type="country"))
    await graph.upsert_entity(Entity(name="CountryB", entity_type="country"))

    ok = await graph.add_relationship(
        source_name="CountryA",
        target_name="CountryB",
        relation_type="allied_with",
        properties={"since": 1949},
    )
    assert ok

    rels = await graph.get_relationships("CountryA", direction="outgoing")
    assert len(rels) >= 1
    rel = [r for r in rels if r["entity_name"] == "CountryB"][0]
    assert rel["relation_type"] == "AlliedWith"
    assert rel["direction"] == "outgoing"


@pytest.mark.asyncio
async def test_get_relationships_incoming(graph: GraphStore):
    await graph.upsert_entity(Entity(name="Boss", entity_type="person"))
    await graph.upsert_entity(Entity(name="Worker", entity_type="person"))

    await graph.add_relationship("Worker", "Boss", "reports_to")

    rels = await graph.get_relationships("Boss", direction="incoming")
    assert any(r["entity_name"] == "Worker" for r in rels)


@pytest.mark.asyncio
async def test_get_relationships_both(graph: GraphStore):
    await graph.upsert_entity(Entity(name="NodeX", entity_type="concept"))
    await graph.upsert_entity(Entity(name="NodeY", entity_type="concept"))
    await graph.upsert_entity(Entity(name="NodeZ", entity_type="concept"))

    await graph.add_relationship("NodeX", "NodeY", "connects_to")
    await graph.add_relationship("NodeZ", "NodeX", "feeds_into")

    rels = await graph.get_relationships("NodeX", direction="both")
    directions = {r["direction"] for r in rels}
    assert "outgoing" in directions
    assert "incoming" in directions


# ------------------------------------------------------------------
# Path finding
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_find_path(graph: GraphStore):
    await graph.upsert_entity(Entity(name="PathA", entity_type="concept"))
    await graph.upsert_entity(Entity(name="PathB", entity_type="concept"))
    await graph.upsert_entity(Entity(name="PathC", entity_type="concept"))

    await graph.add_relationship("PathA", "PathB", "links_to")
    await graph.add_relationship("PathB", "PathC", "links_to")

    path = await graph.find_path("PathA", "PathC", max_depth=4)
    assert path is not None
    assert len(path) >= 1


@pytest.mark.asyncio
async def test_find_path_no_connection(graph: GraphStore):
    await graph.upsert_entity(Entity(name="Isolated1", entity_type="concept"))
    await graph.upsert_entity(Entity(name="Isolated2", entity_type="concept"))

    path = await graph.find_path("Isolated1", "Isolated2", max_depth=4)
    assert path is None


# ------------------------------------------------------------------
# Subgraph
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_query_subgraph(graph: GraphStore):
    await graph.upsert_entity(Entity(name="Center", entity_type="concept"))
    await graph.upsert_entity(Entity(name="Neighbor1", entity_type="concept"))
    await graph.upsert_entity(Entity(name="Neighbor2", entity_type="concept"))

    await graph.add_relationship("Center", "Neighbor1", "has")
    await graph.add_relationship("Center", "Neighbor2", "has")

    sg = await graph.query_subgraph("Center", depth=1, limit=50)
    assert len(sg["entities"]) >= 3
    assert len(sg["relationships"]) >= 2


# ------------------------------------------------------------------
# Raw Cypher
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_execute_cypher_query(graph: GraphStore):
    await graph.upsert_entity(Entity(name="CypherTest", entity_type="concept", properties={"val": 42}))

    results = await graph.execute_cypher(
        "MATCH (n {name: 'CypherTest'}) RETURN n.name AS name, n.val AS val"
    )
    assert len(results) >= 1
    assert results[0].get("name") == "CypherTest"


@pytest.mark.asyncio
async def test_execute_cypher_create(graph: GraphStore):
    results = await graph.execute_cypher(
        "CREATE (n:CypherCreated {name: 'FromCypher', origin: 'test'}) RETURN n"
    )
    assert len(results) >= 1

    found = await graph.find_entity("FromCypher")
    assert found is not None


@pytest.mark.asyncio
async def test_execute_cypher_pattern_match(graph: GraphStore):
    """Test a multi-hop pattern match — this is what AGE is for."""
    await graph.upsert_entity(Entity(name="PatternA", entity_type="concept"))
    await graph.upsert_entity(Entity(name="PatternB", entity_type="concept"))
    await graph.upsert_entity(Entity(name="PatternC", entity_type="concept"))

    await graph.add_relationship("PatternA", "PatternB", "knows")
    await graph.add_relationship("PatternB", "PatternC", "knows")

    results = await graph.execute_cypher(
        "MATCH (a {name: 'PatternA'})-[:Knows]->(b)-[:Knows]->(c {name: 'PatternC'}) "
        "RETURN b.name AS middleman"
    )
    assert len(results) >= 1
    assert results[0].get("middleman") == "PatternB"


@pytest.mark.asyncio
async def test_execute_cypher_error(graph: GraphStore):
    """Bad Cypher should return error dict, not raise."""
    results = await graph.execute_cypher("THIS IS NOT VALID CYPHER")
    assert len(results) == 1
    assert "error" in results[0]


# ------------------------------------------------------------------
# Helpers (no DB needed)
# ------------------------------------------------------------------

def test_sanitize_label():
    assert GraphStore._sanitize_label("country") == "Country"
    assert GraphStore._sanitize_label("military_base") == "MilitaryBase"
    assert GraphStore._sanitize_label("OPEC") == "OPEC"
    assert GraphStore._sanitize_label("person") == "Person"
    assert GraphStore._sanitize_label("123bad") == "Entity123bad"


def test_escape():
    assert GraphStore._escape("it's") == "it\\'s"
    assert GraphStore._escape("a\\b") == "a\\\\b"


def test_dict_to_cypher_map():
    m = GraphStore._dict_to_cypher_map({"name": "test", "count": 42, "active": True})
    assert "name: 'test'" in m
    assert "count: 42" in m
    assert "active: true" in m


def test_infer_return_cols():
    assert GraphStore._infer_return_cols("MATCH (n) RETURN n") == "c0 agtype"
    assert GraphStore._infer_return_cols("RETURN a, b, c") == "c0 agtype, c1 agtype, c2 agtype"
    assert GraphStore._infer_return_cols("RETURN n.name AS name") == "name agtype"
    assert GraphStore._infer_return_cols("RETURN a.x AS foo, b.y AS bar LIMIT 10") == "foo agtype, bar agtype"
    assert GraphStore._infer_return_cols("CREATE (n) SET n.x = 1") == "v agtype"
