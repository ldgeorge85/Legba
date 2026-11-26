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
    # 2026-09-28 §B.3 widened the object; the rule it is pinned for is
    # unchanged and now covers three more buckets — EVERY one of them is None
    # when nothing was measured, and a ``fabricated: 0`` nobody measured would
    # read as "nothing was invented", which is a claim.
    assert counts == {
        "live": None, "web_retrieval": None, "history": None, "seed": None,
        "unresolved": None, "fabricated": None, "malformed": None,
        "by_kind": None, "cited_total": None,
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


# ---------------------------------------------------------------------------
# 2026-09-28 §B.3 — the citation seam: brackets, malformations, fabrications
# ---------------------------------------------------------------------------


def test_a_full_width_bracketed_ordinal_is_a_citation():
    """``【4】`` is the core plane's spelling of ``[4]``. Grading it model
    knowledge is the exact silence the 09-28 review found: the sentence IS
    cited and the census said it was not."""
    for marker in ("【4】", "［4］", "〔4〕", "〖4〗", "【ref:2】"):
        uncited, examined = count_model_knowledge_sentences(
            f"A claim {marker}. A second, bare, claim.", []
        )
        assert examined == 2, marker
        assert uncited == 1, marker


def test_the_normalized_answer_grades_the_same_as_the_raw_one():
    """The loop normalizes before it counts; the census must agree with itself
    either way, or the count depends on which caller got there first."""
    from legba.data.provenance.citation_markers import (
        normalize_inline_citation_markers,
    )

    raw = "A claim 【4】. A second claim 【ref:2】. A bare third."
    assert count_model_knowledge_sentences(
        raw, []
    ) == count_model_knowledge_sentences(normalize_inline_citation_markers(raw), [])


def test_fabricated_and_malformed_ride_the_census_separately():
    """A uuid that matches no table and a token that is not a uuid at all are
    different facts about the answer, and used to be the same silence."""
    census = build_provenance_census(
        answer="A claim.",
        cited_refs=[_A],
        classification={
            "by_origin_class": {"live": 1},
            "by_kind": {"signal": 1, "finding": 2},
            "resolved": 1,
            "unresolved": 2,
            "fabricated": 3,
            "malformed": ["sit-001"],
            "asked": 6,
        },
    )
    assert census["live"] == 1
    assert census["unresolved"] == 2
    assert census["fabricated"] == 3
    assert census["malformed"] == ["sit-001"]
    assert census["by_kind"] == {"signal": 1, "finding": 2}
    assert census["cited_total"] == 6


def test_the_parse_seams_malformed_tokens_merge_with_the_read_seams():
    """Two seams catch the same class of thing one layer apart. The operator
    gets ONE list, de-duplicated."""
    census = build_provenance_census(
        answer="A claim.",
        cited_refs=[],
        classification={
            "by_origin_class": {}, "resolved": 0, "unresolved": 0,
            "fabricated": 0, "malformed": ["sit-001"], "asked": 0,
        },
        malformed_refs=["sit-001", "sit-002"],
    )
    assert census["malformed"] == ["sit-001", "sit-002"]


def test_an_unmeasured_census_reports_None_for_the_new_buckets_too():
    """Never a fabricated zero. ``fabricated: 0`` would read as "nothing was
    invented", which is a claim, and absence is not."""
    counts = classify_by_origin_class(None)
    assert counts["fabricated"] is None
    assert counts["malformed"] is None
    assert counts["by_kind"] is None


# ---------------------------------------------------------------------------
# The provenance HOLD
# ---------------------------------------------------------------------------


def test_the_hold_fires_when_nothing_resolved_and_the_answer_still_asserts():
    from legba.data.analysts.consult_round_protocol import (
        PROVENANCE_HOLD_REASON,
        provenance_hold,
    )

    held = provenance_hold(
        {
            "live": 0, "history": 0, "web_retrieval": 0, "seed": 0,
            "model_knowledge": 4, "sentences_examined": 9,
            "unresolved": 0, "fabricated": 6, "malformed": ["sit-001"],
        }
    )
    assert held is not None
    banner, reason = held
    assert reason == PROVENANCE_HOLD_REASON
    assert banner.startswith("> **Held:")
    # The banner NAMES the counts rather than gesturing at them.
    assert "4 of 9 sentences" in banner
    assert "6 cited ids matched no record at all" in banner
    assert "sit-001" in banner


def test_one_resolved_live_ref_clears_the_hold():
    from legba.data.analysts.consult_round_protocol import provenance_hold

    assert provenance_hold(
        {"live": 1, "history": 0, "model_knowledge": 9, "sentences_examined": 9}
    ) is None


def test_an_answer_that_asserts_nothing_is_not_held():
    """Zero resolved refs with zero asserting sentences is a refusal or a
    framing note, not a held answer. Bannering it would be noise."""
    from legba.data.analysts.consult_round_protocol import provenance_hold

    assert provenance_hold(
        {"live": 0, "history": 0, "model_knowledge": 0, "sentences_examined": 3}
    ) is None


def test_an_unmeasured_census_holds_nothing():
    """A class the census could not read is ``None``. Reading that as 0 would
    hold an answer on the strength of a number nobody measured."""
    from legba.data.analysts.consult_round_protocol import provenance_hold

    assert provenance_hold(None) is None
    assert provenance_hold(
        {"live": None, "history": None, "model_knowledge": 4}
    ) is None


def test_a_web_retrieval_only_answer_is_not_held():
    """A web retrieval is evidence. The hold is about an answer resting on
    nothing, not about an answer resting on something else."""
    from legba.data.analysts.consult_round_protocol import provenance_hold

    assert provenance_hold(
        {
            "live": 0, "history": 0, "web_retrieval": 2, "seed": 0,
            "model_knowledge": 4, "sentences_examined": 9,
        }
    ) is None


# ---------------------------------------------------------------------------
# The two transport boundaries a field goes to die at
# ---------------------------------------------------------------------------


def test_the_consult_route_lifts_the_hold_reason_too():
    from legba.data.registry.consult_api import _project_consult_response

    out = _project_consult_response(
        {
            "question": "q", "answer": "a", "cited_substrate_refs": [],
            "uncertainty": 0.2, "unanswered_aspects": [],
            "data": {"hold_reason": "provenance_hold"},
        },
        finding_id=None,
        derived_from=[],
    )
    assert out.hold_reason == "provenance_hold"


def test_an_unheld_answer_projects_hold_reason_None():
    """``None`` here means "nothing was held", never "not measured" — the hold
    is a decision the server either took or did not."""
    from legba.data.registry.consult_api import _project_consult_response

    out = _project_consult_response(
        {
            "question": "q", "answer": "a", "cited_substrate_refs": [],
            "uncertainty": 0.2, "unanswered_aspects": [], "data": {},
        },
        finding_id=None,
        derived_from=[],
    )
    assert out.hold_reason is None


def test_the_RECOVERY_carries_its_own_census_and_hold():
    """``/synthesize`` writes a NEW answer over the same evidence. Its response
    carried no census at all, so the panel kept printing the ORIGINAL run's
    counts beside prose that no longer existed."""
    from legba.data.registry.consult_synthesis_api import project_recovery

    out = project_recovery(
        {
            "answer": "the recovered answer",
            "provenance_census": {"live": 2, "model_knowledge": 1},
            "hold_reason": "provenance_hold",
            "replay_fidelity": "exact",
        },
        evidence={"session_id": "sess-1", "turn_id": "turn-0"},
        request_id="req-1",
        chosen_model="opus",
        turn_id="turn-1",
    )
    assert out.provenance_census == {"live": 2, "model_knowledge": 1}
    assert out.hold_reason == "provenance_hold"


def test_a_recovery_that_measured_no_census_carries_None():
    from legba.data.registry.consult_synthesis_api import project_recovery

    out = project_recovery(
        {"answer": "the recovered answer", "replay_fidelity": "exact"},
        evidence={"session_id": "sess-1"},
        request_id="req-1",
        chosen_model="opus",
        turn_id="turn-1",
    )
    assert out.provenance_census is None
    assert out.hold_reason is None


# ---------------------------------------------------------------------------
# Events fold into the class buckets; the classify cap is reported
# ---------------------------------------------------------------------------


def test_the_events_fold_golden_signals_observations_and_events_in_one_census():
    """GOLDEN. A ``classify_cited_refs`` answer over 3 signals (2 live, 1
    web_retrieval), 2 observations (archive) and 4 events (3 live, 1
    web_retrieval) folds to live 5, web 2, history 2. The event rows count
    exactly like the signal rows beside them; ``by_kind`` still names them."""
    classification = {
        "by_origin_class": {
            "live": 5, "web_retrieval": 2, "archive": 2,
        },
        "by_kind": {"signal": 3, "observation": 2, "event": 4},
        "resolved": 9,
        "unresolved": 0,
        "matched": 9,
        "fabricated": 0,
        "malformed": [],
        "asked": 9,
        "unclassified_over_cap": 0,
    }
    counts = classify_by_origin_class(classification)
    assert (counts["live"], counts["web_retrieval"], counts["history"],
            counts["seed"]) == (5, 2, 2, 0)
    assert counts["by_kind"] == {"signal": 3, "observation": 2, "event": 4}
    assert counts["cited_total"] == 9
    assert counts["unclassified_over_cap"] == 0


def test_over_cap_refs_are_reported_and_counted_in_the_total():
    census = build_provenance_census(
        answer="Something was said.",
        cited_refs=[_A],
        classification={
            "by_origin_class": {"live": 1}, "by_kind": {"signal": 1},
            "resolved": 1, "unresolved": 0, "matched": 1, "fabricated": 0,
            "malformed": [], "asked": 1, "unclassified_over_cap": 5,
        },
    )
    assert census["unclassified_over_cap"] == 5
    assert census["cited_total"] == 6, "classified 1 + 5 never looked up"


def test_a_classification_without_the_cap_key_carries_no_such_key():
    """A classifier that predates the cap (a test double) is not given a
    fabricated zero for a number it never measured."""
    counts = classify_by_origin_class(_classification(live=2))
    assert "unclassified_over_cap" not in counts


def test_an_over_cap_remainder_holds_nothing():
    """The first CAP refs resolved to nothing, but the rest were never looked
    up - 'nothing resolved' has not been measured, so no hold."""
    from legba.data.analysts.consult_round_protocol import provenance_hold

    base = {
        "live": 0, "history": 0, "web_retrieval": 0, "seed": 0,
        "model_knowledge": 4, "sentences_examined": 9,
        "unresolved": 0, "fabricated": 400, "malformed": [],
    }
    assert provenance_hold(base) is not None
    assert provenance_hold({**base, "unclassified_over_cap": 7}) is None
    assert provenance_hold({**base, "unclassified_over_cap": 0}) is not None
