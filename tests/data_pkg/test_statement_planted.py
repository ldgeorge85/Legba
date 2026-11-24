# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""S3 — the seats' canary (``data/statement_seat_canary.py``) and the planted
wrong statements + catch-rate harness (``data/statement_planted.py``). Pure."""
from __future__ import annotations

from collections import Counter

import pytest

from legba.data import statement_planted as sp
from legba.data.statement_content import find_dates, find_numbers, name_present
from legba.data.statement_gate import evaluate_code_gates
from legba.data.statement_seat_canary import CANARY
from legba.data.statement_shapes import HandedSlice
from legba.data.statement_verifier import (
    DOES_NOT_SUPPORT,
    SUPPORTS,
    UNCLEAR,
    VerifierReport,
    code_precheck,
)

BASES = [sp.from_canary(c) for c in CANARY]


def test_the_canary_is_twenty_known_true_items_that_pass_every_code_gate():
    assert len(CANARY) == 20 and len({c.item_id for c in CANARY}) == 20
    assert set(Counter(c.statement.kind for c in CANARY)) >= {"event", "state", "level", "attributed"}
    for c in CANARY:
        rep = evaluate_code_gates(c.statement, c.support, texts=c.texts,
                                  subject_names=c.subject_names,
                                  handed=HandedSlice.of([c.support[0].ref_id]))
        assert rep.passed, (c.item_id, [f.reason for f in rep.failures])


@pytest.mark.parametrize("kind", sp.PLANTED_KINDS)
def test_every_planted_kind_is_deterministic_and_changes_the_item(kind):
    made = [sp.plant(b, kind, index=i) for i, b in enumerate(BASES)]
    again = [sp.plant(b, kind, index=i) for i, b in enumerate(BASES)]
    assert made == again
    produced = [m for m in made if m is not None]
    assert len(produced) >= 4
    for m in produced:
        base = next(b for b in BASES if b.item_id == m.base_id)
        assert m.truth == kind and m.planted
        assert (m.statement.text, m.support[0].quote) != (base.statement.text, base.support[0].quote)


def test_wrong_number_changes_a_figure_not_a_date():
    m = sp.plant(next(b for b in BASES if b.item_id == "c05"), "wrong_number")
    assert "1,501" in m.statement.text and "9 May" in m.statement.text


def test_wrong_date_moves_the_date_and_valid_from_together():
    base = next(b for b in BASES if b.item_id == "c08")
    m = sp.plant(base, "wrong_date")
    assert [d.day for d in find_dates(m.statement.text)] == [5]
    assert m.statement.valid_from.day == 5


def test_wrong_subject_uses_an_invented_name_absent_from_the_evidence():
    base = next(b for b in BASES if b.item_id == "c09")
    m = sp.plant(base, "wrong_subject")
    assert not name_present("Delmere", m.statement.text)
    assert any(f in m.statement.text for f in sp.FOIL_NAMES)


def test_other_version_quote_keeps_the_quote_inside_its_own_text():
    base = next(b for b in BASES if b.item_id == "c15")
    m = sp.plant(base, "other_version_quote")
    assert m.statement == base.statement
    assert m.support[0].quote in next(iter(m.texts.by_signal.values()))
    assert "not kept" in m.support[0].quote


def test_claim_as_fact_skips_attributed_bases_and_keeps_proper_names():
    assert sp.plant(next(b for b in BASES if b.item_id == "c04"), "claim_as_fact") is None
    m = sp.plant(next(b for b in BASES if b.item_id == "c02"), "claim_as_fact", index=1)
    assert m.support[0].quote.startswith("A partisan news channel claimed that Marrow Valley")


def test_unsupported_inference_adds_a_clause_and_nothing_else():
    base = next(b for b in BASES if b.item_id == "c17")
    m = sp.plant(base, "unsupported_inference")
    assert m.statement.text.startswith(base.statement.text.rstrip("."))
    assert find_numbers(m.statement.text) == find_numbers(base.statement.text)


def test_unknown_kind_is_refused():
    with pytest.raises(ValueError):
        sp.plant(BASES[0], "invented")


def test_the_planted_set_is_four_of_each_kind_plus_six_true_controls():
    items = sp.planted_set(BASES)
    counts = Counter(i.truth for i in items)
    assert counts == {**{k: 4 for k in sp.PLANTED_KINDS}, sp.TRUE: 6}
    assert items == sp.planted_set(BASES)
    # rotation: no kind's four come from one base
    for kind in sp.PLANTED_KINDS:
        assert len({i.base_id for i in items if i.truth == kind}) == 4


def test_code_alone_catches_none_of_the_inference_subject_or_attribution_plants():
    """Why the model's own rate is reported: these three kinds pass every code
    check, so only the model can catch them."""
    items = sp.planted_set(BASES)
    for i in items:
        if i.truth in {"wrong_subject", "unsupported_inference", "claim_as_fact"}:
            assert all(c.passed for c in code_precheck(i.statement, i.support, i.texts)), i.item_id


def _rep(verdict: str, by: str = "model", code: tuple[str, ...] = ()) -> VerifierReport:
    return VerifierReport(verdict, by, None, code)


def test_scoring_separates_the_seat_from_the_model():
    items = sp.planted_set(BASES)
    planted = [i for i in items if i.planted]
    true = [i for i in items if not i.planted]
    results = (
        [(planted[0], _rep(DOES_NOT_SUPPORT, "code", ("G2.numbers:x",)))]   # code caught; no model verdict
        + [(planted[1], _rep(SUPPORTS, "model", ("G2.dates:x",)))]          # model missed, code caught
        + [(p, _rep(DOES_NOT_SUPPORT)) for p in planted[2:22]]
        + [(p, _rep(SUPPORTS)) for p in planted[22:23]]                    # a true miss
        + [(planted[23], _rep(UNCLEAR, "unparsed"))]
        + [(t, _rep(SUPPORTS)) for t in true[:5]]
        + [(true[5], _rep("partly"))]
    )
    out = sp.score(results)
    assert out["seat_catch"]["k"] == 23 and out["seat_catch"]["n"] == 24
    assert out["model_catch"] == {"k": 20, "n": 22, "rate": round(20 / 22, 4),
                                  "wilson95": sp.wilson(20, 22)}
    assert out["seat_false_reject"]["k"] == 1 and out["seat_false_reject"]["n"] == 6
    assert out["unparsed_or_uncalled"] == 1
    assert out["establishment_paused"] is False
    assert set(out["per_kind"]) == set(sp.PLANTED_KINDS)


def test_the_floor_pauses_establishment():
    assert sp.CATCH_FLOOR == 0.90 and sp.FALSE_REJECT_CEILING == 0.15
    assert sp.establishment_paused(0.89) and sp.establishment_paused(None)
    assert not sp.establishment_paused(0.90)


def test_wilson_interval():
    assert sp.wilson(0, 0) is None
    lo, hi = sp.wilson(22, 24)
    assert 0.7 < lo < 0.92 < hi <= 1.0
