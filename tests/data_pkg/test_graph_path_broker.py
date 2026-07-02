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
    _MAX_NODES,
    _MAX_PATH_LEN,
    _cypher_quote,
    build_path_neighbourhood_cypher,
    build_shortest_path_cypher,
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
    def __init__(self, path_rows, nbr_rows):
        self._path_rows = path_rows
        self._nbr_rows = nbr_rows
        self.fetched: list[str] = []

    async def execute(self, sql, *args):
        return "OK"

    async def fetch(self, sql, *args):
        self.fetched.append(sql)
        if "path_nodes agtype" in sql:
            return self._path_rows
        if "src agtype" in sql:
            return self._nbr_rows
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
    pool = _FakePool(_FakeConn(path_rows=[], nbr_rows=[]))
    res = await shortest_path_with_broker(pool, "A", "B")
    assert res["found"] is False
    assert res["path"] == []
    assert res["broker"] is None


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
