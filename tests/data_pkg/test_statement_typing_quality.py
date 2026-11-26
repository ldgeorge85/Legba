# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Making real desk sentences typeable without weakening the gates:
subject names (``statement_subject_names``), splitting multi-claim sentences
(``statement_split`` / ``statement_piece_typing``), the date rule
(``statement_dates``) and the G2 date-part fix. Pure: no DB, no model.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timezone

import pytest

from legba.data import statement_gate as g
from legba.data.statement_dates import EvidenceDating, judge_dates, resolve_relative, valid_from_stated
from legba.data.statement_piece_typing import (
    NO_EVIDENCE_REF,
    UNCITED,
    assemble_pieces,
    build_piece_prompt,
    tally_pieces,
)
from legba.data.statement_sentences import CitedRef, CitedSentence
from legba.data.statement_shapes import EvidenceTexts, HandedSlice, StatementDraft, SupportRow
from legba.data.statement_split import split_sentence, verify_model_split
from legba.data.statement_subject_names import (
    SubjectName,
    clean_surface_form,
    label_name,
    name_in_text,
    related_subjects,
    resolve_subject_names,
    uncomma,
)
from legba.data.statement_typing import EvidenceRef, SubjectCandidate, TypingInput, strip_as_of

UTC = timezone.utc
FRI = datetime(2026, 11, 13, 18, 0, tzinfo=UTC)      # Friday 13 November 2026
READ = datetime(2026, 11, 14, 3, 0, tzinfo=UTC)      # Saturday 14 November 2026

# A fixture gazetteer + roster in the packaged shapes (the defaults read the
# real exports; these tests pin the RULES on a small declared world).
GZ = {
    "target_slug_names": {"xa": ["arlandia", "arland"], "xb": ["borovia"]},
    "desk_countries": {"xa": ["arlandia"]},
    "slug_to_abbrev": {"xa": ["ARL", "XA"]},
    "iso2_home_aliases": {"XA": ["Republic of Arlandia"]},
    "alias_map": {"arl": "Arlandia", "the republic": "Arlandia"},
    "demonym_map": {"arlandian": "Arlandia", "borovian": "Borovia"},
    "irregular_demonyms": {"arlandia": ["arlish"]},
    "officeholder_country_groups": [["arlandia", "arland republic"]],
    "subject_homonyms": {"xa": {"reads_as": ["the XA postal code"], "exclude": ["XA", "Arland Bay"],
                                "subject": ["Arlton", "President Kell", "Arlandian"]}},
}
SUBJECTS = {
    "country_watch_xa": {"name": "Watch — Arlandia, Republic of", "geo": ["XA"], "domain": "geo"},
    "lane_strait": {"name": "Lane — Narrow Strait / Outer Gulf", "geo": ["XA", "XB"], "domain": "thematic"},
    "situation_war": {"name": "Situation — Arlandia–Borovia War", "geo": [], "domain": "thematic"},
}
METONYMS = {"arlton"}


def _names(key: str, kind: str = "target"):
    return resolve_subject_names(kind, key, subjects=SUBJECTS, gazetteer=GZ, metonyms=METONYMS)


# ---------------------------------------------------------------------------
# 1 — subject names
# ---------------------------------------------------------------------------


def test_label_and_iso_comma_names_are_turned_into_what_a_source_writes():
    assert label_name("Watch — Arlandia, Republic of") == "Arlandia, Republic of"
    assert label_name("Situation — US–Iran War") == "US–Iran War"   # unspaced dash kept
    assert uncomma("Iran, Islamic Republic of") == "Islamic Republic of Iran"
    assert uncomma("Congo, The Democratic Republic of the") == "Democratic Republic of the Congo"
    assert uncomma("Bonaire, Sint Eustatius and Saba") is None


def test_country_desk_names_each_carry_their_provenance():
    got = _names("country_watch_xa")
    by = {n.name: (n.kind, n.source, n.usable) for n in got.names}
    assert by["Republic of Arlandia"][0] == "label"
    assert by["arland"][1] == "gazetteer:target_slug_names"
    assert by["ARL"] == ("abbreviation", "gazetteer:slug_to_abbrev", True)
    assert by["arlandian"][0] == "demonym" and by["arlish"][0] == "demonym"
    assert by["arland republic"][1] == "gazetteer:officeholder_country_groups"
    # a capital only because the profile declares it for this subject AND the
    # seat-of-government list knows it
    assert by["Arlton"][0] == "capital"
    # a leader is declared as naming the subject in a headline, but is not a name for it
    assert by["President Kell"] == ("declared_other", "gazetteer:subject_homonyms", False)
    # a declared homonym form is never a name, and is carried as an exclusion
    assert "XA" not in by and "XA" in got.excluded
    assert len(got.usable) >= 2


def test_capital_not_declared_by_the_profile_is_not_a_name():
    got = resolve_subject_names("target", "country_watch_xa", subjects=SUBJECTS, gazetteer=GZ, metonyms=())
    assert "Arlton" not in {n.name for n in got.usable}


def test_abbreviations_match_case_sensitively_and_exclusions_block():
    us = SubjectName("US", "abbreviation", "x")
    assert name_in_text(us, "The US said on Friday")
    assert not name_in_text(us, "officials told us on Friday")
    arl = SubjectName("Arlandia", "label", "x")
    assert not name_in_text(SubjectName("Arland", "short", "x"), "storms over Arland Bay",
                            excluded=("Arland Bay",))
    assert name_in_text(SubjectName("Arland", "short", "x"), "Arland and Arland Bay", excluded=("Arland Bay",))
    assert name_in_text(arl, "Arlandian troops")       # suffix form of a long name


def test_gate_subject_check_reads_resolved_names_and_records_which_matched():
    names = _names("country_watch_xa")
    row = SupportRow("evidence", "signal", "s1", quote="officials in Arlton said the talks failed",
                     title=None, published_at=FRI)
    res = g.check_subject_named([row], names)
    assert res.passed and res.detail["kind"] == "capital" and res.detail["source"].startswith("gazetteer:")
    only_leader = SupportRow("evidence", "signal", "s1", quote="President Kell said the talks failed",
                             title=None, published_at=FRI)
    assert g.check_subject_named([only_leader], names).reason == "subject_not_named"
    homonym = SupportRow("evidence", "signal", "s1", quote="a storm hit Arland Bay overnight",
                         title=None, published_at=FRI)
    assert g.check_subject_named([homonym], names).reason == "subject_not_named"
    # plain strings still work (the caller shape)
    assert g.check_subject_named([row], ["Arlton"]).passed


def test_thematic_targets_offer_the_countries_they_span_never_a_guess():
    # declared geo: a code with its own desk resolves to the desk, else a place
    assert related_subjects("lane_strait", subjects=SUBJECTS, gazetteer=GZ) == (
        ("target", "country_watch_xa"), ("place", "XB"))
    # a label naming declared countries — only real ISO codes are read from a
    # label (the fixture's XA/XB are not), so nothing is guessed here
    assert related_subjects("situation_war", subjects=SUBJECTS, gazetteer=GZ) == ()
    assert related_subjects("country_watch_xa", subjects=SUBJECTS, gazetteer=GZ) == ()
    parts = {n.name for n in _names("lane_strait").usable}
    assert {"Narrow Strait", "Outer Gulf"} <= parts


def test_real_profile_names_for_live_desks():
    """The packaged export: every country desk has at least two usable names."""
    from legba.data.subject import current

    for key, decl in current().subjects.items():
        if key.startswith("country_"):
            got = resolve_subject_names("target", key)
            assert len(got.usable) >= 2, (key, got.usable)
    kp = resolve_subject_names("target", "country_watch_kp")
    assert "KP" in kp.excluded and "Pyongyang" in {n.name for n in kp.usable if n.kind == "capital"}
    assert ("target", "country_watch_ir") in related_subjects("situation_iran_war")


def test_entity_surface_forms_are_cleaned_not_guessed():
    assert clean_surface_form("Hezbollah&apos;s") == "Hezbollah"
    assert clean_surface_form("​the Hezbollah") == "Hezbollah"
    assert clean_surface_form("/>Iranian") is None
    got = resolve_subject_names("entity", "Hezbollah", subjects={}, gazetteer=GZ, metonyms=(),
                                entity_forms={"Hezbollah": ["Hizbollah", "Hezbollah&apos;s"]})
    assert [n.name for n in got.usable] == ["Hezbollah", "Hizbollah"]


# ---------------------------------------------------------------------------
# 3 — the date rule
# ---------------------------------------------------------------------------


def _ev(quote: str, pub=FRI) -> list[EvidenceDating]:
    return [EvidenceDating(quote, pub)]


@pytest.mark.parametrize("quote,anchor,expected", [
    ("the strike on Thursday killed 5", FRI, date(2026, 11, 12)),
    ("talks resumed on Friday", FRI, date(2026, 11, 13)),           # same day counts
    ("the vote last Friday", FRI, date(2026, 11, 6)),               # strictly before
    ("the attack yesterday", FRI, date(2026, 11, 12)),
    ("protests today", FRI, date(2026, 11, 13)),
    ("talks next Tuesday", FRI, None),                              # forward: no day
])
def test_relative_words_resolve_against_publication_deterministically(quote, anchor, expected):
    got = resolve_relative(quote, anchor)
    assert got and got[0].day == expected


def test_rule_a_publication_day_and_relative_quote_words():
    v = judge_dates("Five died in Kabul on 13 November.", _ev("five people died in the blast"))
    assert v.passed and v.accepted == {"publication_day": ["13 November"]}
    v = judge_dates("Five died in Kabul on 12 November.", _ev("the blast on Thursday killed five people"))
    assert v.passed and "relative_in_quote" in v.accepted
    v = judge_dates("Five died in Kabul on 11 November.", _ev("the blast on Thursday killed five people"))
    assert not v.passed and v.missing == ("11 November",)


def test_rule_b_the_reads_own_as_of_frame_is_known_at_not_valid_time():
    v = judge_dates("As of 14 November, the border remains closed.", _ev("the border remains closed"),
                    read_at=READ)
    assert v.passed and v.known_at == date(2026, 11, 14) and v.accepted == {"as_of_frame": ["14 November"]}
    # an as-of frame naming ANOTHER day is a claim about that day
    v = judge_dates("As of 3 November, the border remained closed.", _ev("the border remains closed"),
                    read_at=READ)
    assert not v.passed and v.known_at is None
    # without the read's time the frame cannot be identified, so it is a date like any other
    assert not judge_dates("As of 14 November, the border remains closed.",
                           _ev("the border remains closed")).passed


def test_rule_c_everything_else_must_be_in_a_quote_and_nothing_is_inferred():
    assert judge_dates("The ceasefire began on 9 November.", _ev("the ceasefire began on 9 November")).passed
    assert not judge_dates("The ceasefire began on 9 November.", _ev("the ceasefire began this week")).passed
    # the statement's own relative words are the desk's clock: resolved against the read
    v = judge_dates("Iran said on Thursday that talks failed.", _ev("Iran said talks failed"), read_at=READ)
    assert not v.passed      # Thursday 12 Nov is neither the publication day nor in the quote
    v = judge_dates("Iran said on Friday that talks failed.", _ev("Iran said talks failed"), read_at=READ)
    assert v.passed          # Friday 13 Nov = the row's publication day (rule a)
    v = judge_dates("Iran said on Friday that talks failed.", _ev("Iran said talks failed"))
    assert not v.passed and v.unresolved_relative


def test_years_are_date_parts_checked_as_dates():
    assert judge_dates("Output fell in 2025.", _ev("output fell in September 2025")).passed
    assert not judge_dates("Output fell in 2024.", _ev("output fell in September 2025")).passed
    assert not judge_dates("Output fell in 2025.", _ev("output fell to 2025 tonnes")).passed


def test_g2_weakness_fixed_a_date_part_never_supports_a_quantity():
    texts = EvidenceTexts(by_signal={"s1": "Officials said on 4 April that 2 drones struck the depot."})
    row = SupportRow("evidence", "signal", "s1", quote="on 4 April that 2 drones struck the depot",
                     published_at=FRI)
    planted = StatementDraft("Officials said 4 drones struck the depot.", "event", "place", "X")
    res = g.check_numbers(planted, [row], texts)
    assert not res.passed and res.detail["missing"] == ["4"]
    true = StatementDraft("Officials said 2 drones struck the depot on 4 April.", "event", "place", "X")
    assert g.check_numbers(true, [row], texts).passed
    # and vice versa: a bare quantity in the quote never supports a stated date
    qty = SupportRow("evidence", "signal", "s1", quote="officials said 4 drones and 13 shells hit",
                     published_at=datetime(2026, 4, 20, tzinfo=UTC))
    assert not g.check_dates(StatementDraft("Drones hit on 13 April.", "event", "place", "X"), [qty]).passed


def test_valid_from_must_be_stated_somewhere():
    ev = _ev("the ceasefire began on 9 November")
    assert valid_from_stated(datetime(2026, 11, 9, tzinfo=UTC), "day", "The ceasefire began.", ev) == "quote"
    assert valid_from_stated(datetime(2026, 11, 13, tzinfo=UTC), "day", "x", ev) == "publication_day"
    assert valid_from_stated(datetime(2026, 11, 2, tzinfo=UTC), "day", "x", ev) is None
    st = StatementDraft("The ceasefire began.", "event", "place", "X",
                        valid_from=datetime(2026, 11, 2, tzinfo=UTC), valid_from_precision="day")
    row = SupportRow("evidence", "signal", "s1", quote="the ceasefire began on 9 November", published_at=FRI)
    assert g.check_valid_from_stated(st, [row]).reason == "valid_from_not_stated"


def test_seat_strips_an_as_of_valid_from_and_keeps_known_at():
    vf, prec, known, stripped = strip_as_of(
        "As of 14 November, the border remains closed.", datetime(2026, 11, 14, tzinfo=UTC), "day", READ)
    assert (vf, prec, stripped) == (None, "unknown", True) and known.date() == date(2026, 11, 14)
    vf, _, known, stripped = strip_as_of(
        "As of 14 November, the border closed on 14 November.", datetime(2026, 11, 14, tzinfo=UTC), "day", READ)
    assert vf is not None and not stripped and known is not None


# ---------------------------------------------------------------------------
# 2 — splitting multi-claim sentences
# ---------------------------------------------------------------------------

ROWS = {
    1: "Customs data showed exports rose 12% in October, the ministry of Arlandia said.",
    2: "Arlandia's imports fell 4% in October, according to customs figures.",
    3: "The Arlandia trade deficit hit $3bn in October, officials said.",
}


def _item(text: str, ordinals: tuple[int, ...]) -> TypingInput:
    refs = tuple(CitedRef(n, "signal", f"sig-{n}", f"src-{n}", None, True) for n in ordinals)
    plain = g.strip_markers(text)
    sentence = CitedSentence(0, text, plain, refs)
    evidence = tuple(EvidenceRef(n, "signal", f"sig-{n}", ROWS[n], published_at=FRI, source_id=f"src-{n}",
                                 access_class="public") for n in ordinals)
    subj = SubjectCandidate("target", "country_watch_xa", _names("country_watch_xa"))
    return TypingInput(sentence, evidence, (subj,), read_at=READ)


def _entry(piece: int, row: int, quote: str, value: float) -> dict:
    return {"piece": piece, "kind": "level", "subject": "S1", "valid_from": "2026-10",
            "valid_from_precision": "month", "slots": {"value": value},
            "support": [{"row": row, "quote": quote}], "relation": {"action": "new", "candidate": None}}


HANDED = HandedSlice.of(["sig-1", "sig-2", "sig-3"], run_id="r")


def test_three_figure_sentence_citing_three_rows_passes_as_three():
    text = "In October exports rose 12% [1], imports fell 4% [2] and the deficit hit $3bn [3]."
    item = _item(text, (1, 2, 3))
    pieces = split_sentence(text)
    assert [p.ordinals for p in pieces] == [(1,), (2,), (3,)]
    reply = {"pieces": [_entry(1, 1, "exports rose 12% in October, the ministry of Arlandia said", 12),
                        _entry(2, 2, "Arlandia's imports fell 4% in October", 4),
                        _entry(3, 3, "The Arlandia trade deficit hit $3bn in October", 3e9)]}
    out = assemble_pieces(item, reply, handed=HANDED)
    assert out.split_by == "code" and [p.passed for p in out.pieces] == [True, True, True], \
        [p.why() for p in out.pieces]


def test_three_figure_sentence_citing_one_row_yields_one_supported_piece_and_two_plain_failures():
    text = "In October exports rose 12%, imports fell 4% and the deficit hit $3bn [1]."
    item = _item(text, (1,))
    q = "Customs data showed exports rose 12% in October, the ministry of Arlandia said"
    reply = {"pieces": [_entry(1, 1, q, 12), _entry(2, 1, q, 4), _entry(3, 1, q, 3e9)]}
    out = assemble_pieces(item, reply, handed=HANDED)
    assert [p.passed for p in out.pieces] == [True, False, False]
    for p in out.pieces[1:]:
        assert "G2.numbers:number_not_in_evidence" in p.why()
    assert all(p.piece.inherited for p in out.pieces[:2])
    t = tally_pieces([out])
    assert t["sentences_with_a_supported_piece"] == 1 and t["pieces_passed"] == 1


def test_a_figure_after_the_last_citation_is_marked_uncited():
    text = "Exports rose 12% [1], while imports fell 4%."
    out = assemble_pieces(_item(text, (1,)), {"pieces": [
        _entry(1, 1, "exports rose 12% in October, the ministry of Arlandia said", 12)]}, handed=HANDED)
    assert out.pieces[-1].reason == UNCITED and out.pieces[-1].detail["figures"] == ["4"]


def test_a_piece_citing_only_a_prior_read_has_no_evidence_row():
    # A piece citing only the desk's prior read RESTATES it (linked, no
    # new piece); one citing only a non-record ref still has no evidence row
    text = "Exports rose 12% [1], and imports fell 4% [9]."
    refs = (CitedRef(1, "signal", "sig-1", None, None, True), CitedRef(9, "prior_read", "r", None, None, False))
    item = _item("Exports rose 12% [1].", (1,))
    item = TypingInput(CitedSentence(0, text, g.strip_markers(text), refs), item.evidence, item.subjects,
                       read_at=READ)
    out = assemble_pieces(item, {"pieces": [_entry(1, 1, "exports rose 12% in October, the ministry of Arlandia said", 12)]},
                          handed=HANDED)
    assert out.pieces[1].reason == "restatement" and out.pieces[1].detail["restates"][0]["ref_id"] == "r"
    refs = (refs[0], CitedRef(9, "open_questions", None, None, None, False))
    item = TypingInput(CitedSentence(0, text, g.strip_markers(text), refs), item.evidence, item.subjects,
                       read_at=READ)
    out = assemble_pieces(item, {"pieces": [_entry(1, 1, "exports rose 12% in October, the ministry of Arlandia said", 12)]},
                          handed=HANDED)
    assert out.pieces[1].reason == NO_EVIDENCE_REF


def test_a_quote_from_a_row_the_piece_does_not_cite_is_refused():
    text = "In October exports rose 12% [1], imports fell 4% [2] and the deficit hit $3bn [3]."
    reply = {"pieces": [_entry(1, 2, "Arlandia's imports fell 4% in October", 12)]}
    out = assemble_pieces(_item(text, (1, 2, 3)), reply, handed=HANDED)
    assert "row_not_on_sentence" in out.pieces[0].result.refusals


def test_structure_that_is_not_a_list_is_not_split():
    for t in ("Arlandia and Borovia agreed to hold talks in the capital [1].",
              "Raids killed 10 civilians between 13 and 14 November in the north [1].",
              "The 11 signals concern mask supply, two football matches and a data breach [1].",
              "Talks continued [1], but no deal emerged.",
              "Exports rose 12% in October [1]."):
        assert len(split_sentence(t)) == 1, t


def test_model_split_is_accepted_only_verbatim_and_concatenating():
    text = "Exports rose 12% and imports fell 4% in October [1]."
    ok, reason, counts = verify_model_split(text, [
        {"start": 0, "end": 16, "text": "Exports rose 12%"},
        {"start": 99, "end": 120, "text": "and imports fell 4% in October"}])
    assert reason is None and len(ok) == 2 and counts["offsets_corrected"] == 1
    assert "".join(p.text for p in ok) == text and all(p.ordinals == (1,) for p in ok)
    assert verify_model_split(text, [{"text": "Exports rose 12%"},
                                     {"text": "and imports dropped 4% in October"}])[1] == "split_text_not_in_sentence"
    assert verify_model_split(text, [{"text": "Exports rose 12%"}, {"text": "in October"}])[1] == "split_skips_text"
    assert verify_model_split(text, [{"text": "Exports rose 12%"}, {"text": "and imports"}])[1] == "split_leaves_text"


def test_model_split_through_the_seat_reply_types_each_piece():
    # "as" is no connective code cuts at: the structure does not show the split
    text = "Exports rose 12% as imports fell 4% in October [1]."
    assert len(split_sentence(text)) == 1
    item = _item(text, (1,))
    q = "exports rose 12% in October, the ministry of Arlandia said"
    reply = {"pieces": [{**_entry(1, 1, q, 12), "start": 0, "end": 16, "text": "Exports rose 12%"},
                        {**_entry(2, 1, q, 4), "text": "as imports fell 4% in October"}]}
    out = assemble_pieces(item, reply, handed=HANDED)
    assert out.split_by == "model" and [p.passed for p in out.pieces] == [True, False]
    bad = {"pieces": [{**_entry(1, 1, q, 12), "text": "Exports grew 12%"}, {**_entry(2, 1, q, 4), "text": "x"}]}
    assert assemble_pieces(item, bad, handed=HANDED).refusals == ("split_rejected:split_text_not_in_sentence",)


def test_piece_prompt_shows_markers_pieces_and_rows():
    text = "In October exports rose 12% [1], imports fell 4% [2] and the deficit hit $3bn [3]."
    prompt = build_piece_prompt(_item(text, (1, 2, 3)), split_sentence(text))
    assert "[1]" in prompt and "P3 · rows 3" in prompt and "ROW 2" in prompt
    json.dumps(prompt)


def test_verifier_precheck_applies_the_same_date_rule_as_the_gate():
    from legba.data.statement_verifier import code_precheck

    texts = EvidenceTexts(by_signal={"s1": "Officials said five people died in the blast."})
    row = SupportRow("evidence", "signal", "s1", quote="five people died in the blast", published_at=FRI)
    st = StatementDraft("Five people died in the blast on 13 November.", "event", "place", "X")
    assert all(c.passed for c in code_precheck(st, [row], texts))       # rule (a)
    late = StatementDraft("Five people died in the blast on 10 November.", "event", "place", "X")
    assert not all(c.passed for c in code_precheck(late, [row], texts))
