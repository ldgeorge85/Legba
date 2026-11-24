# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""W1 (2026-11-07) — the world read's composition core, from the dig of world
record e30e67d3 (assessment 64f74201).

What this file proves:

  * THE ABSENCE TEST reads the verdict, not the provenance tail: the US
    Miller-swap block and the US 9-November strike-option block (both live,
    verbatim) are carry-eligible; real absence leads still drop.
  * THE CARRY never quotes one desk finding twice in one world read, and a
    later candidate takes its next-best block instead.
  * THE WORLD ORDER is mass-first, so Türkiye (``elevated``, mass 1.70) ranks
    above ``high`` heads at 0.05.
  * THE RESTRAINT-SHIFT FLOOR reserves at most K world slots for
    change-reporting internal_stability / leadership_transition blocks: a
    carried unit swaps its block in its own slot, an uncarried unit is promoted
    over the lowest-ranked kinetic/economic carry, none qualifying reserves
    nothing, and the floor arm (F1-F5) stays clean.
  * THE RENDERER names a thematic carry by its own unit and makes carried +
    aperture = roster: e30e67d3's own shape renders 16 / 17 / 33, not 18.
  * THE REAL BINDING — ``run_method`` on a world run of assembled children.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import NAMESPACE_URL, uuid5

import pytest

from legba.data.analysts import assembly_carry as ac
from legba.data.analysts import assembly_floor as af
from legba.data.analysts import assembly_payload as ap
from legba.data.analysts import assembly_world as aw
from legba.data.analysts import assessment_prompts as apr
from legba.data.analysts import meta_findings_synthesizer as synth
from legba.data.provenance import assembly_floor_arm as afa

COLL = ("collection_denominator",)

# ---------------------------------------------------------------------------
# Live lead spans, verbatim from the children of world record e30e67d3.
# ---------------------------------------------------------------------------

#: US internal_stability, finding 7acdb8d0 (mass 0.05).
US_MILLER = (
    "Elite/insider churn is the driving vector and continues at a moderate "
    "pace — President Trump replaced chief of staff Susie Wiles with hardliner "
    "Stephen Miller three days after the midterm defeat and a top federal "
    "prosecutor resigned amid probe turmoil, while no mass unrest, "
    "security-force split, emergency powers, or communications shutdown "
    "appears in this desk's collection through 7 November "
    "[5][29][48][78][104][117]."
)
#: US escalation, finding 283b7e30 (mass 1.05).
US_STRIKE_OPTION = (
    "The US-Iran confrontation remains this desk's escalation driver, with US "
    "strikes on Iranian air defenses and a reported 9 November strike option "
    "pushing toward wider combat while active Trump-Iran talks, an advancing "
    "carrier group still days out, and post-midterm political constraint pull "
    "it back [7][68][93][20][62]."
)
#: US military_posture, finding 1c3eca18 (mass 1.00) — carried on e30e67d3.
US_POSTURE = (
    "US military posture toward Iran holds at active-combat high — two carrier "
    "strike groups remain on station in the Middle East with a third inbound, "
    "undisclosed US strikes on Iran continue, and no material change from the "
    "prior read of 6 November [121] — while negotiations and a Democratic "
    "Congress contemplate limits but have not yet reduced any deployed force "
    "[68][93][96]."
)
#: Israel internal_stability, finding 3af9f986 (mass 0.75).
IL_EISENKOT = (
    "The elite-fracture driver has eased slightly rather than spiked, as "
    "opposition leader Gadi Eisenkot consolidated his path to the mandate with "
    "Arab parties and B'Yachad backing his 67 Knesset recommendations ahead of "
    "Herzog's 8 November decision [1][26][45][59], while the counter-pressures "
    "on the regime — Shin Bet's ongoing election inquiries and Ben-Gvir's "
    "pressure to ignore legal restraints — continue without having produced a "
    "security-force rupture in this desk's collection through 7 November "
    "[21][38]."
)
#: Türkiye leadership_transition, finding 18ab94fc — STASIS, not a change.
TR_LEADERSHIP_STASIS = (
    "Assessed a low likelihood of any change in Türkiye's top leadership within "
    "about 30 days — no signal in this desk's collection through 7 November "
    "reports a challenge to President Recep Tayyip Erdoğan, an incapacity, a "
    "cabinet rupture, a no-confidence mechanism, or a succession move."
)

#: Real absence leads — each must still be excluded from a by-mass carry.
TRUE_ABSENCES = (
    # RU leadership_transition 9dabe49e (mass 1.45): the leading negation.
    "No signal in this desk's collection through 7 November reports any "
    "challenge to Vladimir Putin's hold on Russia's top job, so a change in top "
    "leadership within about 30 days is assessed as very unlikely "
    "[2][12][46][71][99][121].",
    # BR internal_stability 311bf0f4: the negation inside the lead clause.
    "This desk's collection through 7 November contains no mass protest, "
    "security-force split, emergency measure, or communications shutdown, while "
    "the standing elevated driver — post-election violence against Guarani "
    "Kaiowá communities — is carried from the 6 November prior read and not "
    "re-sourced by any signal in this slice [47].",
    # IR internal_stability 17616b3a (mass 1.70).
    "Through 7 November, this desk's collection shows no popular unrest or "
    "security-force disloyalty in Iran, while state-organised rallies, official "
    "warnings against 'rumors', and hardening war rhetoric from the Leader's "
    "circle and senior MPs hold the street under an external strike threat that "
    "has not yet produced elite rupture [113][71][10][58].",
    # CD military_posture a3f375a5.
    "No shift in DR Congo's standing military posture appears in this desk's "
    "collection through 7 November 2026 — an ADF attack killed at least 33 "
    "civilians in North Kivu on 5–6 November.",
    # AU internal_stability b6e3c090.
    "No security-force split, emergency powers, communications shutdown, or "
    "nationwide protest wave appears in this desk's collection through 7 "
    "November, while economic strain and isolated violence remain the standing "
    "instability drivers, with no material change since the 6 November read "
    "[121].",
)

KINETIC = "Strikes on the grid widened across nine regions through 6 November [1]."


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _uid(key: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"w1-world/{key}"))


def _block(
    key: str, desk: str, mass: float, text: str, *,
    target: str = "country_x", ordinal: int = 1, severity: str = "high",
    tokens: tuple[str, ...] = COLL,
) -> dict[str, Any]:
    return {
        "ordinal": ordinal,
        "finding_id": _uid(key),
        "desk": desk,
        "target_id": target,
        "target_name": target,
        "question": desk,
        "severity": severity,
        "salience": {"cited_mass": mass},
        "verify": {"effective_confidence": 0.8},
        "spans": [{
            "role": "bluf", "text": text, "scope_tokens": list(tokens),
            "origin": {"head_id": _uid(key)},
        }],
        "signals": [],
    }


def _row(
    unit: str, group: str, head_mass: float, blocks: list[dict[str, Any]], *,
    severity: str = "high", analyst: str = "country_composition",
) -> dict[str, Any]:
    for i, b in enumerate(blocks, start=1):
        b["ordinal"] = i
    produced = datetime.now(timezone.utc) - timedelta(hours=1)
    return {
        "id": _uid(f"head/{unit}"),
        "kind": "finding",
        "analyst_id": analyst,
        "target_id": None if analyst != "country_composition" else unit,
        "title": f"read {unit}",
        "body": "assembled",
        "severity": severity,
        "confidence": 0.8,
        "produced_at": produced.isoformat(),
        "derived_from": [],
        "data": {"data": {"citations": [], "assembly": {
            "schema": ap.ASSEMBLY_SCHEMA, "regime": ap.REGIME_ASSEMBLY,
            "lead": {"kind": "none", "block_ordinals": []}, "blocks": blocks,
        }}},
        ap.CITED_SALIENCE_ROW_KEY: {
            "cited_max": head_mass, "cited_mass": head_mass, "n_above": 0,
            "n_cited_scored": 1, "n_cited": 1, "source": "fixture",
            "version": "fixture",
        },
        af.SUBJECT_GROUP_ROW_KEY: group,
    }


def _plan(rows, *, cap: int, slots: int = aw.RESTRAINT_SHIFT_SLOTS):
    sel = af.floor_select(
        rows, tier=ap.TIER_WORLD, apply_floor=True, cap=cap, key=aw.world_order_key,
    )
    return sel, aw.plan_world_carry(sel, slots=slots)


def _payload(plan):
    sel = plan.selection
    payload = ap.build_assembly(
        tier=ap.TIER_WORLD, as_of="2026-11-07T12:21:03+00:00",
        candidates=sel.order, carried=sel.carried,
    )
    payload = af.stamp_floor(payload, sel.ledger)
    return aw.stamp_world_carry(payload, plan.ledger)


# ---------------------------------------------------------------------------
# 1. THE ABSENCE TEST
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text", [US_MILLER, US_STRIKE_OPTION, IL_EISENKOT])
def test_a_verdict_with_a_provenance_tail_is_not_an_absence(text) -> None:
    assert ac.block_is_absence(_block("b", "internal_stability", 0.1, text)) is False


@pytest.mark.parametrize("text", TRUE_ABSENCES)
def test_a_real_absence_lead_still_drops(text) -> None:
    assert ac.block_is_absence(_block("b", "internal_stability", 0.1, text)) is True


def test_the_lead_clause_stops_at_the_provenance_tail() -> None:
    assert ac.lead_clause(US_MILLER) == (
        "elite/insider churn is the driving vector and continues at a moderate pace"
    )


def test_the_us_read_now_carries_its_strike_option_not_its_posture() -> None:
    """On e30e67d3 the US read carried military posture (1.00, "holds") because
    escalation (1.05, the 9 November strike option) read as an absence."""
    child = {"blocks": [
        _block("us/esc", "escalation", 1.05, US_STRIKE_OPTION, ordinal=1),
        _block("us/mil", "military_posture", 1.00, US_POSTURE, ordinal=2, tokens=()),
        _block("us/int", "internal_stability", 0.05, US_MILLER, ordinal=3),
    ]}
    block, ordinal, why = ac.carried_lead_block(child)
    assert (block["desk"], ordinal, why) == ("escalation", 1, ac.CARRY_BY_MASS)


# ---------------------------------------------------------------------------
# 2. NO FINDING CARRIED TWICE
# ---------------------------------------------------------------------------


def test_an_excluded_block_gives_way_to_the_next_heaviest() -> None:
    child = {"blocks": [
        _block("ru/esc", "escalation", 3.0, KINETIC, ordinal=1),
        _block("ru/mil", "military_posture", 1.2, KINETIC, ordinal=2),
    ]}
    block, _, why = ac.carried_lead_block(child, exclude={_uid("ru/esc")})
    assert block["finding_id"] == _uid("ru/mil") and why == ac.CARRY_BY_MASS
    # Every block excluded: the shipped rule, and the plan counts the duplicate.
    block, _, _ = ac.carried_lead_block(
        child, exclude={_uid("ru/esc"), _uid("ru/mil")},
    )
    assert block["finding_id"] == _uid("ru/esc")


def test_the_thematic_and_the_country_head_never_carry_one_finding_twice() -> None:
    shared = _block("ru/esc", "escalation", 3.0, KINETIC, target="country_g20_ru")
    rows = [
        _row("escalation_composition", "thematic:escalation_composition", 21.6,
             [dict(shared)], analyst="escalation_composition"),
        _row("country_g20_ru", "region_europe", 9.9, [
            dict(shared),
            _block("ru/mil", "military_posture", 2.0, KINETIC, target="country_g20_ru"),
        ]),
    ]
    _, plan = _plan(rows, cap=2)
    payload = _payload(plan)
    fids = [b["finding_id"] for b in payload["blocks"]]
    assert fids == [_uid("ru/esc"), _uid("ru/mil")]
    assert [b["via_unit"] for b in payload["blocks"]] == [
        "escalation_composition", "country_g20_ru",
    ]
    assert payload["carry_plan"]["duplicates_unavoidable"] == 0


# ---------------------------------------------------------------------------
# 3. THE WORLD ORDER
# ---------------------------------------------------------------------------


def test_the_world_order_is_mass_first_so_turkiye_is_not_26th() -> None:
    tr = _row("country_g20_tr", "region_mena", 1.70, [], severity="elevated")
    de = _row("country_g20_de", "region_europe", 0.05, [])
    ht = _row("country_watch_ht", "region_americas", 0.05, [])
    assert [r["target_id"] for r in sorted([de, ht, tr], key=ap.order_key)][-1] == (
        "country_g20_tr"
    )
    assert sorted([de, ht, tr], key=aw.world_order_key)[0] is tr
    # Equal mass: severity still breaks the tie.
    low = _row("country_g20_it", "region_europe", 0.05, [], severity="elevated")
    assert sorted([low, de], key=aw.world_order_key)[0] is de


# ---------------------------------------------------------------------------
# 4. THE RESTRAINT-SHIFT FLOOR
# ---------------------------------------------------------------------------


def test_what_counts_as_a_reported_change() -> None:
    assert aw.reports_change(_block("a", "internal_stability", 0.05, US_MILLER))
    assert aw.reports_change(_block("b", "internal_stability", 0.75, IL_EISENKOT))
    assert not aw.reports_change(
        _block("c", "leadership_transition", 0.25, TR_LEADERSHIP_STASIS)
    )
    assert not aw.reports_change(_block("d", "internal_stability", 1.7, TRUE_ABSENCES[2]))
    # A kinetic report filed on a political desk is not a restraint shift.
    assert not aw.reports_change(_block(
        "e", "internal_stability", 2.8,
        "External war pressure on Saudi territory keeps widening — Houthi drone "
        "and ballistic-missile strikes on Riyadh, Dammam, Medina-region and "
        "Najran targets on 4–6 November [2][3].",
    ))


def _region_rows(il_political: bool = True, us_political: bool = True, ne: bool = True):
    """Seven units over three groups. At cap 5 the subject-group floor carries
    IR / US / SD and the fill adds IL and NE; FR and BR are below the cut. FR
    holds the heaviest restraint-shift block, IL the next."""
    il = [_block("il/esc", "escalation", 2.1, KINETIC, target="country_watch_il")]
    if il_political:
        il.append(_block("il/int", "internal_stability", 0.75, IL_EISENKOT,
                         target="country_watch_il"))
    us = [_block("us/esc", "escalation", 1.05, US_STRIKE_OPTION, target="country_g20_us")]
    if us_political:
        us.append(_block("us/int", "internal_stability", 0.05, US_MILLER,
                         target="country_g20_us"))
    rows = [
        _row("country_watch_ir", "region_mena", 8.0,
             [_block("ir/esc", "escalation", 4.4, KINETIC, target="country_watch_ir")]),
        _row("country_watch_il", "region_mena", 5.0, il),
        _row("country_g20_us", "region_americas", 4.0, us),
        _row("country_watch_sd", "region_africa", 1.6,
             [_block("sd/mil", "military_posture", 1.45, KINETIC, target="country_watch_sd")]),
        _row("country_watch_ne", "region_africa", 1.2,
             [_block("ne/mil", "military_posture", 0.3, KINETIC, target="country_watch_ne")]),
        _row("country_g20_fr", "region_africa", 0.45, [
            _block("fr/esc", "escalation", 0.2, KINETIC, target="country_g20_fr"),
            _block("fr/int", "internal_stability", 0.9,
                   "Mass protest is now the moving vector — 448 arrests on 5 "
                   "November and a suit against the interior minister [20], "
                   "while no security-force split appears in this desk's "
                   "collection.", target="country_g20_fr"),
        ]),
        _row("country_g20_br", "region_americas", 0.05,
             [_block("br/int", "internal_stability", 0.05, TRUE_ABSENCES[1],
                     target="country_g20_br")]),
    ]
    return rows if ne else [r for r in rows if r["target_id"] != "country_watch_ne"]


def test_a_carried_unit_adds_and_an_uncarried_unit_is_promoted() -> None:
    """N-C follow-up: a carried unit's political block is ADDED beside its lead
    (it used to SWAP the lead out), paid for by another unit's lowest carry."""
    sel, plan = _plan(_region_rows(), cap=5)
    assert [r["target_id"] for r in sel.carried] == [
        "country_watch_ir", "country_watch_il", "country_g20_us",
        "country_watch_sd", "country_watch_ne",
    ]
    rf = plan.ledger["restraint_floor"]
    # By mass among the eligible (FR 0.90, IL 0.75, US 0.05), K = 2.
    assert [(p["unit"], p["action"]) for p in rf["picks"]] == [
        ("country_g20_fr", "promoted"), ("country_watch_il", "added"),
    ]
    # FR's promotion took the LOWEST-ranked kinetic carry, NE (Africa keeps SD);
    # IL's addition then took SD (Africa keeps FR) — never IL's own lead.
    assert rf["displaced"] == [
        _uid("head/country_watch_ne"), _uid("head/country_watch_sd"),
    ]
    payload = _payload(plan)
    assert [(b["via_unit"], b["desk"]) for b in payload["blocks"]] == [
        ("country_watch_ir", "escalation"),
        ("country_watch_il", "escalation"),
        ("country_watch_il", "internal_stability"),
        ("country_g20_us", "escalation"),
        ("country_g20_fr", "internal_stability"),
    ]
    by_unit = {(b["via_unit"], b["desk"]): b for b in payload["blocks"]}
    assert by_unit[("country_watch_il", "escalation")].get("carried_by_restraint_floor") is None
    assert by_unit[("country_watch_il", "internal_stability")]["added_by_restraint_floor"] is True
    for key in (("country_g20_fr", "internal_stability"),
                ("country_watch_il", "internal_stability")):
        assert by_unit[key]["carry_reason"] == aw.CARRY_RESTRAINT_SHIFT
        assert by_unit[key]["carried_by_restraint_floor"] is True
    by_unit = {b["via_unit"]: b for b in payload["blocks"]}
    assert by_unit["country_g20_us"]["desk"] == "escalation"
    ne = [d for d in payload["drops"]["not_selected"]
          if d["target_id"] == "country_watch_ne"]
    assert ne[0]["displaced_by_restraint_floor"] is True
    # ARM 4(b): every not-selected row's published rank is above the carried.
    assert min(d["rank"] for d in payload["drops"]["not_selected"]) == 6
    assert afa.floor_disclosure(payload).findings == []
    assert payload["floor"]["honoured"] is True


def test_a_promotion_never_empties_a_subject_group_or_evicts_a_pick() -> None:
    """Without NE, the only kinetic carry whose group keeps another is IR:
    SD and US are their groups' sole carries, and IL is itself a pick."""
    _, plan = _plan(_region_rows(ne=False), cap=4)
    rf = plan.ledger["restraint_floor"]
    assert rf["displaced"] == [
        _uid("head/country_watch_ir"), _uid("head/country_watch_sd"),
    ]
    payload = _payload(plan)
    assert afa.floor_disclosure(payload).findings == []
    assert all(
        g["carried"] >= 1 for g in payload["floor"]["groups"] if g["offered"] >= 1
    )


def test_the_floor_reserves_at_most_k_and_nothing_when_none_qualifies() -> None:
    _, plan = _plan(_region_rows(), cap=5, slots=1)
    assert len(plan.ledger["restraint_floor"]["picks"]) == 1
    rows = _region_rows(il_political=False, us_political=False)
    rows[5]["data"]["data"]["assembly"]["blocks"].pop()  # FR's political block
    sel, plan = _plan(rows, cap=5)
    rf = plan.ledger["restraint_floor"]
    assert rf["eligible"] == 0 and rf["picks"] == []
    assert [r["id"] for r in plan.selection.carried] == [r["id"] for r in sel.carried]
    assert all(
        b["carry_reason"] != aw.CARRY_RESTRAINT_SHIFT for b in _payload(plan)["blocks"]
    )


def test_a_unit_already_carrying_a_change_counts_toward_k() -> None:
    rows = _region_rows()
    # IL's political block outweighs its kinetic one: carried by mass already.
    rows[1]["data"]["data"]["assembly"]["blocks"][1]["salience"]["cited_mass"] = 3.0
    _, plan = _plan(rows, cap=5)
    rf = plan.ledger["restraint_floor"]
    assert rf["already_carried"] == [_uid("head/country_watch_il")]
    assert len(rf["picks"]) == 1


def test_the_floor_arm_catches_a_stamp_the_plan_did_not_make() -> None:
    _, plan = _plan(_region_rows(), cap=5)
    payload = _payload(plan)
    payload["blocks"][0]["carried_by_restraint_floor"] = True
    detail = afa.floor_disclosure(payload).findings[0].detail
    assert "F5" in detail


# --- 4b. two thematic groups + the restraint floor on a 16-slot world ------

_REGIONS = ("region_mena", "region_americas", "region_africa", "region_europe",
            "region_indo_pacific")
FR_PROTEST = (
    "Mass protest is now the moving vector — 448 arrests on 5 November and a "
    "suit against the interior minister [20], while no security-force split "
    "appears in this desk's collection."
)


def _sixteen_slot_rows(hybrid_mass: float) -> list[dict[str, Any]]:
    """22 candidates over SEVEN subject groups (five regions and two thematic
    lanes): escalation_composition at the top, eighteen kinetic country heads
    (9.00 down to 1.35, regions round-robin), two UNcarried heads whose
    political block reports a change, and hybrid_threat_composition at
    ``hybrid_mass``."""
    rows = [_row("escalation_composition", "thematic:escalation_composition", 10.0,
                 [_block("esc/ru", "escalation", 5.0, KINETIC, target="country_g20_ru")],
                 analyst="escalation_composition")]
    for i in range(18):
        unit = f"country_k{i:02d}"
        desk = "escalation" if i % 2 else "military_posture"
        rows.append(_row(unit, _REGIONS[i % 5], 9.0 - 0.45 * i,
                         [_block(f"{unit}/k", desk, 1.0, KINETIC, target=unit)]))
    for unit, region, mass, text in (
        ("country_g20_fr", "region_europe", 0.30, FR_PROTEST),
        ("country_g20_us", "region_americas", 0.20, US_MILLER),
    ):
        rows.append(_row(unit, region, mass, [
            _block(f"{unit}/k", "escalation", 0.1, KINETIC, target=unit),
            _block(f"{unit}/int", "internal_stability", 0.9, text, target=unit),
        ]))
    rows.append(_row("hybrid_threat_composition", "thematic:hybrid_threat_composition",
                     hybrid_mass,
                     [_block("hyb/gb", "escalation", 0.6, KINETIC, target="country_g20_gb")],
                     analyst="hybrid_threat_composition"))
    return rows


@pytest.mark.parametrize("hybrid_mass, kept_by_floor", [(0.05, True), (2.72, False)])
def test_the_hybrid_group_and_the_restraint_floor_never_share_a_slot(
    hybrid_mass, kept_by_floor,
) -> None:
    """W5 activation on the W1 carry plan, at the live cap of 16.

    The subject-group floor guarantees the hybrid lane ONE block (promoted over
    the pure rank-16 row when it ranks below the cut, 0.05; in the prefix at
    rank 16 when it does not, 2.72). The restraint-shift floor then promotes
    its K=2 political picks over the LOWEST-ranked kinetic carries — and the
    hybrid row is never one of them: a group-floor promotion is excluded by
    name, and a sole-carry group (every thematic lane has exactly one head) is
    excluded by the group count. So 16 = 7 group floors + 9 fill, of which the
    restraint floor re-uses 2 fill slots; nothing is double-booked."""
    sel, plan = _plan(_sixteen_slot_rows(hybrid_mass), cap=16)
    hybrid = _uid("head/hybrid_threat_composition")
    assert len(sel.carried) == 16 and hybrid in [r["id"] for r in sel.carried]
    assert (hybrid in sel.ledger["promoted"]) is kept_by_floor
    rf = plan.ledger["restraint_floor"]
    assert [(p["unit"], p["action"]) for p in rf["picks"]] == [
        ("country_g20_fr", "promoted"), ("country_g20_us", "promoted"),
    ]
    assert hybrid not in rf["displaced"] and len(rf["displaced"]) == 2
    carried = plan.selection.carried
    assert len(carried) == 16
    assert hybrid in [r["id"] for r in carried]
    payload = _payload(plan)
    units = [b["via_unit"] for b in payload["blocks"]]
    assert len(units) == 16 == len(set(units))
    assert {"escalation_composition", "hybrid_threat_composition"} <= set(units)
    assert payload["floor"]["honoured"] is True
    assert all(
        g["carried"] >= 1 for g in payload["floor"]["groups"] if g["offered"] >= 1
    )
    assert afa.floor_disclosure(payload).findings == []


# ---------------------------------------------------------------------------
# 5. THE RENDERER — e30e67d3's own shape
# ---------------------------------------------------------------------------

#: e30e67d3's sixteen carried blocks, (target_id, via head key); block 1 is
#: escalation_composition's carry of Russia's escalation head.
E30E67D3_BLOCKS = (
    ("country_g20_ru", "escalation_composition"), ("country_g20_ru", "ru"),
    ("country_watch_ir", "ir"), ("country_g20_sa", "sa"), ("country_watch_il", "il"),
    ("country_watch_ua", "ua"), ("country_g20_us", "us"), ("country_watch_sd", "sd"),
    ("country_watch_cd", "cd"), ("country_watch_kp", "kp"), ("country_watch_mm", "mm"),
    ("country_g20_cn", "cn"), ("country_watch_pk", "pk"), ("country_g20_id", "id"),
    ("country_g20_fr", "fr"), ("country_watch_ne", "ne"),
)
E30E67D3_NOT_CARRIED = (
    "country_watch_ml", "country_g20_in", "country_g20_za", "country_watch_bf",
    "country_g20_kr", "country_g20_ca", "country_watch_ht", "country_g20_de",
    "country_g20_br", "country_g20_tr", "country_watch_tw", "country_g20_jp",
    "country_g20_gb", "country_g20_mx", "country_g20_it", "country_g20_ar",
    "country_g20_au",
)


def _e30e67d3_payload(*, stamped: bool) -> dict[str, Any]:
    blocks = []
    for i, (target, via) in enumerate(E30E67D3_BLOCKS, start=1):
        b = {"ordinal": i, "target_id": target, "via_head_id": _uid(via),
             "order_rank": i, "desk": "escalation"}
        if stamped:
            b["via_unit"] = "escalation_composition" if i == 1 else target
        blocks.append(b)
    roster = sorted(
        {t for t, _ in E30E67D3_BLOCKS} | set(E30E67D3_NOT_CARRIED)
        | {"escalation_composition"}
    )
    return {
        "tier": "world",
        "blocks": blocks,
        "coverage_roster": roster,
        "coverage": [{"unit": u, "status": "in_basis"} for u in roster],
        "floor": {"groups": [{
            "group_id": "thematic:escalation_composition", "offered": 1,
            "carried": 1, "best_order_rank": 1, "reason": "in_prefix",
        }]},
        "drops": {"not_selected": [
            {"target_id": u, "desk": "country_composition", "title": u}
            for u in E30E67D3_NOT_CARRIED
        ], "counts": {}},
    }


@pytest.mark.parametrize("stamped", [False, True])
def test_e30e67d3_renders_16_plus_17_equals_33(stamped) -> None:
    payload = _e30e67d3_payload(stamped=stamped)
    assert len(payload["coverage_roster"]) == 33
    line = next(
        ln for ln in apr.declared_aperture(payload).splitlines() if "CARRIED" in ln
    )
    assert "this record CARRIED 16 — 16 blocks over 16 units:" in line
    assert "The other 17 are its aperture (16 + 17 = 33)" in line
    assert line.count("country_g20_ru") == 1
    assert "escalation_composition" in line


def test_a_payload_without_a_floor_keys_on_the_block_target_as_shipped() -> None:
    payload = _e30e67d3_payload(stamped=False)
    del payload["floor"]
    assert apr.carried_unit(payload, payload["blocks"][0]) == "country_g20_ru"


# ---------------------------------------------------------------------------
# 6. THE REAL BINDING — run_method on a world run of assembled children
# ---------------------------------------------------------------------------


class _NeverCalledLLM:
    subprovider = "never_called"

    async def chat_complete(self, *a, **k):  # pragma: no cover
        raise AssertionError("the assembly path is deterministic")


class _Deps:
    def __init__(self) -> None:
        self.llm = _NeverCalledLLM()


@pytest.mark.asyncio
async def test_a_world_run_publishes_the_plan(monkeypatch) -> None:
    monkeypatch.setenv(ap.ASSEMBLY_ENV, "world_assessor")
    rows = _region_rows()
    shared = _block("ir/esc", "escalation", 4.4, KINETIC, target="country_watch_ir")
    rows.append(_row("escalation_composition", "thematic:escalation_composition",
                     21.0, [dict(shared)], analyst="escalation_composition"))
    rows[0]["data"]["data"]["assembly"]["blocks"].append(
        _block("ir/mil", "military_posture", 2.97, KINETIC, target="country_watch_ir",
               ordinal=2),
    )
    result = await synth.run_method(
        rows, {"analyst_id": "world_assessor", "composition": True}, _Deps(),
    )
    payload = result.finding.data["assembly"]
    fids = [b["finding_id"] for b in payload["blocks"]]
    assert len(fids) == len(set(fids))
    assert payload["carry_plan"]["duplicates_unavoidable"] == 0
    assert payload["blocks"][0]["via_unit"] == "escalation_composition"
    assert all(b.get("via_unit") for b in payload["blocks"])
    assert payload["carry_plan"]["version"] == aw.CARRY_PLAN_VERSION
    # derived_from is minted from the PUBLISHED order: ordinal N is entry N.
    assert [str(u) for u in result.derived_from][: len(fids)] == [
        b["via_head_id"] for b in payload["blocks"]
    ]
    assert afa.floor_disclosure(payload).findings == []
    placed = [b for b in payload["blocks"] if b.get("carried_by_restraint_floor")]
    assert placed and all(
        "carried by the restraint-shift floor" in result.finding.body for _ in placed
    )
