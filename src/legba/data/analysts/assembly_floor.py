# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""THE SUBJECT-GROUP FLOOR — one block per subject group on the world read,
built to pass the strict-prefix arm (W4 F-3; design note
``planning/v2/design/F-3_WORLD_FLOOR.md``).

THE DEFECT. The world cut (``assembly_cap.select_carried``) is a strict
``WORLD_BLOCK_CAP`` prefix of ``assembly_payload.order_key``. On every measured
world read the cap bit (33 offered, 16 carried), and on two of seven a whole
subject group carried NOTHING — its best block ranked 17th or 19th against a
cap of 16 — over a basis in which every group was present. A cap over a full
basis then read as a hole.

WHY THE NAIVE FLOOR WAS REFUSED. Promoting a group's below-cap best into the
carried list while publishing the drop ledger in the pure order makes a
not-selected row rank at or above the carried count, and D-3's ARM 4(b)
(``assembly_arms._prefix_violations``) hard-fails ``drop_order_violation``.

THE DESIGN: THE FLOOR SELECTS, THE PREFIX FILLS, THE PUBLISHED ORDER IS
REBUILT AROUND THE SELECTION.

  1. ``ordered`` = the candidates in ``order_key`` — the PURE order;
     ``order_rank`` is the 1-based position in it.
  2. The floor set F = each group's first row in ``ordered`` (``order_key`` is
     total, so the tie-break is the order's own).
  3. ``cap_eff = max(cap, |F|)`` — the floor is a GUARANTEE (decision D1): a cap
     below the group count grows to it rather than silently dropping a group.
  4. The fill = the first ``cap_eff - |F|`` rows of ``ordered`` not in F.
  5. ``carried`` = F + fill, in ``ordered`` order; the PUBLISHED order (what
     ``build_assembly`` ranks and ``derived_from`` is minted from) is
     ``carried + rest``, the rest also in ``ordered`` order.

So ARM 4(b) passes UNCHANGED (every not-selected row's published rank is above
the carried count), ordinal 1 is still the pure order's head, a promoted block
prints LAST among the carried, and when the floor does not bite the selection
and the published order are byte-identical to the pure prefix. The prefix
check is now trivially true on the world tier, so the floor's own identities
(F1-F4) carry the weight: ``legba.data.provenance.assembly_floor_arm``.

INERT OFF THE WORLD READ. ``apply_floor=False`` (country, thematic, region) is
``sorted(order_key)`` + ``select_carried`` and ``ledger=None``, and
:func:`stamp_floor` with ``ledger=None`` returns the payload unchanged —
byte-identical. A world slice in which ANY candidate carries no group stamp
(the no-roster topology, ``composition_slice._assemble_world_country_slice``)
degrades to the pure order the same way: a floor over a partial grouping would
publish group arithmetic that does not add up to the candidates.

PLATFORM, NOT EXEMPLAR. The mechanism is a floor over SUBJECT GROUPS; the
geopolitics instance's groups are its region frames (plus the cross-region
thematic lane and the ``unassigned`` bucket, decision D5), stamped by
``composition_slice`` as ``_region_id``. The grain word the reader sees is the
roster tag (``composition_slice.REGION_FRAME_TAG``), which is data.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping, NamedTuple, Sequence

from .assembly_cap import BLOCK_CAP, TIER_WORLD, WORLD_BLOCK_CAP, select_carried
from .assembly_payload import order_key
from .composition_slice import REGION_FRAME_TAG

__all__ = [
    "DEFAULT_FLOOR_STALE_HOURS",
    "FLOOR_PER_GROUP",
    "FLOOR_STALE_HOURS_OPTION",
    "FLOOR_VERSION",
    "FloorSelection",
    "REASON_IN_PREFIX",
    "REASON_KEPT_BY_FLOOR",
    "REASON_NO_CANDIDATE",
    "SUBJECT_GROUP_COVERAGE_KEY",
    "SUBJECT_GROUP_ROW_KEY",
    "floor_select",
    "floor_stale_hours",
    "floor_step",
    "stamp_floor",
]

#: The row stamp naming a candidate's subject group
#: (``composition_slice._assemble_world_country_slice`` writes it on every
#: world candidate: a region frame id, ``thematic:<analyst>`` or ``unassigned``).
SUBJECT_GROUP_ROW_KEY: str = "_region_id"

#: The denormalized group roster the slice stamps beside it — one entry per
#: group with its display name and its input count, gaps included.
SUBJECT_GROUP_COVERAGE_KEY: str = "_region_coverage"

#: The version of the audit line persisted at ``data.data.assembly.floor``.
#: Decision D3: the floor is versioned HERE, and ``connective.v1`` does not move.
FLOOR_VERSION: str = "subject_group_floor.v1"

#: Blocks guaranteed per group.
FLOOR_PER_GROUP: int = 1

#: Decision D4 — a block the floor KEPT whose evidence is older than this is
#: stamped ``floor_stale`` so the reader says "not fresh" beside it. Settable
#: per descriptor (``method.options.world_floor_stale_hours``, declared in
#: ``handler_options_catalog``); measured carried world-block age p90 16.5 h.
FLOOR_STALE_HOURS_OPTION: str = "world_floor_stale_hours"
DEFAULT_FLOOR_STALE_HOURS: float = 72.0

#: ``floor.groups[].reason`` — a closed set.
REASON_IN_PREFIX: str = "in_prefix"
REASON_KEPT_BY_FLOOR: str = "kept_by_floor"
REASON_NO_CANDIDATE: str = "no_candidate"


class FloorSelection(NamedTuple):
    """``order`` is the PUBLISHED order (``derived_from`` is minted from it);
    ``carried`` is its prefix of length ``len(carried)``; ``ledger`` is the
    floor's audit material for :func:`stamp_floor`, or ``None`` when no floor
    applied (then ``order`` is the pure order and ``carried`` the pure prefix).
    """

    order: list[Mapping[str, Any]]
    carried: list[Mapping[str, Any]]
    ledger: dict[str, Any] | None


def _resolved_cap(tier: str, cap: int | None) -> int:
    if cap is not None:
        return int(cap)
    return WORLD_BLOCK_CAP if str(tier) == TIER_WORLD else BLOCK_CAP


def _row_id(row: Mapping[str, Any]) -> str:
    return str(row.get("id") or "")


def _group_names(rows: Sequence[Mapping[str, Any]]) -> list[tuple[str, str]]:
    """The slice's group roster, in ITS order: ``[(group_id, group_name)]``.

    Read off the first row that carries one (the slice denormalizes the same
    list onto every row). Absent ⇒ ``[]`` and the ledger lists only the groups
    that arrived, named by id.
    """
    for row in rows:
        cov = row.get(SUBJECT_GROUP_COVERAGE_KEY)
        if isinstance(cov, (list, tuple)) and cov:
            out: list[tuple[str, str]] = []
            for c in cov:
                if not isinstance(c, Mapping):
                    continue
                gid = str(c.get("region_id") or "").strip()
                if gid:
                    out.append((gid, str(c.get("region_name") or gid)))
            return out
    return []


def floor_select(
    candidates: Sequence[Mapping[str, Any]],
    *,
    tier: str,
    apply_floor: bool,
    cap: int | None = None,
    key: Callable[[Mapping[str, Any]], Any] = order_key,
) -> FloorSelection:
    """Select the carried set — the pure prefix, or the floor + prefix fill.

    See the module banner for the five steps. ``apply_floor`` is the caller's
    decision (the WORLD composition, not a region run); a candidate set in which
    any row lacks a group stamp degrades to the pure order whatever it says.
    ``key`` is the PURE order (``order_key``; the world read passes
    ``assembly_world.world_order_key``, W1).
    """
    ordered = sorted(candidates, key=key)
    resolved_cap = _resolved_cap(tier, cap)
    groups = [str(r.get(SUBJECT_GROUP_ROW_KEY) or "").strip() for r in ordered]
    if not apply_floor or not ordered or not all(groups):
        return FloorSelection(
            order=ordered,
            carried=select_carried(ordered, tier=tier, cap=cap),
            ledger=None,
        )

    pure_rank = {id(r): i + 1 for i, r in enumerate(ordered)}
    best: dict[str, Mapping[str, Any]] = {}
    offered: dict[str, int] = {}
    for row, gid in zip(ordered, groups):
        offered[gid] = offered.get(gid, 0) + 1
        best.setdefault(gid, row)
    floor_ids = {id(r) for r in best.values()}
    cap_eff = max(resolved_cap, len(floor_ids))
    fill = [r for r in ordered if id(r) not in floor_ids][: cap_eff - len(floor_ids)]
    carried_ids = floor_ids | {id(r) for r in fill}
    carried = [r for r in ordered if id(r) in carried_ids]
    rest = [r for r in ordered if id(r) not in carried_ids]

    promoted = [r for r in ordered if id(r) in floor_ids and pure_rank[id(r)] > resolved_cap]
    displaced = [r for r in ordered[:resolved_cap] if id(r) not in carried_ids]
    carried_count: dict[str, int] = {}
    for row, gid in zip(ordered, groups):
        if id(row) in carried_ids:
            carried_count[gid] = carried_count.get(gid, 0) + 1

    roster = _group_names(ordered)
    names = dict(roster)
    ordered_gids = [gid for gid, _ in roster] + sorted(
        (g for g in best if g not in names), key=lambda g: pure_rank[id(best[g])]
    )
    promoted_ids = {id(r) for r in promoted}
    group_rows: list[dict[str, Any]] = []
    for gid in ordered_gids:
        n_offered = offered.get(gid, 0)
        head = best.get(gid)
        if head is None or n_offered == 0:
            reason = REASON_NO_CANDIDATE
        elif id(head) in promoted_ids:
            reason = REASON_KEPT_BY_FLOOR
        else:
            reason = REASON_IN_PREFIX
        group_rows.append({
            "group_id": gid,
            "group_name": names.get(gid, gid),
            "offered": n_offered,
            "carried": carried_count.get(gid, 0),
            "best_order_rank": pure_rank[id(head)] if head is not None else None,
            "reason": reason,
        })

    ledger = {
        "version": FLOOR_VERSION,
        "grain": REGION_FRAME_TAG,
        "per_group": FLOOR_PER_GROUP,
        "cap": resolved_cap,
        "cap_effective": cap_eff,
        "honoured": all(
            g["carried"] >= FLOOR_PER_GROUP for g in group_rows if g["offered"] >= 1
        ),
        "promoted": [_row_id(r) for r in promoted],
        "displaced": [_row_id(r) for r in displaced],
        "groups": group_rows,
        # PRIVATE — the join material ``stamp_floor`` needs and does not persist.
        "_order_rank": {_row_id(r): pure_rank[id(r)] for r in ordered},
        "_carried_ids": [_row_id(r) for r in carried],
    }
    return FloorSelection(order=carried + rest, carried=carried, ledger=ledger)


def floor_stale_hours(options: Mapping[str, Any] | None) -> float:
    """Decision D4's bar, option-resolved: the descriptor's
    ``world_floor_stale_hours`` when it is a positive number, else 72."""
    raw = (options or {}).get(FLOOR_STALE_HOURS_OPTION)
    if isinstance(raw, (int, float)) and not isinstance(raw, bool) and raw > 0:
        return float(raw)
    return DEFAULT_FLOOR_STALE_HOURS


def stamp_floor(
    payload: dict[str, Any],
    ledger: Mapping[str, Any] | None,
    *,
    stale_hours: float = DEFAULT_FLOOR_STALE_HOURS,
) -> dict[str, Any]:
    """Write the floor's audit line onto an ``assembly.v1`` payload — a pure
    post-pass, the ``merge_contrary_tension`` shape.

    ``ledger=None`` returns ``payload`` itself, untouched. Otherwise returns a
    copy with:

      * ``floor`` — the audit line (``version`` … ``groups``, plus the stale
        bar it applied);
      * ``order_rank`` on every carried block and every ``shown_not_carried`` /
        ``not_selected`` row — the row's position in the PURE order, beside the
        published ``rank``;
      * ``carried_by_floor: true`` on a promoted block, and ``floor_stale: true``
        on one whose ``evidence_age_h`` exceeds ``stale_hours`` (an unknown age
        is never stale — it is printed as unknown);
      * ``displaced_by_floor: true`` on a row the floor displaced.

    The three flags are written only on the rows they are true of.
    """
    if not ledger:
        return payload
    order_rank: Mapping[str, int] = ledger.get("_order_rank") or {}
    carried_ids: Sequence[str] = ledger.get("_carried_ids") or []
    promoted = {str(x) for x in ledger.get("promoted") or []}
    displaced = {str(x) for x in ledger.get("displaced") or []}

    out = dict(payload)
    blocks = [dict(b) for b in payload.get("blocks") or []]
    if len(blocks) != len(carried_ids):
        raise ValueError(
            f"stamp_floor: {len(blocks)} blocks for {len(carried_ids)} carried "
            f"candidates — the payload was not built from this selection"
        )
    for block, cid in zip(blocks, carried_ids):
        block["order_rank"] = order_rank.get(cid)
        if cid in promoted:
            block["carried_by_floor"] = True
            age = block.get("evidence_age_h")
            if isinstance(age, (int, float)) and not isinstance(age, bool) and age > stale_hours:
                block["floor_stale"] = True
    out["blocks"] = blocks

    drops = dict(payload.get("drops") or {})
    for grain in ("shown_not_carried", "not_selected"):
        rows = []
        for d in drops.get(grain) or []:
            row = dict(d)
            fid = str(row.get("finding_id") or "")
            row["order_rank"] = order_rank.get(fid)
            if fid in displaced:
                row["displaced_by_floor"] = True
            rows.append(row)
        drops[grain] = rows
    out["drops"] = drops

    floor = {k: v for k, v in ledger.items() if not str(k).startswith("_")}
    floor["stale_hours"] = float(stale_hours)
    out["floor"] = floor
    return out


def floor_step(ledger: Mapping[str, Any]) -> dict[str, Any]:
    """The one steps receipt: groups, honoured, promoted, displaced."""
    return {
        "phase": "floor",
        "kind": FLOOR_VERSION,
        "groups": len(ledger.get("groups") or []),
        "honoured": bool(ledger.get("honoured")),
        "promoted": len(ledger.get("promoted") or []),
        "displaced": len(ledger.get("displaced") or []),
    }
