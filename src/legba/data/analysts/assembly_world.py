# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""THE WORLD CARRY PLAN — the order, the one-block-per-slot carry, and the
restraint-shift floor, for the WORLD read only (W1, 2026-11-07).

Built from the world-read dig of record e30e67d3 (assessment 64f74201), which
found three composition defects between the 33 candidate heads and the 16
blocks the voice wrote from. Each has its own section below:

  1. THE ORDER (:func:`world_order_key`). ``assembly_payload.order_key`` is
     severity-first, and the world's candidates are assembled country heads
     whose severity is the MAX over their blocks: 25 of 33 were ``high``. A
     saturated ordinal sorts nothing, so cited mass decided only inside the
     ``high`` band and Türkiye — ``elevated``, cited mass 1.70, about fifth by
     mass — ranked 26th, below eight ``high`` heads at 0.05-0.30.

  2. THE DUPLICATE SLOT (:func:`plan_world_carry`). Each candidate carries one
     block, chosen per candidate with no view of the others, so the thematic
     head (``escalation_composition``) and the Russia head both carried Russia's
     escalation desk head ``e8b3cc08``: slots 1 and 2, one finding.

  3. THE RESTRAINT-SHIFT FLOOR. A political or institutional change (a chief of
     staff replaced, a mandate consolidated) cites magnitudes the salience
     scorer has no class for, so it never wins a by-mass carry against a
     kinetic block. :data:`RESTRAINT_SHIFT_SLOTS` world slots are reserved for
     blocks from :data:`RESTRAINT_SHIFT_DESKS` that NAME A DATED INSTITUTIONAL
     EVENT (N-C, 2026-11-11: the lexical ``reports_change`` test it replaced
     read a blocked impeachment as "no change" and gave the US politics slot to
     a 0.05 Pakistan block; ``world_selection_rules``), ranked by cited mass.

N-C (2026-11-11) adds two more, both from the operator's review of fd6584eb:

  4. THE SHARED THREAD. One uncarried unit whose heads cite a signal a carried
     unit's heads also cite (``world_selection_rules.cross_unit_threads``) is
     promoted into a slot, displacing the lowest-ranked displaceable carry, and
     every thread is published as a line (``assembly.threads``).
  5. NO ORDINAL FALLBACK ON A THEMATIC SLOT. A thematic candidate whose only
     carry would be ``fallback_ordinal_1`` (every block an absence, a duplicate
     or massless — the hybrid lane carrying Italy's "No coordinated narrative
     appears") is not carried; the slot is DECLARED EMPTY (``empty_slots``)
     rather than filled with content that is not the lane's.

INERT OFF THE WORLD READ. The synthesizer calls :func:`plan_world_carry` only
on the world composition; every other tier never stamps
:data:`assembly_carry.WORLD_CARRY_ROW_KEY`, so ``carry_choice`` falls through to
``carried_lead_block`` and the payload is byte-for-byte what shipped.

PLATFORM, NOT EXEMPLAR. :data:`RESTRAINT_SHIFT_DESKS` and
:data:`DISPLACEABLE_DESKS` name the geopolitics instance's desks (the analyst
ids ``descriptors/analyst_internal_stability.yaml`` and
``analyst_leadership_transition.yaml`` declare); the mechanism is "reserve K
slots for the change-reporting blocks of a named desk set".
"""

from __future__ import annotations

from typing import Any, Collection, Mapping, NamedTuple, Sequence

from ..change_report import (
    CHANGE_RE,
    STASIS_RE,
    TAIL_RE,
    text_reports_change,
    verdict_of,
)
from ..provenance.text_fold import normalize_for_match
from .assembly_carry import (
    CARRY_FALLBACK_ORDINAL_1,
    CARRY_RESTRAINT_SHIFT,
    WORLD_CARRY_ROW_KEY,
    block_is_absence,
    block_mass,
    carried_lead_block,
    child_assembly,
)
from .assembly_floor import SUBJECT_GROUP_ROW_KEY, FloorSelection
from .assembly_payload import (
    CITED_SALIENCE_ROW_KEY,
    _iso,
    _mass,
    _neg_iso,
    _severity_rank,
)
from .assembly_spans import quoted_spans
from .world_selection_rules import (
    POLITICS_FLOOR_RULE,
    THREAD_LINES_MAX,
    THREAD_MAGNITUDE_FLOOR,
    THREAD_SLOTS,
    THREAD_VERSION,
    block_names_dated_institutional_event,
    cross_unit_threads,
    pick_thread_unit,
    thread_block_for,
)

__all__ = [
    "CARRY_PLAN_KEY",
    "CARRY_PLAN_VERSION",
    "CARRY_RESTRAINT_SHIFT",
    "CARRY_SHARED_THREAD",
    "DISPLACEABLE_DESKS",
    "RESTRAINT_SHIFT_DESKS",
    "RESTRAINT_SHIFT_SLOTS",
    "THREADS_KEY",
    "VIA_UNIT_KEY",
    "WorldCarryPlan",
    "plan_world_carry",
    "reports_change",
    "stamp_world_carry",
    "world_order_key",
]

#: ``data.data.assembly.carry_plan`` — the plan's audit line.
CARRY_PLAN_KEY: str = "carry_plan"
CARRY_PLAN_VERSION: str = "world_carry_plan.v2"

#: ``carry_reason`` of a block the shared-thread slot placed (N-C).
CARRY_SHARED_THREAD: str = "shared_thread"

#: ``data.data.assembly.threads`` — the cross-unit thread lines (N-C).
THREADS_KEY: str = "threads"

#: Private row stamp on the COPY of a unit's row that carries the politics
#: floor's ADDED block (N-C follow-up): the unit keeps its own lead block.
FLOOR_EXTRA_ROW_KEY: str = "_floor_extra"

#: ``carry_plan.empty_slots[].why`` — a thematic slot refused its ordinal fallback.
EMPTY_SLOT_WHY: str = "thematic_fallback_refused"

#: ``blocks[].via_unit`` — the ROSTER UNIT the block was carried through: the
#: candidate's ``target_id``, or the analyst id of a target-less thematic head.
#: A thematic carry's ``target_id`` is its CHILD's (Russia, for
#: ``escalation_composition``'s carry of the Russia desk head), which is how the
#: aperture renderer came to list Russia twice and the thematic unit never.
VIA_UNIT_KEY: str = "via_unit"

#: ``carry_reason`` of a block the restraint-shift floor placed:
#: ``assembly_carry.CARRY_RESTRAINT_SHIFT`` (imported above), a member of
#: ``assembly_carry.CARRY_REASONS``.

#: K — the world slots reserved for change-reporting restraint blocks. Never
#: more; none reserved when none qualifies.
RESTRAINT_SHIFT_SLOTS: int = 2

#: The political / institutional dimension: each unit's elite-churn-and-unrest
#: desk and its top-job-change desk.
RESTRAINT_SHIFT_DESKS: tuple[str, ...] = ("internal_stability", "leadership_transition")

#: The kinetic and economic desks a floor slot may displace. Narrative
#: coordination is neither, and is never displaced to make room.
DISPLACEABLE_DESKS: tuple[str, ...] = (
    "escalation", "military_posture", "proliferation_watch",
    "economic_coercion", "energy_security",
)


# ---------------------------------------------------------------------------
# 1. THE ORDER
# ---------------------------------------------------------------------------


def world_order_key(row: Mapping[str, Any]) -> tuple[float, int, str, str]:
    """``cited_mass DESC, severity DESC, produced_at DESC, finding_id ASC``.

    MASS-FIRST, not percentile severity, and the choice is the data's: a
    percentile of a saturated ordinal is still saturated (25 of 33 heads tie at
    ``high``, so their percentile ties too), while ``cited_mass`` is the pooled
    evidence under the head and is continuous. ``order_key``'s own argument
    against mass-first — ``energy_security`` gaining at the expense of other
    desks — is about DESK blocks inside one country; here every candidate is a
    whole unit and that dominance has nothing to act on. Severity stays as the
    first tiebreak, so equal mass still reads the desks' own verdicts.

    Total and reproducible, like ``order_key``.
    """
    return (
        -_mass(row),
        -_severity_rank(row.get("severity")),
        _neg_iso(_iso(row.get("produced_at")) or ""),
        str(row.get("id") or ""),
    )


# ---------------------------------------------------------------------------
# 3. WHAT COUNTS AS A REPORTED CHANGE
# ---------------------------------------------------------------------------

#: THE ONE CHANGE TEST lives in :mod:`legba.data.change_report`: the
#: carry rule (``legba.data.carried_state``) asks the same question of a desk
#: read and may not import this package. The names below are that module's
#: objects, kept for the callers and tests that read them here.
_TAIL_RE = TAIL_RE
_CHANGE_RE = CHANGE_RE
_STASIS_RE = STASIS_RE


def _verdict(text: Any) -> str:
    return verdict_of(normalize_for_match(text))


def reports_change(block: Mapping[str, Any]) -> bool:
    """Does this block's verdict REPORT A CHANGE, as opposed to stasis or an
    absence? Total — ``False`` on anything it cannot read.

    Three tests, all required: the block is not an absence
    (:func:`assembly_carry.block_is_absence`); its lead clause, the leading
    *"Assessed"* frame set aside, does not open on stasis (:data:`_STASIS_RE` —
    *"Assessed a low likelihood of any change in Türkiye's top leadership"*);
    and its verdict, up to the provenance tail, names a change
    (:data:`_CHANGE_RE` — *"…President Trump replaced chief of staff Susie
    Wiles with hardliner Stephen Miller…"*). The last two are
    :func:`legba.data.change_report.text_reports_change`, the carry rule's test
    too.
    """
    if not isinstance(block, Mapping) or block_is_absence(block):
        return False
    spans = quoted_spans(block)
    if not spans or not isinstance(spans[0], Mapping):
        return False
    return text_reports_change(str(spans[0].get("text") or ""))


def _is_restraint_shift(block: Mapping[str, Any]) -> bool:
    """Politics-floor eligibility (N-C): a political desk's block that names a
    dated institutional event. ``reports_change`` stays exported (the carry
    rule and its tests read it) but no longer decides the floor."""
    return (
        str(block.get("desk") or "") in RESTRAINT_SHIFT_DESKS
        and block_names_dated_institutional_event(block)
    )


# ---------------------------------------------------------------------------
# 2 + 3. THE PLAN
# ---------------------------------------------------------------------------


class WorldCarryPlan(NamedTuple):
    """``selection`` replaces the caller's :class:`FloorSelection` (its
    ``order`` / ``carried`` and, when present, its ledger re-counted);
    ``ledger`` is the plan's audit material for :func:`stamp_world_carry`."""

    selection: FloorSelection
    ledger: dict[str, Any]


def _rid(row: Mapping[str, Any]) -> str:
    return str(row.get("id") or "")


def _unit(row: Mapping[str, Any]) -> str:
    return str(row.get("target_id") or row.get("analyst_id") or "")


def _fid(block: Mapping[str, Any] | None) -> str:
    return str((block or {}).get("finding_id") or "")


def _is_thematic(row: Mapping[str, Any]) -> bool:
    return not row.get("target_id")


def _choose(
    carried: Sequence[Mapping[str, Any]],
    forced: Mapping[str, Mapping[str, Any]],
    reasons: Mapping[str, str] | None = None,
    exclude: Collection[str] = (),
) -> tuple[dict[str, tuple[Any, Any, Any]], int]:
    """One block per carried assembled row, in published order, no finding
    carried twice. ``forced`` (row id -> block) claims its findings FIRST, under
    the carry reason ``reasons`` names for it (the restraint floor's when
    absent). Returns the choices and the count of duplicates nothing could
    avoid. A THEMATIC row whose only carry is the ordinal fallback gets NO
    choice (rule 5): the caller declares its slot empty."""
    taken = {_fid(b) for b in forced.values()} | {str(x) for x in exclude}
    out: dict[str, tuple[Any, Any, Any]] = {}
    dupes = 0
    for row in carried:
        child = child_assembly(row)
        if child is None:
            continue
        rid = _rid(row)
        if rid in forced:
            b = forced[rid]
            out[rid] = (
                b, int(b.get("ordinal") or 1),
                (reasons or {}).get(rid, CARRY_RESTRAINT_SHIFT),
            )
            continue
        block, ordinal, reason = carried_lead_block(child, exclude=taken)
        if block is None:
            continue
        if _is_thematic(row) and reason == CARRY_FALLBACK_ORDINAL_1:
            continue
        if _fid(block) in taken:
            dupes += 1
        taken.add(_fid(block))
        out[rid] = (block, ordinal, reason)
    return out, dupes


def plan_world_carry(
    selection: FloorSelection, *, slots: int = RESTRAINT_SHIFT_SLOTS,
    thread_slots: int = THREAD_SLOTS,
) -> WorldCarryPlan:
    """Choose every carried block across the whole selection, apply the
    restraint-shift floor, and stamp each carried row with its choice.

    THE FLOOR. Every candidate's change-reporting restraint block
    (:func:`reports_change` over :data:`RESTRAINT_SHIFT_DESKS`) is eligible;
    carried rows whose own choice already is one count toward the K slots.
    The rest are filled by cited mass among the eligible, one per unit:

      * a unit ALREADY carried carries that block in its own slot instead of
        its kinetic/economic one (``swapped``). One block per candidate is the
        record's structural invariant — ordinal N is ``derived_from`` entry N,
        and ARM 4(b) and the floor arm read it — so a carried unit's political
        block can only enter through that unit's slot;
      * a unit NOT carried is promoted into the slot of the LOWEST-ranked
        carried row whose block is kinetic or economic
        (:data:`DISPLACEABLE_DESKS`), that is not itself one of the K picks,
        and whose subject group keeps another carry — so the subject-group
        floor stays honoured (``promoted`` / ``displaced``). No such row: that
        pick is ``unplaced`` and the next eligible unit is tried.

    Never more than ``slots``; none reserved when none qualifies.

    THE SHARED THREAD (N-C). After the floor, at most ``thread_slots``
    uncarried units that share a cross-unit thread with a carried unit are
    promoted the same way (same victim rule), each carrying the block that holds
    the thread (``world_selection_rules.thread_block_for``).

    EMPTY SLOTS (N-C). A thematic candidate with no block of its own to carry
    (the ordinal fallback) leaves the carried set; it is listed in
    ``empty_slots`` and its slot is not refilled.
    """
    order = list(selection.order)
    carried = list(selection.carried)
    base = dict(selection.ledger) if selection.ledger else None
    pure = (
        dict(base.get("_order_rank") or {}) if base
        else {_rid(r): i + 1 for i, r in enumerate(order)}
    )
    natural, _ = _choose(carried, {})
    # 5. EMPTY SLOTS — a thematic row with no block of its own leaves the set.
    empty: list[Mapping[str, Any]] = [
        r for r in carried
        if child_assembly(r) is not None and _rid(r) not in natural
    ]
    empty_ids = {_rid(r) for r in empty}
    carried = [r for r in carried if _rid(r) not in empty_ids]
    # N-T (2026-11-13, world record 3772fcad) — a GROUP-FLOOR promotion whose
    # slot then ends empty displaced a prefix row for nothing (Argentina lost
    # its place to the declared-empty hybrid lane). The promotion is VOIDED and
    # the row it displaced is restored, best pure rank first, so the floor can
    # never displace a block unless the promoted block is actually carried.
    voided: list[str] = []
    restored: list[str] = []
    if base:
        group_promoted = {str(x) for x in base.get("promoted") or []}
        group_displaced = {str(x) for x in base.get("displaced") or []}
        owed = sum(1 for r in empty if _rid(r) in group_promoted)
        voided = [_rid(r) for r in empty if _rid(r) in group_promoted]
        pool = sorted(
            (r for r in order if _rid(r) in group_displaced),
            key=lambda r: pure.get(_rid(r), 10**6),
        )
        while owed > 0 and pool:
            row = pool.pop(0)
            owed -= 1
            restored.append(_rid(row))
            carried = sorted(carried + [row], key=lambda r: pure.get(_rid(r), 10**6))
            natural, _ = _choose(carried, {})
            if child_assembly(row) is not None and _rid(row) not in natural:
                # The restored row is itself a thematic with no block of its own:
                # its slot is empty too, and the next displaced row is owed.
                empty.append(row)
                empty_ids.add(_rid(row))
                carried = [r for r in carried if _rid(r) != _rid(row)]
                owed += 1
        if voided:
            base["promoted"] = [x for x in base.get("promoted") or [] if x not in set(voided)]
            base["voided"] = [
                {"promoted": v, "why": EMPTY_SLOT_WHY} for v in voided
            ]
            base["restored"] = list(restored)
        natural, _ = _choose(carried, {})
    natural_fids = {_fid(c[0]) for c in natural.values()}

    eligible: list[tuple[Mapping[str, Any], Mapping[str, Any]]] = []
    for row in order:
        child = child_assembly(row)
        for b in (child or {}).get("blocks") or ():
            if isinstance(b, Mapping) and _is_restraint_shift(b):
                eligible.append((row, b))
    already = [
        rid for rid, (b, _, _) in natural.items() if _is_restraint_shift(b)
    ]
    need = max(0, int(slots) - len(already))
    eligible.sort(key=lambda rb: (
        -block_mass(rb[1]), pure.get(_rid(rb[0]), 10**6), int(rb[1].get("ordinal") or 0),
    ))

    carried_ids = {_rid(r) for r in carried}
    promoted_by_group_floor = {str(x) for x in (base or {}).get("promoted") or []}
    forced: dict[str, Mapping[str, Any]] = {}
    reasons: dict[str, str] = {}
    swapped: list[str] = []
    added: list[str] = []
    extras: dict[str, Mapping[str, Any]] = {}
    promoted: list[str] = []
    displaced: list[str] = []
    unplaced: list[str] = []
    picks: list[dict[str, Any]] = []
    # One candidate pick per unit, heaviest first. The first ``need`` of them
    # are the intended picks, and none of THEM may be displaced to make room
    # for another: on a tight cap the promotion of one pick would otherwise
    # evict a unit whose own pick is next in line.
    pending: list[tuple[Mapping[str, Any], Mapping[str, Any]]] = []
    seen = set(already)
    for row, block in eligible:
        if _rid(row) not in seen and _fid(block) not in natural_fids:
            seen.add(_rid(row))
            pending.append((row, block))
    intended = {_rid(r) for r, _ in pending[:need]}

    added_ids: set[str] = set()

    def _victim() -> Mapping[str, Any] | None:
        """The LOWEST-ranked displaceable carry of ANOTHER unit whose group keeps
        another — never a floor-added unit's lead nor a forced (floor/thread)
        carry."""
        groups: dict[str, int] = {}
        for r in carried:
            g = str(r.get(SUBJECT_GROUP_ROW_KEY) or "")
            groups[g] = groups.get(g, 0) + 1
        return next((
            r for r in sorted(carried, key=lambda r: -pure.get(_rid(r), 0))
            if _rid(r) not in forced
            and _rid(r) not in added_ids
            and _rid(r) not in intended
            and _rid(r) not in promoted_by_group_floor
            and str((natural.get(_rid(r)) or ({},))[0].get("desk") or "")
            in DISPLACEABLE_DESKS
            and (
                base is None
                or groups.get(str(r.get(SUBJECT_GROUP_ROW_KEY) or ""), 0) >= 2
            )
        ), None)

    for row, block in pending:
        if need <= 0:
            break
        rid = _rid(row)
        if rid in carried_ids:
            # N-C follow-up — ADDED, never swapped: the unit keeps its lead
            # block and the political block takes a SECOND slot, paid for by
            # the lowest-ranked displaceable carry of another unit.
            added_ids.add(rid)
            victim = _victim()
            if victim is None:
                added_ids.discard(rid)
                unplaced.append(rid)
                continue
            action = "added"
            carried = [r for r in carried if r is not victim]
            carried_ids = {_rid(r) for r in carried}
            added.append(rid)
            extras[rid] = block
            displaced.append(_rid(victim))
        else:
            victim = _victim()
            if victim is None:
                unplaced.append(rid)
                continue
            action = "promoted"
            carried = [r for r in carried if r is not victim] + [row]
            carried_ids = {_rid(r) for r in carried}
            promoted.append(rid)
            displaced.append(_rid(victim))
            forced[rid] = block
        need -= 1
        picks.append({
            "via_head_id": rid, "unit": _unit(row), "finding_id": _fid(block),
            "desk": str(block.get("desk") or ""), "cited_mass": block_mass(block),
            "action": action,
        })

    # 4. THE SHARED THREAD — after the floor, so a political pick is never
    # displaced to make room for a thread.
    threads = cross_unit_threads(order)
    thread_picks: list[dict[str, Any]] = []
    thread_unplaced: list[str] = []
    thread_displaced: list[str] = []
    taken_fids = natural_fids | {_fid(b) for b in forced.values()} | {
        _fid(b) for b in extras.values()
    }
    # N-T — the loop counts PLACEMENTS, not attempts: a unit that cannot be
    # placed (its only thread-citing block is an absence, as the UK's was on
    # 3772fcad) is ``unplaced`` and the next eligible unit is tried. Terminates:
    # every pass places one unit or removes one from ``by_unit``.
    #
    # A row a floor DISPLACED is never a thread candidate (2026-11-23, world run
    # c27d26ed and six runs before it): the restraint floor displaced a prefix
    # row, the thread slot then promoted that same row back by displacing
    # another, and ``restraint_floor.displaced`` named a row that was carried —
    # ARM 4(c) F5 failed and the whole floor was withdrawn. A displacement
    # stands: the thread promotes only a row no floor or earlier thread pick
    # has put below the line.
    floor_displaced = set(displaced) | {str(x) for x in (base or {}).get("displaced") or []}
    while len(thread_picks) < max(0, int(thread_slots)):
        carried_units = {str(r.get("target_id")) for r in carried if r.get("target_id")}
        carried_units |= {
            str(b.get("target_id")) for rid, (b, _, _) in natural.items()
            if rid in carried_ids and isinstance(b, Mapping) and b.get("target_id")
        }
        by_unit: dict[str, Mapping[str, Any]] = {}
        for r in order:
            tid = str(r.get("target_id") or "")
            if (
                tid and tid not in by_unit and tid not in thread_unplaced
                and _rid(r) not in carried_ids and _rid(r) not in empty_ids
                and _rid(r) not in floor_displaced and _rid(r) not in thread_displaced
                and child_assembly(r) is not None
            ):
                by_unit[tid] = r
        choice = pick_thread_unit(
            threads, carried_units=carried_units, candidates_by_unit=by_unit, rank=pure,
        )
        if choice is None:
            break
        unit, joined = choice
        row = by_unit[unit]
        block = thread_block_for(row, joined, exclude=taken_fids)
        victim = _victim() if block is not None else None
        if block is None or victim is None:
            thread_unplaced.append(unit)
            if block is not None:
                break  # no displaceable carry left: no unit can be placed
            continue
        carried = [r for r in carried if r is not victim] + [row]
        carried_ids = {_rid(r) for r in carried}
        thread_displaced.append(_rid(victim))
        forced[_rid(row)] = block
        reasons[_rid(row)] = CARRY_SHARED_THREAD
        taken_fids.add(_fid(block))
        thread_picks.append({
            "via_head_id": _rid(row), "unit": unit, "finding_id": _fid(block),
            "desk": str(block.get("desk") or ""), "cited_mass": block_mass(block),
            "displaced": _rid(victim),
            "partners": sorted({u for t in joined for u in t["units"] if u != unit}),
            "signal_ids": sorted({s for t in joined for s in t["signal_ids"]}),
            "action": "promoted",
        })

    # The published order: the carried set in the pure order, then the rest —
    # the subject-group floor's construction, kept (``assembly_floor``).
    carried.sort(key=lambda r: pure.get(_rid(r), 10**6))
    choices, dupes = _choose(
        carried, forced, reasons, exclude={_fid(b) for b in extras.values()},
    )
    # A thematic row the FINAL pass declined (its block went to a forced pick)
    # is an empty slot too — never a silent ordinal-1 carry.
    late = [r for r in carried if child_assembly(r) is not None and _rid(r) not in choices]
    empty.extend(late)
    empty_ids |= {_rid(r) for r in late}
    carried = [r for r in carried if _rid(r) not in empty_ids]
    # N-T — the same void for a group-floor promotion the FINAL pass declined:
    # restore a displaced prefix row in its place when one is still uncarried.
    late_void = [_rid(r) for r in late if _rid(r) in promoted_by_group_floor]
    if late_void and base:
        carried_now = {_rid(r) for r in carried}
        pool = sorted(
            (r for r in order
             if _rid(r) in {str(x) for x in base.get("displaced") or []}
             and _rid(r) not in carried_now and _rid(r) not in empty_ids),
            key=lambda r: pure.get(_rid(r), 10**6),
        )
        restore = pool[: len(late_void)]
        base["promoted"] = [x for x in base.get("promoted") or [] if x not in set(late_void)]
        base["voided"] = list(base.get("voided") or []) + [
            {"promoted": v, "why": EMPTY_SLOT_WHY} for v in late_void
        ]
        base["restored"] = list(base.get("restored") or []) + [_rid(r) for r in restore]
        if restore:
            carried = sorted(carried + restore, key=lambda r: pure.get(_rid(r), 10**6))
            choices, dupes = _choose(
                carried, forced, reasons, exclude={_fid(b) for b in extras.values()},
            )
            dropped = [
                r for r in carried if child_assembly(r) is not None and _rid(r) not in choices
            ]
            empty.extend(dropped)
            empty_ids |= {_rid(r) for r in dropped}
            carried = [r for r in carried if _rid(r) not in empty_ids]
    carried_ids = {_rid(r) for r in carried}
    rest = [r for r in order if _rid(r) not in carried_ids]
    rest.sort(key=lambda r: pure.get(_rid(r), 10**6))
    for row in order:
        # Stamped on the carried, CLEARED on the rest: a row object that
        # outlives this run must not carry a stale choice into the next.
        if _rid(row) in choices and _rid(row) in carried_ids:
            row[WORLD_CARRY_ROW_KEY] = choices[_rid(row)]  # type: ignore[index]
        else:
            row.pop(WORLD_CARRY_ROW_KEY, None)  # type: ignore[attr-defined]

    # The floor's ADDED block rides a COPY of its unit's row, placed right after
    # it: ordinal N stays ``derived_from`` entry N (the copy's id IS the unit's
    # row, which the block came through), and the copy carries no cited mass of
    # its own (its evidence is already the original's).
    published: list[Mapping[str, Any]] = []
    extra_flags: list[bool] = []
    for r in carried:
        published.append(r)
        extra_flags.append(False)
        b = extras.get(_rid(r))
        if b is not None:
            copy = dict(r)
            copy[WORLD_CARRY_ROW_KEY] = (b, int(b.get("ordinal") or 1), CARRY_RESTRAINT_SHIFT)
            copy[FLOOR_EXTRA_ROW_KEY] = True
            copy[CITED_SALIENCE_ROW_KEY] = {
                "cited_max": 0.0, "cited_mass": 0.0, "n_above": 0, "n_cited_scored": 0,
                "n_cited": 0, "source": "floor_added_block", "version": "n/a",
            }
            published.append(copy)
            extra_flags.append(True)

    ledger = _recount(
        base, published, displaced + thread_displaced,
        empty_groups={str(r.get(SUBJECT_GROUP_ROW_KEY) or "") for r in empty},
        extra_groups=[
            str(r.get(SUBJECT_GROUP_ROW_KEY) or "")
            for r, x in zip(published, extra_flags) if x
        ],
    ) if base else None
    plan = {
        "version": CARRY_PLAN_VERSION,
        "order": "cited_mass_first",
        "duplicates_unavoidable": dupes,
        "restraint_floor": {
            "slots": int(slots),
            "desks": list(RESTRAINT_SHIFT_DESKS),
            "rule": POLITICS_FLOOR_RULE,
            "eligible": len(eligible),
            "already_carried": already,
            "picks": picks,
            "swapped": swapped,
            "added": added,
            "promoted": promoted,
            "displaced": displaced,
            "unplaced": unplaced,
        },
        "thread_slot": {
            "version": THREAD_VERSION,
            "slots": int(thread_slots),
            "magnitude_floor": THREAD_MAGNITUDE_FLOOR,
            "threads_found": len(threads),
            "picks": thread_picks,
            "unplaced": thread_unplaced,
        },
        "empty_slots": [
            {
                "via_head_id": _rid(r), "unit": _unit(r), "why": EMPTY_SLOT_WHY,
                "group_id": str(r.get(SUBJECT_GROUP_ROW_KEY) or "") or None,
            }
            for r in empty
        ],
        # PRIVATE — the join material ``stamp_world_carry`` needs.
        "_carried_ids": [_rid(r) for r in published],
        "_units": [_unit(r) for r in published],
        "_extra_flags": extra_flags,
        "_threads": threads[:THREAD_LINES_MAX],
        "_thread_ids": [p["via_head_id"] for p in thread_picks],
        "_empty_ids": sorted(empty_ids),
    }
    return WorldCarryPlan(
        selection=FloorSelection(order=published + rest, carried=published, ledger=ledger),
        ledger=plan,
    )


def _recount(
    base: dict[str, Any], carried: Sequence[Mapping[str, Any]], displaced: Sequence[str],
    *, empty_groups: set[str] | frozenset[str] = frozenset(),
    extra_groups: Sequence[str] = (),
) -> dict[str, Any]:
    """The subject-group ledger re-counted over the plan's carried set: group
    carried counts, ``_carried_ids``, and the group floor's own ``displaced``
    minus any row the plan carried after all.

    N-C — a group left at zero BECAUSE its slot was declared empty (rule 5) is
    published under ``declared_empty`` and does not break ``honoured``: the floor
    guaranteed the group a slot, and the slot is stated as empty rather than
    filled with content that is not the group's. ARM 4(c) F2 cross-checks the
    list against ``carry_plan.empty_slots``."""
    out = dict(base)
    ids = [_rid(r) for r in carried]
    counts: dict[str, int] = {}
    for r in carried:
        g = str(r.get(SUBJECT_GROUP_ROW_KEY) or "")
        counts[g] = counts.get(g, 0) + 1
    # A floor-ADDED block is one more candidate entry of its group (a copy of
    # the unit's row in the published order), so it is offered as well as carried.
    more: dict[str, int] = {}
    for g in extra_groups:
        more[g] = more.get(g, 0) + 1
    groups = [
        dict(
            g, carried=counts.get(str(g.get("group_id")), 0),
            offered=int(g.get("offered") or 0) + more.get(str(g.get("group_id")), 0),
        )
        for g in base.get("groups") or []
    ]
    out["groups"] = groups
    declared = sorted(
        str(g.get("group_id")) for g in groups
        if g["carried"] < 1 and str(g.get("group_id")) in empty_groups
    )
    if declared:
        out["declared_empty"] = declared
    out["honoured"] = all(
        g["carried"] >= 1 for g in groups
        if int(g.get("offered") or 0) >= 1 and str(g.get("group_id")) not in declared
    )
    out["displaced"] = [x for x in base.get("displaced") or [] if x not in set(ids)]
    out["_carried_ids"] = ids
    return out


def stamp_world_carry(
    payload: dict[str, Any], plan: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Write the plan onto an ``assembly.v1`` payload — a pure post-pass, the
    ``stamp_floor`` shape. ``None`` returns ``payload`` untouched.

    Adds ``via_unit`` on every carried block, ``carried_by_restraint_floor`` on
    a block the floor placed, ``displaced_by_restraint_floor`` on a drop row it
    displaced, and the audit line at :data:`CARRY_PLAN_KEY`.
    """
    if not plan:
        return payload
    ids: Sequence[str] = plan.get("_carried_ids") or []
    units: Sequence[str] = plan.get("_units") or []
    blocks = [dict(b) for b in payload.get("blocks") or []]
    if len(blocks) != len(ids):
        raise ValueError(
            f"stamp_world_carry: {len(blocks)} blocks for {len(ids)} carried "
            f"candidates — the payload was not built from this plan"
        )
    floor = plan.get("restraint_floor") or {}
    placed = {str(x) for x in (floor.get("swapped") or []) + (floor.get("promoted") or [])}
    displaced = {str(x) for x in floor.get("displaced") or []}
    by_thread = {str(x) for x in plan.get("_thread_ids") or []}
    thread_displaced = {
        str(p.get("displaced")) for p in (plan.get("thread_slot") or {}).get("picks") or []
    }
    empty = {str(x) for x in plan.get("_empty_ids") or []}
    flags = list(plan.get("_extra_flags") or [False] * len(ids))
    promoted = {str(x) for x in (floor.get("swapped") or []) + (floor.get("promoted") or [])}
    for block, rid, unit, extra in zip(blocks, ids, units, flags):
        block[VIA_UNIT_KEY] = unit
        if extra or (rid in promoted and rid in placed):
            block["carried_by_restraint_floor"] = True
        if extra:
            block["added_by_restraint_floor"] = True
        if rid in by_thread and not extra:
            block["carried_by_thread"] = True
    out = dict(payload)
    out["blocks"] = blocks
    drops = dict(payload.get("drops") or {})
    for grain in ("shown_not_carried", "not_selected"):
        rows = []
        for d in drops.get(grain) or []:
            row = dict(d)
            fid = str(row.get("finding_id") or "")
            if fid in displaced:
                row["displaced_by_restraint_floor"] = True
            if fid in thread_displaced:
                row["displaced_by_thread"] = True
            if fid in empty:
                row["declared_empty_slot"] = True
            rows.append(row)
        drops[grain] = rows
    out["drops"] = drops
    out[CARRY_PLAN_KEY] = {k: v for k, v in plan.items() if not str(k).startswith("_")}
    # N-C — THE SHARED THREADS, each naming the carried blocks that cite it.
    threads = []
    for t in plan.get("_threads") or []:
        sids = set(t.get("signal_ids") or [])
        threads.append({
            **{k: v for k, v in t.items()},
            "block_ordinals": [
                int(b.get("ordinal") or 0) for b in blocks
                if sids & {str(s.get("signal_id")) for s in b.get("signals") or []
                           if isinstance(s, Mapping)}
            ],
        })
    if threads:
        out[THREADS_KEY] = threads
    return out
