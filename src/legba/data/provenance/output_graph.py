# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""AGE :DerivedFrom mirror for analyst-output provenance.

Graph-and-data Wave-1b, item 2 (REVIEW_CONSOLIDATED_2026-06-16 §3.6 / D3).

``write_analyst_output`` accepts an ``age_hook: AgeEdgeHook`` (``Callable[[UUID,
list[UUID]], Awaitable[None]]``) that was scaffolded but never called — the AGE
graph held NO output-lineage edges. This module supplies a concrete hook that
MERGEs ``(:Output {id})-[:DerivedFrom]->(:Output {id})`` into ``legba_graph``,
one DerivedFrom edge per ``derived_from`` parent UUID.

Decision D3 — the MERGE rides the SAME asyncpg connection the output row was
written on (so it is atomic with the INSERT, not a separate post-commit
round-trip) and is OPT-IN: the runtime only passes the hook when
``LEGBA_AGE_DERIVED_FROM`` is enabled, so per-write MERGE latency is under
operator control and the analyst-write critical path is untaxed by default.

Best-effort: the relational ``derived_from uuid[]`` array remains the lineage
source of truth (recursive-CTE walk). A graph hiccup here must NEVER fail the
output write — :func:`make_conn_age_output_hook` swallows + logs cypher errors.

The Cypher runs via the raw ``SELECT * FROM cypher(...)`` form on the supplied
connection after ``LOAD 'age'`` + the AGE ``search_path`` (the same prep
``PostgresStore.cypher`` does), so the hook is self-contained and does not need
a separate store handle.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Sequence
from uuid import UUID

logger = logging.getLogger(__name__)

#: Type of the hook write_analyst_output expects (provenance.writes.AgeEdgeHook).
AgeEdgeHook = Callable[[UUID, "list[UUID]"], Awaitable[None]]

_GRAPH = "legba_graph"


def _cypher_str(value: str) -> str:
    """Single-quote + escape a value for inline Cypher (AGE binds no params).

    Mirrors ``filters._fact_graph._cypher_str`` — UUIDs never contain quotes or
    backslashes, but the escape keeps the helper safe for any string id.
    """
    return "'" + str(value).replace("\\", "\\\\").replace("'", "\\'") + "'"


async def _prepare_age(conn: Any) -> None:
    """Reapply the session state an AGE cypher call needs on ``conn``."""
    await conn.execute("LOAD 'age'")
    await conn.execute('SET search_path = ag_catalog, "$user", public')


async def upsert_derived_from_edges(
    conn: Any,
    *,
    output_id: UUID,
    derived_from: Sequence[UUID],
    graph: str = _GRAPH,
) -> int:
    """MERGE ``(:Output {id})-[:DerivedFrom]->(:Output {id})`` for each parent.

    Runs on ``conn`` (so the edges are atomic with the output INSERT). Returns
    the number of edges MERGEd. Self-loops (a parent equal to the output id) are
    skipped. Never raises — a cypher error is logged + swallowed (best-effort).
    """
    parents = [p for p in derived_from if p and p != output_id]
    if not parents:
        # Still materialize the Output vertex so the node exists for lineage
        # queries even when it has no parents (a root output).
        parents = []
    written = 0
    try:
        await _prepare_age(conn)
        oid = _cypher_str(str(output_id))
        # Always MERGE the output vertex (root or not).
        await conn.execute(
            f"SELECT * FROM cypher('{graph}', $$ "
            f"MERGE (o:Output {{id: {oid}}}) RETURN o "
            f"$$) AS (o agtype)"
        )
        for parent in parents:
            pid = _cypher_str(str(parent))
            await conn.execute(
                f"SELECT * FROM cypher('{graph}', $$ "
                f"MERGE (o:Output {{id: {oid}}}) "
                f"MERGE (p:Output {{id: {pid}}}) "
                f"MERGE (o)-[r:DerivedFrom]->(p) "
                f"RETURN r "
                f"$$) AS (r agtype)"
            )
            written += 1
    except Exception as exc:  # pragma: no cover - best-effort, never fail write
        logger.debug(
            "output_graph.derived_from_skip output_id=%s err=%s", output_id, exc
        )
    return written


def make_conn_age_output_hook(conn: Any, *, graph: str = _GRAPH) -> AgeEdgeHook:
    """Build an :data:`AgeEdgeHook` bound to ``conn``.

    The returned hook matches ``write_analyst_output``'s ``age_hook`` signature
    ``(new_id, derived_from) -> Awaitable[None]`` and MERGEs the DerivedFrom
    edges on ``conn``. Pass it as ``age_hook=`` at the call site only when the
    operator has enabled the mirror.
    """

    async def _hook(output_id: UUID, derived_from: list[UUID]) -> None:
        await upsert_derived_from_edges(
            conn, output_id=output_id, derived_from=derived_from, graph=graph
        )

    return _hook


__all__ = [
    "AgeEdgeHook",
    "make_conn_age_output_hook",
    "upsert_derived_from_edges",
]
