# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The recursive-CTE graph walks — extracted from ``substrate_query_port``.

V3/P3 (DATA MODEL V3 §3, the temporal surface). The port module sits under a
pinned module-size ceiling with ~150 lines of headroom and the temporal
parameters alone cost more than that, so the three signed-graph walks —
``query_paths`` / ``find_proxy_chains`` / ``query_brokers`` — and ALL of their
private machinery (the SQL templates, the endpoint resolver, the family
filter) live here as plain functions taking the asyncpg pool. The port keeps
thin delegating methods so the ``SubstrateQueryPort`` surface is unchanged.

The walks themselves are documented on the port's methods — what moved here
is machinery plus one temporal rule worth restating: **an ``as_of`` walk
applies the as-of predicate PER HOP, on both the seed and the recursive arm**
(spec §3.3 — a path through an edge that was closed on date D is not a path
that existed on D). The open-row predicate and the as-of predicate come from
:mod:`legba.runtime.substrate_temporal` so every reader shares one
definition.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from . import substrate_temporal as _temporal

# Hard ceiling on traversal depth regardless of the caller's request — a
# 3-hop signed path is the deepest the structural-balance / proxy-chain
# tradecraft reads, and CTE fan-out is exponential in hops.
_GRAPH_MAX_HOPS = 3
# Row cap on the recursive frontier AND on the returned path set — bounds
# the working set a single traversal can materialize.
_GRAPH_MAX_PATHS = 100
# Broker discovery caps how many entities it names on each side of the cut
# and how many brokers it returns.
_BROKER_MAX_CAMP = 25
_BROKER_MAX_RESULTS = 50

#: The families a path walk traverses by DEFAULT — the ones that ASSERT
#: something. `cooccurrence` is excluded because two entities appearing in one
#: document is not a relationship, and 8,635 of 12,732 open nexus rows were
#: exactly that: walking them made the co-mention hairball look like tradecraft.
#: `structural` joins the default when it is ever populated; it is in the
#: closed vocabulary (0143) and holds zero rows today.
_ASSERTING_FAMILIES = ("relation", "reference", "structural")

#: The closed `edge_family` vocabulary (migration 0143's CHECK constraint).
_EDGE_FAMILIES = frozenset(
    {"relation", "reference", "cooccurrence", "structural"})


def _walk_families(families: list[str] | None) -> list[str]:
    """Normalize the family filter, dropping unknown values.

    An unknown family is DROPPED rather than raising, because this port is
    called by LLM tool-use where a hallucinated family must degrade to the
    default rather than fail a whole consult turn. An all-unknown list falls
    back to the asserting default for the same reason.
    """
    if not families:
        return list(_ASSERTING_FAMILIES)
    out = [f for f in (str(x).strip().lower() for x in families)
           if f in _EDGE_FAMILIES]
    return out or list(_ASSERTING_FAMILIES)


_RESOLVE_ENDPOINTS_SQL = """
SELECT lower(btrim(t.name)) AS key, public.resolve_entity_name(t.name) AS rid
  FROM unnest($1::text[]) AS t(name)
"""


async def _resolve_walk_endpoints(conn: Any, names: list[str]) -> dict[str, Any]:
    """Resolve walk endpoint NAMES to terminal entity ids, in one round trip.

    Goes through ``resolve_entity_name`` (0143) and nowhere else: it matches
    tombstones so a name the GC merged away lands on its keeper, and it returns
    NULL for an AMBIGUOUS name rather than picking a profile — the caller warns
    instead of walking from an entity nobody named.
    """
    rows = await conn.fetch(_RESOLVE_ENDPOINTS_SQL, list(names))
    return {r["key"]: r["rid"] for r in rows}


def _endpoint_warnings(slots: dict[str, str], resolved: dict[str, Any]) -> list[str]:
    """One warning per endpoint that reached no entity. Never a silent empty."""
    return [
        f"{slot}_unresolved: {name!r} matches no entity, or matches several "
        f"(ambiguous) — the walk cannot start from it"
        for slot, name in slots.items()
        if resolved.get(name.lower()) is None
    ]


async def _hydrate_node_names(conn: Any, node_ids: Any) -> dict[Any, str]:
    """id -> canonical_name for the walk's nodes, in one round trip."""
    ids = [n for n in node_ids if n is not None]
    if not ids:
        return {}
    rows = await conn.fetch(
        "SELECT id, canonical_name FROM entity_profiles WHERE id = ANY($1::uuid[])",
        ids)
    return {r["id"]: r["canonical_name"] for r in rows}


#: The signed path walk, id-keyed. `$1` src, `$2` dst, `$3` hops, `$4` families.
#:
#: The walk continues FROM the first edge's DST. Seeding `head` with the source
#: made hop 2+ re-expand from the origin and fabricate chains (64,136 "paths"
#: where 1,517 existed on live data); the recursive arm always advances to
#: `e.dst_id`, and so must the seed.
#:
#: V3/P3: ``{seed_pred}`` / ``{rec_pred}`` are filled by
#: :func:`substrate_temporal.temporal_predicate` — the open-row pair when
#: ``as_of`` is absent, the per-hop as-of predicate when it is given.
_PATH_WALK_SQL = """
WITH RECURSIVE walk AS (
    SELECT
        e.dst_id                          AS head,
        ARRAY[e.src_id, e.dst_id]         AS visited,
        ARRAY[e.id]                       AS edge_ids,
        ARRAY[e.src_id, e.dst_id]         AS node_path,
        ARRAY[e.polarity]::smallint[]     AS polarities,
        e.polarity::int                   AS pol_product,
        e.confidence                      AS min_conf,
        1                                 AS hops
    FROM entity_edges e
    WHERE {seed_pred}
      AND e.src_id = $1
      AND e.edge_family = ANY($4::text[])
    UNION ALL
    SELECT
        e.dst_id,
        w.visited || e.dst_id,
        w.edge_ids || e.id,
        w.node_path || e.dst_id,
        w.polarities || e.polarity,
        w.pol_product * e.polarity,
        least(w.min_conf, e.confidence),
        w.hops + 1
    FROM walk w
    JOIN entity_edges e ON e.src_id = w.head
    WHERE {rec_pred}
      AND e.edge_family = ANY($4::text[])
      AND w.hops < $3
      AND w.head <> $2
      AND NOT (e.dst_id = ANY(w.visited))
)
SELECT edge_ids, node_path, polarities, pol_product, min_conf, hops
FROM walk
WHERE head = $2
{pol_clause}
ORDER BY hops ASC, min_conf DESC
LIMIT {limit_param}
"""

#: The broker walk. `$1` camp-A ids, `$2` camp-B ids, `$3` hops, `$4` families,
#: `$5` row cap (and `$6` the as-of instant, when one is given).
_BROKER_WALK_SQL = """
WITH RECURSIVE walk AS (
    SELECT
        e.dst_id                     AS head,
        ARRAY[e.src_id, e.dst_id]    AS visited,
        ARRAY[e.id]                  AS edge_ids,
        ARRAY[e.src_id, e.dst_id]    AS node_path,
        1                            AS hops
    FROM entity_edges e
    WHERE {seed_pred}
      AND e.src_id = ANY($1::uuid[])
      AND e.edge_family = ANY($4::text[])
    UNION ALL
    SELECT
        e.dst_id,
        w.visited || e.dst_id,
        w.edge_ids || e.id,
        w.node_path || e.dst_id,
        w.hops + 1
    FROM walk w
    JOIN entity_edges e ON e.src_id = w.head
    WHERE {rec_pred}
      AND e.edge_family = ANY($4::text[])
      AND w.hops < $3
      AND NOT (w.head = ANY($2::uuid[]))
      AND NOT (e.dst_id = ANY(w.visited))
)
SELECT edge_ids, node_path
FROM walk
WHERE head = ANY($2::uuid[])
LIMIT $5
"""


async def _count_unbounded_starts(conn: Any, edge_ids: list[Any]) -> int:
    """How many of the walked edges carry no recorded ``valid_from``.

    The as-of contract's ``unbounded_start``: a NULL start over-includes by
    construction, so the reader is told how much of the answer rests on a
    start date nobody recorded. One extra round trip, only on as-of walks.
    """
    if not edge_ids:
        return 0
    return int(await conn.fetchval(
        "SELECT count(*) FROM entity_edges "
        " WHERE id = ANY($1::uuid[]) AND valid_from IS NULL",
        edge_ids) or 0)


async def query_paths(
    pool: Any,
    *,
    subject: str,
    obj: str,
    max_hops: int = 3,
    polarity_product: int | None = None,
    limit: int = 30,
    families: list[str] | None = None,
    as_of: datetime | None = None,
) -> dict[str, Any]:
    """Ranked signed PATHS from ``subject`` to ``obj`` (see the port docstring).

    ``as_of`` is the already-parsed instant (the port boundary refuses a
    malformed string before it gets here): when given, every hop's edge must
    satisfy the canonical as-of predicate on BOTH the seed and the recursive
    arm, and the envelope carries ``unbounded_start``.
    """
    s = (subject or "").strip()
    o = (obj or "").strip()
    if not s or not o:
        return {
            "subject": subject,
            "object": obj,
            "paths": [],
            "refs": [],
            "error": "both subject and object must be non-empty",
        }
    hops = max(1, min(int(max_hops), _GRAPH_MAX_HOPS))
    clamped_limit = max(1, min(int(limit), _GRAPH_MAX_PATHS))
    pol_filter = (
        int(polarity_product)
        if polarity_product is not None and int(polarity_product) in (-1, 0, 1)
        else None
    )
    fams = _walk_families(families)

    async with pool.acquire() as conn:
        ends = await _resolve_walk_endpoints(conn, [s, o])
        warnings = _endpoint_warnings({"subject": s, "object": o}, ends)
        if warnings:
            # FAIL LOUD. An unresolvable endpoint on an ID-keyed walk is
            # indistinguishable from "not connected" unless it is SAID —
            # this is the confidently-empty-answer class `graph_paths`
            # already guards with GRAPH_MISS_WARNINGS.
            return {
                "subject": subject, "object": obj, "max_hops": hops,
                "polarity_product_filter": pol_filter,
                "edge_families": fams,
                "paths": [], "refs": [], "warnings": warnings,
            }
        src_id, dst_id = ends[s.lower()], ends[o.lower()]

        params: list[Any] = [src_id, dst_id, hops, fams]
        as_of_param: int | None = None
        if as_of is not None:
            params.append(as_of)
            as_of_param = len(params)
        pred = _temporal.temporal_predicate("e", as_of_param)

        pol_clause = ""
        if pol_filter is not None:
            params.append(pol_filter)
            pol_clause = f"AND COALESCE(pol_product, 0) = ${len(params)}"
        params.append(clamped_limit)
        sql = _PATH_WALK_SQL.format(
            seed_pred=pred, rec_pred=pred,
            pol_clause=pol_clause, limit_param=f"${len(params)}")
        # The recursive frontier is bounded by the visited-set guard + the
        # hop cap; the LIMIT caps the materialized terminal set (already
        # polarity-filtered, so the cutoff cannot hide a matching path).
        records = await conn.fetch(sql, *params)
        names = await _hydrate_node_names(
            conn, {n for r in records for n in (r["node_path"] or [])})
        unbounded = (
            await _count_unbounded_starts(
                conn, [e for r in records for e in (r["edge_ids"] or [])])
            if as_of is not None else None
        )

    paths: list[dict[str, Any]] = []
    refs: list[str] = []
    for r in records:
        net = int(r["pol_product"]) if r["pol_product"] is not None else 0
        edge_ids = [str(e) for e in (r["edge_ids"] or [])]
        for e in edge_ids:
            if e not in refs:
                refs.append(e)
        node_ids = list(r["node_path"] or [])
        paths.append({
            # `nodes` stays the DISPLAY-NAME list every consumer already
            # reads; `node_ids` is the identity the walk actually used.
            "nodes": [names.get(n, str(n)) for n in node_ids],
            "node_ids": [str(n) for n in node_ids],
            "edge_ids": edge_ids,
            "polarities": [int(p) for p in (r["polarities"] or [])],
            "polarity_product": net,
            "min_confidence": float(r["min_conf"])
                if r["min_conf"] is not None else None,
            "hops": int(r["hops"]),
        })

    out: dict[str, Any] = {
        "subject": subject,
        "object": obj,
        "max_hops": hops,
        "polarity_product_filter": pol_filter,
        "edge_families": fams,
        "paths": paths,
        "refs": refs,
        "warnings": [],
    }
    if as_of is not None:
        out["as_of"] = as_of.isoformat()
        out["unbounded_start"] = unbounded
    return out


async def find_proxy_chains(
    pool: Any,
    *,
    subject: str,
    obj: str,
    max_hops: int = 3,
    polarity_product: int | None = None,
    limit: int = 30,
    families: list[str] | None = None,
    as_of: datetime | None = None,
) -> dict[str, Any]:
    """Proxy / cut-out chains from ``subject`` to ``obj`` — INDIRECT only.

    A specialization of :func:`query_paths` that drops the trivial direct
    ``subject → object`` edge and surfaces only the INDIRECT links —
    multi-hop chains (hops >= 2) AND single edges that carry a non-null
    ``intermediary`` cut-out (the reified ``A → via → B`` proxy edge). Same
    cyclic-graph VISITED-SET guard, hop cap, ``polarity_product`` filter,
    and per-hop ``as_of`` semantics as :func:`query_paths`.
    """
    # Reuse the bounded walk, then keep only the indirect chains.
    base = await query_paths(
        pool,
        subject=subject,
        obj=obj,
        max_hops=max_hops,
        polarity_product=polarity_product,
        limit=_GRAPH_MAX_PATHS,
        families=families,
        as_of=as_of,
    )
    if base.get("error"):
        return {
            "subject": subject,
            "object": obj,
            "chains": [],
            "refs": [],
            "error": base["error"],
        }
    if base.get("warnings"):
        # Propagate the miss rather than reporting "no proxy chains".
        return {
            "subject": subject, "object": obj,
            "max_hops": base["max_hops"],
            "polarity_product_filter": base["polarity_product_filter"],
            "edge_families": base["edge_families"],
            "chains": [], "refs": [], "warnings": base["warnings"],
        }
    clamped_limit = max(1, min(int(limit), _GRAPH_MAX_PATHS))

    # A single-edge path is "proxy" only if that edge reifies an
    # intermediary cut-out; pull the intermediary for the 1-hop edges so
    # we can keep the A→via→B reified proxies and drop bare A→B edges.
    # The cut-out is an entity ID now, so it is NAMED by a join instead of
    # returned as whatever string the producer happened to write.
    single_edge_ids: list[str] = [
        p["edge_ids"][0]
        for p in base["paths"]
        if p["hops"] == 1 and p["edge_ids"]
    ]
    intermediary_by_edge: dict[str, str | None] = {}
    if single_edge_ids:
        async with pool.acquire() as conn:
            irows = await conn.fetch(
                """
                SELECT e.id, p.canonical_name AS intermediary
                  FROM entity_edges e
                  LEFT JOIN entity_profiles p ON p.id = e.intermediary_id
                 WHERE e.id = ANY($1::uuid[])
                """,
                single_edge_ids,
            )
        intermediary_by_edge = {
            str(r["id"]): r["intermediary"] for r in irows
        }

    chains: list[dict[str, Any]] = []
    refs: list[str] = []
    for p in base["paths"]:
        if p["hops"] >= 2:
            indirect = True
            intermediary = None
        else:
            intermediary = intermediary_by_edge.get(p["edge_ids"][0]) \
                if p["edge_ids"] else None
            indirect = bool(intermediary)
        if not indirect:
            continue
        chain = dict(p)
        if intermediary is not None:
            chain["intermediary"] = intermediary
        for e in p["edge_ids"]:
            if e not in refs:
                refs.append(e)
        chains.append(chain)
        if len(chains) >= clamped_limit:
            break

    out: dict[str, Any] = {
        "subject": subject,
        "object": obj,
        "max_hops": base["max_hops"],
        "polarity_product_filter": base["polarity_product_filter"],
        "edge_families": base["edge_families"],
        "chains": chains,
        "refs": refs,
        "warnings": [],
    }
    if as_of is not None:
        out["as_of"] = base["as_of"]
        out["unbounded_start"] = base["unbounded_start"]
    return out


async def query_brokers(
    pool: Any,
    *,
    camp_a: list[str],
    camp_b: list[str],
    max_hops: int = 3,
    limit: int = 50,
    families: list[str] | None = None,
    as_of: datetime | None = None,
) -> dict[str, Any]:
    """Entities that SIT ON paths between two entity sets (see the port).

    ``as_of`` applies the canonical predicate per hop on both arms of the
    broker walk, exactly as on :func:`query_paths`.
    """
    a_names = [str(x).strip() for x in (camp_a or []) if str(x).strip()]
    b_names = [str(x).strip() for x in (camp_b or []) if str(x).strip()]
    a_names = a_names[:_BROKER_MAX_CAMP]
    b_names = b_names[:_BROKER_MAX_CAMP]
    if not a_names or not b_names:
        return {
            "camp_a": camp_a,
            "camp_b": camp_b,
            "brokers": [],
            "refs": [],
            "error": "both camp_a and camp_b must be non-empty",
        }
    hops = max(1, min(int(max_hops), _GRAPH_MAX_HOPS))
    clamped_limit = max(1, min(int(limit), _BROKER_MAX_RESULTS))
    fams = _walk_families(families)

    # Walk from every camp_a seed; keep terminal paths that land on a
    # camp_b member. The interior nodes (everything between the first and
    # last) are the brokers. VISITED-SET guard + hop cap as above.
    async with pool.acquire() as conn:
        resolved = await _resolve_walk_endpoints(conn, a_names + b_names)
        a_ids = [resolved[n.lower()] for n in a_names
                 if resolved.get(n.lower()) is not None]
        b_ids = [resolved[n.lower()] for n in b_names
                 if resolved.get(n.lower()) is not None]
        # A camp member that resolves to nothing is NAMED, not dropped
        # silently: a broker set computed over half a camp is a different
        # answer from one computed over all of it, and the caller has to be
        # able to tell those apart.
        warnings = [
            f"camp member {n!r} matches no entity, or matches several "
            f"(ambiguous) — excluded from the walk"
            for n in a_names + b_names if resolved.get(n.lower()) is None
        ]
        if not a_ids or not b_ids:
            return {
                "camp_a": a_names, "camp_b": b_names, "max_hops": hops,
                "edge_families": fams, "brokers": [], "refs": [],
                "warnings": warnings or [
                    "neither camp resolved to any entity"],
            }
        params: list[Any] = [a_ids, b_ids, hops, fams, _GRAPH_MAX_PATHS]
        as_of_param: int | None = None
        if as_of is not None:
            params.append(as_of)
            as_of_param = len(params)
        pred = _temporal.temporal_predicate("e", as_of_param)
        records = await conn.fetch(
            _BROKER_WALK_SQL.format(seed_pred=pred, rec_pred=pred), *params)
        names = await _hydrate_node_names(
            conn, {n for r in records for n in (r["node_path"] or [])})
        unbounded = (
            await _count_unbounded_starts(
                conn, [e for r in records for e in (r["edge_ids"] or [])])
            if as_of is not None else None
        )

    # Tally interior nodes (exclude the camp endpoints themselves). The
    # tally key is the entity ID now, so two surfaces of one actor can no
    # longer be counted as two different brokers — which is exactly the
    # error a brokerage ranking must not make.
    broker_paths: dict[Any, int] = {}
    broker_refs: dict[Any, set[str]] = {}
    camp_set = set(a_ids) | set(b_ids)
    for r in records:
        node_path = list(r["node_path"] or [])
        edge_ids = [str(e) for e in (r["edge_ids"] or [])]
        for node in node_path[1:-1]:  # drop both camp endpoints
            if node in camp_set:
                continue
            broker_paths[node] = broker_paths.get(node, 0) + 1
            broker_refs.setdefault(node, set()).update(edge_ids)

    ranked = sorted(
        broker_paths.items(), key=lambda kv: kv[1], reverse=True
    )[:clamped_limit]
    refs: list[str] = []
    brokers: list[dict[str, Any]] = []
    for node_id, count in ranked:
        edge_ids = sorted(broker_refs.get(node_id, set()))
        for e in edge_ids:
            if e not in refs:
                refs.append(e)
        brokers.append({
            "entity": names.get(node_id, str(node_id)),
            "entity_id": str(node_id),
            "path_count": count,
            "edge_ids": edge_ids,
        })

    out: dict[str, Any] = {
        "camp_a": a_names,
        "camp_b": b_names,
        "max_hops": hops,
        "edge_families": fams,
        "brokers": brokers,
        "refs": refs,
        "warnings": warnings,
    }
    if as_of is not None:
        out["as_of"] = as_of.isoformat()
        out["unbounded_start"] = unbounded
    return out
