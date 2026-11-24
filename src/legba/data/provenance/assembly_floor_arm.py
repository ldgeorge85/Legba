# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""ARM 4(c) — FLOOR DISCLOSURE: the subject-group floor's own identities (W4 F-3).

WHY A NEW CHECK. On a world read the floor SELECTS one block per subject group
and the strict prefix FILLS the rest (``legba.data.analysts.assembly_floor``),
and the published order is rebuilt around that selection — so ARM 4(b)'s prefix
check (``assembly_arms._prefix_violations``) is now TRUE BY CONSTRUCTION on the
world tier, the way §F-7 already said it was free. The identities that carry the
weight are the floor's, read off the persisted audit line
(``data.data.assembly.floor``) and the ``order_rank`` stamps beside it:

  * **F1, arithmetic** — ``Σ groups[].offered == counts.candidates`` and
    ``Σ groups[].carried == len(blocks)``: the group ledger accounts for every
    candidate and every block, once.
  * **F2, honoured** — every group with ``offered ≥ 1`` has ``carried ≥ 1``, and
    the persisted ``honoured`` flag says so (decision D1 makes the floor a
    guarantee, so a group at zero is a broken constructor, not a policy).
  * **F3, prefix modulo the floor** — every ``not_selected[].order_rank`` is
    greater than every FILL block's ``order_rank`` (a fill block = a carried
    block that is not its group's floor member). The generalised strict prefix;
    it reduces to ARM 4(b)'s check when nothing is promoted.
  * **F4, stamp honesty** — a block stamped ``carried_by_floor`` sits below the
    cap (``order_rank > cap``) AND is its group's declared best
    (``order_rank`` equals a ``kept_by_floor`` group's ``best_order_rank``), the
    stamped blocks are exactly ``floor.promoted``, and every
    ``displaced_by_floor`` row is one the pure prefix would have carried
    (``order_rank ≤ cap``) and is exactly ``floor.displaced``.
  * **F5, restraint-shift floor** (W1) — the ``carried_by_restraint_floor`` /
    ``displaced_by_restraint_floor`` stamps match ``carry_plan.restraint_floor``
    (at most ``slots`` placements, one displaced row per promotion); those
    rows are a floor's members, so F3 sets them aside the way it sets aside
    the group floor's.
  * **F6, shared thread and empty slots** (N-C, 2026-11-11) — the
    ``carried_by_thread`` / ``displaced_by_thread`` stamps match
    ``carry_plan.thread_slot.picks`` (at most ``slots``, one displaced row
    each), every ``declared_empty_slot`` drop row is a ``carry_plan.empty_slots``
    entry, and ``floor.declared_empty`` names only those entries' groups. F2
    accepts a group at zero only when it is declared empty; F3 sets the
    thread's and the empty slot's rows aside like the restraint floor's.

ONE FINDING, HARD, however many identities break (``drop_floor_violation``): a
broken floor ledger is one construction defect and costs the read one claim —
the ``selection_honesty`` rule. The detail names every identity that broke.

INERT WITHOUT ``floor``. No key, no counter, no finding: every country,
thematic and region payload — and every world payload written before this
train — audits byte-identically. ``assembly_floor_checked`` is bumped once per
read that CARRIES a floor, which is also the one extra check unit
``assembly_arms.branch_score`` adds (``LEDGER_CHECK_UNITS`` stays 6).

The arm reads the PAYLOAD only and is DB-free, like its siblings. It imports
from ``assembly_arms`` one way; ``assembly_arms.selection_honesty`` calls it
through a deferred import, so neither module needs the other at import time.
"""

from __future__ import annotations

from typing import Any, Mapping

from .assembly_arms import (
    _DETAIL_CHARS,
    DROP_FLOOR_VIOLATION,
    ArmFinding,
    ArmResult,
    _as_int,
    _blocks,
    _str,
)

__all__ = ["DROP_FLOOR_VIOLATION", "floor_disclosure"]

_KEPT_BY_FLOOR = "kept_by_floor"

#: W1 — the restraint-shift floor's stamps (``assembly_world``), spelled here
#: because a guard may not import the analyst it grades.
_RESTRAINT_CARRIED = "carried_by_restraint_floor"
_RESTRAINT_DISPLACED = "displaced_by_restraint_floor"
#: N-C — the shared-thread slot's stamps and the declared-empty drop stamp.
_THREAD_CARRIED = "carried_by_thread"
_THREAD_DISPLACED = "displaced_by_thread"
_EMPTY_SLOT = "declared_empty_slot"


def _rows(raw: Any) -> list[Mapping[str, Any]]:
    if not isinstance(raw, (list, tuple)):
        return []
    return [r for r in raw if isinstance(r, Mapping)]


def floor_disclosure(assembly: Mapping[str, Any]) -> ArmResult:
    """ARM 4(c) — F1-F4 over the persisted floor. See the module banner."""
    res = ArmResult()
    floor = assembly.get("floor")
    if floor is None:
        return res
    res.bump("assembly_floor_checked")
    broken: list[str] = []
    if not isinstance(floor, Mapping):
        broken.append("F1: assembly.floor is not an object")
        floor = {}

    blocks = _blocks(assembly)
    drops = assembly.get("drops") if isinstance(assembly.get("drops"), Mapping) else {}
    counts = drops.get("counts") if isinstance(drops.get("counts"), Mapping) else {}
    not_selected = _rows(drops.get("not_selected"))
    groups = _rows(floor.get("groups"))
    cap = _as_int(floor.get("cap"))

    # F1 — the group ledger adds up.
    offered = sum(_as_int(g.get("offered")) or 0 for g in groups)
    carried = sum(_as_int(g.get("carried")) or 0 for g in groups)
    n_candidates = _as_int(counts.get("candidates"))
    if n_candidates is None or offered != n_candidates:
        broken.append(
            f"F1: groups offer {offered} candidates but counts.candidates={n_candidates}"
        )
    if carried != len(blocks):
        broken.append(f"F1: groups carry {carried} blocks but the read carries {len(blocks)}")

    # F2 — every offered group carried something, and the flag says so.
    declared_empty = {_str(x) for x in floor.get("declared_empty") or []}
    missed = [
        _str(g.get("group_name") or g.get("group_id"))
        for g in groups
        if (_as_int(g.get("offered")) or 0) >= 1 and (_as_int(g.get("carried")) or 0) < 1
        and _str(g.get("group_id")) not in declared_empty
    ]
    if missed:
        broken.append("F2: floor not honoured for " + ", ".join(missed))
    if bool(floor.get("honoured")) != (not missed):
        broken.append(
            f"F2: floor.honoured={floor.get('honoured')!r} but "
            f"{len(missed)} offered group(s) carried nothing"
        )

    # F3 — prefix modulo the floor.
    member_ranks = {
        r for g in groups if (r := _as_int(g.get("best_order_rank"))) is not None
    }
    block_ranks = [_as_int(b.get("order_rank")) for b in blocks]
    if any(r is None for r in block_ranks):
        broken.append("F3: a carried block has no order_rank")
    # W1 — a restraint-shift promotion is a floor member too, and the row it
    # displaced stands outside the prefix by the same rule; F5 audits both.
    fill_ranks = [
        r for b, r in zip(blocks, block_ranks)
        if r is not None and r not in member_ranks
        and b.get(_RESTRAINT_CARRIED) is not True
        and b.get(_THREAD_CARRIED) is not True
    ]
    ns_ranks = [
        _as_int(d.get("order_rank")) for d in not_selected
        if d.get(_RESTRAINT_DISPLACED) is not True
        and d.get(_THREAD_DISPLACED) is not True
        and d.get(_EMPTY_SLOT) is not True
    ]
    if any(r is None for r in ns_ranks):
        broken.append("F3: a not-selected row has no order_rank")
    if fill_ranks:
        top_fill = max(fill_ranks)
        above = sorted(r for r in ns_ranks if r is not None and r <= top_fill)
        if above:
            broken.append(
                f"F3: not-selected order rank(s) {above} at or above a fill "
                f"block's order rank {top_fill}"
            )

    # F4 — the stamps say what happened.
    kept_ranks = {
        _as_int(g.get("best_order_rank"))
        for g in groups
        if _str(g.get("reason")) == _KEPT_BY_FLOOR
    }
    stamped = [b for b in blocks if b.get("carried_by_floor") is True]
    for b in stamped:
        r = _as_int(b.get("order_rank"))
        if cap is None or r is None or r <= cap:
            broken.append(
                f"F4: block {b.get('ordinal')} is stamped carried_by_floor at "
                f"order rank {r}, inside the cap {cap}"
            )
        if r not in kept_ranks:
            broken.append(
                f"F4: block {b.get('ordinal')} is stamped carried_by_floor but "
                f"order rank {r} is no kept_by_floor group's best"
            )
    # ``floor.promoted`` names CANDIDATES; a carried block's candidate is its
    # ``via_head_id`` when it was carried through a lower read (P3-B), else its
    # own ``finding_id``.
    stamped_ids = sorted(
        _str(b.get("via_head_id") or b.get("finding_id")) for b in stamped
    )
    promoted = sorted(_str(x) for x in floor.get("promoted") or [])
    if stamped_ids != promoted:
        broken.append(
            f"F4: {len(stamped)} block(s) stamped carried_by_floor but "
            f"floor.promoted names {len(promoted)} — not the same set"
        )
    displaced_rows = [
        d for d in _rows(drops.get("not_selected")) if d.get("displaced_by_floor") is True
    ]
    for d in displaced_rows:
        r = _as_int(d.get("order_rank"))
        if cap is None or r is None or r > cap:
            broken.append(
                f"F4: {_str(d.get('finding_id'))[:12]!r} is stamped displaced_by_floor "
                f"at order rank {r}, outside the cap {cap}"
            )
    displaced = sorted(_str(x) for x in floor.get("displaced") or [])
    if sorted(_str(d.get("finding_id")) for d in displaced_rows) != displaced:
        broken.append(
            f"F4: {len(displaced_rows)} not-selected row(s) stamped displaced_by_floor but "
            f"floor.displaced names {len(displaced)}"
        )

    broken.extend(_restraint_violations(assembly, blocks, not_selected))
    broken.extend(_thread_and_empty_violations(assembly, blocks, not_selected, declared_empty))

    if broken:
        res.bump("assembly_floor_violation")
        res.findings.append(
            ArmFinding(
                reason=DROP_FLOOR_VIOLATION,
                text=f"floor ledger ({len(groups)} groups, {len(blocks)} blocks carried)",
                detail=("; ".join(broken))[:_DETAIL_CHARS],
            )
        )
    return res


def _restraint_violations(
    assembly: Mapping[str, Any],
    blocks: list[Mapping[str, Any]],
    not_selected: list[Mapping[str, Any]],
) -> list[str]:
    """F5 — the restraint-shift floor's stamps say what its ledger says
    (``carry_plan.restraint_floor``, W1): the stamped blocks are exactly the
    swapped + promoted candidates and number at most ``slots``; the stamped
    drop rows are exactly ``displaced``, one per promotion. Silent when the
    payload carries no plan — every record written before W1."""
    plan = assembly.get("carry_plan")
    floor = plan.get("restraint_floor") if isinstance(plan, Mapping) else None
    stamped = [b for b in blocks if b.get(_RESTRAINT_CARRIED) is True]
    rows = [d for d in not_selected if d.get(_RESTRAINT_DISPLACED) is True]
    if not isinstance(floor, Mapping):
        return (
            [f"F5: {len(stamped) + len(rows)} restraint-floor stamp(s) with no carry_plan"]
            if stamped or rows else []
        )
    out: list[str] = []
    placed = sorted(
        _str(x) for x in (floor.get("swapped") or []) + (floor.get("promoted") or [])
        + (floor.get("added") or [])
    )
    if sorted(_str(b.get("via_head_id") or b.get("finding_id")) for b in stamped) != placed:
        out.append(
            f"F5: {len(stamped)} block(s) stamped {_RESTRAINT_CARRIED} but the "
            f"plan placed {len(placed)}"
        )
    slots = _as_int(floor.get("slots"))
    if slots is None or len(placed) > slots:
        out.append(f"F5: {len(placed)} restraint-floor placement(s) over {slots} slot(s)")
    displaced = sorted(_str(x) for x in floor.get("displaced") or [])
    if sorted(_str(d.get("finding_id")) for d in rows) != displaced:
        out.append(
            f"F5: {len(rows)} row(s) stamped {_RESTRAINT_DISPLACED} but the plan "
            f"displaced {len(displaced)}"
        )
    # N-C — an ADDED block (a carried unit's second, political block) pays for
    # its slot exactly as a promotion does.
    paying = len(floor.get("promoted") or []) + len(floor.get("added") or [])
    if len(displaced) != paying:
        out.append(f"F5: {paying} promotion(s)/addition(s) displaced {len(displaced)} row(s)")
    return out


def _thread_and_empty_violations(
    assembly: Mapping[str, Any],
    blocks: list[Mapping[str, Any]],
    not_selected: list[Mapping[str, Any]],
    declared_empty: set[str],
) -> list[str]:
    """F6 — the shared-thread and empty-slot stamps say what the plan says
    (N-C). Silent on every payload with none of the stamps and no plan keys."""
    plan = assembly.get("carry_plan")
    plan = plan if isinstance(plan, Mapping) else {}
    thread = plan.get("thread_slot") if isinstance(plan.get("thread_slot"), Mapping) else None
    empty = _rows(plan.get("empty_slots"))
    stamped = [b for b in blocks if b.get(_THREAD_CARRIED) is True]
    rows = [d for d in not_selected if d.get(_THREAD_DISPLACED) is True]
    empty_rows = [d for d in not_selected if d.get(_EMPTY_SLOT) is True]
    out: list[str] = []
    if thread is None:
        if stamped or rows:
            out.append(f"F6: {len(stamped) + len(rows)} thread stamp(s) with no thread_slot")
    else:
        picks = _rows(thread.get("picks"))
        placed = sorted(_str(p.get("via_head_id")) for p in picks)
        if sorted(_str(b.get("via_head_id") or b.get("finding_id")) for b in stamped) != placed:
            out.append(
                f"F6: {len(stamped)} block(s) stamped {_THREAD_CARRIED} but the plan "
                f"placed {len(picks)}"
            )
        slots = _as_int(thread.get("slots"))
        if slots is None or len(picks) > slots:
            out.append(f"F6: {len(picks)} thread placement(s) over {slots} slot(s)")
        if sorted(_str(d.get("finding_id")) for d in rows) != sorted(
            _str(p.get("displaced")) for p in picks
        ):
            out.append(
                f"F6: {len(rows)} row(s) stamped {_THREAD_DISPLACED} but the plan "
                f"displaced {len(picks)}"
            )
    if sorted(_str(d.get("finding_id")) for d in empty_rows) != sorted(
        _str(e.get("via_head_id")) for e in empty
    ):
        out.append(
            f"F6: {len(empty_rows)} row(s) stamped {_EMPTY_SLOT} but the plan "
            f"declared {len(empty)}"
        )
    if declared_empty - {_str(e.get("group_id")) for e in empty}:
        out.append("F6: floor.declared_empty names a group no empty slot belongs to")
    return out
