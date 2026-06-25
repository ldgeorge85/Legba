# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``graph_mining`` sub-handler — L-006 sub-split A.

Reads the Apache AGE entity-relationship subgraph touched by the inputs
and computes structured deterministic features over it:

  * **Community detection** via greedy modularity (networkx). Returns
    communities + modularity score.
  * **Centrality** — degree + betweenness (capped graph size so the run
    stays sub-second on the recent-activity subgraph).
  * **Proxy chains** — length-2/3 paths through intermediary entities
    where the start-end pair has no direct edge. Classic proxy/cut-out
    mining shape.

No LLM. Uses ``networkx`` for the heavy lifting; AGE only as the read
source. Results land in ``FindingPayload.data`` under the keys documented
in :func:`_build_finding`.

The handler degrades gracefully:

  * If ``deps`` has no live ``pg_pool``, the handler runs the same
    pipeline over edges synthesized from ``inputs`` themselves (each
    input row may carry ``source_entity``/``target_entity``/``polarity``
    keys). This is the unit-test path.
  * If networkx is missing the algorithm needed (e.g. greedy modularity
    on the very-old 3.0 fallback), the handler skips that block and
    continues; ``data.warnings`` records which blocks were skipped.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping
from uuid import UUID

import networkx as nx

from ...provenance.models import FindingPayload
from ....runtime.analyst_method import AnalystMethodResult
from ._graph_metrics_sink import write_graph_metric

logger = logging.getLogger(__name__)

# Cap to keep deterministic mining bounded. Larger subgraphs should be
# scoped at the descriptor level (predicate filter) rather than here.
_MAX_NODES = 5_000
_MAX_PROXY_PATH_LEN = 3
_MAX_PROXY_CHAINS = 200
_MAX_INTERESTING = 12  # shared "interesting" shortlist cap (#99 contract).
# A negative-polarity nexus whose valid_from is within this window counts as a
# "new" hostile edge; recency score decays linearly to 0 across it.
_NEW_EDGE_WINDOW_DAYS = 30.0
# #99: max discovered proxy chains reified back as first-class nexuses per run.
_MAX_REIFIED_CHAINS = 25


def _reify_discovered_chains_enabled() -> bool:
    """Operator opt-in for reifying discovered proxy chains (#99).

    Default OFF — live behavior is UNCHANGED until the operator flips this on,
    mirroring the AGE write-leg opt-in discipline. Reads the flag at call time
    (not import) so a runtime env change takes effect on the next run.
    """
    return os.getenv("LEGBA_REIFY_DISCOVERED_CHAINS", "").strip().lower() in (
        "1", "true", "yes", "on",
    )


# ---------------------------------------------------------------------------
# Input → graph
# ---------------------------------------------------------------------------


def _graph_from_inputs(
    inputs: list[dict[str, Any]],
) -> nx.MultiDiGraph:
    """Build a directed multigraph from input rows.

    Each row may carry the canonical edge keys:

      * ``source_entity`` / ``target_entity`` — vertex ids (str).
      * ``edge_label`` — relationship type (defaults to ``"RelatedTo"``).
      * ``polarity`` — ``+1``/``-1``/``0`` (used by other handlers; preserved
        as edge attribute so callers can re-share the graph).
      * ``confidence`` — optional weight.

    Rows missing both endpoints are skipped (logged at debug level).
    """
    g: nx.MultiDiGraph = nx.MultiDiGraph()
    for row in inputs:
        src = row.get("source_entity") or row.get("src") or row.get("subject")
        tgt = row.get("target_entity") or row.get("dst") or row.get("object")
        if not src or not tgt:
            logger.debug("graph_mining.skip_row reason=missing_endpoints row=%r", row)
            continue
        label = row.get("edge_label") or row.get("relationship_type") or "RelatedTo"
        polarity = int(row.get("polarity", 0) or 0)
        confidence = float(row.get("confidence", 1.0) or 1.0)
        g.add_node(str(src))
        g.add_node(str(tgt))
        g.add_edge(
            str(src),
            str(tgt),
            label=str(label),
            polarity=polarity,
            confidence=confidence,
        )
    return g


# ---------------------------------------------------------------------------
# Community detection
# ---------------------------------------------------------------------------


def _detect_communities(
    g: nx.Graph,
) -> tuple[list[list[str]], float | None]:
    """Run greedy modularity community detection.

    Operates on the undirected projection of ``g`` because modularity
    is defined for undirected graphs. Returns (communities, modularity).
    Returns ``([], None)`` if the graph is empty or networkx lacks the
    algorithm.
    """
    if g.number_of_nodes() == 0:
        return [], None
    undirected = nx.Graph()
    for u, v, d in g.edges(data=True):
        if undirected.has_edge(u, v):
            undirected[u][v]["weight"] += float(d.get("confidence", 1.0))
        else:
            undirected.add_edge(u, v, weight=float(d.get("confidence", 1.0)))
    try:
        comm_iter = nx.community.greedy_modularity_communities(
            undirected, weight="weight"
        )
    except (AttributeError, nx.NetworkXError) as exc:  # pragma: no cover
        logger.warning("graph_mining.community.unavailable err=%s", exc)
        return [], None
    communities = [sorted(c) for c in comm_iter]
    try:
        modularity = nx.community.modularity(undirected, communities, weight="weight")
    except Exception as exc:  # pragma: no cover
        logger.debug("graph_mining.community.modularity_failed err=%s", exc)
        modularity = None
    return communities, modularity


# ---------------------------------------------------------------------------
# Centrality
# ---------------------------------------------------------------------------


def _centrality(g: nx.Graph, top_n: int = 25) -> dict[str, dict[str, float]]:
    """Compute degree + betweenness centrality, return top_n nodes by degree."""
    if g.number_of_nodes() == 0:
        return {}
    # Project to a simple directed graph for centrality (drops parallel-
    # edge multiplicity; that's the right semantic for centrality).
    simple = nx.DiGraph()
    for u, v, d in g.edges(data=True):
        if simple.has_edge(u, v):
            simple[u][v]["weight"] += float(d.get("confidence", 1.0))
        else:
            simple.add_edge(u, v, weight=float(d.get("confidence", 1.0)))
    deg = dict(simple.degree())
    # k-sampled betweenness on big graphs to stay sub-second.
    k_sample = min(simple.number_of_nodes(), 64)
    try:
        bet = nx.betweenness_centrality(simple, k=k_sample, seed=42, weight="weight")
    except Exception as exc:  # pragma: no cover
        logger.debug("graph_mining.centrality.betweenness_failed err=%s", exc)
        bet = {n: 0.0 for n in simple.nodes()}
    ordered = sorted(deg.items(), key=lambda kv: kv[1], reverse=True)[:top_n]
    return {
        node: {
            "degree": float(deg.get(node, 0)),
            "betweenness": float(bet.get(node, 0.0)),
        }
        for node, _ in ordered
    }


# ---------------------------------------------------------------------------
# Proxy chains
# ---------------------------------------------------------------------------


def _proxy_chains(
    g: nx.MultiDiGraph,
    *,
    max_chains: int = _MAX_PROXY_CHAINS,
    max_path_len: int = _MAX_PROXY_PATH_LEN,
) -> list[dict[str, Any]]:
    """Find indirect paths whose endpoints have no direct edge.

    Returns a list of ``{actor, target, via, length, polarity_sign}``
    dicts. The polarity_sign is the product of edge polarities along the
    path — useful for hostile-via-proxy detection (negative product) and
    laundering-detection (positive via hostile intermediate).
    """
    if g.number_of_nodes() < 3:
        return []
    simple = nx.DiGraph()
    edge_polarity: dict[tuple[str, str], int] = {}
    for u, v, d in g.edges(data=True):
        simple.add_edge(u, v)
        # Multi-edges between same pair: combine polarities multiplicatively.
        sign = 1 if int(d.get("polarity", 0) or 0) >= 0 else -1
        edge_polarity[(u, v)] = edge_polarity.get((u, v), 1) * sign

    # DETERMINISM FIX (#99): the old code early-broke after the first
    # ``max_chains`` paths in ``list(g.nodes())`` order — WHICH chains surfaced
    # depended on arbitrary node insertion order. Instead enumerate ALL chains
    # (bounded by the n^2 pair walk + cutoff), SCORE each, then return a
    # deterministic top-K. Score rewards a negative sign-product cut-out (the
    # hostile-via-proxy / laundering shape this miner exists to find) and
    # shorter, tighter chains. A stable secondary sort key (the chain tuple)
    # makes ties deterministic regardless of node order.
    nodes = sorted(simple.nodes())
    scored: list[tuple[float, tuple[str, ...], dict[str, Any]]] = []
    for actor in nodes:
        for target in nodes:
            if actor == target or simple.has_edge(actor, target):
                continue
            try:
                paths = nx.all_simple_paths(
                    simple, actor, target, cutoff=max_path_len
                )
            except nx.NetworkXError:  # pragma: no cover
                continue
            for path in paths:
                if len(path) < 3:  # must have at least one intermediary
                    continue
                via = path[1:-1]
                sign = 1
                for u, v in zip(path[:-1], path[1:]):
                    sign *= edge_polarity.get((u, v), 1)
                hops = len(path) - 1
                # Interestingness: a negative sign-product cut-out is the
                # headline shape (score 1.0 base); a positive one is mundane
                # (0.4). Tighter chains rank above sprawling ones.
                base = 1.0 if sign < 0 else 0.4
                score = base * (1.0 / float(hops))
                chain = {
                    "actor": actor,
                    "target": target,
                    "via": via,
                    "length": hops,
                    "polarity_sign": sign,
                    "score": round(score, 4),
                }
                scored.append((score, tuple(path), chain))
    # Deterministic: score desc, then the full path tuple asc as the tiebreak.
    scored.sort(key=lambda t: (-t[0], t[1]))
    return [c for _, _, c in scored[:max_chains]]


# ---------------------------------------------------------------------------
# Live-substrate AGE pull (opt-in)
# ---------------------------------------------------------------------------


async def _augment_from_age(
    inputs: list[dict[str, Any]],
    deps: Any,
    g: nx.MultiDiGraph,
) -> dict[str, int]:
    """Pull additional edges incident to input-row entities from AGE.

    Best-effort: any failure (missing pool, AGE not loaded, label
    permission issue) logs and returns zero-augmentation. The handler's
    finding still ships with the input-only graph.

    Returns counters: ``{"edges_pulled": N, "nodes_added": N}``.
    """
    counters = {"edges_pulled": 0, "nodes_added": 0}
    pool = getattr(deps, "pg_pool", None) if deps is not None else None
    if pool is None:
        return counters
    seed_entities = {n for n in g.nodes()}
    if not seed_entities:
        return counters
    quoted = ", ".join(f'"{e}"' for e in list(seed_entities)[:200])
    if not quoted:
        return counters
    cypher = (
        "MATCH (a)-[r]->(b) "
        f"WHERE a.id IN [{quoted}] OR b.id IN [{quoted}] "
        "RETURN a.id AS src, b.id AS dst, label(r) AS rel "
        f"LIMIT {_MAX_NODES}"
    )
    try:
        async with pool.acquire() as conn:
            await conn.execute("LOAD 'age'")
            await conn.execute('SET search_path = ag_catalog, "$user", public')
            rows = await conn.fetch(
                "SELECT * FROM cypher('legba_graph', $$"
                + cypher
                + "$$) AS (src agtype, dst agtype, rel agtype)"
            )
    except Exception as exc:
        logger.warning("graph_mining.age.pull_failed err=%s", exc)
        return counters

    for row in rows:
        src = _strip_agtype(row["src"])
        dst = _strip_agtype(row["dst"])
        rel = _strip_agtype(row["rel"])
        if not src or not dst:
            continue
        if src not in g.nodes():
            counters["nodes_added"] += 1
        if dst not in g.nodes():
            counters["nodes_added"] += 1
        polarity = _label_polarity(rel)
        g.add_edge(
            src, dst, label=rel, polarity=polarity, confidence=1.0,
        )
        counters["edges_pulled"] += 1
    return counters


def _strip_agtype(value: Any) -> str:
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


# Heuristic mapping AGE edge labels → polarity. Anything not listed is
# treated as neutral (0). The structural_balance handler has the
# authoritative version of this table.
_NEGATIVE_LABELS = {"HostileTo", "Targets", "SuppliesWeaponsTo"}
_POSITIVE_LABELS = {"AlliedWith", "MemberOf", "LeaderOf", "AffiliatedWith"}


def _label_polarity(label: str) -> int:
    if label in _NEGATIVE_LABELS:
        return -1
    if label in _POSITIVE_LABELS:
        return 1
    return 0


async def _augment_from_nexuses(deps: Any, g: "nx.MultiDiGraph") -> int:
    """Add directed SIGNED edges from the OPEN ``nexuses`` rows (PIECE A).

    A reified relationship with an ``intermediary`` is added as the two-hop
    proxy chain ``subject -> intermediary -> object`` (each hop carrying the
    nexus polarity) so the proxy-chain sign-product mining sees the cut-out;
    a direct relationship is added as ``subject -> object``. This is what makes
    proxy-chain detection run over the reifier's typed signed edges instead of
    the untyped co-occurrence graph (PIECE A light-up). Returns edges added;
    failures degrade to 0.
    """
    pool = getattr(deps, "pg_pool", None) if deps is not None else None
    if pool is None:
        return 0
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT subject, intermediary, object, polarity, rel_type
                  FROM nexuses
                 WHERE valid_until IS NULL AND superseded_by IS NULL
                   -- #99: NEVER re-discover from our own inferred reifications,
                   -- else proxy-chain mining amplifies inferred-on-inferred.
                   AND COALESCE(source_type, '') <> 'inferred'
                 LIMIT 20000
                """
            )
    except Exception as exc:
        logger.warning("graph_mining.nexus.pull_failed err=%s", exc)
        return 0
    added = 0
    for r in rows:
        subj, obj = r["subject"], r["object"]
        via = r["intermediary"]
        pol = int(r["polarity"] or 0)
        rel = r["rel_type"]
        if not subj or not obj:
            continue
        if via:
            g.add_edge(subj, via, label=rel, polarity=pol, confidence=1.0)
            g.add_edge(via, obj, label=rel, polarity=pol, confidence=1.0)
            added += 2
        else:
            g.add_edge(subj, obj, label=rel, polarity=pol, confidence=1.0)
            added += 1
    return added


async def _recent_hostile_edges(deps: Any) -> list[dict[str, Any]]:
    """Pull OPEN negative-polarity nexuses with a recent ``valid_from`` (#99).

    Feeds the ``new_hostile_edge`` interesting-item: a newly-asserted hostile
    relationship is high-signal (an alignment shift). Returns rows shaped
    ``{subject, object, polarity, rel_type, valid_from}`` ordered most-recent
    first. Best-effort — any failure (no pool, no valid_from column) yields
    ``[]`` so the interesting list simply omits this kind.
    """
    pool = getattr(deps, "pg_pool", None) if deps is not None else None
    if pool is None:
        return []
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT subject, object, polarity, rel_type, valid_from
                  FROM nexuses
                 WHERE valid_until IS NULL AND superseded_by IS NULL
                   AND polarity < 0
                   AND valid_from IS NOT NULL
                   -- #99: exclude our own inferred reifications from re-discovery.
                   AND COALESCE(source_type, '') <> 'inferred'
                 ORDER BY valid_from DESC
                 LIMIT 200
                """
            )
    except Exception as exc:
        logger.warning("graph_mining.recent_hostile.pull_failed err=%s", exc)
        return []
    out: list[dict[str, Any]] = []
    for r in rows:
        if not r["subject"] or not r["object"]:
            continue
        out.append({
            "subject": r["subject"],
            "object": r["object"],
            "polarity": int(r["polarity"] or 0),
            "rel_type": r["rel_type"],
            "valid_from": r["valid_from"],
        })
    return out


# ---------------------------------------------------------------------------
# Reify discovered proxy chains as first-class nexuses (#99, operator-gated)
# ---------------------------------------------------------------------------


async def _reify_discovered_chains(
    deps: Any,
    options: Mapping[str, Any],
    proxy_chains: list[dict[str, Any]],
) -> int:
    """Write negative sign-product cut-out chains back as reified nexuses (#99).

    Operator-gated (``LEGBA_REIFY_DISCOVERED_CHAINS``, default OFF). For each
    discovered negative-polarity proxy chain ``A -> via... -> B`` whose endpoints
    have NO direct edge, emit one ``subject=A, intermediary=via[0], object=B``
    reified nexus marked ``source_type="inferred"`` and ``channel="proxy"`` so
    these synthetic relationships are distinguishable from observed nexuses and
    EXCLUDED from re-discovery (see the nexus-pull filters). Bounded at
    :data:`_MAX_REIFIED_CHAINS` per run. Best-effort: any failure degrades to a
    partial count and never fails the run.
    """
    pool = getattr(deps, "pg_pool", None) if deps is not None else None
    if pool is None:
        return 0
    # local import — avoid an import cycle (mirrors relationship_reifier).
    from ...provenance import AnalystContext, NexusPayload, write_nexus

    run_id = options.get("run_id")
    actx = AnalystContext(
        analyst_id=str(options.get("analyst_id") or "graph_mining"),
        analyst_version=str(options.get("analyst_version") or ""),
        run_id=run_id if isinstance(run_id, UUID) else None,  # type: ignore[arg-type]
        target_id=options.get("target_id"),
        target_version=options.get("target_version"),
    )
    written = 0
    for c in proxy_chains:
        if written >= _MAX_REIFIED_CHAINS:
            break
        if int(c.get("polarity_sign", 1) or 1) >= 0:
            continue  # only the hostile-via-proxy / cut-out shape is reified
        actor, target = str(c.get("actor") or ""), str(c.get("target") or "")
        via_list = [str(v) for v in (c.get("via") or []) if str(v)]
        if not actor or not target or not via_list:
            continue
        # Collapse a multi-hop chain to a single named intermediary (the first
        # cut-out); the full path stays in `data` for provenance.
        payload = NexusPayload(
            subject=actor,
            intermediary=via_list[0],
            object=target,
            rel_type="proxy_hostility",
            label="discovered proxy chain",
            polarity=-1,
            intent="indirect / cut-out",
            channel="proxy",
            confidence=float(c.get("score", 0.0) or 0.0),
            data={
                "discovered_via": "graph_mining.proxy_chains",
                "path": [actor, *via_list, target],
                "chain_length": int(c.get("length", len(via_list) + 1) or 0),
                "polarity_sign": int(c.get("polarity_sign", -1) or -1),
            },
        )
        try:
            async with pool.acquire() as conn:
                out, _dlq = await write_nexus(
                    conn,
                    analyst_ctx=actx,
                    payload=payload,
                    derived_from=[],
                    source_type="inferred",
                )
                if out is not None:
                    written += 1
        except Exception as exc:
            logger.warning(
                "graph_mining.reify_chain_failed actor=%s target=%s err=%s",
                actor, target, exc,
            )
            continue
    if written:
        logger.info("graph_mining.reified_chains count=%d", written)
    return written


# ---------------------------------------------------------------------------
# Interesting-shortlist distillation (#99 shared contract)
# ---------------------------------------------------------------------------


def _build_interesting(
    *,
    communities: list[list[str]],
    centrality: dict[str, dict[str, float]],
    proxy_chains: list[dict[str, Any]],
    recent_hostile: list[dict[str, Any]],
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Distil the mined enumerations into a scored ``interesting`` shortlist.

    Three item kinds per the #99 contract:

      * ``broker`` — a high-betweenness node, scored by betweenness x
        cross-community-ness (a broker that sits between TWO DIFFERENT
        communities is far more interesting than one inside a single camp).
      * ``new_hostile_edge`` — a negative-polarity nexus with a recent
        ``valid_from``, scored by recency x ``|polarity|``.
      * ``proxy_chain`` — a negative sign-product cut-out, scored by the
        chain's own interestingness score (already computed in
        :func:`_proxy_chains`).

    Returns at most :data:`_MAX_INTERESTING` items, sorted by score desc.
    """
    items: list[dict[str, Any]] = []

    # node -> community index, for cross-community broker scoring.
    node_community: dict[str, int] = {}
    for idx, comm in enumerate(communities):
        for n in comm:
            node_community[n] = idx

    # --- brokers ----------------------------------------------------------
    bet_values = [m.get("betweenness", 0.0) for m in centrality.values()]
    max_bet = max(bet_values) if bet_values else 0.0
    for node, m in centrality.items():
        bet = float(m.get("betweenness", 0.0))
        if bet <= 0.0:
            continue
        norm_bet = bet / max_bet if max_bet > 0 else 0.0
        # cross-community-ness: 1.0 if the broker's graph neighbours (proxied
        # here by community membership of the centrality cohort) span more than
        # one community, else a flat 0.5 (still a hub, just within one camp).
        own_comm = node_community.get(node)
        neighbour_comms = {
            node_community.get(other)
            for other in centrality
            if other != node and node_community.get(other) is not None
        }
        spans = own_comm is not None and any(
            c is not None and c != own_comm for c in neighbour_comms
        )
        cross = 1.0 if spans else 0.5
        score = norm_bet * cross
        items.append({
            "kind": "broker",
            "label": str(node),
            "score": round(float(score), 4),
            "rationale": (
                f"high betweenness ({bet:.3f})"
                + (" sitting between opposed camps" if spans else " (intra-camp hub)")
                + " — a structural conduit / chokepoint"
            ),
            "entities": [str(node)],
        })

    # --- new hostile edges ------------------------------------------------
    now = now or datetime.now(tz=timezone.utc)
    for r in recent_hostile:
        vf = r.get("valid_from")
        if not isinstance(vf, datetime):
            continue
        if vf.tzinfo is None:
            vf = vf.replace(tzinfo=timezone.utc)
        age_days = (now - vf).total_seconds() / 86400.0
        if age_days < 0:
            age_days = 0.0
        recency = max(0.0, 1.0 - age_days / _NEW_EDGE_WINDOW_DAYS)
        if recency <= 0.0:
            continue
        pol_mag = abs(int(r.get("polarity", 0) or 0)) or 1
        score = recency * float(pol_mag)
        if score > 1.0:
            score = 1.0
        subj, obj = str(r["subject"]), str(r["object"])
        rel = r.get("rel_type") or "HostileTo"
        items.append({
            "kind": "new_hostile_edge",
            "label": f"{subj} -[{rel}]-> {obj}",
            "score": round(float(score), 4),
            "rationale": (
                f"newly-asserted hostile tie ({age_days:.0f}d old) — a fresh "
                "antagonism / possible alignment shift"
            ),
            "entities": [subj, obj],
        })

    # --- proxy chains (negative sign-product cut-outs) --------------------
    for c in proxy_chains:
        if int(c.get("polarity_sign", 1)) >= 0:
            continue  # only the hostile-via-proxy / cut-out shape is "interesting"
        actor, target = str(c.get("actor")), str(c.get("target"))
        via = [str(v) for v in (c.get("via") or [])]
        path_label = " -> ".join([actor, *via, target])
        # The chain already carries a 0..1 interestingness score from the miner.
        score = float(c.get("score", 0.0))
        items.append({
            "kind": "proxy_chain",
            "label": path_label,
            "score": round(score, 4),
            "rationale": (
                "negative sign-product chain — hostile action routed through an "
                "intermediary (cut-out / proxy shape)"
            ),
            "entities": [actor, *via, target],
        })

    items.sort(key=lambda it: (it["score"], it["label"]), reverse=True)
    return items[:_MAX_INTERESTING]


# ---------------------------------------------------------------------------
# Finding assembly
# ---------------------------------------------------------------------------


def _build_finding(
    *,
    communities: list[list[str]],
    modularity: float | None,
    centrality: dict[str, dict[str, float]],
    proxy_chains: list[dict[str, Any]],
    interesting: list[dict[str, Any]],
    node_count: int,
    edge_count: int,
    warnings: list[str],
    augment_counters: dict[str, int],
    target_id: str | None,
) -> FindingPayload:
    """Pack handler outputs into the typed FindingPayload."""
    title = (
        f"Graph mining over {node_count} nodes / {edge_count} edges"
        f"{' for ' + target_id if target_id else ''}"
    )
    body_lines = [
        f"communities={len(communities)} "
        f"modularity={modularity:.3f}" if modularity is not None
        else f"communities={len(communities)} modularity=n/a",
        f"top_centrality_nodes={len(centrality)} proxy_chains={len(proxy_chains)}",
    ]
    if augment_counters.get("edges_pulled"):
        body_lines.append(
            f"age_augmented edges={augment_counters['edges_pulled']} "
            f"nodes_added={augment_counters['nodes_added']}"
        )
    tags = ["deterministic", "graph_mining"]
    if proxy_chains:
        tags.append("proxy_chains_present")
    return FindingPayload(
        title=title[:2048],
        body="\n".join(body_lines)[:65536],
        confidence=1.0,  # deterministic — no model uncertainty
        evidence=[],
        tags=tags,
        data={
            "sub_handler": "graph_mining",
            "communities": communities,
            "modularity": modularity,
            "centrality": centrality,
            "proxy_chains": proxy_chains,
            "interesting": interesting,
            "node_count": node_count,
            "edge_count": edge_count,
            "warnings": warnings,
            "augment_counters": augment_counters,
        },
    )


# ---------------------------------------------------------------------------
# Public handler entry point
# ---------------------------------------------------------------------------


async def handle(
    inputs: list[dict[str, Any]],
    options: Mapping[str, Any],
    deps: Any | None,
) -> AnalystMethodResult:
    """Sub-handler entry point — see module docstring for shape."""
    warnings: list[str] = []
    g = _graph_from_inputs(inputs)
    augment_counters = {"edges_pulled": 0, "nodes_added": 0}
    if deps is not None and bool(options.get("augment_from_nexuses", True)):
        # PIECE A: reified signed typed edges (incl. proxy chains via the
        # intermediary) are the primary signed-edge source for proxy mining.
        augment_counters["nexus_edges"] = await _augment_from_nexuses(deps, g)
    if deps is not None and bool(options.get("augment_from_age", True)):
        age_counters = await _augment_from_age(inputs, deps, g)
        augment_counters.update(age_counters)

    if g.number_of_nodes() > _MAX_NODES:
        warnings.append(
            f"graph_mining.truncated nodes={g.number_of_nodes()} max={_MAX_NODES}"
        )
        keep = list(g.nodes())[:_MAX_NODES]
        g = g.subgraph(keep).copy()

    communities, modularity = _detect_communities(g)
    centrality = _centrality(g)
    proxy_chains = _proxy_chains(g)

    # #99 (operator-gated, default OFF): reify discovered negative cut-out chains
    # back as first-class `source_type="inferred"` nexuses. Shares the nexus opt-in
    # (`augment_from_nexuses`) AND the dedicated env flag so live behavior is
    # UNCHANGED until the operator flips it on.
    if (
        deps is not None
        and bool(options.get("augment_from_nexuses", True))
        and _reify_discovered_chains_enabled()
    ):
        augment_counters["reified_chains"] = await _reify_discovered_chains(
            deps, options, proxy_chains
        )

    # #99: distil into the scored `interesting` shortlist. The new-hostile-edge
    # leg needs nexus valid_from, pulled best-effort (omitted if unavailable).
    recent_hostile: list[dict[str, Any]] = []
    if deps is not None and bool(options.get("augment_from_nexuses", True)):
        recent_hostile = await _recent_hostile_edges(deps)
    interesting = _build_interesting(
        communities=communities,
        centrality=centrality,
        proxy_chains=proxy_chains,
        recent_hostile=recent_hostile,
    )

    node_count = g.number_of_nodes()
    edge_count = g.number_of_edges()

    # FIX P2-1: persist the run's mined metrics to the graph_metrics sink (the
    # table had no writer). Best-effort — never fails the run. We store COUNTS
    # + the modularity scalar + the top-centrality slice (already capped to 25
    # nodes upstream) rather than the full community membership lists, keeping
    # the row bounded; the full structure stays in the FindingPayload.
    await write_graph_metric(
        deps,
        options,
        metric_kind="graph_mining",
        payload={
            "community_count": len(communities),
            "modularity": modularity,
            "centrality_node_count": len(centrality),
            "top_centrality": centrality,
            "proxy_chain_count": len(proxy_chains),
            "interesting": interesting,
            "node_count": node_count,
            "edge_count": edge_count,
            "augment_counters": augment_counters,
            "target_id": options.get("target_id"),
        },
    )

    finding = _build_finding(
        communities=communities,
        modularity=modularity,
        centrality=centrality,
        proxy_chains=proxy_chains,
        interesting=interesting,
        node_count=node_count,
        edge_count=edge_count,
        warnings=warnings,
        augment_counters=augment_counters,
        target_id=options.get("target_id"),
    )
    return AnalystMethodResult(
        finding=finding,
        usage={"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0},
    )


__all__ = ["handle"]
