# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``structural_balance`` sub-handler — L-006 sub-split A (signed graph slice).

Signed-edge structural-balance metrics on the entity-relationship graph.

Theory recap. A signed undirected triad (A, B, C) with edge signs
σ_AB, σ_BC, σ_AC is **balanced** if the product σ_AB · σ_BC · σ_AC = +1,
**unbalanced** otherwise. Heider's classic statement: "friend of my
friend is my friend, enemy of my enemy is my friend." Unbalanced triads
predict realignment — analytically high-value.

This handler:

  1. Builds a signed undirected graph from input rows (and, optionally,
     additional AGE edges).
  2. Enumerates all triads, classifies each as balanced / unbalanced /
     incomplete (one or more neutral/zero edges — excluded from the
     balance ratio).
  3. Computes the **balance ratio** = balanced / (balanced + unbalanced).
  4. Reports per-node "frustration" — count of unbalanced triads each
     node participates in.

The neutral-edge handling matches the legacy ``shared/structural_balance``
contract: ``intent="dual_use"`` / ``polarity=0`` excludes a triad from
the ratio rather than counting as a stable side.

Output ``data`` keys:
    balance_ratio       float — None if no signed triads
    balanced_count      int
    unbalanced_count    int
    incomplete_count    int
    unbalanced_triads   [{a, b, c, signs: {ab, bc, ac}}] (capped)
    frustration         {node_id: unbalanced_count}
    edge_count          int (signed edges considered)
    node_count          int
"""

from __future__ import annotations

import logging
from itertools import combinations
from typing import Any, Mapping

import networkx as nx

from ...provenance.models import FindingPayload
from ....runtime.analyst_method import AnalystMethodResult
from ._graph_metrics_sink import write_graph_metric

logger = logging.getLogger(__name__)

# Canonical edge-label → polarity table. Kept in this module (the
# graph_mining handler has a smaller copy for proxy-chain sign products;
# this is the authoritative one). Anything not listed is treated as 0
# (neutral, excluded from balance).
POLARITY: dict[str, int] = {
    # Negative — antagonistic.
    "HostileTo": -1,
    "Targets": -1,
    "SuppliesWeaponsTo": -1,
    # Positive — supportive / allied.
    "AlliedWith": +1,
    "MemberOf": +1,
    "LeaderOf": +1,
    "AffiliatedWith": +1,
    "PartOf": +1,
    # Neutral — excluded.
    "LocatedIn": 0,
    "PartyTo": 0,
    "OperatesIn": 0,
    "CoOccursWith": 0,
    "InvolvedIn": 0,
    "ConductedVia": 0,
}

_MAX_UNBALANCED_REPORTED = 200
_MAX_NODES = 1_500  # triadic enumeration is O(n^3); cap aggressively.
_MAX_INTERESTING = 12  # shared "interesting" shortlist cap (#99 contract).


# ---------------------------------------------------------------------------
# D14 — POLARITY derived DETERMINISTICALLY from intent / rel_type.
#
# The live review (PLATFORM_HEALTH_RESULTS D14) found nexuses whose `polarity`
# and `intent` DISAGREED ("Spain hostile to Saudi Arabia" carried polarity=-1
# but intent=supportive; co-occurrence sports fixtures were signed -1 "hostile
# to"). The fix per the W2 contract: polarity is a PURE FUNCTION of (intent,
# rel_type) computed at the producer, so the two can never contradict on write.
#
# Priority — intent first (it is the producer's explicit semantic claim), then
# the rel_type POLARITY table, then 0. A supportive intent can NEVER come out
# negative; a hostile/conflict intent can NEVER come out positive.
# ---------------------------------------------------------------------------

#: Intent string → canonical sign. The keys are the closed _VALID_INTENTS set
#: the reifier coerces to (supportive / hostile / dual-use / neutral) PLUS the
#: structural_balance legacy "dual_use" spelling and a few conflict synonyms a
#: producer might surface. Anything unmapped falls through to the rel_type table.
INTENT_POLARITY: dict[str, int] = {
    "supportive": 1,
    "allied": 1,
    "cooperative": 1,
    "hostile": -1,
    "antagonistic": -1,
    "conflict": -1,
    "adversarial": -1,
    "neutral": 0,
    "dual-use": 0,
    "dual_use": 0,
    "structural": 0,
}


def polarity_from(intent: Any, rel_type: Any) -> int:
    """Resolve the canonical polarity sign DETERMINISTICALLY (D14).

    Pure. ``intent`` is the producer's explicit semantic claim and wins when it
    is one of the known intents (so polarity can never disagree with intent);
    otherwise the rel_type :data:`POLARITY` table decides; otherwise 0 (neutral).

    This is the single source of truth both the reifier (producer) and this
    handler (consumer) sign through, so a relationship's sign is a function of
    its declared intent / type — never a free LLM integer that can contradict
    the words next to it.
    """
    key = str(intent or "").strip().lower()
    if key in INTENT_POLARITY:
        return INTENT_POLARITY[key]
    table = POLARITY.get(str(rel_type or "").strip(), 0)
    if table > 0:
        return 1
    if table < 0:
        return -1
    return 0


# ---------------------------------------------------------------------------
# Interesting-shortlist distillation (#99 shared contract)
# ---------------------------------------------------------------------------


def _build_interesting(metrics: dict[str, Any]) -> list[dict[str, Any]]:
    """Distil the raw triad enumeration into a scored ``interesting`` shortlist.

    Two item kinds per the #99 contract:

      * ``tense_actor`` — the nodes caught in the most unbalanced triads
        (frustration), score = node frustration / max frustration (0..1).
      * ``sign_imbalanced_triad`` — an unbalanced signed triad (A-B-C where
        the product of the three signs is negative). Score by how "lopsided"
        the imbalance is: a +,+,- triad (two allies pulled apart by one
        hostility — classic realignment pressure) scores highest; a -,-,-
        triad (three mutual hostilities) scores lowest among unbalanced.

    Returns at most :data:`_MAX_INTERESTING` items, sorted by score desc.
    """
    items: list[dict[str, Any]] = []

    frustration: dict[str, int] = metrics.get("frustration") or {}
    if frustration:
        max_frust = max(frustration.values()) or 1
        for node, count in frustration.items():
            score = float(count) / float(max_frust)
            items.append({
                "kind": "tense_actor",
                "label": str(node),
                "score": round(score, 4),
                "rationale": (
                    f"caught in {int(count)} sign-imbalanced triad(s) — the most "
                    "structurally conflicted ties in the graph"
                ),
                "entities": [str(node)],
            })

    for tri in metrics.get("unbalanced_triads") or []:
        signs = tri.get("signs") or {}
        sab = int(signs.get("ab", 0))
        sbc = int(signs.get("bc", 0))
        sac = int(signs.get("ac", 0))
        # Count negatives among the three signed edges. An unbalanced triad has
        # an odd number of negatives (1 or 3). One negative = two allies split
        # by a single hostility (highest realignment pressure → score 1.0);
        # three negatives = all-mutual-hostility (lowest → ~0.4).
        neg = sum(1 for s in (sab, sbc, sac) if s < 0)
        score = 1.0 if neg == 1 else 0.4
        a, b, c = tri.get("a"), tri.get("b"), tri.get("c")
        items.append({
            "kind": "sign_imbalanced_triad",
            "label": f"{a} - {b} - {c}",
            "score": round(float(score), 4),
            "rationale": (
                "unbalanced signed triad (sign product negative — "
                f"{neg} hostile edge(s)); Heider-unstable, predicts realignment"
            ),
            "entities": [str(x) for x in (a, b, c) if x],
        })

    items.sort(key=lambda it: it["score"], reverse=True)
    return items[:_MAX_INTERESTING]


# ---------------------------------------------------------------------------
# Graph construction
# ---------------------------------------------------------------------------


def _polarity_for(row: Mapping[str, Any]) -> int:
    """Pick a sign for an input row.

    Order of preference:
      1. Explicit ``polarity`` (int, may be -1/0/+1 or any int — coerced).
      2. ``edge_label`` looked up in :data:`POLARITY`.
      3. Zero (neutral).
    """
    if "polarity" in row and row["polarity"] is not None:
        try:
            v = int(row["polarity"])
            if v > 0:
                return 1
            if v < 0:
                return -1
            return 0
        except (TypeError, ValueError):
            pass
    label = row.get("edge_label") or row.get("relationship_type") or ""
    return POLARITY.get(str(label), 0)


def _signed_graph_from_inputs(inputs: list[dict[str, Any]]) -> nx.Graph:
    """Build an undirected signed graph. Multiple edges between the same
    pair are reduced by summing signs and re-projecting to {-1, 0, +1}.
    """
    aggregated: dict[tuple[str, str], int] = {}
    for row in inputs:
        src = row.get("source_entity") or row.get("src") or row.get("subject")
        tgt = row.get("target_entity") or row.get("dst") or row.get("object")
        if not src or not tgt or src == tgt:
            continue
        key = tuple(sorted((str(src), str(tgt))))
        sign = _polarity_for(row)
        aggregated[key] = aggregated.get(key, 0) + sign
    g: nx.Graph = nx.Graph()
    for (u, v), s in aggregated.items():
        # Re-project sum to sign.
        proj = 1 if s > 0 else (-1 if s < 0 else 0)
        g.add_edge(u, v, sign=proj)
    return g


# ---------------------------------------------------------------------------
# Triadic classification
# ---------------------------------------------------------------------------


def _enumerate_triads(
    g: nx.Graph,
    *,
    max_unbalanced: int,
) -> dict[str, Any]:
    """Walk every triangle in g, classify, accumulate metrics."""
    balanced = 0
    unbalanced = 0
    incomplete = 0
    unbalanced_list: list[dict[str, Any]] = []
    frustration: dict[str, int] = {}

    # Use networkx triangles() to get a count of triangles per node, then
    # iterate distinct triangles via the standard pattern.
    nodes = list(g.nodes())
    nodes.sort()  # stable iteration order

    # Map for O(1) sign lookup.
    sign_of: dict[tuple[str, str], int] = {}
    for u, v, d in g.edges(data=True):
        s = int(d.get("sign", 0))
        sign_of[tuple(sorted((u, v)))] = s

    # Adjacency view for triangle enumeration.
    neighbors: dict[str, set[str]] = {n: set(g.neighbors(n)) for n in nodes}

    seen_triads: set[tuple[str, str, str]] = set()
    for a in nodes:
        for b in neighbors[a]:
            if b <= a:
                continue
            common = neighbors[a] & neighbors[b]
            for c in common:
                if c <= b:
                    continue
                triad = (a, b, c)
                if triad in seen_triads:
                    continue
                seen_triads.add(triad)
                sab = sign_of.get(tuple(sorted((a, b))), 0)
                sbc = sign_of.get(tuple(sorted((b, c))), 0)
                sac = sign_of.get(tuple(sorted((a, c))), 0)
                if 0 in (sab, sbc, sac):
                    incomplete += 1
                    continue
                product = sab * sbc * sac
                if product > 0:
                    balanced += 1
                else:
                    unbalanced += 1
                    if len(unbalanced_list) < max_unbalanced:
                        unbalanced_list.append({
                            "a": a, "b": b, "c": c,
                            "signs": {"ab": sab, "bc": sbc, "ac": sac},
                        })
                    for node in (a, b, c):
                        frustration[node] = frustration.get(node, 0) + 1

    signed_total = balanced + unbalanced
    balance_ratio: float | None = (
        (balanced / signed_total) if signed_total > 0 else None
    )
    # Frustration: stable sort by count desc, node id asc, top 50.
    top_frustration = dict(
        sorted(
            frustration.items(),
            key=lambda kv: (-kv[1], kv[0]),
        )[:50]
    )
    return {
        "balance_ratio": balance_ratio,
        "balanced_count": balanced,
        "unbalanced_count": unbalanced,
        "incomplete_count": incomplete,
        "unbalanced_triads": unbalanced_list,
        "frustration": top_frustration,
    }


# ---------------------------------------------------------------------------
# Live-substrate AGE augmentation (best-effort)
# ---------------------------------------------------------------------------


async def _augment_from_age(
    deps: Any,
    inputs: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Pull signed edges over AGE incident to input entities.

    Returns extra rows in the canonical input shape so they merge cleanly
    upstream. Failures degrade to ``[]``.
    """
    pool = getattr(deps, "pg_pool", None) if deps is not None else None
    if pool is None:
        return []
    seed_entities: set[str] = set()
    for row in inputs:
        for k in ("source_entity", "src", "subject", "target_entity", "dst", "object"):
            v = row.get(k)
            if v:
                seed_entities.add(str(v))
    if not seed_entities:
        return []
    sample = list(seed_entities)[:200]
    quoted = ", ".join(f'"{e}"' for e in sample)
    # Only pull labels we have polarity for.
    label_filter = ", ".join(f"'{lbl}'" for lbl in POLARITY if POLARITY[lbl] != 0)
    cypher = (
        "MATCH (a)-[r]->(b) "
        f"WHERE label(r) IN [{label_filter}] AND "
        f"(a.id IN [{quoted}] OR b.id IN [{quoted}]) "
        "RETURN a.id AS src, b.id AS dst, label(r) AS rel "
        "LIMIT 5000"
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
        logger.warning("structural_balance.age.pull_failed err=%s", exc)
        return []
    extras: list[dict[str, Any]] = []
    for row in rows:
        src = _strip_agtype(row["src"])
        dst = _strip_agtype(row["dst"])
        rel = _strip_agtype(row["rel"])
        if not src or not dst:
            continue
        extras.append({
            "source_entity": src,
            "target_entity": dst,
            "edge_label": rel,
        })
    return extras


async def _augment_from_nexuses(deps: Any) -> list[dict[str, Any]]:
    """Pull OPEN signed nexus rows (PIECE A) as canonical signed-edge inputs.

    The ``relationship_reifier`` writes first-class, typed, SIGNED relationships
    to the ``nexuses`` table. Those are exactly the signed edges this handler
    needs — feeding them in directly (with explicit ``polarity``) is what makes
    the signed-triad balance run over real signed data instead of the untyped
    ``CoOccursWith`` edges the live AGE graph mostly carries (PIECE A light-up).

    Only non-neutral (``polarity <> 0``) open nexuses are pulled — neutral ones
    are excluded from the balance ratio anyway. Failures degrade to ``[]``.
    """
    pool = getattr(deps, "pg_pool", None) if deps is not None else None
    if pool is None:
        return []
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT subject, object, polarity, rel_type
                  FROM nexuses
                 WHERE valid_until IS NULL AND superseded_by IS NULL
                   AND polarity <> 0
                 LIMIT 20000
                """
            )
    except Exception as exc:
        logger.warning("structural_balance.nexus.pull_failed err=%s", exc)
        return []
    out: list[dict[str, Any]] = []
    for r in rows:
        if not r["subject"] or not r["object"]:
            continue
        out.append({
            "source_entity": r["subject"],
            "target_entity": r["object"],
            "polarity": int(r["polarity"]),
            "edge_label": r["rel_type"],
        })
    return out


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


# ---------------------------------------------------------------------------
# Finding assembly
# ---------------------------------------------------------------------------


def _build_finding(
    *,
    metrics: dict[str, Any],
    interesting: list[dict[str, Any]],
    node_count: int,
    edge_count: int,
    warnings: list[str],
    target_id: str | None,
) -> FindingPayload:
    br = metrics["balance_ratio"]
    title = (
        f"Structural balance: ratio={br:.3f} "
        f"({metrics['balanced_count']}b / {metrics['unbalanced_count']}u)"
        if br is not None
        else "Structural balance: no signed triads"
    )
    if target_id:
        title = f"{title} for {target_id}"
    body = (
        f"signed_edges={edge_count} nodes={node_count}\n"
        f"balanced={metrics['balanced_count']} "
        f"unbalanced={metrics['unbalanced_count']} "
        f"incomplete={metrics['incomplete_count']}\n"
    )
    tags = ["deterministic", "structural_balance"]
    if metrics["unbalanced_count"] > 0:
        tags.append("unbalanced_triads_present")
    return FindingPayload(
        title=title[:2048],
        body=body[:65536],
        confidence=1.0,
        evidence=[],
        tags=tags,
        data={
            "sub_handler": "structural_balance",
            "balance_ratio": br,
            "balanced_count": metrics["balanced_count"],
            "unbalanced_count": metrics["unbalanced_count"],
            "incomplete_count": metrics["incomplete_count"],
            "unbalanced_triads": metrics["unbalanced_triads"],
            "frustration": metrics["frustration"],
            "interesting": interesting,
            "edge_count": edge_count,
            "node_count": node_count,
            "warnings": warnings,
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
    """Sub-handler entry point — see module docstring."""
    warnings: list[str] = []
    rows = list(inputs)
    if deps is not None and bool(options.get("augment_from_nexuses", True)):
        # PIECE A: the signed typed nexuses are the primary signed-edge source.
        rows.extend(await _augment_from_nexuses(deps))
    if deps is not None and bool(options.get("augment_from_age", True)):
        rows.extend(await _augment_from_age(deps, inputs))

    g = _signed_graph_from_inputs(rows)
    if g.number_of_nodes() > _MAX_NODES:
        warnings.append(
            f"structural_balance.truncated nodes={g.number_of_nodes()} "
            f"max={_MAX_NODES}"
        )
        keep = list(g.nodes())[:_MAX_NODES]
        g = g.subgraph(keep).copy()

    metrics = _enumerate_triads(g, max_unbalanced=_MAX_UNBALANCED_REPORTED)
    interesting = _build_interesting(metrics)

    node_count = g.number_of_nodes()
    edge_count = g.number_of_edges()

    # FIX P2-1: persist the run's signed-triad balance metrics to the
    # graph_metrics sink (the table had no writer). Best-effort — never fails
    # the run. The frustration map is capped upstream, so the payload is bounded.
    await write_graph_metric(
        deps,
        options,
        metric_kind="structural_balance",
        payload={
            "balance_ratio": metrics["balance_ratio"],
            "balanced_count": metrics["balanced_count"],
            "unbalanced_count": metrics["unbalanced_count"],
            "incomplete_count": metrics["incomplete_count"],
            "frustration": metrics["frustration"],
            "interesting": interesting,
            "node_count": node_count,
            "edge_count": edge_count,
            "target_id": options.get("target_id"),
        },
    )

    finding = _build_finding(
        metrics=metrics,
        interesting=interesting,
        node_count=node_count,
        edge_count=edge_count,
        warnings=warnings,
        target_id=options.get("target_id"),
    )
    return AnalystMethodResult(
        finding=finding,
        usage={"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0},
    )


__all__ = ["POLARITY", "INTENT_POLARITY", "polarity_from", "handle"]
