# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""THE TIER + THE CAP — ``assembly_payload``'s tier enum and the carry-cut it
selects on, split out to keep the module under the size-gate's 1,500-line
entry threshold (``tests/test_module_size_gate.py``). Imported ONE WAY and
re-exported by :mod:`assembly_payload`, same idiom as ``assembly_carry`` and
``assembly_spans`` next door: this leaf is invisible to every importer.

CONSULT_REVIEW_2026-09-28 §B.5 is why :data:`WORLD_BLOCK_CAP` and
:func:`select_carried` exist at all — see that function's docstring for the
tier-aware cap and the documented (not shipped) per-region floor.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

TIER_COUNTRY: str = "country"
TIER_WORLD: str = "world"
TIER_THEMATIC: str = "thematic"

#: D-1 §F-10. Eight reads as a page rather than a wall; live country reads
#: already carry 7.1 citations on average, so this changes almost nothing at the
#: country tier and is a real cut at the world tier (~32 candidates).
BLOCK_CAP: int = 8

#: CONSULT_REVIEW_2026-09-28 §B.5 — the WORLD tier's OWN cap. ``BLOCK_CAP`` was
#: never tier-aware: a flat 8 over a ~32-candidate world pool is the cut that
#: produced eea5b823 (``derived_from`` 33, ``carried`` 8, ``not_selected`` 25)
#: with no guarantee any given region survived it — "8 of 33 absent" read as a
#: hole where it was a cap over a full basis (§C), but a region that lost every
#: one of its candidates to the order still reads as though it said nothing.
#: Doubled rather than left flat: 16 blocks at the live ~7 citations/block is
#: the country tier's own budget, just twice over, still a page. See
#: :func:`select_carried`, which backs this cap with a per-region floor of 1.
WORLD_BLOCK_CAP: int = 16


def select_carried(
    candidates: Sequence[Mapping[str, Any]],
    *,
    tier: str,
    cap: int | None = None,
) -> list[Mapping[str, Any]]:
    """CONSULT_REVIEW_2026-09-28 §B.5 — the tier-aware carry cut, called by the
    caller BEFORE ``assembly_payload.build_assembly`` (which still just takes
    whatever ``carried`` it is handed; this is the one place that decides what
    that is).

    W4 F-3: the per-subject-group floor described below as not shippable here
    now ships in ``assembly_floor.floor_select``, which keeps the strict-prefix
    property by rebuilding the PUBLISHED order around the selection and calls
    this function for every tier and topology the floor does not apply to.
    The text below is the record of why the naive version was refused.

    ``candidates`` is already in ``order_key`` order — the caller's job,
    unchanged. COUNTRY and THEMATIC keep the flat :data:`BLOCK_CAP` (8): a
    strict ``candidates[:BLOCK_CAP]`` prefix, byte-identical to the cut that
    shipped. WORLD widens the cap to :data:`WORLD_BLOCK_CAP` (16), also a
    strict prefix.

    CAP ONLY — NO PER-REGION FLOOR, and that is a deliberate finding, not an
    unfinished stub (§B.5 authorizes shipping the cap alone when the floor
    cannot be done in ~40 lines). A per-region floor of 1 ("every region with a
    candidate keeps its top one even below the cap line") is buildable here —
    ``composition_slice`` already stamps ``_region_id`` on every world-tier
    candidate row (``_assemble_world_country_slice``,
    ``composition_slice.py::1204/1208``) — but it CANNOT be a strict prefix of
    the order by construction: promoting a region's below-cap candidate always
    means some higher-ranked candidate from an over-represented region is
    either bumped or the carried count grows past a contiguous ``1..N`` run.
    Either way, some ``not_selected`` entry then ranks ABOVE (or at) a carried
    block's own rank.

    That is not a cosmetic property here. It is ``assembly_payload``'s own
    FIRST invariant (that module's banner, "selection is a STRICT PREFIX of
    the order") and it ships as a HARD-FAIL production audit: D-3's ARM 4(b)
    (``legba.data.provenance.assembly_arms._drop_disclosure`` /
    ``_prefix_violations``, wired live off ``verify.py``) reads every
    persisted ``not_selected`` entry's ``rank`` against ``len(blocks)`` and
    fires ``drop_order_violation`` (severity ``hard_fail``) the moment a
    not-selected candidate ranks at or above the carried count — which a
    region-floor promotion does BY DESIGN. Relaxing that arm for a
    now-legitimate non-prefix selection is a real change (the arm's own
    docstring, ``_prefix_violations``, and its dedicated
    ``test_assembly_arms_d3.py`` coverage), in a file this lane was not asked
    to touch. Shipping the floor without that companion change would pass
    every test in this lane's own KEEP-GREEN list while silently hard-failing
    faithfulness verification on every world read that uses it — worse than
    not shipping it. See the lane report for the full account.
    """
    resolved_cap = int(
        cap if cap is not None
        else (WORLD_BLOCK_CAP if str(tier) == TIER_WORLD else BLOCK_CAP)
    )
    return list(candidates[:resolved_cap])
