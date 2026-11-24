# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``inputs_for``: the ONE function that builds what a seat is handed
(plan P4 §3.3). Blindness is the store's job, not the seats'.

A seat at position R (its phase-instance segments) asking for a read gets
only rows that are:

1. allowed by its ``reads`` (default deny) — nothing else is ever looked up;
2. **sealed and released** (gates passed, on time) — an unreleased attempt is
   handed as an ``absent`` marker, never as content;
3. **in scope** — an output written inside a map item is invisible from any
   other item of the same map (items never see each other);
4. **closed** — every phase instance the output sits in, below the deepest
   phase it shares with the reader, has closed;
5. **not a blind sibling** — if the deepest shared phase is blind (a
   ``blind`` phase, a rebuttal pass, a panel's gate or vote stage) and still
   open, the read is a leak attempt and raises :class:`VisibilityRefused`
   (the step then fails with ``visibility_refused``).

Withheld fields are cut from the payload here; views (rows, sentence table,
prose) are rendered here. Positions are tuples of segments: phase ids, plus
``[key]`` for a map item and ``~i1`` / ``~p1`` / ``~t1`` / ``~s1`` /
``~gates`` / ``~votes`` for loop iterations, rebuttal passes, debate turns
and settles, and panel stages.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from .refs import Ref
from .spec import Blind, Escalate, Panel, ReadSpec, RoundSpec, Seat
from .store import Receipt, RunStore
from .tables import Handed, table_view

BLIND_SUBSTAGES = ("~p", "~gates", "~votes")


class VisibilityRefused(RuntimeError):
    """A read that would hand a seat a row it may not see."""


@dataclass(frozen=True)
class Position:
    """Where a reader stands: its segments, its map items and their values."""

    segments: tuple[str, ...]
    items: Mapping[str, Any] = field(default_factory=dict)  # item name (map `as`) -> value

    @property
    def path(self) -> str:
        return "/".join(self.segments)


def map_scope(segments: Sequence[str]) -> dict[str, str]:
    """``{map_id: item_key}`` for every map item a path sits in."""
    out: dict[str, str] = {}
    for i, seg in enumerate(segments):
        if seg.startswith("[") and seg.endswith("]") and i > 0:
            out[segments[i - 1]] = seg[1:-1]
    return out


def in_scope(receipt_segments: Sequence[str], reader_segments: Sequence[str]) -> bool:
    mine = map_scope(reader_segments)
    theirs = map_scope(receipt_segments)
    return all(mine[m] == k for m, k in theirs.items() if m in mine)


def _common(a: Sequence[str], b: Sequence[str]) -> int:
    n = 0
    for x, y in zip(a, b, strict=False):
        if x != y:
            break
        n += 1
    return n


def _is_blind(spec_phases: Mapping[str, Any], segments: Sequence[str]) -> bool:
    last = segments[-1] if segments else ""
    if any(last.startswith(p) for p in BLIND_SUBSTAGES):
        return True
    return isinstance(spec_phases.get(last), Blind)


class Visibility:
    """The visibility rules over one run's store."""

    def __init__(self, spec: RoundSpec, store: RunStore, phases: Mapping[str, Any],
                 inputs: Mapping[str, Any]):
        self.spec = spec
        self.store = store
        self.phases = phases
        self.inputs = inputs
        #: Only rows written before this step number are visible, and only
        #: phases closed before it count as closed: a replay must see the
        #: store as it was at that point, not as it is after the interruption.
        self.horizon: int | None = None

    def _before(self, seq: int | None) -> bool:
        return seq is not None and (self.horizon is None or seq <= self.horizon)

    def _closed(self, path: str) -> bool:
        rec = self.store.get_phase(path)
        return rec is not None and rec.status == "closed" and self._before(rec.closed_seq)

    # -- the rule ------------------------------------------------------------

    def check(self, r: Receipt, pos: Position) -> str:
        """``ok``, ``scope`` (another map item's: silently not this reader's),
        ``open`` (a phase it sits in has not closed: not handed) or ``leak``
        (the deepest phase reader and row share is blind and open: raise)."""
        if not in_scope(r.phase_path, pos.segments):
            return "scope"
        n = _common(r.phase_path, pos.segments)
        shared = r.phase_path[:n]
        if n and _is_blind(self.phases, shared) and not self._closed("/".join(shared)):
            return "leak"
        for i in range(n + 1, len(r.phase_path) + 1):
            if not self._closed("/".join(r.phase_path[:i])):
                return "open"
        return "ok"

    def _targets(self, ref: Ref) -> list[Receipt]:
        tgt = ref.target
        rs = [r for r in self.store.receipts()
              if self.horizon is None or r.seq < self.horizon]
        if ref.kind in ("node", "field") and tgt in self.spec.seats:
            return [r for r in rs if r.seat == tgt]
        if ref.kind == "tally":
            return [r for r in rs if r.seat == tgt and r.row_type == "tally/1"]
        return [r for r in rs if tgt in r.phase_path]

    @staticmethod
    def _latest(rs: list[Receipt], *, per_instance: bool) -> list[Receipt]:
        """Collapse attempts: the last attempt per (seat, candidate, instance
        path) — or, for a seat reference, per (seat, candidate, map item)."""
        best: dict[tuple[Any, ...], Receipt] = {}
        for r in rs:
            where: Any = r.path if per_instance else tuple(sorted(map_scope(r.phase_path).items()))
            k = (r.seat, r.candidate or "", where)
            if k not in best or r.seq > best[k].seq:
                best[k] = r
        return sorted(best.values(), key=lambda r: r.seq)

    def resolve(self, ref: Ref, reader: str, pos: Position) -> list[Receipt]:
        """The receipts a reference reaches from ``pos`` (raises on a leak)."""
        found: list[Receipt] = []
        refused: list[str] = []
        for r in self._targets(ref):
            if r.seat == reader and r.path == pos.path:
                continue  # a seat is never handed its own attempts at this step
            verdict = self.check(r, pos)
            if verdict == "leak":
                refused.append(r.key)
            elif verdict == "ok":
                found.append(r)
        if refused:
            raise VisibilityRefused(
                f"{reader!r} at {pos.path} may not read {ref.raw!r}: {len(refused)} row(s) sit "
                "in a blind phase that has not closed")
        found = self._latest(found, per_instance=True)
        if ref.kind in ("node", "field") and ref.target in self.spec.seats:
            found = self._latest(found, per_instance=False)
        node = self.phases.get(ref.target)
        if ref.kind == "node" and isinstance(node, Escalate):
            to = [r for r in found if r.seat == node.to and r.status == "sealed" and r.released]
            found = to[-1:] if to else [r for r in found if r.seat != node.to]
        if ref.kind == "node" and isinstance(node, Panel):
            found = [r for r in found if r.row_type == "tally/1"]
        if ref.kind == "others":
            own = map_scope(pos.segments)
            found = [r for r in found
                     if not (r.seat == reader and map_scope(r.phase_path).items() <= own.items())]
        return found

    # -- handing -------------------------------------------------------------

    def hand(self, r: Receipt, read: ReadSpec, ref: Ref, seat: Seat | None) -> Handed:
        item = next(iter(map_scope(r.phase_path).values()), None)
        if not (r.status == "sealed" and r.released):
            return Handed(read.from_, r.key, r.seat, r.row_type, None, item, None,
                          absent=r.failure or r.status)
        payload: Any = r.payload
        if seat is not None and isinstance(payload, Mapping):
            cut = {w.split(".", 1)[1] for w in seat.withheld if w.split(".", 1)[0] == r.seat}
            if cut:
                payload = {k: v for k, v in payload.items() if k not in cut}
        if ref.kind == "field" and isinstance(payload, Mapping):
            payload = {ref.field: payload.get(ref.field or "")}
        h = Handed(read.from_, r.key, r.seat, r.row_type, payload, item, r.sha256)
        if read.view == "sentence_table":
            return Handed(read.from_, r.key, r.seat, "sentence_table/1", table_view(h), item,
                          r.sha256)
        if read.view == "prose":
            return Handed(read.from_, r.key, r.seat, None, {"prose": r.prose}, item, r.sha256)
        return h

    def inputs_for(self, seat_id: str, reads: Sequence[ReadSpec], pos: Position) -> list[Handed]:
        seat = self.spec.seats.get(seat_id)
        out: list[Handed] = []
        for read in reads:
            ref = read.ref
            if ref.kind == "input":
                out.append(Handed(read.from_, f"input.{ref.target}", None, None,
                                  self.inputs.get(ref.target)))
                continue
            if ref.kind == "item":
                if ref.target not in pos.items:
                    raise VisibilityRefused(f"{seat_id!r}: no map item {ref.target!r} here")
                out.append(Handed(read.from_, f"item.{ref.target}", None, None,
                                  pos.items[ref.target]))
                continue
            for r in self.resolve(ref, seat_id, pos):
                out.append(self.hand(r, read, ref, seat))
        return out
