# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Fuzzy value clustering — group "same-claim" fact VALUES (Holes-B Wave 3).

The shared `(subject, predicate)` value-grouping primitive. Real sources phrase
the SAME claim differently ("Russian" vs "Russia", "Kyiv" vs "Kiev",
"de-escalating" vs "de escalating"), so EXACT-string grouping is dormant: it
finds 0 same-triple groups across the live corpus. This helper does the
two-stage normalize-then-cluster that BOTH the Holes-B contested-claims arbiter
(`fact_contention_arbiter`) AND the Holes-A noisy-OR corroboration leg need:

  1. **Canonicalize** each raw value via :func:`canonicalize_entity` — the shared
     canon that already collapses national demonyms ("Russian" -> "Russia"),
     country aliases ("USA" -> "United States"), and types places/orgs. Then a
     SMALL local spelling-variant alias map folds high-frequency transliteration
     pairs that the country-focused shared canon does NOT carry (Kyiv/Kiev,
     Beijing/Peking, ...). The local map is deliberately tiny + value-only: it
     never touches the shared write-path canon, so it triggers NO registry
     rebuild.
  2. **Cluster** the canonical forms by normalized-Levenshtein distance
     (:func:`_normalized_levenshtein`, distance in ``[0, 1]``) under a TIGHT
     threshold so a typo / spacing variant merges but two genuinely different
     claims do NOT.

Threshold (:data:`FUZZY_MERGE_MAX_DISTANCE` = ``0.12``)
------------------------------------------------------
Chosen to sit BELOW the closest adversarial NON-merge pair and ABOVE the typo
band, measured on the live canon:

    "North Korea" vs "South Korea"   normlev 0.182   MUST NOT merge
    "de-escalating" vs "de escalating" normlev 0.077  SHOULD merge (spacing)
    "ceasefire" vs "cease-fire"      normlev ~0.111   SHOULD merge (hyphen)
    "Russian" vs "Russia"            normlev 0.000   (canon collapses first)
    "Kyiv" vs "Kiev"                 normlev 0.000   (alias-folded first)

``0.12`` clears the 0.077/0.111 typo band yet stays well under the 0.182
North/South Korea floor (a 34% margin). Country/city pairs that differ by a
single leading word ("North X"/"South X", "East X"/"West X") are the tightest
real-world false-merge risk and they all land at/above 0.18, so the constant is
conservative by construction. Raising it toward 0.18 would start merging
opposite directions; lowering it below 0.077 would split spacing variants.

This is the cheap canon+Levenshtein tier (DECIDED 2026-06-29). An
embedding/cosine semantic tier ("clashes ongoing" vs "fighting continues") is a
flagged follow-up — out of scope here.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .._entity_canon import DEFAULT_CLASS, canonicalize_entity
from ..filters.dedupe import _normalized_levenshtein

#: Tight normalized-Levenshtein DISTANCE ceiling for a same-claim merge. Two
#: canonical values merge iff their normalized distance is ``<=`` this. See the
#: module docstring for the adversarial-pair justification.
FUZZY_MERGE_MAX_DISTANCE: float = 0.12

#: Local spelling / transliteration variants the country-focused shared canon
#: does NOT fold (it carries country aliases + national demonyms, but not city
#: transliterations). Lower-cased surface -> canonical surface. Deliberately
#: tiny + value-scoped — this map never touches the shared write-path canon, so
#: it forces no registry rebuild. Extend conservatively (high-frequency,
#: unambiguous spelling pairs only).
_VALUE_ALIAS_MAP: dict[str, str] = {
    "kiev": "Kyiv",
    "kyiv": "Kyiv",
    "peking": "Beijing",
    "beijing": "Beijing",
    "bombay": "Mumbai",
    "mumbai": "Mumbai",
    "calcutta": "Kolkata",
    "kolkata": "Kolkata",
    "saigon": "Ho Chi Minh City",
    "rangoon": "Yangon",
    "yangon": "Yangon",
}


def canonical_value_key(value: str) -> str:
    """Normalize one raw fact VALUE to its clustering key.

    Pure + deterministic + idempotent. Runs the shared :func:`canonicalize_entity`
    (demonym/alias/typing collapse) then the local spelling-variant fold, and
    lower-cases the result. An empty / whitespace-only value returns ``""`` (the
    caller treats it as its own degenerate group — it never merges with a real
    value because :func:`_normalized_levenshtein` of ``""`` vs anything is 1.0).
    """
    raw = " ".join(str(value or "").split()).strip()
    if not raw:
        return ""
    canon, _ = canonicalize_entity(raw, DEFAULT_CLASS)
    canon = canon or raw
    folded = _VALUE_ALIAS_MAP.get(canon.lower(), canon)
    return folded.lower()


@dataclass
class ValueCluster:
    """One "same-claim" group of fact values.

    ``key`` is the canonical key of the FIRST (representative) member — a stable,
    reproducible label for the cluster. ``members`` are the original input indices
    (positions in the list passed to :func:`cluster_values`) so the caller can map
    a cluster back to its fact rows. ``canonical_keys`` are the per-member
    canonical keys (one per member, same order).
    """

    key: str
    members: list[int] = field(default_factory=list)
    canonical_keys: list[str] = field(default_factory=list)


def cluster_values(values: list[str]) -> list[ValueCluster]:
    """Cluster a list of raw fact VALUES into same-claim groups.

    Two-stage (canon-fold then tight Levenshtein). Greedy single-link assignment
    against each existing cluster's REPRESENTATIVE (first) canonical key:
      * a value whose canonical key is IDENTICAL to a representative merges
        (distance 0), and
      * a value within :data:`FUZZY_MERGE_MAX_DISTANCE` of a representative merges,
    otherwise it opens a new cluster. Greedy-by-representative (not full
    average-link) is intentional: it is deterministic, order-stable for the
    dedup-y inputs this sees, and cannot chain two far-apart values together via
    a midpoint (each member is within the threshold of its cluster's anchor).

    Deterministic for a fixed input order. Returns clusters in first-seen order;
    ``members`` carry the input indices so the caller maps a cluster to its rows.
    """
    clusters: list[ValueCluster] = []
    for idx, raw in enumerate(values):
        ckey = canonical_value_key(raw)
        placed = False
        for cluster in clusters:
            rep = cluster.canonical_keys[0]
            if ckey == rep or _normalized_levenshtein(ckey, rep) <= FUZZY_MERGE_MAX_DISTANCE:
                cluster.members.append(idx)
                cluster.canonical_keys.append(ckey)
                placed = True
                break
        if not placed:
            clusters.append(
                ValueCluster(key=ckey, members=[idx], canonical_keys=[ckey])
            )
    return clusters


__all__ = [
    "FUZZY_MERGE_MAX_DISTANCE",
    "ValueCluster",
    "canonical_value_key",
    "cluster_values",
]
