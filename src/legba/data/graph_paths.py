# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""P1-T9 — shortest relationship-path + broker between two actors over AGE.

A **leaf** module: it imports ONLY stdlib, ``networkx`` and uses a caller-
supplied asyncpg pool/connection (the AGE ``LOAD 'age'`` + ``SET search_path``
+ ``cypher('legba_graph', ...)`` idiom). It deliberately imports NOTHING from
the ``legba.data.analysts`` handler package — that package eagerly pulls
runtime-only deps (entity_resolution -> geocode -> ``pycountry``) that the slim
REGISTRY image does not ship. Keeping this logic in a dependency-light leaf lets
the registry's ``/graph/path`` endpoint import + serve it directly.

The path question a graph oracle must survive — "is there a path A<->B / who's
the broker on it?" — is the BACKEND verb. The AGE connection idiom + cypher
patterns mirror ``graph_mining._augment_from_age``; the betweenness scoring
reuses ``nx.betweenness_centrality`` exactly as ``graph_mining._centrality``
does. The new pieces are a bounded variable-length path cypher and a small
orchestrator that parses the path and names the highest-betweenness
intermediary (the broker).

Path direction is treated UNDIRECTED (``-[*..K]-``): "is A connected to B / who
sits between them" is a connectivity/brokerage question, not a directed-
reachability one. Length is capped at :data:`_MAX_PATH_LEN` and the broker
neighbourhood pull is capped at :data:`_MAX_NODES`, so a dense graph cannot
explode the query.

``graph_mining`` re-exports these names so its public API + internal callers
are unchanged; the canonical definitions live HERE.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

# networkx is imported LAZILY inside _broker_on_path (its only consumer): the slim
# registry image omits it, and the path-finding itself needs only stdlib + the AGE
# pool — so /graph/path serves in the registry (the broker just degrades to None).

logger = logging.getLogger(__name__)

# Cap on the broker-neighbourhood pull (shared with graph_mining's mining cap):
# betweenness is scored over a bounded subgraph rather than the whole component.
_MAX_NODES = 5_000
# Hard ceiling on variable-length path expansion (P1-T9). A dense graph makes
# ``(a)-[*..K]-(b)`` blow up combinatorially, so the relationship-distance
# between two actors is capped here; callers may request a SMALLER K but never
# a larger one (see :func:`build_shortest_path_cypher`).
_MAX_PATH_LEN = 6


# ---------------------------------------------------------------------------
# Cypher builders
# ---------------------------------------------------------------------------


def _cypher_quote(value: Any) -> str:
    """Return a safely double-quoted AGE-cypher string literal.

    The actor identifiers arrive from the API surface, so backslashes and
    embedded double-quotes are escaped to keep the inlined literal from
    breaking out of the cypher string (AGE has no client-side bind params for
    the cypher body — values are inlined, mirroring ``_augment_from_age``).
    """
    s = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{s}"'


def build_shortest_path_cypher(
    src_id: str,
    dst_id: str,
    *,
    max_len: int = _MAX_PATH_LEN,
) -> str:
    """Build the bounded shortest-relationship-path cypher for AGE.

    Emits a variable-length undirected match anchored on the two actor ids,
    returning the ordered node-id list, the per-hop edge labels and the path
    length of the SINGLE shortest path (``ORDER BY length(p) ASC LIMIT 1``).

    ``max_len`` is clamped to ``1 <= max_len <= _MAX_PATH_LEN`` so a caller can
    only ever request a TIGHTER bound, never an unbounded walk. The actor ids
    are escaped + inlined via :func:`_cypher_quote`.
    """
    max_len = max(1, min(int(max_len), _MAX_PATH_LEN))
    # Return the raw vertex / edge sequences (NOT a ``[n IN nodes(p) | n.id]``
    # comprehension — this AGE build rejects property access on the
    # comprehension variable: "could not find properties for n"). The vertex /
    # edge agtype objects carry ``properties.id`` + ``start_id``/``end_id`` so
    # the Python side reconstructs the ordered ids AND the real edge direction.
    return (
        f"MATCH p = (a)-[*1..{max_len}]-(b) "
        f"WHERE a.id = {_cypher_quote(src_id)} AND b.id = {_cypher_quote(dst_id)} "
        "RETURN nodes(p) AS path_nodes, "
        "relationships(p) AS path_rels, "
        "length(p) AS path_len "
        "ORDER BY path_len ASC LIMIT 1"
    )


def build_path_neighbourhood_cypher(
    node_ids: list[str],
    *,
    max_nodes: int = _MAX_NODES,
) -> str:
    """Build the edge-pull cypher for the path's neighbourhood (broker scoring).

    Mirrors ``_augment_from_age``: every directed edge incident to ANY path
    node, capped at ``max_nodes`` (clamped to ``_MAX_NODES``) so betweenness is
    computed over a bounded subgraph rather than the whole component.
    """
    max_nodes = max(1, min(int(max_nodes), _MAX_NODES))
    quoted = ", ".join(_cypher_quote(n) for n in node_ids)
    return (
        "MATCH (a)-[r]->(b) "
        f"WHERE a.id IN [{quoted}] OR b.id IN [{quoted}] "
        "RETURN a.id AS src, b.id AS dst, label(r) AS rel "
        f"LIMIT {max_nodes}"
    )


# ---------------------------------------------------------------------------
# agtype parsing helpers
# ---------------------------------------------------------------------------


_AGTYPE_ANNOT = re.compile(r"::(vertex|edge|path)\b")


def _parse_age_entities(value: Any) -> list[dict[str, Any]]:
    """Parse an AGE ``nodes(p)`` / ``relationships(p)`` agtype value to dicts.

    AGE returns a list of graph objects, each a JSON object annotated with a
    trailing ``::vertex`` / ``::edge`` type tag (e.g.
    ``[{"id": 1.., "label": "Entity", "properties": {"id": "A"}}::vertex]``).
    The annotation is stripped before JSON-decoding. A single object (no list)
    is wrapped; anything unparseable degrades to ``[]``.
    """
    if value is None:
        return []
    s = _AGTYPE_ANNOT.sub("", str(value).strip())
    try:
        parsed = json.loads(s)
    except (ValueError, TypeError):
        return []
    if isinstance(parsed, dict):
        return [parsed]
    if isinstance(parsed, list):
        return [x for x in parsed if isinstance(x, dict)]
    return []


def _vertex_business_id(vertex: dict[str, Any]) -> str:
    """Return a vertex's business id (``properties.id``), AGE-graphid fallback."""
    props = vertex.get("properties")
    if isinstance(props, dict) and props.get("id") is not None:
        return str(props["id"])
    return str(vertex.get("id", ""))


def _strip_agtype(value: Any) -> str:
    """Strip an AGE agtype annotation + surrounding quotes from a scalar value."""
    if value is None:
        return ""
    s = str(value).strip()
    for suffix in ("::vertex", "::edge", "::path", "::numeric"):
        if s.endswith(suffix):
            s = s[: -len(suffix)]
    s = s.strip()
    if s.startswith('"') and s.endswith('"'):
        s = s[1:-1]
    return s


# ---------------------------------------------------------------------------
# Orchestrator: shortest path + broker
# ---------------------------------------------------------------------------


async def shortest_path_with_broker(
    pool: Any,
    src_id: str,
    dst_id: str,
    *,
    max_len: int = _MAX_PATH_LEN,
    max_nodes: int = _MAX_NODES,
) -> dict[str, Any]:
    """Find the shortest relationship path A<->B over AGE + name its broker.

    Returns a dict::

        {
          "found": bool,
          "source": str, "target": str,
          "path":   [node_id, ...]          # ordered, [] when no path
          "edges":  [{"source","target","label"}, ...]
          "length": int | None,             # hop count
          "broker": {"node": id, "betweenness": float} | None,
          "max_len": int,                   # the clamped cap actually used
          "warnings": [str, ...],
        }

    The broker is the path INTERMEDIARY with the highest betweenness, computed
    over the bounded path neighbourhood (``build_path_neighbourhood_cypher``).
    A direct A->B edge (no intermediary) yields ``broker = None``. Best-effort:
    any AGE failure degrades to ``found=False`` + a warning, never raises.
    """
    clamped = max(1, min(int(max_len), _MAX_PATH_LEN))
    result: dict[str, Any] = {
        "found": False,
        "source": str(src_id),
        "target": str(dst_id),
        "path": [],
        "edges": [],
        "length": None,
        "broker": None,
        "max_len": clamped,
        "warnings": [],
    }
    if pool is None:
        result["warnings"].append("no_pool")
        return result

    path_cypher = build_shortest_path_cypher(src_id, dst_id, max_len=clamped)
    try:
        async with pool.acquire() as conn:
            await conn.execute("LOAD 'age'")
            await conn.execute('SET search_path = ag_catalog, "$user", public')
            rows = await conn.fetch(
                "SELECT * FROM cypher('legba_graph', $$"
                + path_cypher
                + "$$) AS (path_nodes agtype, path_rels agtype, path_len agtype)"
            )
    except Exception as exc:
        logger.warning("graph_mining.path.query_failed err=%s", exc)
        result["warnings"].append("path_query_failed")
        return result

    if not rows:
        return result  # no path within the length cap

    vertices = _parse_age_entities(rows[0]["path_nodes"])
    rels = _parse_age_entities(rows[0]["path_rels"])
    node_ids = [_vertex_business_id(v) for v in vertices]
    if len(node_ids) < 2:
        result["warnings"].append("degenerate_path")
        return result

    # Map AGE internal graphid -> business id so edges carry the REAL stored
    # direction (the path match is undirected, so an edge's start/end may run
    # opposite to traversal order).
    gid_to_bid = {
        v.get("id"): _vertex_business_id(v) for v in vertices if v.get("id") is not None
    }
    edges: list[dict[str, Any]] = []
    for i, r in enumerate(rels):
        src = gid_to_bid.get(r.get("start_id"))
        dst = gid_to_bid.get(r.get("end_id"))
        # Fall back to traversal order if start/end ids aren't resolvable.
        if src is None and i < len(node_ids):
            src = node_ids[i]
        if dst is None and i + 1 < len(node_ids):
            dst = node_ids[i + 1]
        edges.append({
            "source": str(src or ""),
            "target": str(dst or ""),
            "label": str(r.get("label") or ""),
        })
    result.update(found=True, path=node_ids, edges=edges, length=len(node_ids) - 1)

    # Broker = highest-betweenness intermediary node, scored over the bounded
    # path neighbourhood. No intermediary => no broker.
    intermediaries = node_ids[1:-1]
    if intermediaries:
        result["broker"] = await _broker_on_path(
            pool, node_ids, edges, intermediaries, max_nodes=max_nodes
        )
    return result


async def _broker_on_path(
    pool: Any,
    node_ids: list[str],
    edges: list[dict[str, Any]],
    intermediaries: list[str],
    *,
    max_nodes: int,
) -> dict[str, Any] | None:
    """Score the path intermediaries by betweenness; return the top broker.

    Pulls the path neighbourhood from AGE, builds a directed networkx graph
    (always seeded with the path edges so the broker is scored even if the
    neighbourhood pull comes back empty) and reuses ``nx.betweenness_centrality``
    — the same instrument ``_centrality`` uses. Degrades to ``None`` on any
    failure.
    """
    try:
        import networkx as nx
    except ImportError:  # slim registry image omits networkx → no broker (path still served)
        logger.debug("graph_paths.broker: networkx unavailable; path returned without broker")
        return None
    simple = nx.DiGraph()
    try:
        nbr_cypher = build_path_neighbourhood_cypher(node_ids, max_nodes=max_nodes)
        async with pool.acquire() as conn:
            await conn.execute("LOAD 'age'")
            await conn.execute('SET search_path = ag_catalog, "$user", public')
            nbr_rows = await conn.fetch(
                "SELECT * FROM cypher('legba_graph', $$"
                + nbr_cypher
                + "$$) AS (src agtype, dst agtype, rel agtype)"
            )
        for r in nbr_rows:
            s = _strip_agtype(r["src"])
            d = _strip_agtype(r["dst"])
            if s and d:
                simple.add_edge(s, d)
    except Exception as exc:
        logger.warning("graph_mining.path.broker_pull_failed err=%s", exc)
    # Always include the path itself so a broker is named even with no
    # neighbourhood context.
    for e in edges:
        simple.add_edge(e["source"], e["target"])

    if simple.number_of_nodes() == 0:
        return None
    k_sample = min(simple.number_of_nodes(), 64)
    try:
        bet = nx.betweenness_centrality(simple, k=k_sample, seed=42)
    except Exception as exc:  # pragma: no cover
        logger.debug("graph_mining.path.betweenness_failed err=%s", exc)
        bet = {}

    best_node: str | None = None
    best_score = -1.0
    for n in intermediaries:
        sc = float(bet.get(n, 0.0))
        if sc > best_score:
            best_node, best_score = n, sc
    if best_node is None:
        return None
    return {"node": best_node, "betweenness": round(best_score, 6)}


__all__ = [
    "shortest_path_with_broker",
    "build_shortest_path_cypher",
    "build_path_neighbourhood_cypher",
    "_broker_on_path",
    "_cypher_quote",
    "_parse_age_entities",
    "_vertex_business_id",
    "_strip_agtype",
    "_MAX_PATH_LEN",
    "_MAX_NODES",
]
