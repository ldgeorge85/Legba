# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Static facts about a spec's flow tree, shared by the validator, the
worst-case calculator and the interpreter.

* every phase by id, with its chain of enclosing phases;
* every PLACEMENT of a seat — as a leaf, a panel judge or gate judge, a
  debate speaker or settler, or an escalation target — with its chain;
* the read graph: for each seat, the seats its reads can draw on (a phase
  reference expands to every seat placed inside the phase).

Pure; computed once per spec.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from .spec import (
    PHASE_TYPES,
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
    Seq,
    children_of,
)

Role = Literal["leaf", "judge", "gate_judge", "speaker", "settler", "escalate_to"]


@dataclass(frozen=True)
class Placement:
    seat: str
    role: Role
    chain: tuple[str, ...]  # enclosing phase ids, outermost first
    loc: str


@dataclass
class TreeIndex:
    phases: dict[str, Node] = field(default_factory=dict)
    phase_chain: dict[str, tuple[str, ...]] = field(default_factory=dict)  # excludes itself
    placements: dict[str, list[Placement]] = field(default_factory=dict)
    duplicate_ids: list[tuple[str, str]] = field(default_factory=list)  # (id, loc)

    def placement(self, seat: str) -> Placement | None:
        got = self.placements.get(seat)
        return got[0] if got else None

    def seats_in(self, phase_id: str) -> set[str]:
        """Every seat placed anywhere inside ``phase_id`` (rebuttal: the seats
        of the phase it rebuts, which it runs again)."""
        out: set[str] = set()
        for sid, places in self.placements.items():
            if any(phase_id in p.chain for p in places):
                out.add(sid)
        node = self.phases.get(phase_id)
        if isinstance(node, Rebuttal) and node.of in self.phases and node.of != phase_id:
            out |= self.seats_in(node.of)
        return out

    def chain_of(self, node_id: str) -> tuple[str, ...]:
        """Enclosing phases of a seat or phase (outermost first)."""
        if node_id in self.phase_chain:
            return self.phase_chain[node_id]
        p = self.placement(node_id)
        return p.chain if p else ()


def node_id(node: Node) -> str | None:
    return None if isinstance(node, SeatLeaf) else node.id


def build_index(spec: RoundSpec) -> TreeIndex:
    idx = TreeIndex()
    seen: set[str] = set()

    def place(seat: str, role: Role, chain: tuple[str, ...], loc: str) -> None:
        idx.placements.setdefault(seat, []).append(Placement(seat, role, chain, loc))

    def visit(node: Node, chain: tuple[str, ...]) -> None:
        if isinstance(node, SeatLeaf):
            place(node.seat, "leaf", chain, node.loc)
            return
        pid = node.id
        inner = chain
        if pid is not None:
            if pid in seen or pid in spec.seats:
                idx.duplicate_ids.append((pid, node.loc))
            seen.add(pid)
            idx.phases[pid] = node
            idx.phase_chain[pid] = chain
            inner = chain + (pid,)
        if isinstance(node, Panel):
            for j in node.gates_first:
                place(j, "gate_judge", inner, node.loc)
            for j in node.judges:
                place(j, "judge", inner, node.loc)
        elif isinstance(node, Debate):
            for s in node.seats:
                place(s, "speaker", inner, node.loc)
            if node.settle_by:
                place(node.settle_by, "settler", inner, node.loc)
        elif isinstance(node, Escalate):
            place(node.to, "escalate_to", inner, node.loc)
        for child in children_of(node):
            visit(child, inner)

    visit(spec.flow, ())
    return idx


def enclosing(idx: TreeIndex, chain: tuple[str, ...], kind: type) -> list[str]:
    return [p for p in chain if isinstance(idx.phases.get(p), kind)]


def blind_groups(idx: TreeIndex) -> list[tuple[str, list[set[str]]]]:
    """Each blind group as (phase id, [seat set per mutually-blind member]).

    A ``blind`` phase's members are its children; a ``panel``'s are its
    judges (and, separately, its gate judges); a ``rebuttal`` pass's are the
    seats it runs again; a blind ``map`` has ONE member (its body), because
    items of one body are kept apart by the store's item scope, not by the
    read graph."""
    out: list[tuple[str, list[set[str]]]] = []
    for pid, node in idx.phases.items():
        if isinstance(node, Blind):
            members = []
            for child in node.children:
                if isinstance(child, SeatLeaf):
                    members.append({child.seat})
                elif child.id is not None:
                    members.append(idx.seats_in(child.id))
            out.append((pid, members))
        elif isinstance(node, Panel):
            out.append((pid, [{j} for j in node.judges]))
            if node.gates_first:
                out.append((pid, [{j} for j in node.gates_first]))
        elif isinstance(node, Rebuttal):
            out.append((pid, [{s} for s in sorted(idx.seats_in(node.of))]))
    return out


def is_phase(node: object) -> bool:
    return isinstance(node, PHASE_TYPES)


__all__ = [
    "Placement", "TreeIndex", "blind_groups", "build_index", "enclosing", "is_phase", "node_id",
    "Blind", "Branch", "Loop", "Map", "Par", "Seq",
]
