# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Graph-walk read API — anchored ego expansion over ``entity_edges`` (K-G4).

*"Walking the world graph, asking multi-hop questions interactively IS
basically the entire vision."* This module is the read surface under that
sentence. It serves the **viewer verb**: point at one actor, see what is
around it, click a neighbour to keep walking, click an edge to see why it
exists.

Endpoints (bearer-gated), mounted at ``/api/v1``:

  * ``GET /graph/ego``          — anchored 1-hop neighbourhood of one entity,
    with family / type / polarity / confidence / time filters, honest facets
    over the UNFILTERED neighbourhood, and optional induced-edge stitching
    against the nodes a caller already has on screen.
  * ``GET /graph/edge/{id}``    — the evidence behind one edge: its verbatim
    snippet, its resolved source signals, its observed count and provenance.

**There is deliberately no ``depth`` parameter.** ``docs/AGE_PROBE_REPORT.md``
§5.2 measured the relational 3-hop ego of a hub at 472 ms on a million-edge
graph — "still an answer, no longer interactive" — because at average degree
~37 the 3-hop neighbourhood of a hub *is* most of the graph. So the viewer
never issues a k-hop query. Multi-hop is the operator's own traversal: every
hop is a fresh anchored 1-hop ego from the node they clicked, each one
index-driven and interactive, and the depth reached is bounded by attention
rather than by a query cap. Measured on the live substrate against the
highest-degree actor in the graph (United States, open degree 693), the ego
query below plans as two index scans over ``idx_entity_edges_out`` /
``idx_entity_edges_in`` and runs in **4 ms** with node metadata joined (warm,
re-measured 2026-08-03 over that actor's 696 open edges: 3.3 ms before the
per-family budget below, 4.2 ms after — the extra window costs about a
millisecond and the walk stays interactive); the unfiltered degree count runs
in **1.7 ms**. Both filter predicates (family and
confidence) are index-resident, not post-filters, because those two partial
indexes are keyed ``(src_id|dst_id, edge_family, confidence DESC)`` and
partial on exactly the open predicate below.

**The open predicate is carried on every read.** ``valid_until IS NULL AND
superseded_by IS NULL`` is what every production reader carries: an edge that
has been closed or superseded is an edge that no longer exists, and a walk
through one is a walk that is no longer true. Today nothing in the live
substrate is closed, so the predicate is not selective — it is carried anyway,
because the day it becomes selective is the day silently omitting it starts
producing confident wrong answers. (This is also the predicate §3.6 of the
probe found AGE cannot express at all over a variable-length match.)

**The edge budget is spent per FAMILY, never globally.** ``limit`` is a budget,
and a budget handed out on confidence alone is handed entirely to whichever
family happens to score highest. Measured on the live substrate for the
highest-degree actor (United States): 585 ``cooccurrence`` edges at mean
confidence 0.874 and *minimum* 0.500, against 89 ``relation`` at mean 0.564 and
22 ``reference`` whose *maximum* is 0.370. Ordered on confidence alone, a
budget of 80 went to 80 co-mentions and **zero** asserted edges — so switching
the co-mention family on did not add density to the walk, it evicted every
claim from it. That inverts what a filter control means. ``_ego_sql`` therefore
ranks each family independently and spends the budget round-robin across those
ranks, so a family can only ever lose slots to another family's *better-ranked*
edges, never to its sheer volume. See ``_ego_sql``.

**Fail loud.** An anchor that does not resolve is a 404, never a 200 with an
empty neighbourhood — "this actor has no relationships" and "this actor does
not exist" are different answers and the caller must be able to tell them
apart. An unknown ``family`` is a 400 rather than a silently-empty result.
An edge whose evidence was never captured says so explicitly
(``evidence_available: false`` plus the reason) rather than rendering an empty
box that reads as "no evidence found".

Built via ``build_graph_walk_router(deps)``; wired in ``server.py`` beside the
graph-structure router.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from .api import RegistryAPIDeps, require_bearer

# ---- Contract constants ----

#: The four tiers the reified edge store models. ``structural`` currently holds
#: no rows in the live substrate; it is still accepted as a filter value so the
#: viewer's family control does not have to change the day the first one lands.
EDGE_FAMILIES: tuple[str, ...] = ("relation", "reference", "cooccurrence", "structural")

#: Polarity is a signed smallint. Only ``relation`` carries a real signed
#: distribution (measured: 205 negative / 324 neutral / 352 positive);
#: ``cooccurrence`` is ~100% neutral because a co-mention is a statistic, not a
#: claim, and ``reference`` is ~100% positive because membership is an
#: assertion of belonging. The viewer leans on exactly this to keep the three
#: families visually distinct instead of rendering one hairball.
EDGE_POLARITIES: tuple[int, ...] = (-1, 0, 1)

DEFAULT_LIMIT = 60
MAX_LIMIT = 400

#: Cap on the ``known`` set a caller may ask us to stitch. The induced-subgraph
#: query is the most expensive shape here — measured at 37 ms over 300 real
#: nodes, against 4 ms for the ego itself — so it is bounded explicitly rather
#: than left to grow with the caller's canvas.
MAX_KNOWN = 250

#: Cap on signals resolved for one edge's evidence panel.
MAX_EVIDENCE_SIGNALS = 25

# The open-edge predicate, spelled once. See the module docstring.
_OPEN = "e.valid_until IS NULL AND e.superseded_by IS NULL"

# The per-edge projection shared by the ego and stitch queries. ``has_evidence``
# and ``signal_count`` are computed here so the viewer can tell, without a
# second round trip, which edges will actually answer a click.
_EDGE_COLS = """
        e.id, e.edge_family, e.edge_type, e.polarity, e.confidence,
        e.observed_count, e.intent, e.channel, e.source_type,
        e.valid_from, e.first_seen_at, e.last_seen_at,
        (e.evidence_set IS NOT NULL) AS has_evidence,
        coalesce(array_length(e.source_signal_ids, 1), 0) AS signal_count
"""


# ---- Wire models ----


class GraphNode(BaseModel):
    """One actor on the canvas."""

    id: str
    canonical_name: str
    entity_class: str = ""
    entity_type: str = ""
    geo_country: str | None = None
    #: Total OPEN degree, unfiltered. Drives node sizing and — more usefully —
    #: tells the operator whether clicking this node will expand into anything.
    degree: int = 0
    #: False when the edge endpoint has no ``entity_profiles`` row. Referential
    #: integrity is clean today (measured: zero dangling endpoints), but the
    #: neighbour join is a LEFT JOIN so that a future dangling edge surfaces as
    #: an explicitly unresolved node instead of silently vanishing from the walk.
    resolved: bool = True


class GraphEdge(BaseModel):
    """One reified, evidentiary edge."""

    id: str
    src_id: str
    dst_id: str
    #: ``out`` = anchor is ``src_id``; ``in`` = anchor is ``dst_id``;
    #: ``stitch`` = neither endpoint is the anchor (an induced edge between two
    #: nodes already on the caller's canvas).
    direction: str
    edge_family: str
    edge_type: str
    polarity: int = 0
    confidence: float = 0.0
    observed_count: int = 0
    intent: str = ""
    channel: str = ""
    source_type: str = ""
    valid_from: datetime | None = None
    first_seen_at: datetime | None = None
    last_seen_at: datetime | None = None
    has_evidence: bool = False
    signal_count: int = 0


class GraphFacet(BaseModel):
    """A (family, type) bucket over the anchor's UNFILTERED neighbourhood.

    Facets are deliberately computed WITHOUT the caller's filters applied.
    They are the honest denominator: they let the viewer say "showing 60 of
    693, and 8,722 cooccurrence edges are hidden" instead of quietly
    presenting a filtered view as if it were the whole neighbourhood.
    """

    edge_family: str
    edge_type: str
    count: int
    negative: int = 0
    neutral: int = 0
    positive: int = 0


class EgoFilters(BaseModel):
    """Echo of the filters actually applied, so the client never guesses."""

    family: list[str] = Field(default_factory=list)
    edge_type: list[str] = Field(default_factory=list)
    polarity: list[int] = Field(default_factory=list)
    min_confidence: float = 0.0
    since: datetime | None = None
    until: datetime | None = None
    direction: str = "both"
    limit: int = DEFAULT_LIMIT


class EgoResponse(BaseModel):
    anchor: GraphNode
    nodes: list[GraphNode] = Field(default_factory=list)
    edges: list[GraphEdge] = Field(default_factory=list)
    #: Edges between nodes already on the caller's canvas that do NOT touch the
    #: anchor. Without these an expand-on-click walk renders as a tree even when
    #: the underlying graph is densely connected — the viewer would
    #: systematically under-state connectivity, which is exactly the class of
    #: quiet wrongness this surface exists to avoid.
    stitch_edges: list[GraphEdge] = Field(default_factory=list)
    facets: list[GraphFacet] = Field(default_factory=list)
    #: Open degree of the anchor with NO filters applied.
    degree_total: int = 0
    #: Open degree after filters, BEFORE the limit — so `degree_matched >
    #: len(edges)` is precisely the truncation the viewer must disclose.
    degree_matched: int = 0
    truncated: bool = False
    filters: EgoFilters


class EvidenceSignal(BaseModel):
    """One source signal underwriting an edge."""

    id: str
    title: str = ""
    url: str | None = None
    source_id: str = ""
    fetched_at: datetime | None = None
    language: str | None = None


class EdgeEvidence(BaseModel):
    """Why one edge exists."""

    edge: GraphEdge
    src: GraphNode
    dst: GraphNode
    #: True when there is anything at all to show — a snippet or a signal.
    evidence_available: bool = False
    #: Present when ``evidence_available`` is false: the reason, in operator
    #: language, so an empty panel is never mistaken for a failed lookup.
    detail: str = ""
    evidence_text: str = ""
    signals: list[EvidenceSignal] = Field(default_factory=list)
    #: Signal ids referenced by the edge that no longer resolve to a row —
    #: reported rather than dropped, because a vanished source is a fact about
    #: the evidence, not a rendering detail.
    unresolved_signal_ids: list[str] = Field(default_factory=list)
    signal_count: int = 0
    promoted_from_proposed_edge: str | None = None
    derived_from: list[str] = Field(default_factory=list)
    analyst_id: str | None = None
    run_id: str | None = None
    produced_at: datetime | None = None


# ---- Pure helpers ----


def _parse_uuid(raw: str, *, field: str) -> UUID:
    try:
        return UUID(str(raw).strip())
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{field} must be a uuid, got {raw!r}",
        ) from None


def _validate_families(raw: list[str] | None) -> list[str]:
    """Reject an unknown family rather than returning a confidently empty walk."""
    if not raw:
        return []
    out: list[str] = []
    for item in raw:
        for part in str(item).split(","):
            fam = part.strip().lower()
            if not fam:
                continue
            if fam not in EDGE_FAMILIES:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=(
                        f"unknown edge_family {fam!r}; "
                        f"expected one of {', '.join(EDGE_FAMILIES)}"
                    ),
                )
            if fam not in out:
                out.append(fam)
    return out


def _validate_polarity(raw: list[int] | None) -> list[int]:
    if not raw:
        return []
    out: list[int] = []
    for item in raw:
        try:
            val = int(item)
        except (TypeError, ValueError):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"polarity must be an integer, got {item!r}",
            ) from None
        if val not in EDGE_POLARITIES:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"polarity must be one of -1, 0, 1; got {val}",
            )
        if val not in out:
            out.append(val)
    return out


def _split_csv(raw: list[str] | None) -> list[str]:
    """Accept both repeated params and comma-separated values."""
    if not raw:
        return []
    out: list[str] = []
    for item in raw:
        for part in str(item).split(","):
            val = part.strip()
            if val and val not in out:
                out.append(val)
    return out


def _validate_direction(raw: str) -> str:
    val = (raw or "both").strip().lower()
    if val not in ("both", "out", "in"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"direction must be one of both, out, in; got {raw!r}",
        )
    return val


def _validate_limit(raw: int) -> int:
    return DEFAULT_LIMIT if raw <= 0 else min(int(raw), MAX_LIMIT)


def _build_edge_filter(
    args: list[Any],
    *,
    families: list[str],
    edge_types: list[str],
    polarity: list[int],
    min_confidence: float,
    since: datetime | None,
    until: datetime | None,
) -> str:
    """Append filter params to ``args`` and return the matching SQL fragment.

    The fragment is spliced into BOTH arms of the ego union, which is safe and
    intentional: the two arms share one positional argument list, so the same
    ``$n`` means the same value on both sides. Nothing from the caller is ever
    interpolated — only ``$n`` placeholders — so the fragment carries no
    injection surface.
    """
    clauses: list[str] = []
    if families:
        args.append(families)
        clauses.append(f"AND e.edge_family = ANY(${len(args)}::text[])")
    if edge_types:
        args.append(edge_types)
        clauses.append(f"AND e.edge_type = ANY(${len(args)}::text[])")
    if polarity:
        args.append(polarity)
        clauses.append(f"AND e.polarity = ANY(${len(args)}::smallint[])")
    if min_confidence > 0:
        args.append(float(min_confidence))
        clauses.append(f"AND e.confidence >= ${len(args)}")
    if since is not None:
        args.append(since)
        clauses.append(f"AND e.last_seen_at >= ${len(args)}")
    if until is not None:
        args.append(until)
        clauses.append(f"AND e.last_seen_at <= ${len(args)}")
    return "\n          ".join(clauses)


def _node(row: Mapping[str, Any], *, degree: int = 0) -> GraphNode:
    """Hydrate a node, keeping an unresolved endpoint visible as unresolved."""
    name = row.get("canonical_name")
    return GraphNode(
        id=str(row["id"]),
        canonical_name=str(name) if name else f"unresolved:{str(row['id'])[:8]}",
        entity_class=str(row.get("entity_class") or ""),
        entity_type=str(row.get("entity_type") or ""),
        geo_country=row.get("geo_country"),
        degree=degree,
        resolved=name is not None,
    )


def _edge(row: Mapping[str, Any], *, direction: str, anchor: str | None = None) -> GraphEdge:
    src = str(row["src_id"])
    dst = str(row["dst_id"])
    return GraphEdge(
        id=str(row["id"]),
        src_id=src,
        dst_id=dst,
        direction=direction if anchor is None or anchor in (src, dst) else "stitch",
        edge_family=str(row["edge_family"]),
        edge_type=str(row["edge_type"]),
        polarity=int(row.get("polarity") or 0),
        confidence=float(row.get("confidence") or 0.0),
        observed_count=int(row.get("observed_count") or 0),
        intent=str(row.get("intent") or ""),
        channel=str(row.get("channel") or ""),
        source_type=str(row.get("source_type") or ""),
        valid_from=row.get("valid_from"),
        first_seen_at=row.get("first_seen_at"),
        last_seen_at=row.get("last_seen_at"),
        has_evidence=bool(row.get("has_evidence")),
        signal_count=int(row.get("signal_count") or 0),
    )


def _no_evidence_detail(source_type: str) -> str:
    """Say WHY an edge has nothing to show, in the operator's language."""
    if source_type == "seed":
        return (
            "this edge came from a curated seed import, which asserts the "
            "relationship without attaching a source document — there is no "
            "snippet or signal to show, and that is the seed contract, not a "
            "lookup failure"
        )
    if source_type == "manual":
        return (
            "this edge was entered manually; no source signal was recorded "
            "against it"
        )
    return (
        "no evidence snippet or source signal was captured for this edge — "
        "it exists in the store without provenance, which is itself worth "
        "knowing"
    )


# ---- SQL ----

_ANCHOR_SQL = """
    SELECT id, canonical_name, entity_class, entity_type, geo_country
      FROM entity_profiles
     WHERE id = $1
"""

# Unfiltered facets + total degree over the anchor's open neighbourhood. The
# honest denominator behind every filtered view. Measured at 1.7 ms on the
# highest-degree actor in the live graph.
_FACETS_SQL = f"""
    SELECT edge_family, edge_type,
           count(*)::int                                AS n,
           count(*) FILTER (WHERE polarity < 0)::int    AS negative,
           count(*) FILTER (WHERE polarity = 0)::int    AS neutral,
           count(*) FILTER (WHERE polarity > 0)::int    AS positive
      FROM (
            SELECT e.edge_family, e.edge_type, e.polarity
              FROM entity_edges e
             WHERE e.src_id = $1 AND {_OPEN}
            UNION ALL
            SELECT e.edge_family, e.edge_type, e.polarity
              FROM entity_edges e
             WHERE e.dst_id = $1 AND {_OPEN}
           ) s
     GROUP BY 1, 2
     ORDER BY n DESC, 1, 2
"""

# Open degree for a batch of neighbours, so the viewer can size nodes and show
# which ones still have somewhere to go.
_DEGREE_SQL = f"""
    SELECT eid, count(*)::int AS degree
      FROM (
            SELECT e.src_id AS eid FROM entity_edges e
             WHERE e.src_id = ANY($1::uuid[]) AND {_OPEN}
            UNION ALL
            SELECT e.dst_id FROM entity_edges e
             WHERE e.dst_id = ANY($1::uuid[]) AND {_OPEN}
           ) s
     GROUP BY 1
"""

_EVIDENCE_EDGE_SQL = f"""
    SELECT e.src_id, e.dst_id, {_EDGE_COLS.strip()},
           e.evidence_set, e.source_signal_ids, e.derived_from,
           e.analyst_id, e.run_id, e.produced_at
      FROM entity_edges e
     WHERE e.id = $1
"""

_EVIDENCE_SIGNALS_SQL = """
    SELECT s.id,
           coalesce(s.payload->>'title', '')                       AS title,
           coalesce(s.canonical_url, s.payload->>'link')            AS url,
           s.source_id, s.fetched_at, s.language
      FROM signals s
     WHERE s.id = ANY($1::uuid[])
     ORDER BY s.fetched_at DESC NULLS LAST
     LIMIT $2
"""

_EVIDENCE_ENDPOINTS_SQL = """
    SELECT id, canonical_name, entity_class, entity_type, geo_country
      FROM entity_profiles
     WHERE id = ANY($1::uuid[])
"""


def _family_precedence_sql(column: str) -> str:
    """Rank the families in declared order, for the budget's final tie-break.

    Built from ``EDGE_FAMILIES``, a module constant — nothing the caller sends
    reaches this string, so it carries no injection surface.
    """
    whens = " ".join(f"WHEN '{fam}' THEN {i}" for i, fam in enumerate(EDGE_FAMILIES))
    return f"CASE {column} {whens} ELSE {len(EDGE_FAMILIES)} END"


def _ego_sql(filt: str, *, direction: str, limit_param: int) -> str:
    """Assemble the anchored ego query.

    Both arms carry the open predicate and the caller's filter fragment. The
    ``count(*) OVER ()`` gives the pre-limit match count in the same pass, so
    truncation is disclosed without a second query.

    **The budget is spent per family.** Each family is ranked independently
    (``row_number() OVER (PARTITION BY edge_family ORDER BY confidence DESC)``)
    and the outer ordering leads with that rank, so the budget is filled
    round-robin: every family's best edge, then every family's second-best, and
    so on until the limit runs out. Two properties follow, and they are the
    whole point of the shape:

      * **No family can starve another by volume.** A family's k-th edge is
        only displaced by *k-th-or-better* edges of other families, never by
        the 500th co-mention. With ``f`` families matching, each is guaranteed
        ``min(its matches, floor(limit / f))`` slots. Ordering on confidence
        alone gave a 585-edge family the entire budget (see the module
        docstring); this cannot.
      * **No budget is wasted.** There is no per-family WHERE cap, so a family
        that runs out of edges simply stops appearing at higher ranks and its
        share flows to the families that have more. The limit is always filled
        when there are rows to fill it with.

    Family precedence breaks ties *within* one rank, which only decides who
    takes the leftover slots in the tier the limit cuts through — at most
    ``f - 1`` rows. It runs claims-first (``EDGE_FAMILIES`` order), because
    when the budget cuts mid-tier the assertion is worth more than the
    statistic.

    ``match_count`` is still computed over the whole pre-limit set, so
    ``degree_matched`` — and the truncation the viewer discloses from it —
    means exactly what it meant before.
    """
    out_arm = f"""
            SELECT e.src_id, e.dst_id, e.dst_id AS other_id, 'out'::text AS direction,
                   {_EDGE_COLS.strip()}
              FROM entity_edges e
             WHERE e.src_id = $1 AND {_OPEN}
          {filt}
    """
    in_arm = f"""
            SELECT e.src_id, e.dst_id, e.src_id AS other_id, 'in'::text AS direction,
                   {_EDGE_COLS.strip()}
              FROM entity_edges e
             WHERE e.dst_id = $1 AND {_OPEN}
          {filt}
    """
    if direction == "out":
        arms = out_arm
    elif direction == "in":
        arms = in_arm
    else:
        arms = f"{out_arm}\n            UNION ALL\n{in_arm}"
    return f"""
    WITH nb AS (
    {arms}
    ), ranked AS (
        SELECT nb.*, count(*) OVER () AS match_count,
               row_number() OVER (
                   PARTITION BY nb.edge_family
                   ORDER BY nb.confidence DESC,
                            nb.last_seen_at DESC NULLS LAST,
                            nb.id
               ) AS family_rank
          FROM nb
    )
    SELECT ranked.*,
           p.canonical_name, p.entity_class, p.entity_type, p.geo_country
      FROM ranked
      LEFT JOIN entity_profiles p ON p.id = ranked.other_id
     ORDER BY ranked.family_rank,
              {_family_precedence_sql("ranked.edge_family")},
              ranked.confidence DESC,
              ranked.last_seen_at DESC NULLS LAST
     LIMIT ${limit_param}
    """


def _stitch_sql(filt: str, *, limit_param: int) -> str:
    """Induced edges among the caller's node set that do NOT touch the anchor."""
    return f"""
    SELECT e.src_id, e.dst_id, {_EDGE_COLS.strip()}
      FROM entity_edges e
     WHERE e.src_id = ANY($2::uuid[])
       AND e.dst_id = ANY($2::uuid[])
       AND e.src_id <> $1 AND e.dst_id <> $1
       AND {_OPEN}
  {filt}
     ORDER BY e.confidence DESC
     LIMIT ${limit_param}
    """


# ---- Router ----


def build_graph_walk_router(deps: RegistryAPIDeps) -> APIRouter:
    router = APIRouter(tags=["graph-walk"])

    @router.get("/graph/ego", response_model=EgoResponse)
    async def graph_ego(
        entity_id: str = Query(
            ..., description="uuid of the actor to anchor the walk on"
        ),
        family: list[str] | None = Query(
            default=None,
            description=(
                "edge families to include (repeatable or comma-separated); "
                "omit for all. One of: " + ", ".join(EDGE_FAMILIES)
            ),
        ),
        edge_type: list[str] | None = Query(
            default=None,
            description="edge types to include (repeatable or comma-separated)",
        ),
        polarity: list[int] | None = Query(
            default=None, description="polarity values to include: -1, 0, 1"
        ),
        min_confidence: float = Query(
            default=0.0, ge=0.0, le=1.0, description="minimum edge confidence"
        ),
        since: datetime | None = Query(
            default=None, description="only edges last seen at or after this instant"
        ),
        until: datetime | None = Query(
            default=None, description="only edges last seen at or before this instant"
        ),
        direction: str = Query(
            default="both", description="both | out | in (relative to the anchor)"
        ),
        limit: int = Query(default=DEFAULT_LIMIT, description="max edges to return"),
        known: list[str] | None = Query(
            default=None,
            description=(
                "uuids already on the caller's canvas; edges among them that do "
                "not touch the anchor come back as stitch_edges so an "
                "expand-on-click walk does not render a dense graph as a tree"
            ),
        ),
        principal: str = Depends(require_bearer),
    ) -> EgoResponse:
        anchor_id = _parse_uuid(entity_id, field="entity_id")
        families = _validate_families(family)
        edge_types = _split_csv(edge_type)
        polarities = _validate_polarity(polarity)
        direction = _validate_direction(direction)
        limit = _validate_limit(limit)

        if since is not None and until is not None and since > until:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="since must not be after until",
            )

        known_ids: list[UUID] = []
        for raw in _split_csv(known)[:MAX_KNOWN]:
            parsed = _parse_uuid(raw, field="known")
            if parsed != anchor_id and parsed not in known_ids:
                known_ids.append(parsed)

        args: list[Any] = [anchor_id]
        filt = _build_edge_filter(
            args,
            families=families,
            edge_types=edge_types,
            polarity=polarities,
            min_confidence=min_confidence,
            since=since,
            until=until,
        )
        args.append(limit)
        ego_sql = _ego_sql(filt, direction=direction, limit_param=len(args))

        async with deps.descriptor_registry.pg.acquire() as conn:
            anchor_row = await conn.fetchrow(_ANCHOR_SQL, anchor_id)
            # FAIL LOUD. "no such actor" and "this actor has no edges" are
            # different answers; collapsing them into an empty 200 would let a
            # typo read as evidence of isolation.
            if anchor_row is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"no entity profile with id {anchor_id}",
                )

            edge_rows = await conn.fetch(ego_sql, *args)
            facet_rows = await conn.fetch(_FACETS_SQL, anchor_id)

            # One node per distinct neighbour, first occurrence wins. Two actors
            # can be joined by several edges (a `relation` and a `cooccurrence`,
            # say) and the canvas wants one node carrying both.
            neighbour_rows: dict[UUID, Mapping[str, Any]] = {}
            for row in edge_rows:
                other = row["other_id"]
                if other != anchor_id and other not in neighbour_rows:
                    neighbour_rows[other] = row
            neighbour_ids = list(neighbour_rows)

            # Stitch across the caller's canvas plus whatever this hop added.
            stitch_rows: list[Any] = []
            if known_ids:
                canvas = list(dict.fromkeys([*known_ids, *neighbour_ids]))[:MAX_KNOWN]
                if len(canvas) > 1:
                    stitch_args: list[Any] = [anchor_id, canvas]
                    stitch_filt = _build_edge_filter(
                        stitch_args,
                        families=families,
                        edge_types=edge_types,
                        polarity=polarities,
                        min_confidence=min_confidence,
                        since=since,
                        until=until,
                    )
                    # $1 anchor, $2 canvas, then the filter fragment's params,
                    # then the limit — so the limit's position is whatever the
                    # filter left it at, which is why _stitch_sql takes it.
                    stitch_args.append(MAX_LIMIT)
                    stitch_rows = list(
                        await conn.fetch(
                            _stitch_sql(stitch_filt, limit_param=len(stitch_args)),
                            *stitch_args,
                        )
                    )

            degrees: dict[UUID, int] = {}
            degree_targets = list(
                dict.fromkeys([anchor_id, *neighbour_ids])
            )
            if degree_targets:
                for row in await conn.fetch(_DEGREE_SQL, degree_targets):
                    degrees[row["eid"]] = int(row["degree"])

        degree_total = degrees.get(anchor_id, 0)
        degree_matched = int(edge_rows[0]["match_count"]) if edge_rows else 0

        nodes = [
            _node(
                {
                    "id": nid,
                    "canonical_name": row["canonical_name"],
                    "entity_class": row["entity_class"],
                    "entity_type": row["entity_type"],
                    "geo_country": row["geo_country"],
                },
                degree=degrees.get(nid, 0),
            )
            for nid, row in neighbour_rows.items()
        ]

        return EgoResponse(
            anchor=_node(anchor_row, degree=degree_total),
            nodes=nodes,
            edges=[_edge(r, direction=str(r["direction"])) for r in edge_rows],
            stitch_edges=[_edge(r, direction="stitch") for r in stitch_rows],
            facets=[
                GraphFacet(
                    edge_family=str(r["edge_family"]),
                    edge_type=str(r["edge_type"]),
                    count=int(r["n"]),
                    negative=int(r["negative"]),
                    neutral=int(r["neutral"]),
                    positive=int(r["positive"]),
                )
                for r in facet_rows
            ],
            degree_total=degree_total,
            degree_matched=degree_matched,
            truncated=degree_matched > len(edge_rows),
            filters=EgoFilters(
                family=families,
                edge_type=edge_types,
                polarity=polarities,
                min_confidence=min_confidence,
                since=since,
                until=until,
                direction=direction,
                limit=limit,
            ),
        )

    @router.get("/graph/edge/{edge_id}", response_model=EdgeEvidence)
    async def graph_edge_evidence(
        edge_id: str,
        principal: str = Depends(require_bearer),
    ) -> EdgeEvidence:
        eid = _parse_uuid(edge_id, field="edge_id")

        async with deps.descriptor_registry.pg.acquire() as conn:
            row = await conn.fetchrow(_EVIDENCE_EDGE_SQL, eid)
            if row is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"no edge with id {eid}",
                )
            endpoint_rows = await conn.fetch(
                _EVIDENCE_ENDPOINTS_SQL, [row["src_id"], row["dst_id"]]
            )
            signal_ids = list(row["source_signal_ids"] or [])
            signal_rows: list[Any] = []
            if signal_ids:
                signal_rows = list(
                    await conn.fetch(
                        _EVIDENCE_SIGNALS_SQL,
                        signal_ids[:MAX_EVIDENCE_SIGNALS],
                        MAX_EVIDENCE_SIGNALS,
                    )
                )

        by_id = {r["id"]: r for r in endpoint_rows}
        src_row = by_id.get(row["src_id"]) or {"id": row["src_id"], "canonical_name": None}
        dst_row = by_id.get(row["dst_id"]) or {"id": row["dst_id"], "canonical_name": None}

        evidence_set = row["evidence_set"]
        if isinstance(evidence_set, str):
            import json

            try:
                evidence_set = json.loads(evidence_set)
            except ValueError:
                evidence_set = {}
        if not isinstance(evidence_set, dict):
            evidence_set = {}

        evidence_text = str(evidence_set.get("evidence_text") or "")
        promoted = evidence_set.get("promoted_from_proposed_edge")

        resolved = {r["id"] for r in signal_rows}
        unresolved = [str(s) for s in signal_ids if s not in resolved]

        available = bool(evidence_text or signal_rows)
        source_type = str(row["source_type"] or "")

        return EdgeEvidence(
            edge=_edge(row, direction="out"),
            src=_node(src_row),
            dst=_node(dst_row),
            evidence_available=available,
            detail="" if available else _no_evidence_detail(source_type),
            evidence_text=evidence_text,
            signals=[
                EvidenceSignal(
                    id=str(r["id"]),
                    title=str(r["title"] or ""),
                    url=r["url"],
                    source_id=str(r["source_id"] or ""),
                    fetched_at=r["fetched_at"],
                    language=r["language"],
                )
                for r in signal_rows
            ],
            unresolved_signal_ids=unresolved,
            signal_count=len(signal_ids),
            promoted_from_proposed_edge=str(promoted) if promoted else None,
            derived_from=[str(d) for d in (row["derived_from"] or [])],
            analyst_id=row["analyst_id"],
            run_id=str(row["run_id"]) if row["run_id"] else None,
            produced_at=row["produced_at"],
        )

    return router


__all__ = [
    "EDGE_FAMILIES",
    "EDGE_POLARITIES",
    "EdgeEvidence",
    "EgoResponse",
    "GraphEdge",
    "GraphFacet",
    "GraphNode",
    "build_graph_walk_router",
]
