# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""N-C (2026-11-11) — world read selection, from the operator's review of world
record fd6584eb (WR3, WR4, WR5, WR7, WR8) and the 11-11b quality sweep.

One section per rule:

  1. a tension is one entity AND one variable (the Türkiye × Burkina Faso pair
     is impossible; the dropped side names the desk its gate compared);
  2. a declared tension carries a deterministic evidence balance;
  3. the politics floor selects by cited mass among political blocks that name
     a dated institutional event (US 0.80 over Pakistan 0.05);
  4. a cross-unit shared thread is published and earns one uncarried unit a slot;
  5. the new-material bullet is quoted beside the BLUF;
  6. opposite verdicts on one event inside one unit's head render as contested;
  7. a thematic slot never carries its ordinal fallback — it is declared empty.

The spans are verbatim from fd6584eb's candidates where a live specimen exists.
"""

from __future__ import annotations

import asyncio
from typing import Any

from legba.data.analysts import assembly_payload as ap
from legba.data.analysts import assembly_render as ar
from legba.data.analysts import assembly_world as aw
from legba.data.analysts import world_block_evidence as wbe
from legba.data.analysts import world_selection_rules as wsr
from legba.data.analysts.assembly_spans import build_context_span, build_span
from legba.data.provenance import assembly_arms as aa
from legba.data.provenance import assembly_floor_arm as afa

from tests.data_pkg.test_world_read_composition_w1 import (
    IL_EISENKOT,
    KINETIC,
    US_MILLER,
    _block,
    _payload,
    _plan,
    _row,
    _uid,
)

# --- live specimens (fd6584eb children) ---------------------------------------

#: US internal_stability c52a2c06 (cited mass 0.80).
US_HEGSETH = (
    "Elite and security-civilian friction remains the driving instability vector "
    "but its acute phase paused, as the House blocked the Hegseth impeachment on 10 "
    "November by a 211-204 referral vote while no mass unrest, security-force "
    "defection, or state-repression measure appears in this desk's collection "
    "through 11 November [57][116][36]."
)
#: Pakistan internal_stability 3c054960 (cited mass 0.05) — the old floor's pick.
PK_REPRESSION = (
    "State repression and the AJK crackdown remain this desk's driving vector and "
    "have widened rather than eased — a third contempt filing over Imran Khan's "
    "medical care, preventive detentions of union leaders, and a blocked opposition "
    "delegation to Kashmir — while the government's advancing petroleum-levy deal "
    "with Jamaat-i-Islami holds the mass-mobilization threat in check and no "
    "security-force split appears in this desk's collection through 11 November "
    "2026 [23][62][39][20]."
)
#: Türkiye leadership_transition 82f8d029 — carried as block 8 of fd6584eb.
TR_LT = (
    "Assessed a low likelihood of any change in Türkiye's top leadership within "
    "about 30 days — no signal in this desk's collection through 11 November "
    "reports a challenge to President Recep Tayyip Erdoğan, an incapacity, a "
    "no-confidence mechanism, a cabinet rupture, or a succession move, while "
    "Erdoğan conducted head-of-state business across the window by hosting COP31 "
    "bilaterals, mediating US-Iran diplomacy, and opening the Izmir steel-plant "
    "investigation [6][28][33][36][39][103][67]."
)
#: Burkina Faso leadership_transition — the lead block of BF composition 9b3d43ac.
BF_LT = (
    "Assessed likelihood of a change in Burkina Faso's top leadership within 30 "
    "days is low — Capt. Ibrahim Traoré presided over the Tour du Faso closing "
    "ceremony on 8 November, government business continues normally across AES and "
    "ministerial channels, and no signal in this desk's collection through 11 "
    "November reports a no-confidence or impeachment move, a cabinet defection, "
    "leader ill-health, or a coup attempt [14][1][10]."
)
#: GB escalation 87a1aa3d — the UK read the cut dropped at rank 18.
GB_ESC = (
    "Iran-linked threats to US aircraft on UK soil remain this desk's escalation "
    "driver, with US tankers and Ospreys relocated from RAF Mildenhall on 9 November "
    "over a reported Iranian threat and a superseding indictment issued 10 November "
    "on an alleged plot against RAF Fairford, while no UK mobilization tied to a "
    "near-term confrontation appears in this desk's collection through 11 November "
    "[68][71][13][40][121]."
)
GB_MARITIME = (
    "British maritime posture continues moving upward — a signed 10-nation Northern "
    "Navies Initiative agreement and a £90m 'Atlantic Net' programme build on the "
    "prior read's direction [17][46][68]."
)
DOJ = "U.S. charges three ‘Iranian operatives’ over U.K. airbase plot"
MILDENHALL = "US relocates military aircraft from British base amid Iran threats"


def _sig(marker: str, sid: str, source: str, title: str, mag: float = 0.6) -> dict:
    return {"marker": marker, "signal_id": sid, "source_id": source, "title": title,
            "url": "", "salience_magnitude": mag, "also_cited_by": []}


# ---------------------------------------------------------------------------
# 1. ONE ENTITY, ONE VARIABLE
# ---------------------------------------------------------------------------


def _child_row(unit: str, mass: float, blocks: list[dict[str, Any]], group="region_x"):
    return _row(unit, group, mass, blocks)


def test_rule1_the_turkiye_burkina_pairing_is_impossible() -> None:
    """The live pair: TR leadership_transition carried, BF composition dropped
    with a leadership_transition lead. Same desk, two entities: no tension."""
    tr = _child_row("country_g20_tr", 2.0, [
        _block("tr/lt", "leadership_transition", 0.4, TR_LT, target="country_g20_tr",
               tokens=()),
    ])
    bf = _child_row("country_watch_bf", 0.05, [
        _block("bf/lt", "leadership_transition", 0.05, BF_LT, target="country_watch_bf",
               tokens=()),
    ])
    _, plan = _plan([tr, bf], cap=1)
    payload = _payload(plan)
    assert payload["tensions"] == []
    assert payload["tension_checked"]["pairs_found"] == 0


def test_rule1_world_same_unit_needs_the_same_desk_and_names_it() -> None:
    up = "Oil exports through the corridor rose sharply [1]."
    down = "Oil exports through the corridor fell sharply [1]."
    a = _block("u/es1", "energy_security", 1.0, up, target="country_g20_us", tokens=())
    b = _block("u/es2", "energy_security", 0.5, down, target="country_g20_us", tokens=())
    c = _block("u/esc", "escalation", 0.4, down, target="country_g20_us", tokens=())
    carried = _child_row("country_g20_us", 3.0, [a])
    dropped_same = _child_row("country_g20_us#2", 0.2, [b])
    dropped_same["target_id"] = "country_g20_us"
    dropped_other_desk = _child_row("country_g20_us#3", 0.1, [c])
    dropped_other_desk["target_id"] = "country_g20_us"
    payload = ap.build_assembly(
        tier=ap.TIER_WORLD, as_of="2026-11-11T12:21:06+00:00",
        candidates=[carried, dropped_same, dropped_other_desk], carried=[carried],
    )
    assert len(payload["tensions"]) == 1
    t = payload["tensions"][0]
    assert t["kind"] == "carried_vs_dropped" and t["same_target"] is True
    # The statement names the desk the gate compared, never "country_composition".
    assert t["b_ref"]["block_desk"] == "energy_security"
    assert "country_composition" not in t["statement"]
    assert "different desks" not in t["statement"]


# ---------------------------------------------------------------------------
# 2. THE EVIDENCE BALANCE
# ---------------------------------------------------------------------------


def test_rule2_the_balance_counts_distinct_sources_and_sets_shared_aside() -> None:
    a_sigs = [_sig("[1]", "s1", "source.irna", "x"), _sig("[2]", "s2", "source.tass", "x"),
              _sig("[3]", "s3", "source.irna", "x"), _sig("[4]", "s9", "source.bbc", "x")]
    b_sigs = [_sig("[7]", "s7", "source.cbc", "x"), _sig("[8]", "s9", "source.bbc", "x")]
    bal = wbe.evidence_balance("A says so [1][2][3][4].", a_sigs, "B says not [7][8].", b_sigs)
    assert bal["a"]["n_sources"] == 2 and bal["a"]["n_signals"] == 3
    assert bal["b"]["n_sources"] == 1 and bal["shared_signals"] == 1
    assert bal["favours"] == "a"
    even = wbe.evidence_balance("[1]", a_sigs[:1], "[7]", b_sigs[:1])
    assert even["favours"] == "balanced"
    line = wbe.balance_sentence(bal, a_label="block 6", b_label="block 7")
    assert line.startswith("Evidence balance (cited signals only): block 6 cites 2 distinct")
    assert line.endswith("the count favours block 6.")


def test_rule2_a_declared_tension_is_weighed_in_payload_and_body() -> None:
    from tests.data_pkg.test_composition_assembly_d2 import _assembly, _row as drow

    up = drow("a", analyst_id="energy_security", target_id="country_g20_us",
              body="**BLUF:** Oil exports through the corridor rose sharply [1].\n",
              signal_ids=("s1", "s2"))
    down = drow("b", analyst_id="energy_security", target_id="country_g20_us",
                body="**BLUF:** Oil exports through the corridor fell sharply [1].\n",
                signal_ids=("s3",))
    _, payload = _assembly([up, down])
    t = payload["tensions"][0]
    assert t[wbe.BALANCE_KEY]["basis"] == wbe.BALANCE_BASIS
    assert t[wbe.BALANCE_KEY]["favours"] in ("a", "b", "balanced")
    assert "Evidence balance (cited signals only)" in ar.render_assembly_body(payload)


def test_rule2_dates_are_stamped_from_signals_and_degrade_without_a_pool() -> None:
    sid = "00000000-0000-4000-8000-000000000001"
    payload = {"tensions": [{wbe.BALANCE_KEY: {
        "a": {"signal_ids": [sid]}, "b": {"signal_ids": []},
    }}]}

    class _Conn:
        async def fetch(self, sql, ids):
            assert "FROM signals" in sql
            return [{"id": sid, "fetched_at": None,
                     "published_at": "2026-11-11T08:21:00+00:00"}]

    out = asyncio.run(wbe.stamp_balance_dates(payload, _Conn()))
    bal = out["tensions"][0][wbe.BALANCE_KEY]
    assert bal["a"]["newest_at"].startswith("2026-11-11T08:21")
    assert asyncio.run(wbe.stamp_balance_dates({"tensions": []}, None)) == {"tensions": []}


# ---------------------------------------------------------------------------
# 3. THE POLITICS FLOOR
# ---------------------------------------------------------------------------


def test_rule3_a_dated_institutional_event_is_the_test() -> None:
    f = wsr.names_dated_institutional_event
    assert f(US_HEGSETH)
    assert f(US_MILLER)                      # "three days after" dates it
    assert not f(PK_REPRESSION)              # detentions, undated
    assert not f(TR_LT)                      # negated, window date only
    assert not f(BF_LT)
    assert not f("the SAF's detention of Ahmed Haroun is now publicly disputed")


def test_rule3_the_us_hegseth_block_beats_pakistan_on_mass() -> None:
    """fd6584eb: the old floor (lexical reports_change) took PK at 0.05 and left
    the 211-204 vote (0.80) off the page."""
    us = _row("country_g20_us", "region_x", 3.0, [
        _block("us/esc", "escalation", 1.65, KINETIC, target="country_g20_us"),
        _block("us/is", "internal_stability", 0.80, US_HEGSETH, target="country_g20_us"),
    ])
    pk = _row("country_watch_pk", "region_x", 2.0, [
        _block("pk/esc", "escalation", 0.8, KINETIC, target="country_watch_pk"),
        _block("pk/is", "internal_stability", 0.05, PK_REPRESSION, target="country_watch_pk"),
    ])
    assert not aw.reports_change(us["data"]["data"]["assembly"]["blocks"][1])
    _, plan = _plan([us, pk], cap=2, slots=1)
    rf = plan.ledger["restraint_floor"]
    assert rf["rule"] == wsr.POLITICS_FLOOR_RULE
    assert [(p["unit"], p["finding_id"], p["action"]) for p in rf["picks"]] == [
        ("country_g20_us", _uid("us/is"), "added"),
    ]
    assert rf["eligible"] == 1
    # ADDED, not swapped: the US keeps its lead (escalation) and the political
    # block takes the slot PK's carry paid for.
    payload = _payload(plan)
    assert [(b["via_unit"], b["desk"]) for b in payload["blocks"]] == [
        ("country_g20_us", "escalation"), ("country_g20_us", "internal_stability"),
    ]
    assert payload["blocks"][1]["added_by_restraint_floor"] is True
    assert rf["displaced"] == [_uid("head/country_watch_pk")]
    assert afa.floor_disclosure(payload).findings == []
    # The fixture carries no head citations (attribution declines); the LEDGER
    # arms — counts, prefix, floor — must hold with the duplicated unit row.
    assert [f for f in aa.audit(payload, []).findings if f.reason.startswith("drop_")] == []


# ---------------------------------------------------------------------------
# 4. THE SHARED THREAD
# ---------------------------------------------------------------------------


def _thread_rows():
    us_esc = _block("us/esc", "escalation", 1.65, KINETIC, target="country_g20_us")
    us_esc["signals"] = [_sig("[1]", "doj", "source.mee", DOJ)]
    ir_esc = _block("ir/esc", "escalation", 2.3, KINETIC, target="country_watch_ir")
    ir_esc["signals"] = [_sig("[1]", "mild", "source.twz", MILDENHALL)]
    gb_esc = _block("gb/esc", "escalation", 0.3, GB_ESC, target="country_g20_gb", tokens=())
    gb_esc["signals"] = [_sig("[68]", "mild", "source.twz", MILDENHALL)]
    gb_mar = _block("gb/mar", "military_posture", 0.4, GB_MARITIME, target="country_g20_gb",
                    tokens=())
    gb_mar["signals"] = [_sig("[68]", "mild", "source.twz", MILDENHALL),
                         _sig("[28]", "doj", "source.mee", DOJ)]
    return [
        _row("country_watch_ir", "region_mena", 3.0, [ir_esc]),
        _row("country_g20_us", "region_americas", 2.0, [us_esc]),
        _row("country_watch_tw", "region_indo", 1.0,
             [_block("tw/esc", "escalation", 0.5, KINETIC, target="country_watch_tw")]),
        _row("country_watch_ht", "region_americas", 0.9,
             [_block("ht/esc", "escalation", 0.6, KINETIC, target="country_watch_ht")]),
        _row("country_g20_gb", "region_indo", 0.7, [gb_esc, gb_mar]),
    ]


def test_rule4_threads_are_the_signals_two_units_heads_cite() -> None:
    threads = wsr.cross_unit_threads(_thread_rows())
    assert [(t["units"], t["signal_ids"]) for t in threads] == [
        (["country_g20_gb", "country_g20_us"], ["doj"]),
        (["country_g20_gb", "country_watch_ir"], ["mild"]),
    ]
    assert all(t["max_magnitude"] >= wsr.THREAD_MAGNITUDE_FLOOR for t in threads)
    low = _thread_rows()
    for r in low:
        for b in r["data"]["data"]["assembly"]["blocks"]:
            for s in b["signals"]:
                s["salience_magnitude"] = 0.3
    assert wsr.cross_unit_threads(low) == []


def test_rule4_the_uk_below_the_cut_takes_a_slot_carrying_its_lead_read() -> None:
    rows = _thread_rows()
    sel, plan = _plan(rows, cap=4)
    assert "country_g20_gb" not in [r["target_id"] for r in sel.carried]
    ts = plan.ledger["thread_slot"]
    assert [(p["unit"], p["finding_id"]) for p in ts["picks"]] == [
        ("country_g20_gb", _uid("gb/esc")),
    ], "the read whose own sentence cites the thread, not the maritime one"
    assert sorted(ts["picks"][0]["partners"]) == ["country_g20_us", "country_watch_ir"]
    payload = _payload(plan)
    gb = [b for b in payload["blocks"] if b["via_unit"] == "country_g20_gb"][0]
    assert gb["carried_by_thread"] is True and gb["carry_reason"] == aw.CARRY_SHARED_THREAD
    displaced = [d for d in payload["drops"]["not_selected"] if d.get("displaced_by_thread")]
    assert [d["target_id"] for d in displaced] == ["country_watch_ht"]
    assert payload[aw.THREADS_KEY] and all("block_ordinals" in t for t in payload[aw.THREADS_KEY])
    assert afa.floor_disclosure(payload).findings == []
    body = ar.render_assembly_body(payload)
    assert "## Shared threads" in body and "carried by the shared-thread slot" in body


def test_rule4_a_stamp_the_plan_did_not_make_is_caught() -> None:
    _, plan = _plan(_thread_rows(), cap=4)
    payload = _payload(plan)
    payload["blocks"][0]["carried_by_thread"] = True
    assert "F6" in afa.floor_disclosure(payload).findings[0].detail


def _floor_then_thread_rows():
    """The shape of world run c27d26ed (2026-11-23 12:20Z; six earlier runs too),
    made-up content: the restraint floor ADDS two carried units' political blocks
    and pays with the two lowest-ranked kinetic carries (NE, YE); NE's head also
    holds a second, non-lead block that cites a signal a carried unit (IR) cites —
    a shared thread whose only uncarried unit is the one the floor displaced."""
    ir_esc = _block("ir/esc", "escalation", 4.4, KINETIC, target="country_watch_ir")
    ir_esc["signals"] = [_sig("[1]", "thr", "source.a", MILDENHALL)]
    ne_eco = _block("ne/eco", "economic_coercion", 0.2, KINETIC, target="country_watch_ne")
    ne_eco["signals"] = [_sig("[1]", "thr", "source.a", MILDENHALL)]
    return [
        _row("country_watch_ir", "region_mena", 8.0, [ir_esc]),
        _row("country_watch_il", "region_mena", 5.0, [
            _block("il/esc", "escalation", 2.1, KINETIC, target="country_watch_il"),
            _block("il/int", "internal_stability", 0.75, IL_EISENKOT,
                   target="country_watch_il"),
        ]),
        _row("country_g20_us", "region_americas", 4.0, [
            _block("us/esc", "escalation", 1.05, KINETIC, target="country_g20_us"),
            _block("us/int", "internal_stability", 0.05, US_MILLER, target="country_g20_us"),
        ]),
        _row("country_watch_sd", "region_africa", 1.6,
             [_block("sd/mil", "military_posture", 1.45, KINETIC, target="country_watch_sd")]),
        _row("country_watch_ye", "region_mena", 1.4,
             [_block("ye/esc", "escalation", 1.3, KINETIC, target="country_watch_ye")]),
        _row("country_watch_ne", "region_africa", 1.2, [
            _block("ne/mil", "military_posture", 0.3, KINETIC, target="country_watch_ne"),
            ne_eco,
        ]),
        _row("country_g20_fr", "region_africa", 0.45,
             [_block("fr/esc", "escalation", 0.2, KINETIC, target="country_g20_fr")]),
    ]


def test_rule4_the_thread_never_promotes_back_a_row_the_floor_displaced() -> None:
    """2026-11-23 (c27d26ed, and 11-14 .. 11-21 before it): the thread slot ran
    after the restraint floor and promoted the very row the floor had just
    displaced, so ``restraint_floor.displaced`` named a carried row, ARM 4(c)
    failed F5 ("1 row(s) stamped displaced_by_restraint_floor but the plan
    displaced 2") and the whole floor was withdrawn before publication."""
    sel, plan = _plan(_floor_then_thread_rows(), cap=6)
    assert [r["target_id"] for r in sel.carried] == [
        "country_watch_ir", "country_watch_il", "country_g20_us",
        "country_watch_sd", "country_watch_ye", "country_watch_ne",
    ]
    rf = plan.ledger["restraint_floor"]
    assert [(p["unit"], p["action"]) for p in rf["picks"]] == [
        ("country_watch_il", "added"), ("country_g20_us", "added"),
    ]
    ne, ye = _uid("head/country_watch_ne"), _uid("head/country_watch_ye")
    assert rf["displaced"] == [ne, ye]
    carried = {r["id"] for r in plan.selection.carried}
    assert not carried & set(rf["displaced"]), "a displaced row is not carried back"
    assert all(p["via_head_id"] not in rf["displaced"]
               for p in plan.ledger["thread_slot"]["picks"])
    payload = _payload(plan)
    stamped = sorted(d["finding_id"] for d in payload["drops"]["not_selected"]
                     if d.get("displaced_by_restraint_floor"))
    assert stamped == sorted([ne, ye])
    assert afa.floor_disclosure(payload).findings == [], "ARM 4(c) is clean"


# ---------------------------------------------------------------------------
# 5. THE NEW-MATERIAL BULLET
# ---------------------------------------------------------------------------

HEAD = (
    "*As of 11 November 2026.*\n\n"
    "**BLUF:** The confrontation is drifting toward negotiation at a cautious pace [1].\n\n"
    "## What changed\n\n"
    "- On 10 November, the House voted 211-204 [2].\n"
    "- This desk's prior read held the same driver; the new material is the "
    "blocked impeachment and the Fairford indictment [3].\n"
)


def _child_block() -> dict:
    head_id = "11111111-1111-4111-8111-111111111111"
    row = {"body": HEAD}
    bluf_start = HEAD.index("The confrontation")
    bluf = build_span(role="bluf", body=HEAD, start_char=bluf_start,
                      end_char=HEAD.index("\n", bluf_start), head_id=head_id)
    return {"finding_id": head_id, "desk": "escalation",
            "spans": [bluf, build_context_span(row, head_id=head_id)], "signals": []}


def test_rule5_the_new_material_bullet_is_quoted_verbatim_with_offsets() -> None:
    span = wbe.window_adds_span(_child_block())
    assert span is not None and span["role"] == "window_adds"
    assert span["text"].startswith("This desk's prior read held the same driver")
    raw = HEAD.encode("utf-8")
    o = span["origin"]
    assert raw[o["start"]:o["end"]].decode("utf-8") == span["text"]
    # Outside the capture the quote arm grades against: not quoted at all.
    assert wbe.window_adds_span(_child_block(), captured=HEAD[:120]) is None
    assert wbe.window_adds_span(_child_block(), captured=HEAD) is not None


def test_rule5_a_world_carry_quotes_it_and_the_quote_arm_passes() -> None:
    child_block = _child_block()
    child_block.update({"ordinal": 1, "target_id": "country_g20_us",
                        "salience": {"cited_mass": 1.0}})
    row = _row("country_g20_us", "region_americas", 1.0, [child_block])
    row["data"]["data"]["citations"] = [{
        "ref_id": child_block["finding_id"], "evidence_text": HEAD, "source": "escalation",
    }]
    payload = ap.build_assembly(
        tier=ap.TIER_WORLD, as_of="2026-11-11T12:21:06+00:00", candidates=[row],
        carried=[row],
    )
    roles = [s["role"] for s in payload["blocks"][0]["spans"]]
    assert roles == ["bluf", "window_adds"]
    assert "the new material is the blocked impeachment" in ar.render_assembly_body(payload)
    res = aa.audit(payload, [])
    assert res.counters.get("assembly_quote_spans_ok") == 2 and res.findings == []


# ---------------------------------------------------------------------------
# 6. CONTESTED FACTS
# ---------------------------------------------------------------------------


def test_rule6_the_haroun_arrest_is_contested_not_resolved_by_recency() -> None:
    child = {"blocks": [
        {"signals": [
            _sig("[44]", "cea8e1dc", "source.jpost.frontpage",
                 "Sudan military arrests former ruling party chairman, Islamist leader "
                 "Ahmed Haroun"),
            _sig("[10]", "522747fd", "source.dabanga.sudan",
                 "Alleged Sudan ‘arrest’ of ICC fugitive Ahmed Haroun in doubt"),
        ]},
        {"signals": [
            _sig("[1]", "d8b3ef0d", "source.allafrica.headlines",
                 "Sudan: Alleged 'Arrest' of ICC Fugitive Ahmed Haroun in Doubt"),
            _sig("[2]", "ae2c1763", "source.africanews.all",
                 "Sudan re-arrests former Bashir official wanted by ICC"),
        ]},
    ]}
    facts = wbe.contested_facts(child, unit_name="Watch — Sudan")
    assert len(facts) == 1
    f = facts[0]
    assert {s["signal_id"] for s in f["affirmed_by"]} == {"cea8e1dc"}
    assert {s["signal_id"] for s in f["disputed_by"]} == {"522747fd", "d8b3ef0d"}
    assert {"haroun", "arrest"} <= set(f["event_terms"])
    line = wbe.contested_sentence(f)
    assert line.startswith("Contested in cited reporting — reported by jpost.frontpage")
    assert "disputed by dabanga.sudan" in line


def test_rule6_nouns_negations_and_generic_words_are_not_contests() -> None:
    child = {"blocks": [{"signals": [
        _sig("[1]", "a", "source.mx",
             "Tanker Linked to Trump's Russian Diesel Waiver Pauses off U.S. East Coast"),
        _sig("[2]", "b", "source.gc",
             "Russian Diesel Tanker Awaits Orders Off U.S. Coast Amid Import Ban Dispute"),
        _sig("[3]", "c", "source.tt",
             "Araghchi: No plans for meeting between Vance and Iran’s security chief"),
        _sig("[4]", "d", "source.mee",
             "Iran denies reports of Rezaei meeting with US Vice President JD Vance"),
    ]}]}
    assert wbe.contested_facts(child, unit_name="Watch — Iran") == []


# ---------------------------------------------------------------------------
# 7. NO ORDINAL FALLBACK ON A THEMATIC SLOT
# ---------------------------------------------------------------------------

IT_NARRATIVE_ABSENCE = (
    "No coordinated narrative appears in this desk's collection through 11 November "
    "2026 — the spread block lists no near-verbatim cross-source reuse across 59 "
    "signals [51][59]."
)


def test_rule7_the_hybrid_slot_is_declared_empty_not_filled_by_fallback() -> None:
    """fd6584eb block 5: hybrid_threat_composition carried Italy's narrative
    absence through fallback_ordinal_1 (its other block was RU's, already taken)."""
    ru_mil = _block("ru/mil", "military_posture", 3.95, KINETIC, target="country_g20_ru")
    rows = [
        _row("country_g20_ru", "region_europe", 5.0, [dict(ru_mil)]),
        _row("hybrid_threat_composition", "thematic:hybrid_threat_composition", 2.0, [
            dict(ru_mil),
            _block("it/nar", "narrative_coordination", 0.1, IT_NARRATIVE_ABSENCE,
                   target="country_g20_it"),
        ], analyst="hybrid_threat_composition"),
        _row("country_g20_us", "region_americas", 1.0,
             [_block("us/esc", "escalation", 1.0, KINETIC, target="country_g20_us")]),
    ]
    sel, plan = _plan(rows, cap=3)
    hybrid = _uid("head/hybrid_threat_composition")
    assert hybrid in [r["id"] for r in sel.carried]
    assert hybrid not in [r["id"] for r in plan.selection.carried]
    assert plan.ledger["empty_slots"][0]["via_head_id"] == hybrid
    payload = _payload(plan)
    assert len(payload["blocks"]) == 2
    assert all(b["carry_reason"] != "fallback_ordinal_1" for b in payload["blocks"])
    row = [d for d in payload["drops"]["not_selected"] if d["finding_id"] == hybrid][0]
    assert row["declared_empty_slot"] is True
    assert payload["floor"]["honoured"] is True
    assert payload["floor"]["declared_empty"] == ["thematic:hybrid_threat_composition"]
    assert afa.floor_disclosure(payload).findings == []
    assert "1 slot declared empty: hybrid_threat_composition" in ar.render_assembly_body(payload)


def test_rule7_a_thematic_carry_with_its_own_block_is_unchanged() -> None:
    rows = [
        _row("hybrid_threat_composition", "thematic:hybrid_threat_composition", 2.0,
             [_block("hyb/gb", "escalation", 0.6, KINETIC, target="country_g20_gb")],
             analyst="hybrid_threat_composition"),
    ]
    _, plan = _plan(rows, cap=1)
    assert plan.ledger["empty_slots"] == []
    assert _payload(plan)["blocks"][0]["carry_reason"] == "by_mass"
