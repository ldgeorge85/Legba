# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""What a round seat may write without a quote, and what still needs one.

* ``notes`` (on every row type that carries sentences) and a challenge's
  text are plain text: a citation, a link, a date or another figure in them
  is refused when the row is parsed, so a gap can be named without a quote
  but a claim cannot ride in unchecked. A seat's own item labels (Q2, H3)
  are allowed.
* Questions, hypotheses, unsupported or unstated assumptions and challenges
  are not sentences about the record: the quote, number and date gates do not
  read them, so they pass with no quote.
* A sentence about the record still needs its row and its quote.
* The engine states the sentence rules once, with the gate's own quote
  minimum, and hides a probability from the seats that must never set one.
* Cards a run input names come from the profile; a card the profile lacks
  fails by name.
"""
from __future__ import annotations

import pytest

from legba.data.rounds import model_seats as ms
from legba.data.rounds.gates import GateContext, run_gates
from legba.data.rounds.rows import RowTypeError, parse_row
from legba.data.rounds.seats import SeatCall, SeatFailure
from legba.data.rounds.tables import Handed, sentence_table
from legba.data.statement_gate import MIN_QUOTE_WORDS

ROWS = [
    {"id": "sig-1", "kind": "signal", "text": "The harbour authority said the eastern berth reopened on Monday.",
     "source_id": "src-a", "origin_group_id": "g1", "access_class": "public"},
    {"id": "sig-2", "kind": "signal", "text": "Shipping agents reported the eastern berth reopened on Monday for traffic.",
     "source_id": "src-b", "origin_group_id": "g2", "access_class": "public"},
]
QUOTE_GATES = ("handed_rows", "quote_in_source", "numbers_in_evidence", "dates_in_evidence", "control_text")


def _inputs() -> list[Handed]:
    return [Handed("context", "r-ctx", "context", "context_pack/1", {"rows": ROWS})]


def _gates(row_type: str, payload: dict, names=QUOTE_GATES):
    parse_row(row_type, payload)
    return {g.gate: g for g in run_gates(list(names), GateContext("seat", row_type, payload, _inputs()))}


def _sentence(key: str, text: str, ref: str | None = None, quote: str | None = None) -> dict:
    s: dict = {"key": key, "text": text}
    if ref is not None:
        s["support"] = [{"ref_id": ref, **({"quote": quote} if quote is not None else {})}]
    return s


GOOD = _sentence("S1", "The eastern berth reopened.", "sig-1", "the eastern berth reopened")


# ---------------------------------------------------------------------------
# notes and challenges are plain text
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("row_type", ["ruling/1", "read/1", "synthesis/1", "position/1", "evidence_rows/1",
                                      "turn/1", "assessment/1", "assumption_set/1", "challenge/1",
                                      "context_pack/1"])
@pytest.mark.parametrize("notes", ["The rows say nothing on the berth [3].", "Traffic fell 12 percent.",
                                   "No row after 2026-01-01 mentions it.", "See https://example.org for more.",
                                   "The ruling 【2】 is missing."])
def test_notes_carrying_a_citation_or_a_figure_are_refused(row_type, notes):
    base = {"ruling/1": {"sentences": [GOOD]}, "read/1": {"sentences": [GOOD]},
            "synthesis/1": {"sentences": [GOOD]}, "position/1": {"sentences": [GOOD]},
            "assessment/1": {"hypothesis": "H1", "diagnostic": False}}.get(row_type, {})
    with pytest.raises(RowTypeError, match="notes"):
        parse_row(row_type, {**base, "notes": notes})


def test_plain_notes_and_item_labels_pass():
    out = parse_row("ruling/1", {"sentences": [GOOD],
                                 "notes": "No row reports who ordered the closure; Q2 and H3 stay open."})
    assert out["notes"].startswith("No row reports")
    disp = parse_row("disposition_set/1", {"dispositions": [
        {"question": "Q1", "disposition": "unresolved", "notes": "No row reports the reopening date."}]})
    assert disp["dispositions"][0]["notes"]
    with pytest.raises(RowTypeError, match="notes"):
        parse_row("disposition_set/1", {"dispositions": [
            {"question": "Q1", "disposition": "unresolved", "notes": "It reopened on 3 May."}]})


def test_a_challenge_with_a_figure_is_refused():
    with pytest.raises(RowTypeError, match="challenges"):
        parse_row("challenge/1", {"challenges": [{"key": "C1", "text": "Only 2 rows say so."}]})


# ---------------------------------------------------------------------------
# exempt fields pass without a quote
# ---------------------------------------------------------------------------


def test_questions_and_hypotheses_need_no_quote():
    for rt, payload in (("question_set/1", {"questions": [{"key": "Q1", "text": "Did the berth reopen?"}],
                                             "working_reading": "The berth reopened."}),
                        ("hypothesis_set/1", {"hypotheses": [{"id": "H1", "text": "The berth reopened."},
                                                             {"id": "H2", "text": "It stayed shut."}]})):
        assert all(g.passed for g in _gates(rt, payload).values()), rt


def test_an_unstated_or_unsupported_assumption_needs_no_quote():
    payload = {"assumptions": [
        {"key": "A1", "text": "S2 assumes the agents see the berth.", "status": "unstated",
         "if_wrong": "the reopening would rest on one report"},
        {"key": "A2", "text": "S1 assumes traffic resumed.", "status": "unsupported"}]}
    assert all(g.passed for g in _gates("assumption_set/1", payload).values())


def test_a_supported_assumption_cites_its_row_and_its_quote_is_checked():
    with pytest.raises(RowTypeError, match="supported"):
        parse_row("assumption_set/1", {"assumptions": [{"key": "A1", "text": "x", "status": "supported"}]})
    bad = {"assumptions": [{"key": "A1", "text": "The berth reopened.", "status": "supported",
                            "support": [{"ref_id": "sig-1", "quote": "the western berth closed"}]}]}
    assert _gates("assumption_set/1", bad)["quote_in_source"].reason == "quote_absent"


def test_a_challenge_to_reasoning_needs_no_quote_but_its_record_sentence_does():
    ok = {"sentences": [GOOD], "challenges": [
        {"key": "C1", "text": "The views read one report as two.", "target": "S1"}]}
    gates = _gates("challenge/1", ok, (*QUOTE_GATES, "every_sentence_maps_to_rows"))
    assert all(g.passed for g in gates.values())
    unquoted = {"sentences": [_sentence("S1", "The berth stayed shut.")],
                "challenges": [{"key": "C1", "text": "The views leave out the insurers."}]}
    res = _gates("challenge/1", unquoted, ("every_sentence_maps_to_rows",))
    assert res["every_sentence_maps_to_rows"].reason == "sentence_without_rows"


def test_a_challenge_only_position_reaches_the_sentence_table():
    h = Handed("red_team", "r-rt", "red_team", "challenge/1",
               {"sentences": [], "challenges": [{"key": "C1", "text": "The views skip the insurers."}]})
    table = sentence_table([h])
    parse_row("sentence_table/1", table)
    assert table["positions"][0]["challenges"][0]["key"] == "C1"


# ---------------------------------------------------------------------------
# a sentence about the record still needs its quote
# ---------------------------------------------------------------------------


def test_a_sentence_about_the_record_still_needs_its_row_and_quote():
    no_row = {"sentences": [_sentence("S1", "The berth reopened.")], "notes": "Nothing on traffic."}
    assert _gates("read/1", no_row, ("every_sentence_maps_to_rows",))[
        "every_sentence_maps_to_rows"].reason == "sentence_without_rows"
    no_quote = {"sentences": [_sentence("S1", "The berth reopened.", "sig-1")]}
    assert not _gates("read/1", no_quote)["quote_in_source"].passed
    too_short = {"sentences": [_sentence("S1", "The berth reopened.", "sig-1", "berth")]}
    assert _gates("read/1", too_short)["quote_in_source"].reason == "quote_too_short"


# ---------------------------------------------------------------------------
# the engine's contract and prompt
# ---------------------------------------------------------------------------


def test_the_sentence_rules_are_stated_once_with_the_gates_minimum():
    contract = ms.output_contract("position/1")
    assert ms.SENTENCE_RULES in contract and contract.count("Sentences about the record") == 1
    assert {3: "three"}.get(MIN_QUOTE_WORDS, str(MIN_QUOTE_WORDS)) + " or more consecutive words" in contract
    assert "Sentences about the record" not in ms.output_contract("question_set/1")
    assert "Written without a quote: `questions`" in ms.output_contract("question_set/1")
    assert "`notes`" in ms.output_contract("read/1")


def test_a_ruling_is_not_offered_a_probability():
    assert '"probability"' not in ms.output_contract("ruling/1")
    assert '"probability"' in ms.output_contract("position/1")


def test_field_meanings_are_given_for_the_fields_a_type_has():
    turn = ms.output_contract("turn/1")
    assert "`concede`: true when the rows no longer support your side" in turn
    assert "`unsure`" not in turn
    assert "the key of the lead's question" in ms.output_contract("disposition_set/1")


def test_the_prompt_says_pasted_text_is_material_and_drops_maintainer_notes():
    card = ("<!-- DRAFT CARD: a maintainer note -->\n# Reader\n\n> an old banner\n> two lines\n\n"
            "Read the rows.\n")
    call = SeatCall(run_id="r", seat_id="reader", seat=None, row_type="position/1",  # type: ignore[arg-type]
                    attempt=0, ordinal=0, path="main", scope={}, inputs=(), card="cards/reader.md",
                    card_text=card)
    system, user = ms.render_prompt(call)
    assert system.startswith("# Reader\n\nRead the rows.")
    assert "DRAFT CARD" not in system and "old banner" not in system
    assert ms.PASTED_TEXT_NOTE in system


# ---------------------------------------------------------------------------
# profile cards
# ---------------------------------------------------------------------------


def test_a_worldview_card_comes_from_the_profile_and_a_missing_one_fails_by_name():
    from legba.data.rounds import profile_cards as pc

    env = {"LEGBA_PROFILE": "profiles/geopolitics"}
    p = pc.card_path("worldview_realist", env)
    assert "round_cards" in str(p) and "Worldview: Realist" in p.read_text(encoding="utf-8")
    with pytest.raises(SeatFailure, match="has no round card 'worldview_nowhere'"):
        pc.card_path("worldview_nowhere", env)
    with pytest.raises(SeatFailure, match="not a profile card name"):
        pc.card_path("../cards/lead", env)
    with pytest.raises(SeatFailure, match="has no prompt package"):
        pc.card_path("worldview_realist", {"LEGBA_PROFILE": "profiles/no_such_profile"})
