# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""ARC F-3 (W3 design lane) — the per-subject-group floor on the world read,
pinned BEFORE the build so the build lane starts from proven ground.

Three things are pinned here, and none of them touches ``src/``:

  1. TODAY'S BEHAVIOUR. ``select_carried`` at the world tier is a strict
     ``WORLD_BLOCK_CAP`` prefix of ``order_key`` with no floor, so a group whose
     best candidate ranks below the cap carries NOTHING. The fixture is the live
     shape of world read 2933f773 (2026-10-02 00:20Z): 33 candidates in six
     groups, the Americas' best at order rank 17 — carried 0 of 6.
  2. WHY THE NAIVE FLOOR WAS NOT SHIPPED. Promoting the Americas' best into the
     carried list while keeping the drop ledger ranked by ``order_key`` makes a
     not-selected candidate rank at or above the carried count, and D-3's ARM
     4(b) fires ``drop_order_violation`` (hard) — the interim's own finding
     (``assembly_cap.select_carried`` docstring).
  3. THE DESIGN, as a test-local reference (``_floor_select``). The floor
     SELECTS (each group's ``order_key``-best), the strict prefix FILLS the rest
     of the cap in ``order_key``, and the PUBLISHED order is the carried set in
     ``order_key`` followed by the rest in ``order_key``. ARM 4(b) passes
     UNCHANGED; when the floor does not bite the selection is byte-identical to
     ``candidates[:cap]``; rows with no group stamp degrade to the pure order.
     The build lane (W4 F-3) replaced ``_floor_select`` with the shipped
     function (``assembly_floor.floor_select``) and these assertions hold.
"""

from __future__ import annotations

from typing import Any

from legba.data.analysts import assembly_payload as ap
from legba.data.provenance import assembly_arms as AA

GROUP_KEY = "_region_id"

BODY = (
    "**BLUF:** A coordinated escalation narrative is emerging that links "
    "recent strikes to retaliatory messaging [1].\n"
)

#: World read 2933f773 (2026-10-02 00:20Z), read-only SELECT, in order rank.
#: (rank, unit, subject group). All 33 sit in one severity band in this
#: fixture; mass falls with rank so ``order_key`` reproduces the live ranks.
LIVE_2933F773: tuple[tuple[int, str, str], ...] = (
    (1, "escalation_composition", "thematic:escalation_composition"),
    (2, "country_watch_ua", "region_europe"),
    (3, "country_g20_sa", "region_mena"),
    (4, "country_watch_ir", "region_mena"),
    (5, "country_g20_ru", "region_europe"),
    (6, "country_watch_pk", "region_indo_pacific"),
    (7, "country_watch_il", "region_mena"),
    (8, "country_g20_gb", "region_europe"),
    (9, "country_watch_mm", "region_indo_pacific"),
    (10, "country_g20_cn", "region_indo_pacific"),
    (11, "country_g20_fr", "region_europe"),
    (12, "country_g20_za", "region_africa"),
    (13, "country_watch_sd", "region_africa"),
    (14, "country_g20_kr", "region_indo_pacific"),
    (15, "country_watch_kp", "region_indo_pacific"),
    (16, "country_watch_ml", "region_africa"),
    (17, "country_g20_ca", "region_americas"),
    (18, "country_g20_id", "region_indo_pacific"),
    (19, "country_g20_us", "region_americas"),
    (20, "country_watch_ht", "region_americas"),
    (21, "country_g20_in", "region_indo_pacific"),
    (22, "country_watch_ne", "region_africa"),
    (23, "country_g20_au", "region_indo_pacific"),
    (24, "country_g20_jp", "region_indo_pacific"),
    (25, "country_g20_ar", "region_americas"),
    (26, "country_g20_mx", "region_americas"),
    (27, "country_watch_cd", "region_africa"),
    (28, "country_watch_tw", "region_indo_pacific"),
    (29, "country_g20_de", "region_europe"),
    (30, "country_g20_tr", "region_mena"),
    (31, "country_g20_it", "region_europe"),
    (32, "country_g20_br", "region_americas"),
    (33, "country_watch_bf", "region_africa"),
)


def _rows(shape=LIVE_2933F773, *, grouped: bool = True) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for rank, unit, group in shape:
        mass = round(4.0 - rank * 0.1, 2)
        row: dict[str, Any] = {
            "id": f"{rank:02d}-{unit}",
            "analyst_id": "country_composition",
            "target_id": unit,
            "title": f"read {unit}",
            "body": BODY,
            "severity": "high",
            "produced_at": "2026-10-02T00:00:00+00:00",
            "faithfulness_score": 0.9,
            "effective_confidence": 0.8,
            "data": {"data": {"citations": []}},
            ap.CITED_SALIENCE_ROW_KEY: {
                "cited_max": mass, "cited_mass": mass, "n_above": 0,
                "n_cited_scored": 1, "n_cited": 1, "source": "fixture",
                "version": "fixture",
            },
        }
        if grouped:
            row[GROUP_KEY] = group
        rows.append(row)
    return rows


def _groups(rows) -> dict[str, int]:
    out: dict[str, int] = {}
    for r in rows:
        g = r.get(GROUP_KEY)
        if g:
            out[g] = out.get(g, 0) + 1
    return out


def _floor_select(candidates, *, cap: int):
    """W4 F-3 BUILD: the reference is now the SHIPPED function,
    ``assembly_floor.floor_select`` (world tier, floor on), adapted to this
    file's ``(published_order, carried, promoted_ids)`` shape. Every assertion
    below was written against the design lane's test-local reference and holds
    unchanged against the real one.
    """
    from legba.data.analysts import assembly_floor as af

    sel = af.floor_select(candidates, tier=ap.TIER_WORLD, apply_floor=True, cap=cap)
    promoted = set((sel.ledger or {}).get("promoted") or [])
    return sel.order, sel.carried, promoted


def _payload(order, carried):
    return ap.build_assembly(
        tier=ap.TIER_WORLD,
        as_of="2026-10-02T00:20:00+00:00",
        candidates=order,
        carried=carried,
    )


def _reasons(payload) -> list[str]:
    return [f.reason for f in AA.selection_honesty(payload).findings]


# ---------------------------------------------------------------------------
# 1. TODAY — the world cut is a pure prefix and can carry a group at zero
# ---------------------------------------------------------------------------


def test_today_the_world_cut_is_a_pure_order_key_prefix() -> None:
    ordered = sorted(_rows(), key=ap.order_key)
    carried = ap.select_carried(ordered, tier=ap.TIER_WORLD)
    assert carried == ordered[: ap.WORLD_BLOCK_CAP]
    assert [r["id"] for r in ordered] == [
        f"{rank:02d}-{unit}" for rank, unit, _ in LIVE_2933F773
    ], "the fixture must reproduce the live order ranks"


def test_today_the_2933f773_shape_carries_the_americas_at_zero() -> None:
    """The defect, measured: 6 groups offered, 5 carried; the Americas offered
    6 candidates and carried none (best order rank 17 against a cap of 16)."""
    ordered = sorted(_rows(), key=ap.order_key)
    carried = ap.select_carried(ordered, tier=ap.TIER_WORLD)
    offered = _groups(ordered)
    kept = _groups(carried)
    assert len(offered) == 6
    assert offered["region_americas"] == 6
    assert "region_americas" not in kept
    assert len(kept) == 5
    payload = _payload(ordered, carried)
    assert _reasons(payload) == []  # today passes ARM 4(b) — with a hole


# ---------------------------------------------------------------------------
# 2. WHY THE NAIVE FLOOR WAS REFUSED — it hard-fails ARM 4(b)
# ---------------------------------------------------------------------------


def test_a_naive_promotion_over_the_pure_order_trips_drop_order_violation() -> None:
    ordered = sorted(_rows(), key=ap.order_key)
    carried = ordered[: ap.WORLD_BLOCK_CAP - 1] + [ordered[16]]  # CA in, ML out
    assert carried[-1]["target_id"] == "country_g20_ca"
    payload = _payload(ordered, carried)
    assert AA.DROP_ORDER_VIOLATION in _reasons(payload)


# ---------------------------------------------------------------------------
# 3. THE DESIGN — floor selects, prefix fills, published order keeps the arm
# ---------------------------------------------------------------------------


def test_the_floor_select_carries_every_group_and_passes_arm_4b_unchanged() -> None:
    order, carried, promoted = _floor_select(_rows(), cap=ap.WORLD_BLOCK_CAP)
    assert len(carried) == ap.WORLD_BLOCK_CAP
    assert set(_groups(carried)) == set(_groups(order))  # 6 of 6
    assert promoted == {"17-country_g20_ca"}
    # The one block the floor costs: the cut's last pure-prefix row.
    assert "16-country_watch_ml" not in {r["id"] for r in carried}
    payload = _payload(order, carried)
    assert _reasons(payload) == []
    ranks = [d["rank"] for d in payload["drops"]["not_selected"]]
    assert min(ranks) == ap.WORLD_BLOCK_CAP + 1
    # Ordinal 1 is still the pure order's head (lead v1 crowns ordinal 1).
    assert payload["blocks"][0]["finding_id"] == "01-escalation_composition"
    # Carried blocks print in order_key order: the promoted block is last.
    assert payload["blocks"][-1]["finding_id"] == "17-country_g20_ca"


def test_the_floor_select_is_byte_identical_when_the_floor_does_not_bite() -> None:
    """Every group's best already inside the prefix -> exactly today's cut."""
    shape = tuple(row for row in LIVE_2933F773 if row[2] != "region_americas")
    rows = _rows(shape)
    ordered = sorted(rows, key=ap.order_key)
    order, carried, promoted = _floor_select(rows, cap=ap.WORLD_BLOCK_CAP)
    assert promoted == set()
    assert [r["id"] for r in carried] == [
        r["id"] for r in ap.select_carried(ordered, tier=ap.TIER_WORLD)
    ]
    assert [r["id"] for r in order] == [r["id"] for r in ordered]


def test_rows_without_a_group_stamp_degrade_to_the_pure_order() -> None:
    """The no-roster topology (``composition_slice`` returns the rows
    unbucketed): no floor, today's prefix, today's order."""
    rows = _rows(grouped=False)
    ordered = sorted(rows, key=ap.order_key)
    order, carried, promoted = _floor_select(rows, cap=ap.WORLD_BLOCK_CAP)
    assert promoted == set()
    assert [r["id"] for r in order] == [r["id"] for r in ordered]
    assert [r["id"] for r in carried] == [r["id"] for r in ordered[: ap.WORLD_BLOCK_CAP]]


def test_more_groups_than_the_cap_grows_the_carry_to_the_group_count() -> None:
    """Decision D1's recommended arm: the floor is a guarantee, so a cap below
    the group count grows to it rather than silently dropping a group."""
    shape = tuple((i + 1, f"unit_{i}", f"group_{i}") for i in range(20))
    order, carried, _ = _floor_select(_rows(shape), cap=ap.WORLD_BLOCK_CAP)
    assert len(carried) == 20
    assert _reasons(_payload(order, carried)) == []
