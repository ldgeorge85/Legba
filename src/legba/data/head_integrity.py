# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""HEAD INTEGRITY (lane N-A, 2026-11-11) — one current head per desk × unit, and it is verified.

THE DEFECTS, measured read-only 2026-11-11 (sweep_quality_1111b WR2):

  * **A failed parse superseded a verified head.** ``inline_target`` lands a
    read whose JSON did not parse as a 0.30 ``unstructured`` row (so the run
    stays auditable). The ``finding_supersession`` clusterer then picked the
    NEWEST row of the cluster as its head, whatever it was — CD escalation
    ``fae8b3ca`` (07:02Z, parse_failed) closed the verified 11-10 head
    ``1423491b``; UA escalation ``7f6280e0`` closed ``6fb1cfff``. Every gather
    drops coerce-tagged rows, so the unit had NO admissible head at all.
  * **Orphan heads.** The clusterer only sees rows with a derivable situation
    signature, and ``derive_signature`` returns ``None`` for a read whose model
    emitted no tags (no ``key_entities``) — 51 desk reads in 14 days. Such a row
    is never superseded by anything: CD escalation ``bb2ce5fc`` (11-03) stayed
    open while the chain went ``6a082ea8`` → ``e916d607`` past it. 80 desk ×
    unit keys held more than one open row on 11-11.
  * Together: the composition basis gather (``superseded_by IS NULL``, coerce
    rows dropped, floor, THEN newest-per-key) found the 11-03 orphan as the only
    open passing row and quoted a 7.7-day-old read as the desk's current one.

THE RULES (one module, so the write path, the read path and the one-off repair
cannot drift):

  1. A failed-parse row (:data:`COERCE_TAGS`) is RECORDED but is never a head:
     :func:`pick_head` takes the newest structured row, and the failed row is
     closed TOWARD it rather than closing it.
  2. Per ``(analyst_id, target_id)`` key there is exactly one open row:
     :func:`plan_key_fold` closes every other open row of a situation-bearing
     key toward its head, whether or not that row has a signature.
  3. The read side does not trust supersession alone:
     :func:`live_head_predicate` treats a row closed BY a failed parse as still
     live, and the basis gather folds to the newest row per key BEFORE the
     floor, so an older orphan can never stand in for a newer head.

Stdlib only (the slim registry image imports through ``carried_state``'s
neighbours; nothing here reaches ``legba.runtime``).
"""
from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

#: The coerce-fallback tags. A row carrying one is a read whose body could not
#: be structured (``inline_target._unstructured_finding`` / the composition and
#: correlator coercers). Its faithfulness score is vacuous; it is never a head.
COERCE_TAGS: frozenset[str] = frozenset({"unstructured", "coerce_failed"})

#: The SQL twin of :data:`COERCE_TAGS` (the array every gather already spells).
COERCE_TAGS_SQL = "array['unstructured','coerce_failed']"

#: Audit reasons on the ``finding_supersessions`` edge.
REASON_KEY_FOLD = "head_key_fold"
REASON_PARSE_FAILED = "parse_failed_not_a_head"


def _data(row: Mapping[str, Any]) -> Mapping[str, Any]:
    raw = row.get("data")
    if isinstance(raw, (str, bytes)):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            return {}
    return raw if isinstance(raw, Mapping) else {}


def row_tags(row: Mapping[str, Any]) -> tuple[str, ...]:
    """The finding's tags: the row's own ``tags`` (a payload) or the persisted
    ``data -> 'tags'`` (an ``analyst_outputs`` row)."""
    tags = row.get("tags")
    if not isinstance(tags, (list, tuple)):
        tags = _data(row).get("tags")
    if not isinstance(tags, (list, tuple)):
        return ()
    return tuple(str(t) for t in tags)


def is_failed_parse(row: Mapping[str, Any]) -> bool:
    """True for a coerce-fallback row — recorded, never a head."""
    return any(t in COERCE_TAGS for t in row_tags(row))


def _order_key(row: Mapping[str, Any]) -> tuple[Any, str]:
    return (row.get("produced_at"), str(row.get("id")))


def pick_head(rows: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
    """The head of a set of reads of one key: the newest STRUCTURED row (newest
    ``produced_at``, tie-broken by largest ``id``). Only when every row failed to
    parse is the newest failed row the head — there is no verified read to
    protect, and every gather drops it by tag anyway."""
    if not rows:
        raise ValueError("pick_head of no rows")
    structured = [r for r in rows if not is_failed_parse(r)]
    return max(structured or list(rows), key=_order_key)


def head_key(row: Mapping[str, Any]) -> tuple[str, str] | None:
    """``(analyst_id, target_id)``, or ``None`` when either is missing."""
    a, t = row.get("analyst_id"), row.get("target_id")
    if not a or not t:
        return None
    return (str(a), str(t))


@dataclass(frozen=True)
class FoldLink:
    """One planned close: ``superseded_id`` gets ``superseded_by = superseding_id``."""

    superseded_id: str
    superseding_id: str
    reason: str
    key: tuple[str, str]


def plan_key_fold(
    rows: Iterable[Mapping[str, Any]],
    *,
    closed_ids: Iterable[Any] = (),
    exclude_analysts: Iterable[str] = (),
    signature_key: str = "_sig",
) -> list[FoldLink]:
    """Close every open row of a key except its head (rule 2). Pure.

    ``rows`` are OPEN finding rows (``superseded_by IS NULL``) carrying ``id``,
    ``analyst_id``, ``target_id``, ``produced_at`` and ``data``/``tags``;
    ``signature_key`` names the row field holding its situation signature (the
    clusterer stamps ``_sig``; the repair passes the column). ``closed_ids`` are
    rows already closed this run, which are left out.

    A key is folded only when it is a SITUATION-BEARING desk stream — at least
    one of its rows carries a derived ``sig:`` signature — so a deterministic
    metrics stream (no signature ever) is never touched. A row with an explicit
    ``sit:`` signature names its own situation and is never folded by key.
    """
    closed = {str(i) for i in closed_ids}
    excluded = {str(a) for a in exclude_analysts}
    groups: dict[tuple[str, str], list[Mapping[str, Any]]] = defaultdict(list)
    bearing: set[tuple[str, str]] = set()
    for row in rows:
        key = head_key(row)
        if key is None or key[0] in excluded or str(row.get("id")) in closed:
            continue
        sig = str(row.get(signature_key) or "")
        if sig.startswith("sit:"):
            continue
        if sig.startswith("sig:"):
            bearing.add(key)
        groups[key].append(row)
    links: list[FoldLink] = []
    for key in sorted(groups):
        members = groups[key]
        if key not in bearing or len(members) < 2:
            continue
        head = pick_head(members)
        head_id = str(head.get("id"))
        for row in sorted(members, key=_order_key):
            if str(row.get("id")) == head_id:
                continue
            links.append(FoldLink(
                superseded_id=str(row.get("id")),
                superseding_id=head_id,
                reason=REASON_PARSE_FAILED if is_failed_parse(row) else REASON_KEY_FOLD,
                key=key,
            ))
    return links


#: Every coerce path stamps confidence 0.30 exactly (``_unstructured_finding``,
#: ``composition_coercion``, the critic / correlator / cross-target coercers;
#: 210 of 210 coerce rows in the 60 days to 2026-11-11). The superseder probe
#: checks this column BEFORE the tags, so the probe reads the superseder's heap
#: tuple and detoasts its ``data`` only for a 0.30 row (live EXPLAIN: the
#: unguarded probe cost the world gather 720 ms against 56 ms).
FAILED_PARSE_CONFIDENCE_CEIL = 0.31


def live_head_predicate(alias: str = "f", *, gated: Sequence[str] = ()) -> str:
    """The open-head predicate for a gather (rule 3, first half).

    A row is live when nothing superseded it — OR when what superseded it is a
    failed parse, which is never a head (rule 1). The second arm is a primary-key
    probe per superseded row in the window; it keeps a verified head reachable
    on rows written before the write-path fix (and until the repair runs).

    ``gated`` clauses (the ones that read ``data`` — the coerce-tag and meta
    filters) are evaluated only on LIVE rows, inside a ``CASE``: Postgres orders
    top-level quals by estimated cost and cannot see TOAST, so as separate quals
    they would detoast every superseded row in the window first."""
    probe = (
        f"EXISTS (SELECT 1 FROM analyst_outputs hs WHERE hs.id = {alias}.superseded_by"
        f" AND hs.confidence < {FAILED_PARSE_CONFIDENCE_CEIL}"
        f" AND (hs.data -> 'tags' ?| {COERCE_TAGS_SQL}))"
    )
    live = f"{alias}.superseded_by IS NULL OR {probe}"
    if not gated:
        return f"({live})"
    return f"(CASE WHEN {live} THEN ({' AND '.join(gated)}) ELSE false END)"


def live_head_select(
    cols: str, where: Sequence[str], *, gated: Sequence[str] = (), alias: str = "f",
) -> str:
    """The newest LIVE row per ``(analyst_id, target_id)`` among ``where`` (rule
    3, second half): the fold runs BEFORE any floor, so a key whose newest head
    fails the floor has no basis head (it is periphery, and the floor fallback
    decides what stands in for it) instead of an older open orphan. ``where``
    must not already carry :func:`live_head_predicate`; ``gated`` as there."""
    preds = [*where, live_head_predicate(alias, gated=gated)]
    return (
        f"SELECT DISTINCT ON ({alias}.analyst_id, {alias}.target_id) {cols}"
        f" FROM analyst_outputs {alias} WHERE {' AND '.join(preds)}"
        f" ORDER BY {alias}.analyst_id, {alias}.target_id,"
        f" {alias}.produced_at DESC, {alias}.id DESC"
    )


__all__ = [
    "COERCE_TAGS",
    "COERCE_TAGS_SQL",
    "FAILED_PARSE_CONFIDENCE_CEIL",
    "FoldLink",
    "REASON_KEY_FOLD",
    "REASON_PARSE_FAILED",
    "head_key",
    "is_failed_parse",
    "live_head_predicate",
    "live_head_select",
    "pick_head",
    "plan_key_fold",
    "row_tags",
]
