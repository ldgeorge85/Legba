# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared builders for the round tests: a small valid base spec (as a dict,
so a test can plant one violation) and a random bounded-tree generator for
the property tests. Domain-free on purpose."""
from __future__ import annotations

import copy
import random
from pathlib import Path
from typing import Any

PRIMARY = "llm.primary.openai_compat"
SECOND = "llm.verify.gateway_judge.openai_compat"

BASE_CARDS = ("reader.md", "judge.md", "editor.md")


def write_cards(root: Path, names: tuple[str, ...] = BASE_CARDS) -> Path:
    (root / "cards").mkdir(parents=True, exist_ok=True)
    for n in names:
        (root / "cards" / n).write_text(f"card {n}\n", encoding="utf-8")
    return root


def model(card: str = "cards/reader.md", **kw: Any) -> dict[str, Any]:
    seat = {"kind": "model", "card": card, "plane": PRIMARY, "thinking": "off",
            "reads": [{"from": "input.question"}, {"from": "freeze"}],
            "output": "position/1", "deadline_s": 60}
    seat.update(kw)
    return seat


def judge(**kw: Any) -> dict[str, Any]:
    seat = {"kind": "judge", "card": "cards/judge.md", "plane": SECOND,
            "family_rule": {"differs_from": ["a", "b", "c"]},
            "reads": [{"from": "table", "view": "sentence_table"}],
            "output": "verdict/1", "deadline_s": 60}
    seat.update(kw)
    return seat


def base_spec() -> dict[str, Any]:
    """freeze -> blind(a, b, c) -> table -> panel(j1, j2, j3) -> editor."""
    return {
        "id": "base_round",
        "version": 1,
        "inputs": {"subject": {"type": "subject_ref"}, "question": {"type": "text"},
                   "as_of": {"type": "timestamp"}},
        "seats": {
            "freeze": {"kind": "code", "fn": "scope_and_freeze",
                       "reads": [{"from": "input.subject"}, {"from": "input.as_of"}],
                       "output": "frozen_scope/1", "deadline_s": 10},
            "a": model(gates=["handed_rows", "quote_in_source"]),
            "b": {"seat_ref": "a"},
            "c": {"seat_ref": "a"},
            "table": {"kind": "code", "fn": "sentence_table",
                      "reads": [{"from": "open.sealed"}], "output": "sentence_table/1",
                      "deadline_s": 10},
            "j1": judge(),
            "j2": {"seat_ref": "j1"},
            "j3": {"seat_ref": "j1"},
            "editor": model(card="cards/editor.md", output="read/1",
                            reads=[{"from": "judge.tally"},
                                   {"from": "table", "view": "sentence_table"}],
                            gates=["every_sentence_maps_to_rows"]),
        },
        "flow": {"seq": [
            {"seat": "freeze"},
            {"blind": [{"seat": "a"}, {"seat": "b"}, {"seat": "c"}], "id": "open",
             "deadline_s": 120, "min_present": 2},
            {"seat": "table"},
            {"panel": {"judges": ["j1", "j2", "j3"], "on": "table", "tally": "majority"},
             "id": "judge", "deadline_s": 120},
            {"seat": "editor"},
        ], "id": "main"},
        "budgets": {"wall_s": 900, "model_calls": 20, "max_parallel": 4},
        "publishes": "none",
    }


def mutate(spec: dict[str, Any]) -> dict[str, Any]:
    return copy.deepcopy(spec)


# ---------------------------------------------------------------------------
# Random bounded trees (property tests)
# ---------------------------------------------------------------------------


class TreeGen:
    """Random flow trees over model / code seats that read only inputs and the
    frozen scope, so any tree it builds is a structurally valid spec. Every
    shape the interpreter runs appears; caps are respected."""

    def __init__(self, seed: int):
        self.rng = random.Random(seed)
        self.seats: dict[str, dict[str, Any]] = {}
        self.n = 0
        self.ids = 0

    def _seat(self, *, judge_seat: bool = False) -> str:
        self.n += 1
        sid = f"s{self.n}"
        if judge_seat:
            self.seats[sid] = judge(reads=[], family_rule={"differs_from": ["freeze"]},
                                    deadline_s=self.rng.randint(5, 60),
                                    retries=self.rng.randint(0, 1))
            self.seats[sid]["reads"] = [{"from": "input.question", "view": "sentence_table"}]
        else:
            self.seats[sid] = model(deadline_s=self.rng.randint(5, 60),
                                    retries=self.rng.randint(0, 2),
                                    reads=[{"from": "input.question"}, {"from": "freeze"}])
        return sid

    def _pid(self) -> str:
        self.ids += 1
        return f"p{self.ids}"

    def node(self, depth: int) -> dict[str, Any]:
        r = self.rng.random()
        if depth <= 0 or r < 0.3:
            return {"seat": self._seat()}
        kind = self.rng.choice(["seq", "par", "blind", "loop", "map", "branch", "escalate",
                                "panel", "debate"])
        pid = self._pid()
        if kind in ("seq", "par"):
            kids = [self.node(depth - 1) for _ in range(self.rng.randint(1, 3))]
            out: dict[str, Any] = {kind: kids, "id": pid}
            if kind == "par":
                out["deadline_s"] = self.rng.randint(30, 400)
                out["join"] = self.rng.choice(["all", "first", {"quorum": 1}])
            return out
        if kind == "blind":
            kids = [{"seat": self._seat()} for _ in range(self.rng.randint(1, 4))]
            return {"blind": kids, "id": pid, "deadline_s": self.rng.randint(30, 400),
                    "min_present": self.rng.randint(0, len(kids))}
        if kind == "loop":
            return {"loop": {"body": self.node(depth - 1),
                             "max_iterations": self.rng.randint(1, 4),
                             "model_calls": self.rng.randint(1, 10)}, "id": pid}
        if kind == "map":
            return {"map": {"over": "input.items", "as": "it", "body": self.node(depth - 1),
                            "max_items": self.rng.randint(1, 5),
                            "concurrency": self.rng.randint(1, 4)},
                    "id": pid, "deadline_s": self.rng.randint(30, 600),
                    "blind": self.rng.random() < 0.5}
        if kind == "branch":
            return {"branch": {"cases": [{"when": "budget.remaining.calls >= 3",
                                          "do": self.node(depth - 1)}],
                               "default": self.node(depth - 1)}, "id": pid}
        if kind == "escalate":
            try_seat = self._seat()
            return {"escalate": {"try": {"seat": try_seat}, "to": self._seat(),
                                 "when": f"not gate.{try_seat}.passed"}, "id": pid}
        if kind == "panel":
            n = self.rng.choice([1, 3])
            judges = [self._seat(judge_seat=True) for _ in range(n)]
            return {"panel": {"judges": judges, "on": "freeze", "tally": "majority"},
                    "id": pid, "deadline_s": self.rng.randint(30, 300)}
        speakers = [self._seat(), self._seat()]
        for s in speakers:
            self.seats[s]["output"] = "turn/1"
        return {"debate": {"seats": speakers, "max_turns": self.rng.randint(1, 5)}, "id": pid}

    def spec(self) -> dict[str, Any]:
        flow = {"seq": [{"seat": "freeze"}, self.node(3)], "id": "main"}
        seats = {"freeze": {"kind": "code", "fn": "scope_and_freeze",
                            "reads": [{"from": "input.subject"}], "output": "frozen_scope/1",
                            "deadline_s": 5}, **self.seats}
        return {
            "id": "random_round", "version": 1,
            "inputs": {"subject": {"type": "subject_ref"}, "question": {"type": "text"},
                       "as_of": {"type": "timestamp"}, "items": {"type": "id_list"}},
            "seats": seats, "flow": flow,
            "budgets": {"wall_s": 7200, "model_calls": 400,
                        "max_parallel": self.rng.randint(1, 6)},
            "publishes": "none",
        }


def rich_spec() -> dict[str, Any]:
    """Most shapes in one spec: blind, rebuttal (two passes), panel with a
    gate judge beside a non-voting judge in a par, a map over generated
    hypotheses whose body is a bounded loop, an escalation, a branch."""
    s = base_spec()
    s["id"] = "rich_round"
    seats = s["seats"]
    seats["table"]["reads"] = [{"from": "rebut.sealed"}]
    seats["g1"] = judge()
    seats["weakest"] = judge(output="weakest_link/1")
    seats["generator"] = model(output="hypothesis_set/1")
    seats["seeker"] = model(output="evidence_rows/1", gates=["handed_rows", "quote_in_source"],
                            reads=[{"from": "item.h"}, {"from": "hunt.sealed"}, {"from": "freeze"}])
    seats["assessor"] = model(output="assessment/1",
                              reads=[{"from": "item.h"}, {"from": "seeker"}])
    seats["matrix"] = {"kind": "code", "fn": "consistency_matrix",
                       "reads": [{"from": "per_h.sealed"}], "output": "matrix/1", "deadline_s": 10}
    seats["adjudicator"] = model(output="ruling/1", gates=["handed_rows"],
                                 reads=[{"from": "matrix"}, {"from": "per_h.sealed"}])
    seats["deep"] = {"seat_ref": "adjudicator", "thinking": "on"}
    seats["tiebreak"] = model(output="ruling/1", reads=[{"from": "judge.tally"},
                                                         {"from": "table", "view": "sentence_table"}])
    seats["confirm"] = {"seat_ref": "tiebreak"}
    seats["editor"]["reads"] = [{"from": "judge.tally"}, {"from": "table", "view": "sentence_table"},
                                {"from": "rule"}, {"from": "close"}]
    s["flow"] = {"seq": [
        {"seat": "freeze"},
        {"blind": [{"seat": "a"}, {"seat": "b"}, {"seat": "c"}], "id": "open",
         "deadline_s": 120, "min_present": 2},
        {"rebuttal": {"of": "open", "passes": 2}, "id": "rebut", "deadline_s": 300},
        {"seat": "table"},
        {"par": [{"panel": {"judges": ["j1", "j2", "j3"], "on": "table", "tally": "majority",
                            "gates_first": ["g1"]}, "id": "judge", "deadline_s": 200},
                 {"seat": "weakest"}], "id": "judging", "join": "all", "deadline_s": 240},
        {"seat": "generator"},
        {"map": {"over": "generator.hypotheses", "as": "h", "max_items": 4, "concurrency": 2,
                 "body": {"loop": {"body": {"seq": [{"seat": "seeker"}, {"seat": "assessor"}],
                                            "id": "pass"},
                                   "until": "count(pass.assessment/1 where diagnostic == true) >= 1",
                                   "max_iterations": 3, "model_calls": 6, "id": "hunt"}}},
         "id": "per_h", "deadline_s": 1200},
        {"seat": "matrix"},
        {"escalate": {"try": {"seat": "adjudicator"}, "to": "deep",
                      "when": "count(matrix.matrix/1 where top_two_separated == false) >= 1"},
         "id": "rule"},
        {"branch": {"cases": [{"when": "tally.judge.outcome == tie", "do": {"seat": "tiebreak"}}],
                    "default": {"seat": "confirm"}}, "id": "close"},
        {"seat": "editor"},
    ], "id": "main"}
    s["budgets"] = {"wall_s": 4000, "model_calls": 60, "max_parallel": 4}
    return s


INPUTS = {"subject": "subject-x", "question": "Will it happen?", "as_of": "2026-01-01T00:00:00Z",
          "items": ["one", "two", "three"]}
