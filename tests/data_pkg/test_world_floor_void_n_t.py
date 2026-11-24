# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""N-T (2026-11-13) — the world read's subject-group floor may never displace a
block for a slot it then declares empty, and a floor ledger that fails its own
identities is never published.

World record 3772fcad (12:21 UTC): the group floor promoted the hybrid-threat
thematic head (a76c36dd, pure rank 29) into the carry and displaced Argentina's
composition head (a12aa9a1, pure rank 16); the carry plan then declared the
hybrid slot empty (``thematic_fallback_refused``), so Argentina lost its place
to nothing, and ARM 4(c) logged ``drop_floor_violation`` (F4: 0 blocks stamped
carried_by_floor but floor.promoted names 1) on a record that was published
anyway.
"""

from __future__ import annotations

import pytest

from legba.data.analysts import assembly_payload as ap
from legba.data.analysts import assembly_world as aw
from legba.data.analysts import composition_assemble as ca
from legba.data.analysts import meta_findings_synthesizer as synth
from legba.data.provenance import assembly_floor_arm as afa

from tests.data_pkg.test_world_read_composition_w1 import (
    KINETIC,
    _block,
    _Deps,
    _payload,
    _plan,
    _region_rows,
    _row,
    _uid,
)
from tests.data_pkg.test_world_selection_n_c import IT_NARRATIVE_ABSENCE


def _rows_3772fcad():
    """The 3772fcad shape at cap 3: RU (Europe), US and AR (Americas) fill the
    pure prefix; the hybrid lane ranks 4th and its only blocks are RU's head
    (already carried) and an absence, so its carry would be the ordinal
    fallback."""
    ru_mil = _block("ru/mil", "military_posture", 3.95, KINETIC, target="country_g20_ru")
    return [
        _row("country_g20_ru", "region_europe", 5.0, [dict(ru_mil)]),
        _row("country_g20_us", "region_americas", 4.0,
             [_block("us/esc", "escalation", 1.0, KINETIC, target="country_g20_us")]),
        _row("country_g20_ar", "region_americas", 3.0,
             [_block("ar/eco", "economic_coercion", 0.9, KINETIC, target="country_g20_ar")]),
        _row("hybrid_threat_composition", "thematic:hybrid_threat_composition", 0.5, [
            dict(ru_mil),
            _block("it/nar", "narrative_coordination", 0.1, IT_NARRATIVE_ABSENCE,
                   target="country_g20_it"),
        ], analyst="hybrid_threat_composition"),
    ]


def test_a_promotion_whose_slot_ends_empty_is_voided_and_the_displaced_row_restored() -> None:
    sel, plan = _plan(_rows_3772fcad(), cap=3)
    hybrid = _uid("head/hybrid_threat_composition")
    argentina = _uid("head/country_g20_ar")
    # The group floor's own selection is unchanged: it promotes the hybrid lane
    # and displaces Argentina ...
    assert sel.ledger["promoted"] == [hybrid]
    assert sel.ledger["displaced"] == [argentina]
    # ... but the plan voids that promotion when the slot ends empty.
    carried = [r["id"] for r in plan.selection.carried]
    assert argentina in carried and hybrid not in carried
    floor = plan.selection.ledger
    assert floor["promoted"] == []
    assert floor["displaced"] == []
    assert floor["voided"] == [{"promoted": hybrid, "why": aw.EMPTY_SLOT_WHY}]
    assert floor["restored"] == [argentina]
    assert plan.ledger["empty_slots"][0]["via_head_id"] == hybrid
    payload = _payload(plan)
    assert len(payload["blocks"]) == 3, "the cap is spent, not one slot short"
    assert not [b for b in payload["blocks"] if b.get("carried_by_floor")]
    assert not [d for d in payload["drops"]["not_selected"] if d.get("displaced_by_floor")]
    assert payload["floor"]["declared_empty"] == ["thematic:hybrid_threat_composition"]
    assert afa.floor_disclosure(payload).findings == [], "ARM 4(c) is clean"


def test_a_promotion_that_is_carried_still_displaces() -> None:
    """The void is only for an EMPTY promoted slot: a thematic lane with a block
    of its own keeps its floor place and the prefix row stays displaced."""
    rows = _rows_3772fcad()
    rows[3] = _row(
        "hybrid_threat_composition", "thematic:hybrid_threat_composition", 0.5,
        [_block("hyb/gb", "escalation", 0.6, KINETIC, target="country_g20_gb")],
        analyst="hybrid_threat_composition",
    )
    _, plan = _plan(rows, cap=3)
    floor = plan.selection.ledger
    assert floor["promoted"] == [_uid("head/hybrid_threat_composition")]
    assert floor["displaced"] == [_uid("head/country_g20_ar")]
    assert "voided" not in floor
    payload = _payload(plan)
    assert [b for b in payload["blocks"] if b.get("carried_by_floor")]
    assert afa.floor_disclosure(payload).findings == []


@pytest.mark.asyncio
async def test_a_broken_floor_ledger_is_withdrawn_before_publication(monkeypatch) -> None:
    """The real binding (``run_method``): a plan whose ledger fails ARM 4(c) is
    never published. The read is the strict prefix of the pure order, the record
    says why, and ``derived_from`` is re-minted from the published order."""
    monkeypatch.setenv(ap.ASSEMBLY_ENV, "world_assessor")
    real_plan = aw.plan_world_carry

    def _broken_plan(selection, **kw):
        plan = real_plan(selection, **kw)
        ledger = dict(plan.selection.ledger)
        ledger["promoted"] = list(ledger.get("promoted") or []) + [_uid("head/ghost")]
        return aw.WorldCarryPlan(
            selection=plan.selection._replace(ledger=ledger), ledger=plan.ledger,
        )

    monkeypatch.setattr(synth, "plan_world_carry", _broken_plan)
    result = await synth.run_method(
        _region_rows(), {"analyst_id": "world_assessor", "composition": True}, _Deps(),
    )
    payload = result.finding.data["assembly"]
    withdrawn = payload[ca.FLOOR_WITHDRAWN_KEY]
    assert withdrawn["why"] == afa.DROP_FLOOR_VIOLATION
    assert "F4" in withdrawn["detail"]
    assert withdrawn["fallback"] == "strict_prefix_pure_order"
    assert "floor" not in payload and "carry_plan" not in payload
    assert afa.floor_disclosure(payload).findings == []
    assert [str(u) for u in result.derived_from][: len(payload["blocks"])] == [
        b["via_head_id"] for b in payload["blocks"]
    ]
    assert any(
        s.get("kind") == ca.FLOOR_WITHDRAWN_KEY for s in result.intermediate_steps
    )


@pytest.mark.asyncio
async def test_a_sound_floor_publishes_unchanged(monkeypatch) -> None:
    monkeypatch.setenv(ap.ASSEMBLY_ENV, "world_assessor")
    result = await synth.run_method(
        _region_rows(), {"analyst_id": "world_assessor", "composition": True}, _Deps(),
    )
    payload = result.finding.data["assembly"]
    assert ca.FLOOR_WITHDRAWN_KEY not in payload
    assert "carry_plan" in payload


def test_a_thread_unit_that_cannot_be_placed_does_not_spend_the_slot() -> None:
    """3772fcad: the shared-thread slot found 12 threads and placed none — the
    one uncarried unit sharing a thread (the UK) cited it only in its
    narrative-absence block, which can never carry. Correct, but the loop
    counted ATTEMPTS: a second eligible unit would never have been tried. FR
    (heavier thread, absence-only) is unplaced and the UK still takes the slot."""
    from tests.data_pkg.test_world_selection_n_c import _sig, _thread_rows

    rows = _thread_rows()
    rows[0]["data"]["data"]["assembly"]["blocks"][0]["signals"].append(
        _sig("[2]", "fr1", "source.fr", "French strike wave widens", mag=0.9),
    )
    fr_abs = _block("fr/nar", "narrative_coordination", 0.1, IT_NARRATIVE_ABSENCE,
                    target="country_g20_fr")
    fr_abs["signals"] = [_sig("[51]", "fr1", "source.fr", "French strike wave widens",
                              mag=0.9)]
    rows.append(_row("country_g20_fr", "region_indo", 0.65, [fr_abs]))
    _, plan = _plan(rows, cap=4)
    ts = plan.ledger["thread_slot"]
    assert ts["unplaced"] == ["country_g20_fr"]
    assert [p["unit"] for p in ts["picks"]] == ["country_g20_gb"]
    assert afa.floor_disclosure(_payload(plan)).findings == []
