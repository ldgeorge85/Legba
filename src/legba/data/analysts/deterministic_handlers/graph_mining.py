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

import asyncio
import concurrent.futures
import itertools
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping
from uuid import UUID

import networkx as nx

from ...provenance.models import FindingPayload
from ....runtime.analyst_method import AnalystMethodResult
# M20 (2026-07-06 mining audit) — the shared canon spine vets edge endpoints so
# the "interesting" shortlist stops amplifying NER fragments / vague tokens
# (``West`` / ``Leader`` / ``Parl``) and mis-signed neutral edges into headline
# geopolitical signal. ``canonicalize_entity`` types a surface (country /
# location / organization); ``is_junk_entity`` drops the vague / fragment
# tokens; ``same_referent`` drops a self-loop.
from ..._entity_canon import (
    COUNTRY_CLASS,
    DEFAULT_CLASS,
    LOCATION_CLASS,
    ORGANIZATION_CLASS,
    canonicalize_entity,
    is_junk_entity,
    same_referent,
)
from ._graph_metrics_sink import write_graph_metric

# P1-T9: the shortest-path + broker engine lives in the LEAF module
# ``legba.data.graph_paths`` (stdlib + networkx + a caller-supplied AGE pool —
# NO deterministic-handler-package deps) so the slim REGISTRY image can import
# and serve ``/graph/path`` without pulling pycountry. Re-exported here so
# graph_mining's public API + internal callers (``_augment_from_age`` /
# the node-cap truncation) are unchanged — the definitions live in the leaf.
from ...graph_paths import (  # noqa: F401  (re-exports)
    _MAX_NODES,
    _MAX_PATH_LEN,
    _strip_agtype,
    build_path_neighbourhood_cypher,
    build_shortest_path_cypher,
    shortest_path_with_broker,
)

logger = logging.getLogger(__name__)

# Cap to keep deterministic mining bounded (``_MAX_NODES`` is imported from the
# graph_paths leaf above so the cap is single-sourced). Larger subgraphs should
# be scoped at the descriptor level (predicate filter) rather than here.
#
# P1 EVENT-LOOP FREEZE FIX (2026-07-24): ``_proxy_chains`` calls networkx
# ``all_simple_paths`` — worst-case EXPONENTIAL path enumeration — over the live
# reified-nexus graph. Measured live: ~1,980 vertices, max degree 474, avg 8.72,
# p99 ~99, 120 nodes at degree >= 27. A pairwise all_simple_paths walk over that
# spins for tens of minutes holding the GIL (caught by py-spy at the 2026-07-24
# 12:52 freeze; MainThread active+gil ~33 min until the watchdog killed it),
# starving healthchecks + the reminder dispatch on the SAME asyncio loop. The
# ``cutoff=`` below bounds each DFS to <= ``_MAX_PROXY_PATH_LEN`` edges (a proxy
# cut-out is subject->intermediary->object = 2 hops; >3 strains the analytic
# "cut-out" reading), and — critically — ``all_simple_paths`` is a GENERATOR, so
# ``itertools.islice(..., _MAX_PROXY_PATHS_SCANNED)`` makes worst-case work
# LINEAR in the scan cap instead of exponential. The cap is total paths CONSUMED
# across all source->target pairs (the enumeration input), distinct from
# ``_MAX_PROXY_CHAINS`` (the scored top-K OUTPUT). When the scan cap bites the
# finding is stamped ``proxy_chains_truncated: true`` (no silent truncation —
# the platform honesty rule).
_MAX_PROXY_PATH_LEN = 3
_MAX_PROXY_CHAINS = 200
# Total simple paths enumerated (consumed from the generators) across the whole
# pairwise walk before the miner stops scanning. 50k keeps a pathological
# 474-degree-hub run to well under a second while still surfacing every genuine
# short cut-out on the real graph (the top-K is re-scored from whatever was
# scanned). Overridable via LEGBA_MAX_PROXY_PATHS for operator tuning.
_MAX_PROXY_PATHS_SCANNED = 50_000
# Belt 3: a wall-clock ceiling (seconds) on the off-loop CPU compute. If mining
# somehow still overruns (a networkx pathology the path cap misses), the run is
# abandoned with a logged warning + an honest partial/empty finding rather than
# hanging a worker thread. 0/negative disables the guard. Overridable via
# LEGBA_GRAPH_MINING_BUDGET_S.
_GRAPH_MINING_BUDGET_S = 25.0
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


def _max_proxy_paths_scanned() -> int:
    """Total-paths-scanned cap for :func:`_proxy_chains` (P1 freeze bound).

    Read at call time (not import) so an operator env tweak takes effect on the
    next run. Falls back to :data:`_MAX_PROXY_PATHS_SCANNED` on any bad value; a
    non-positive value is coerced to the default (the cap is a safety floor —
    disabling it re-opens the exponential blow-up, so we never honor <= 0).
    """
    raw = os.getenv("LEGBA_MAX_PROXY_PATHS", "").strip()
    if not raw:
        return _MAX_PROXY_PATHS_SCANNED
    try:
        val = int(raw)
    except ValueError:
        return _MAX_PROXY_PATHS_SCANNED
    return val if val > 0 else _MAX_PROXY_PATHS_SCANNED


def _graph_mining_budget_s() -> float:
    """Wall-clock ceiling (seconds) for the off-loop CPU compute (belt 3).

    Read at call time. Non-numeric → the default; <= 0 DISABLES the guard (the
    path cap is the primary bound, so an operator may legitimately turn the
    wall-clock belt off).
    """
    raw = os.getenv("LEGBA_GRAPH_MINING_BUDGET_S", "").strip()
    if not raw:
        return _GRAPH_MINING_BUDGET_S
    try:
        return float(raw)
    except ValueError:
        return _GRAPH_MINING_BUDGET_S


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
    max_paths_scanned: int | None = None,
) -> tuple[list[dict[str, Any]], bool]:
    """Find indirect paths whose endpoints have no direct edge.

    Returns ``(chains, truncated)`` where ``chains`` is a list of
    ``{actor, target, via, length, polarity_sign, score}`` dicts (the
    deterministic scored top-``max_chains``) and ``truncated`` is ``True`` iff the
    total-paths-scanned cap bit (i.e. the pairwise ``all_simple_paths`` walk was
    stopped early — the returned top-K is then from a PARTIAL enumeration and the
    caller must stamp the honesty marker). The polarity_sign is the product of
    edge polarities along the path — useful for hostile-via-proxy detection
    (negative product) and laundering-detection (positive via hostile
    intermediate).

    P1 FREEZE BOUND: ``all_simple_paths`` is worst-case exponential and runs
    synchronously; on the dense live graph it froze the event loop for ~33 min.
    ``cutoff=max_path_len`` bounds path length, and ``itertools.islice`` over the
    (lazy generator) enumeration caps total paths CONSUMED at
    ``max_paths_scanned`` (default :func:`_max_proxy_paths_scanned`), making the
    worst case linear in the cap rather than exponential in the graph.
    """
    if max_paths_scanned is None:
        max_paths_scanned = _max_proxy_paths_scanned()
    if g.number_of_nodes() < 3:
        return [], False
    simple = nx.DiGraph()
    edge_polarity: dict[tuple[str, str], int] = {}
    for u, v, d in g.edges(data=True):
        simple.add_edge(u, v)
        # Multi-edges between same pair: combine polarities multiplicatively.
        sign = 1 if int(d.get("polarity", 0) or 0) >= 0 else -1
        edge_polarity[(u, v)] = edge_polarity.get((u, v), 1) * sign

    # DETERMINISM FIX (#99): the old code early-broke after the first
    # ``max_chains`` paths in ``list(g.nodes())`` order — WHICH chains surfaced
    # depended on arbitrary node insertion order. Instead enumerate chains
    # (bounded by the n^2 pair walk + cutoff + the P1 total-scan cap), SCORE
    # each, then return a deterministic top-K. Score rewards a negative
    # sign-product cut-out (the hostile-via-proxy / laundering shape this miner
    # exists to find) and shorter, tighter chains. A stable secondary sort key
    # (the chain tuple) makes ties deterministic regardless of node order.
    #
    # P1: iterate the pairwise cut-out candidates through ONE islice-bounded
    # chain of generators so the total number of enumerated simple paths is
    # capped at ``max_paths_scanned`` regardless of how dense any single hub is.
    # Node order is sorted → the SCAN is deterministic, so a truncated run still
    # returns a stable (if partial) top-K for a fixed graph.
    nodes = sorted(simple.nodes())

    def _candidate_paths() -> Iterable[list[str]]:
        for actor in nodes:
            for target in nodes:
                if actor == target or simple.has_edge(actor, target):
                    continue
                try:
                    yield from nx.all_simple_paths(
                        simple, actor, target, cutoff=max_path_len
                    )
                except nx.NetworkXError:  # pragma: no cover
                    continue

    scored: list[tuple[float, tuple[str, ...], dict[str, Any]]] = []
    scanned = 0
    truncated = False
    for path in _candidate_paths():
        if scanned >= max_paths_scanned:
            truncated = True
            break
        scanned += 1
        if len(path) < 3:  # must have at least one intermediary
            continue
        # Endpoints come from the path itself (the walk only emits actor->target
        # pairs with no direct edge, so path[0]/path[-1] ARE the cut-out ends).
        actor, target = path[0], path[-1]
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
    if truncated:
        logger.warning(
            "graph_mining.proxy_chains.truncated scanned=%d cap=%d nodes=%d "
            "edges=%d — top-K is from a PARTIAL enumeration",
            scanned, max_paths_scanned, simple.number_of_nodes(),
            simple.number_of_edges(),
        )
    # Deterministic: score desc, then the full path tuple asc as the tiebreak.
    scored.sort(key=lambda t: (-t[0], t[1]))
    return [c for _, _, c in scored[:max_chains]], truncated


# ---------------------------------------------------------------------------
# Off-loop CPU bundle (P1 event-loop freeze fix)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _CpuMiningResult:
    """Plain container for the three pure-CPU mining products.

    Deliberately NOT a coroutine / asyncpg-touching object — an instance of this
    is the whole output of the off-loop worker, so nothing actor-/pool-bound
    crosses the thread boundary. A ``@dataclass`` gives a free ``__eq__`` so the
    off-loop result can be compared directly against an inline reference.
    """

    communities: list[list[str]]
    modularity: float | None
    centrality: dict[str, dict[str, float]]
    proxy_chains: list[dict[str, Any]]
    proxy_chains_truncated: bool
    # True only on the wall-clock-budget abandon path: the empty products above
    # are an ABANDONED run, not a genuinely empty graph. Kept distinct so the
    # finding can flag it as prominently as truncation (the honesty rule).
    mining_abandoned: bool = False


def _mine_graph_cpu(g: "nx.MultiDiGraph") -> _CpuMiningResult:
    """Run the three CPU-heavy mining phases over an IN-MEMORY graph.

    P1 FREEZE FIX: this is the pure-compute core that ``handle`` runs OFF the
    asyncio event loop (``asyncio.to_thread``). It touches ONLY the passed
    networkx graph — no asyncpg connection, no Dapr actor state, no ``deps`` —
    so it is safe to run on a worker thread while the loop keeps servicing
    healthchecks + the reminder dispatch. ``handle`` does every DB read BEFORE
    calling this (nexus/AGE augmentation) and every DB write AFTER (reify /
    recent-hostile / graph_metrics), so the seam is clean.

    The two heaviest phases are ``_centrality`` (k-sampled betweenness) and
    ``_proxy_chains`` (bounded ``all_simple_paths``); community detection is
    comparatively cheap. Bundling all three keeps the thread hand-off to ONE
    round trip per run.
    """
    communities, modularity = _detect_communities(g)
    centrality = _centrality(g)
    proxy_chains, proxy_truncated = _proxy_chains(g)
    return _CpuMiningResult(
        communities=communities,
        modularity=modularity,
        centrality=centrality,
        proxy_chains=proxy_chains,
        proxy_chains_truncated=proxy_truncated,
    )


async def _run_mining_offloop(
    g: "nx.MultiDiGraph",
    *,
    budget_s: float | None = None,
) -> tuple[_CpuMiningResult, list[str]]:
    """Execute :func:`_mine_graph_cpu` on a worker thread with a wall-clock belt.

    Returns ``(result, warnings)``. On a clean run ``warnings`` is empty. If the
    compute exceeds ``budget_s`` (default :func:`_graph_mining_budget_s`; <= 0
    disables the belt) the run is ABANDONED and an EMPTY :class:`_CpuMiningResult`
    is returned with a ``graph_mining.cpu_budget_exceeded`` warning so the
    finding is honest-empty rather than the whole plane hanging. The primary
    bound is still the path cap inside ``_proxy_chains`` — this belt only catches
    a networkx pathology that the path cap misses.

    Threading note: we drive ``_mine_graph_cpu`` through a single-worker
    ``ThreadPoolExecutor`` so we can ``concurrent.futures`` wait WITH a timeout
    (``asyncio.to_thread`` gives no cancellation). If the timeout fires the
    background thread cannot be force-killed (Python has no thread kill), so the
    executor is left to drain in the background (``shutdown(wait=False)``) — it
    holds the GIL intermittently but the event loop is already unblocked by the
    time we return, and the path cap makes an unbounded spin unreachable in
    practice.
    """
    if budget_s is None:
        budget_s = _graph_mining_budget_s()
    loop = asyncio.get_running_loop()

    if budget_s <= 0:
        # Belt disabled — still OFF-loop (to_thread), just no wall-clock abandon.
        result = await asyncio.to_thread(_mine_graph_cpu, g)
        return result, []

    executor = concurrent.futures.ThreadPoolExecutor(
        max_workers=1, thread_name_prefix="graph_mining_cpu"
    )
    fut = loop.run_in_executor(executor, _mine_graph_cpu, g)
    try:
        result = await asyncio.wait_for(fut, timeout=budget_s)
        executor.shutdown(wait=False)
        return result, []
    except (asyncio.TimeoutError, concurrent.futures.TimeoutError):
        # Do NOT wait for the runaway thread — release the loop immediately.
        executor.shutdown(wait=False)
        logger.warning(
            "graph_mining.cpu_budget_exceeded budget_s=%.1f nodes=%d edges=%d "
            "— abandoned mining, emitting honest-empty finding",
            budget_s, g.number_of_nodes(), g.number_of_edges(),
        )
        empty = _CpuMiningResult(
            communities=[], modularity=None, centrality={},
            proxy_chains=[], proxy_chains_truncated=False,
            mining_abandoned=True,
        )
        return empty, [
            f"graph_mining.cpu_budget_exceeded budget_s={budget_s:.1f} "
            f"nodes={g.number_of_nodes()} edges={g.number_of_edges()}"
        ]


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


# ---------------------------------------------------------------------------
# M20 (2026-07-06 mining audit) — hostile-edge vetting.
#
# The ``new_hostile_edge`` interesting-item pulls OPEN negative-polarity nexuses.
# The live audit found three false-signal shapes surfacing as TOP findings:
#   1. a NEUTRAL rel_type carrying polarity=-1 relabeled "hostile tie"
#      ("Iran -[conducted via]-> Mohammad Baqer", "Trump -[involved in]-> Italy");
#   2. FRAGMENT / vague endpoints ("West", "Leader", "Parl");
#   3. subject-attribution collapse — a protest-AT-a-location edge emitted as
#      "<state> hostile to <person>" ("Australia -[hostile to]-> Isaac Herzog",
#      from "Hundreds protest Israeli president's visit … Australia's parliament").
# Because this is an UNVERIFIED structural analyst (confidence 1.0, no verify
# pass), false content inherits max confidence. These guards are CONSERVATIVE —
# a real interstate hostile edge (Russia/Ukraine, Israel/Iran, Pakistan/
# Afghanistan, US/Hezbollah) survives every one.
# ---------------------------------------------------------------------------

#: A real actor class (entity_profiles / canon). A bare generic-``entity``-class
#: endpoint with NO real-actor class is a fragment ("Parl", "Fed") — not vetted.
_ACTOR_CLASSES: frozenset[str] = frozenset(
    {COUNTRY_CLASS, ORGANIZATION_CLASS, LOCATION_CLASS, "person"}
)

_STATE_CLASSES: frozenset[str] = frozenset({COUNTRY_CLASS, LOCATION_CLASS})


def _norm_rel(rel: Any) -> str:
    """Fold a rel_type to a case/space/punct-insensitive key so the stored
    lowercase-spaced form ("hostile to") and the CamelCase vocabulary form
    ("HostileTo") compare equal."""
    return re.sub(r"[^a-z0-9]+", "", str(rel or "").lower())


#: The ONLY rel_types that make an edge a genuine hostile tie — the negative
#: POLARITY labels (:data:`_NEGATIVE_LABELS`) plus the observed stored form "in
#: active conflict with". A polarity=-1 nexus whose rel_type is NOT one of these
#: (a mis-signed "conducted via" / "involved in" / "operates in") is NOT emitted
#: as a hostile tie.
_HOSTILE_REL_KEYS: frozenset[str] = frozenset(
    _norm_rel(x) for x in _NEGATIVE_LABELS
) | {_norm_rel("in active conflict with")}


def _is_hostile_rel(rel: Any) -> bool:
    """True only for a genuine hostility rel_type (see :data:`_HOSTILE_REL_KEYS`)."""
    return _norm_rel(rel) in _HOSTILE_REL_KEYS


#: M20 F2 — EXPLICIT-TARGETING hostility rel_types. Unlike the GENERIC
#: co-occurrence label "hostile to" (where the protest-AT-a-location collapse
#: mints "<state> hostile to <person>"), these name a state ACTING ON a specific
#: NAMED person — an assassination / decapitation / arming indicator ("<state>
#: Targets <named person>", "<state> SuppliesWeaponsTo <named person>"). That is
#: a genuine state->person I&W signal, so the subject-attribution guard (c) is
#: scoped to NOT fire for these. Add any future explicit-targeting /
#: assassination-class label here.
_EXPLICIT_TARGETING_REL_KEYS: frozenset[str] = frozenset(
    {_norm_rel("Targets"), _norm_rel("SuppliesWeaponsTo")}
)


def _is_explicit_targeting_rel(rel: Any) -> bool:
    """True for an explicit-targeting rel_type (see :data:`_EXPLICIT_TARGETING_REL_KEYS`)."""
    return _norm_rel(rel) in _EXPLICIT_TARGETING_REL_KEYS


def _canon_class(name: str) -> str:
    """Canon's class for a surface (country / location / organization / entity).
    The canon never returns ``person`` (it does not do person detection); person
    typing comes from the entity_profiles class set instead."""
    _canon, cls = canonicalize_entity(str(name or ""), "entity")
    return cls


def _class_set(raw: Any) -> set[str] | None:
    """Coerce a fetched ``entity_profiles.entity_class`` array to a set. ``None``
    (endpoint ABSENT from entity_profiles) is preserved as ``None`` so vetting can
    give an absent-but-canon-typed endpoint the benefit of the doubt."""
    if raw is None:
        return None
    return {str(c) for c in raw if c}


def _is_canonical_actor(name: str, ep_classes: set[str] | None) -> bool:
    """True when an endpoint is a plausible actor (i.e. NOT dropped by the
    class-vet). The genuine fragment drop is :func:`is_junk_entity` — applied
    SEPARATELY upstream — so this vet must not ALSO drop a REAL actor merely
    because the live store mis-typed it.

    Canon typing (country / location / organization) is authoritative. Otherwise:
      * an endpoint ABSENT from entity_profiles (``ep_classes is None``) is kept
        (benefit of the doubt);
      * an endpoint PROFILED only as the generic ``entity`` class is treated the
        SAME as absent and kept — real actors are routinely mis-typed ``{entity}``
        in the live store (Hamas / IRGC / ISIS / Wagner / Lavrov are ALL bare
        ``{entity}``; dropping them here silently discarded live hostile edges
        like ``Lavrov -[hostile to]-> United States``). A bare fragment ("Parl",
        "Fed", "West", "Leader") is caught by :func:`is_junk_entity` upstream, not
        here;
      * a real actor class in the profile set keeps it.
    """
    if _canon_class(name) in _ACTOR_CLASSES:
        return True
    if ep_classes is None:
        return True
    if ep_classes & _ACTOR_CLASSES:
        return True
    # Profiled ONLY as the generic 'entity' class → treat the SAME as absent
    # (kept). is_junk_entity already removed the genuine fragments upstream.
    return ep_classes.issubset({DEFAULT_CLASS})


def _is_state_surface(name: str, ep_classes: set[str] | None) -> bool:
    """True when the surface names a STATE / place (country or location)."""
    if _canon_class(name) in _STATE_CLASSES:
        return True
    return bool(ep_classes and (ep_classes & _STATE_CLASSES))


def _is_person_only(ep_classes: set[str] | None) -> bool:
    """True when the surface is a bare PERSON — entity_profiles rows exist and are
    ALL ``person`` (no competing country/org/location actor). Used only to gate
    the state->person attribution collapse; an ambiguous surface with any
    non-person actor class (e.g. "Hezbollah" = entity+person) is NOT person-only,
    so "United States -[hostile to]-> Hezbollah" survives."""
    return bool(ep_classes) and ep_classes.issubset({"person"})


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
            # M20: also fetch the nexus confidence (feeds the per-edge quality
            # score) + the DISTINCT entity_profiles class set for each endpoint
            # (feeds the fragment / state->person attribution guards in
            # _build_interesting). A NULL class array = the surface is absent from
            # entity_profiles (benefit-of-the-doubt on vetting).
            rows = await conn.fetch(
                """
                SELECT n.subject, n.object, n.polarity, n.rel_type,
                       n.valid_from, n.confidence,
                       (SELECT array_agg(DISTINCT ep.entity_class)
                          FROM entity_profiles ep
                         WHERE lower(ep.canonical_name) = lower(n.subject))
                         AS subject_classes,
                       (SELECT array_agg(DISTINCT ep.entity_class)
                          FROM entity_profiles ep
                         WHERE lower(ep.canonical_name) = lower(n.object))
                         AS object_classes
                  FROM nexuses n
                 WHERE n.valid_until IS NULL AND n.superseded_by IS NULL
                   AND n.polarity < 0
                   AND n.valid_from IS NOT NULL
                   -- #99: exclude our own inferred reifications from re-discovery.
                   AND COALESCE(n.source_type, '') <> 'inferred'
                 ORDER BY n.valid_from DESC
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
            "confidence": r["confidence"],
            "subject_classes": list(r["subject_classes"])
            if r["subject_classes"] is not None else None,
            "object_classes": list(r["object_classes"])
            if r["object_classes"] is not None else None,
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
        # M20: a fragment / vague token ("West", "Leader", "Parl") is not a
        # meaningful structural broker — drop it from the shortlist.
        if is_junk_entity(str(node)):
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
        subj, obj = str(r.get("subject") or ""), str(r.get("object") or "")
        rel = r.get("rel_type") or ""
        if not subj or not obj:
            continue
        # M20 (b): require an ACTUAL hostility rel_type. polarity<0 was already
        # filtered upstream, but a mis-signed NEUTRAL rel_type (a polarity=-1
        # "conducted via" / "involved in" / "operates in") is NOT a hostile tie
        # and must not be relabeled as one.
        if not _is_hostile_rel(rel):
            continue
        # M20 (a): drop a vague / fragment endpoint ("West", "Leader", "Parl",
        # "Fed", …) and a canonicalize-to-self self-loop before it becomes
        # headline signal. is_junk_entity is the SINGLE fragment authority (F1).
        if is_junk_entity(subj) or is_junk_entity(obj) or same_referent(subj, obj):
            continue
        subj_cls = _class_set(r.get("subject_classes"))
        obj_cls = _class_set(r.get("object_classes"))
        # M20 (a) + F1: keep endpoints typed by canon OR profiled as a real actor.
        # A profiled-only-generic-'entity' endpoint is treated the SAME as absent
        # (kept) — the genuine fragments were already dropped by is_junk_entity
        # above, so this vet must NOT also discard a real actor the live store
        # merely mis-typed {entity} (Hamas / IRGC / Lavrov).
        if not _is_canonical_actor(subj, subj_cls) or not _is_canonical_actor(obj, obj_cls):
            continue
        # M20 (c) + F2: subject-attribution guard — under a GENERIC co-occurrence
        # hostility label ("hostile to" / "in active conflict with") a STATE /
        # place is not meaningfully hostile to a bare foreign PERSON; that shape is
        # the "protesters in X" / leader-visit-at-location collapse ("Australia
        # hostile to Isaac Herzog"). But an EXPLICIT-TARGETING rel_type (a state
        # that literally Targets / SuppliesWeaponsTo a NAMED person) is a genuine
        # assassination / decapitation / arming I&W signal and MUST survive — so
        # the guard is scoped to NOT fire for those. State->state / state->org /
        # person->* hostility all survive either way.
        if (
            not _is_explicit_targeting_rel(rel)
            and _is_state_surface(subj, subj_cls)
            and _is_person_only(obj_cls)
        ):
            continue
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
        # M20 (d): fold the backing nexus confidence into the per-edge quality
        # score so a thinly-corroborated hostile edge ranks below a solid one.
        conf = r.get("confidence")
        conf_factor = 1.0 if conf is None else max(0.0, min(1.0, float(conf)))
        score = recency * float(pol_mag) * conf_factor
        if score > 1.0:
            score = 1.0
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
        # M20 (a): a chain touching a vague / fragment token ("West", "Leader")
        # at ANY position is not a trustworthy cut-out — drop it.
        if (
            is_junk_entity(actor) or is_junk_entity(target)
            or any(is_junk_entity(v) for v in via)
        ):
            continue
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
    proxy_chains_truncated: bool = False,
    mining_abandoned: bool = False,
) -> FindingPayload:
    """Pack handler outputs into the typed FindingPayload."""
    title = (
        f"Graph mining over {node_count} nodes / {edge_count} edges"
        f"{' for ' + target_id if target_id else ''}"
        f"{' — ABANDONED (CPU budget)' if mining_abandoned else ''}"
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
    if mining_abandoned:
        # Honesty: the empty result is an ABANDONED run over a real graph, NOT an
        # empty graph — say so as prominently as truncation.
        body_lines.append(
            "mining ABANDONED — CPU wall-clock budget exceeded; the empty "
            "communities/centrality/proxy_chains are NOT a real empty graph"
        )
    if proxy_chains_truncated:
        body_lines.append(
            "proxy_chains TRUNCATED — top-K from a partial enumeration "
            f"(scan cap {_max_proxy_paths_scanned()})"
        )
    tags = ["deterministic", "graph_mining"]
    if proxy_chains:
        tags.append("proxy_chains_present")
    if proxy_chains_truncated:
        tags.append("proxy_chains_truncated")
    if mining_abandoned:
        tags.append("mining_abandoned")
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
            # P1: honest truncation marker — True iff the proxy-chain path-scan
            # cap bit and the returned top-K is from a PARTIAL enumeration.
            "proxy_chains_truncated": bool(proxy_chains_truncated),
            # P1: honest abandon marker — True iff the CPU wall-clock budget bit
            # and the empty products are an abandoned run, not an empty graph.
            "mining_abandoned": bool(mining_abandoned),
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

    # P1 EVENT-LOOP FREEZE FIX (2026-07-24): the three CPU-heavy mining phases
    # (community detection, k-sampled betweenness, and — the caught culprit —
    # bounded ``all_simple_paths`` proxy-chain enumeration) run OFF the asyncio
    # event loop on a worker thread, so even the internally-bounded work cannot
    # block the reminder dispatch / healthchecks on the MAIN loop. All DB reads
    # (nexus/AGE augmentation) already happened ABOVE on-loop; all DB writes
    # (reify / recent-hostile / graph_metrics) happen BELOW on-loop; the off-loop
    # function touches ONLY the in-memory graph. A wall-clock belt inside
    # ``_run_mining_offloop`` abandons a pathological run with an honest-empty
    # finding rather than hanging a worker thread.
    cpu, cpu_warnings = await _run_mining_offloop(g)
    warnings.extend(cpu_warnings)
    communities = cpu.communities
    modularity = cpu.modularity
    centrality = cpu.centrality
    proxy_chains = cpu.proxy_chains
    if cpu.proxy_chains_truncated:
        # No silent truncation (platform honesty rule): the proxy-chain top-K is
        # from a PARTIAL enumeration because the path-scan cap bit.
        warnings.append(
            f"graph_mining.proxy_chains_truncated cap={_max_proxy_paths_scanned()}"
        )

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
            "proxy_chains_truncated": bool(cpu.proxy_chains_truncated),
            "mining_abandoned": bool(cpu.mining_abandoned),
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
        proxy_chains_truncated=cpu.proxy_chains_truncated,
        mining_abandoned=cpu.mining_abandoned,
    )
    return AnalystMethodResult(
        finding=finding,
        usage={"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0},
    )


__all__ = [
    "handle",
    "build_shortest_path_cypher",
    "build_path_neighbourhood_cypher",
    "shortest_path_with_broker",
]
