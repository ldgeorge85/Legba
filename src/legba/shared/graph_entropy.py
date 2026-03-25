"""Graph entropy computation for the knowledge graph.

Computes information-theoretic entropy of the current graph state using the
relationship type distribution as the probability distribution.

Higher entropy = more diverse/surprising relationship landscape.
Entropy spikes = relationship reorganization underway.
"""

from __future__ import annotations

import json
import logging
import math
from typing import Any

import asyncpg

logger = logging.getLogger("legba.shared.graph_entropy")

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


async def compute_graph_entropy(pool: asyncpg.Pool) -> float:
    """Compute information-theoretic entropy of the current graph state.

    Uses relationship type distribution as the probability distribution.
    Higher entropy = more diverse/surprising relationship landscape.
    Entropy spikes = relationship reorganization underway.

    Returns:
        Shannon entropy value in bits. Returns 0.0 on error or empty graph.
    """
    try:
        return await _compute_entropy(pool)
    except Exception as exc:
        logger.warning("Graph entropy computation failed: %s", exc)
        return 0.0


async def _compute_entropy(pool: asyncpg.Pool) -> float:
    """Internal implementation — may raise."""

    # Query relationship type counts from AGE graph.
    # AGE stores edge labels in the ag_catalog.ag_label table,
    # but we can also query via Cypher MATCH to get actual edge counts.
    # Using the ag_label catalog approach for efficiency.
    async with pool.acquire() as conn:
        await _prepare(conn)

        # Get all edge label names from the graph's label catalog
        label_rows = await conn.fetch("""
            SELECT name FROM ag_catalog.ag_label
            WHERE graph = (
                SELECT graphid FROM ag_catalog.ag_graph
                WHERE name = $1
            )
            AND kind = 'e'
        """, GRAPH_NAME)

        if not label_rows:
            return 0.0

        # Count edges per label by querying each label's backing table
        type_counts: dict[str, int] = {}
        graph_oid_row = await conn.fetchrow("""
            SELECT graphid FROM ag_catalog.ag_graph WHERE name = $1
        """, GRAPH_NAME)

        if not graph_oid_row:
            return 0.0

        for lrow in label_rows:
            label_name = lrow["name"]
            if label_name.startswith("_"):
                # Skip internal AGE labels (like _ag_label_edge)
                continue
            try:
                # Each edge label has a backing table in the graph's schema
                count = await conn.fetchval(
                    f'SELECT COUNT(*) FROM "{GRAPH_NAME}"."{label_name}"'
                )
                if count and count > 0:
                    type_counts[label_name] = count
            except Exception:
                # Label table might not exist or be empty
                continue

    if not type_counts:
        return 0.0

    # Compute probability distribution (count / total for each type)
    total = sum(type_counts.values())
    if total == 0:
        return 0.0

    # Compute Shannon entropy: H = -sum(p(x) * log2(p(x)))
    entropy = 0.0
    for count in type_counts.values():
        p = count / total
        if p > 0:
            entropy -= p * math.log2(p)

    return round(entropy, 4)
