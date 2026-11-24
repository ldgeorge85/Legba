# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The ``institutional_change`` salience lift (world read dig 2026-11-07, item 1).

Political and institutional change scored 0.30-0.45 under the 0.50
``MASS_FLOOR``, so internal_stability desks citing the Wiles to Miller swap
carried ``cited_mass`` of about zero. The fixtures below are real signal texts
(the scorer's own title-plus-lead text, trimmed) with the magnitude the model
gave them on 2026-11-06/07: the acts must lift over the floor, and the routine
political chatter (opinion, polls, rallies, explainers, hedged or non-state
items) must stay exactly where the model put it.
"""
from __future__ import annotations

import json

import pytest

from legba.data import taxon_families as taxa
from legba.data.analysts import salience_institutional as si
from legba.data.analysts import signal_salience as ss

# (scorer text, source_class, model magnitude)
LIFTS: list[tuple[str, str, float]] = [
    ("Trump names Stephen Miller new White House chief of staff after midterm defeat — WASHINGTON, Nov. 6 (Yonhap) -- U.S. President Donald Trump announced Friday that...", "reporting", 0.45),
    ("Trump promotes Stephen Miller to chief of staff, replacing Susie Wiles — BLUF: President Trump replaced Chief of Staff Susie Wiles with Stephen Miller following Republican losses in the midterms", "reporting", 0.5),
    ("Stephen Miller named by Trump as new White House chief of staff — Trump said he would be replacing Susie Wiles for the top advisory role.", "reporting", 0.35),
    ("Trump faces criticism for ‘dangerous’ Stephen Miller appointment as White House chief of staff — <p>Democrats and Republicans blast promotion of ‘xenophobe’", "reporting", 0.3),
    ("US Republican lawmaker to force House vote on Hegseth’s impeachment unless he resigns — Thomas Massie plans to trigger vote when Congress returns next week", "state_media", 0.45),
    ("BLUF: House Republicans will begin impeachment proceedings against Defense Secretary Pete Hegseth next week, blaming him for the Iran war's role in the GOP's Nov. 3 midterm losses", "reporting", 0.55),
    ("Defiant Hegseth tells Democrats their impeachment push ‘won’t work’ — US Secretary of Defence Pete Hegseth said he will “not back down” against a “lame duck” Congress", "reporting", 0.45),
    ("Bennett to recommend Eisenkot as prime minister, B'Yachad party says ahead of meeting with Herzog — <img align=\"right\" alt=\"Gadi Eisenkot and Naftali Bennett meet\"", "reporting", 0.3),
    ("Israel's main Arab list backs Eisenkot to form government after election — Israel's largest Arab party grouping said Thursday it would recommend opposition leader Gadi Eisenkot", "reporting", 0.5),
    ("Israel’s Yashar party urges Herzog to grant its leader mandate to form government — Opposition party says 67 recommendations signal Israelis’ desire for change", "state_media", 0.5),
    ("Turkiye’s parliament ratifies Mecca defence agreement — Parliament&#039;s approval lays the groundwork for further Turkish involvement in Saudi Arabia&#039;s conflict with the Houthis.", "reporting", 0.35),
    ("Turkish Parliament Ratifies Makkah Defense Pact", "reporting", 0.5),
    ("Democrats win control of US House in midterm elections, Decision Desk HQ projects — Democrats appeared to ride a wave of voter dissatisfaction", "reporting", 0.55),
    ("Spanish transport minister resigns after deadly train crash — <p>Óscar Puente also announces his withdrawal from upcoming election following disaster that killed 69 people</p>", "reporting", 0.45),
    ("Germany names army chief Freuding as next top general — Army commander replaces Breuer who moves to chair NATO’s Military Committee", "state_media", 0.25),
]

ROUTINE: list[tuple[str, str, float]] = [
    ("Who is Stephen Miller, Trump’s new White House chief of staff? — Miller has been a close confidant of Trump since 2016", "reporting", 0.25),
    ("Five Things To Know About Stephen Miller, Trump’s New Chief of Staff — Miller has long shaped many of the president’s most aggressive policies", "reporting", 0.35),
    ("Trump announces Stephen Miller as administration's new chief of staff - analysis — <img align=\"right\" alt=\"White House Deputy Chief of Staff Stephen Miller\"", "reporting", 0.4),
    ("Trump’s new chief of staff Stephen Miller says he ‘saved America’ — Stephen Miller, US President Donald Trump’s incoming chief of staff praises him.", "reporting", 0.3),
    ("Katie Miller, wife of new WH Chief of Staff, faces backlash after admitting she skipped Virginia midterm voting: ‘I prefer my own safety’ — Katie Miller drew criticism", "reporting", 0.1),
    ("‘Knives out’: Will Congress impeach Pete Hegseth after GOP midterms loss? — Democrats can adopt the impeachment articles in the House but will need Republican backing", "reporting", 0.3),
    ("Thousands rally in Israel urging Eisenkot-Arab deal to remove Netanyahu — In a counterprotest, Netanyahu supporters demand that Israel should not have a government", "reporting", 0.4),
    ("EXPLAINER - Who could form Israel's next government after election deadlock? — - Final count gives Netanyahu’s bloc 53 seats, Eisenkot’s bloc 48", "state_media", 0.5),
    ("Eisenkot may get the mandate, but haredi parties hold key to Israel's next government - analysis — <img align=\"right\" alt=\"Shas leader MK Arye Deri\"", "reporting", 0.4),
    ("Israel needs a bold, fresh approach and unity government in wake of election stalemate - editorial — <img align=\"right\" alt=\"United Arab List (Ra’am) party chairman\"", "reporting", 0.4),
    ("US midterms: 2 days to go – what polls say as Trump, El-Sayed hit the trail — Democrats remain ahead in national polls as Trump and candidates make their final push", "reporting", 0.4),
    ("Democrats lead in 5 Republican-held Senate races ahead of US midterms: Polls — New York Times/Siena surveys show Democratic candidates ahead in Texas, Kansas", "state_media", 0.4),
    ("Polls show Trump's party could lose control of Congress but they may be wrong — Polls say the Democratic Party could be about to seize control of the US Congress", "reporting", 0.4),
    ("Trump rallies in Iowa, Kansas to maintain Republican control of US Senate — US President Trump returns to the campaign trail", "reporting", 0.35),
    ("Trump launches racist diatribe against Abdul El-Sayed at Iowa rally — <p>President again took aim at progressive Senate candidate and lauded Haley Stevens for endorsing Mike Rogers</p>", "reporting", 0.3),
    ("Trump approval near record low ahead of midterm elections, Reuters/Ipsos poll finds — <img align=\"right\" alt=\"US President Donald Trump delivers remarks\"", "reporting", 0.4),
    ("OPINION - Did alignment with Israel influence Serbia’s elections? — Pledging a break with past foreign policy, the Student List manifesto vows", "state_media", 0.15),
    ("DIGITAL POLITICS OP-ED: The anti-foreigner movement squares up to the state in run-up to elections — From xenophobic campaigning to farm attacks", "reporting", 0.45),
    ("Cornell faculty pass no-confidence vote over leadership's handling of Jane Doe allegations — Friday's resolution by academic staff is largely symbolic", "reporting", 0.15),
    ("US President Donald Trump vowed to become \"Mr. Veto\" and block legislation if Democrats win control of Congress in Tuesday's midterm elections, according to an interview published Monday.", "reporting", 0.45),
    ("Belgian Prime Minister may resign on Monday — newspaper — Unsuccessful attempts to coordinate the budget may become the reason to step down from the office", "state_media", 0.4),
    ("Florida declares state of emergency in 7 counties after heavy rains trigger flooding; roads closed, schools disrupted — Governor Ron DeSantis issued the emergency order", "reporting", 0.35),
    ("Democrats may seek Trump’s impeachment after midterms — political analyst — \"Given the midterm election results, President Donald Trump’s domestic political standing is clearly weakening,\"", "state_media", 0.3),
]


def _sal(mag: float, cls: str = "other") -> dict:
    return {"event_class": cls, "actor_rank": "individual", "magnitude": mag,
            "authority": "reporting", "confidence": 0.8}


@pytest.mark.parametrize("text,source_class,mag", LIFTS)
def test_institutional_acts_lift_over_the_mass_floor(text, source_class, mag) -> None:
    out = si.lift_salience(_sal(mag), text, source_class=source_class)
    assert out is not None, text
    assert out["magnitude"] > ss.MASS_FLOOR
    assert out["magnitude"] < 0.85          # never into the kinetic / leader-death band
    assert out["event_class"] == si.INSTITUTIONAL_CHANGE
    assert out["lift"]["rule"] == si.INSTITUTIONAL_LIFT_VERSION
    assert out["lift"]["from_magnitude"] == mag and out["lift"]["from_class"] == "other"


@pytest.mark.parametrize("text,source_class,mag", ROUTINE)
def test_routine_political_chatter_stays_where_the_model_put_it(text, source_class, mag) -> None:
    assert si.recognise_institutional_change(text, source_class=source_class) is None, text
    assert si.lift_salience(_sal(mag), text, source_class=source_class) is None


def test_fixture_sets_are_the_sizes_the_brief_names() -> None:
    assert len(LIFTS) >= 12 and len(ROUTINE) >= 20


def test_subclasses_and_anchors() -> None:
    cases = {
        "Trump names Stephen Miller new White House chief of staff": "personnel",
        "BLUF: House Republicans will begin impeachment proceedings against Defense Secretary Pete Hegseth": "removal",
        "Bennett to recommend Eisenkot as prime minister": "government_formation",
        "Turkish Parliament Ratifies Makkah Defense Pact": "ratification",
        "Democrats win control of US House in midterm elections": "legislative_control",
        "Military government declares martial law across the country": "emergency_rule",
        "Interim leader sworn in as president after parliament vote": "head_of_government",
    }
    for text, sub in cases.items():
        m = si.recognise_institutional_change(text, source_class="reporting")
        assert m is not None and m.subclass == sub, (text, m)
        assert m.anchor == si.ANCHORS[sub]
    assert all(ss.MASS_FLOOR < a < 0.85 for a in si.ANCHORS.values())


def test_legislative_control_is_read_on_the_title_only() -> None:
    # A personnel story whose body recalls last week's result is personnel.
    text = ("Trump names Stephen Miller as White House Chief of Staff — Trump said he would be "
            "replacing Susie Wiles, days after Republicans lost control of Congress")
    m = si.recognise_institutional_change(text, source_class="reporting")
    assert m is not None and m.subclass == "personnel"
    # And a body-only mention of control never lifts on its own.
    body_only = "Lecce warns that Democrats offer no tariff cure — Democrats won control of Congress last week"
    assert si.recognise_institutional_change(body_only, source_class="reporting") is None


def test_analysis_sources_never_lift() -> None:
    text = "Kremlin appoints new defence minister after reshuffle"
    assert si.recognise_institutional_change(text, source_class="reporting") is not None
    assert si.recognise_institutional_change(text, source_class="analysis") is None


def test_the_lift_never_lowers_and_skips_degraded_or_already_lifted() -> None:
    text = "Trump names Stephen Miller new White House chief of staff"
    assert si.lift_salience(_sal(0.7, "coup_unrest"), text) is None        # already over the anchor
    assert si.lift_salience({"magnitude": None, "degraded": True}, text) is None
    once = si.lift_salience(_sal(0.3), text)
    assert once is not None and si.lift_salience(once, text) is None       # idempotent
    assert si.lifted_magnitude(None, si.recognise_institutional_change(text)) is None


def test_bound_class_none_keeps_the_model_class() -> None:
    out = si.lift_salience(_sal(0.3, "diplomatic_rupture"), "Turkish Parliament Ratifies Makkah Defense Pact",
                           bound_class=None)
    assert out is not None and out["event_class"] == "diplomatic_rupture"
    assert out["lift"]["from_class"] == "diplomatic_rupture"


def test_deterministic_same_text_same_answer() -> None:
    for text, sc, _ in LIFTS + ROUTINE:
        a = si.recognise_institutional_change(text, source_class=sc)
        b = si.recognise_institutional_change(text, source_class=sc)
        assert a == b


def test_institutional_change_is_a_registered_salience_class() -> None:
    assert si.INSTITUTIONAL_CHANGE in taxa.SALIENCE_CLASSES
    assert si.INSTITUTIONAL_CHANGE in ss.EVENT_CLASSES
    assert ss._coerce_event_class("institutional_change") == "institutional_change"
    assert taxa.SALIENCE_CLASSES[-1] == "other"


# ---------------------------------------------------------------------------
# The scorer wiring: score_signals runs the lift on every scored verdict.
# ---------------------------------------------------------------------------


class _CannedLLM:
    def __init__(self, content: str) -> None:
        self._content = content

    async def chat_complete(self, messages, *, max_tokens=None, temperature=None, system=None, **kw):
        from types import SimpleNamespace
        return SimpleNamespace(content=self._content)


class _FakeConn:
    def __init__(self, rows) -> None:
        self._rows = rows
        self.writes: list[tuple[str, dict]] = []

    async def fetch(self, sql, *params):
        return list(self._rows)

    async def execute(self, sql, *params):
        self.writes.append((params[0], json.loads(params[1])))
        return "UPDATE 1"


@pytest.mark.asyncio
async def test_score_signals_lifts_the_miller_swap_and_leaves_the_poll() -> None:
    rows = [
        {"id": "aaa", "payload": {"title": "Trump names Stephen Miller new White House chief of staff after midterm defeat"},
         "source_class": "reporting"},
        {"id": "bbb", "payload": {"title": "Trump approval near record low ahead of midterm elections, Reuters/Ipsos poll finds"},
         "source_class": "reporting"},
        {"id": "ccc", "payload": {"title": "US missile strike hits IRGC position"}, "source_class": "reporting"},
    ]
    content = json.dumps([
        {"id": "aaa", "event_class": "other", "actor_rank": "individual", "magnitude": 0.45, "confidence": 0.8},
        {"id": "bbb", "event_class": "other", "actor_rank": "head_of_state", "magnitude": 0.4, "confidence": 0.8},
        {"id": "ccc", "event_class": "kinetic_strike", "actor_rank": "state_organ", "magnitude": 0.9, "confidence": 0.9},
    ])
    conn = _FakeConn(rows)
    examined, scored, sample = await ss.score_signals(
        conn, _CannedLLM(content), apply=True, max_rows=10, batch_size=12,
        model_id="m", now_iso="2026-11-07T00:00:00+00:00")
    assert examined == 3 and scored == 3
    w = dict(conn.writes)
    assert w["aaa"]["magnitude"] == si.ANCHORS["personnel"]
    assert w["aaa"]["event_class"] == "institutional_change"
    assert w["aaa"]["lift"]["subclass"] == "personnel"
    assert w["aaa"]["lift"]["from_magnitude"] == 0.45
    assert w["bbb"]["magnitude"] == 0.4 and "lift" not in w["bbb"]
    assert w["ccc"]["magnitude"] == 0.9 and w["ccc"]["event_class"] == "kinetic_strike"
    # every scored row says which lift rule it was checked against
    assert {s["lift_rule"] for s in w.values()} == {si.INSTITUTIONAL_LIFT_VERSION}
    # the strike still outranks the cabinet change
    assert ss.salience_sort_key(w["ccc"]) > ss.salience_sort_key(w["aaa"]) > ss.salience_sort_key(w["bbb"])
    assert any(s.get("lifted") == "personnel" for s in sample)
    assert ss.HANDLER_VERSION == "0.2.0"


def test_lift_keeps_the_model_class_until_the_family_holds_the_value() -> None:
    class _Vocab:
        def values(self, family):
            return {"other", "kinetic_strike"} if family == taxa.SALIENCE_CLASS else set()

    v = ss.SalienceVerdict(signal_id="x", event_class="other", actor_rank="individual",
                           magnitude=0.3, authority="reporting", confidence=0.8)
    row = ss.SignalRow(id="x", text="Turkish Parliament Ratifies Makkah Defense Pact", source_class="reporting")
    prev = taxa.installed_vocabulary()
    taxa.install_vocabulary(_Vocab())
    try:
        out = ss.apply_institutional_lift(v, row)
    finally:
        taxa.install_vocabulary(prev)
    assert out.magnitude == si.ANCHORS["ratification"]
    assert out.event_class == "other"           # not yet in the live family: never "unclassified"
    assert out.lift and out.lift["subclass"] == "ratification"


def test_degraded_verdict_passes_through_untouched() -> None:
    v = ss.SalienceVerdict(signal_id="x", event_class="other", actor_rank="none",
                           magnitude=None, authority="reporting", confidence=0.0, degraded=True)
    row = ss.SignalRow(id="x", text="Trump names Stephen Miller new chief of staff", source_class="reporting")
    assert ss.apply_institutional_lift(v, row) is v
