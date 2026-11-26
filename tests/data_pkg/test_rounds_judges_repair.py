# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The repair retry, verdict/2 judges, the calibration host rule,
the planted positions, decide-plane judges, the tool-read bound and the
shadow gate on statement_gate.v3 — against fakes. No database, no model, no
network.
"""
from __future__ import annotations

import asyncio
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from legba.data.rounds import judge_planted as jp
from legba.data.rounds import judge_render as jr
from legba.data.rounds.calibration import CalibrationBook, card_sha256, judge, save_book
from legba.data.rounds.gates import GateContext, dates_in_evidence, numbers_in_evidence
from legba.data.rounds.interpreter import Interpreter
from legba.data.rounds.model_seats import (
    LoopThread,
    ModelSeats,
    output_contract,
    render_prompt,
    render_repair,
)
from legba.data.rounds.repair import repair_note
from legba.data.rounds.rows import parse_row
from legba.data.rounds.seats import Dispatch, FrozenClock, SeatCall, SeatFailure, SeatReply, SyntheticSeats
from legba.data.rounds.spec import parse_spec
from legba.data.rounds.store import MemoryStore
from legba.data.rounds.tables import Handed
from legba.data.rounds.tally import entry_outcome, votes_of
from legba.data.rounds.tool_binding import Exclusions, RoundTools
from legba.data.rounds.validate import validate
from legba.data.statement_planted import TRUE

from ._rounds_helpers import INPUTS, SECOND, base_spec, mutate, write_cards

ROW = {"id": "row-0", "kind": "signal", "published_at": "2026-03-13T08:00:00+00:00",
       "text": "Officials said the plant shut 3 lines on 12 March after the storm"}


def _spec(raw):
    spec, refusals = parse_spec(raw)
    assert spec is not None, refusals
    return spec


# --------------------------------------------------------------------------
# Gates name the sentence; the date rule reads the row's publication day
# --------------------------------------------------------------------------


def _ctx(sentences: list[dict[str, Any]]) -> GateContext:
    return GateContext("a", "position/1", {"sentences": sentences},
                       [Handed("freeze", "f#0", "freeze", "frozen_scope/1", {"rows": [ROW]})], "r")


def test_failed_number_checks_name_the_sentence_and_a_row_id_in_text_fails():
    good = {"key": "ok", "text": "The plant shut 3 lines.",
            "support": [{"ref_id": "row-0", "quote": "the plant shut 3 lines"}]}
    bad = {"key": "uuid", "text": "Row 2787733b-c9bb-43a7 says the plant shut 3 lines.",
           "support": [{"ref_id": "row-0", "quote": "the plant shut 3 lines"}]}
    res = numbers_in_evidence(_ctx([good, bad]))
    assert not res.passed
    failed = res.as_dict()["failed_checks"]
    assert [f["sentence"] for f in failed] == ["uuid"] and failed[0]["sentence_index"] == 1


def test_the_date_rule_accepts_the_rows_publication_day():
    pub = {"key": "pub", "text": "On 13 March the plant shut 3 lines.",
           "support": [{"ref_id": "row-0", "quote": "the plant shut 3 lines"}]}
    other = {"key": "late", "text": "On 20 March the plant shut 3 lines.",
             "support": [{"ref_id": "row-0", "quote": "the plant shut 3 lines"}]}
    assert dates_in_evidence(_ctx([pub])).passed
    res = dates_in_evidence(_ctx([other])).as_dict()
    assert res["failed_checks"][0]["sentence"] == "late"


# --------------------------------------------------------------------------
# The repair retry
# --------------------------------------------------------------------------


def _repair_spec(**seat_kw):
    raw = mutate(base_spec())
    raw["seats"]["a"].update({"retries": 1, "repair": "reasons",
                              "gates": ["handed_rows", "quote_in_source", "numbers_in_evidence"]},
                             **seat_kw)
    return raw


def test_repair_reasons_needs_a_retry_and_a_model_seat():
    raw = _repair_spec(retries=0)
    assert "bound_invalid" in validate(_spec(raw)).reasons()
    raw = mutate(base_spec())
    raw["seats"]["table"]["repair"] = "reasons"
    assert "seat_kind_invalid" in validate(_spec(raw)).reasons()


def test_a_refused_attempt_hands_its_retry_the_gates_reasons(tmp_path: Path):
    write_cards(tmp_path)
    seen: list[SeatCall] = []
    synth = SyntheticSeats()

    def provider(call: SeatCall) -> SeatReply:
        if call.seat_id != "a":
            return synth(call)
        seen.append(call)
        rows = [r for h in call.inputs if isinstance(h.payload, dict) for r in h.payload.get("rows", ())]
        quote = " ".join(rows[0]["text"].split()[:5])
        text = ("A synthetic row reports 999 units." if call.attempt == 0
                else "A synthetic row reports the state.")
        return SeatReply({"sentences": [{"key": "k1", "text": text,
                                         "support": [{"ref_id": rows[0]["id"], "quote": quote}]}]},
                         elapsed_s=1.0)

    store = MemoryStore()
    rec = Interpreter(_spec(_repair_spec()), store, Dispatch(provider), inputs=INPUTS, run_id="r",
                      clock=FrozenClock(), card_root=tmp_path).run()
    assert rec["run"]["state"] == "ended"
    assert [c.attempt for c in seen] == [0, 1]
    assert seen[0].repair is None
    note = seen[1].repair
    assert note["kind"] == "gates" and note["failed"][0]["sentence"] == "k1"
    assert note["failed"][0]["reason"] == "number_not_in_evidence"
    assert "999" in note["failed"][0]["missing"] and note["previous"]["sentences"]
    second = next(r for r in store.receipts() if r.seat == "a" and r.attempt == 1)
    assert second.released and second.repair["from_attempt"] == 0
    # the note is rendered into the retry's prompt
    _, user = render_prompt(SeatCall(**{**seen[1].__dict__, "card_text": "# card"}))
    assert "was refused" in user and "sentence `k1`" in user and "999" in user


def test_a_parse_failure_hands_the_parse_error_and_a_deadline_hands_nothing():
    class R:
        status, released, attempt = "failed", False, 0
        failure = "unparseable: position/1: sentences.0.stance: Input should be 'supports'"
        raw = "{not json"
        gates = payload = None

    note = repair_note(R())
    assert note["kind"] == "parse" and "stance" in note["error"] and note["raw_head"]
    R.failure = "deadline"
    assert repair_note(R()) is None


def test_a_parse_repair_shows_the_refused_reply():
    note = {"attempt": 0, "kind": "parse",
            "error": "unparseable: position/1: sentences.0.stance: Input should be 'supports'",
            "raw_head": '{"sentences": [{"key": "k1", "stance": "agrees"}]}'}
    user = render_repair(note)
    assert "It could not be read:" in user
    assert "Your refused reply began:" in user
    assert '"stance": "agrees"' in user


def test_parse_row_lists_every_validation_error():
    bad = {"sentences": [
        {"key": "k1", "text": "a", "stance": "agrees"},
        {"key": "k2", "text": "b", "support": [{"ref_id": "r", "kind": "page"}]},
    ]}
    with pytest.raises(Exception) as info:
        parse_row("position/1", bad)
    msg = str(info.value)
    assert "sentences.0.stance" in msg and "sentences.1.support.0.kind" in msg
    assert "'supports'" in msg and "'signal'" in msg  # the allowed values are named


def test_parse_row_caps_the_error_list():
    bad = {"sentences": [{"key": f"k{i}", "text": "a", "stance": "x"} for i in range(12)]}
    with pytest.raises(Exception) as info:
        parse_row("position/1", bad)
    msg = str(info.value)
    assert "sentences.7.stance" in msg and "sentences.8.stance" not in msg
    assert "4 more" in msg


def test_the_contract_lists_closed_fields_and_says_not_to_copy_the_shape():
    text = output_contract("position/1")
    assert "`stance`: supports, opposes, neutral, unsure" in text
    assert "do not copy its words as values" in text


# --------------------------------------------------------------------------
# verdict/2 judges
# --------------------------------------------------------------------------


def test_a_failing_sentence_check_makes_the_candidate_fall():
    v = {"candidate": "c", "outcome": "stands", "reason": "supported",
         "checks": [{"sentence": "s1", "holds": True}, {"sentence": "s2", "holds": False,
                                                        "problem": "contradicts_quote"}]}
    assert entry_outcome(v) == ("falls", "contradicted")
    assert entry_outcome({"candidate": "c", "outcome": "stands", "checks": [
        {"sentence": "s1", "holds": True}]}) == ("stands", "other")
    votes = votes_of("j", {"verdicts": [v]}, ["c"])
    assert votes["c"].outcome == "falls"


def _table(n: int = 3) -> Handed:
    items = jp.calibration_set()[:n]
    return Handed("table", "t#0", "table", "sentence_table/1", jp.table_of(items)), items


def test_the_judge_view_is_a_table_of_live_candidates_and_reads_back_by_label():
    h, items = _table()
    ids = [it.item_id for it in items]
    system, user, view = jr.render("# card", [h], ids[1:])
    assert "### Candidate A" in user and "### Candidate C" not in user
    assert ids[0] not in user and "A1." in user and "quote (evidence" in user
    assert "verdict/2" in system and "(A, B)" in system and "{" not in user.split("\n")[0]
    reply = {"verdicts": [{"candidate": "A", "checks": [{"sentence": "A1", "holds": False,
                                                         "problem": "number_changed"}],
                           "weakest": "A1", "outcome": "falls", "reason": "unsupported"},
                          {"candidate": "Q"}], "ranking": ["B", "A", "Z"]}
    payload, unmapped = jr.read_back(reply, view)
    assert payload["verdicts"][0]["candidate"] == ids[1]
    assert payload["verdicts"][0]["checks"][0]["sentence"] == "k1"
    assert payload["ranking"] == [ids[2], ids[1]]
    assert sorted(unmapped) == ["candidate:Q", "ranking:Z"]
    parse_row("verdict/2", payload)


class FakeDecide:
    subprovider = "decide"

    def __init__(self) -> None:
        self.asked: list[dict[str, Any]] = []

    async def decide(self, state: str, questions: dict[str, Any], **kw: Any) -> dict[str, Any]:
        self.asked.append(questions)
        return {q: {"choice": "fails" if q.startswith("A") else "holds",
                    "probabilities": {"holds": 0.1 if q.startswith("A") else 0.9}}
                for q in questions}


def test_a_decision_plane_judge_answers_typed_questions_per_sentence():
    h, items = _table(2)
    ids = tuple(it.item_id for it in items)
    seat = _spec(base_spec()).seats["j1"].model_copy(update={"output": "verdict/2"})
    lt = LoopThread()
    try:
        fake = FakeDecide()
        taken = []
        models = ModelSeats(planes={SECOND: fake}, run=lt.run, take_call=lambda: taken.append(1) or True)
        reply = models(SeatCall("r", "j1", seat, "verdict/2", 0, 0, "p", {}, (h,), "cards/judge.md",
                                "# card", candidates=ids))
    finally:
        lt.close()
    assert len(taken) == 1 and len(fake.asked) == 1
    assert set(fake.asked[0]) == {"A1", "A2", "A3", "B1", "B2", "B3"}
    out = parse_row("verdict/2", reply.payload)
    outcomes = {v["candidate"]: entry_outcome(v)[0] for v in out["verdicts"]}
    assert outcomes == {ids[0]: "falls", ids[1]: "stands"}
    assert out["ranking"] == [ids[1], ids[0]] and reply.meta["decide"] is True


# --------------------------------------------------------------------------
# The calibration rule (host rule in the interpreter)
# --------------------------------------------------------------------------


def _rec(card_text: str, catch: float, fr: float, row_type: str = "verdict/1") -> dict[str, Any]:
    return {"card_sha256": card_sha256(card_text), "plane": SECOND, "row_type": row_type,
            "render": jr.render_version(row_type), "catch": {"rate": catch}, "false_reject": {"rate": fr}}


def test_the_rule_needs_the_catch_floor_and_the_false_reject_ceiling():
    assert judge(None).reason == "not_calibrated"
    assert judge({"catch": {"rate": 0.89}, "false_reject": {"rate": 0.0}}).reason == "catch_below_floor"
    assert judge({"catch": {"rate": 1.0}, "false_reject": {"rate": 0.2}}).reason == \
        "false_reject_above_ceiling"
    assert judge({"catch": {"rate": 0.9}, "false_reject": {"rate": 0.15}}).may_gate


def _panel_run(tmp_path: Path, book: CalibrationBook | None, votes: dict[str, str]):
    write_cards(tmp_path)
    raw = mutate(base_spec())
    raw["seats"]["j1"]["card"] = "cards/judge.md"
    raw["seats"]["gate"] = {"seat_ref": "j1", "card": "cards/gate.md"}
    (tmp_path / "cards" / "gate.md").write_text("gate card\n", encoding="utf-8")
    raw["flow"]["seq"][3]["panel"]["gates_first"] = ["gate"]
    rec = Interpreter(_spec(raw), MemoryStore(), Dispatch(SyntheticSeats(votes=votes)), inputs=INPUTS,
                      run_id="r", clock=FrozenClock(), card_root=tmp_path, calibration=book).run()
    return rec["tallies"][0]


def test_uncalibrated_judges_neither_remove_nor_vote(tmp_path: Path):
    t = _panel_run(tmp_path, CalibrationBook.of([]), {"gate": "falls"})
    assert t["removed"] == [] and t["outcome"] == "no_quorum"
    assert {a["stage"] for a in t["advisory"]} == {"gates", "votes"}
    gate = next(a for a in t["advisory"] if a["stage"] == "gates")
    assert gate["reason"] == "not_calibrated" and gate["would_remove"]
    assert t["calibration"]["enforced"] is True


def test_calibrated_judges_gate_and_no_book_says_not_enforced(tmp_path: Path):
    book = CalibrationBook.of([_rec("card judge.md\n", 0.95, 0.0), _rec("gate card\n", 0.95, 0.0)])
    t = _panel_run(tmp_path, book, {"gate": "falls"})
    assert t["removed"] and not t["advisory"] and t["outcome"] == "falls"
    t = _panel_run(tmp_path, None, {})
    assert t["calibration"] == {"enforced": False} and t["outcome"] == "stands"


def test_a_book_round_trips(tmp_path: Path):
    save_book(tmp_path / "b.json", [_rec("x", 0.95, 0.0)])
    book = CalibrationBook.load(tmp_path / "b.json")
    assert book.standing(card_sha256("x"), SECOND, "verdict/1", jr.render_version("verdict/1")).may_gate
    assert CalibrationBook.load(tmp_path / "missing.json").records == ()


# --------------------------------------------------------------------------
# The planted positions
# --------------------------------------------------------------------------


def test_the_planted_set_is_deterministic_complete_and_leaves_no_other_support():
    a, b = jp.calibration_set(), jp.calibration_set()
    assert [i.item_id for i in a] == [i.item_id for i in b]
    kinds = Counter(i.truth for i in a)
    assert kinds[TRUE] == 12 and all(kinds[k] == 3 for k in jp.ALL_KINDS)
    for it in a:
        assert not jp.supporting_elsewhere(it)
        cited = {c["ref_id"] for s in it.position["sentences"] for c in s["cited"]}
        assert cited <= {r["id"] for r in it.rows}, it.item_id
    batches = jp.batches(a)
    assert sum(len(x) for x in batches) == len(a)
    assert all(len({i.item_id for i in x}) == len(x) for x in batches)


def test_each_position_plant_carries_its_error():
    by = {i.truth: i for i in jp.calibration_set()}
    key = lambda it: next(s for s in it.position["sentences"] if s["key"] == "k1")  # noqa: E731
    assert "denied" in key(by["contradicts_own_rows"])["cited"][0]["quote"]
    assert by["restates_without_evidence"].position["sentences"][0]["cited"] == []
    unsup = key(by["key_sentence_unsupported"])
    assert unsup["cited"][0]["quote"].split()[0].lower() not in unsup["text"].lower()[:5]


def test_scoring_counts_falls_and_ranking_pairs():
    s = jp.calibration_set()
    planted = next(i for i in s if i.planted)
    true = next(i for i in s if not i.planted)
    out = jp.score([(planted, "falls", [true.item_id, planted.item_id]),
                    (true, "stands", [true.item_id, planted.item_id])])
    assert out["catch"]["k"] == 1 and out["false_reject"]["k"] == 0
    assert out["ranking_pairs"]["rate"] == 1.0


# --------------------------------------------------------------------------
# Tool reads are bounded and timed
# --------------------------------------------------------------------------


def test_a_slow_tool_read_is_cut_off_and_timed():
    async def slow(name, args, known_at):
        await asyncio.sleep(5)

    tools = RoundTools(dispatch=slow, known_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                       packs={"substrate_read": ("search_signals",)},
                       classified={"search_signals": "cut"}, exclusions=lambda: Exclusions(),
                       timeout_s=0.05)
    seat = _spec(base_spec()).seats["a"].model_copy(update={"tools": ("substrate_read",)})
    call = SeatCall("r", "a", seat, "position/1", 0, 0, "p", {}, ())
    out, rows, entry = asyncio.run(tools.call(call, "search_signals", {}))
    assert entry["failed"] == "tool_timeout" and entry["duration_s"] < 1 and "tool_timeout" in out["error"]


# --------------------------------------------------------------------------
# The shadow gate (statement_gate.v3)
# --------------------------------------------------------------------------


def test_the_shadow_gate_splits_picks_the_named_subject_and_types_outcomes():
    from legba.data.rounds.shadow_gate import gate_sentence, handed_slice
    from legba.data.statement_subject_names import resolve_subject_names
    from legba.data.statement_typing import SubjectCandidate

    subjects = {"desk_x": {"name": "Watch — Calder", "geo": []}}
    desk = SubjectCandidate("target", "desk_x", resolve_subject_names(
        "target", "desk_x", subjects=subjects, gazetteer={}, metonyms=()))
    rows = {"11111111-1111-1111-1111-111111111111": {
        "text": "Officials said a fire at Calder port destroyed 3 storage tanks on 12 March.",
        "published_at": "2026-03-13T00:00:00+00:00", "source_id": "src"}}
    h = handed_slice(rows, "r", subject="desk_x", as_of=datetime(2026, 3, 14, tzinfo=timezone.utc),
                     window_hours=168)
    ok = {"key": "s1", "text": "A fire at Calder port destroyed 3 storage tanks on 12 March.",
          "support": [{"ref_id": "11111111-1111-1111-1111-111111111111", "role": "evidence",
                       "kind": "signal", "quote": "a fire at Calder port destroyed 3 storage tanks on 12 March"}]}
    out = gate_sentence(ok, subject="desk_x", offered=[desk], rows=rows, linked=[], handed=h)
    assert out[0]["gate"]["outcome"] == "established_eligible", out[0]["gate"]
    none = {"key": "s2", "text": "No row in the slice reports casualties.", "support": []}
    assert gate_sentence(none, subject="desk_x", offered=[desk], rows=rows, linked=[],
                         handed=h)[0]["gate"]["outcome"] == "absence"
    judgement = {"key": "s3", "text": "The port is likely to stay shut.", "support": []}
    got = gate_sentence(judgement, subject="desk_x", offered=[desk], rows=rows, linked=[], handed=h)
    assert got[0]["gate"]["outcome"] == "assessment" and got[0]["gate"]["premises"] == 0
    assert all(p["gate"]["restatement_checked"] is False for p in out + got)


def test_report_json_safe():
    json.dumps(jp.score([]))


def test_a_refused_quote_carries_the_rows_nearest_own_words():
    from legba.data.rounds.gates import closest_passage, quote_in_source

    s = {"key": "q", "text": "The plant shut lines after the storm.",
         "support": [{"ref_id": "row-0", "quote": "the plant closed 3 lines on 12 March"}]}
    res = quote_in_source(_ctx([s])).as_dict()
    hint = res["failed_checks"][0]["detail"]["closest_passage"]
    assert "shut 3 lines on 12 March" in hint and hint in ROW["text"]
    assert closest_passage("bananas are yellow fruit", ROW["text"]) is None
