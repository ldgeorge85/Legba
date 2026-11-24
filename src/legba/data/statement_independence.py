# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Gate G7 — independence: corroboration counts origins, not articles.

Plan P1 §3 G7: "``independent_groups`` counts distinct ``origin_group_id``
among evidence rows after collapsing near-identical texts by shingle hash.
Until groups are populated, each source is its own group and every read says
so." Twenty syndicated copies of one story count once; a paraphrase from a
second owner counts twice — and the count says that is its limit.

THE COUNT, exactly.

1. Each evidence row gets a group key: its ``origin_group_id``; failing that
   ``source:<source_id>`` (each source its own group — the fallback the plan
   names); failing that ``ref:<ref_id>``.
2. Rows sharing a key are one origin.
3. Rows whose stored texts are near-identical are one origin too, whatever
   their keys: word 5-gram shingles, hashed, compared by CONTAINMENT
   (shared shingles over the smaller set) so a wire copy with an outlet's
   paragraph appended still collapses. Texts too short to shingle reliably
   collapse only when their shingle sets are equal.
4. ``independent_groups`` is the number of resulting components;
   ``checkable_groups`` the number with at least one checkable row.

WHAT IT CANNOT SEE, stated on every count (``basis``): two owners each
paraphrasing one planted story are two groups until the knowledge-source model
records syndication; and while any row lacks an origin group the count is an
upper bound.

A LEAF: stdlib + the shapes leaf + the house fold.
"""
from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from .provenance.text_fold import normalize_for_match
from .statement_shapes import EvidenceTexts, SupportLike, SupportRole, enum_value

#: Words per shingle.
SHINGLE_WORDS = 5
#: Containment at or above which two texts are one origin.
NEAR_DUPLICATE_CONTAINMENT = 0.8
#: Below this many shingles a text is "short": it collapses only on equality.
MIN_SHINGLES_FOR_CONTAINMENT = 8

BASIS_GROUPS = "origin_groups"
BASIS_FALLBACK = "each_source_own_group"


def shingles(text: str | None, *, width: int = SHINGLE_WORDS) -> frozenset[int]:
    """Hashed word ``width``-gram shingles of the folded text (a text shorter
    than ``width`` words is one shingle of all its words)."""
    words = normalize_for_match(text).split()
    if not words:
        return frozenset()
    if len(words) < width:
        grams = [" ".join(words)]
    else:
        grams = [" ".join(words[i:i + width]) for i in range(len(words) - width + 1)]
    return frozenset(
        int.from_bytes(hashlib.blake2b(g.encode("utf-8"), digest_size=8).digest(), "big")
        for g in grams
    )


def near_identical(a: frozenset[int], b: frozenset[int]) -> bool:
    """Are two shingle sets one text for independence purposes?"""
    if not a or not b:
        return False
    if min(len(a), len(b)) < MIN_SHINGLES_FOR_CONTAINMENT:
        return a == b
    return len(a & b) / min(len(a), len(b)) >= NEAR_DUPLICATE_CONTAINMENT


def group_key(row: SupportLike) -> tuple[str, bool]:
    """``(key, from_origin_group)`` for one row (step 1 of the count)."""
    if row.origin_group_id:
        return f"group:{row.origin_group_id}", True
    if row.source_id:
        return f"source:{row.source_id}", False
    return f"ref:{row.ref_id}", False


def row_text(row: SupportLike, texts: EvidenceTexts | None) -> str:
    """The stored text the row points at, or its quote when none resolves."""
    if texts is not None:
        fv = row.fetch_version_id
        if fv is not None and (fv, str(row.ref_id)) in texts.by_fetch_item:
            return texts.by_fetch_item[(fv, str(row.ref_id))]
        if fv is not None and fv in texts.by_fetch_version:
            return texts.by_fetch_version[fv]
        kind = enum_value(row.support_kind)
        if kind == "signal" and str(row.ref_id) in texts.by_signal:
            return texts.by_signal[str(row.ref_id)]
        if kind == "observation" and str(row.ref_id) in texts.by_observation:
            return texts.by_observation[str(row.ref_id)].text
    return row.quote or ""


def is_checkable(row: SupportLike) -> bool:
    """An explicit ``checkable`` wins; unset, only ``public`` material is
    checkable (design B1 §3 floor F2 — undeclared is not promoted)."""
    if row.checkable is not None:
        return bool(row.checkable)
    return (row.access_class or "") == "public"


@dataclass(frozen=True)
class IndependenceCount:
    """G7's answer for one role's rows."""

    groups: int
    checkable_groups: int
    rows: int
    #: Row indices (into the caller's list) of each component, in order.
    components: tuple[tuple[int, ...], ...] = ()
    #: Rows that joined a component by text alone (different keys).
    collapsed_by_text: tuple[int, ...] = ()
    #: ``origin_groups`` when every row carried one, else the fallback.
    basis: str = BASIS_GROUPS
    keys: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, object]:
        return {
            "groups": self.groups,
            "checkable_groups": self.checkable_groups,
            "rows": self.rows,
            "components": [list(c) for c in self.components],
            "collapsed_by_text": list(self.collapsed_by_text),
            "basis": self.basis,
            "limit": (
                "independence is counted by declared origin group and near-identical "
                "text; separately owned paraphrases of one story count separately"
                + ("" if self.basis == BASIS_GROUPS
                   else "; origin groups not populated, each source counts as its own "
                        "group, so this count is an upper bound")
            ),
        }


def count_groups(
    support: Sequence[SupportLike],
    texts: EvidenceTexts | None = None,
    *,
    role: str = SupportRole.EVIDENCE.value,
) -> IndependenceCount:
    """G7 over the rows of ``support`` with ``role`` (indices are positions in
    ``support``, so a caller's per-row records line up)."""
    idx = [i for i, r in enumerate(support) if enum_value(r.role) == role]
    parent = {i: i for i in idx}

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    keys: dict[int, str] = {}
    all_grouped = True
    first_by_key: dict[str, int] = {}
    for i in idx:
        key, grouped = group_key(support[i])
        all_grouped = all_grouped and grouped
        keys[i] = key
        if key in first_by_key:
            union(first_by_key[key], i)
        else:
            first_by_key[key] = i

    sh = {i: shingles(row_text(support[i], texts)) for i in idx}
    by_text: set[int] = set()
    for a_pos, a in enumerate(idx):
        for b in idx[a_pos + 1:]:
            if keys[a] != keys[b] and near_identical(sh[a], sh[b]):
                if find(a) != find(b):
                    by_text.add(b)
                union(a, b)

    comps: dict[int, list[int]] = {}
    for i in idx:
        comps.setdefault(find(i), []).append(i)
    components = tuple(tuple(c) for _, c in sorted(comps.items()))
    checkable = sum(1 for c in components if any(is_checkable(support[i]) for i in c))
    return IndependenceCount(
        groups=len(components),
        checkable_groups=checkable,
        rows=len(idx),
        components=components,
        collapsed_by_text=tuple(sorted(by_text)),
        basis=BASIS_GROUPS if all_grouped else BASIS_FALLBACK,
        keys=tuple(keys[i] for i in idx),
    )


def contradicting_groups(
    support: Sequence[SupportLike],
    texts: EvidenceTexts | None = None,
) -> int:
    """Independent origins among ``contradicts`` rows, not counting any that
    share a group key with an evidence row (one origin cannot both support and
    contest a claim and be counted on both sides)."""
    evidence_keys = {
        group_key(r)[0] for r in support
        if enum_value(r.role) == SupportRole.EVIDENCE.value
    }
    count = count_groups(support, texts, role=SupportRole.CONTRADICTS.value)
    return sum(
        1 for comp in count.components
        if not any(group_key(support[i])[0] in evidence_keys for i in comp)
    )


#: Plan §1.3 ``support_word`` — derived in code, never written by a model.
SUPPORT_WORDS: tuple[str, ...] = ("corroborated", "single_source", "contested", "unclear")
#: Independent contradicting origins at or above which a statement reads
#: ``contested`` (design B1 §2.6: one contradicting outlet changes nothing).
CONTEST_MIN_GROUPS = 2


def derive_support_word(independent: int, contradicting: int = 0) -> str:
    """``contested`` / ``corroborated`` / ``single_source`` / ``unclear``."""
    if contradicting >= CONTEST_MIN_GROUPS:
        return "contested"
    if independent >= 2:
        return "corroborated"
    if independent == 1:
        return "single_source"
    return "unclear"


def support_word_for(
    support: Sequence[SupportLike], texts: EvidenceTexts | None = None,
) -> Mapping[str, object]:
    """The three numbers and the word, for a caller that wants just those."""
    ev = count_groups(support, texts)
    con = contradicting_groups(support, texts)
    return {
        "independent_groups": ev.groups,
        "checkable_groups": ev.checkable_groups,
        "contradicting_groups": con,
        "support_word": derive_support_word(ev.groups, con),
    }


__all__ = [
    "BASIS_FALLBACK",
    "BASIS_GROUPS",
    "CONTEST_MIN_GROUPS",
    "MIN_SHINGLES_FOR_CONTAINMENT",
    "NEAR_DUPLICATE_CONTAINMENT",
    "SHINGLE_WORDS",
    "SUPPORT_WORDS",
    "IndependenceCount",
    "contradicting_groups",
    "count_groups",
    "derive_support_word",
    "group_key",
    "is_checkable",
    "near_identical",
    "row_text",
    "shingles",
    "support_word_for",
]
