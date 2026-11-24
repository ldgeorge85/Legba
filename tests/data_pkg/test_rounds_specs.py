# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Lane G-4: every spec in ``legba/data/rounds/specs/`` is validated and run.

Parametrised over the folder: a new spec dropped there is picked up with no
test edit. Each one must validate, run to its end with synthetic seats,
stay within its computed worst case, seal every blind member before anyone
reads it (and never hand a blind sibling's text to a seat), tally its panels,
and carry dissent through to the run record. Example run inputs come from
``specs/inputs/<id>.yaml`` when present, else neutral defaults.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from legba.data.rounds.interpreter import Interpreter
from legba.data.rounds.seats import Dispatch, FrozenClock, SeatCall, SeatReply, SyntheticSeats
from legba.data.rounds.spec import Panel, load_spec, load_yaml, walk
from legba.data.rounds.store import MemoryStore
from legba.data.rounds.validate import main as validate_main
from legba.data.rounds.validate import validate

SPECS = Path(__file__).resolve().parents[2] / "src" / "legba" / "data" / "rounds" / "specs"
SPEC_FILES = sorted(SPECS.glob("*.yaml"))
WORKED = {"stance_linear", "dialogues_mapped", "worldview_panel", "hypotheses_mapped",
          "options_contest"}


def _inputs(spec: Any) -> dict[str, Any]:
    f = SPECS / "inputs" / f"{spec.id}.yaml"
    if f.is_file():
        return load_yaml(f.read_text(encoding="utf-8"))
    defaults = {"subject_ref": "example-subject", "text": "Will it change within thirty days?",
                "timestamp": "2026-01-01T00:00:00Z", "id_list": ["one", "two", "three"]}
    return {k: defaults[d.type] for k, d in spec.inputs.items()}


def _dissenting(spec: Any) -> dict[str, str]:
    """The last voting judge of every panel votes 'falls'."""
    return {n.judges[-1]: "falls" for n in walk(spec.flow) if isinstance(n, Panel)}


def test_the_worked_specs_are_all_present():
    assert {p.stem for p in SPEC_FILES} >= WORKED


@pytest.mark.parametrize("path", SPEC_FILES, ids=lambda p: p.stem)
def test_spec_validates_runs_and_keeps_its_promises(path: Path):
    spec, refusals = load_spec(path)
    assert spec is not None, refusals
    report = validate(spec)
    assert report.ok, [str(r) for r in report.refusals]
    seen: dict[str, str] = {}
    synthetic = SyntheticSeats(votes=_dissenting(spec))

    def provider(call: SeatCall) -> SeatReply:
        seen[f"{call.seat_id}@{call.path}@{call.candidate}"] = json.dumps(
            [h.payload for h in call.inputs], default=str)
        return synthetic(call)

    it = Interpreter(spec, MemoryStore(), Dispatch(provider), inputs=_inputs(spec),
                     run_id=f"fixture-{spec.id}", clock=FrozenClock())
    rec = it.run()

    # terminates, within the computed worst case
    assert rec["run"]["state"] == "ended", rec["run"]
    w, t = report.worst, rec["totals"]
    assert t["model_calls"] <= w.calls <= spec.budgets.model_calls
    assert t["charged_s"] <= w.seconds <= spec.budgets.wall_s
    assert t["peak_parallel"] <= w.peak <= spec.budgets.max_parallel
    assert all(p["status"] == "closed" for p in rec["phases"])

    # blind members are sealed, and no seat saw a blind sibling's text
    blind = [p for p in rec["phases"] if p["shape"] in ("blind", "rebuttal_pass", "panel_stage")]
    for p in blind:
        assert p["members"] and all(m["sha256"] for m in p["members"] if m["status"] == "sealed")
        for a in p["members"]:
            for b in p["members"]:
                if a["seat"] != b["seat"] and a["status"] == "sealed":
                    mine = next(v for k, v in seen.items() if k.startswith(f"{a['seat']}@{p['path']}@"))
                    assert f"{b['seat']}@{p['path']}" not in mine

    # panels tally; dissent survives to the record
    panels = [n for n in walk(spec.flow) if isinstance(n, Panel)]
    assert len(rec["tallies"]) == len(panels)
    for tally in rec["tallies"]:
        assert tally["outcome"] in ("stands", "falls", "tie", "no_quorum", "mixed")
        for d in tally["dissent"]:
            assert {"kind": "vote", "phase": tally["phase"], **d} in rec["dissent"]
    assert rec["dissent"], "dissent must reach the run record"
    if any(len(n.judges) > 1 and n.tally.rule != "borda" for n in panels):
        assert any(d["kind"] == "vote" for d in rec["dissent"])

    # every seat's gates passed on synthetic, gate-clean replies
    assert all(r["pass_rate"] == 1.0 for r in rec["gate_rates"].values())


@pytest.mark.parametrize("path", SPEC_FILES, ids=lambda p: p.stem)
def test_spec_card_files_exist_and_drafts_say_so(path: Path):
    spec, _ = load_spec(path)
    for seat in spec.seats.values():
        if isinstance(seat.card, str):
            assert (SPECS / seat.card).is_file(), seat.card
    for draft in (SPECS / "cards" / "drafts").glob("*.md"):
        assert "DRAFT CARD" in draft.read_text(encoding="utf-8"), draft.name


def test_the_validator_cli_accepts_every_spec(capsys):
    assert validate_main([str(p) for p in SPEC_FILES]) == 0
    out = capsys.readouterr().out
    assert out.count("OK ") == len(SPEC_FILES)
