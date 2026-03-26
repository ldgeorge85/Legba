"""Structural balance analysis on the knowledge graph.

JDL Level 2: Structural balance analysis.

Computes balance score from AlliedWith (+) and HostileTo (-) triads,
augmented by reified Nexus nodes.  Nexuses carry an `intent`
property that maps to edge sign:

  supportive → +1   hostile → -1   dual-use/neutral → excluded (0)

This means a hostile SuppliesWeaponsTo through a proxy correctly counts
as a negative edge in the balance computation.

Structural Balance Theory on signed networks: AlliedWith = positive edge,
HostileTo = negative edge. Balanced triads are stable (friend-friend-friend,
or enemy-enemy-friend). Unbalanced triads (friend-of-friend-is-enemy) predict
realignment and are analytically interesting.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import asyncpg

logger = logging.getLogger("legba.shared.structural_balance")

GRAPH_NAME = "legba_graph"


async def _prepare(conn: asyncpg.Connection) -> None:
    """Prepare a connection for AGE queries."""
    await conn.execute("LOAD 'age'")
    await conn.execute('SET search_path = ag_catalog, "$user", public')


def _parse_agtype(val: Any) -> Any:
    """Parse an agtype text value into a Python object."""
    if val is None:
        return None
    s = str(val).strip()
    for suffix in (
        "::path", "::vertex", "::edge",
        "::numeric", "::integer", "::float", "::boolean",
    ):
        s = s.replace(suffix, "")
    s = s.strip()
    try:
        return json.loads(s)
    except (json.JSONDecodeError, TypeError):
        return s


async def _cypher(
    conn: asyncpg.Connection,
    query: str,
    cols: str = "v agtype",
) -> list[dict[str, Any]]:
    """Execute a Cypher query via AGE and parse the results."""
    await _prepare(conn)
    sql = f"SELECT * FROM cypher('{GRAPH_NAME}', $$ {query} $$) AS ({cols})"
    rows = await conn.fetch(sql)
    col_names = [c.strip().split()[0] for c in cols.split(",")]
    return [
        {name: _parse_agtype(row[name]) for name in col_names}
        for row in rows
    ]


async def compute_structural_balance(pool: asyncpg.Pool) -> dict:
    """Compute structural balance metrics from the knowledge graph.

    Returns dict with:
        - balance_score: float 0-1 (1 = fully balanced)
        - balanced_triads: int
        - unbalanced_triads: list of (entity_a, entity_b, entity_c, description)
        - total_signed_edges: int
    """
    try:
        return await _compute_balance(pool)
    except Exception as exc:
        logger.warning("Structural balance computation failed: %s", exc)
        return {
            "balance_score": 1.0,
            "balanced_triads": 0,
            "unbalanced_triads": [],
            "total_signed_edges": 0,
        }


async def _compute_balance(pool: asyncpg.Pool) -> dict:
    """Internal implementation — may raise."""

    # 1. Query all AlliedWith and HostileTo edges from AGE graph
    async with pool.acquire() as conn:
        allied_rows = await _cypher(
            conn,
            "MATCH (a)-[r:AlliedWith]->(b) RETURN a.name AS src, b.name AS dst",
            cols="src agtype, dst agtype",
        )
        hostile_rows = await _cypher(
            conn,
            "MATCH (a)-[r:HostileTo]->(b) RETURN a.name AS src, b.name AS dst",
            cols="src agtype, dst agtype",
        )

    # 2. Query reified Nexuses from the nexuses table.
    #    Faster than AGE queries and captures relationship intent.
    async with pool.acquire() as conn2:
        operation_rows = await conn2.fetch("""
            SELECT actor_entity, target_entity, intent
            FROM nexuses
            WHERE (valid_until IS NULL OR valid_until > NOW())
              AND intent IN ('supportive', 'hostile')
        """)

    # 3. Build a signed adjacency dict (entity -> {entity: +1 or -1})
    #    Treat edges as undirected for triad analysis (A allied-with B
    #    means B is also allied-with A for balance theory purposes).
    signed_edges: dict[tuple[str, str], int] = {}

    for row in allied_rows:
        src, dst = str(row["src"]), str(row["dst"])
        if src and dst and src != dst:
            pair = tuple(sorted([src, dst]))
            # Positive edge — if conflict with an existing negative edge,
            # the graph itself is inconsistent; keep the latest (positive)
            signed_edges[pair] = +1

    for row in hostile_rows:
        src, dst = str(row["src"]), str(row["dst"])
        if src and dst and src != dst:
            pair = tuple(sorted([src, dst]))
            signed_edges[pair] = -1

    # Overlay signed edges from Nexuses (intent-based).
    # Nexuses override AGE edges when both exist — the reified
    # relationship carries richer analyst-assigned intent.
    for op in operation_rows:
        src, dst = op["actor_entity"], op["target_entity"]
        if src and dst and src != dst:
            pair = tuple(sorted([src, dst]))
            if op["intent"] == "supportive":
                signed_edges[pair] = +1
            elif op["intent"] == "hostile":
                signed_edges[pair] = -1

    total_signed = len(signed_edges)

    if total_signed < 3:
        return {
            "balance_score": 1.0,
            "balanced_triads": 0,
            "unbalanced_triads": [],
            "total_signed_edges": total_signed,
        }

    # 4. Build adjacency for efficient triad enumeration.
    #    Only consider entities that have BOTH positive and negative edges
    #    for efficiency — entities with only one sign can't form unbalanced triads.
    neighbors: dict[str, set[str]] = {}
    for (a, b) in signed_edges:
        neighbors.setdefault(a, set()).add(b)
        neighbors.setdefault(b, set()).add(a)

    # 5. Find all triads (3-node subgraphs where all 3 pairs have signed edges)
    #    Enumerate triads by iterating entities and checking neighbor intersections
    balanced_count = 0
    unbalanced_triads: list[dict] = []
    seen_triads: set[tuple[str, ...]] = set()

    # All entities with signed edges
    all_entities = sorted(neighbors.keys())

    for entity in all_entities:
        entity_neighbors = neighbors[entity]
        # Find triads: look for pairs of neighbors that are also connected
        neighbor_list = sorted(entity_neighbors)
        for i, n1 in enumerate(neighbor_list):
            for n2 in neighbor_list[i + 1:]:
                # Check if n1 and n2 are also connected by a signed edge
                pair_n1_n2 = tuple(sorted([n1, n2]))
                if pair_n1_n2 not in signed_edges:
                    continue

                # We have a triad: entity, n1, n2
                triad_key = tuple(sorted([entity, n1, n2]))
                if triad_key in seen_triads:
                    continue
                seen_triads.add(triad_key)

                # 6. Classify: product of all three edge signs
                pair_e_n1 = tuple(sorted([entity, n1]))
                pair_e_n2 = tuple(sorted([entity, n2]))
                s1 = signed_edges[pair_e_n1]
                s2 = signed_edges[pair_e_n2]
                s3 = signed_edges[pair_n1_n2]
                product = s1 * s2 * s3

                if product > 0:
                    # Balanced: all positive, or two negative + one positive
                    balanced_count += 1
                else:
                    # Unbalanced: one negative + two positive, or all negative
                    a, b, c = triad_key
                    desc = _describe_triad(a, b, c, signed_edges)
                    unbalanced_triads.append({
                        "entity_a": a,
                        "entity_b": b,
                        "entity_c": c,
                        "description": desc,
                    })

    total_triads = balanced_count + len(unbalanced_triads)
    balance_score = (
        balanced_count / total_triads if total_triads > 0 else 1.0
    )

    return {
        "balance_score": round(balance_score, 4),
        "balanced_triads": balanced_count,
        "unbalanced_triads": unbalanced_triads,
        "total_signed_edges": total_signed,
    }


def _describe_triad(
    a: str, b: str, c: str,
    signed_edges: dict[tuple[str, str], int],
) -> str:
    """Generate a human-readable description of an unbalanced triad."""

    def _edge_desc(x: str, y: str) -> str:
        pair = tuple(sorted([x, y]))
        sign = signed_edges.get(pair, 0)
        return "allied with" if sign > 0 else "hostile to"

    ab = _edge_desc(a, b)
    ac = _edge_desc(a, c)
    bc = _edge_desc(b, c)

    return (
        f"{a} is {ab} {b}, {a} is {ac} {c}, "
        f"but {b} is {bc} {c} — structurally unstable"
    )
