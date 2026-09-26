# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A-2 — the two diffs and the gauge.

Both arms in isolation (C1/C2/C3 and E1/E2/E3), ``unanchorable`` excluded from
both sides, ``None``-not-``0.0`` on every empty denominator, a degraded day
declining both, and — the case the whole train encodes — THE IL-SHAPED
REGRESSION (9 Iran rows in the slice, 0 frames, 0 engaged -> ``attention_rate
0.0``, ``licensed false``) with its negative control beside it.
"""
from __future__ import annotations

from uuid import uuid4

from legba.data.analysts.deterministic_handlers import _reference_diff as rd
from legba.data._polity_match import home_prose


IL_HOME = home_prose(["IL"], ["Israel"])


def _item(entities, urls=("https://a.example/1",), **kw):
    base = {
        "ordinal": 1,
        "headline": "Iran strike reported",
        "sentence": "Iran struck a site on 2026-09-04.",
        "entities": list(entities),
        "urls": list(urls),
        "materiality": "high",
    }
    base.update(kw)
    return base


def _slice_row(*, url=None, prose="", surfaces=(), signal_id=None):
    return {
        "signal_id": signal_id or uuid4(),
        "canonical_url": url,
        "prose": prose,
        "folds": rd.surface_folds(surfaces),
    }


# ---------------------------------------------------------------------------
# The item key + unanchorable
# ---------------------------------------------------------------------------


def test_folds_drop_junk_and_home_country():
    """An item naming ONLY the desk's own country is UNANCHORABLE: every desk's
    slice is about its own country, so "did we collect it?" keyed on the home
    polity answers itself."""
    assert rd.item_folds(_item(["Iran"]), home_blob=IL_HOME)  # a foreign polity
    assert rd.item_folds(_item(["Israel"]), home_blob=IL_HOME) == []  # home only
    assert rd.item_folds(_item(["Israel", "Iran"]), home_blob=IL_HOME)  # mixed -> Iran
    # a bare numeral is junk
    assert rd.item_folds(_item(["2024"]), home_blob=IL_HOME) == []


def test_unanchorable_is_excluded_from_both_numerator_and_denominator():
    """The SA/Yemen lesson made structural: a matcher that cannot name the thing
    does not get to score it. An unanchorable item raises neither n_anchorable
    nor n_collected — it changes no ratio."""
    record = rd.score_pair(
        items=[_item(["Israel"])],  # home-only -> unanchorable
        status="ok",
        home_blob=IL_HOME,
        slice_rows=[_slice_row(url="https://a.example/1")],
        head_prose="",
        head_cited=[],
        frame_names=[],
    )
    assert record["n_items"] == 1
    assert record["n_unanchorable"] == 1
    assert record["n_anchorable"] == 0
    assert record["collection_recall"] is None  # empty denominator
    assert record["attention_rate"] is None


# ---------------------------------------------------------------------------
# Diff (a) — the three collection arms, in isolation
# ---------------------------------------------------------------------------


def test_c1_url_is_zero_fp_and_fires_alone():
    folds = rd.item_folds(_item(["Iran"]), home_blob=IL_HOME)
    names = rd.item_names(_item(["Iran"]), home_blob=IL_HOME)
    collected, arms, matched = rd.collect_item(
        folds=folds, names=names,
        # A row that shares only the URL — no entity, no prose. C1 alone.
        slice_rows=[_slice_row(url="https://a.example/1", prose="unrelated text")],
        urls=["https://a.example/1"],
    )
    assert collected is True
    assert arms == {rd.ARM_C1_URL: True, rd.ARM_C2_ENTITY: False,
                    rd.ARM_C3_PROSE: False}
    assert len(matched) == 1


def test_c2_entity_needs_both_clauses():
    """C2 fires only when a row's folded NER surfaces intersect the item AND that
    row's prose is represented_by an item name — the coverage floor's own
    two-clause shape. A fold hit with no prose match does not fire C2."""
    folds = rd.item_folds(_item(["Iran"]), home_blob=IL_HOME)
    names = rd.item_names(_item(["Iran"]), home_blob=IL_HOME)
    # Both clauses: NER surface "Iran" AND prose naming Iran.
    collected, arms, _m = rd.collect_item(
        folds=folds, names=names,
        slice_rows=[_slice_row(surfaces=["Iran"], prose="tehran and iran talks")],
        urls=["https://z.example/none"],
    )
    assert arms[rd.ARM_C2_ENTITY] is True
    # Fold hit but prose does NOT name Iran -> C2 does not fire (and neither does C3).
    _c, arms2, _m2 = rd.collect_item(
        folds=folds, names=names,
        slice_rows=[_slice_row(surfaces=["Iran"], prose="a story about france")],
        urls=["https://z.example/none"],
    )
    assert arms2[rd.ARM_C2_ENTITY] is False
    assert arms2[rd.ARM_C3_PROSE] is False


def test_c3_prose_is_the_backstop_for_rows_with_no_ner():
    """18.3% of the IL slice carries no NER array; C3 catches the item name in
    the title/summary prose of exactly those rows."""
    folds = rd.item_folds(_item(["Iran"]), home_blob=IL_HOME)
    names = rd.item_names(_item(["Iran"]), home_blob=IL_HOME)
    collected, arms, _m = rd.collect_item(
        folds=folds, names=names,
        slice_rows=[_slice_row(surfaces=(), prose="iran expands enrichment")],
        urls=["https://z.example/none"],
    )
    assert collected is True
    assert arms[rd.ARM_C3_PROSE] is True
    assert arms[rd.ARM_C1_URL] is False


def test_collected_is_the_disjunction_and_an_uncollected_item_names_itself():
    record = rd.score_pair(
        items=[_item(["Iran"], urls=["https://never.example/x"])],
        status="ok",
        home_blob=IL_HOME,
        slice_rows=[_slice_row(prose="a story about france", surfaces=["France"])],
        head_prose="",
        head_cited=[],
        frame_names=[],
    )
    assert record["n_anchorable"] == 1
    assert record["n_collected"] == 0
    assert record["collection_recall"] == 0.0  # a REAL zero — anchorable, uncollected
    assert record["uncollected"] and record["uncollected"][0]["headline"]


# ---------------------------------------------------------------------------
# Diff (b) — the three engagement arms, in isolation
# ---------------------------------------------------------------------------


def test_e1_named_is_engagement():
    engaged, arms, licensed = rd.engage_item(
        names=["Iran"], head_prose="the desk notes iran's posture",
        head_cited=[], matched_signal_ids=[], frame_names=[],
    )
    assert engaged is True
    assert arms == {rd.ARM_E1_NAMED: True, rd.ARM_E2_CITED: False}
    assert licensed is False


def test_e2_cited_is_engagement_even_without_naming():
    """TR's live case: a desk that CITED an Israel signal without ever naming
    Israel in prose. E2 fires, E1 does not — and merging them would erase the
    whole difference between "the desk saw it" and "the desk said it"."""
    sid = uuid4()
    engaged, arms, _lic = rd.engage_item(
        names=["Israel"], head_prose="a head that never says the word",
        head_cited=[str(sid)], matched_signal_ids=[sid], frame_names=[],
    )
    assert engaged is True
    assert arms == {rd.ARM_E1_NAMED: False, rd.ARM_E2_CITED: True}


def test_e3_licensed_is_a_covariate_never_engagement():
    """An open frame naming the item is the register's LICENCE — the cause R-1
    will move, not the effect. It is recorded, and it is NOT in the engagement
    disjunction."""
    engaged, arms, licensed = rd.engage_item(
        names=["Iran"], head_prose="unrelated prose",
        head_cited=[], matched_signal_ids=[],
        frame_names=["Iran nuclear escalation"],
    )
    assert licensed is True
    assert engaged is False
    assert arms == {rd.ARM_E1_NAMED: False, rd.ARM_E2_CITED: False}


# ---------------------------------------------------------------------------
# THE IL-SHAPED REGRESSION — the case the whole train encodes
# ---------------------------------------------------------------------------


def test_the_il_shaped_regression():
    """``IL_BLINDNESS_DIAGNOSIS.md``, made a test.

    9 Iran-naming rows in the desk's own slice, ZERO frames licensing Iran, a
    head that never engages it: ``attention_rate`` must be exactly 0.0 (the
    story was collected and the desk did not write it), ``licensed`` false,
    ``collection_recall`` 1.0 (we HAD it). This is the shape that was invisible
    from every angle the system currently looks.
    """
    iran_slice = [
        _slice_row(surfaces=["Iran"], prose="iran and tehran escalation",
                   url=f"https://slice.example/{i}")
        for i in range(9)
    ]
    record = rd.score_pair(
        items=[_item(["Iran"], urls=["https://slice.example/0"])],
        status="ok",
        home_blob=IL_HOME,
        slice_rows=iran_slice,
        head_prose="the coalition splinters over a submarine deal",  # never Iran
        head_cited=[],
        frame_names=["Coalition politics", "Dolphin-class submarine"],  # no Iran
    )
    assert record["n_anchorable"] == 1
    assert record["n_collected"] == 1
    assert record["collection_recall"] == 1.0     # we HAD it
    assert record["n_engaged"] == 0
    assert record["attention_rate"] == 0.0         # collected, NOT engaged
    assert record["attention_gap"] == 1.0
    assert record["n_licensed"] == 0               # unlicensed
    # The 2x2 R-1 reads: one item, unlicensed (row 0) and unengaged (col 0).
    assert record["licensed_engaged_2x2"] == [[1, 0], [0, 0]]


def test_the_negative_control_a_demonym_only_frame_licenses():
    """The control beside the regression: an open frame that names Iran only by
    DEMONYM ("Iranian") still licenses — the matcher's alias/demonym expansion
    is what keeps the licence honest, and its absence is what the regression
    turns on."""
    record = rd.score_pair(
        items=[_item(["Iran"], urls=["https://slice.example/0"])],
        status="ok",
        home_blob=IL_HOME,
        slice_rows=[_slice_row(surfaces=["Iran"], prose="iran escalation",
                               url="https://slice.example/0")],
        head_prose="the desk engages iranian moves directly",  # E1 via demonym
        head_cited=[],
        frame_names=["Iranian proxy activity intensifies"],   # E3 via demonym
    )
    assert record["n_licensed"] == 1               # licensed true, via demonym
    assert record["n_engaged"] == 1                # engaged, via demonym
    assert record["attention_rate"] == 1.0
    assert record["licensed_engaged_2x2"] == [[0, 0], [0, 1]]  # licensed+engaged


# ---------------------------------------------------------------------------
# None-not-0.0 and the degraded decline
# ---------------------------------------------------------------------------


def test_none_not_zero_on_every_empty_denominator():
    # No items at all -> both ratios None, never 0.0.
    r = rd.score_pair(items=[], status="ok", home_blob=IL_HOME,
                      slice_rows=[], head_prose="", head_cited=[], frame_names=[])
    assert r["collection_recall"] is None
    assert r["attention_rate"] is None
    # Anchorable but nothing collected -> recall is a REAL 0.0, but attention
    # (denominator = collected = 0) is None.
    r2 = rd.score_pair(
        items=[_item(["Iran"], urls=["https://never.example/x"])],
        status="ok", home_blob=IL_HOME,
        slice_rows=[_slice_row(prose="unrelated")],
        head_prose="", head_cited=[], frame_names=[],
    )
    assert r2["collection_recall"] == 0.0
    assert r2["attention_rate"] is None


def test_a_degraded_day_declines_both_metrics():
    """A search outage must never read as a quiet world. A non-scorable status
    short-circuits to None for BOTH ratios with declined=True — the reference
    itself is unreliable that day, so every number from it would be too."""
    for status in ("degraded", "unverified_liveness"):
        r = rd.score_pair(
            items=[_item(["Iran"])], status=status, home_blob=IL_HOME,
            slice_rows=[_slice_row(surfaces=["Iran"], prose="iran")],
            head_prose="iran", head_cited=[], frame_names=[],
        )
        assert r["declined"] is True
        assert r["collection_recall"] is None
        assert r["attention_rate"] is None
        assert r["n_collected"] == 0  # nothing scored at all


def test_nothing_material_is_scorable():
    """A liveness-VERIFIED empty ("nothing material") is an honest, scorable
    day: zero items, so both ratios are None by the empty-denominator rule, but
    it is NOT declined — it is a real measurement of a quiet window."""
    r = rd.score_pair(items=[], status="nothing_material", home_blob=IL_HOME,
                      slice_rows=[], head_prose="", head_cited=[], frame_names=[])
    assert r["declined"] is False
    assert r["collection_recall"] is None


# ---------------------------------------------------------------------------
# Pooling
# ---------------------------------------------------------------------------


def test_pooled_rate_sums_both_sides_never_averages_rates():
    """A pair with one collected item must not weigh as much as a pair with
    five — the operator-axis pooling rule."""
    records = [
        {"n_engaged": 1, "n_collected": 1},   # rate 1.0 over 1
        {"n_engaged": 0, "n_collected": 9},   # rate 0.0 over 9
    ]
    # Mean-of-rates would be 0.5; pooled is 1/10.
    assert rd.pooled_rate(records, numerator="n_engaged",
                          denominator="n_collected") == 0.1
    assert rd.pooled_rate([], numerator="n_engaged",
                          denominator="n_collected") is None
