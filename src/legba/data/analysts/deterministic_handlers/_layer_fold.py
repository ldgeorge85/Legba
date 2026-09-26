# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Program 6 L2 — the COUNT half of ``layer_divergence``: the row shapes and
the wire fold that turn a day's raw signal rows into a layer's honest count.

Extracted from :mod:`legba.data.analysts.deterministic_handlers
.layer_divergence` along its own author-marked seam: everything here answers
ONE question — *how many distinct dispatches did this layer carry on this day*
— and nothing here knows about baselines, z-scores, apertures or findings. The
divergence module imports these names ONE WAY.

THE FOLD, AND WHY IT IS PLACED WHERE IT IS
-------------------------------------------

Folding happens WITHIN one (target, layer, day) bucket and nowhere else. Two
copies of one dispatch inside one layer are one voice and must count once; the
SAME dispatch appearing in ``domestic_press`` AND in ``foreign_press`` is not a
duplicate at all — it is the narrative-control pair's actual subject, and a
global fold would silently destroy the thing being measured.

Inside a bucket, three rules, all BORROWED rather than re-invented:

  * ``content_hash`` equality — the ingest's own dedupe key.
  * NORMALIZED HEADLINE equality, through
    ``wire_pair_collapse._normalize_headline`` and its ``_MIN_KEY_TOKENS``
    floor. This is exactly ``source_independence.HEADLINE_MAX_DISTANCE`` (0.0 —
    an UNDECLARED pair must normalize to the same string: "two outlets with no
    known relationship writing byte-identical normalized headlines on one story
    is syndication essentially always"), applied as a GROUP KEY rather than
    pairwise, which is the same answer at O(n) instead of O(n²).
  * the DECLARED relaxation — for two sources the wire map calls feeds of one
    publisher (``publisher_of``) or known re-carriers of one wire
    (``shared_wire``), the headline bar loosens to
    ``HEADLINE_MAX_DISTANCE_DECLARED`` (0.15 — a masthead trimming a wire
    headline to its column width: a trim, not a rewrite). Only this rule needs
    the pairwise walk, and it is BOUNDED: rows past ``max_fold_rows`` still
    COUNT, they simply do not fold. That over-counts a layer and can never
    under-count it, which is the direction every guard in
    ``source_independence`` resolves toward.

The union-find is over ITEM INDICES, not outlets, and the difference from
``source_independence._Folds`` is deliberate: that module asks how many
independent VOICES one claim rests on, this one asks how many distinct
DISPATCHES a layer carried. Two outlets that ran one wire story and then each
ran an original piece carried three dispatches, not one.

A LEAF as far as this package goes — it imports the two fold maps and the
headline normalizer, and nothing else from the analyst tree.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Sequence

from ..source_independence import (
    HEADLINE_MAX_DISTANCE_DECLARED,
    _within,
    publisher_of,
    shared_wire,
)
from ..wire_pair_collapse import _MIN_KEY_TOKENS, _normalize_headline


# ---------------------------------------------------------------------------
# Row shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SignalItem:
    """One ingested signal, reduced to what the fold and the citation need."""

    signal_id: str
    source_id: str
    ts: datetime
    content_hash: str = ""
    title: str = ""

    @property
    def day(self) -> str:
        return self.ts.astimezone(timezone.utc).date().isoformat()

    @property
    def title_key(self) -> str:
        """The normalized-headline identity, or ``""`` when it is not one.

        Empty for a headline that normalizes away or carries fewer than
        ``_MIN_KEY_TOKENS`` tokens — a degenerate key folds everything it
        touches, which is far worse than the duplicate it was chasing
        (``wire_pair_collapse``'s own lesson, same floor).
        """
        key = _normalize_headline(self.title)
        return key if len(key.split()) >= _MIN_KEY_TOKENS else ""


@dataclass
class TargetBundle:
    """Everything one desk contributes to a run, already fetched."""

    target_id: str
    country: str
    map_version: str = ""
    layer_map: dict[str, str] = field(default_factory=dict)
    apertures: dict[str, tuple[str, str]] = field(default_factory=dict)
    signals: list[SignalItem] = field(default_factory=list)
    rows_truncated: bool = False


@dataclass
class DayCount:
    """One layer on one day: what survived the fold, and what it absorbed."""

    kept: int = 0
    folded: int = 0
    raw: int = 0


# ---------------------------------------------------------------------------
# The fold
# ---------------------------------------------------------------------------


class _Folds:
    """Union-find over ITEM INDICES inside one (layer, day) bucket."""

    def __init__(self, n: int) -> None:
        self._parent = list(range(n))

    def find(self, i: int) -> int:
        root = i
        while self._parent[root] != root:
            root = self._parent[root]
        while self._parent[i] != root:
            self._parent[i], i = root, self._parent[i]
        return root

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self._parent[ra] = rb

    def groups(self) -> dict[int, list[int]]:
        out: dict[int, list[int]] = defaultdict(list)
        for i in range(len(self._parent)):
            out[self.find(i)].append(i)
        return dict(out)


def _same_publisher(a: str, b: str) -> bool:
    """Both source_ids are feeds of ONE outlet — an identity statement."""
    pa = publisher_of(a)
    return pa is not None and pa == publisher_of(b)


def fold_bucket(
    items: Sequence[SignalItem], *, max_fold_rows: int
) -> tuple[list[SignalItem], int]:
    """Fold one (layer, day) bucket. Returns ``(representatives, folded)``.

    ``folded`` is how many items were absorbed (``len(items) - len(kept)``).
    Representatives are ordered newest-first, tie-broken by ``signal_id`` — a
    TOTAL order, so the citation list is byte-stable across two runs over the
    same data, which is what makes the body-identity dedup downstream mean
    what it says.
    """
    n = len(items)
    if n <= 1:
        return list(items), 0

    folds = _Folds(n)
    # (1) + (2) the exact keys — content_hash, then normalized headline.
    by_key: dict[str, int] = {}
    for i, item in enumerate(items):
        for key in (
            f"h:{item.content_hash}" if item.content_hash.strip() else "",
            f"t:{item.title_key}" if item.title_key else "",
        ):
            if not key:
                continue
            first = by_key.setdefault(key, i)
            folds.union(first, i)

    # (3) the DECLARED relaxation — a BOUNDED pairwise walk. Rows past the
    # bound keep their own group, i.e. they count, which over-counts the layer
    # rather than under-counting it.
    walk = min(n, max_fold_rows)
    for i in range(walk):
        a = items[i]
        for j in range(i + 1, walk):
            b = items[j]
            if a.source_id == b.source_id or folds.find(i) == folds.find(j):
                continue
            if not (
                shared_wire(a.source_id, b.source_id)
                or _same_publisher(a.source_id, b.source_id)
            ):
                continue
            ka, kb = a.title_key, b.title_key
            if ka and kb and _within(ka, kb, HEADLINE_MAX_DISTANCE_DECLARED):
                folds.union(i, j)

    kept: list[SignalItem] = []
    for members in folds.groups().values():
        kept.append(min(
            (items[m] for m in members),
            key=lambda it: (-it.ts.timestamp(), it.signal_id),
        ))
    kept.sort(key=lambda it: (-it.ts.timestamp(), it.signal_id))
    return kept, n - len(kept)


def count_layer_days(
    bundle: TargetBundle, *, days: Sequence[str], max_fold_rows: int
) -> tuple[dict[str, dict[str, DayCount]], dict[str, dict[str, list[SignalItem]]]]:
    """Folded counts per (layer, day), plus each bucket's representatives.

    A signal whose ``source_id`` is not in the country's open layer map is
    dropped — it is not in the map, so it is in no layer, and nothing here ever
    guesses one. That guess is precisely SEAMS #59's subject.
    """
    buckets: dict[tuple[str, str], list[SignalItem]] = defaultdict(list)
    day_set = set(days)
    for item in bundle.signals:
        layer = bundle.layer_map.get(item.source_id)
        if layer is None or item.day not in day_set:
            continue
        buckets[(layer, item.day)].append(item)

    counts: dict[str, dict[str, DayCount]] = {}
    reps: dict[str, dict[str, list[SignalItem]]] = {}
    for (layer, day), items in buckets.items():
        kept, folded = fold_bucket(items, max_fold_rows=max_fold_rows)
        counts.setdefault(layer, {})[day] = DayCount(
            kept=len(kept), folded=folded, raw=len(items)
        )
        reps.setdefault(layer, {})[day] = kept
    return counts, reps


__all__ = [
    "DayCount",
    "SignalItem",
    "TargetBundle",
    "count_layer_days",
    "fold_bucket",
]
