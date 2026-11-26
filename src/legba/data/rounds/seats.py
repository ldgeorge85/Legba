# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The seat-call contract and the seat providers a host injects.

The interpreter never calls a model. It hands each seat a :class:`SeatCall`
(the seat's definition, its card, exactly the inputs the store released to
it) and takes back a :class:`SeatReply` (a payload of the seat's row type,
optional prose, optional raw text, and — for a synthetic or scripted seat —
the seconds the call is to be charged). A provider is any callable
``SeatCall -> SeatReply``; raising :class:`SeatFailure` (or any exception) is
a failed attempt.

Providers here:

* :func:`pure_code` — the pure code functions of :mod:`.tables`, run as is;
* :class:`SyntheticSeats` — deterministic synthetic replies for every row
  type (cites only handed rows, quotes their text verbatim), with knobs for
  durations, failures and judge votes; what the harness runs with no script;
* :class:`ScriptedSeats` — replies read from a script file (per seat: one
  reply, or a list by execution ordinal), falling back to another provider;
* :class:`Dispatch` — routes code seats with a pure function to it, and
  everything else to the injected provider. A substrate code function with no
  provider raises :class:`SeatNotBuilt`.

Model seats (:mod:`.model_seats`) and the substrate code seats
(:mod:`.substrate_seats`) are other providers with the same signature.
"""
from __future__ import annotations

import hashlib
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from .rows import ROW_TYPES, sentences_of
from .spec import Seat, load_yaml
from .tables import PURE_FNS, Handed, candidates_of, evidence_rows


class SeatFailure(Exception):
    """A seat attempt that failed, with a short reason."""

    def __init__(self, reason: str, raw: str | None = None):
        super().__init__(reason)
        self.reason = reason
        self.raw = raw


class SeatNotBuilt(RuntimeError):
    """A run reached a code function no host has implemented."""


@dataclass(frozen=True)
class SeatCall:
    run_id: str
    seat_id: str
    seat: Seat
    row_type: str
    attempt: int
    ordinal: int  # how many earlier executions this seat has in the run
    path: str
    scope: Mapping[str, str]
    inputs: tuple[Handed, ...]
    card: str | None = None
    card_text: str | None = None
    item: Any = None
    candidate: str | None = None
    candidates: tuple[str, ...] = ()
    turn: int | None = None
    iteration: int | None = None
    #: ``repair: reasons`` seats only, on a retry: the refused attempt's typed
    #: failure (:func:`legba.data.rounds.repair.repair_note`). ``None`` on a
    #: first attempt and for every other seat.
    repair: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class SeatReply:
    """A seat's answer. ``meta`` is what a model seat's calls cost and ran on
    (model, thinking, tokens, durations; recorded on the receipt as ``call``);
    ``tool_rows`` are substrate rows the seat's own tool calls returned (handed
    to the gates beside the store's inputs, and listed on the receipt);
    ``tools_bound`` says the seat's declared tools were really offered."""

    payload: Any = None
    prose: str | None = None
    raw: str | None = None
    elapsed_s: float | None = None
    meta: Mapping[str, Any] | None = None
    tool_rows: tuple[Mapping[str, Any], ...] = ()
    tools_bound: bool = False


SeatFn = Callable[[SeatCall], SeatReply]


class Clock(Protocol):
    def now(self) -> datetime: ...
    def monotonic(self) -> float: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def monotonic(self) -> float:
        return time.monotonic()


@dataclass
class FrozenClock:
    """A clock that never moves (deterministic records in tests)."""

    at: datetime = field(default_factory=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc))

    def now(self) -> datetime:
        return self.at

    def monotonic(self) -> float:
        return 0.0


def pure_code(call: SeatCall) -> SeatReply:
    fn = PURE_FNS.get(call.seat.fn or "")
    if fn is None:
        raise SeatNotBuilt(f"code function {call.seat.fn!r} has no implementation here")
    return SeatReply(fn(list(call.inputs)), elapsed_s=0.0)


@dataclass
class Dispatch:
    """Pure code seats run in-process; everything else goes to ``provider``."""

    provider: SeatFn | None = None

    def __call__(self, call: SeatCall) -> SeatReply:
        if call.seat.kind == "code" and (call.seat.fn or "") in PURE_FNS:
            return pure_code(call)
        if self.provider is None:
            raise SeatNotBuilt(f"no provider for seat {call.seat_id!r} "
                               f"({call.seat.kind} {call.seat.fn or call.seat.card})")
        return self.provider(call)


# ---------------------------------------------------------------------------
# Synthetic seats
# ---------------------------------------------------------------------------


def _h(*parts: Any) -> int:
    return int(hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()[:8], 16)


def _quote(text: str, words: int = 6) -> str:
    return " ".join(str(text).split()[:words])


def synthetic_rows(subject: str, n: int = 4) -> list[dict[str, Any]]:
    """Neutral synthetic evidence rows (no domain): what a synthetic
    ``scope_and_freeze`` hands the round."""
    return [{"id": f"row-{i}", "kind": "signal",
             "text": f"Synthetic source {i} reports that the subject {subject} shows state {i} "
                     "in the period under review",
             "source_id": f"source-{i}", "origin_group_id": f"group-{i % 3}",
             "access_class": "public", "required": i == 0} for i in range(n)]


@dataclass
class SyntheticSeats:
    """Deterministic synthetic seats for every registered row type.

    * ``elapsed`` — seconds charged per call: a constant, or a function of the
      call (default 1 s);
    * ``fail`` — ``{seat_id: n}``: the first n attempts of that seat fail;
    * ``late`` — seats whose every attempt runs past its deadline;
    * ``votes`` — ``{judge_id: outcome}``; default ``stands`` (judges rank
      candidates in their given order, rotated by judge for Borda);
    * ``diagnostic_after`` — assessment passes before ``diagnostic`` is true;
    * ``separated`` — whether the matrix the assessors imply separates.
    """

    elapsed: float | Callable[[SeatCall], float] = 1.0
    fail: Mapping[str, int] = field(default_factory=dict)
    late: Sequence[str] = ()
    votes: Mapping[str, str] = field(default_factory=dict)
    diagnostic_after: int = 2
    concede_after: int | None = None
    settle: bool = False
    marker: bool = True

    def _elapsed(self, call: SeatCall) -> float:
        if call.seat_id in self.late:
            return float(call.seat.effective_deadline_s + 1)
        return float(self.elapsed(call) if callable(self.elapsed) else self.elapsed)

    def __call__(self, call: SeatCall) -> SeatReply:
        if call.attempt < self.fail.get(call.seat_id, 0):
            raise SeatFailure("synthetic failure")
        payload = self.payload(call)
        return SeatReply(payload, prose=f"synthetic prose of {call.seat_id}",
                         elapsed_s=self._elapsed(call))

    # -- payloads ------------------------------------------------------------

    def _sentences(self, call: SeatCall, n: int = 2) -> list[dict[str, Any]]:
        """Sentences that pass the honesty gates: digit-free text, each citing
        one handed row with a verbatim quote of its text — or, with no rows
        handed, a row an upstream sentence cited, or the upstream output
        itself. The key carries ``seat@path`` (the blind-prompt marker)."""
        rows = list(evidence_rows(call.inputs).values())
        upstream = [r for h in call.inputs for s in sentences_of(h.payload)
                    for r in s.get("support") or ()]
        outputs = [h.ref_id for h in call.inputs if h.seat is not None and not h.absent]
        tag = f"{call.seat_id}@{call.path}" if self.marker else call.seat_id
        out = []
        for i in range(n):
            s: dict[str, Any] = {"key": f"{tag}#{i}",
                                 "text": f"A synthetic sentence written by the {call.seat.kind} seat.",
                                 "stance": ("supports", "opposes", "neutral")[_h(tag, i, "s") % 3]}
            if rows:
                row = rows[_h(tag, i) % len(rows)]
                s["support"] = [{"ref_id": row["id"], "role": "evidence",
                                 "kind": row.get("kind", "signal"),
                                 "quote": _quote(row.get("text") or "")}]
            elif upstream:
                s["support"] = [dict(upstream[_h(tag, i) % len(upstream)])]
            elif outputs:
                s["support"] = [{"ref_id": outputs[_h(tag, i) % len(outputs)], "role": "context",
                                 "kind": "row"}]
            out.append(s)
        return out

    def payload(self, call: SeatCall) -> dict[str, Any]:  # noqa: C901 - one case per row type
        rt = call.row_type
        subject = next((h.payload for h in call.inputs if h.read == "input.subject"), "subject")
        if rt == "frozen_scope/1":
            as_of = next((h.payload for h in call.inputs if h.read == "input.as_of"), "")
            return {"subject": str(subject), "as_of": str(as_of), "rows": synthetic_rows(str(subject))}
        rows = list(evidence_rows(call.inputs).values())
        if rt == "seat_slices/1":
            ids = tuple(r["id"] for r in rows)
            return {"core": ids, "slices": {}, "rows": rows}
        if rt == "context_pack/1":
            return {"row_ids": [r["id"] for r in rows]}
        if rt == "question_set/1":
            return {"questions": [{"key": f"q{i}", "text": f"Question {i}?"} for i in range(3)],
                    "working_reading": f"working reading of {call.seat_id}@{call.path}"}
        if rt == "disposition_set/1":
            return {"dispositions": [{"question": "q0", "disposition": "supported",
                                      "sentences": self._sentences(call)}]}
        if rt == "evidence_rows/1":
            return {"rows": rows[:2], "sentences": self._sentences(call, 1)}
        if rt == "series_panel/1":
            return {"series": [{"key": "s0", "label": "synthetic series", "values": [1.0, 2.0]}]}
        if rt in ("position/1", "turn/1"):
            out: dict[str, Any] = {"sentences": self._sentences(call)}
            if rt == "turn/1" and self.concede_after is not None \
                    and (call.turn or 0) >= self.concede_after:
                out["concede"] = True
            if rt == "position/1":
                out["probability"] = round((_h(call.seat_id, call.path) % 90 + 5) / 100, 2)
            return out
        if rt == "challenge/1":
            return {"sentences": self._sentences(call),
                    "challenges": [{"key": "C1", "text": "The positions leave out the quiet sources."}]}
        if rt in ("verdict/1", "verdict/2"):
            return self._verdict(call)
        if rt == "weakest_link/1":
            cands = candidates_of(call.inputs)
            return {"candidate": cands[0] if cands else None, "reason": "weakest_link"}
        if rt == "blind_spots/1":
            return {"absences": [{"kind": "source_quiet", "what": "synthetic gap"}]}
        if rt == "assumption_set/1":
            return {"assumptions": [{"key": "a0", "text": "Synthetic assumption.",
                                     "status": "unstated"}]}
        if rt == "hypothesis_set/1":
            return {"hypotheses": [{"id": f"h{i}", "text": f"Hypothesis {i}."} for i in range(3)]}
        if rt == "assessment/1":
            hyp = call.item.get("id") if isinstance(call.item, Mapping) else str(call.item)
            passes = call.iteration or 1
            marks = [{"ref_id": r["id"], "mark": ("consistent", "inconsistent")[_h(hyp, r["id"]) % 2]}
                     for r in rows[:3]]
            return {"hypothesis": str(hyp), "marks": marks,
                    "diagnostic": int(passes) >= self.diagnostic_after}
        if rt in ("ruling/1", "synthesis/1", "read/1"):
            sents = self._sentences(call)
            probs = [p for h in call.inputs for p in _probabilities(h.payload)]
            if probs and rt == "synthesis/1":
                sents[0]["probability"] = probs[0]
            out = {"sentences": sents}
            if rt in ("ruling/1", "synthesis/1", "read/1"):
                out["dissent"] = self._sentences(call, 1)
            return out
        if rt == "statement_proposals/1":
            return {"proposals": []}
        if rt == "concede/1":
            return {"to": None}
        if rt in ROW_TYPES:
            return {}
        raise SeatFailure(f"no synthetic payload for {rt}")

    def _verdict(self, call: SeatCall) -> dict[str, Any]:
        cands = [call.candidate] if call.candidate else list(call.candidates)
        outcome = self.votes.get(call.seat_id, "stands")
        k = _h(call.seat_id) % max(1, len(cands))
        out: dict[str, Any] = {
            "verdicts": [{"candidate": c, "outcome": outcome, "reason": "supported"} for c in cands],
            "ranking": cands[k:] + cands[:k],
        }
        if self.settle:
            out["settled"] = True
        return out


def _probabilities(obj: Any) -> list[float]:
    out: list[float] = []
    if isinstance(obj, Mapping):
        for k, v in obj.items():
            if k == "probability" and isinstance(v, (int, float)) and not isinstance(v, bool):
                out.append(float(v))
            else:
                out.extend(_probabilities(v))
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            out.extend(_probabilities(v))
    return out


# ---------------------------------------------------------------------------
# Scripted seats
# ---------------------------------------------------------------------------


@dataclass
class ScriptedSeats:
    """Replies from a script: ``{seat_id: reply | [reply, ...]}`` where a
    reply is ``{payload, prose, raw, elapsed_s}`` or ``{fail: reason}``. A
    list is indexed by the seat's execution ordinal (deterministic on
    resume); the last entry repeats. Seats not in the script go to
    ``fallback``."""

    script: Mapping[str, Any]
    fallback: SeatFn | None = None

    @classmethod
    def from_file(cls, path: str | Path, fallback: SeatFn | None = None) -> ScriptedSeats:
        return cls(load_yaml(Path(path).read_text(encoding="utf-8")) or {}, fallback)

    def __call__(self, call: SeatCall) -> SeatReply:
        entry = self.script.get(call.seat_id)
        if entry is None:
            if self.fallback is None:
                raise SeatFailure(f"no script entry for {call.seat_id!r}")
            return self.fallback(call)
        if isinstance(entry, list):
            entry = entry[min(call.ordinal + call.attempt, len(entry) - 1)] if entry else {}
        if not isinstance(entry, Mapping):
            raise SeatFailure(f"script entry for {call.seat_id!r} is not a mapping")
        if entry.get("fail"):
            raise SeatFailure(str(entry["fail"]), entry.get("raw"))
        return SeatReply(entry.get("payload"), entry.get("prose"), entry.get("raw"),
                         entry.get("elapsed_s"))
