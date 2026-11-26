# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A declared cross-unit tension is ONE VARIABLE, not a shared word.

The two mismatched pairs are the verbatim BLUF spans of world_assessor record
e30e67d3 (world read 64f74201, 2026-11-07), where 48 of 392 examined pairs were
declared on subjects like "remain" and "pressure".
"""
from __future__ import annotations

import legba.data.provenance.composition_integrity as ci
from legba.data.analysts.unit_names import tension_statement
from legba.data.provenance import tension_pairing as tp

# Block 4 / block 8 of e30e67d3 — economic_coercion (SA) vs energy_security (SD),
# paired on the one shared word "pressure".
SA_COERCION = (
    "Saudi Arabia is the TARGET of a Houthi aviation-and-oil coercion campaign that "
    "keeps striking Riyadh, Dammam, Medina and Najran through 6-7 November, and while "
    "Saudi Arabia is simultaneously the WIELDER of its own military-economic pressure "
    "on Yemen the squeeze on the kingdom is deepening — evidenced by severe damage at "
    "King Khalid International Airport and a 3.8% Q3 GDP contraction — with Turkish "
    "and Syrian defense cover widening but capping, not lifting, the economic cost "
    "[3][6][7][24][42]."
)
SD_ENERGY = (
    "I judge Sudan's energy-security pressure to remain high and steady, as UN OCHA "
    "reported drone strikes on power infrastructure across at least nine states with "
    "widespread blackouts and fuel shortages disrupting water and aid, while parallel "
    "fuel prices in Wad Madani held at SDG 150,000–170,000 [5][27]."
)
# Block 5 / block 7 — military_posture (IL) vs military_posture (US): the SAME desk,
# different variables (procurement squeeze vs carrier lay-down), paired on
# "continue, military, posture, remain, strike".
IL_POSTURE = (
    "Procurement remains the driver of Israel's standing posture — the aid-funded "
    "rearmament squeeze is widening, with the announced end of roughly $3.8-4 billion "
    "in annual US military aid still unimplemented and analysts warning it would slow "
    "Israeli acquisition timelines, while strikes and raids in Gaza, Lebanon and Syria "
    "continue at their standing pace [4][8][10][40][50][81]."
)
US_POSTURE = (
    "US military posture toward Iran holds at active-combat high — two carrier strike "
    "groups remain on station in the Middle East with a third inbound, undisclosed US "
    "strikes on Iran continue, and no material change from the prior read of 6 "
    "November [121] — while negotiations and a Democratic Congress contemplate limits "
    "but have not yet reduced any deployed force [68][93][96]."
)

SA, SD, IL, US = "country_g20_sa", "country_watch_sd", "country_watch_il", "country_g20_us"


def _pair(ta, tb, *, a, b, desk_a, desk_b, ind_a=None, ind_b=None):
    return ci.direction_conflict(
        ta, tb, target_id=a, other_target_id=b, desk_a=desk_a, desk_b=desk_b,
        indicators_a=ind_a, indicators_b=ind_b,
    )


# --- the two pairs from e30e67d3 must NOT pair -------------------------------


def test_saudi_sudan_pressure_does_not_pair() -> None:
    """Different desks (economic_coercion vs energy_security) — and "pressure" is
    desk boilerplate, not a variable."""
    assert _pair(SA_COERCION, SD_ENERGY, a=SA, b=SD,
                 desk_a="economic_coercion", desk_b="energy_security") is None


def test_israel_us_procurement_vs_force_laydown_does_not_pair() -> None:
    """The SAME desk, but "military / posture / strike / remain / continue" is the
    desk's own vocabulary: no content term is shared, so no variable is matched."""
    assert _pair(IL_POSTURE, US_POSTURE, a=IL, b=US,
                 desk_a="military_posture", desk_b="military_posture") is None


def test_the_boilerplate_terms_are_the_stopword_constant() -> None:
    for w in ("remain", "november", "driver", "pressure", "continue", "standing",
              "through", "military", "posture", "strike"):
        assert w in tp.TENSION_STOPWORDS, w
    # months and generic verbs are screened even in the same-unit/text-only mode
    assert {"november", "remain", "continue", "through"} <= tp.TENSION_STOPWORDS_GENERIC


# --- a TRUE pair must pair ----------------------------------------------------

UP = (
    "Iran keeps forces in active operations: the Hormuz carrier group is "
    "reinforcing and the deployment is widening [1][2]."
)
DOWN = (
    "Pakistan's forces in active operations along the western frontier are reduced, "
    "with the deployment drawn down this week [3]."
)


def test_n_c_rule_0_a_cross_unit_pair_never_pairs_even_on_one_indicator() -> None:
    """N-C (2026-11-11): same entity AND same variable, no exceptions. Iran's and
    Pakistan's military_posture reads on one shared indicator are two variables
    (two countries), so the pair is blocked before any word is compared."""
    assert _pair(
        UP, DOWN, a="country_watch_ir", b="country_watch_pk",
        desk_a="military_posture", desk_b="military_posture",
        ind_a=["forces-in-active-operations"], ind_b=["forces-in-active-operations"],
    ) is None
    assert tp.pair_scope_blocker(
        target_a="country_watch_ir", target_b="country_watch_pk",
        desk_a="military_posture", desk_b="military_posture",
    ) == "cross_unit"
    assert tp.CROSS_UNIT_BLOCKS is True


def test_n_c_the_fd6584eb_turkiye_burkina_pair_is_impossible() -> None:
    """The live pair: TR leadership_transition (carried) against BF's lead block
    — the same desk, matched on the desk's own template words."""
    tr = (
        "Assessed a low likelihood of any change in Türkiye's top leadership within "
        "about 30 days — no signal in this desk's collection through 11 November "
        "reports a challenge to President Recep Tayyip Erdoğan, an incapacity, a "
        "no-confidence mechanism, a cabinet rupture, or a succession move, while "
        "Erdoğan conducted head-of-state business across the window by hosting "
        "COP31 bilaterals, mediating US-Iran diplomacy, and opening the Izmir "
        "steel-plant investigation [6][28][33][36][39][103][67]."
    )
    bf = (
        "Assessed likelihood of a change in Burkina Faso's top leadership within 30 "
        "days is low — Capt. Ibrahim Traoré presided over the Tour du Faso closing "
        "ceremony on 8 November, government business continues normally across AES "
        "and ministerial channels, and no signal in this desk's collection through "
        "11 November reports a no-confidence or impeachment move, a cabinet "
        "defection, leader ill-health, or a coup attempt [14][1][10]."
    )
    assert _pair(tr, bf, a="country_g20_tr", b="country_watch_bf",
                 desk_a="leadership_transition", desk_b="leadership_transition") is None


def test_same_unit_same_desk_shared_content_term_pairs() -> None:
    """One unit, one desk, a shared term that survives the stopword screen."""
    detail = _pair(
        UP, DOWN, a="country_watch_ir", b="country_watch_ir",
        desk_a="military_posture", desk_b="military_posture",
    )
    assert detail is not None and "one subject:" in detail
    assert "deployment" in detail


def test_same_desk_different_indicator_ids_do_not_pair() -> None:
    assert _pair(
        UP, DOWN, a="country_watch_ir", b="country_watch_pk",
        desk_a="military_posture", desk_b="military_posture",
        ind_a=["forces-in-active-operations"], ind_b=["heightened-posture-in-force"],
    ) is None


def test_different_desks_never_pair_across_units_even_on_a_real_shared_term() -> None:
    assert _pair(
        UP, DOWN, a="country_watch_ir", b="country_watch_pk",
        desk_a="military_posture", desk_b="escalation",
    ) is None


def test_a_shared_month_and_hedge_alone_never_pairs() -> None:
    up = "It is rising through November and continues to remain firm."
    down = "It is falling through November and continues to remain firm."
    assert _pair(up, down, a=IL, b=US, desk_a="escalation", desk_b="escalation") is None
    # text-only callers (no desks) get the generic screen too
    assert ci.direction_conflict(up, down) is None


# --- shipped behaviour that must NOT move -------------------------------------


def test_n_c_two_desks_of_one_unit_never_pair_and_text_only_is_unchanged() -> None:
    """N-C follow-up: same entity AND same variable at every tier — two desks of
    ONE country are two bounded questions. The text-only call is unchanged."""
    assert _pair(UP, DOWN, a=IL, b=IL,
                 desk_a="military_posture", desk_b="escalation") is None
    assert tp.pair_scope_blocker(target_a=IL, target_b=IL, desk_a="military_posture",
                                 desk_b="escalation") == "different_desk"
    assert _pair(UP, DOWN, a=IL, b=IL,
                 desk_a="military_posture", desk_b="military_posture") is not None
    assert ci.direction_conflict(UP, DOWN) is not None


def test_h2_composition_vs_own_desk_is_unchanged() -> None:
    assert ci.direction_conflict(
        "UK offensive support to Ukraine is expanding",
        "UK offensive support to Ukraine remains unchanged",
    ) is not None


# --- the template states only what was matched --------------------------------


def _blk(desk, tid, **extra):
    return {"desk": desk, "target_id": tid, "target_name": None, **extra}


def test_template_says_same_dimension_only_when_the_desks_match() -> None:
    same = tension_statement(_blk("military_posture", IL), _blk("military_posture", US))
    assert same == (
        "military_posture on country_watch_il and military_posture on country_g20_us "
        "describe the same dimension in the same window and point in opposite "
        "directions."
    )
    diff = tension_statement(_blk("economic_coercion", SA), _blk("energy_security", SD))
    # N-C: the "are different desks" sentence is gone; two units and two desks
    # name no dimension at all.
    assert "same dimension" not in diff and "different desks" not in diff
    assert "point in opposite directions" in diff


def test_template_names_an_indicator_only_when_both_sides_carry_it() -> None:
    a = _blk("military_posture", IL, indicator_ids=["forces-in-active-operations"])
    b = _blk("military_posture", US, indicator_ids=["forces-in-active-operations"])
    assert "(indicator forces-in-active-operations)" in tension_statement(a, b)
    b2 = _blk("military_posture", US, indicator_ids=["heightened-posture-in-force"])
    assert "indicator" not in tension_statement(a, b2)
    assert "indicator" not in tension_statement(_blk("military_posture", IL), b)


def test_cross_tier_template_states_the_desk_mismatch_too() -> None:
    s = tension_statement(
        _blk("economic_coercion", SA), _blk("country_composition", "country_g20_au"),
        b_carried=False, b_why="shown_not_selected",
    )
    assert "did NOT carry" in s and "different desks" not in s
    assert "describes the same dimension" not in s
    same = tension_statement(
        _blk("military_posture", IL), _blk("military_posture", "country_g20_de"),
        b_carried=False, b_why="shown_not_selected",
    )
    assert "describes the same dimension" in same
