# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The worst-case calculator: model calls, seconds and peak parallel calls a
spec can spend, computed from the tree (design §5.2 V9).

The numbers are UPPER BOUNDS on what the interpreter charges (a property test
holds it to them):

* a seat: ``(1 + retries)`` model calls (code seats: none, counted apart as
  code calls); ``(1 + retries) × deadline_s`` seconds; peak 1 (code 0);
* ``seq``: sums, peak = the largest child's;
* ``par`` / ``blind`` / ``map`` / ``panel`` / ``rebuttal``: calls sum; seconds
  are the serial sum capped by the phase deadline (a parallel schedule never
  takes longer than the serial one, and the deadline closes the phase); peak
  is the children's peaks summed, capped by ``max_parallel`` (and by a map's
  ``concurrency``);
* ``loop``: ``max_iterations`` bodies, calls capped by its ``model_calls``;
* ``debate``: ``max_turns`` turns plus one settle call per full cycle;
* ``branch``: the largest case; ``escalate``: the try plus the target once.

Budgets count calls and seconds only — never tokens (house rule).
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from .spec import (
    Blind,
    Branch,
    Debate,
    Escalate,
    Loop,
    Map,
    Node,
    Panel,
    Par,
    Rebuttal,
    RoundSpec,
    SeatLeaf,
    Select,
    Seq,
    UseRound,
)
from .tree import TreeIndex, build_index


@dataclass(frozen=True)
class Worst:
    calls: int = 0
    code_calls: int = 0
    seconds: int = 0
    peak: int = 0
    executions: int = 0  # seat executions (members a rebuttal would re-run)

    def as_dict(self) -> dict[str, int]:
        return {"calls": self.calls, "code_calls": self.code_calls, "seconds": self.seconds,
                "peak_parallel": self.peak}


def _seq(parts: list[Worst]) -> Worst:
    return Worst(sum(p.calls for p in parts), sum(p.code_calls for p in parts),
                 sum(p.seconds for p in parts), max((p.peak for p in parts), default=0),
                 sum(p.executions for p in parts))


def _par(parts: list[Worst], deadline: int | None, cap: int) -> Worst:
    serial = sum(p.seconds for p in parts)
    secs = min(serial, deadline) if deadline is not None else serial
    return Worst(sum(p.calls for p in parts), sum(p.code_calls for p in parts), secs,
                 min(cap, sum(p.peak for p in parts)), sum(p.executions for p in parts))


def _envelope(parts: list[Worst]) -> Worst:
    """The worst of each measure across alternatives (one member's worst
    calls and another's worst seconds may both be the bound)."""
    return Worst(max((p.calls for p in parts), default=0),
                 max((p.code_calls for p in parts), default=0),
                 max((p.seconds for p in parts), default=0),
                 max((p.peak for p in parts), default=0),
                 max((p.executions for p in parts), default=0))


class WorstCase:
    """Computes :class:`Worst` per node; ``by_phase`` keeps each phase's."""

    def __init__(self, spec: RoundSpec, index: TreeIndex | None = None):
        self.spec = spec
        self.idx = index or build_index(spec)
        self.cap = max(1, spec.budgets.max_parallel)
        self.by_phase: dict[str, Worst] = {}

    def seat(self, sid: str) -> Worst:
        seat = self.spec.seats.get(sid)
        if seat is None:
            return Worst()
        attempts = 1 + max(0, seat.retries)
        secs = attempts * seat.effective_deadline_s
        if seat.calls_model:
            return Worst(attempts, 0, secs, 1, 1)
        return Worst(0, attempts, secs, 0, 1)

    def total(self) -> Worst:
        return self.node(self.spec.flow)

    def node(self, node: Node) -> Worst:  # noqa: C901 - one case per shape
        w = self._node(node)
        if not isinstance(node, SeatLeaf) and node.id is not None:
            self.by_phase[node.id] = w
        return w

    def _node(self, node: Node) -> Worst:  # noqa: C901
        if isinstance(node, SeatLeaf):
            return self.seat(node.seat)
        if isinstance(node, Seq):
            return _seq([self.node(c) for c in node.children])
        if isinstance(node, (Par, Blind)):
            return _par([self.node(c) for c in node.children], node.deadline_s, self.cap)
        if isinstance(node, Rebuttal):
            members = self.by_phase.get(node.of) or Worst()
            seats = sorted(self.idx.seats_in(node.of))
            per_pass = [self.seat(s) for s in seats]
            n_exec = max(members.executions, len(seats))
            # every member execution of the rebutted phase writes once per pass
            one = _envelope(per_pass)
            pass_w = _par([one] * n_exec, None, self.cap)
            passes = max(1, node.passes or 1)
            secs = passes * pass_w.seconds
            if node.deadline_s is not None:
                secs = min(secs, node.deadline_s)
            return Worst(passes * pass_w.calls, passes * pass_w.code_calls, secs, pass_w.peak,
                         passes * pass_w.executions)
        if isinstance(node, Debate):
            turns = max(0, node.max_turns or 0)
            one = _envelope([self.seat(s) for s in node.seats])
            cycles = math.ceil(turns / max(1, len(node.seats)))
            settle = self.seat(node.settle_by) if node.settle_by else Worst()
            calls = turns * one.calls + cycles * settle.calls
            if node.model_calls is not None:
                calls = min(calls, node.model_calls)
            secs = turns * one.seconds + cycles * settle.seconds
            if node.deadline_s is not None:
                secs = min(secs, node.deadline_s)
            return Worst(calls, turns * one.code_calls + cycles * settle.code_calls, secs,
                         1 if calls else 0, turns + (cycles if node.settle_by else 0))
        if isinstance(node, Panel):
            mult = max(1, node.max_candidates or 1) if node.per == "candidate" else 1
            gates = [self.seat(j) for j in node.gates_first for _ in range(mult)]
            votes = [self.seat(j) for j in node.judges for _ in range(mult)]
            gw = _par(gates, None, self.cap)
            vw = _par(votes, None, self.cap)
            secs = gw.seconds + vw.seconds
            if node.deadline_s is not None:
                secs = min(secs, node.deadline_s)
            return Worst(gw.calls + vw.calls, gw.code_calls + vw.code_calls + 1, secs,
                         max(gw.peak, vw.peak), gw.executions + vw.executions)
        if isinstance(node, Select):
            return Worst(0, 1, 0, 0, 0)
        if isinstance(node, Branch):
            parts = [self.node(c.do) for c in node.cases]
            if node.default is not None:
                parts.append(self.node(node.default))
            return _envelope(parts)
        if isinstance(node, Loop):
            body = self.node(node.body)
            n = max(1, node.max_iterations or 1)
            calls = n * body.calls
            if node.model_calls is not None:
                calls = min(calls, node.model_calls)
            return Worst(calls, n * body.code_calls, n * body.seconds, body.peak,
                         n * body.executions)
        if isinstance(node, Map):
            body = self.node(node.body)
            n = max(1, node.max_items or 1)
            conc = max(1, min(node.concurrency or 1, self.cap))
            calls = n * body.calls
            if node.model_calls is not None:
                calls = min(calls, node.model_calls)
            secs = n * body.seconds
            if node.deadline_s is not None:
                secs = min(secs, node.deadline_s)
            peak = min(self.cap, min(conc, n) * body.peak)
            return Worst(calls, n * body.code_calls, secs, peak, n * body.executions)
        if isinstance(node, Escalate):
            return _seq([self.node(node.try_), self.seat(node.to)])
        if isinstance(node, UseRound):
            return Worst()
        raise TypeError(f"unknown node {node!r}")


def worst_case(spec: RoundSpec) -> Worst:
    return WorstCase(spec).total()
