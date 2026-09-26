# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The TITLE-FRAME GAUGE's classifiers (TITLE-FRAME-FIX / review §5.2 P3).

Every fixture title below is a REAL one, taken verbatim from
``analyst_outputs.title`` on 2026-09-01, because the gauge's whole claim is that
it separates two eras of live output that every lexical-diversity metric scored
the wrong way round. A gauge tested only on invented strings would be a gauge
tested only on the shape its author was already imagining.

The anchors it is calibrated against, from ``VOICE_ORGANIC_REVIEW_2026-09-01``
§1.1 and the appendix:

    world E0 (pre-07-01)   strict frame 0/60   ·  no named entity 60/60
    world E2 (post-08-04)  strict frame 54/58 (93.1%)
    escalation_composition E2  strict frame 43/56 (76.8%)
    world E1 -> E2 concordance 0.662 -> 0.458

Measured over the live corpus, this implementation reads world E0 0.000 /
roll-call 1.000, world E2 0.914, escalation_composition E2 0.768, and world
concordance 0.631 -> 0.484. The two ERA-DEFINING extremes reproduce exactly;
the middle numbers sit within a few points of the review's, which used a
449-term gazetteer this module deliberately does not carry.
"""
from __future__ import annotations

import pytest

from legba.data.analysts.deterministic_handlers import _title_frame_gauge as g

# Real world_assessor titles, 2026-08-13 .. 2026-09-01, all strict-frame.
_E2_FRAMED = [
    "Myanmar airstrike campaign drives global escalation risk",
    "DRC Ebola surge eclipses other escalation risks",
    "Hormuz blockade drives global oil risk amid rising regional escalations",
    "Russia's maritime drills amplify global escalation risk",
    "Ukraine drone surge fuels global escalation risk",
    "Sudan offensive steadies as top global escalation driver",
    "Intensifying proxy attacks lift South Asian escalation risk amid global drone surge",
]

# Real world_assessor titles from before 2026-07-01 — the deterministic era the
# predicate frame was installed to replace.
_E0_ROLL_CALL = [
    "World situational assessment - 2026-06-16",
    "World situational assessment - 2026-06-29",
    "Assessment for target",
]


# ---------------------------------------------------------------------------
# The predicate frame
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("title", _E2_FRAMED)
def test_the_lock_era_titles_all_read_as_the_frame(title: str) -> None:
    v = g.classify_frame(title)
    assert v.strict, title
    assert v.verb is not None and v.risk_noun is not None


@pytest.mark.parametrize("title", _E0_ROLL_CALL)
def test_the_pre_lock_era_titles_read_as_no_frame(title: str) -> None:
    assert not g.classify_frame(title).strict, title


def test_the_frame_needs_a_subject_in_front_of_its_verb() -> None:
    """A verb-initial title is an imperative, not a crowning.

    Without this the gauge would score any sentence containing a driving verb
    and a risk noun, which is most sentences the tier writes.
    """
    assert not g.classify_frame("Drives global escalation risk").strict
    assert g.classify_frame("Sudan drives global escalation risk").strict


def test_the_frame_needs_the_risk_noun_AFTER_the_verb() -> None:
    """``<risk noun> <verb> <subject>`` is a different sentence, not the frame.

    This is the check that stops the gauge scoring the inverted form as a lock,
    which would make an actual improvement look like no change at all.
    """
    assert not g.classify_frame("Escalation risk reshapes Sudan's calculus").strict


def test_the_last_governing_verb_wins() -> None:
    """Review §1's rule: scan ALL driving verbs, take the LAST that governs a
    risk object — so a title whose frame is at the END is still caught, and the
    subject is the span before THAT verb rather than before the first one."""
    v = g.classify_frame(
        "Sudan's offensive widens as Hormuz traffic halts, raising oil risk"
    )
    assert v.strict
    assert v.verb == "raising"


def test_the_conservative_reading_drops_the_copular_verbs() -> None:
    """``faces`` / ``sees`` inflate the region tier's frame rate from 30.7% to
    70.3% (review §1.1), so both readings ship and the honest one is named."""
    v = g.classify_frame("Nigeria faces a widening security risk")
    assert v.strict and not v.core
    v = g.classify_frame("Nigeria amplifies a widening security risk")
    assert v.strict and v.core


def test_the_risk_object_window_does_not_span_the_whole_title() -> None:
    """A risk noun 200 characters downstream is not governed by the verb.

    The 120-char window is the review's own, kept so the numbers stay
    comparable with its census rather than drifting into a wider definition
    that would flatter any fix.
    """
    far = "Sudan drives " + ("a very long clause about logistics " * 6) + "risk"
    assert len(far) > 120
    assert not g.classify_frame(far).strict


# ---------------------------------------------------------------------------
# The roll-call counter-metric
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("title", _E0_ROLL_CALL)
def test_the_pre_lock_era_is_the_roll_call_class(title: str) -> None:
    """The floor this train must not fall through.

    ``no named entity`` is the feature that separates the eras cleanly in the
    census — 60/60 before 07-01 — so it is the feature the counter-metric uses.
    """
    assert g.is_roll_call(title), title
    assert g.is_masthead(title), title


def test_the_empty_universal_is_roll_call_too() -> None:
    """The degenerate case of the CURRENT contract, live on 2026-08-30.

    Review §2.e: with no block dominating, the model is still required to name
    one subject in 90 characters, has nothing to pick, and emits the empty
    universal. It names nobody, so it fails the same floor the masthead does —
    but it does assert something, so it is not a masthead.
    """
    title = "Global escalation risk stays elevated across multiple regions"
    assert g.is_roll_call(title)
    assert not g.is_masthead(title)


@pytest.mark.parametrize("title", _E2_FRAMED)
def test_the_lock_era_titles_are_not_roll_call(title: str) -> None:
    """The frame is a defect, but it is not THIS defect — the two metrics must
    move independently or the gauge cannot tell a fix from a swap."""
    assert not g.is_roll_call(title), title


def test_a_multi_subject_headline_passes_both_floors() -> None:
    """The shape the new contract asks for: plural AND concrete.

    This is the acceptance case for the whole train — it must read as neither
    the frame nor the roll call, which is exactly the gap the old contract left
    no room for.
    """
    title = "Sudan's offensive widens while Sahel coup pressure eases"
    assert not g.classify_frame(title).strict
    assert not g.is_roll_call(title)
    assert g.entity_tokens(title) == ["Sudan's", "Sahel"]


def test_the_frame_and_the_crown_are_different_measurements() -> None:
    """``crowned_rate`` splits the sentence SHAPE from the act of crowning.

    The review's frame definition is about grammar and is silent on how many
    subjects fill the slot, so a plural-subject headline in the frame scores as
    frame — correctly — while being a materially different headline. Without the
    split, a change that un-crowned the read without changing its grammar would
    read as no change at all, and a change that only reshuffled the grammar
    would read as a cure.
    """
    crowned = "Myanmar air strikes drive global escalation risk"
    plural = "Myanmar instability and Ukraine drone surge drive global escalation risk"
    unframed = "Sudan's offensive widens while Sahel coup pressure eases"

    assert g.classify_frame(crowned).strict and g.is_crowned(crowned)
    assert g.classify_frame(plural).strict and not g.is_crowned(plural)
    assert not g.classify_frame(unframed).strict and not g.is_crowned(unframed)

    out = g.gauge_rows([
        {"analyst_id": "world_assessor", "title": t, "body": _BODY}
        for t in (crowned, plural, unframed)
    ])["world_assessor"]
    # Rounded to 4dp by the roll-up, which is what lands in the finding.
    assert out["frame_rate"] == round(2 / 3, 4)
    assert out["crowned_rate"] == round(1 / 3, 4)


def test_an_abstraction_is_not_a_named_subject() -> None:
    """Found by the TITLE-FRAME-FIX replay, in arm B's own output.

    ``Energy-security pressures and kinetic triggers heighten global escalation
    risk`` is plural, un-crowned, and names nobody — the empty universal reached
    by a different road than the masthead. It scored as naming somebody because
    ``Energy-security`` is ONE token and is not in the stoplist while both of
    its parts are. A roll-call counter with that hole is blind to exactly the
    abstraction an un-crowned headline reaches for, so it was the counter that
    got fixed and not the case that got excused.
    """
    title = "Energy-security pressures and kinetic triggers heighten global escalation risk"
    assert g.entity_tokens(title) == []
    assert g.is_roll_call(title)


def test_a_hyphenated_real_name_still_counts() -> None:
    """The other half: the hyphen rule folds compounds of GENERICS only.

    ``Asia-Pacific`` has no generic part and must survive, or the fix above
    would have bought its coverage by breaking the live titles §1's normalisation
    tests already depend on.
    """
    assert g.entity_tokens("Asia-Pacific repression widens") == ["Asia-Pacific"]
    assert not g.is_roll_call("Asia-Pacific repression widens")


def test_a_pattern_headline_without_names_is_still_roll_call() -> None:
    """"Three theatres escalate at once" names nobody.

    Deliberate, and the reason the contract's PATTERN shape requires the
    theatres to be named: an un-named pattern is the empty universal wearing a
    number, and the gauge must not reward it.
    """
    assert g.is_roll_call("Three theatres escalate in the same week")
    assert not g.is_roll_call("Sudan, Myanmar and Haiti all widen in one week")


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------


def test_typography_does_not_split_a_subject() -> None:
    """Live titles carry U+2011 and curly apostrophes; NFKC folds neither.

    Without this the crown streak would break on a typographic difference and
    report churn where the tier crowned the same subject twice.
    """
    a = "Asia‑Pacific repression drives world’s gravest humanitarian risk"
    b = "Asia-Pacific repression drives world's gravest humanitarian risk"
    assert g.normalize(a) == g.normalize(b)
    assert g.subject_key(a) == g.subject_key(b)


def test_fullwidth_citation_brackets_fold() -> None:
    assert g.normalize("Sudan 【4】 offensive") == "Sudan [4] offensive"


# ---------------------------------------------------------------------------
# Streak / churn
# ---------------------------------------------------------------------------


def test_a_held_crown_is_one_streak_across_reworded_titles() -> None:
    """Burkina Faso held the world crown for 10 consecutive runs under 10
    DIFFERENT titles. A streak metric keyed on the title string would have
    reported 10 distinct crowns and seen nothing."""
    longest, churn = g.streak_and_churn([
        "Myanmar air strikes drive global escalation risk",
        "Myanmar airstrike campaign drives global escalation risk",
        "Myanmar troop surge and regional flashpoints heighten global escalation risk",
    ])
    assert longest == 3
    assert churn == 0.0


def test_churn_is_reported_from_the_other_end() -> None:
    """67% of consecutive world runs change the crown. Repetition and churn are
    OPPOSITE failures and a gauge that only watched one would call the other
    healthy."""
    longest, churn = g.streak_and_churn([
        "Sudan offensive drives global escalation risk",
        "Myanmar air strikes drive global escalation risk",
        "Haiti gang violence spikes, raising global escalation risk",
    ])
    assert longest == 1
    assert churn == 1.0


def test_a_single_title_has_no_churn_rather_than_zero_churn() -> None:
    """``None``, not 0.0 — an undefined ratio printed as zero reads as
    'perfectly stable', which is the exact class of false green §5.0 retires
    title entropy for."""
    assert g.streak_and_churn(["Sudan offensive drives global escalation risk"]) == (1, None)
    assert g.streak_and_churn([]) == (0, None)


# ---------------------------------------------------------------------------
# Dimensions
# ---------------------------------------------------------------------------


def test_dimension_coverage_sees_the_monoculture() -> None:
    """0 of 58 world titles named leadership, narrative, coercion or
    proliferation. The gauge has to be able to SAY that."""
    dims: set[str] = set()
    for t in _E2_FRAMED:
        dims |= g.title_dimensions(t)
    assert "escalation" in dims
    assert "energy_security" in dims
    assert not dims & {
        "leadership_transition",
        "narrative_coordination",
        "economic_coercion",
        "proliferation_watch",
    }


def test_the_unnamed_dimensions_are_detectable_when_present() -> None:
    """The other half of the claim above: the four missing dimensions are
    missing from the OUTPUT, not from the classifier."""
    assert "leadership_transition" in g.title_dimensions(
        "Thailand's prime minister resigns, opening a contested succession"
    )
    assert "economic_coercion" in g.title_dimensions(
        "US tariffs on Brazilian steel take effect"
    )
    assert "proliferation_watch" in g.title_dimensions(
        "IAEA loses safeguards access at Natanz"
    )
    assert "narrative_coordination" in g.title_dimensions(
        "Coordinated messaging campaign targets Moldova's referendum"
    )


# ---------------------------------------------------------------------------
# Concordance
# ---------------------------------------------------------------------------


_BODY = """*As of 1 September 2026*

**BLUF:** Myanmar's sustained high-tempo air-strike campaign remains the
world's most consequential escalation risk.

## The picture

Myanmar's air-strike surge continued through August, with strikes on
Sagaing reported on 29 August.

Across other theaters, Iran's IRGC expanded kinetic operations in the Gulf
while Sudan's army deployed a brigade to Blue Nile.

In the Americas, Haiti's gang-related massacres have killed at least 47
people since 20 August.

## Coverage

The proliferation_watch reads are below the verification floor.
"""


def test_concordance_names_the_headline_body_gap() -> None:
    """The defect the review actually found: a crowned headline over a body
    that names 17 entities. Two of five prose paragraphs mention Myanmar — the
    BLUF and the one paragraph the crowned subject gets; the other three carry
    Iran, Sudan, Haiti and the coverage footer between them."""
    score = g.concordance(
        "Myanmar airstrike campaign drives global escalation risk", _BODY
    )
    assert score is not None
    assert score == pytest.approx(0.4)


def test_concordance_drops_headings_and_the_as_of_line() -> None:
    """The review's paragraph filter, so the denominator is PROSE.

    Counting the ``## The picture`` line and the italic dateline as paragraphs
    would depress every score by a constant and make the metric untethered from
    the published 0.458.
    """
    paras = g.prose_paragraphs(_BODY)
    assert all(not p.startswith("#") for p in paras)
    assert not any("As of 1 September" in p for p in paras)
    assert len(paras) == 5


def test_concordance_is_unaskable_not_zero_for_a_subjectless_headline() -> None:
    """A headline that names nobody has no subject to look for, and scoring it
    0.0 would blame the body for the headline's emptiness — the wrong metric
    would light up and the roll-call counter would not."""
    assert g.concordance("Global escalation risk stays elevated", _BODY) is None
    assert g.concordance("Myanmar strikes continue", "") is None


# ---------------------------------------------------------------------------
# The roll-up
# ---------------------------------------------------------------------------


def test_gauge_rows_groups_per_analyst_and_skips_untitled_rows() -> None:
    rows = [
        {"analyst_id": "world_assessor", "title": t, "body": _BODY}
        for t in _E2_FRAMED
    ] + [
        {"analyst_id": "region_composition", "title": "Sahel coup pressure eases as Mali reopens talks", "body": _BODY},
        {"analyst_id": "world_assessor", "title": "   ", "body": _BODY},
        {"analyst_id": "", "title": "orphan", "body": _BODY},
    ]
    out = g.gauge_rows(rows)
    assert set(out) == {"world_assessor", "region_composition"}
    assert out["world_assessor"]["n"] == len(_E2_FRAMED)
    assert out["world_assessor"]["frame_rate"] == 1.0
    assert out["region_composition"]["frame_rate"] == 0.0
    assert out["region_composition"]["crown_churn"] is None


def test_gauge_rows_reports_none_not_zero_for_empty_denominators() -> None:
    out = g.gauge_rows([
        {"analyst_id": "world_assessor",
         "title": "Global escalation risk stays elevated", "body": _BODY},
    ])
    m = out["world_assessor"]
    assert m["roll_call_rate"] == 1.0
    assert m["concordance"] is None
    assert m["concordance_n"] == 0
    assert m["crown_churn"] is None
