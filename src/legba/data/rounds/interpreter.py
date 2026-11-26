# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The round interpreter: runs a validated spec with
injected seats over a :class:`~.store.RunStore`.

Guarantees:

* **Deterministic.** Every step gets a sequence number in tree order; parallel
  members run in their declared order, and the parallel schedule is MODELLED
  (members packed into waves under ``max_parallel`` by their static slot
  demand), so the charged seconds, deadlines and peak are the same on every
  run. The harness executes members one after another; the runtime host
  can run them truly in parallel with the same accounting.
* **Replay = resume.** The walk is re-run from the root on every call; a step
  whose receipt exists is never re-run (its sealed output is reused, its
  calls and seconds re-counted). Interrupt a run anywhere and run it again on
  the same store: it lands where it stopped and ends in the same record.
* **Sealed, store-enforced blindness.** Outputs are sealed by hash when
  written; a seat's inputs come only from :meth:`Visibility.inputs_for`.
* **Bounded.** Seat deadlines and retries, phase deadlines, loop/map/debate
  call budgets, and the round's ``model_calls`` / ``wall_s`` are enforced; a
  spent budget ends the phase as ``partial`` and is recorded — it never
  truncates a call. Every close and stop reason is recorded.
* **Kept.** Failed attempts (raw text kept), absent seats, failed gates,
  losing candidates and dissent all stay in the record.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .budget import WorstCase
from .calibration import CalibrationBook, Standing
from .catalog import Catalog, default_catalog
from .gates import GateContext, run_gates
from .judge_render import render_version
from .record import build_record
from .refs import Predicate, compare, parse_ref
from .repair import repair_note
from .rows import RowTypeError, fill_rows_from_ids, parse_row
from .seats import Clock, SeatCall, SeatFailure, SeatFn, SeatNotBuilt, SeatReply, SystemClock
from .spec import (
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
    RoundSpec,
    SeatLeaf,
    Select,
    Seq,
    UseRound,
    walk,
)
from .store import PhaseRecord, Receipt, RunStore
from .tables import SCORE_FNS, Handed, candidates_of, evidence_rows, stance_signature
from .tally import gate_removals, tally, votes_of
from .tree import build_index
from .validate import require_valid
from .visibility import Position, Visibility, VisibilityRefused, in_scope, map_scope


class SpecChanged(RuntimeError):
    """A store holds a run of a different spec version (no resume across specs)."""


class ReplayDiverged(RuntimeError):
    """A replayed step landed on a different sequence number than recorded."""


class _Stop(Exception):
    def __init__(self, reason: str, state: str):
        super().__init__(reason)
        self.reason = reason
        self.state = state


class _PhaseBudget(Exception):
    def __init__(self, phase_path: str):
        super().__init__(phase_path)
        self.phase_path = phase_path


@dataclass
class Res:
    charged: float = 0.0
    peak: int = 0
    present: bool = False
    validity: str = "valid"
    escalate: bool = False


@dataclass
class _Budget:
    path: str
    limit: int
    used: int = 0


def waves(demands: Sequence[int], cap: int, per_wave: int | None = None) -> list[list[int]]:
    """Greedy, in order: pack members into waves whose slot demand sums to at
    most ``cap`` (a member alone may fill a wave) and, if given, at most
    ``per_wave`` members."""
    out: list[list[int]] = []
    cur: list[int] = []
    used = 0
    for i, d in enumerate(demands):
        d = min(d, cap)
        full = (used + d > cap) or (per_wave is not None and len(cur) >= per_wave)
        if cur and full:
            out.append(cur)
            cur, used = [], 0
        cur.append(i)
        used += d
    if cur:
        out.append(cur)
    return out


def _slug(value: Any) -> str:
    raw = value.get("id") if isinstance(value, Mapping) and "id" in value else value
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", str(raw))[:64] or "item"


class Interpreter:
    @property
    def seq(self) -> int:
        """The next step number. The store as seen by visibility rules and
        predicates is cut at it, so a replay sees what the first run saw."""
        return self._seq_value

    @seq.setter
    def seq(self, value: int) -> None:
        self._seq_value = value
        self.vis.horizon = value

    def __init__(self, spec: RoundSpec, store: RunStore, seats: SeatFn, *,
                 inputs: Mapping[str, Any], run_id: str, clock: Clock | None = None,
                 catalog: Catalog | None = None, card_root: str | Path | None = None,
                 validated: bool = True, calibration: CalibrationBook | None = None):
        self.spec = spec
        self.calibration = calibration
        self.store = store
        self.seats = seats
        self.inputs = dict(inputs)
        self.run_id = run_id
        self.clock = clock or SystemClock()
        self.catalog = catalog or default_catalog()
        self.card_root = Path(card_root) if card_root else (
            Path(spec.source).parent if spec.source else None)
        self.report = require_valid(spec, self.catalog, card_root=self.card_root) if validated \
            else None
        self.idx = build_index(spec)
        wc = WorstCase(spec, self.idx)
        wc.total()
        self.demand = {k: max(0, w.peak) for k, w in wc.by_phase.items()}
        self.cap = max(1, spec.budgets.max_parallel)
        self.vis = Visibility(spec, store, self.idx.phases, self.inputs)
        self._seq_value = 0
        self.seq = 0
        self.status_watched = {p.target for n in walk(spec.flow)
                               for p in _predicates(n) if p.kind == "phase_status"}
        self.calls = 0
        self.code_calls = 0
        self.peak = 0
        self.budgets: list[_Budget] = []
        self.ordinals: dict[str, int] = {}

    # ------------------------------------------------------------------ run

    def run(self) -> dict[str, Any]:
        missing = [k for k, d in self.spec.inputs.items() if not d.optional and k not in self.inputs]
        if missing:
            raise ValueError(f"missing run inputs: {missing}")
        header = self.store.get_run()
        if header is not None and header.get("spec", {}).get("sha256") != self.spec.sha256:
            raise SpecChanged(f"store holds a run of {header.get('spec')}, not {self.spec.label}")
        if header is None:
            header = {"run_id": self.run_id, "state": "running",
                      "spec": {"id": self.spec.id, "version": self.spec.version,
                               "sha256": self.spec.sha256},
                      "inputs": self.inputs, "publishes": self.spec.publishes,
                      "started_at": self.clock.now().isoformat(), "resumes": 0}
        else:
            header = {**header, "resumes": int(header.get("resumes", 0)) + 1}
        self.store.put_run(header)
        state, reason, charged = "ended", "flow_complete", 0.0
        try:
            res = self.exec(self.spec.flow, Position(()), 0.0, float(self.spec.budgets.wall_s))
            charged = res.charged
        except _Stop as stop:
            state, reason = stop.state, stop.reason
            charged = max((r.offset_s + r.charged_s for r in self.store.receipts()), default=0.0)
        header = {**header, "state": state, "stop_reason": reason,
                  "ended_at": self.clock.now().isoformat()}
        self.store.put_run(header)
        return build_record(self, header, charged)

    # ------------------------------------------------------------- phases

    def _open(self, segs: tuple[str, ...], phase_id: str, shape: str, start: float) -> PhaseRecord:
        path = "/".join(segs)
        rec = self.store.get_phase(path)
        if rec is None:
            rec = PhaseRecord(path, phase_id, shape, self.run_id, opened_seq=self.seq,
                              offset_s=start)
            self.store.put_phase(rec)
        return rec

    def _close(self, rec: PhaseRecord, reason: str, res: Res, **detail: Any) -> None:
        if rec.status == "closed":
            return
        rec.status, rec.close_reason, rec.closed_seq = "closed", reason, self.seq
        rec.validity, rec.charged_s = res.validity, round(res.charged, 6)
        rec.members = [{"key": r.key, "seat": r.seat, "status": r.status,
                        "sha256": r.sha256, "released": r.released}
                       for r in self.store.receipts() if r.path.startswith(rec.path)
                       and (r.path == rec.path or r.path.startswith(rec.path + "/"))]
        tally_row = detail.pop("tally", None)
        if tally_row is not None:
            rec.tally = tally_row
        rec.detail.update(detail)
        self.store.put_phase(rec)

    def _guarded(self, rec: PhaseRecord, body: Callable[[], tuple[str, Res, dict[str, Any]]],
                 start: float) -> Res:
        """Run a phase body; a stop or a spent budget closes it as partial."""
        try:
            reason, res, detail = body()
        except _PhaseBudget as spent:
            res = Res(self._spent_since(rec, start), 0, True, "partial")
            self._close(rec, "budget", res)
            if spent.phase_path != rec.path:
                raise
            return res
        except _Stop as stop:
            self._close(rec, stop.reason, Res(self._spent_since(rec, start), 0, False, "partial"))
            raise
        self._close(rec, reason, res, **detail)
        if res.validity == "invalid" and rec.phase_id not in self.status_watched:
            raise _Stop(f"invalid_phase:{rec.phase_id}", "stopped_invalid")
        return res

    @contextmanager
    def _sub(self, rec: PhaseRecord, start: float) -> Iterator[None]:
        """Guard a sub-instance record (a pass, turn, stage, iteration, item):
        a stop or a spent budget closes it as partial before propagating."""
        try:
            yield
        except _PhaseBudget:
            self._close(rec, "budget", Res(self._spent_since(rec, start), 0, False, "partial"))
            raise
        except _Stop as stop:
            self._close(rec, stop.reason, Res(self._spent_since(rec, start), 0, False, "partial"))
            raise

    def _spent_since(self, rec: PhaseRecord, start: float) -> float:
        ends = [r.offset_s + r.charged_s for r in self.store.receipts()
                if r.path == rec.path or r.path.startswith(rec.path + "/")]
        return max(0.0, max(ends, default=start) - start)

    # --------------------------------------------------------------- nodes

    def exec(self, node: Node, pos: Position, start: float, deadline_at: float) -> Res:  # noqa: C901
        if start >= float(self.spec.budgets.wall_s):
            raise _Stop("budget_wall_s", "stopped_budget")
        if isinstance(node, SeatLeaf):
            return self.run_seat(node.seat, pos, start, deadline_at)
        if isinstance(node, UseRound):
            raise _Stop("use_round_not_built", "stopped_failure")
        segs = pos.segments + (node.id or "",)
        here = Position(segs, pos.items)
        rec = self._open(segs, node.id or "", type(node).__name__.lower(), start)
        dl = deadline_at
        if getattr(node, "deadline_s", None) is not None:
            dl = min(deadline_at, start + float(node.deadline_s))  # type: ignore[union-attr]
        handler = {
            Seq: self._seq, Par: self._par, Blind: self._par, Rebuttal: self._rebuttal,
            Debate: self._debate, Panel: self._panel, Select: self._select,
            Branch: self._branch, Loop: self._loop, Map: self._map, Escalate: self._escalate,
        }[type(node)]
        pushed = None
        limit = getattr(node, "model_calls", None)
        if limit is not None:
            pushed = _Budget(rec.path, int(limit))
            self.budgets.append(pushed)
        try:
            return self._guarded(rec, lambda: handler(node, here, start, dl), start)
        finally:
            if pushed is not None:
                self.budgets.remove(pushed)

    def _seq(self, node: Seq, pos: Position, start: float, dl: float):
        t, res = start, Res()
        for child in node.children:
            r = self.exec(child, pos, t, dl)
            t += r.charged
            res.peak = max(res.peak, r.peak)
            res.present = res.present or r.present
            res.validity = _worse(res.validity, r.validity)
        res.charged = t - start
        return "all_done", res, {}

    def _demand(self, child: Node) -> int:
        if isinstance(child, SeatLeaf):
            seat = self.spec.seats.get(child.seat)
            return 1 if seat is not None and seat.calls_model else 0
        return self.demand.get(child.id or "", 1)

    def _run_waves(self, items: Sequence[Callable[[float], Res]], demands: Sequence[int],
                   start: float, dl: float, per_wave: int | None = None,
                   stop_after: Callable[[list[Res]], bool] | None = None
                   ) -> tuple[list[Res | None], float, int, bool]:
        """Run members wave by wave; returns (results, charged, peak, stopped)."""
        results: list[Res | None] = [None] * len(items)
        t, peak = start, 0
        for wave in waves(demands, self.cap, per_wave):
            longest, wave_peak = 0.0, 0
            for i in wave:
                r = items[i](t)
                results[i] = r
                longest = max(longest, r.charged)
                wave_peak += r.peak
            peak = max(peak, wave_peak)
            t += longest
            if stop_after is not None and stop_after([r for r in results if r is not None]):
                self.peak = max(self.peak, peak)
                return results, min(t, dl) - start, peak, True
        self.peak = max(self.peak, peak)
        return results, min(t, dl) - start if t > dl else t - start, peak, False

    def _par(self, node: Par | Blind, pos: Position, start: float, dl: float):
        need = None
        if isinstance(node, Par) and node.join.mode != "all":
            need = 1 if node.join.mode == "first" else int(node.join.k or 1)
        stop = (lambda rs: sum(r.present for r in rs) >= need) if need else None
        runs = [(lambda t, c=c: self.exec(c, pos, t, dl)) for c in node.children]
        results, charged, peak, joined = self._run_waves(
            runs, [self._demand(c) for c in node.children], start, dl, stop_after=stop)
        skipped = []
        for c, r in zip(node.children, results, strict=True):
            if r is None:
                skipped.append(c.seat if isinstance(c, SeatLeaf) else c.id)
                if isinstance(c, SeatLeaf):
                    self._absent(c.seat, pos, "join_met", start + charged)
        present = sum(1 for r in results if r is not None and r.present)
        res = Res(charged, peak, present > 0,
                  _worse_all([r.validity for r in results if r is not None]))
        late = any(r is not None and not r.present for r in results)
        reason = ("join_" + node.join.mode) if joined else (
            "closed_with_absences" if late else "all_sealed")
        if isinstance(node, Blind) and node.min_present is not None and present < node.min_present:
            res.validity = "invalid"
            reason = "below_min_present"
        elif late and res.validity == "valid":
            res.validity = "partial"
        return reason, res, {"present": present, "skipped": skipped}

    # ------------------------------------------------------------ seats

    def _card(self, seat_id: str, pos: Position) -> tuple[str | None, str | None, str | None]:
        seat = self.spec.seats[seat_id]
        card = seat.card
        if isinstance(card, CardFromItem):
            # a card named by a run input is the profile's own prose
            from .profile_cards import card_path

            value = pos.items.get(card.from_item)
            if not isinstance(value, str):
                raise SeatFailure(f"card_missing: item {card.from_item!r} names no card")
            text = card_path(value).read_text(encoding="utf-8")
            return f"profile:{value}", text, hashlib.sha256(text.encode("utf-8")).hexdigest()
        if not isinstance(card, str):
            return None, None, None
        if self.card_root is None:
            return card, None, None
        p = self.card_root / card
        if not p.is_file():
            raise SeatFailure(f"card_missing: {card}")
        text = p.read_text(encoding="utf-8")
        return card, text, hashlib.sha256(text.encode("utf-8")).hexdigest()

    def _base(self, seat_id: str, pos: Position, candidate: str | None) -> str:
        return f"{pos.path}/{seat_id}" + (f"@{candidate}" if candidate else "")

    def _next_seq(self, key: str) -> tuple[int, Receipt | None]:
        seq = self.seq
        self.seq += 1
        got = self.store.get_receipt(key)
        if got is not None and got.seq != seq:
            raise ReplayDiverged(f"{key}: recorded at step {got.seq}, replayed at {seq}")
        return seq, got

    def _absent(self, seat_id: str, pos: Position, reason: str, at: float,
                candidate: str | None = None) -> Receipt:
        key = self._base(seat_id, pos, candidate) + "#absent"
        seq, got = self._next_seq(key)
        if got is not None:
            return got
        r = Receipt(key, self.run_id, seq, seat_id, pos.path, pos.segments,
                    map_scope(pos.segments), -1, "absent", failure=reason, offset_s=at,
                    spec=self._spec_stamp(), candidate=candidate)
        self.store.put_receipt(r)
        return r

    def _spec_stamp(self) -> dict[str, Any]:
        return {"id": self.spec.id, "version": self.spec.version, "sha256": self.spec.sha256}

    def _check_budgets(self, seat_id: str, pos: Position, at: float,
                       candidate: str | None) -> None:
        if self.calls >= self.spec.budgets.model_calls:
            self._absent(seat_id, pos, "budget_model_calls", at, candidate)
            raise _Stop("budget_model_calls", "stopped_budget")
        for b in reversed(self.budgets):
            if b.used >= b.limit:
                self._absent(seat_id, pos, "phase_budget", at, candidate)
                raise _PhaseBudget(b.path)

    def _count_call(self) -> None:
        self.calls += 1
        for b in self.budgets:
            b.used += 1

    def run_seat(self, seat_id: str, pos: Position, start: float, deadline_at: float, *,
                 extra_reads: Sequence[ReadSpec] = (), implicit: Sequence[Handed] = (),
                 candidate: str | None = None, candidates: Sequence[str] = (),
                 turn: int | None = None, item: Any = None) -> Res:
        seat = self.spec.seats[seat_id]
        ordinal = self.ordinals.get(seat_id, 0)
        self.ordinals[seat_id] = ordinal + 1
        t, called, last = start, False, None
        base = self._base(seat_id, pos, candidate)
        note: dict[str, Any] | None = None
        for attempt in range(1 + seat.retries):
            key = f"{base}#{attempt}"
            if self.store.get_receipt(key) is None:
                if t >= deadline_at:
                    last = "deadline"
                    break
                if seat.calls_model:
                    self._check_budgets(seat_id, pos, t, candidate)
            seq, got = self._next_seq(key)
            r = got or self._attempt(seat_id, seat, pos, key, seq, attempt, ordinal, t,
                                     deadline_at, extra_reads, implicit, candidate,
                                     candidates, turn, item, note)
            note = repair_note(r) if seat.repair == "reasons" else None
            if r.model_call:
                self._count_call()
                called = True
            elif seat.kind == "code":
                self.code_calls += 1
            t += r.charged_s
            if called:
                self.peak = max(self.peak, 1)
            if r.status == "sealed" and r.released:
                return Res(t - start, 1 if called else 0, True)
            last = r.failure
            if r.failure in ("visibility_refused",) or t >= deadline_at:
                break
        self._absent(seat_id, pos, last or "failed", t, candidate)
        res = Res(t - start, 1 if called else 0, False, "partial")
        if seat.on_failure == "stop_round":
            raise _Stop(f"seat_failed:{seat_id}", "stopped_failure")
        res.escalate = seat.on_failure == "escalate"
        return res

    def _attempt(self, seat_id: str, seat: Any, pos: Position, key: str, seq: int,
                 attempt: int, ordinal: int, t: float, deadline_at: float,
                 extra_reads: Sequence[ReadSpec], implicit: Sequence[Handed],
                 candidate: str | None, candidates: Sequence[str], turn: int | None,
                 item: Any, note: Mapping[str, Any] | None = None) -> Receipt:
        r = Receipt(key, self.run_id, seq, seat_id, pos.path, pos.segments,
                    map_scope(pos.segments), attempt, "failed", row_type=seat.output,
                    plane=seat.plane, family=self.catalog.family(seat.plane),
                    thinking=seat.thinking, tools=list(seat.tools), offset_s=t,
                    spec=self._spec_stamp(), candidate=candidate)
        self.vis.horizon = seq  # what existed before this step, on a replay too
        try:
            handed = list(self.vis.inputs_for(seat_id, tuple(seat.reads) + tuple(extra_reads),
                                              pos)) + list(implicit)
        except VisibilityRefused as exc:
            r.failure, r.raw = "visibility_refused", str(exc)
            self.store.put_receipt(r)
            return r
        self.vis.horizon = self.seq
        r.handed = [{"read": h.read, "ref": h.ref_id, "sha256": h.sha256, "absent": h.absent}
                    for h in handed]
        iteration = next((int(s[2:]) for s in reversed(pos.segments) if s.startswith("~i")), None)
        try:
            r.card, card_text, r.card_sha256 = self._card(seat_id, pos)
            call = SeatCall(self.run_id, seat_id, seat, seat.output, attempt, ordinal, pos.path,
                            map_scope(pos.segments), tuple(handed), r.card, card_text,
                            item if item is not None else _innermost_item(pos),
                            candidate, tuple(candidates), turn, iteration, note)
            if note is not None:
                r.repair = {"from_attempt": note.get("attempt"), "kind": note.get("kind"),
                            "reasons": sorted({str(f.get("reason")) for f in note.get("failed") or ()})
                            or [str(note.get("error", ""))[:120]]}
            m0 = self.clock.monotonic()
            r.model_call = bool(seat.calls_model)
            reply: SeatReply = self.seats(call)
            duration = reply.elapsed_s if reply.elapsed_s is not None \
                else self.clock.monotonic() - m0
        except SeatNotBuilt:
            raise
        except SeatFailure as exc:
            r.failure, r.raw = f"seat_failure: {exc.reason}", exc.raw
            r.charged_s = 0.0
            self.store.put_receipt(r)
            return r
        except Exception as exc:  # noqa: BLE001 - any seat error is a failed attempt, kept
            r.failure, r.raw = f"error: {type(exc).__name__}: {exc}", None
            self.store.put_receipt(r)
            return r
        limit = min(t + seat.effective_deadline_s, deadline_at)
        r.duration_s = round(float(duration), 6)
        if t + duration > limit:
            if reply.meta is not None:
                r.call = dict(reply.meta)
            r.failure = "deadline"
            r.raw = _dump(reply.payload)
            r.charged_s = round(max(0.0, limit - t), 6)
            self.store.put_receipt(r)
            return r
        r.charged_s = r.duration_s
        if reply.meta is not None:
            r.call = dict(reply.meta)
        r.tools_bound = bool(reply.tools_bound)
        if reply.tool_rows:
            # Rows the seat's own (cut, run-excluded) tool calls returned are
            # handed to the gates beside the store's inputs, and listed.
            tool_hand = Handed("tool", f"tool:{key}", None, None,
                               {"rows": [dict(t) for t in reply.tool_rows]})
            handed = handed + [tool_hand]
            r.handed.append({"read": "tool", "ref": tool_hand.ref_id,
                             "rows": [str(t.get("id")) for t in reply.tool_rows],
                             "absent": None})
        try:
            payload = parse_row(seat.output, fill_rows_from_ids(
                seat.output, reply.payload, evidence_rows(handed)))
        except RowTypeError as exc:
            r.failure, r.raw = f"unparseable: {exc}", reply.raw or _dump(reply.payload)
            self.store.put_receipt(r)
            return r
        results = run_gates(seat.gates, GateContext(seat_id, seat.output, payload, handed,
                                                    self.run_id))
        r.gates = [g.as_dict() for g in results]
        r.status, r.payload, r.prose, r.raw = "sealed", payload, reply.prose, reply.raw
        r.released = all(g.passed for g in results)
        r.failure = None if r.released else "gate_failed"
        r.sealed_at = self.clock.now().isoformat()
        self.store.put_receipt(r)
        return r

    def _code_receipt(self, owner: str, pos: Position, row_type: str, payload: Any,
                      at: float) -> Receipt:
        """A sealed row written by a shape (a tally, a selection)."""
        key = f"{pos.path}/{owner}.{row_type.split('/')[0]}"
        seq, got = self._next_seq(key)
        if got is not None:
            return got
        r = Receipt(key, self.run_id, seq, owner, pos.path, pos.segments,
                    map_scope(pos.segments), 0, "sealed", True, row_type,
                    parse_row(row_type, payload), offset_s=at, spec=self._spec_stamp(),
                    sealed_at=self.clock.now().isoformat())
        self.store.put_receipt(r)
        return r

    # ----------------------------------------------------------- shapes

    def _members_of(self, of: str, pos: Position) -> list[Receipt]:
        seats = self.idx.seats_in(of)
        found = [r for r in self.vis.resolve(parse_ref(f"{of}.sealed"), "", pos)
                 if r.seat in seats]
        return [r for r in found if r.status == "sealed" and r.released]

    def _rebuttal(self, node: Rebuttal, pos: Position, start: float, dl: float):
        members = self._members_of(node.of, pos)
        prev = {(m.seat, _item_key(m)): m for m in members}
        items = self._items_of(node.of)
        t, peak, reason, done = start, 0, "passes", 0
        for k in range(1, int(node.passes or 1) + 1):
            psegs = pos.segments + (f"~p{k}",)
            prec = self._open(psegs, f"~p{k}", "rebuttal_pass", t)
            ppos = Position(psegs, pos.items)

            snapshot = dict(prev)

            def one(at: float, key: tuple[str, str | None], ppos: Position = ppos,
                    snapshot: dict[tuple[str, str | None], Receipt] = snapshot) -> Res:
                seat, item_key = key
                others = [self.vis.hand(r, ReadSpec(**{"from": f"{node.of}.others"}),
                                        parse_ref(f"{node.of}.others"), self.spec.seats.get(seat))
                          for kk, r in snapshot.items() if kk != key]
                value = items.get(item_key) if item_key else None
                mpos = Position(ppos.segments, {**ppos.items, **(value or {})})
                return self.run_seat(seat, mpos, at, dl, extra_reads=node.also,
                                     implicit=others, candidate=item_key,
                                     item=next(iter((value or {}).values()), None))
            keys = list(prev)
            with self._sub(prec, t):
                res_list, charged, wpeak, _ = self._run_waves(
                    [(lambda at, kk=kk: one(at, kk)) for kk in keys], [1] * len(keys), t, dl)
            self._close(prec, "all_sealed", Res(charged, wpeak, True))
            t += charged
            peak = max(peak, wpeak)
            done = k
            now = {}
            for (seat, item_key), _r in zip(keys, res_list, strict=True):
                latest = [r for r in self.store.receipts()
                          if r.seat == seat and r.path == ppos.path and r.candidate == item_key
                          and r.status == "sealed" and r.released]
                now[(seat, item_key)] = latest[-1] if latest else prev[(seat, item_key)]
            unchanged = all(stance_signature(now[x].payload) == stance_signature(prev[x].payload)
                            for x in keys)
            prev = now
            if unchanged:
                reason = "unchanged"
                break
            if t >= dl:
                reason = "deadline"
                break
        res = Res(t - start, peak, bool(members))
        return reason, res, {"passes_run": done, "members": len(members),
                             "unchanged": reason == "unchanged"}

    def _items_of(self, phase_id: str) -> dict[str, dict[str, Any]]:
        node = self.idx.phases.get(phase_id)
        if not isinstance(node, Map):
            return {}
        out: dict[str, dict[str, Any]] = {}
        for rec in self.store.phases():
            if rec.phase_id == phase_id and "items" in rec.detail:
                for key, value in rec.detail["items"].items():
                    out[key] = {node.as_: value}
        return out

    def _debate(self, node: Debate, pos: Position, start: float, dl: float):
        transcript = ReadSpec(**{"from": f"{node.id}.sealed", "view": "rows"})
        t, reason = start, "max_turns"
        n = len(node.seats)
        for turn in range(1, int(node.max_turns or 0) + 1):
            if t >= dl:
                reason = "deadline"
                break
            order = list(node.seats)
            if node.order == "round_robin":
                shift = ((turn - 1) // n) % n
                order = order[shift:] + order[:shift]
            speaker = order[(turn - 1) % n]
            tsegs = pos.segments + (f"~t{turn}",)
            trec = self._open(tsegs, f"~t{turn}", "turn", t)
            with self._sub(trec, t):
                r = self.run_seat(speaker, Position(tsegs, pos.items), t, dl,
                                  extra_reads=(transcript,), turn=turn)
            self._close(trec, "spoken" if r.present else "absent", r)
            t += r.charged
            out = self._latest_released(speaker, tsegs)
            if out is not None and (out.row_type == "concede/1"
                                    or (isinstance(out.payload, Mapping) and out.payload.get("concede"))):
                reason = "concede"
                break
            if node.settle_by and turn % n == 0:
                ssegs = pos.segments + (f"~s{turn // n}",)
                srec = self._open(ssegs, f"~s{turn // n}", "settle", t)
                with self._sub(srec, t):
                    sr = self.run_seat(node.settle_by, Position(ssegs, pos.items), t, dl)
                self._close(srec, "ruled" if sr.present else "absent", sr)
                t += sr.charged
                ruling = self._latest_released(node.settle_by, ssegs)
                if ruling is not None and isinstance(ruling.payload, Mapping) \
                        and ruling.payload.get("settled"):
                    reason = "settled"
                    break
        return reason, Res(t - start, 1, True), {}

    def _latest_released(self, seat: str, segs: tuple[str, ...]) -> Receipt | None:
        path = "/".join(segs)
        got = [r for r in self.store.receipts() if r.seat == seat and r.path == path
               and r.status == "sealed" and r.released]
        return got[-1] if got else None

    def _judge_stage(self, stage: str, judges: Sequence[str], cands: list[str],
                     pos: Position, per: str, t: float, dl: float
                     ) -> tuple[dict[str, dict[str, Any] | None], float, int]:
        segs = pos.segments + (stage,)
        rec = self._open(segs, stage, "panel_stage", t)
        spos = Position(segs, pos.items)
        calls: list[tuple[str, str | None]] = [(j, c) for j in judges for c in cands] \
            if per == "candidate" else [(j, None) for j in judges]
        runs = [(lambda at, j=j, c=c: self.run_seat(j, spos, at, dl, candidate=c,
                                                    candidates=cands)) for j, c in calls]
        with self._sub(rec, t):
            _, charged, peak, _ = self._run_waves(runs, [1] * len(runs), t, dl)
        self._close(rec, "all_sealed", Res(charged, peak, True))
        verdicts: dict[str, dict[str, Any] | None] = {}
        for j in judges:
            got = [r for r in self.store.receipts() if r.seat == j and r.path == spos.path
                   and r.status == "sealed" and r.released]
            if per == "candidate":
                latest: dict[str, Receipt] = {}
                for r in got:
                    latest[r.candidate or ""] = r
                entries = [v for r in latest.values() for v in (r.payload or {}).get("verdicts", ())]
                verdicts[j] = {"verdicts": entries} if latest else None
            else:
                verdicts[j] = got[-1].payload if got else None
        return verdicts, charged, peak

    def _standings(self, judges: Sequence[str], pos: Position) -> dict[str, Standing] | None:
        """The calibration rule for each judge (``None``: no book, not enforced)."""
        if self.calibration is None:
            return None
        out: dict[str, Standing] = {}
        for j in judges:
            seat = self.spec.seats[j]
            try:
                _, _, sha = self._card(j, pos)
            except SeatFailure:
                sha = None
            out[j] = self.calibration.standing(sha, seat.plane, seat.output,
                                               render_version(seat.output))
        return out

    def _panel(self, node: Panel, pos: Position, start: float, dl: float):
        on = self.vis.resolve(parse_ref(node.on), node.id or "", pos)
        handed = [self.vis.hand(r, ReadSpec(**{"from": node.on}), parse_ref(node.on), None)
                  for r in on if r.status == "sealed" and r.released]
        cands = candidates_of(handed)
        dropped: list[str] = []
        if node.per == "candidate" and node.max_candidates and len(cands) > node.max_candidates:
            cands, dropped = cands[:node.max_candidates], cands[node.max_candidates:]
        t, peak = start, 0
        removed: dict[str, list[str]] = {}
        standing = self._standings(tuple(node.gates_first) + tuple(node.judges), pos)
        advisory: list[dict[str, Any]] = []

        def may(j: str) -> bool:
            return standing is None or standing[j].may_gate

        if node.gates_first:
            gv, charged, p = self._judge_stage("~gates", node.gates_first, cands, pos,
                                               node.per, t, dl)
            t, peak = t + charged, max(peak, p)
            removed = gate_removals(cands, {j: v for j, v in gv.items() if may(j)})
            for j in node.gates_first:
                if not may(j) and standing is not None:
                    advisory.append({"judge": j, "stage": "gates", "reason": standing[j].reason,
                                     "would_remove": sorted(c for c, js in gate_removals(
                                         cands, {j: gv.get(j)}).items() if js)})
        live = [c for c in cands if c not in removed]
        votes, charged, p = self._judge_stage("~votes", node.judges, live, pos, node.per, t, dl)
        t, peak = t + charged, max(peak, p)
        counted = {j: (v if may(j) else None) for j, v in votes.items()}
        for j in node.judges:
            if not may(j) and standing is not None:
                advisory.append({"judge": j, "stage": "votes", "reason": standing[j].reason,
                                 "votes": {c: v.outcome for c, v in
                                           votes_of(j, votes.get(j), live).items()}
                                 if votes.get(j) is not None else None})
        result = tally(node.tally, node.judges, cands, counted, removed)
        row = result.as_row()
        row["advisory"] = advisory
        row["calibration"] = {"enforced": False} if standing is None else {
            "enforced": True, "source": self.calibration.source if self.calibration else None,
            "judges": {j: st.as_dict() for j, st in standing.items()}}
        self._code_receipt(node.id or "", pos, "tally/1", row, t)
        self.code_calls += 1
        res = Res(t - start, peak, True)
        return result.outcome, res, {"tally": row, "candidates": cands, "dropped": dropped}

    def _select(self, node: Select, pos: Position, start: float, dl: float):
        src = [r for r in self.vis.resolve(parse_ref(node.from_), node.id or "", pos)
               if r.status == "sealed" and r.released]
        handed = [self.vis.hand(r, ReadSpec(**{"from": node.from_}), parse_ref(node.from_), None)
                  for r in src]
        cands = candidates_of(handed)
        scores: dict[str, float] = {}
        outcomes: dict[str, str] = {}
        if node.by in self.idx.phases:
            tal = [r for r in self.vis.resolve(parse_ref(f"{node.by}.tally"), node.id or "", pos)
                   if r.status == "sealed"]
            for c in (tal[-1].payload.get("candidates") if tal else ()) or ():
                cid = str(c.get("candidate"))
                scores[cid] = float(c.get("points", c.get("stands", 0)) or 0)
                outcomes[cid] = str(c.get("outcome"))
        else:
            fn = SCORE_FNS[node.by]
            for h in handed:
                scores[h.ref_id] = float(fn(h))
        if node.keep_stands:
            kept = [c for c in cands if outcomes.get(c) == "stands"]
            tied = False
        else:
            ranked = sorted(cands, key=lambda c: (-scores.get(c, 0.0), cands.index(c)))
            k = int(node.keep_top or 1)
            cut = scores.get(ranked[k - 1], 0.0) if len(ranked) >= k else None
            kept = [c for c in ranked if cut is not None and scores.get(c, 0.0) >= cut] \
                if len(ranked) > k else ranked
            tied = len(kept) > k
        payload = {"kept": kept, "dropped": [c for c in cands if c not in kept],
                   "by": node.by, "tied_at_cut": tied}
        self._code_receipt(node.id or "", pos, "selection/1", payload, start)
        self.code_calls += 1
        return "selected", Res(0.0, 0, True), {"kept": kept, "tied_at_cut": tied}

    def _branch(self, node: Branch, pos: Position, start: float, dl: float):
        for i, case in enumerate(node.cases):
            if self.holds(case.when, pos, start):
                r = self.exec(case.do, pos, start, dl)
                return f"case[{i}]", r, {"chosen": i}
        if node.default is None:
            return "no_case", Res(), {}
        r = self.exec(node.default, pos, start, dl)
        return "default", r, {"chosen": "default"}

    def _loop(self, node: Loop, pos: Position, start: float, dl: float):
        t, peak, reason, done = start, 0, "bound", 0
        for i in range(1, int(node.max_iterations or 1) + 1):
            isegs = pos.segments + (f"~i{i}",)
            irec = self._open(isegs, f"~i{i}", "iteration", t)
            ipos = Position(isegs, pos.items)
            try:
                with self._sub(irec, t):
                    r = self.exec(node.body, ipos, t, dl)
            except _PhaseBudget as spent:
                if spent.phase_path != pos.path:
                    raise
                return "budget", Res(self._spent_since_path(pos.path, start), peak, True,
                                     "partial"), {"iterations": i}
            self._close(irec, "iteration_done", r)
            t += r.charged
            peak = max(peak, r.peak)
            done = i
            if node.until is not None and self.holds(node.until, ipos, t):
                reason = "until"
                break
        return reason, Res(t - start, peak, True), {"iterations": done}

    def _spent_since_path(self, path: str, start: float) -> float:
        ends = [r.offset_s + r.charged_s for r in self.store.receipts()
                if r.path == path or r.path.startswith(path + "/")]
        return max(0.0, max(ends, default=start) - start)

    def _map(self, node: Map, pos: Position, start: float, dl: float):
        values = self._map_items(node, pos)
        dropped = values[node.max_items:] if node.max_items else []
        values = values[:node.max_items] if node.max_items else values
        keys: list[str] = []
        for v in values:
            k = _slug(v)
            while k in keys:
                k += "_"
            keys.append(k)
        rec = self.store.get_phase(pos.path)
        if rec is not None and rec.status != "closed":
            rec.detail["items"] = dict(zip(keys, values, strict=True))
            self.store.put_phase(rec)
        body_demand = self._demand(node.body)
        conc = max(1, min(int(node.concurrency or 1), self.cap))

        def one(at: float, key: str, value: Any) -> Res:
            isegs = pos.segments + (f"[{key}]",)
            irec = self._open(isegs, f"[{key}]", "item", at)
            with self._sub(irec, at):
                r = self.exec(node.body, Position(isegs, {**pos.items, node.as_: value}), at, dl)
            self._close(irec, "item_done", r)
            return r
        runs = [(lambda at, k=k, v=v: one(at, k, v)) for k, v in zip(keys, values, strict=True)]
        results, charged, peak, _ = self._run_waves(runs, [max(body_demand, 0)] * len(runs),
                                                    start, dl, per_wave=conc)
        present = sum(1 for r in results if r is not None and r.present)
        res = Res(charged, peak, present > 0,
                  _worse_all([r.validity for r in results if r is not None]))
        return "all_items", res, {"items_run": len(keys), "items_dropped": len(dropped)}

    def _map_items(self, node: Map, pos: Position) -> list[Any]:
        ref = node.over
        if ref.kind == "input":
            value = self.inputs.get(ref.target)
        elif ref.kind == "item":
            value = pos.items.get(ref.target)
        else:
            got = [r for r in self.vis.resolve(ref, node.id or "", pos)
                   if r.status == "sealed" and r.released]
            payload = got[-1].payload if got else {}
            value = payload.get(ref.field) if ref.kind == "field" and isinstance(payload, Mapping) \
                else payload
        return list(value) if isinstance(value, (list, tuple)) else []

    def _escalate(self, node: Escalate, pos: Position, start: float, dl: float):
        r = self.exec(node.try_, pos, start, dl)
        t = start + r.charged
        if r.escalate or self.holds(node.when, pos, t):
            r2 = self.run_seat(node.to, pos, t, dl)
            return "escalated", Res(t - start + r2.charged, max(r.peak, r2.peak),
                                    r.present or r2.present), {}
        return "not_escalated", r, {}

    # ------------------------------------------------------- predicates

    def holds(self, p: Predicate, pos: Position, now: float) -> bool:
        value = self._eval(p, pos, now)
        return (not value) if p.negate else value

    def _scoped(self, pos: Position) -> list[Receipt]:
        return [r for r in self.store.receipts()
                if r.seq < self.seq and in_scope(r.phase_path, pos.segments)]

    def _eval(self, p: Predicate, pos: Position, now: float) -> bool:  # noqa: C901
        if p.kind == "budget":
            left = (self.spec.budgets.model_calls - self.calls) if p.resource == "calls" \
                else (self.spec.budgets.wall_s - now)
            return compare(left, p.op or "<", p.n or 0)
        tgt = p.target or ""
        rs = self._scoped(pos)
        if p.kind == "gate":
            mine = [r for r in rs if r.seat == tgt and r.status in ("sealed", "failed")]
            if not mine:
                return False
            last = mine[-1]
            if last.status != "sealed":
                return False
            gates = [g for g in last.gates if p.gate is None or g.get("gate") == p.gate]
            return all(g.get("passed") for g in gates)
        if p.kind == "tally":
            tl = [r for r in rs if r.seat == tgt and r.row_type == "tally/1"]
            return bool(tl) and tl[-1].payload.get("outcome") == p.outcome
        if p.kind == "count":
            hits = [r for r in rs if r.status == "sealed" and r.released
                    and r.row_type == p.row_type
                    and (r.seat == tgt or tgt in r.phase_path)
                    and (p.field is None or (r.payload or {}).get(p.field) == p.value)]
            return compare(len(hits), p.op or ">=", p.n or 0)
        recs = [ph for ph in self.store.phases() if ph.phase_id == tgt
                and ph.closed_seq is not None and ph.closed_seq <= self.seq
                and in_scope(ph.path.split("/"), pos.segments)]
        if p.kind == "unchanged":
            return bool(recs) and bool(recs[-1].detail.get("unchanged"))
        if p.kind == "phase_status":
            return bool(recs) and recs[-1].validity == p.outcome
        return False


def _innermost_item(pos: Position) -> Any:
    return next(iter(reversed(list(pos.items.values()))), None) if pos.items else None


def _item_key(r: Receipt) -> str | None:
    scope = map_scope(r.phase_path)
    return next(iter(scope.values()), None) if scope else r.candidate


def _worse(a: str, b: str) -> str:
    """Merge a child's validity into its parent's: an invalid child makes the
    parent partial (only a phase's own rule makes it invalid)."""
    order = {"valid": 0, "partial": 1, "invalid": 2}
    b = "partial" if b == "invalid" else b
    return a if order.get(a, 0) >= order.get(b, 0) else b


def _worse_all(vals: Sequence[str]) -> str:
    out = "valid"
    for v in vals:
        out = _worse(out, v)
    return out


def _predicates(node: Node) -> list[Predicate]:
    if isinstance(node, Branch):
        return [c.when for c in node.cases]
    if isinstance(node, Loop) and node.until is not None:
        return [node.until]
    if isinstance(node, Escalate):
        return [node.when]
    return []


def run_round(spec: RoundSpec, store: RunStore, seats: SeatFn, *, inputs: Mapping[str, Any],
              run_id: str, clock: Clock | None = None, catalog: Catalog | None = None,
              card_root: str | Path | None = None,
              calibration: CalibrationBook | None = None) -> dict[str, Any]:
    """Validate and run one round; returns the run record."""
    return Interpreter(spec, store, seats, inputs=inputs, run_id=run_id, clock=clock,
                       catalog=catalog, card_root=card_root, calibration=calibration).run()


def _dump(payload: Any) -> str:
    """The raw text kept for a reply that was late or did not parse (sorted
    keys, so a replay keeps the same text)."""
    return json.dumps(payload, default=str, sort_keys=True)[:20000]
