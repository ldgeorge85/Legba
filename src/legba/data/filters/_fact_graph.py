# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Nexus → AGE edge writer for the ``fact_extractor`` stage (anchor §5).

Facts-first, nexuses-second: the ``facts`` row is the deliverable; the AGE
graph edge is an OPTIONAL second step gated behind the stage's
``emit_graph_edges`` flag (default off). This module is thin orchestration
over the existing ``PostgresStore.cypher()`` MERGE helper
(``data/stack/postgres/age.py:486``) — it does NOT reinvent graph writes.

A typed entity relation maps to an AGE edge only when:

  * both triple endpoints classify to one of the seeded 9 vertex labels
    (Entity/Location/Organization/Person/Event/Country/Concept/Corporation/
    Software — ``0001_baseline.sql``), and
  * the predicate maps to one of the 14 seeded edge labels.

The predicate→edge map below reuses NER's predicate cue sets
(``filters/ner.py``); unknown predicates fall back to ``CoOccursWith`` (the
safe generic relation). If neither endpoint classifies to a graph vertex,
the edge is skipped — the fact still persists (facts-first).

Errors are swallowed by the caller (best-effort): a graph hiccup must never
fail the facts write.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# NER entity_class (lower-case) → AGE vertex label (PascalCase, one of the 9
# seeded labels). Anything not here is NOT a graph vertex → skip the edge.
_CLASS_TO_VERTEX: dict[str, str] = {
    "entity": "Entity",
    "location": "Location",
    "organization": "Organization",
    "person": "Person",
    "event": "Event",
    "country": "Country",
    "concept": "Concept",
    "corporation": "Corporation",
    "software": "Software",
}

# Predicate (lower-cased, trimmed) → AGE edge label (one of the 14 seeded).
# Reuses the NER predicate cue intent; everything unmapped → CoOccursWith.
_PREDICATE_TO_EDGE: dict[str, str] = {
    "located in": "LocatedIn",
    "capital of": "LocatedIn",
    "headquarters location": "LocatedIn",
    "country": "LocatedIn",
    "country of citizenship": "LocatedIn",
    "country of origin": "LocatedIn",
    "member of": "MemberOf",
    "employer": "MemberOf",
    "part of": "PartOf",
    "subsidiary": "PartOf",
    "parent organization": "PartOf",
}

_GENERIC_EDGE = "CoOccursWith"

# AGE cypher identifiers must be a bare label (no quoting needed for the
# closed PascalCase set above) — we only ever emit values from the maps.
_SAFE = frozenset(_CLASS_TO_VERTEX.values())
_SAFE_EDGES = frozenset(_PREDICATE_TO_EDGE.values()) | {_GENERIC_EDGE}


def edge_label_for_predicate(predicate: str) -> str:
    """Map a GLiREL relation predicate to a seeded AGE edge label (fallback)."""
    return _PREDICATE_TO_EDGE.get((predicate or "").lower().strip(), _GENERIC_EDGE)


def vertex_label_for_class(entity_class: str | None) -> str | None:
    """Map a NER entity_class to a seeded AGE vertex label, or None."""
    if not entity_class:
        return None
    return _CLASS_TO_VERTEX.get(entity_class.lower().strip())


async def resolve_vertex_id(
    conn: Any, name: str, entity_class: str | None
) -> str | None:
    """Resolve a fact endpoint's NAME to a live ``entity_profiles.id``, or None.

    The graph's vertex key is the entity uuid, so a writer needs the id before
    it can emit an edge. Two properties matter:

    * ``merged_into IS NULL`` — a tombstone is never keyed. Repointing edges
      after a merge is the whole reason identity is a uuid, and keying a vertex
      on a loser would recreate the very debt this fixes (defects #6/#9/#10).
    * a matching ``entity_class`` is PREFERRED, not required. ``(lower(name),
      entity_class)`` is the resolution key, so the same name in two classes is
      two real rows; the ordering picks the class the fact claims, then the
      most recently updated, and never invents a row.

    Returns None when the entity has not been resolved yet — the caller must
    then skip the edge rather than fall back to a name key.
    """
    row = await conn.fetchrow(
        """
        SELECT id::text AS id
          FROM entity_profiles
         WHERE lower(canonical_name) = lower($1)
           AND merged_into IS NULL
         ORDER BY (lower(entity_class) = lower($2)) DESC, updated_at DESC
         LIMIT 1
        """,
        name,
        entity_class or "",
    )
    return row["id"] if row else None


def _cypher_str(value: str) -> str:
    """Escape a Python string for inline interpolation into a Cypher literal.

    AGE does not bind Cypher params through asyncpg, so the cypher() helper
    str.format()s trusted values in. We single-quote + escape to keep an
    entity name with an apostrophe (``O'Brien``) from breaking the query.
    """
    return "'" + str(value).replace("\\", "\\\\").replace("'", "\\'") + "'"


async def upsert_fact_edge(
    store: Any,
    *,
    subject: str,
    subject_id: str | None,
    subject_class: str | None,
    predicate: str,
    value: str,
    value_id: str | None,
    value_class: str | None,
    fact_id: str,
    graph: str = "legba_graph",
) -> bool:
    """Best-effort MERGE of one fact-derived, **id-keyed** AGE edge.

    Returns True when an edge was emitted, False when skipped. Never raises on
    a graph error — the fact has already persisted; the edge is the optional
    second step.

    **Vertex identity is the ``entity_profiles`` uuid, never the name.** Until
    2026-08-03 this writer MERGEd ``(a:Person {name: 'Iran'})`` while every
    reader — ``graph_paths``, ``graph_mining._augment_from_age``,
    ``structural_balance._augment_from_age`` — filters ``WHERE a.id = …``. The
    two contracts never met: a vertex written here could not be found by any
    query in the codebase, and all three readers swallowed the miss (graph
    debate defect #13; A_age_commit.md §3.1 "src_id/dst_id are uuid, ALWAYS").

    So an endpoint with no resolved uuid is now SKIPPED rather than written
    under a name key. A name-keyed vertex is not a partial answer, it is
    landfill: unreadable, unmergeable (an entity merge cannot repoint it), and
    indistinguishable from a real vertex once it is in the graph. Refusing the
    write is what keeps the surface correct for the day feeding begins.
    """
    subj_label = vertex_label_for_class(subject_class)
    val_label = vertex_label_for_class(value_class)
    if subj_label is None or val_label is None:
        return False
    if not subject_id or not value_id:
        # Loud, not silent: the caller asked for a graph edge and could not say
        # WHICH entities it connects. That is a resolution gap upstream, and it
        # is exactly the condition that produced 27 unreachable fixtures.
        logger.warning(
            "fact_graph.edge_skip_unresolved fact_id=%s subject_resolved=%s value_resolved=%s "
            "— refusing a name-keyed vertex no reader can find",
            fact_id, bool(subject_id), bool(value_id),
        )
        return False
    edge_label = edge_label_for_predicate(predicate)
    # Defensive: only ever interpolate labels from the closed safe sets.
    if subj_label not in _SAFE or val_label not in _SAFE or edge_label not in _SAFE_EDGES:
        return False

    # `name` rides along as a display property but is NEVER the merge key —
    # renames and merges move the name, they do not move the identity.
    query = (
        f"MERGE (a:{subj_label} {{id: {_cypher_str(subject_id)}}}) "
        f"SET a.name = {_cypher_str(subject)} "
        f"MERGE (b:{val_label} {{id: {_cypher_str(value_id)}}}) "
        f"SET b.name = {_cypher_str(value)} "
        f"MERGE (a)-[r:{edge_label}]->(b) "
        f"SET r.fact_id = {_cypher_str(fact_id)} "
        f"RETURN r"
    )
    try:
        # PostgresStore.cypher(query, *, cols, graph_name) — query is the FIRST
        # positional arg and the graph is keyword-only. (The AgeStore wrapper
        # takes graph-first; the runtime injects the bare PostgresStore here.)
        await store.cypher(query, cols="r agtype", graph_name=graph)
        return True
    except Exception as exc:                                  # pragma: no cover
        logger.debug("fact_graph.edge_skip fact_id=%s err=%s", fact_id, exc)
        return False


__all__ = [
    "edge_label_for_predicate",
    "vertex_label_for_class",
    "resolve_vertex_id",
    "upsert_fact_edge",
]
