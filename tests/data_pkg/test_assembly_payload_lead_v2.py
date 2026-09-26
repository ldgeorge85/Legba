# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""AMENDMENT 4b — LEAD TEST v2, the crown, behind ``LEGBA_LEAD_TEST_V2``.

Three rules, one switch (``assembly_payload.LEAD_TEST_V2_ENV``):

  1. the count bar is relative to the roster the desk could FIELD —
     ``max(LEAD_CANDIDATE_FLOOR, MIN_LEAD_CANDIDATES - no_head)`` — instead of
     an absolute 8 that a seven-dimension roster can never meet;
  2. the earned crown is the ARGMAX-mass block instead of unconditionally
     ordinal 1;
  3. the co-lead band is anchored on ``max(masses)`` and emitted in mass order
     instead of being anchored on ordinal 1 by construction.

The defect these repair is ONE anchor bug wearing three costumes. Live, at
2026-09-06 23:30Z: Canada cleared both substantive bars (ratio 2.50, share
0.714) and was refused on the count alone; 4 of the 5 desks that DID earn a
lead crowned a block that did not hold the top mass; and every co-lead band in
the fleet began at ordinal 1 because ``m >= 0.60 * ms[0]`` is trivially true at
index 0.

The FOURTH rule that is deliberately absent is worth a test too: the block
ORDER stays severity-first under both regimes (the policy lane measured
mass-first and found it makes energy_security's dominance worse). A crown
change that also reordered the page would be a different instrument.

Flag OFF must be byte-identical to the shipped payload, ``lead.test`` KEYS
included — that is what makes this landable pre-T0 as a flip rather than as a
migration.
"""
from __future__ import annotations

import pytest

from legba.data.analysts import assembly_payload as ap


# ---------------------------------------------------------------------------
# The switch itself
# ---------------------------------------------------------------------------


def test_the_flag_is_off_by_default_and_reads_the_house_three_state_grammar(
    monkeypatch,
):
    monkeypatch.delenv(ap.LEAD_TEST_V2_ENV, raising=False)
    assert ap.lead_test_v2_enabled() is False
    for raw, wanted in (("0", False), ("", False), ("1", True)):
        monkeypatch.setenv(ap.LEAD_TEST_V2_ENV, raw)
        assert ap.lead_test_v2_enabled() is wanted
    # Anything else is an allow-list of analyst ids — the same spelling
    # ``assembly_enabled`` uses, so the rollout walks one desk at a time.
    monkeypatch.setenv(ap.LEAD_TEST_V2_ENV, "country_composition, world_assessor")
    assert ap.lead_test_v2_enabled(None, "country_composition") is True
    assert ap.lead_test_v2_enabled(None, "region_composition") is False


def test_the_descriptor_option_wins_over_the_env_in_both_directions(monkeypatch):
    """The house ``_coerce`` idiom: env supplies the base, the option wins.

    BOTH directions, because a switch that could only be turned on by a
    descriptor would make the fleet-wide rollback a lie."""
    monkeypatch.setenv(ap.LEAD_TEST_V2_ENV, "0")
    assert ap.lead_test_v2_enabled({ap.LEAD_TEST_V2_OPTION: True}) is True
    monkeypatch.setenv(ap.LEAD_TEST_V2_ENV, "1")
    assert ap.lead_test_v2_enabled({ap.LEAD_TEST_V2_OPTION: False}) is False


def test_an_unreadable_option_keeps_the_env_base_and_never_reads_as_on(
    monkeypatch,
):
    """A typo'd knob must not silently move the crown."""
    monkeypatch.setenv(ap.LEAD_TEST_V2_ENV, "0")
    assert ap.lead_test_v2_enabled({ap.LEAD_TEST_V2_OPTION: "yes please"}) is False
    monkeypatch.setenv(ap.LEAD_TEST_V2_ENV, "1")
    assert ap.lead_test_v2_enabled({ap.LEAD_TEST_V2_OPTION: object()}) is True


# ---------------------------------------------------------------------------
# RULE 1 — the count bar counts what can exist
# ---------------------------------------------------------------------------


def test_the_specced_bars_are_untouched_by_the_amendment():
    """v2 discounts the count bar; it does not restate it. The two substantive
    bars and the specced 8 stay exactly where D-1 §1.5.3 put them."""
    assert (ap.RATIO_BAR, ap.SHARE_BAR, ap.MIN_LEAD_CANDIDATES) == (1.50, 0.15, 8)
    assert ap.LEAD_CANDIDATE_FLOOR == 6


@pytest.mark.parametrize(
    "no_head, wanted",
    [(0, 8), (1, 7), (2, 6), (3, 6), (8, 6), (99, 6)],
)
def test_the_effective_bar_is_the_roster_minus_the_headless_never_below_the_floor(
    no_head, wanted,
):
    assert ap.min_lead_candidates(no_head) == wanted


@pytest.mark.parametrize("bad", [None, -1, -99, "seven", object(), float("nan")])
def test_a_malformed_no_head_can_only_make_the_bar_stricter(bad):
    """Fail-safe in the direction that matters: an unreadable ledger count must
    never BUY a lead. Every unusable value lands on the specced 8."""
    assert ap.min_lead_candidates(bad) == ap.MIN_LEAD_CANDIDATES


def test_the_headless_dimension_case_is_the_whole_defect():
    """CANADA, 2026-09-06 23:30Z, from the live row.

    Seven blocks, one headless dimension, ratio 2.50 and top-share 0.714 —
    both substantive bars cleared by a wide margin — and ``earned=false`` on
    the count alone. This is the read the bar was never meant to stop."""
    masses = [5.0, 2.0, 0.0, 0.0, 0.0, 0.0, 0.0]
    v1 = ap.earned_lead(masses, no_head=1, v2=False)
    assert v1["n_candidates"] == 7 and v1["min_candidates"] == 8
    assert v1["ratio_12"] >= ap.RATIO_BAR and v1["top_share"] >= ap.SHARE_BAR
    assert v1["earned"] is False  # refused on the count, and ONLY on the count

    v2 = ap.earned_lead(masses, no_head=1, v2=True)
    assert v2["min_candidates_effective"] == 7 and v2["no_head"] == 1
    assert v2["earned"] is True


def test_a_genuinely_thin_read_still_cannot_crown_anything():
    """The bar's ORIGINAL intent, preserved exactly. A desk that fielded three
    of eight dimensions is thin however concentrated it looks, and the floor is
    what keeps the discount from eating the bar."""
    thin = ap.earned_lead([9.0, 0.1, 0.05], no_head=5, v2=True)
    assert thin["ratio_12"] >= ap.RATIO_BAR and thin["top_share"] >= ap.SHARE_BAR
    assert thin["min_candidates_effective"] == ap.LEAD_CANDIDATE_FLOOR
    assert thin["n_candidates"] == 3 and thin["earned"] is False


def test_the_floor_binds_only_below_six_fielded_dimensions():
    """The floor's exact edge, both sides of it."""
    at_floor = ap.earned_lead([9.0] + [0.5] * 5, no_head=3, v2=True)
    assert at_floor["n_candidates"] == 6 and at_floor["earned"] is True
    below = ap.earned_lead([9.0] + [0.5] * 4, no_head=3, v2=True)
    assert below["n_candidates"] == 5 and below["earned"] is False


# ---------------------------------------------------------------------------
# RULE 2 — the crown goes to the block that earned it
# ---------------------------------------------------------------------------


def test_v1_crowns_ordinal_one_whatever_the_masses_say():
    """The shipped behaviour, pinned so the amendment has something to be a
    diff against. ISRAEL, 23:30Z: crowned 0.20 while ordinal 2 held 1.58."""
    masses = [0.20, 1.58, 0.10, 0.05, 0.05, 0.02, 0.0, 0.0]
    test = ap.earned_lead(masses, v2=False)
    assert test["earned"] is True
    assert ap._lead_block(masses, test, v2=False)["block_ordinals"] == [1]


def test_v2_crowns_the_argmax_block():
    masses = [0.20, 1.58, 0.10, 0.05, 0.05, 0.02, 0.0, 0.0]
    test = ap.earned_lead(masses, v2=True)
    lead = ap._lead_block(masses, test, v2=True)
    assert lead["kind"] == ap.LEAD_EARNED_SINGLE
    assert lead["block_ordinals"] == [2]


def test_a_crown_tie_goes_to_the_lowest_ordinal():
    """Ties resolve by the member's own total order, never by iteration luck."""
    masses = [0.0, 2.0, 2.0, 0.1]
    assert ap._argmax_ordinal(masses) == 2
    assert ap._argmax_ordinal([]) == 1


# ---------------------------------------------------------------------------
# RULE 3 — the co-lead band is anchored on the maximum
# ---------------------------------------------------------------------------


def test_v1_band_always_begins_at_ordinal_one_by_construction():
    """``m >= 0.60 * ms[0]`` is trivially true at index 0, so ``co_leads``
    could never move the crown — it was a LABEL on ordinal 1. Measured: 129 of
    129 live member-carries, under all three lead kinds."""
    masses = [0.15, 0.10, 0.02, 0.66]
    test = ap.earned_lead(masses, v2=False)
    band = ap._lead_block(masses, test, v2=False)["block_ordinals"]
    assert band and band[0] == 1


def test_v2_band_anchored_on_the_max_can_dissolve_a_spurious_co_lead():
    """GERMANY's shape, 23:30Z. The v1 band was anchored on 0.15 while block 4
    carried 0.66, so ordinals 1 and 2 were declared co-drivers of a day whose
    evidence sat somewhere else entirely.

    Anchored on the true maximum, only block 4 is within 0.60 of the top — one
    block is not a band, so the honest answer is ``none`` rather than a
    two-name shape line. v2 does not always produce MORE crowns; it produces
    the crown the arithmetic supports."""
    masses = [0.15, 0.10, 0.02, 0.66]
    v1 = ap._lead_block(masses, ap.earned_lead(masses, v2=False), v2=False)
    # 0.60 * 0.15 = 0.09 — a threshold so low that three of four blocks clear
    # it, and ordinal 1 leads a band anchored on itself.
    assert v1["kind"] == ap.LEAD_CO_LEADS and v1["block_ordinals"] == [1, 2, 4]
    v2 = ap._lead_block(masses, ap.earned_lead(masses, v2=True), v2=True)
    assert v2["kind"] == ap.LEAD_NONE and v2["block_ordinals"] == []


def test_v2_band_membership_is_computed_against_the_true_top():
    masses = [1.0, 0.1, 0.9, 0.7]
    test = ap.earned_lead(masses, v2=True)
    lead = ap._lead_block(masses, test, v2=True)
    assert lead["kind"] == ap.LEAD_CO_LEADS
    # 0.60 * 1.0 = 0.60 -> ordinals 1, 3, 4 qualify, heaviest first.
    assert lead["block_ordinals"] == [1, 3, 4]


def test_the_flat_day_is_still_none_under_both_regimes():
    """VOICE §4.5.2's required state. v2 makes the crown honest; it does not
    manufacture one out of a flat day."""
    flat = [0.0] * 8
    for v2 in (False, True):
        test = ap.earned_lead(flat, v2=v2)
        lead = ap._lead_block(flat, test, v2=v2)
        assert lead["kind"] == ap.LEAD_NONE and lead["block_ordinals"] == []


# ---------------------------------------------------------------------------
# THE FLAG-OFF GOLDEN — the property that makes this landable
# ---------------------------------------------------------------------------


def test_flag_off_stamps_no_v2_key_at_all():
    """A flag-off row must be INDISTINGUISHABLE from a pre-amendment one. Not
    'the values match' — the KEY SET matches, because a new key in a persisted
    payload is a schema change the arms would have to learn."""
    off = ap.earned_lead([3.0, 1.0] + [0.1] * 6, no_head=2, v2=False)
    assert set(off) == {
        "key", "top_share", "ratio_12", "bar_share", "bar_ratio",
        "min_candidates", "earned", "n_candidates",
    }
    on = ap.earned_lead([3.0, 1.0] + [0.1] * 6, no_head=2, v2=True)
    assert set(on) - set(off) == {"min_candidates_effective", "no_head"}
    # and every shared key agrees except the verdict the amendment exists to
    # move — which here it does not, since n=8 clears both bars anyway.
    assert {k: off[k] for k in off} == {k: on[k] for k in off}


def test_the_block_order_is_severity_first_under_both_regimes():
    """The fourth rule that is NOT in this amendment. ``order_key`` takes no
    regime argument at all, which is the strongest form of that promise: there
    is no flag-on branch to drift."""
    rows = [
        {"severity": "low", "id": "a", ap.CITED_SALIENCE_ROW_KEY: {"cited_mass": 9.0}},
        {"severity": "high", "id": "b", ap.CITED_SALIENCE_ROW_KEY: {"cited_mass": 0.1}},
    ]
    assert [r["id"] for r in sorted(rows, key=ap.order_key)] == ["b", "a"]
    with pytest.raises(TypeError):
        ap.order_key(rows[0], v2=True)  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# AMENDMENT 4c — THE ABSENCE EXCLUSION, applied to BOTH halves
#
# `block_is_absence` = ARM 2's collection-denominator truthmaker (reused
# verbatim) OR the span's declared `scope_tokens` OR `leading_negation`.
#
# The third clause was added on measurement, not on taste. Amendment 4's first
# cut used ARM 2's predicate alone, as specced — and ARM 2 fires on a
# COLLECTION denominator and explicitly NOT on a clock. The live absence spans
# are clock-bounded ("in this 72-hour window") or bounded by a named national
# media set ("in Argentina's media"), so the predicate caught 1 of the 8 region
# carries and 1 of the 16 crowns that opened with a negation. Replayed with the
# third clause: region carries opening with a negation 8 -> 0, flag-on crowns
# 15 -> 0, at a cost of 3.19 total cited mass over 96 carries and with the
# carry_reason distribution and the zero-mass count unchanged.
# ---------------------------------------------------------------------------


#: The corpus-derived openers, verbatim from the 129-assembly replay set with
#: their live counts. Every one of the 121 negation-opening spans starts with
#: `no`; these are the distinct trigrams it starts with.
CORPUS_NEGATION_OPENERS = (
    ("No coordinated narrative is evident in Argentina's media over the "
     "past three days.", 58),
    ("No credible indication of a leadership change appears in this window.", 5),
    ("No economic coercion measures were recorded against this target.", 5),
    ("No new military-posture activity is observable this cycle.", 4),
    ("No external economic pressure is evident.", 4),
    ("No unrest or security-force mobilisation was reported.", 4),
    ("No observable shift in deployment or readiness.", 4),
    ("No credible evidence of narrative coordination was found.", 3),
    ("No significant internal-stability events occurred.", 3),
    ("No new coordinated narrative surge is evident.", 2),
    ("No collected signal supports a posture change.", 2),
    ("No street protests, strikes or factional moves are reported.", 2),
    ("No material change in the sanctions picture.", 1),
    ("No mass protests, riots or security incidents appear.", 1),
    ("No posture-shift vector is present in this slice.", 1),
)


@pytest.mark.parametrize("text, _count", CORPUS_NEGATION_OPENERS)
def test_leading_negation_fires_on_every_corpus_derived_opener(text, _count):
    assert ap.leading_negation(text) is True


def test_leading_negation_reads_the_class_not_just_the_one_live_token():
    """121 of 121 live openers start with `no`; the rest of the class is
    carried anyway, because a list fitted to one week misses the first
    `Nothing in this window…` span that arrives after it."""
    for text in (
        "None of the monitored sources report a change.",
        "Nothing in this window changes the posture assessment.",
        "Neither the ministry nor the opposition has moved.",
        "Never in this slice does the indicator cross its band.",
        "There is no coordinated narrative in the collected reporting.",
        "There have been no strikes on the corridor.",
    ):
        assert ap.leading_negation(text) is True, text


def test_leading_negation_is_about_the_OPENER_and_not_about_a_negative_word():
    """THE LINE, and it is deliberate. 236 live spans carry a negative word in
    their first clause; only 121 lead with one. A span whose SUBJECT is the
    day's posture has said something about Niger even when what it says is
    'no material shift' — a reader takes it as a finding. A span with no
    subject but the absence itself has not."""
    for text in (
        "Niger's standing high-alert military posture shows no material shift "
        "in this window.",
        "Canada continues to bear a 50% U.S. tariff that remains unchanged.",
        "Brazil's energy-security pressure remains moderate and unchanged, "
        "with no new supply disruptions.",
        "The probability of a change in Argentina's top leadership is low.",
    ):
        assert ap.leading_negation(text) is False, text


def test_leading_negation_runs_through_the_one_shared_fold():
    """The MECH-6 class: a comparator that folds one side and not the other
    does not merely miss, it decides the wrong way at fleet scale. Full-width
    and non-breaking forms reach this predicate off the wire."""
    assert ap.leading_negation("Ｎｏ coordinated narrative is evident.") is True
    assert ap.leading_negation("NO COORDINATED NARRATIVE IS EVIDENT.") is True
    assert ap.leading_negation("  no coordinated narrative is evident.") is True


@pytest.mark.parametrize("bad", [None, "", 42, object(), b"no bytes here"])
def test_leading_negation_is_total_and_declines_rather_than_raises(bad):
    """A predicate that cannot see its text decides nothing, and deciding
    nothing here leaves the block ELIGIBLE — the fail-open direction, because
    a crash in the lead test would take the whole read down."""
    assert ap.leading_negation(bad) is False


def test_block_is_absence_is_the_union_of_three_signals():
    def block(text, tokens=()):
        return {"spans": [{"text": text, "scope_tokens": list(tokens)}]}

    # 1. ARM 2's collection denominator, reused verbatim.
    assert ap.block_is_absence(
        block("Coordination is absent in the available evidence.")) is True
    # 2. the declared token, for ARM 2's `scope_truncated` case — the qualifier
    #    trimmed out of the quoted span but still on the payload.
    assert ap.block_is_absence(
        block("Coordination is absent.", ("collection_denominator",))) is True
    # 3. the corpus-derived opener, which neither of the first two catch.
    assert ap.block_is_absence(
        block("No coordinated narrative is evident in this 72-hour window.")
    ) is True
    # and a real finding is none of the three.
    assert ap.block_is_absence(
        block("Deep-range drone attacks on Russian territory are intensifying.")
    ) is False
    assert ap.block_is_absence(None) is False
    assert ap.block_is_absence({}) is False


# --- the exclusion, in the CROWN (flag-on only) ----------------------------


def _masses_and_absence(specs):
    return [m for m, _ in specs], [ap.leading_negation(t) for _, t in specs]


#: A full eight-dimension roster: the block under test, the absence claim, and
#: six thin real blocks, so `n_candidates` clears the count bar and the earned
#: branch is genuinely reached.
_FILLER = [(0.02, f"A marginal move number {i}.") for i in range(6)]


def test_the_crown_skips_an_absence_block_under_v2_and_only_under_v2():
    """INDONESIA's shape: the heaviest block in the read says nothing
    happened. v1 never looked at the masses at all; v2 must not crown it."""
    specs = [
        (0.80, "LNG offtake contracts were re-priced."),
        (5.79, "No coordinated narrative is evident in collected reporting."),
    ] + _FILLER
    ms, absence = _masses_and_absence(specs)
    test = ap.earned_lead(ms, v2=True)
    assert test["earned"] is True
    without = ap._lead_block(ms, test, v2=True)
    assert without["block_ordinals"] == [2], "argmax alone promotes the absence"
    with_ = ap._lead_block(ms, test, v2=True, absence=absence)
    assert with_["block_ordinals"] == [1]
    # v1 ignores the flag argument entirely — ordinal 1 by the old rule.
    v1 = ap._lead_block(ms, ap.earned_lead(ms, v2=False), v2=False, absence=absence)
    assert v1["block_ordinals"] == [1]


def test_the_co_lead_band_excludes_absence_members_and_reanchors():
    """The band is a claim about which blocks share the day. A block that says
    nothing happened is not sharing it."""
    specs = [
        (1.00, "Strikes on the corridor continue."),
        (0.95, "No coordinated narrative is evident in this window."),
        (0.70, "Sanctions enforcement widened."),
    ] + _FILLER
    ms, absence = _masses_and_absence(specs)
    test = ap.earned_lead(ms, v2=True)
    assert test["earned"] is False  # flat enough that no single block is crowned
    band = ap._lead_block(ms, test, v2=True, absence=absence)["block_ordinals"]
    assert band == [1, 3]


def test_an_all_absence_read_still_gets_its_honest_crown():
    """The exclusion is DROPPED rather than escalated when every block is an
    absence claim. A read whose every dimension found nothing has a lead
    position; it is just an honest one — and a `none` manufactured by an empty
    eligible set would be a different statement than the arithmetic makes."""
    specs = [
        (0.40, "No unrest or security-force mobilisation was reported."),
        (2.00, "No coordinated narrative is evident in collected reporting."),
        (0.10, "No credible indication of a leadership change appears."),
        (0.05, "No external economic pressure is evident."),
        (0.04, "No observable shift in deployment or readiness."),
        (0.03, "No street protests, strikes or factional moves are reported."),
        (0.02, "No significant internal-stability events occurred."),
        (0.01, "No collected signal supports a posture change."),
    ]
    ms, absence = _masses_and_absence(specs)
    assert all(absence)
    test = ap.earned_lead(ms, v2=True)
    assert test["earned"] is True
    lead = ap._lead_block(ms, test, v2=True, absence=absence)
    assert lead["block_ordinals"] == [2]


def test_the_absence_flags_never_touch_the_test_arithmetic():
    """`top_share` and `ratio_12` are a measurement of the day's SHAPE over the
    whole pool. A denominator that quietly dropped rows would make the
    concentration test say something it did not measure — so the exclusion
    decides which block wears the crown and nothing else."""
    specs = [
        (0.80, "LNG offtake contracts were re-priced."),
        (5.79, "No coordinated narrative is evident in collected reporting."),
    ] + _FILLER
    ms, absence = _masses_and_absence(specs)
    test = ap.earned_lead(ms, v2=True)
    a = ap._lead_block(ms, test, v2=True, absence=absence)
    b = ap._lead_block(ms, test, v2=True)
    assert a["test"] == b["test"] == test
    # `top_share` is over the WHOLE pool, absence included.
    assert test["top_share"] == round(5.79 / sum(ms), 4)


def test_the_three_named_desks_go_back_to_a_real_block():
    """AR / MX / TR — the three the ARM-2-only cut got wrong, from the
    2026-09-06 23:30Z rows. Each carried a thin real block, was moved onto a
    well-cited 'nothing happened', and comes back."""
    for real_mass, real, absent_mass, absent in (
        (0.15, "Argentina's escalation risk is elevated as President Javier "
               "Milei has signed an accord.",
         0.64, "No coordinated narrative is evident in Argentina's media over "
               "the past three days."),
        (0.15, "Mexico's energy-security pressure stays elevated, with no new "
               "supply disruptions despite Hurricane Marie.",
         0.55, "No coordinated narrative is evident across Mexican sources in "
               "this 72-hour window."),
        (0.05, "US sanctions on Golden Global keep Turkey's escalation risk "
               "elevated but steady.",
         0.16, "No new coordinated narrative surge is evident; the balanced "
               "non-aligned framing holds."),
    ):
        specs = [(real_mass, real), (absent_mass, absent)] + _FILLER
        ms, absence = _masses_and_absence(specs)
        assert absence[:2] == [False, True]
        test = ap.earned_lead(ms, v2=True)
        # The absence block holds the top mass, so the argmax alone would
        # crown it; with the exclusion the read keeps its own thin finding.
        assert ap._lead_block(ms, test, v2=True)["block_ordinals"][:1] == [2]
        lead = ap._lead_block(ms, test, v2=True, absence=absence)
        assert lead["block_ordinals"][:1] == [1], real[:40]
