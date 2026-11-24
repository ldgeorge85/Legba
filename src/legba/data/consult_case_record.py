# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The case RECORD composer and the registry's case-frame accumulator (C-1b; C-1a §3).

A case persists under the assistant turn's 0223 response bag, key ``case``
(``null`` = this turn presented no case). It is composed in exactly two
places, both here:

* :func:`compose_record` — from the GATE-ADMITTED actions, by the analyst at
  the end of a run that finished. Exact by construction: the gate is the only
  writer of ``seq``.
* :class:`CaseFrameAccumulator` — by the REGISTRY, from the ``case_*`` frames it
  watched go by, for a run that did not finish (stopped, failed, cancelled).
  The live stream is core NATS and drops under pressure, so the accumulator
  checks that ``seq`` runs 1..n with no hole: a missing frame makes the record
  ``complete: false, gap_at_seq: k`` and replay stops at the hole rather than
  running past it. After an acknowledged stop, every later case frame is kept
  OUT of the record (the step trace keeps it, marked ``after_stop``) — EXCEPT
  an action frame the workstation's stop claim covers (W10-F10): the
  workstation ran ``seq <= executed_through_seq`` before it stopped, so a frame
  for one of those steps that reaches the registry after the stop's ack (core
  NATS is not ordered against the stop route) still lands in the record.

A LEAF: it imports only :mod:`legba.data.workstation_vocabulary`, so the
registry (slim image, leaf imports only) can compose a record without the
analyst package. The record's shape is ``consult_case_schema.CaseRecord``; the
contract test pins that this module's output validates against it.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

from . import workstation_vocabulary as V

__all__ = ["ACTION_KEYS", "CaseFrameAccumulator", "compose_record"]

#: The fields one stored action carries, in order.
ACTION_KEYS: tuple[str, ...] = ("seq", "action", "args", "at", "cites", "rests_on")


def _now_z() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def compose_record(
    *,
    request_id: str | None,
    status: str,
    plane: str | None,
    refused: int,
    summary: str | None,
    actions: list[Mapping[str, Any]],
    as_of: str | None = None,
    stopped_after_seq: int | None = None,
    complete: bool = True,
    gap_at_seq: int | None = None,
) -> dict[str, Any]:
    """One ``case/1`` record (``consult_case_schema.CaseRecord.to_json()`` shape)."""
    if status not in V.CASE_STATUSES:
        raise ValueError(f"case status {status!r} is not one of {V.CASE_STATUSES}")
    return {
        "version": V.CASE_RECORD_VERSION,
        "request_id": request_id,
        "status": status,
        "as_of": as_of or _now_z(),
        "plane": plane,
        "vocabulary": V.vocabulary_hash(),
        "refused": int(refused),
        "stopped_after_seq": stopped_after_seq,
        "complete": bool(complete),
        "gap_at_seq": gap_at_seq,
        "summary": summary,
        "actions": [{k: a.get(k) for k in ACTION_KEYS} for a in actions],
    }


class CaseFrameAccumulator:
    """The registry's view of one case run's ``case_*`` frames.

    ``observe`` every step frame the run's subject carries (in arrival order);
    ``stop`` when the operator's stop is acknowledged; ``compose`` once the run
    is over. Not thread-safe — it lives on the registry's one event loop.
    """

    def __init__(self, *, request_id: str | None, plane: str | None) -> None:
        self.request_id = request_id
        self.plane = plane
        self._actions: dict[int, dict[str, Any]] = {}
        self.refused = 0
        self.sealed = False
        self.summary: str | None = None
        self.stopped_after_seq: int | None = None
        #: The workstation's stop claim (``executed_through_seq``), kept so a
        #: covered action frame arriving after the ack is still recorded.
        self._claim: int | None = None

    @property
    def stopped(self) -> bool:
        return self.stopped_after_seq is not None

    def contiguous_through(self) -> int:
        """The highest ``n`` such that seq 1..n all arrived."""
        n = 0
        while (n + 1) in self._actions:
            n += 1
        return n

    def observe(self, step: dict[str, Any]) -> None:
        """Fold one step frame in. Marks a post-stop case frame ``after_stop``."""
        if step.get("phase") != V.CASE_PHASE or step.get("kind") not in V.CASE_FRAME_KINDS:
            return
        kind = step["kind"]
        if kind == "case_stopped":
            return
        if self.stopped:
            if not self._admit_covered(step):
                step["after_stop"] = True
            return
        if kind == "case_action":
            seq = step.get("seq")
            if isinstance(seq, int) and seq >= 1 and seq not in self._actions:
                self._actions[seq] = {k: step.get(k) for k in ACTION_KEYS}
        elif kind == "case_refused":
            self.refused += 1
        elif kind == "case_sealed":
            self.sealed = True
            summary = step.get("summary")
            self.summary = str(summary) if summary is not None else None

    def _admit_covered(self, step: dict[str, Any]) -> bool:
        """W10-F10 — a post-ack action frame the stop claim covers.

        The workstation executes an action only on its frame, so a claim of
        ``n`` means seq 1..n were emitted before the stop. Their frames can
        still be in flight when the stop route acknowledges; the record keeps
        each one and ``stopped_after_seq`` rises to what the registry now holds
        (never past the claim — the W5-F6 clamp, applied to the final state).
        """
        seq = step.get("seq")
        if (
            step.get("kind") != "case_action"
            or self._claim is None
            or not isinstance(seq, int)
            or not 1 <= seq <= self._claim
        ):
            return False
        if seq not in self._actions:
            self._actions[seq] = {k: step.get(k) for k in ACTION_KEYS}
        self.stopped_after_seq = min(self._claim, max(self._actions))
        return True

    def stop(self, executed_through_seq: int | None) -> int:
        """Acknowledge the operator's stop; returns ``stopped_after_seq``.

        ``executed_through_seq`` is what the workstation actually ran; absent,
        the stop lands after the last contiguous frame the registry saw. A
        claim past the highest seq the registry actually saw is clamped to it
        (W5-F6): a stop body claiming 3 with no action frame records 0 — the
        record never names an action it holds no frame for.
        """
        if self.stopped_after_seq is None:
            seen = self.contiguous_through()
            if executed_through_seq is None:
                n = seen
            else:
                self._claim = max(0, int(executed_through_seq))
                highest = max(self._actions, default=0)
                n = min(self._claim, highest)
            self.stopped_after_seq = n
        return self.stopped_after_seq

    def compose(self) -> dict[str, Any]:
        """The record for a run that did NOT finish (or was stopped)."""
        limit = self.stopped_after_seq
        seqs = sorted(s for s in self._actions if limit is None or s <= limit)
        top = seqs[-1] if seqs else 0
        gap = next((s for s in range(1, top + 1) if s not in self._actions), None)
        kept = [self._actions[s] for s in range(1, (gap or top + 1))]
        if self.stopped:
            status = "stopped"
        elif self.sealed:
            status = "sealed"
        else:
            status = "unsealed"
        return compose_record(
            request_id=self.request_id,
            status=status,
            plane=self.plane,
            refused=self.refused,
            summary=self.summary,
            actions=kept,
            stopped_after_seq=self.stopped_after_seq,
            complete=gap is None,
            gap_at_seq=gap,
        )
