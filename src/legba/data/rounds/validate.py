# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The round-spec validator: refusal rules V1-V12.

Every refusal names its rule, a stable reason and the path in the spec:

====  ===========================================  ====================================
Rule  Refused                                      Reason names
====  ===========================================  ====================================
V1    a shape without its bound; a bound above     ``bound_missing``, ``bound_above_cap``,
      the caps; a branch without a default         ``bound_invalid``, ``branch_no_default``
V2    a spec that cannot be shown to end by its    ``cannot_terminate``
      own rule (an unreachable join / quorum /
      min_present, a seat_ref cycle)
V3    a blindness leak, directly or through an     ``blindness_leak``, ``withheld_reachable``
      earlier seat's output; a withheld field
      reachable through another seat
V4    a read / predicate / phase reference to      ``read_not_available``
      something not produced before it on every
      path
V5    a judge on the same family as a seat it      ``judge_same_family``,
      judges when the spec declares another        ``judge_family_rule_missing``
      family available
V6    a judge reading prose / anything but a       ``judge_reads_prose``,
      sentence table / another judge; a judge      ``judge_view_not_sentence_table``,
      writing a non-verdict row                    ``judge_reads_judge``, ``judge_output_not_verdict``
V7    an even panel; a quorum it cannot reach      ``panel_even``, ``quorum_unreachable``,
                                                   ``panel_member_not_judge``
V8    an unknown card, pack, row type, gate,       ``unknown_reference``, ``code_output_mismatch``
      function, plane, seat or phase
V9    budgets that cannot cover the worst case;    ``budget_below_worst_case``,
      budgets counted in tokens                    ``token_budget_refused``
V10   ``publishes: reader`` on a type not allowed  ``publishes_reader_not_allowed``
V11   shapes declared but not built (use_round,    ``shape_not_built``
      human seats, shared seat files,
      multi-stage select, prior-run reads)
V12   a seat whose tools could read this run's     ``tool_not_run_cut``
      rows or rows after the as-of time
====  ===========================================  ====================================

Structural errors are rule ``schema`` (``schema_invalid``, ``phase_id_missing``,
``predicate_invalid``, ``reference_invalid``, ``duplicate_id``,
``seat_placed_twice``, ``seat_kind_invalid``, ``rebuttal_of_invalid``).

CLI: ``python -m legba.data.rounds.validate SPEC [SPEC…] [--json]`` prints the
refusals (exit 1) or the worst case (exit 0).
"""
from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from .budget import Worst, WorstCase
from .catalog import Catalog, default_catalog
from .refs import Predicate, Ref, parse_ref
from .rows import JUDGE_ROW_TYPES, row_fields
from .spec import (
    CAP_DEBATE_TURNS,
    CAP_LOOP_ITERATIONS,
    CAP_MAP_CONCURRENCY,
    CAP_MAP_ITEMS,
    CAP_MAX_PARALLEL,
    CAP_MODEL_CALLS,
    CAP_PHASE_DEADLINE_S,
    CAP_REBUTTAL_PASSES,
    CAP_RETRIES,
    CAP_SEAT_DEADLINE_S,
    CAP_WALL_S,
    Blind,
    Branch,
    CardFromItem,
    Debate,
    Escalate,
    Loop,
    Map,
    Node,
    Panel,
    Par,
    ReadSpec,
    Rebuttal,
    Refusal,
    RoundSpec,
    SeatLeaf,
    Select,
    Seq,
    SpecRefused,
    UseRound,
    load_spec,
    walk,
)
from .tree import TreeIndex, blind_groups, build_index

#: Above this many candidates a per-candidate panel is refused (V1 cap).
CAP_PANEL_CANDIDATES = 12


@dataclass
class Report:
    """The validator's answer for one spec."""

    spec: RoundSpec | None
    refusals: list[Refusal] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    worst: Worst | None = None
    worst_by_phase: dict[str, Worst] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.refusals and self.spec is not None

    def reasons(self) -> set[str]:
        return {r.reason for r in self.refusals}

    def as_dict(self) -> dict[str, object]:
        return {
            "spec": self.spec.label if self.spec else None,
            "sha256": self.spec.sha256 if self.spec else None,
            "ok": self.ok,
            "refusals": [r.as_dict() for r in self.refusals],
            "warnings": list(self.warnings),
            "worst_case": self.worst.as_dict() if self.worst else None,
            "worst_case_by_phase": {k: v.as_dict() for k, v in self.worst_by_phase.items()},
        }


class _Validator:
    def __init__(self, spec: RoundSpec, catalog: Catalog, card_root: Path | None):
        self.spec = spec
        self.cat = catalog
        self.card_root = card_root
        self.idx: TreeIndex = build_index(spec)
        self.refusals: list[Refusal] = []
        self.warnings: list[str] = []

    def refuse(self, rule: str, reason: str, path: str, message: str) -> None:
        self.refusals.append(Refusal(rule, reason, path, message))

    # -- entry --------------------------------------------------------------

    def run(self) -> Report:
        self.check_ids()
        self.check_seats()
        self.check_shapes()
        self.check_flow_reads()
        self.check_blindness()
        self.check_withheld()
        self.check_judges()
        self.check_publish()
        worst: Worst | None = None
        by_phase: dict[str, Worst] = {}
        if not any(r.rule in ("schema", "V8") for r in self.refusals):
            wc = WorstCase(self.spec, self.idx)
            worst = wc.total()
            by_phase = dict(wc.by_phase)
            self.check_budget(worst)
        return Report(self.spec, self.refusals, self.warnings, worst, by_phase)

    # -- ids and placements -------------------------------------------------

    def check_ids(self) -> None:
        for pid, loc in self.idx.duplicate_ids:
            self.refuse("schema", "duplicate_id", loc, f"id {pid!r} is used twice")
        for sid, places in self.idx.placements.items():
            if sid not in self.spec.seats:
                for p in places:
                    self.refuse("V8", "unknown_reference", p.loc, f"seat {sid!r} is not declared")
                continue
            if len(places) > 1:
                self.refuse("schema", "seat_placed_twice", places[1].loc,
                            f"seat {sid!r} is placed {len(places)} times; use seat_ref for a copy")
        for sid in self.spec.seats:
            if sid not in self.idx.placements:
                self.warnings.append(f"seat {sid!r} is declared but never placed in the flow")

    # -- seats ----------------------------------------------------------------

    def check_seats(self) -> None:  # noqa: C901 - one rule per field
        for sid, seat in self.spec.seats.items():
            path = f"seats.{sid}"
            if seat.kind == "human":
                self.refuse("V11", "shape_not_built", path,
                            "human seats are declared but not available yet (SEAMS #92)")
                continue
            if seat.kind in ("model", "judge"):
                if seat.card is None:
                    self.refuse("schema", "seat_kind_invalid", path, f"a {seat.kind} seat needs a card")
                if seat.plane is None:
                    self.refuse("schema", "seat_kind_invalid", path, f"a {seat.kind} seat needs a plane")
                elif seat.plane not in self.cat.planes:
                    self.refuse("V8", "unknown_reference", f"{path}.plane",
                                f"plane {seat.plane!r} is not registered")
                if seat.fn is not None:
                    self.refuse("schema", "seat_kind_invalid", path, "fn belongs to code seats")
                self.check_card(sid, seat.card, path)
            if seat.kind == "code":
                if seat.fn is None:
                    self.refuse("schema", "seat_kind_invalid", path, "a code seat needs fn")
                elif seat.fn not in self.cat.code_fns:
                    self.refuse("V8", "unknown_reference", f"{path}.fn",
                                f"function {seat.fn!r} is not registered")
                elif self.cat.code_fns[seat.fn].output != seat.output:
                    self.refuse("V8", "code_output_mismatch", f"{path}.output",
                                f"{seat.fn} writes {self.cat.code_fns[seat.fn].output}, "
                                f"not {seat.output}")
                for name in ("card", "plane", "thinking"):
                    if getattr(seat, name) is not None:
                        self.refuse("schema", "seat_kind_invalid", f"{path}.{name}",
                                    f"{name} belongs to model and judge seats")
                if seat.tools:
                    self.refuse("schema", "seat_kind_invalid", f"{path}.tools",
                                "code seats have no tools")
            if seat.output not in self.cat.row_types:
                self.refuse("V8", "unknown_reference", f"{path}.output",
                            f"row type {seat.output!r} is not registered")
            for g in seat.gates:
                if g not in self.cat.gates:
                    self.refuse("V8", "unknown_reference", f"{path}.gates",
                                f"gate {g!r} is not registered")
            for t in seat.tools:
                if t not in self.cat.packs:
                    self.refuse("V8", "unknown_reference", f"{path}.tools",
                                f"pack {t!r} is not registered")
                elif not self.cat.packs[t]:
                    self.refuse("V12", "tool_not_run_cut", f"{path}.tools",
                                f"pack {t!r} cannot be cut to the run's as-of time and "
                                "could read this run's rows")
            if seat.deadline_s is not None and not 1 <= seat.deadline_s <= CAP_SEAT_DEADLINE_S:
                self.refuse("V1", "bound_above_cap" if seat.deadline_s > 0 else "bound_invalid",
                            f"{path}.deadline_s", f"seat deadline_s is 1..{CAP_SEAT_DEADLINE_S}")
            if not 0 <= seat.retries <= CAP_RETRIES:
                self.refuse("V1", "bound_above_cap" if seat.retries > 0 else "bound_invalid",
                            f"{path}.retries", f"retries is 0..{CAP_RETRIES}")
            if seat.repair == "reasons":
                if not seat.calls_model:
                    self.refuse("schema", "seat_kind_invalid", f"{path}.repair",
                                "repair belongs to model and judge seats")
                elif seat.retries < 1:
                    self.refuse("V1", "bound_invalid", f"{path}.repair",
                                "repair: reasons is a retry: it needs retries >= 1")
            for d in seat.differs_from:
                if d not in self.spec.seats:
                    self.refuse("V8", "unknown_reference", f"{path}.family_rule",
                                f"differs_from names {d!r}, not a seat here")
            if seat.on_failure == "escalate":
                place = self.idx.placement(sid)
                chain = place.chain if place else ()
                if not any(isinstance(self.idx.phases.get(p), Escalate) for p in chain):
                    self.refuse("schema", "schema_invalid", f"{path}.on_failure",
                                "on_failure: escalate needs the seat inside an escalate's try")

    def check_card(self, sid: str, card: object, path: str) -> None:
        if isinstance(card, CardFromItem):
            place = self.idx.placement(sid)
            maps = [self.idx.phases[p] for p in (place.chain if place else ())
                    if isinstance(self.idx.phases.get(p), Map)]
            if not any(m.as_ == card.from_item for m in maps):  # type: ignore[union-attr]
                self.refuse("V4", "read_not_available", f"{path}.card",
                            f"card from item {card.from_item!r}: no enclosing map has that item")
            return
        if isinstance(card, str) and self.card_root is not None:
            if not (self.card_root / card).is_file():
                self.refuse("V8", "unknown_reference", f"{path}.card", f"card {card!r} not found")

    # -- shapes and bounds ---------------------------------------------------

    def _bound(self, value: int | None, name: str, node: Node, cap: int | None,
               required: bool = True) -> None:
        loc = f"{node.loc}.{name}"
        label = f"{type(node).__name__.lower()} `{getattr(node, 'id', None)}`"
        if value is None:
            if required:
                self.refuse("V1", "bound_missing", loc, f"{label} has no {name}")
            return
        if value < 1:
            self.refuse("V1", "bound_invalid", loc, f"{label}: {name} must be >= 1")
        elif cap is not None and value > cap:
            self.refuse("V1", "bound_above_cap", loc, f"{label}: {name} {value} > cap {cap}")

    def check_shapes(self) -> None:  # noqa: C901 - one case per shape
        b = self.spec.budgets
        for name, value, cap in (("wall_s", b.wall_s, CAP_WALL_S),
                                 ("model_calls", b.model_calls, CAP_MODEL_CALLS),
                                 ("max_parallel", b.max_parallel, CAP_MAX_PARALLEL)):
            if value < 1:
                self.refuse("V1", "bound_invalid", f"budgets.{name}", f"{name} must be >= 1")
            elif value > cap:
                self.refuse("V1", "bound_above_cap", f"budgets.{name}", f"{name} {value} > cap {cap}")
        for node in walk(self.spec.flow):
            if isinstance(node, (Par, Blind)):
                self._bound(node.deadline_s, "deadline_s", node, CAP_PHASE_DEADLINE_S)
            if isinstance(node, Par) and node.join.mode == "quorum":
                if not node.join.k or node.join.k > len(node.children):
                    self.refuse("V2", "cannot_terminate", f"{node.loc}.join",
                                f"par `{node.id}` waits for {node.join.k} of "
                                f"{len(node.children)} children")
            if isinstance(node, Blind):
                if node.min_present is None:
                    self.refuse("V1", "bound_missing", f"{node.loc}.min_present",
                                f"blind `{node.id}` has no min_present")
                else:
                    if node.min_present < 0:
                        self.refuse("V1", "bound_invalid", f"{node.loc}.min_present",
                                    "min_present is >= 0")
                    elif node.min_present > len(node.children):
                        self.refuse("V2", "cannot_terminate", f"{node.loc}.min_present",
                                    f"blind `{node.id}` needs {node.min_present} of "
                                    f"{len(node.children)} children present")
            elif isinstance(node, Rebuttal):
                self._bound(node.passes, "passes", node, CAP_REBUTTAL_PASSES)
                self._bound(node.deadline_s, "deadline_s", node, CAP_PHASE_DEADLINE_S, False)
                target = self.idx.phases.get(node.of)
                ok = isinstance(target, Blind) and all(isinstance(c, SeatLeaf)
                                                       for c in target.children)
                ok = ok or (isinstance(target, Map) and target.blind
                            and isinstance(target.body, SeatLeaf))
                if node.of not in self.idx.phases:
                    self.refuse("V8", "unknown_reference", f"{node.loc}.rebuttal.of",
                                f"phase {node.of!r} does not exist")
                elif not ok:
                    self.refuse("schema", "rebuttal_of_invalid", f"{node.loc}.rebuttal.of",
                                "a rebuttal rebuts a blind phase of seats, or a blind map "
                                "whose body is one seat")
            elif isinstance(node, Debate):
                self._bound(node.max_turns, "max_turns", node, CAP_DEBATE_TURNS)
                self._bound(node.model_calls, "model_calls", node, CAP_MODEL_CALLS, False)
                self._bound(node.deadline_s, "deadline_s", node, CAP_PHASE_DEADLINE_S, False)
                if len(node.seats) < 2:
                    self.refuse("V2", "cannot_terminate", f"{node.loc}.debate.seats",
                                "a debate needs at least two speakers")
                if node.settle_by and node.settle_by in node.seats:
                    self.refuse("V2", "cannot_terminate", f"{node.loc}.debate.settle_by",
                                "the settling seat may not also speak")
            elif isinstance(node, Panel):
                self.check_panel(node)
            elif isinstance(node, Select):
                if node.stages > 1:
                    self.refuse("V11", "shape_not_built", f"{node.loc}.select.stages",
                                "multi-stage selection is declared but not built (SEAMS #92)")
                if node.keep_top is not None:
                    self._bound(node.keep_top, "keep.top", node, CAP_MAP_ITEMS)
                if node.from_ not in self.idx.phases and node.from_ not in self.spec.seats:
                    self.refuse("V8", "unknown_reference", f"{node.loc}.select.from",
                                f"{node.from_!r} is not a phase or seat")
                by = self.idx.phases.get(node.by)
                if not isinstance(by, Panel) and node.by not in self.cat.score_fns:
                    self.refuse("V8", "unknown_reference", f"{node.loc}.select.by",
                                f"{node.by!r} is neither a panel nor a registered score fn")
            elif isinstance(node, Branch):
                if node.default is None:
                    self.refuse("V1", "branch_no_default", f"{node.loc}.branch",
                                f"branch `{node.id}` has no default case")
            elif isinstance(node, Loop):
                self._bound(node.max_iterations, "max_iterations", node, CAP_LOOP_ITERATIONS)
                self._bound(node.model_calls, "model_calls", node, CAP_MODEL_CALLS)
            elif isinstance(node, Map):
                self._bound(node.max_items, "max_items", node, CAP_MAP_ITEMS)
                self._bound(node.concurrency, "concurrency", node, CAP_MAP_CONCURRENCY)
                self._bound(node.deadline_s, "deadline_s", node, CAP_PHASE_DEADLINE_S)
                self._bound(node.model_calls, "model_calls", node, CAP_MODEL_CALLS, False)
            elif isinstance(node, Escalate):
                to = self.spec.seats.get(node.to)
                if to is not None and to.on_failure == "escalate":
                    self.refuse("V2", "cannot_terminate", f"{node.loc}.escalate.to",
                                "the escalation target may not itself escalate")
            elif isinstance(node, UseRound):
                self.refuse("V11", "shape_not_built", f"{node.loc}.use_round",
                            "use_round is not available yet (SEAMS #92); use map with an "
                            "inline body")
        if self.spec.prior_runs != "none":
            self.refuse("V11", "shape_not_built", "prior_runs",
                        "reading earlier runs' outputs is not available yet (SEAMS #92)")

    def check_panel(self, node: Panel) -> None:
        loc = f"{node.loc}.panel"
        self._bound(node.deadline_s, "deadline_s", node, CAP_PHASE_DEADLINE_S)
        n = len(node.judges)
        if n == 0 or n % 2 == 0:
            self.refuse("V7", "panel_even", f"{loc}.judges",
                        f"panel `{node.id}` has {n} voting judges; it needs an odd number")
        t = node.tally
        if t.rule == "quorum":
            if t.of != n or not t.k or t.k > n or 2 * t.k <= n:
                self.refuse("V7", "quorum_unreachable", f"{loc}.tally",
                            f"quorum {t.k} of {t.of} with {n} judges: of must equal the judge "
                            "count and k must be a strict majority of it")
            if t.threshold is not None and not 0.0 < t.threshold <= 1.0:
                self.refuse("V1", "bound_invalid", f"{loc}.tally", "threshold is in (0, 1]")
        if node.per == "candidate":
            self._bound(node.max_candidates, "max_candidates", node, CAP_PANEL_CANDIDATES)
        for j in node.judges + node.gates_first:
            seat = self.spec.seats.get(j)
            if seat is not None and seat.kind != "judge":
                self.refuse("V7", "panel_member_not_judge", f"{loc}.judges",
                            f"{j!r} sits on a panel but is a {seat.kind} seat")
        if node.on not in self.idx.phases and node.on not in self.spec.seats:
            self.refuse("V8", "unknown_reference", f"{loc}.on", f"{node.on!r} does not exist")

    # -- reads (V4) ----------------------------------------------------------

    def check_flow_reads(self) -> None:
        self._done: set[str] = set()
        self._flow(self.spec.flow, (), frozenset())

    def _ref_ok(self, ref: Ref, chain: tuple[str, ...], items: frozenset[str],
                path: str, reader: str | None) -> None:
        tgt = ref.target
        if ref.kind == "input":
            if tgt not in self.spec.inputs:
                self.refuse("V8", "unknown_reference", path, f"input {tgt!r} is not declared")
            return
        if ref.kind == "item":
            if tgt not in items:
                self.refuse("V4", "read_not_available", path,
                            f"item.{tgt}: no enclosing map names its item {tgt!r}")
            return
        known = tgt in self.spec.seats or tgt in self.idx.phases
        if not known:
            self.refuse("V8", "unknown_reference", path, f"{tgt!r} is not a seat or phase")
            return
        if ref.kind == "tally" and not isinstance(self.idx.phases.get(tgt), Panel):
            self.refuse("V8", "unknown_reference", path, f"{tgt!r} is not a panel; it has no tally")
            return
        if ref.kind == "field":
            seat = self.spec.seats.get(tgt)
            if seat is None or ref.field not in row_fields(seat.output):
                self.refuse("V8", "unknown_reference", path,
                            f"{ref.raw!r}: no such field on {tgt!r}'s output")
                return
        if tgt in self._done:
            return
        if tgt in chain:
            node = self.idx.phases.get(tgt)
            if isinstance(node, (Loop, Debate)) and ref.kind in ("sealed", "others"):
                return  # earlier iterations of the loop / the debate transcript so far
            if isinstance(node, (Blind, Map, Panel, Rebuttal)):
                self.refuse("V3", "blindness_leak", path,
                            f"{reader!r} is inside `{tgt}` and reads it before it closes")
                return
        self.refuse("V4", "read_not_available", path,
                    f"{reader or 'this'} reads {ref.raw!r}, which is not produced before it "
                    "on every path")

    def _seat_reads(self, sid: str, chain: tuple[str, ...], items: frozenset[str],
                    extra: Iterable[ReadSpec] = ()) -> None:
        seat = self.spec.seats.get(sid)
        if seat is None:
            return
        for i, r in enumerate(tuple(seat.reads) + tuple(extra)):
            try:
                ref = r.ref
            except ValueError as exc:
                self.refuse("schema", "reference_invalid", f"seats.{sid}.reads[{i}]", str(exc))
                continue
            self._ref_ok(ref, chain, items, f"seats.{sid}.reads[{i}]", sid)

    def _pred_ok(self, pred: Predicate, chain: tuple[str, ...], path: str) -> None:
        if pred.kind == "budget":
            return
        tgt = pred.target or ""
        if pred.kind == "gate":
            seat = self.spec.seats.get(tgt)
            if seat is None:
                self.refuse("V8", "unknown_reference", path, f"gate predicate on unknown seat {tgt!r}")
            elif pred.gate is not None and pred.gate not in seat.gates:
                self.refuse("V8", "unknown_reference", path,
                            f"seat {tgt!r} does not run gate {pred.gate!r}")
        elif pred.kind == "tally" and not isinstance(self.idx.phases.get(tgt), Panel):
            self.refuse("V8", "unknown_reference", path, f"{tgt!r} is not a panel")
            return
        elif pred.kind in ("unchanged", "phase_status") and tgt not in self.idx.phases:
            self.refuse("V8", "unknown_reference", path, f"{tgt!r} is not a phase")
            return
        elif pred.kind == "count":
            seats = self.idx.seats_in(tgt) if tgt in self.idx.phases else {tgt}
            types = {self.spec.seats[s].output for s in seats if s in self.spec.seats}
            if tgt not in self.idx.phases and tgt not in self.spec.seats:
                self.refuse("V8", "unknown_reference", path, f"{tgt!r} is not a seat or phase")
                return
            if pred.row_type not in types:
                self.refuse("V8", "unknown_reference", path,
                            f"no seat in {tgt!r} writes {pred.row_type}")
            elif pred.field is not None and pred.field not in row_fields(pred.row_type or ""):
                self.refuse("V8", "unknown_reference", path,
                            f"{pred.row_type} has no field {pred.field!r}")
        if tgt in self._done or tgt in chain:
            return
        if tgt in self.spec.seats or tgt in self.idx.phases:
            self.refuse("V4", "read_not_available", path,
                        f"predicate reads {tgt!r} before it is produced on every path")

    def _flow(self, node: Node, chain: tuple[str, ...], items: frozenset[str]) -> None:  # noqa: C901
        done = self._done
        if isinstance(node, SeatLeaf):
            self._seat_reads(node.seat, chain, items)
            done.add(node.seat)
            return
        pid = node.id or ""
        inner = chain + (pid,)
        if isinstance(node, Seq):
            for c in node.children:
                self._flow(c, inner, items)
        elif isinstance(node, (Par, Blind)):
            start = set(done)
            after: set[str] = set(done)
            for c in node.children:
                self._done = set(start)
                self._flow(c, inner, items)
                after |= self._done
            self._done = after
        elif isinstance(node, Rebuttal):
            if node.of not in done:
                self.refuse("V4", "read_not_available", f"{node.loc}.rebuttal.of",
                            f"rebuttal of {node.of!r} before it has closed")
            for i, r in enumerate(node.also):
                self._ref_ok(r.ref, inner, items, f"{node.loc}.rebuttal.also[{i}]", pid)
        elif isinstance(node, Debate):
            for s in node.seats + ((node.settle_by,) if node.settle_by else ()):
                self._seat_reads(s, inner, items)
            done.update(node.seats)
            if node.settle_by:
                done.add(node.settle_by)
        elif isinstance(node, Panel):
            if node.on not in done:
                self.refuse("V4", "read_not_available", f"{node.loc}.panel.on",
                            f"panel `{pid}` rules on {node.on!r} before it is produced")
            for j in node.gates_first + node.judges:
                self._seat_reads(j, inner, items)
            done.update(node.gates_first + node.judges)
        elif isinstance(node, Select):
            for what, ref in (("from", node.from_), ("by", node.by)):
                if (ref in self.idx.phases or ref in self.spec.seats) and ref not in done:
                    self.refuse("V4", "read_not_available", f"{node.loc}.select.{what}",
                                f"select `{pid}` reads {ref!r} before it is produced")
        elif isinstance(node, Branch):
            start = set(done)
            outs: list[set[str]] = []
            for i, case in enumerate(node.cases):
                self._done = set(start)
                self._pred_ok(case.when, inner, f"{node.loc}.branch.cases[{i}].when")
                self._flow(case.do, inner, items)
                outs.append(self._done)
            if node.default is not None:
                self._done = set(start)
                self._flow(node.default, inner, items)
                outs.append(self._done)
            self._done = set.intersection(*outs) if outs else start
        elif isinstance(node, Loop):
            self._flow(node.body, inner, items)
            if node.until is not None:
                self._pred_ok(node.until, inner, f"{node.loc}.loop.until")
        elif isinstance(node, Map):
            self._ref_ok(node.over, chain, items, f"{node.loc}.map.over", pid)
            self._flow(node.body, inner, items | {node.as_})
        elif isinstance(node, Escalate):
            self._flow(node.try_, inner, items)
            self._pred_ok(node.when, inner, f"{node.loc}.escalate.when")
            self._seat_reads(node.to, inner, items)
        self._done.add(pid)

    # -- blindness (V3) ------------------------------------------------------

    def _direct_sources(self, sid: str) -> set[str]:
        seat = self.spec.seats.get(sid)
        out: set[str] = set()
        if seat is None:
            return out
        for r in seat.reads:
            try:
                ref = r.ref
            except ValueError:
                continue
            tgt = ref.node_id
            if tgt is None:
                continue
            if tgt in self.idx.phases:
                node = self.idx.phases[tgt]
                out |= self.idx.seats_in(tgt)
                if isinstance(node, Panel):
                    out |= set(node.judges) | set(node.gates_first)
            else:
                out.add(tgt)
        return out

    def check_blindness(self) -> None:
        for pid, members in blind_groups(self.idx):
            group = set().union(*members) if members else set()
            for i, mine in enumerate(members):
                others = group - mine
                for sid in sorted(mine):
                    reached: set[str] = set()
                    frontier = [sid]
                    while frontier:
                        cur = frontier.pop()
                        for src in self._direct_sources(cur):
                            if src in group and src not in reached:
                                reached.add(src)
                                frontier.append(src)
                    leak = sorted((reached & others) - {sid})
                    if leak:
                        self.refuse("V3", "blindness_leak", f"seats.{sid}.reads",
                                    f"{sid!r} is in blind phase `{pid}` and reads "
                                    f"{', '.join(leak)} (directly or through a sibling's output)")

    def check_withheld(self) -> None:
        for sid, seat in self.spec.seats.items():
            for i, w in enumerate(seat.withheld):
                path = f"seats.{sid}.withheld[{i}]"
                try:
                    ref = parse_ref(w)
                except ValueError as exc:
                    self.refuse("schema", "reference_invalid", path, str(exc))
                    continue
                src = self.spec.seats.get(ref.target)
                if ref.kind != "field" or src is None or ref.field not in row_fields(src.output):
                    self.refuse("V8", "unknown_reference", path,
                                f"withheld {w!r} is not <seat>.<field of its output>")
                    continue
                for via in sorted(self._direct_sources(sid) - {ref.target}):
                    if self._carries(via, ref.target, ref.field or "", set()):
                        self.refuse("V3", "withheld_reachable", path,
                                    f"{sid!r} withholds {w} but reads {via!r}, which saw it")

    def _carries(self, sid: str, src: str, fld: str, seen: set[str]) -> bool:
        """Did ``sid``'s output possibly see ``src.fld``? (conservative)"""
        if sid in seen:
            return False
        seen.add(sid)
        seat = self.spec.seats.get(sid)
        if seat is None:
            return False
        withholds = f"{src}.{fld}" in seat.withheld
        for nxt in self._direct_sources(sid):
            if nxt == src and not withholds:
                return True
            if nxt != src and self._carries(nxt, src, fld, seen):
                return True
        return False

    # -- judges (V5, V6) -----------------------------------------------------

    def check_judges(self) -> None:
        families = {self.cat.family(s.plane) for s in self.spec.seats.values() if s.plane}
        families |= set(self.spec.families_available)
        families.discard(None)
        judges = {sid for sid, s in self.spec.seats.items() if s.kind == "judge"}
        for sid in sorted(judges):
            seat = self.spec.seats[sid]
            path = f"seats.{sid}"
            if seat.output not in JUDGE_ROW_TYPES:
                self.refuse("V6", "judge_output_not_verdict", f"{path}.output",
                            f"a judge writes a verdict row, not {seat.output}")
            for i, r in enumerate(seat.reads):
                if r.view == "prose":
                    self.refuse("V6", "judge_reads_prose", f"{path}.reads[{i}]",
                                "judges read sentence tables only")
                elif r.view != "sentence_table":
                    self.refuse("V6", "judge_view_not_sentence_table", f"{path}.reads[{i}]",
                                f"judges read sentence tables only, not {r.view}")
                tgt = r.ref.node_id if r.from_ else None
                if tgt in judges:
                    self.refuse("V6", "judge_reads_judge", f"{path}.reads[{i}]",
                                "judges never read each other")
            mine = self.cat.family(seat.plane)
            if len(families) >= 2:
                if not seat.differs_from:
                    self.refuse("V5", "judge_family_rule_missing", f"{path}.family_rule",
                                "another model family is available: declare differs_from")
                for d in seat.differs_from:
                    other = self.spec.seats.get(d)
                    if other is not None and mine and self.cat.family(other.plane) == mine:
                        self.refuse("V5", "judge_same_family", f"{path}.family_rule",
                                    f"{sid!r} and {d!r} both resolve to the {mine} family")
            elif seat.differs_from:
                self.warnings.append(
                    f"V5: only one model family is declared; judge {sid!r} cannot be kept off "
                    "the family it judges (warning only)")

    # -- publish and budget --------------------------------------------------

    def check_publish(self) -> None:
        if self.spec.publishes == "reader" and self.spec.id not in self.cat.reader_allowed:
            self.refuse("V10", "publishes_reader_not_allowed", "publishes",
                        f"the operator has not allowed {self.spec.id!r} to publish to readers")

    def check_budget(self, worst: Worst) -> None:
        b = self.spec.budgets
        if worst.calls > b.model_calls:
            self.refuse("V9", "budget_below_worst_case", "budgets.model_calls",
                        f"worst case is {worst.calls} calls; the round allows {b.model_calls}")
        if worst.seconds > b.wall_s:
            self.refuse("V9", "budget_below_worst_case", "budgets.wall_s",
                        f"worst case is {worst.seconds} s; the round allows {b.wall_s}")


def validate(spec: RoundSpec, catalog: Catalog | None = None, *,
             card_root: Path | None = None) -> Report:
    """Apply V1-V12. ``card_root`` (default: the spec file's folder) is where
    ``card:`` paths resolve; ``None`` with no source skips the card check."""
    root = card_root
    if root is None and spec.source:
        root = Path(spec.source).parent
    return _Validator(spec, catalog or default_catalog(), root).run()


def validate_file(path: str | Path, catalog: Catalog | None = None) -> Report:
    spec, refusals = load_spec(path)
    if spec is None:
        return Report(None, refusals)
    return validate(spec, catalog)


def require_valid(spec: RoundSpec, catalog: Catalog | None = None, *,
                  card_root: Path | None = None) -> Report:
    report = validate(spec, catalog, card_root=card_root)
    if not report.ok:
        raise SpecRefused(report.refusals)
    return report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m legba.data.rounds.validate",
                                 description="Validate round specs (refusal rules V1-V12).")
    ap.add_argument("specs", nargs="+", help="spec files (.yaml / .json)")
    ap.add_argument("--json", action="store_true", help="print one JSON report per spec")
    args = ap.parse_args(argv)
    bad = 0
    for p in args.specs:
        report = validate_file(p)
        bad += 0 if report.ok else 1
        if args.json:
            print(json.dumps({"path": p, **report.as_dict()}, sort_keys=True))
            continue
        label = report.spec.label if report.spec else p
        if report.ok and report.worst is not None:
            w = report.worst
            print(f"OK  {label}  sha256={report.spec.sha256[:12]}  worst case: {w.calls} calls, "  # type: ignore[union-attr]
                  f"{w.seconds} s, peak {w.peak} parallel ({w.code_calls} code calls)")
        else:
            print(f"REFUSED  {label}")
            for r in report.refusals:
                print(f"  {r}")
        for warn in report.warnings:
            print(f"  warning: {warn}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
