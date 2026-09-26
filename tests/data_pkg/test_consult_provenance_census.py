# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The consult PROVENANCE CENSUS on a synthetic answer (7g-2 §6).

"Mostly model knowledge" was a judgement a reader had to take on trust. This
is the count behind it, and these tests pin the two properties that make a
count worth printing:

  * it is measured from the ROWS' own ``origin_class``, never from which tool
    returned them — a guess dressed as provenance is worse than no number;
  * an unmeasurable class is ``None``, never ``0``. "Cites no live reporting"
    is a claim; absence is not.
"""

from __future__ import annotations

from legba.data.analysts.consult_provenance_census import (
    CENSUS_VERSION,
    HISTORY_ORIGIN_CLASSES,
    build_provenance_census,
    classify_by_origin_class,
    count_model_knowledge_sentences,
)

_A = "11111111-1111-4111-8111-111111111111"
_B = "22222222-2222-4222-8222-222222222222"


def _classification(**by_class):
    total = sum(by_class.values())
    return {"by_origin_class": dict(by_class), "resolved": total,
            "unresolved": 0, "asked": total}


# ---------------------------------------------------------------------------
# The class fold
# ---------------------------------------------------------------------------


def test_the_three_history_classes_fold_into_one_bucket():
    """The question a reader is asking is "is this a curated past or a live
    feed", not which loader wrote it."""
    assert HISTORY_ORIGIN_CLASSES == {
        "archive", "backfill_native", "backfill_reconstructed",
    }
    counts = classify_by_origin_class(
        _classification(archive=2, backfill_native=1, live=6)
    )
    assert counts["history"] == 3
    assert counts["live"] == 6
    assert counts["web_retrieval"] == 0
    assert counts["cited_total"] == 9


def test_an_unknown_class_is_reported_never_folded_into_a_neighbour():
    """A vocabulary change must show up as a number, not as a silent
    reassignment into whichever bucket happened to absorb it."""
    counts = classify_by_origin_class(_classification(live=1, something_new=4))
    assert counts["unclassified"] == 4
    assert counts["live"] == 1
    assert counts["history"] == 0


def test_an_unresolvable_ref_is_counted_as_such():
    counts = classify_by_origin_class(
        {"by_origin_class": {"live": 2}, "resolved": 2, "unresolved": 3, "asked": 5}
    )
    assert counts["unresolved"] == 3
    assert counts["cited_total"] == 5


def test_nothing_measured_is_None_in_every_bucket_never_zero():
    counts = classify_by_origin_class(None)
    assert counts == {
        "live": None, "web_retrieval": None, "history": None, "seed": None,
        "unresolved": None, "cited_total": None,
    }


# ---------------------------------------------------------------------------
# The uncited-sentence count
# ---------------------------------------------------------------------------


def test_an_unmarked_answer_is_all_model_knowledge():
    """The honest reading of today's consult contract: the prose carries no
    markers, so the platform cannot say which evidence any sentence rests on,
    because the sentence does not say."""
    answer = "Iran produced more crude in 2024. Israel imported less."
    uncited, examined = count_model_knowledge_sentences(answer, [])
    assert examined == 2
    assert uncited == 2


def test_a_sentence_naming_one_of_the_answers_own_cited_refs_counts_as_cited():
    answer = (
        f"Iran produced more crude in 2024 ({_A}). "
        "Israel imported less."
    )
    uncited, examined = count_model_knowledge_sentences(answer, [_A])
    assert examined == 2
    assert uncited == 1


def test_a_uuid_the_answer_did_not_cite_does_not_buy_a_sentence_out():
    """Only the answer's OWN cited refs count. Any uuid-shaped text would let
    a model exempt itself by writing one."""
    answer = f"A claim about something ({_B})."
    uncited, _ = count_model_knowledge_sentences(answer, [_A])
    assert uncited == 1


def test_an_ordinal_marker_is_taken_at_its_word():
    answer = "A claim [[ref:2]]. Another claim [4]. A third."
    uncited, examined = count_model_knowledge_sentences(answer, [])
    assert examined == 3
    assert uncited == 1


def test_a_heading_or_a_bare_rule_is_not_a_claim():
    """Reused from the Assessment channel's own uncited rule — one definition
    of "a sentence with nothing behind it", not a second copy that can drift."""
    uncited, _ = count_model_knowledge_sentences("**Summary**\n\n---\n\n", [])
    assert uncited == 0


def test_an_empty_answer_counts_nothing():
    assert count_model_knowledge_sentences("", [_A]) == (0, 0)
    assert count_model_knowledge_sentences("   ", []) == (0, 0)


# ---------------------------------------------------------------------------
# The whole census
# ---------------------------------------------------------------------------


def test_the_census_is_the_line_the_panel_prints():
    census = build_provenance_census(
        answer=f"Iran's output rose ({_A}). It will keep rising.",
        cited_refs=[_A, _B],
        classification=_classification(live=1, archive=1),
    )
    assert census["version"] == CENSUS_VERSION
    assert census["live"] == 1
    assert census["history"] == 1
    assert census["web_retrieval"] == 0
    assert census["model_knowledge"] == 1
    assert census["sentences_examined"] == 2
    assert "origin_class" in census["basis"]


def test_a_census_that_could_not_classify_says_so_and_prints_no_zero():
    census = build_provenance_census(
        answer="A claim.", cited_refs=[_A], classification=None,
    )
    assert census["live"] is None
    assert census["history"] is None
    assert census["model_knowledge"] == 1
    assert census["basis"] == "origin class not measured for this run"


def test_the_two_halves_are_different_units_and_stay_separate():
    """REFS and SENTENCES. A census that added them would be a number with no
    meaning, and the keys are what stop a reader surface trying."""
    census = build_provenance_census(
        answer="One. Two. Three.",
        cited_refs=[_A],
        classification=_classification(live=1),
    )
    assert census["cited_total"] == 1
    assert census["model_knowledge"] == 3
    assert census["sentences_examined"] == 3


def test_the_consult_route_lifts_the_census_or_it_dies_at_that_boundary():
    """``forced_final`` was carried on ``data`` all along and the projection
    dropped it, so the panel could not tell a complete answer from a truncated
    one. This is the same seam."""
    from legba.data.registry.consult_api import _project_consult_response

    payload = {
        "question": "q",
        "answer": "a",
        "cited_substrate_refs": [],
        "uncertainty": 0.2,
        "unanswered_aspects": [],
        "data": {"provenance_census": {"live": 3, "model_knowledge": 1}},
    }
    out = _project_consult_response(payload, finding_id=None, derived_from=[])
    assert out.provenance_census == {"live": 3, "model_knowledge": 1}


def test_a_turn_with_no_census_projects_None_not_an_empty_object():
    from legba.data.registry.consult_api import _project_consult_response

    out = _project_consult_response(
        {
            "question": "q", "answer": "a", "cited_substrate_refs": [],
            "uncertainty": 0.2, "unanswered_aspects": [], "data": {},
        },
        finding_id=None,
        derived_from=[],
    )
    assert out.provenance_census is None
