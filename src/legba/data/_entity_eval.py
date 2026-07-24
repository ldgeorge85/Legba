# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Evaluation metrics for entity resolution (the E4 eval harness).

WHY THIS MODULE EXISTS
----------------------
The entity_researcher (E4) MERGES entity_profiles rows — a permanent, if
reversible, graph mutation. Before trusting its adjudicator we measure it
against a labeled truth set drawn from the known damage (the Khamenei-30 cluster
of MULTIPLE real people, the SNSC 4-way fragmentation, the Resistance family,
the "the X"/"X" article-twins). Two complementary metrics, both standard in the
entity-resolution literature (Splink / OpenSanctions report the same pair):

  * PAIRWISE precision/recall/F1 — over the set of unordered id pairs judged
    "same referent". Precision = did the merges we made belong together; recall
    = did we find the merges that should happen. Directly scores E3+E4's
    candidate + verdict pipeline (a false-positive merge is the dangerous kind,
    so precision is the guardrail; recall the coverage).

  * B-CUBED precision/recall/F1 — element-weighted CLUSTER agreement (Bagga &
    Baldwin). Unlike pairwise, it degrades gracefully as clusters grow and does
    not over-weight one giant cluster, so it is the honest headline for a
    de-fragmentation sweep. For each element: precision = |pred∩gold cluster| /
    |pred cluster|, recall = |pred∩gold cluster| / |gold cluster|; F1 per element
    then averaged.

Both are PURE (sets / dicts in, floats out) — no DB, no LLM, no analyst import —
so E4 (and any regression test) can score a run deterministically. Truth sets
live with the caller (a test fixture, or a curated live snapshot); this module
only scores.
"""

from __future__ import annotations

from dataclasses import dataclass


def _f1(precision: float, recall: float) -> float:
    return 0.0 if (precision + recall) == 0 else 2 * precision * recall / (precision + recall)


@dataclass(frozen=True)
class PairwiseScore:
    precision: float
    recall: float
    f1: float
    tp: int
    fp: int
    fn: int


@dataclass(frozen=True)
class BCubedScore:
    precision: float
    recall: float
    f1: float
    n_elements: int


def _norm_pairs(pairs) -> set[frozenset]:
    """Coerce an iterable of 2-element id pairs to a set of 2-frozensets,
    dropping self-pairs (a == b) which are never a merge."""
    out: set[frozenset] = set()
    for p in pairs:
        it = list(p)
        if len(it) != 2:
            raise ValueError(f"pair must have exactly 2 ids, got {p!r}")
        a, b = str(it[0]), str(it[1])
        if a != b:
            out.add(frozenset((a, b)))
    return out


def pairwise_prf(predicted_same, gold_same) -> PairwiseScore:
    """Pairwise precision/recall/F1 over "same referent" id pairs.

    ``predicted_same`` / ``gold_same`` are iterables of unordered id pairs
    (each a 2-tuple/list/set of ids). Order within a pair is irrelevant;
    duplicates and self-pairs are ignored.
    """
    pred = _norm_pairs(predicted_same)
    gold = _norm_pairs(gold_same)
    tp = len(pred & gold)
    fp = len(pred - gold)
    fn = len(gold - pred)
    precision = 1.0 if (tp + fp) == 0 else tp / (tp + fp)
    recall = 1.0 if (tp + fn) == 0 else tp / (tp + fn)
    return PairwiseScore(precision, recall, _f1(precision, recall), tp, fp, fn)


def clusters_to_pairs(clusters) -> set[frozenset]:
    """Expand a clustering into its set of intra-cluster "same" pairs.

    ``clusters`` maps element id -> cluster label. A singleton contributes no
    pair. Useful to score a full clustering with :func:`pairwise_prf`.
    """
    by_label: dict[str, list[str]] = {}
    for elem, label in clusters.items():
        by_label.setdefault(str(label), []).append(str(elem))
    pairs: set[frozenset] = set()
    for members in by_label.values():
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                pairs.add(frozenset((members[i], members[j])))
    return pairs


def bcubed(predicted_clusters, gold_clusters) -> BCubedScore:
    """B-cubed precision/recall/F1 over two clusterings of the SAME elements.

    Each arg maps element id -> cluster label. Only elements present in BOTH
    are scored (a fair comparison needs the same universe). Precision uses the
    predicted cluster as the denominator, recall the gold cluster; each is
    averaged element-wise, and F1 is the harmonic mean of the two averages
    (the conventional B-cubed aggregation).
    """
    pred = {str(k): str(v) for k, v in predicted_clusters.items()}
    gold = {str(k): str(v) for k, v in gold_clusters.items()}
    elements = [e for e in pred if e in gold]
    n = len(elements)
    if n == 0:
        return BCubedScore(1.0, 1.0, 1.0, 0)

    # Precompute membership lists per label for each clustering.
    pred_members: dict[str, list[str]] = {}
    gold_members: dict[str, list[str]] = {}
    for e in elements:
        pred_members.setdefault(pred[e], []).append(e)
        gold_members.setdefault(gold[e], []).append(e)

    p_sum = 0.0
    r_sum = 0.0
    for e in elements:
        p_cluster = set(pred_members[pred[e]])
        g_cluster = set(gold_members[gold[e]])
        common = len(p_cluster & g_cluster)
        p_sum += common / len(p_cluster)
        r_sum += common / len(g_cluster)
    precision = p_sum / n
    recall = r_sum / n
    return BCubedScore(precision, recall, _f1(precision, recall), n)


__all__ = [
    "PairwiseScore",
    "BCubedScore",
    "pairwise_prf",
    "bcubed",
    "clusters_to_pairs",
]
