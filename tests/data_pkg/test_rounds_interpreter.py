# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Lane G-2: the interpreter and the harness host, with synthetic seats.

Every shape runs; a run killed at any step resumes from its file receipts to
the same record; a planted leak is refused by the store; a late seat in a
blind phase is recorded absent and the phase closes by rule; budgets end
phases as partial; the blind-prompt property holds (no seat is ever handed a
blind sibling's or a withheld field's text); random bounded specs terminate
within their computed worst case. Pure — no database, no model.
"""
from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any

import pytest
import yaml

from legba.data.rounds.harness import main as harness_main
from legba.data.rounds.interpreter import Interpreter, ReplayDiverged, SpecChanged, waves
from legba.data.rounds.refs import parse_ref
from legba.data.rounds.seats import (
    Dispatch,
    FrozenClock,
    ScriptedSeats,
    SeatCall,
    SeatNotBuilt,
    SeatReply,
    SyntheticSeats,
)
from legba.data.rounds.spec import parse_spec
from legba.data.rounds.store import FileStore, MemoryStore
from legba.data.rounds.validate import validate
from legba.data.rounds.visibility import Position, VisibilityRefused

from ._rounds_helpers import INPUTS, TreeGen, base_spec, judge, model, mutate, rich_spec, write_cards


def _spec(raw: dict[str, Any]):
    spec, refusals = parse_spec(raw)
    assert spec is not None, refusals
    return spec


def run(raw: dict[str, Any], tmp_path: Path, seats: Any = None, *, store: Any = None,
        validated: bool = True, inputs: dict[str, Any] | None = None, run_id: str = "run-1"):
    write_cards(tmp_path)
    it = Interpreter(_spec(raw), store if store is not None else MemoryStore(),
                     Dispatch(seats or SyntheticSeats()), inputs=inputs or INPUTS, run_id=run_id,
                     clock=FrozenClock(), card_root=tmp_path, validated=validated)
    return it.run(), it


def steps(rec: dict[str, Any], seat: str | None = None) -> list[dict[str, Any]]:
    return [s for s in rec["steps"] if seat is None or s["seat"] == seat]


def phase(rec: dict[str, Any], path: str) -> dict[str, Any]:
    return next(p for p in rec["phases"] if p["path"] == path)


def _strip(rec: dict[str, Any]) -> dict[str, Any]:
    out = json.loads(json.dumps(rec, sort_keys=True, default=str))
    for k in ("resumes", "ended_at", "started_at"):
        out["run"].pop(k, None)
    for s in out["steps"]:
        s.pop("sealed_at", None)
    return out


# --------------------------------------------------------------------------
# Every shape runs
# --------------------------------------------------------------------------


def test_rich_round_runs_every_shape_and_records_everything(tmp_path):
    rec, it = run(rich_spec(), tmp_path)
    assert rec["run"]["state"] == "ended", rec["run"]
    closes = {p["path"]: p["close_reason"] for p in rec["phases"]}
    assert closes["main/open"] == "all_sealed"
    assert closes["main/rebut"] in ("passes", "unchanged")
    assert closes["main/judging/judge"] in ("stands", "falls", "tie", "mixed", "no_quorum")
    assert closes["main/per_h/[h0]/hunt"] == "until"  # synthetic assessor is diagnostic on pass 2
    assert closes["main/rule"] in ("escalated", "not_escalated")
    assert closes["main/close"] in ("case[0]", "default")
    assert all(p["status"] == "closed" for p in rec["phases"])
    for s in steps(rec):
        if s["status"] == "sealed":
            assert len(s["sha256"]) == 64 and s["spec"]["sha256"] == it.spec.sha256
    assert rec["tallies"] and rec["tallies"][0]["phase"] == "judge"
    w = rec["worst_case"]
    t = rec["totals"]
    assert t["model_calls"] <= w["calls"] and t["charged_s"] <= w["seconds"]
    assert t["peak_parallel"] <= w["peak_parallel"]


def test_runs_are_deterministic(tmp_path):
    a, _ = run(rich_spec(), tmp_path)
    b, _ = run(rich_spec(), tmp_path)
    assert _strip(a) == _strip(b)


def test_waves_pack_by_demand_under_the_cap():
    assert waves([1, 1, 1, 1, 1], 2) == [[0, 1], [2, 3], [4]]
    assert waves([3, 2, 0, 0], 4) == [[0], [1, 2, 3]]
    assert waves([1, 1, 1], 6, per_wave=2) == [[0, 1], [2]]


# --------------------------------------------------------------------------
# Resume from file receipts
# --------------------------------------------------------------------------


class _Crash(BaseException):
    """A process kill: not an Exception, so nothing catches it as a failure."""


class Crashing:
    def __init__(self, inner: Any, at: int):
        self.inner, self.at, self.n = inner, at, 0

    def __call__(self, call: SeatCall) -> SeatReply:
        self.n += 1
        if self.n == self.at:
            raise _Crash(f"killed at call {self.at}")
        return self.inner(call)


def test_kill_at_every_step_and_resume_to_the_same_record(tmp_path):
    counter = Crashing(SyntheticSeats(), at=0)
    ref, _ = run(rich_spec(), tmp_path / "ref", counter, store=FileStore(tmp_path / "ref" / "run"))
    n_calls = counter.n  # every provider call: a kill can land on any of them
    assert n_calls > 20
    for at in range(1, n_calls + 1):
        folder = tmp_path / f"k{at}"
        with pytest.raises(_Crash):
            run(rich_spec(), folder, Crashing(SyntheticSeats(), at), store=FileStore(folder / "run"))
        resumed, _ = run(rich_spec(), folder, store=FileStore(folder / "run"))
        assert _strip(resumed) == _strip(ref), f"diverged after a kill at call {at}"
        assert resumed["run"]["resumes"] == 1


def test_resuming_a_folder_with_another_spec_version_is_refused(tmp_path):
    run(base_spec(), tmp_path, store=FileStore(tmp_path / "run"))
    other = mutate(base_spec())
    other["flow"]["seq"].insert(1, {"seat": "x"})
    other["seats"]["x"] = model()
    with pytest.raises(SpecChanged):
        run(other, tmp_path, store=FileStore(tmp_path / "run"))


def test_a_replay_that_lands_on_another_step_number_is_refused(tmp_path):
    store = MemoryStore()
    run(base_spec(), tmp_path, store=store)
    moved = store.get_receipt("main/editor#0")
    moved.seq += 1  # as if the walk had changed under the same spec hash
    with pytest.raises(ReplayDiverged):
        run(base_spec(), tmp_path, store=store)


# --------------------------------------------------------------------------
# Blindness is the store's job
# --------------------------------------------------------------------------


def test_planted_leak_is_refused_by_the_store_at_run_time(tmp_path):
    raw = mutate(base_spec())
    raw["seats"]["b"] = model(reads=[{"from": "a"}])  # the validator would refuse this
    assert not validate(_spec(raw)).ok
    rec, _ = run(raw, tmp_path, validated=False)
    b = steps(rec, "b")
    assert b[0]["failure"] == "visibility_refused"
    assert b[-1]["status"] == "absent"
    assert rec["run"]["state"] == "ended"  # min_present 2 is still met by a and c


def test_inputs_for_raises_on_a_sibling_in_an_open_blind_phase(tmp_path):
    raw = mutate(base_spec())
    rec, it = run(raw, tmp_path)
    # reopen the blind phase in a copy of the store and ask for a sibling
    store = MemoryStore()
    for r in it.store.receipts():
        store.put_receipt(r)
    for p in it.store.phases():
        p2 = type(p).from_json(p.to_json())
        if p2.path == "main/open":
            p2.status = "open"
        store.put_phase(p2)
    it.vis.store = store
    with pytest.raises(VisibilityRefused):
        it.vis.resolve(parse_ref("a"), "b", Position(("main", "open")))


def test_late_seat_is_absent_and_the_blind_phase_closes_by_rule(tmp_path):
    rec, _ = run(base_spec(), tmp_path, SyntheticSeats(late=["c"]))
    c = steps(rec, "c")
    assert c[0]["failure"] == "deadline" and c[0]["raw"]
    assert c[-1]["status"] == "absent" and c[-1]["failure"] == "deadline"
    open_ = phase(rec, "main/open")
    assert open_["close_reason"] == "closed_with_absences" and open_["validity"] == "partial"
    assert rec["run"]["state"] == "ended"


def test_below_min_present_makes_the_phase_invalid_and_stops_the_round(tmp_path):
    rec, _ = run(base_spec(), tmp_path, SyntheticSeats(late=["b", "c"]))
    assert phase(rec, "main/open")["validity"] == "invalid"
    assert rec["run"]["state"] == "stopped_invalid"
    assert rec["run"]["stop_reason"] == "invalid_phase:open"
    assert phase(rec, "main")["close_reason"] == "invalid_phase:open"


def test_a_branch_watching_phase_status_routes_an_invalid_phase(tmp_path):
    raw = mutate(base_spec())
    raw["seats"]["fallback"] = model(card="cards/editor.md", output="read/1",
                                     reads=[{"from": "input.question"}])
    raw["seats"]["editor"]["reads"] = [{"from": "input.question"}]
    raw["flow"]["seq"] = [raw["flow"]["seq"][0], raw["flow"]["seq"][1],
                          {"branch": {"cases": [{"when": "phase.open.status == invalid",
                                                 "do": {"seat": "fallback"}}],
                                      "default": {"seat": "editor"}}, "id": "route"}]
    rec, _ = run(raw, tmp_path, SyntheticSeats(late=["b", "c"]))
    assert rec["run"]["state"] == "ended"
    assert phase(rec, "main/route")["close_reason"] == "case[0]"


def _capture(inner: Any, seen: dict[str, str]):
    def provider(call: SeatCall) -> SeatReply:
        seen[f"{call.seat_id}@{call.path}@{call.candidate}"] = json.dumps(
            [h.payload for h in call.inputs], default=str)
        return inner(call)
    return provider


def _blind_siblings(rec: dict[str, Any]) -> list[tuple[dict, dict]]:
    """Pairs of receipts written in the same blind phase instance."""
    blind = {p["path"] for p in rec["phases"]
             if p["shape"] in ("blind", "rebuttal_pass", "panel_stage")}
    sealed = [s for s in rec["steps"] if s["status"] == "sealed" and s["path"] in blind]
    return [(a, b) for a in sealed for b in sealed
            if a["path"] == b["path"] and (a["seat"], a["candidate"]) != (b["seat"], b["candidate"])]


@pytest.mark.parametrize("builder", [base_spec, rich_spec])
def test_blind_prompt_property_no_sibling_or_withheld_text_reaches_a_seat(builder, tmp_path):
    seen: dict[str, str] = {}
    rec, _ = run(builder(), tmp_path, _capture(SyntheticSeats(), seen))
    pairs = _blind_siblings(rec)
    assert pairs
    for a, b in pairs:
        prompt = seen[f"{a['seat']}@{a['path']}@{a['candidate']}"]
        assert f"{b['seat']}@{b['path']}" not in prompt, (a["key"], b["key"])


def test_withheld_field_never_reaches_the_seat(tmp_path):
    raw = mutate(base_spec())
    raw["seats"]["lead"] = model(output="question_set/1")
    raw["seats"]["a"] = model(reads=[{"from": "lead"}, {"from": "freeze"}],
                              withheld=["lead.working_reading"])
    raw["flow"]["seq"].insert(1, {"seat": "lead"})
    seen: dict[str, str] = {}
    run(raw, tmp_path, _capture(SyntheticSeats(), seen))
    a_prompt = next(v for k, v in seen.items() if k.startswith("a@"))
    assert "Question" in a_prompt and "working reading" not in a_prompt
    b_prompt = next(v for k, v in seen.items() if k.startswith("editor@"))
    assert "working reading" not in b_prompt


# --------------------------------------------------------------------------
# Failures, gates and budgets
# --------------------------------------------------------------------------


def test_gate_failure_is_sealed_and_kept_but_never_released(tmp_path):
    bad = {"sentences": [{"key": "k", "text": "A sentence.",
                          "support": [{"ref_id": "row-99", "quote": "not handed at all"}]}]}
    seats = ScriptedSeats({"a": {"payload": bad, "elapsed_s": 1}}, fallback=SyntheticSeats())
    raw = mutate(base_spec())
    raw["seats"]["a"]["retries"] = 1
    rec, _ = run(raw, tmp_path, seats)
    a = steps(rec, "a")
    assert [s["status"] for s in a] == ["sealed", "sealed", "absent"]
    assert all(s["failure"] == "gate_failed" and not s["released"] for s in a[:2])
    assert {g["gate"] for g in a[0]["gates"] if not g["passed"]} == {"handed_rows", "quote_in_source"}
    assert rec["gate_rates"]["a"]["pass_rate"] == 0.0
    table_in = next(s for s in steps(rec, "table"))["handed"]
    assert all(h["ref"] != a[0]["key"] or h["absent"] for h in table_in)


def test_unparseable_reply_is_a_failure_with_the_raw_text_kept(tmp_path):
    seats = ScriptedSeats({"a": {"payload": {"nonsense": 1}, "raw": "<<garbage>>"}},
                          fallback=SyntheticSeats())
    rec, _ = run(base_spec(), tmp_path, seats)
    a = steps(rec, "a")
    assert a[0]["failure"].startswith("unparseable") and a[0]["raw"] == "<<garbage>>"


def test_on_failure_stop_round(tmp_path):
    raw = mutate(base_spec())
    raw["seats"]["editor"]["on_failure"] = "stop_round"
    rec, _ = run(raw, tmp_path, SyntheticSeats(fail={"editor": 5}))
    assert rec["run"]["state"] == "stopped_failure"
    assert rec["run"]["stop_reason"] == "seat_failed:editor"


def test_round_call_budget_stops_the_round_and_marks_phases_partial(tmp_path):
    raw = mutate(base_spec())
    raw["budgets"]["model_calls"] = 4
    rec, _ = run(raw, tmp_path, validated=False)
    assert rec["run"]["state"] == "stopped_budget"
    assert rec["totals"]["model_calls"] == 4
    assert phase(rec, "main/judge")["validity"] == "partial"
    assert steps(rec)[-1]["failure"] == "budget_model_calls"


def test_loop_call_budget_closes_the_loop_as_budget(tmp_path):
    raw = mutate(base_spec())
    raw["seats"]["again"] = model()
    raw["flow"]["seq"].insert(1, {"loop": {"body": {"seat": "again"}, "max_iterations": 5,
                                           "model_calls": 2,
                                           "until": "budget.remaining.calls < 0"}, "id": "rep"})
    rec, _ = run(raw, tmp_path)
    assert phase(rec, "main/rep")["close_reason"] == "budget"
    assert len([s for s in steps(rec, "again") if s["status"] == "sealed"]) == 2
    assert rec["run"]["state"] == "ended"


def test_par_join_first_skips_the_rest_and_records_them_absent(tmp_path):
    raw = mutate(base_spec())
    raw["budgets"]["max_parallel"] = 1
    raw["flow"]["seq"][1] = {"par": [{"seat": "a"}, {"seat": "b"}, {"seat": "c"}], "id": "open",
                             "join": "first", "deadline_s": 120}
    rec, _ = run(raw, tmp_path)
    assert phase(rec, "main/open")["close_reason"] == "join_first"
    assert [s["failure"] for s in steps(rec, "c")] == ["join_met"]


def test_debate_ends_on_concession_and_select_keeps_ties(tmp_path):
    raw = mutate(base_spec())
    raw["seats"]["p1"] = model(output="turn/1")
    raw["seats"]["p2"] = {"seat_ref": "p1"}
    raw["seats"]["mod"] = judge(reads=[{"from": "floor.sealed", "view": "sentence_table"}],
                                family_rule={"differs_from": ["p1", "p2"]})
    raw["flow"]["seq"][4:4] = [
        {"select": {"from": "open", "by": "judge", "keep": {"top": 1}}, "id": "pick"},
        {"debate": {"seats": ["p1", "p2"], "max_turns": 6, "settle_by": "mod"}, "id": "floor"},
    ]
    rec, _ = run(raw, tmp_path, SyntheticSeats(concede_after=3))
    assert phase(rec, "main/floor")["close_reason"] == "concede"
    pick = next(s for s in steps(rec, "pick"))
    # every opener stands with three votes: a three-way tie at the cut is kept whole
    assert pick["payload"]["tied_at_cut"] is True and len(pick["payload"]["kept"]) == 3
    rec2, _ = run(raw, tmp_path, SyntheticSeats(settle=True))
    assert phase(rec2, "main/floor")["close_reason"] == "settled"


def test_escalate_runs_the_deeper_seat_when_the_try_fails_with_on_failure_escalate(tmp_path):
    raw = mutate(base_spec())
    raw["seats"]["editor"]["on_failure"] = "escalate"
    raw["seats"]["deeper"] = model(card="cards/editor.md", output="read/1",
                                   reads=[{"from": "judge.tally"}], thinking="on")
    raw["flow"]["seq"][4] = {"escalate": {"try": {"seat": "editor"}, "to": "deeper",
                                          "when": "not gate.editor.passed"}, "id": "rule"}
    rec, _ = run(raw, tmp_path, SyntheticSeats(fail={"editor": 1}))
    assert phase(rec, "main/rule")["close_reason"] == "escalated"
    assert steps(rec, "deeper")[0]["status"] == "sealed"


def test_dissent_survives_to_the_record(tmp_path):
    rec, _ = run(base_spec(), tmp_path, SyntheticSeats(votes={"j3": "falls"}))
    votes = [d for d in rec["dissent"] if d["kind"] == "vote"]
    assert votes and all(d["judge"] == "j3" for d in votes)
    assert any(d["kind"] == "sentence" for d in rec["dissent"])


def test_a_substrate_function_with_no_provider_fails_loudly(tmp_path):
    it = Interpreter(_spec(base_spec()), MemoryStore(), Dispatch(None), inputs=INPUTS,
                     run_id="r", clock=FrozenClock(), card_root=write_cards(tmp_path))
    with pytest.raises(SeatNotBuilt):
        it.run()


# --------------------------------------------------------------------------
# Property: accepted random specs terminate within their worst case
# --------------------------------------------------------------------------


@pytest.mark.parametrize("seed", range(80))
def test_random_accepted_specs_terminate_within_the_worst_case(seed, tmp_path):
    raw = TreeGen(seed).spec()
    report = validate(_spec(raw))
    assert report.ok, report.refusals
    rng = random.Random(seed)
    seats = SyntheticSeats(
        elapsed=lambda call: float(rng.randint(0, call.seat.effective_deadline_s + 5)),
        fail={sid: rng.randint(0, 2) for sid in raw["seats"] if rng.random() < 0.3})
    inputs = {**INPUTS, "items": [f"i{k}" for k in range(rng.randint(0, 7))]}
    rec, _ = run(raw, tmp_path, seats, inputs=inputs)
    w, t = report.worst, rec["totals"]
    assert rec["run"]["state"] in ("ended", "stopped_invalid", "stopped_failure")
    assert t["model_calls"] <= w.calls
    assert t["charged_s"] <= w.seconds + 1e-6
    assert t["peak_parallel"] <= w.peak
    assert all(p["status"] == "closed" for p in rec["phases"])


# --------------------------------------------------------------------------
# The harness host
# --------------------------------------------------------------------------


def test_harness_cli_runs_a_spec_file_and_writes_the_record(tmp_path, capsys):
    write_cards(tmp_path)
    spec_file = tmp_path / "round.yaml"
    spec_file.write_text(yaml.safe_dump({"round": base_spec()}), encoding="utf-8")
    script = tmp_path / "script.yaml"
    script.write_text(yaml.safe_dump({"j3": {"payload": {"verdicts": []}, "elapsed_s": 2}}),
                      encoding="utf-8")
    out = tmp_path / "out"
    args = [str(spec_file), "--out", str(out), "--input", "subject=s", "--input", "question=q?",
            "--input", "as_of=2026-01-01", "--run-id", "cli", "--script", str(script)]
    assert harness_main(args) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["state"] == "ended" and printed["totals"]["model_calls"] == 7
    rec = json.loads((out / "record.json").read_text())
    assert rec["run"]["run_id"] == "cli" and (out / "receipts").is_dir()
    assert harness_main(args) == 0  # a second run on the folder is a pure replay
    again = json.loads((out / "record.json").read_text())
    assert again["run"]["resumes"] == 1 and _strip(again) == _strip(rec)


def _replayable_seats(seed: int, raw: dict[str, Any]) -> SyntheticSeats:
    rng = random.Random(seed)
    fail = {sid: rng.randint(0, 2) for sid in raw["seats"] if rng.random() < 0.3}

    def elapsed(call: SeatCall) -> float:
        key = f"{seed}|{call.seat_id}|{call.path}|{call.attempt}|{call.candidate}"
        return float(random.Random(key).randint(0, call.seat.effective_deadline_s + 5))
    return SyntheticSeats(elapsed=elapsed, fail=fail)


@pytest.mark.parametrize("seed", range(40))
def test_random_specs_killed_anywhere_resume_to_the_same_record(seed, tmp_path):
    raw = TreeGen(seed).spec()
    inputs = {**INPUTS, "items": [f"i{k}" for k in range(random.Random(seed).randint(0, 7))]}
    counter = Crashing(_replayable_seats(seed, raw), at=0)
    ref, _ = run(raw, tmp_path / "a", counter, store=FileStore(tmp_path / "a" / "s"),
                 inputs=inputs)
    if not counter.n:
        return
    at = random.Random(seed).randint(1, counter.n)
    with pytest.raises(_Crash):
        run(raw, tmp_path / "b", Crashing(_replayable_seats(seed, raw), at),
            store=FileStore(tmp_path / "b" / "s"), inputs=inputs)
    resumed, _ = run(raw, tmp_path / "b", _replayable_seats(seed, raw),
                     store=FileStore(tmp_path / "b" / "s"), inputs=inputs)
    assert _strip(resumed) == _strip(ref)
