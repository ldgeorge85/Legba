# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""W4 F-3 — ARM 4(c), FLOOR DISCLOSURE (``assembly_floor_arm.floor_disclosure``).

  * each of F1-F4 fires ALONE on a payload tampered in exactly that identity;
  * however many identities break, the read is charged ONE finding
    (``drop_floor_violation``, HARD) and the detail names every one;
  * INERT without ``floor``: no counter, no finding, no check unit — so
    ``branch_score`` on a country payload is byte-identical to the formula that
    shipped, and a floored world payload gains exactly one check unit.
"""

from __future__ import annotations

import copy
import json

import pytest

from legba.data.analysts import assembly_floor as af
from legba.data.analysts import assembly_payload as ap
from legba.data.provenance import assembly_arms as AA
from legba.data.provenance import assembly_floor_arm as AFA
from legba.data.provenance import verify as V

from .test_assembly_floor import LIVE_2933F773, _id_of, _rows, _world_payload


def _floored() -> dict:
    sel, payload = _world_payload()
    return af.stamp_floor(payload, sel.ledger)


def _detail(payload) -> list[str]:
    res = AFA.floor_disclosure(payload)
    assert len(res.findings) <= 1
    if not res.findings:
        return []
    f = res.findings[0]
    assert f.reason == AFA.DROP_FLOOR_VIOLATION == "drop_floor_violation"
    return [part.split(":", 1)[0] for part in f.detail.split("; ")]


def test_a_constructed_floor_passes_all_four_identities() -> None:
    res = AFA.floor_disclosure(_floored())
    assert res.findings == []
    assert res.counters == {"assembly_floor_checked": 1}


def test_inert_without_a_floor_key() -> None:
    sel, payload = _world_payload()
    assert "floor" not in payload
    res = AFA.floor_disclosure(payload)
    assert res.findings == [] and res.counters == {}


def test_f1_fires_alone_when_the_group_arithmetic_does_not_add_up() -> None:
    p = _floored()
    p["floor"]["groups"][0]["offered"] += 1
    assert set(_detail(p)) == {"F1"}


def test_f2_fires_alone_when_an_offered_group_carried_nothing() -> None:
    p = _floored()
    groups = {g["group_id"]: g for g in p["floor"]["groups"]}
    # Move the Americas' one carry onto Europe: F1's sums still close.
    groups["region_americas"]["carried"] -= 1
    groups["region_europe"]["carried"] += 1
    assert set(_detail(p)) == {"F2"}


def test_f2_fires_alone_when_the_honoured_flag_lies() -> None:
    p = _floored()
    p["floor"]["honoured"] = False
    assert set(_detail(p)) == {"F2"}


def test_f3_fires_alone_when_a_not_selected_row_outranks_a_fill_block() -> None:
    p = _floored()
    ns = [d for d in p["drops"]["not_selected"] if not d.get("displaced_by_floor")]
    ns[0]["order_rank"] = 3
    assert set(_detail(p)) == {"F3"}


def test_f4_fires_alone_when_a_prefix_block_is_stamped_kept_by_the_floor() -> None:
    p = _floored()
    p["blocks"][1]["carried_by_floor"] = True
    assert set(_detail(p)) == {"F4"}


def test_f4_fires_alone_when_a_displacement_goes_unstamped() -> None:
    p = _floored()
    for d in p["drops"]["not_selected"]:
        d.pop("displaced_by_floor", None)
    assert set(_detail(p)) == {"F4"}


def test_f4_fires_alone_when_the_promoted_list_names_another_block() -> None:
    p = _floored()
    p["floor"]["promoted"] = [_id_of("country_g20_us")]
    assert set(_detail(p)) == {"F4"}


def test_several_broken_identities_cost_one_finding() -> None:
    p = _floored()
    p["floor"]["groups"][0]["offered"] += 1
    p["floor"]["honoured"] = False
    p["blocks"][1]["carried_by_floor"] = True
    res = AFA.floor_disclosure(p)
    assert len(res.findings) == 1
    assert set(_detail(p)) == {"F1", "F2", "F4"}
    assert res.counters == {"assembly_floor_checked": 1, "assembly_floor_violation": 1}


def test_a_non_object_floor_is_a_violation_not_a_crash() -> None:
    p = _floored()
    p["floor"] = "honoured"
    assert "F1" in _detail(p)


def test_the_reason_is_hard_and_in_the_one_table() -> None:
    assert AA.FAIL_CLASSES[AA.DROP_FLOOR_VIOLATION] == "hard_fail"
    assert V.fail_class_for_reason("drop_floor_violation") == V.FAIL_CLASS_HARD
    assert "assembly_floor_checked" in AA.COUNTERS
    assert AA.LEDGER_CHECK_UNITS == 6


def _country_payload() -> dict:
    rows = _rows(LIVE_2933F773[:10])
    sel = af.floor_select(rows, tier=ap.TIER_COUNTRY, apply_floor=False)
    return ap.build_assembly(
        tier=ap.TIER_COUNTRY, as_of="2026-10-02T00:20:00+00:00",
        candidates=sel.order, carried=sel.carried,
    )


def test_branch_score_on_a_country_payload_is_the_shipped_formula() -> None:
    p = _country_payload()
    res = AA.audit(p, [])
    score = AA.branch_score(res)
    assert "assembly_floor_checked" not in res.counters
    assert score["checkable"] == (
        res.counters.get("assembly_quote_spans_checked", 0)
        + res.counters.get("assembly_attribution_blocks_checked", 0)
        + 6
    )
    # And the selection arm on it is exactly ARM 4(a)+(b): byte-identical
    # counters with the floor arm removed from the fold.
    sel = AA.selection_honesty(p)
    bare = AA.ArmResult()
    bare.extend(AA._coverage_completeness(p))
    bare.extend(AA._drop_disclosure(p))
    assert json.dumps(sel.counters, sort_keys=True) == json.dumps(
        bare.counters, sort_keys=True
    )
    assert sel.findings == bare.findings


def test_a_floored_world_payload_gains_exactly_one_check_unit() -> None:
    p = _floored()
    bare = copy.deepcopy(p)
    bare.pop("floor")
    with_floor = AA.branch_score(AA.audit(p, []))
    without = AA.branch_score(AA.audit(bare, []))
    assert with_floor["checkable"] == without["checkable"] + 1
    assert with_floor["supported"] == without["supported"] + 1


@pytest.mark.parametrize("tamper", ["offered", "honoured"])
def test_a_broken_floor_costs_the_read_one_claim(tamper) -> None:
    p = _floored()
    if tamper == "offered":
        p["floor"]["groups"][0]["offered"] += 1
    else:
        p["floor"]["honoured"] = False
    good = AA.branch_score(AA.audit(_floored(), []))
    bad = AA.branch_score(AA.audit(p, []))
    assert bad["checkable"] == good["checkable"]
    assert bad["supported"] == good["supported"] - 1
