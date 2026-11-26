# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The typing gaps, closed in code where structure allows. Pure.

* the NUMBER rule (``statement_numbers``): number words, qualifiers,
  ranges, units, ordinals, compounds — as a table of cases;
* date RANGES and two-day lists, and French dates (``statement_content``,
  ``statement_dates``);
* the COVERAGE behaviour class and its gate (``statement_coverage``);
* judgement / absence TAILS split in code (``statement_split.split_tails``)
  and typed without the seat (``statement_piece_typing``);
* the planted-error design: no other supporting quote, twenty per kind,
  the paraphrase controls (``statement_planted``, ``statement_seat_canary``);
* ``thinking`` reaches the model call only when asked (``statement_seats``).
"""
from __future__ import annotations

import asyncio
from collections import Counter
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from legba.data import statement_gate as g
from legba.data import statement_planted as sp
from legba.data.statement_content import find_date_ranges, find_dates
from legba.data.statement_coverage import coverage_stated, evaluate_coverage_gates
from legba.data.statement_dates import EvidenceDating, judge_dates
from legba.data.statement_numbers import evidence_figures, read_figures, unsupported_figures
from legba.data.statement_piece_typing import (
    ABSENCE,
    ASSESSMENT,
    COVERAGE,
    ESTABLISHED_ELIGIBLE,
    OUTCOMES,
    assemble_pieces,
    build_piece_prompt,
    code_pieces,
    needs_seat,
    tally_pieces,
    type_pieces,
)
from legba.data.statement_seat_canary import CANARY, CONTROLS
from legba.data.statement_seats import call_seat
from legba.data.statement_sentences import CitedRef, CitedSentence
from legba.data.statement_shapes import (
    GATE_VERSION,
    EvidenceTexts,
    HandedSlice,
    StatementDraft,
    StatementKind,
    SupportRow,
)
from legba.data.statement_split import split_sentence, split_tails
from legba.data.statement_typing import EvidenceRef, SubjectCandidate, TypingInput
from legba.data.statement_verifier import NOT_APPLICABLE, code_precheck, verify_support

UTC = timezone.utc
PUB = datetime(2026, 11, 14, 9, tzinfo=UTC)


def _numbers_ok(statement: str, *quotes: str) -> bool:
    _, missing = unsupported_figures(statement, evidence_figures(quotes))
    return not missing


# ---------------------------------------------------------------------------
# 1. NUMBERS — the rule as a table
# ---------------------------------------------------------------------------

NUMBER_CASES = [
    # number words on the statement side (kept "one" out; kept)
    ("Unrest killed ten", "dix personnes sont mortes", True),             # French quote word
    ("Unrest killed ten", "10 people died", True),
    ("Unrest killed ten", "11 people died", False),
    ("One report says 5 died", "5 people died", True),                     # "one" alone: a pronoun
    ("one million people", "1 million people", True),                      # "one" before a scale
    ("one million people", "2 million people", False),
    ("twenty-five arrests", "25 arrests", True),
    ("two million barrels", "2 million barrels", True),
    ("a dozen boats", "12 boats", True),
    ("73,000 miners", "73 000 personnes", True),                           # space-grouped thousands
    # qualifiers: the quote's values lie inside the statement's
    ("about 300 stores", "some 300 stores", True),
    ("about 300 stores", "300 stores", True),
    ("about 300 stores", "nearly 300 stores", True),
    ("about 300 stores", "297 stores", False),                             # a different number, never
    ("300 stores", "about 300 stores", False),                             # exact needs exact
    ("nearly 300 stores", "nearly 300 stores", True),
    ("nearly 300 stores", "300 stores", False),
    ("over 300 stores", "more than 300 stores", True),
    ("over 300 stores", "300 stores", False),
    ("over 300 stores", "312 stores", False),                              # same number only
    ("at least 300 stores", "300 stores", True),
    ("at least 300 stores", "more than 300 stores", True),
    ("up to 300 stores", "fewer than 300 stores", True),
    ("under 300 stores", "up to 300 stores", False),
    ("300 or more stores", "at least 300 stores", True),
    ("over 3 days", "3 days", True),                                       # "over" a time: during
    # ranges
    ("between 300 and 400 killed", "between 300 and 400 killed", True),
    ("300-400 killed", "300 to 400 people were killed", True),
    ("300–400 killed", "300 killed and 400 wounded", True),                # both endpoints stated
    ("300–400 killed", "300 killed", False),                               # one endpoint only
    ("300 killed", "between 300 and 400 killed", False),                   # an endpoint is not stated
    ("350 killed", "300-400 killed", False),                               # inside the range: never
    ("won 19-14", "won 19-14", True),                                      # a score: two figures
    ("rose from 300 to 400", "300 then 400", True),                        # a change: two figures
    ("3-4% growth", "growth of 3-4%", True),
    # units
    ("rose 2 percentage points", "rose 2%", False),
    ("rose 2%", "rose 2 percentage points", False),
    ("rose 2%", "up 2 per cent", True),
    ("rose 2", "up 2 per cent", True),                                     # no unit stated: any
    ("a 48-hour strike", "a strike lasting 48 hours", True),
    ("a three-day visit", "a 3-day visit", True),
    ("a three-day visit", "3 visitors", False),                            # the unit is stated
    ("a 12-year-old boy", "un garçon de 12 ans, a 12-year-old", True),
    # ordinals
    ("the third attack", "the 3rd attack", True),
    ("the third attack", "3 attacks", False),
    ("3 attacks", "the third attack", False),
    ("the first time", "never before", True),                              # "first": not read
    ("a third of the vote", "33% of the vote", True),                      # a fraction: not read
]


@pytest.mark.parametrize("statement,quote,ok", NUMBER_CASES)
def test_the_number_rule_table(statement: str, quote: str, ok: bool) -> None:
    assert _numbers_ok(statement, quote) is ok, [f.label() for f in read_figures(statement)]


def test_g2_reports_how_each_figure_was_read_and_the_gate_version_moved() -> None:
    texts = EvidenceTexts(by_signal={"s1": "Officials said some 300 stores closed."})
    row = SupportRow("evidence", "signal", "s1", quote="some 300 stores closed")
    res = g.check_numbers(StatementDraft("About 300 stores closed.", "event", "target", "t"), [row], texts)
    assert res.passed and res.detail["read_as"] == ["approx:300"]
    bad = g.check_numbers(StatementDraft("300 stores closed.", "event", "target", "t"), [row], texts)
    assert not bad.passed and bad.detail["missing"] == ["300"]
    assert GATE_VERSION == "statement_gate.v4"


# ---------------------------------------------------------------------------
# 1b. DATES — ranges, lists, French months
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text,singles,ranges", [
    ("on 11–12 October", [], ["????-10-11..????-10-12"]),
    ("between 13 and 14 November", [], ["????-11-13..????-11-14"]),
    ("from November 18 to 19", [], ["????-11-18..????-11-19"]),
    ("from 30 October to 2 November", [], ["????-10-30..????-11-02"]),
    ("du 10 au 30 novembre 2026", [], ["2026-11-10..2026-11-30"]),
    ("on 13 and 14 November", ["????-11-13", "????-11-14"], []),            # a list states both days
    ("October 11 and 12", ["????-10-11", "????-10-12"], []),
    ("October 11 and 12 people died", ["????-10-11"], []),                  # a date and a count
    ("le vendredi 13 novembre 2026", ["2026-11-13"], []),
])
def test_date_ranges_and_lists(text: str, singles: list[str], ranges: list[str]) -> None:
    assert [d.label() for d in find_dates(text)] == singles
    assert [r.label() for r in find_date_ranges(text)] == ranges


@pytest.mark.parametrize("statement,quote,ok", [
    ("The summit runs on 18–19 November.", "the meeting from November 18 to 19", True),
    ("The summit runs on 18–19 November.", "talks on November 18 and 19", True),   # both endpoints
    ("The summit opens on 18 November.", "the meeting from November 18 to 19", False),  # an endpoint
    ("The summit is on 25 November.", "the meeting from 20 to 30 November", False),     # inside
    ("Stores were hit on 11–12 October.", "On October 11 and 12, hackers struck", True),
    ("Stores were hit on 11–12 October.", "On October 11, hackers struck", False),
    ("Warrants were issued on 13 November 2026.", "le vendredi 13 novembre 2026, des mandats", True),
])
def test_the_date_rule_reads_ranges(statement: str, quote: str, ok: bool) -> None:
    verdict = judge_dates(statement, [EvidenceDating(quote, datetime(2026, 11, 30, tzinfo=UTC))],
                          allow_publication_date=False)
    assert verdict.passed is ok, verdict


# ---------------------------------------------------------------------------
# 2. COVERAGE
# ---------------------------------------------------------------------------

WS, WE = datetime(2026, 11, 13, 20, tzinfo=UTC), datetime(2026, 11, 14, 9, tzinfo=UTC)


def _cov_rows(n: int, *, sources: int | None = None, spread_h: float = 3.0) -> tuple[list[SupportRow], EvidenceTexts]:
    sources = n if sources is None else sources
    rows, by_signal = [], {}
    for i in range(n):
        sid = f"sig-{i}"
        by_signal[sid] = f"Distinct story number {i} about the vote with its own wording and detail {i * 7}."
        rows.append(SupportRow("evidence", "signal", sid, quote="", source_id=f"src-{i % sources}",
                               access_class="public",
                               published_at=WS + timedelta(hours=spread_h * i / max(1, n - 1))))
    return rows, EvidenceTexts(by_signal=by_signal)


def _handed(n: int, *, window: bool = True) -> HandedSlice:
    return HandedSlice.of([f"sig-{i}" for i in range(n)], "run-1", collection="t-1",
                          window_start=WS if window else None, window_end=WE if window else None)


def _cov(text: str, rows: list[SupportRow], texts: EvidenceTexts, handed: HandedSlice):
    return evaluate_coverage_gates(StatementDraft(text, "coverage", "target", "t-1"),
                                   handed=handed, rows=rows, texts=texts)


def _cov_reason(rep) -> list[str]:
    return [f"{c.check}:{c.reason}" for c in rep.failures]


def test_coverage_is_a_kind_and_its_own_outcome() -> None:
    assert StatementKind.COVERAGE.value == "coverage" and COVERAGE in OUTCOMES
    assert coverage_stated("Three outlets reported the vote") and not coverage_stated("The army seized a town")


@pytest.mark.parametrize("text,n_rows,sources,ok,reason", [
    ("Three outlets reported the vote.", 3, 3, True, None),
    ("Three outlets reported the vote.", 3, 2, False, "coverage_counts:coverage_count_mismatch"),
    ("Only two distinct sources carried the framing.", 3, 3, False, "coverage_counts:coverage_count_mismatch"),
    ("Only two distinct sources carried the framing.", 3, 2, True, None),
    ("Two reports covered the vote.", 3, 3, True, None),                   # at least two rows
    ("The framing spans 3 hours across three outlets.", 3, 3, True, None),
    ("The framing spans 7 hours across three outlets.", 3, 3, False, "coverage_counts:coverage_count_mismatch"),
    ("Each clears three sources within 3.3 hours.", 3, 3, True, None),
    ("This 13-hour slice carries the vote coverage.", 3, 3, True, None),   # the handed window: 13 h
    ("This window's 3 signals cover the vote.", 3, 3, True, None),         # the handed rows
    ("This window's 38 signals cover the vote.", 3, 3, False, "coverage_counts:coverage_count_mismatch"),
    ("Reporting framed the vote as a referendum on 14 November.", 2, 2, True, None),
    ("Reporting framed the vote as a referendum on 2 November.", 2, 2, False, "coverage_dates:coverage_date_unchecked"),
    ("Reporting put the turnout at 61% of voters.", 2, 2, False, "coverage_counts:coverage_number_unchecked"),
    ("Reporting framed the vote as a referendum, not as a protest.", 2, 2, True, None),   # a contrast
    ("The army seized the town.", 2, 2, False, "coverage_stated:coverage_not_stated"),
    ("No outlet reported a coup.", 2, 2, False, "coverage_not_absence:coverage_states_absence"),
])
def test_the_coverage_gate(text: str, n_rows: int, sources: int, ok: bool, reason: str | None) -> None:
    rows, texts = _cov_rows(n_rows, sources=sources)
    rep = _cov(text, rows, texts, _handed(n_rows))
    assert rep.passed is ok, _cov_reason(rep)
    if reason:
        assert reason in _cov_reason(rep)


def test_coverage_is_scoped_to_the_handed_slice_and_its_rows() -> None:
    rows, texts = _cov_rows(2)
    no_window = _cov("Two outlets reported the vote.", rows, texts, _handed(2, window=False))
    assert "coverage_scope:coverage_scope_unknown" in _cov_reason(no_window)
    not_handed = _cov("Two outlets reported the vote.", rows, texts, HandedSlice.of(["sig-0"], "r",
                      collection="t-1", window_start=WS, window_end=WE))
    assert "cited_ids:not_in_run_slice" in _cov_reason(not_handed)
    none_cited = _cov("Outlets reported the vote.", [], texts, _handed(2))
    assert "coverage_rows:coverage_rows_missing" in _cov_reason(none_cited)
    # the ceiling is the counted rows'
    private = [replace(r, access_class="restricted") for r in rows]
    assert _cov("Two outlets reported the vote.", private, texts, _handed(2)).access_ceiling == "restricted"


def test_a_coverage_piece_counts_the_rows_the_sentence_cites() -> None:
    rows = [EvidenceRef(i, "signal", f"sig-{i}", f"Story {i} on the vote, wording number {i * 11}.",
                        published_at=WS + timedelta(hours=i), source_id=f"src-{i}", access_class="public")
            for i in (1, 2, 3)]
    text = "Three outlets covered the vote [1][2], with a later wire on Saturday [3]."
    refs = tuple(CitedRef(r.ordinal, "signal", r.ref_id, None, None, True) for r in rows)
    sent = CitedSentence(0, text, text, refs, ())
    item = TypingInput(sent, tuple(rows), (SubjectCandidate("target", "t-1", ["Borovia"]),),
                       read_at=datetime(2026, 11, 14, tzinfo=UTC))
    handed = HandedSlice.of([r.ref_id for r in rows], "run-1", collection="t-1", window_start=WS, window_end=WE)
    pieces = split_sentence(text)
    entry = {"kind": "coverage", "subject": "S1", "support": [], "relation": {"action": "new"}}
    out = assemble_pieces(item, {"pieces": [{**entry, "piece": i + 1} for i in range(len(pieces))]},
                          handed=handed, pieces=pieces[:1])
    p = out.pieces[0]
    assert p.outcome == COVERAGE, p.why()
    assert p.detail["counted"] == "sentence_rows" and p.result.detail["counted_rows"] == 3


def test_coverage_never_reaches_the_verifier_and_never_establishes() -> None:
    rep = asyncio.run(verify_support(None, StatementDraft("Three outlets reported it.", "coverage", "target", "t"),
                                     (), texts=EvidenceTexts(), system_prompt="x"))
    assert rep.verdict == NOT_APPLICABLE and not rep.establishes


# ---------------------------------------------------------------------------
# 3. TAILS split in code
# ---------------------------------------------------------------------------


def _tails(text: str) -> list[tuple[str | None, int | None, str]]:
    return [(p.code_kind, p.rests_on, p.plain) for p in split_tails(text, split_sentence(text))]


def test_a_contrastive_appositive_becomes_an_assessment_resting_on_the_fact() -> None:
    out = _tails("On 14 November, authorities in Assam demolished 132 homes, an internal-security "
                 "item rather than an interstate one [5].")
    assert out == [(None, None, "On 14 November, authorities in Assam demolished 132 homes"),
                   ("assessment", 0, "an internal-security item rather than an interstate one.")]


def test_a_bare_contrast_takes_its_appositive_with_it() -> None:
    out = _tails("The two ministers discussed air defence support on 14 November — a discussion of "
                 "support, not a signed transfer [4].")
    assert out[0][2].endswith("on 14 November") and out[1][0] == "assessment"
    assert out[1][2].startswith("a discussion of support")


def test_an_inference_clause_and_text_after_the_last_citation_are_assessments() -> None:
    out = _tails("Russia and Ukraine swapped prisoners on 3 May, which means talks continue [2].")
    assert out[-1] == ("assessment", 0, "which means talks continue.")
    out = _tails("The ambassador welcomed the talks on 14 November [14]; this is a diplomatic statement, "
                 "not a posture change.")
    assert out[-1][0] == "assessment" and out[-1][1] == 0


def test_an_absence_tail_is_an_absence_piece() -> None:
    out = _tails('Carney said a deal is possible but "not yet there" on 14 November — no new measure '
                 'and no relief in this window [20].')
    assert out[-1] == ("absence", None, "no new measure and no relief in this window.")


@pytest.mark.parametrize("text", [
    "Russia and Ukraine agreed a prisoner exchange on 3 May, officials said [2].",       # attribution
    "Exports rose 12%, imports fell 4% and the deficit hit $3bn [1].",                  # facts
    "The fleet seized a tanker on Friday, signalling a blockade of 3 ports [1].",       # a figure stays
    "Strikes hit ports, a step rather than a campaign [1].",                            # head too short
    "Prices rose 5% on Monday [1]; 3 more rises are due.",                              # uncited figure
])
def test_what_is_never_split_off(text: str) -> None:
    pieces = split_tails(text, split_sentence(text))
    assert not any(p.code_kind for p in pieces), [(p.code_kind, p.plain) for p in pieces]


FACT_ROW = EvidenceRef(1, "signal", "sig-1", "On Friday the Northern Fleet seized a Borovian tanker off Kessel.",
                       published_at=datetime(2026, 11, 13, 12, tzinfo=UTC), source_id="src-1",
                       access_class="public")


def _item(text: str, evidence=(FACT_ROW,), refs=None) -> TypingInput:
    refs = refs if refs is not None else tuple(CitedRef(e.ordinal, "signal", e.ref_id, None, None, True)
                                               for e in evidence)
    sent = CitedSentence(0, text, text.replace(" [1]", ""), refs, ())
    subj = SubjectCandidate("target", "borovia", ["Borovia", "Borovian", "Northern Fleet"])
    return TypingInput(sent, tuple(evidence), (subj,), read_at=datetime(2026, 11, 14, tzinfo=UTC))


HANDED = HandedSlice.of(["sig-1"], "run-1", collection="borovia", window_start=WS, window_end=WE)


def test_code_typed_pieces_are_typed_without_the_seat_and_shown_as_such() -> None:
    text = "The Northern Fleet seized a Borovian tanker on Friday, which means a blockade is near [1]."
    item = _item(text)
    pieces = code_pieces(text)
    assert [p.code_kind for p in pieces] == [None, "assessment"]
    prompt = build_piece_prompt(item, pieces)
    assert "P2 · code-typed assessment (do not type it)" in prompt
    reply = {"pieces": [{"piece": 1, "kind": "event", "subject": "S1", "valid_from": "2026-11-13",
                         "valid_from_precision": "day", "slots": {},
                         "support": [{"row": 1, "quote": "the Northern Fleet seized a Borovian tanker"}],
                         "relation": {"action": "new"}},
                        {"piece": 2, "kind": "event", "subject": "S1", "support": []}]}   # ignored
    out = assemble_pieces(item, reply, handed=HANDED)
    assert [p.outcome for p in out.pieces] == [ESTABLISHED_ELIGIBLE, ASSESSMENT]
    tail = out.pieces[1]
    assert tail.detail["typed_by"] == "code_tail" and tail.result.detail["premises"] == 1
    assert tally_pieces([out])["code_typed"] == {"code_tail:assessment:assessment": 1}


def test_a_code_tail_whose_head_failed_is_an_assessment_on_nothing() -> None:
    text = "The Northern Fleet seized a Borovian tanker on Friday, which means a blockade is near [1]."
    out = assemble_pieces(_item(text), {"pieces": [{"piece": 1, "kind": "event", "subject": "S9",
                                                    "support": [], "relation": {"action": "new"}}]},
                          handed=HANDED)
    assert out.pieces[0].outcome == "failed"
    assert out.pieces[1].outcome == ASSESSMENT and out.pieces[1].detail.get("premise_unavailable")


def test_an_absence_tail_is_gated_as_an_absence() -> None:
    text = "The Northern Fleet seized a Borovian tanker on Friday, with no other seizure in this window [1]."
    pieces = code_pieces(text)
    assert [p.code_kind for p in pieces] == [None, "absence"]
    out = assemble_pieces(_item(text), {"pieces": []}, handed=HANDED)
    assert out.pieces[1].outcome == ABSENCE


def test_no_call_when_code_typed_every_piece() -> None:
    text = "Posture is unchanged [3]."
    refs = (CitedRef(3, "prior_read", "r", None, None, False),)
    item = _item(text, evidence=(), refs=refs)
    assert not needs_seat(code_pieces(text), item)

    class Boom:
        async def chat_complete(self, *a: Any, **k: Any) -> Any:
            raise AssertionError("no call expected")

    out = asyncio.run(type_pieces(Boom(), item, system_prompt="x", handed=HANDED))
    assert out.call is None and out.detail["seat_called"] is False


# ---------------------------------------------------------------------------
# 4. PLANTED — no other supporting quote; twenty per kind; controls
# ---------------------------------------------------------------------------

BASES = [sp.from_canary(c) for c in CANARY] + [sp.from_canary(c) for c in CONTROLS]


def test_the_paraphrase_controls_are_known_true_and_pass_every_code_gate() -> None:
    assert len(CONTROLS) == 20 and len({c.item_id for c in CONTROLS}) == 20
    assert any(len(c.support) == 2 for c in CONTROLS)
    for c in CONTROLS:
        rep = g.evaluate_code_gates(c.statement, c.support, texts=c.texts, subject_names=c.subject_names,
                                    handed=HandedSlice.of([c.support[0].ref_id]))
        assert rep.passed, (c.item_id, [f.reason for f in rep.failures])
        assert not [f for f in code_precheck(c.statement, c.support, c.texts) if not f.passed]


def test_twenty_of_each_kind_from_canary_and_controls_deterministically() -> None:
    items = sp.planted_set(BASES, per_kind=20, true_controls=0)
    counts = Counter(i.truth for i in items)
    assert counts == {k: 20 for k in sp.PLANTED_KINDS}, counts
    assert items == sp.planted_set(BASES, per_kind=20, true_controls=0)


@pytest.mark.parametrize("kind", ["other_version_quote", "claim_as_fact"])
def test_a_quote_plant_replaces_every_quote(kind: str) -> None:
    multi = [b for b in BASES if len(b.support) > 1]
    assert multi
    for b in multi:
        m = sp.plant(b, kind, index=0)
        if m is None:
            continue
        old = {r.quote for r in b.support}
        assert not old & {r.quote for r in m.support}, m.item_id
        stored = " ".join(m.texts.by_signal.values())
        assert all(r.quote in stored for r in m.support)
        if kind == "other_version_quote":
            assert not any(q in stored for q in old)
        else:
            assert all(" claimed that " in r.quote for r in m.support)


def test_a_figure_or_date_plant_is_unsupported_by_every_quote() -> None:
    for b in BASES:
        n = sp.plant(b, "wrong_number")
        if n is not None:
            fails = [c for c in code_precheck(n.statement, n.support, n.texts) if not c.passed]
            assert any(c.check == "numbers" for c in fails), n.item_id
        d = sp.plant(b, "wrong_date")
        if d is not None:
            fails = [c for c in code_precheck(d.statement, d.support, d.texts) if not c.passed]
            assert any(c.check == "dates" for c in fails), d.item_id


def test_a_wrong_subject_names_the_true_subject_nowhere() -> None:
    for b in BASES:
        m = sp.plant(b, "wrong_subject")
        if m is not None:
            assert not any(n in m.statement.text for n in b.subject_names)


# ---------------------------------------------------------------------------
# 5. THINKING reaches the call only when asked
# ---------------------------------------------------------------------------


class _Rec:
    def __init__(self) -> None:
        self.kwargs: list[dict[str, Any]] = []

    async def chat_complete(self, messages: Any, **kwargs: Any) -> Any:
        self.kwargs.append(kwargs)

        class R:
            content = '{"verdict": "supports", "reason_code": "fully_stated"}'
            usage = None
        return R()


@pytest.mark.parametrize("thinking,expected", [(None, None), (True, True), (False, False)])
def test_call_seat_passes_thinking_only_when_asked(thinking: bool | None, expected: bool | None) -> None:
    rec = _Rec()
    _, call = asyncio.run(call_seat(rec, seat="s", system_prompt="p", user_content="u", thinking=thinking))
    assert ("thinking" in rec.kwargs[0]) is (expected is not None)
    assert rec.kwargs[0].get("thinking") == expected and call.thinking == thinking


def test_the_verifier_passes_thinking_through() -> None:
    rec = _Rec()
    c = CANARY[0]
    rep = asyncio.run(verify_support(rec, c.statement, c.support, texts=c.texts, system_prompt="p",
                                     thinking=True))
    assert rep.establishes and rec.kwargs[0]["thinking"] is True
