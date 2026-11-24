# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Judge calibration records, and the HOST RULE built on them (lane G-6):
a judge whose measured planted-error catch rate is under the floor the
statement verifier uses may not gate.

A record says how ONE judge — a card (by its sha256) on a plane, writing a
row type under a rendering version (:data:`judge_render.RENDER_VERSION` for
``verdict/2``) — did on the planted set (:mod:`.judge_planted`): catch and
false-reject counts with Wilson intervals. The rule, applied by the
interpreter at every panel (both hosts run the interpreter):

* a judge MAY GATE only with a record whose catch rate is at least
  :data:`CATCH_FLOOR` (0.90, ``statement_planted.CATCH_FLOOR``) and whose
  false-reject rate is at most :data:`FALSE_REJECT_CEILING` (0.15, the same
  module's ceiling: a judge that rejects everything "catches" everything);
* a ``gates_first`` judge that may not gate has its removals recorded as
  ADVISORY, not applied;
* a voting judge that may not gate is left out of the count (recorded as
  advisory, and its absence is what the quorum rule sees).

The book is read from a file the host is given (the harness: a path under
``planning/``; results are run data and stay out of the repo). An interpreter
given no book records ``calibration: not_enforced`` on every tally it writes,
so a run that skipped the rule says so. Pure.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..statement_planted import CATCH_FLOOR, FALSE_REJECT_CEILING

BOOK_SCHEMA = "legba.rounds.judge_calibration/1"


def card_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Standing:
    """What the rule says about one judge seat at one panel."""

    may_gate: bool
    reason: str
    record: Mapping[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"may_gate": self.may_gate, "reason": self.reason}
        if self.record is not None:
            for k in ("catch", "false_reject", "measured_at", "planted_set"):
                if k in self.record:
                    out[k] = self.record[k]
        return out


def _rate(r: Any) -> float | None:
    if isinstance(r, Mapping):
        v = r.get("rate")
        return float(v) if isinstance(v, (int, float)) else None
    return float(r) if isinstance(r, (int, float)) else None


def judge(record: Mapping[str, Any] | None, *, floor: float = CATCH_FLOOR,
          ceiling: float = FALSE_REJECT_CEILING) -> Standing:
    """The rule over one record."""
    if record is None:
        return Standing(False, "not_calibrated")
    catch, fr = _rate(record.get("catch")), _rate(record.get("false_reject"))
    if catch is None or fr is None:
        return Standing(False, "calibration_incomplete", record)
    if catch < floor:
        return Standing(False, "catch_below_floor", record)
    if fr > ceiling:
        return Standing(False, "false_reject_above_ceiling", record)
    return Standing(True, "calibrated", record)


@dataclass(frozen=True)
class CalibrationBook:
    """Records keyed by (card sha256, plane, row type, render)."""

    records: tuple[Mapping[str, Any], ...] = ()
    floor: float = CATCH_FLOOR
    ceiling: float = FALSE_REJECT_CEILING
    source: str | None = None
    _index: Mapping[tuple[str, str, str, str], Mapping[str, Any]] = field(
        default_factory=dict, compare=False, repr=False)

    @staticmethod
    def key(card_sha: str, plane: str, row_type: str, render: str) -> tuple[str, str, str, str]:
        return (card_sha, plane, row_type, render)

    @classmethod
    def of(cls, records: Iterable[Mapping[str, Any]], *, source: str | None = None) -> CalibrationBook:
        recs = tuple(records)
        idx: dict[tuple[str, str, str, str], Mapping[str, Any]] = {}
        for r in recs:  # the latest record of a key wins
            idx[cls.key(str(r.get("card_sha256")), str(r.get("plane")), str(r.get("row_type")),
                        str(r.get("render") or ""))] = r
        return cls(recs, source=source, _index=idx)

    @classmethod
    def load(cls, path: str | Path) -> CalibrationBook:
        p = Path(path)
        if not p.is_file():
            return cls.of((), source=str(p))
        raw = json.loads(p.read_text(encoding="utf-8"))
        if raw.get("schema") != BOOK_SCHEMA:
            raise ValueError(f"{p}: not a judge calibration book ({raw.get('schema')!r})")
        return cls.of(raw.get("records") or (), source=str(p))

    def record_for(self, card_sha: str | None, plane: str | None, row_type: str,
                   render: str) -> Mapping[str, Any] | None:
        if not card_sha or not plane:
            return None
        return self._index.get(self.key(card_sha, plane, row_type, render))

    def standing(self, card_sha: str | None, plane: str | None, row_type: str,
                 render: str) -> Standing:
        return judge(self.record_for(card_sha, plane, row_type, render),
                     floor=self.floor, ceiling=self.ceiling)


def save_book(path: str | Path, records: Iterable[Mapping[str, Any]]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps({"schema": BOOK_SCHEMA, "floor": CATCH_FLOOR,
                               "false_reject_ceiling": FALSE_REJECT_CEILING,
                               "records": list(records)}, indent=1, sort_keys=True, default=str),
                   encoding="utf-8")
    tmp.replace(p)


__all__ = ["BOOK_SCHEMA", "CATCH_FLOOR", "FALSE_REJECT_CEILING", "CalibrationBook", "Standing",
           "card_sha256", "judge", "save_book"]
