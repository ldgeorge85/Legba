# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""P1-T9 — shortest relationship-path + broker between two actors over AGE.

Two layers:

  * **Pure-logic** over the cypher builders (``build_shortest_path_cypher`` /
    ``build_path_neighbourhood_cypher``) — asserts the parametrised cypher,
    the length cap, the node budget cap and quote-escaping. No DB.

  * **Fake-conn** over the ``shortest_path_with_broker`` orchestrator — a
    connection that records SQL + replays canned ``agtype`` rows, so the path
    parse + broker (highest-betweenness intermediary) selection are asserted
    deterministically without a live graph.

  * **@integration** — where pg+AGE is reachable: build a tiny actor graph
    (A->M->B plus a longer A->X->Y->B detour) and assert the orchestrator
    returns the REAL shortest path + names the broker, and 'no path' for a
    disconnected actor.
"""

from __future__ import annotations

import pytest

from legba.data.graph_paths import (
    GRAPH_MISS_WARNINGS,
    GRAPH_UNAVAILABLE_WARNINGS,
    _MAX_NODES,
    _MAX_PATH_LEN,
    _cypher_quote,
    build_path_neighbourhood_cypher,
    build_population_probe_cypher,
    build_shortest_path_cypher,
    graph_is_populated,
    shortest_path_with_broker,
)


# ---------------------------------------------------------------------------
# Pure-logic: the path cypher builder
# ---------------------------------------------------------------------------


def test_path_cypher_is_parametrised_and_bounded():
    cy = build_shortest_path_cypher("Iran", "Hezbollah")
    # Variable-length undirected match, capped at the default ceiling.
    assert f"(a)-[*1..{_MAX_PATH_LEN}]-(b)" in cy
    # Anchored on BOTH actor ids (escaped + double-quoted).
    assert 'a.id = "Iran"' in cy
    assert 'b.id = "Hezbollah"' in cy
    # Returns the raw vertex + edge sequences + length, single shortest path.
    assert "nodes(p) AS path_nodes" in cy
    assert "relationships(p) AS path_rels" in cy
    assert "length(p) AS path_len" in cy
    assert "ORDER BY path_len ASC LIMIT 1" in cy


def test_path_cypher_length_cap_clamps_both_ways():
    # A tighter bound is honoured...
    assert "(a)-[*1..2]-(b)" in build_shortest_path_cypher("A", "B", max_len=2)
    # ...a larger one is clamped DOWN to the server ceiling (anti-explosion).
    assert f"(a)-[*1..{_MAX_PATH_LEN}]-(b)" in build_shortest_path_cypher(
        "A", "B", max_len=10_000
    )
    # ...and a non-positive bound clamps UP to 1 (never an unbounded walk).
    assert "(a)-[*1..1]-(b)" in build_shortest_path_cypher("A", "B", max_len=0)


def test_cypher_quote_escapes_injection():
    # A quote / backslash in an actor id cannot break out of the literal.
    assert _cypher_quote('a"b') == '"a\\"b"'
    assert _cypher_quote("a\\b") == '"a\\\\b"'
    cy = build_shortest_path_cypher('X"; MATCH (z) DETACH DELETE z //', "B")
    # The raw injection terminator must be escaped, not left bare.
    assert '"X\\"; MATCH (z) DETACH DELETE z //"' in cy


# ---------------------------------------------------------------------------
# Pure-logic: the neighbourhood (broker-scoring) cypher builder
# ---------------------------------------------------------------------------


def test_neighbourhood_cypher_mirrors_age_pull_and_caps_nodes():
    cy = build_path_neighbourhood_cypher(["A", "B", "C"])
    assert "MATCH (a)-[r]->(b)" in cy
    assert 'a.id IN ["A", "B", "C"]' in cy
    assert "RETURN a.id AS src, b.id AS dst, label(r) AS rel" in cy
    assert f"LIMIT {_MAX_NODES}" in cy


def test_neighbourhood_cypher_node_budget_clamps():
    assert "LIMIT 50" in build_path_neighbourhood_cypher(["A"], max_nodes=50)
    # A caller cannot request more than the node ceiling.
    assert f"LIMIT {_MAX_NODES}" in build_path_neighbourhood_cypher(
        ["A"], max_nodes=10 * _MAX_NODES
    )


# ---------------------------------------------------------------------------
# Fake-conn: the orchestrator (path parse + broker selection)
# ---------------------------------------------------------------------------


class _Acq:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *exc):
        return False


class _FakeConn:
    def __init__(self, path_rows, nbr_rows, *, populated: bool = True, probe_raises=False):
        self._path_rows = path_rows
        self._nbr_rows = nbr_rows
        self._populated = populated
        self._probe_raises = probe_raises
        self.fetched: list[str] = []

    async def execute(self, sql, *args):
        return "OK"

    async def fetch(self, sql, *args):
        self.fetched.append(sql)
        if "path_nodes agtype" in sql:
            return self._path_rows
        if "src agtype" in sql:
            return self._nbr_rows
        # The population probe — the only other cypher this module issues.
        if "n.id IS NOT NULL" in sql:
            if self._probe_raises:
                raise RuntimeError("engine down")
            return [{"id": '"some-uuid"'}] if self._populated else []
        return []


def _vrow(gid: int, bid: str) -> str:
    return f'{{"id": {gid}, "label": "Entity", "properties": {{"id": "{bid}"}}}}::vertex'


def _erow(gid: int, start: int, end: int, label: str) -> str:
    return (
        f'{{"id": {gid}, "label": "{label}", "start_id": {start}, '
        f'"end_id": {end}, "properties": {{}}}}::edge'
    )


class _FakePool:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        return _Acq(self._conn)


async def test_orchestrator_parses_path_and_picks_betweenness_broker():
    # Shortest path A -> M -> N -> B (two intermediaries M, N). The
    # neighbourhood makes M a hub (P,Q -> M -> R,S) so its betweenness > N.
    nodes = "[" + ", ".join(
        _vrow(g, b) for g, b in ((1, "A"), (2, "M"), (3, "N"), (4, "B"))
    ) + "]"
    rels = "[" + ", ".join(
        _erow(g, s, e, "AlliedWith") for g, s, e in ((10, 1, 2), (11, 2, 3), (12, 3, 4))
    ) + "]"
    path_rows = [{"path_nodes": nodes, "path_rels": rels, "path_len": "3"}]
    nbr_rows = [
        {"src": '"P"', "dst": '"M"', "rel": '"AlliedWith"'},
        {"src": '"Q"', "dst": '"M"', "rel": '"AlliedWith"'},
        {"src": '"M"', "dst": '"R"', "rel": '"AlliedWith"'},
        {"src": '"M"', "dst": '"S"', "rel": '"AlliedWith"'},
    ]
    pool = _FakePool(_FakeConn(path_rows, nbr_rows))
    res = await shortest_path_with_broker(pool, "A", "B")

    assert res["found"] is True
    assert res["path"] == ["A", "M", "N", "B"]
    assert res["length"] == 3
    assert res["edges"][0] == {"source": "A", "target": "M", "label": "AlliedWith"}
    assert res["edges"][-1] == {"source": "N", "target": "B", "label": "AlliedWith"}
    # M is the hub intermediary => the broker.
    assert res["broker"] is not None
    assert res["broker"]["node"] == "M"
    assert res["broker"]["betweenness"] >= 0.0


async def test_orchestrator_no_path():
    pool = _FakePool(_FakeConn(path_rows=[], nbr_rows=[], populated=True))
    res = await shortest_path_with_broker(pool, "A", "B")
    assert res["found"] is False
    assert res["path"] == []
    assert res["broker"] is None
    # A POPULATED graph with no route between these two actors is the only case
    # that is genuinely an answer — and it says so rather than staying silent.
    assert res["warnings"] == ["no_path"]


# ---------------------------------------------------------------------------
# Fail-loud: an empty graph is NOT "no path" (JUDGE_SYNTHESIS §4.3 item 2/3)
# ---------------------------------------------------------------------------


def test_population_probe_requires_an_id_key():
    cy = build_population_probe_cypher()
    # Vertices without an `id` do not count as population: every reader in the
    # codebase filters on `.id`, so a name-keyed graph is unreadable to them.
    assert "n.id IS NOT NULL" in cy
    assert "LIMIT 1" in cy


async def test_unpopulated_graph_reports_graph_unpopulated_not_no_path():
    """The defect this closes: 27 smoke fixtures answering as if they were the world.

    Before 2026-08-03 an empty graph and a genuine miss BOTH returned
    ``found=False`` with an empty ``warnings`` list, which the API rendered as
    ``detail="no path"`` — a confident negative about a graph that had never
    held a production row.
    """
    pool = _FakePool(_FakeConn(path_rows=[], nbr_rows=[], populated=False))
    res = await shortest_path_with_broker(pool, "A", "B")
    assert res["found"] is False
    assert res["warnings"] == ["graph_unpopulated"]
    assert "no_path" not in res["warnings"]


async def test_engine_failure_reports_engine_unreachable():
    pool = _FakePool(_FakeConn(path_rows=[], nbr_rows=[], probe_raises=True))
    res = await shortest_path_with_broker(pool, "A", "B")
    assert res["found"] is False
    assert "engine_unreachable" in res["warnings"]
    assert "graph_unpopulated" not in res["warnings"]


async def test_graph_is_populated_returns_none_on_error_not_false():
    """None (unknown) and False (empty) are different facts and must not merge."""
    assert await graph_is_populated(None) is None
    assert await graph_is_populated(
        _FakePool(_FakeConn([], [], probe_raises=True))
    ) is None
    assert await graph_is_populated(_FakePool(_FakeConn([], [], populated=False))) is False
    assert await graph_is_populated(_FakePool(_FakeConn([], [], populated=True))) is True


async def test_every_miss_carries_exactly_one_reason():
    """No path lookup may ever return found=False with an empty warnings list."""
    cases = [
        _FakePool(_FakeConn([], [], populated=True)),
        _FakePool(_FakeConn([], [], populated=False)),
        _FakePool(_FakeConn([], [], probe_raises=True)),
        None,
    ]
    for pool in cases:
        res = await shortest_path_with_broker(pool, "A", "B")
        assert res["found"] is False
        reasons = [w for w in res["warnings"] if w in GRAPH_MISS_WARNINGS]
        assert len(reasons) == 1, res["warnings"]
    # And the three "we could not ask" reasons are a strict subset of them.
    assert set(GRAPH_UNAVAILABLE_WARNINGS) < set(GRAPH_MISS_WARNINGS)


async def test_orchestrator_direct_edge_has_no_broker():
    # A direct A -> B edge has no intermediary, hence no broker.
    nodes = "[" + ", ".join(_vrow(g, b) for g, b in ((1, "A"), (2, "B"))) + "]"
    rels = "[" + _erow(10, 1, 2, "HostileTo") + "]"
    path_rows = [{"path_nodes": nodes, "path_rels": rels, "path_len": "1"}]
    pool = _FakePool(_FakeConn(path_rows, nbr_rows=[]))
    res = await shortest_path_with_broker(pool, "A", "B")
    assert res["found"] is True
    assert res["length"] == 1
    assert res["broker"] is None


async def test_orchestrator_no_pool_soft_fails():
    res = await shortest_path_with_broker(None, "A", "B")
    assert res["found"] is False
    assert "no_pool" in res["warnings"]


# ---------------------------------------------------------------------------
# The HTTP surface: /graph/path must FAIL, not answer, on an unfed graph
# ---------------------------------------------------------------------------


def _path_app(pool):
    """A minimal app carrying only the graph-structure router + a stub pool."""
    from types import SimpleNamespace

    from fastapi import FastAPI

    from legba.data.registry.api import require_bearer
    from legba.data.registry.graph_structure_api import build_graph_structure_router

    deps = SimpleNamespace(descriptor_registry=SimpleNamespace(pg=pool))
    app = FastAPI()
    app.include_router(build_graph_structure_router(deps))
    app.dependency_overrides[require_bearer] = lambda: "test-principal"
    return app


async def _get_path(app, **params):
    from httpx import ASGITransport, AsyncClient

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://t"
    ) as client:
        return await client.get("/graph/path", params=params)


async def test_route_503s_when_the_graph_is_unpopulated():
    """`/graph/path` over an unfed graph must be an ERROR, never a 200 "no path".

    This is the live-defect fix: the route used to render a confident
    ``detail="no path"`` from a graph holding 27 June-2026 smoke fixtures, so
    every answer it gave was a statement about a test island.
    """
    app = _path_app(_FakePool(_FakeConn([], [], populated=False)))
    resp = await _get_path(app, source="Iran", target="Hezbollah")
    assert resp.status_code == 503, resp.text
    body = resp.json()["detail"]
    assert body["error"] == "graph_unpopulated"
    assert body["source"] == "Iran" and body["target"] == "Hezbollah"
    # It is an ERROR envelope, not a GraphPath answer — no `found`, no verdict.
    assert "found" not in resp.json()
    assert body.get("message")


async def test_route_503s_when_the_engine_is_unreachable():
    app = _path_app(_FakePool(_FakeConn([], [], probe_raises=True)))
    resp = await _get_path(app, source="A", target="B")
    assert resp.status_code == 503
    assert resp.json()["detail"]["error"] == "engine_unreachable"


async def test_route_503s_when_no_pool_is_bound():
    app = _path_app(None)
    resp = await _get_path(app, source="A", target="B")
    assert resp.status_code == 503
    assert resp.json()["detail"]["error"] == "no_pool"


async def test_route_200s_with_a_reason_on_a_genuine_miss():
    """A POPULATED graph with no route is a real answer — 200, with the reason."""
    app = _path_app(_FakePool(_FakeConn([], [], populated=True)))
    resp = await _get_path(app, source="A", target="B")
    assert resp.status_code == 200
    body = resp.json()
    assert body["found"] is False
    assert body["warnings"] == ["no_path"]
    assert body["detail"] == "no path within the hop cap"


async def test_route_200s_and_carries_the_path_on_a_hit():
    nodes = "[" + ", ".join(
        _vrow(g, b) for g, b in ((1, "A"), (2, "M"), (3, "B"))
    ) + "]"
    rels = "[" + ", ".join(
        _erow(g, s, e, "AlliedWith") for g, s, e in ((10, 1, 2), (11, 2, 3))
    ) + "]"
    app = _path_app(_FakePool(
        _FakeConn([{"path_nodes": nodes, "path_rels": rels, "path_len": "2"}], [])
    ))
    resp = await _get_path(app, source="A", target="B")
    assert resp.status_code == 200
    body = resp.json()
    assert body["found"] is True
    assert body["path"] == ["A", "M", "B"]
    assert body["warnings"] == []


# ---------------------------------------------------------------------------
# @integration: real AGE graph
# ---------------------------------------------------------------------------

_P = "p1t9"  # id prefix to isolate from any pre-existing graph data.


async def _run_cypher(conn, body: str, cols: str = "v agtype"):
    await conn.execute("LOAD 'age'")
    await conn.execute('SET search_path = ag_catalog, "$user", public')
    return await conn.fetch(
        f"SELECT * FROM cypher('legba_graph', $$ {body} $$) AS ({cols})"
    )


@pytest.mark.integration
async def test_integration_real_shortest_path_and_broker(migrated_pg):
    asyncpg = pytest.importorskip("asyncpg")
    pool = await asyncpg.create_pool(migrated_pg.dsn, min_size=1, max_size=4)
    try:
        async with pool.acquire() as conn:
            # Vertices: A, M, B (short path) + X, Y (longer detour) + Z (island).
            for vid in ("A", "M", "B", "X", "Y", "Z"):
                await _run_cypher(
                    conn, f"CREATE (:Entity {{id: '{_P}_{vid}'}})"
                )

            def _edge(a: str, b: str, rel: str = "AlliedWith") -> str:
                return (
                    f"MATCH (a:Entity {{id:'{_P}_{a}'}}), "
                    f"(b:Entity {{id:'{_P}_{b}'}}) "
                    f"CREATE (a)-[:{rel}]->(b)"
                )

            # Short path A->M->B and a longer detour A->X->Y->B.
            for body in (
                _edge("A", "M"),
                _edge("M", "B"),
                _edge("A", "X"),
                _edge("X", "Y"),
                _edge("Y", "B"),
            ):
                await _run_cypher(conn, body)

        res = await shortest_path_with_broker(pool, f"{_P}_A", f"{_P}_B")
        assert res["found"] is True, res
        # Shortest is the 2-hop A-M-B, NOT the 3-hop detour.
        assert res["length"] == 2
        assert res["path"] == [f"{_P}_A", f"{_P}_M", f"{_P}_B"]
        assert res["broker"] is not None
        assert res["broker"]["node"] == f"{_P}_M"

        # Z is an island => no path.
        no = await shortest_path_with_broker(pool, f"{_P}_A", f"{_P}_Z")
        assert no["found"] is False
        assert no["broker"] is None
    finally:
        # Best-effort cleanup of just our prefixed nodes.
        try:
            async with pool.acquire() as conn:
                await _run_cypher(
                    conn,
                    f"MATCH (n) WHERE n.id STARTS WITH '{_P}_' DETACH DELETE n",
                )
        except Exception:
            pass
        await pool.close()
