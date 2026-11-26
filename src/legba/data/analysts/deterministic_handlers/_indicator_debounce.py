# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The ``indicator_tracker`` flip debounce — pure code, no I/O.

The tracker used to diff the TWO most recent runs of every unit stream on every
heartbeat and re-publish the whole flip set whenever any stream moved: over
seven days 265 receipts carried 11,517 flip mentions for 684 distinct flips
(about 17 repetitions each), and the distinct flips were nearly symmetric
(346 up, 338 down) because nothing held a status still long enough to call it a
change. This module is the replacement core. It walks each stream's runs in
order, once, and keeps two records apart:

``raw``
    every status change between two consecutive runs of one indicator — the
    old pairwise diff, kept for the audit and never fed.

``debounced``
    a change of the indicator's CONFIRMED status. A status is confirmed once it
    has HELD: observed in ``hysteresis_runs`` consecutive runs (default 2), or
    across consecutive runs spanning at least ``hysteresis_hours`` (default:
    one cadence of the source unit). A debounced flip goes from the previously
    confirmed status to the newly held one, so an indicator that toggles
    triggered / not_observed every run never confirms anything and produces raw
    records only; one that wobbles and then settles produces exactly one flip,
    which carries the number of raw toggles it took (``raw_flips_between``).
    The first status an indicator ever holds is its baseline, not a flip.

State is per stream and per indicator LINEAGE: a lineage is one indicator
followed from run to run by the tracker's existing join (the id slug when the
two runs share any id, else the normalised statement — the DQ P6 fallback for
units that re-mint their slugs). An indicator missing from a run ends its
lineage; a later reappearance starts a new one with no prior status, exactly as
a newly introduced id was never a flip.

Every identity a flip carries comes from the source rows it was read from (the
onset run's slug, the runs' finding ids), never from the window the walk
started in, so the same history read twice yields the same keys. That is what
lets the store's unique index make a re-run write nothing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable, Mapping

#: Default runs a status must be observed in, consecutively, to be held.
DEFAULT_HYSTERESIS_RUNS = 2

_WS_RE = re.compile(r"\s+")
_NONWORD_RE = re.compile(r"[^a-z0-9 ]+")


def normalize_statement(statement: Any) -> str:
    """Lowercased, punctuation-stripped, whitespace-collapsed statement key."""
    s = str(statement or "").strip().lower()
    s = _NONWORD_RE.sub(" ", s)
    return _WS_RE.sub(" ", s).strip()


def is_activation(from_status: str, to_status: str) -> bool:
    """A flip INTO ``triggered`` from a non-triggered state."""
    return to_status == "triggered" and from_status != "triggered"


def join_runs(
    prev: list[Mapping[str, Any]], curr: list[Mapping[str, Any]],
) -> tuple[list[tuple[int, int]], str]:
    """Pair indicator entries of two consecutive runs.

    Returns ``([(prev_index, curr_index), ...], match)``. The join prefers the
    id slug; when the two runs share NO id at all it falls back to the
    normalised statement (``match='statement'``). On a duplicate key the last
    entry wins, as it always has.
    """
    def _by(entries: list[Mapping[str, Any]], key) -> dict[str, int]:
        out: dict[str, int] = {}
        for i, e in enumerate(entries):
            k = key(e)
            if k:
                out[k] = i
        return out

    p_id = _by(prev, lambda e: str(e.get("id") or ""))
    c_id = _by(curr, lambda e: str(e.get("id") or ""))
    if set(p_id) & set(c_id):
        pj, cj, match = p_id, c_id, "id"
    else:
        pj = _by(prev, lambda e: normalize_statement(e.get("statement")))
        cj = _by(curr, lambda e: normalize_statement(e.get("statement")))
        match = "statement"
    pairs = [(pj[k], ci) for k, ci in cj.items() if k in pj]
    return pairs, match


# ---------------------------------------------------------------------------
# Per-stream state
# ---------------------------------------------------------------------------


@dataclass
class Lineage:
    """One indicator followed through a stream, as of the last run walked."""

    id: str                     # the slug in the last run
    statement: str
    status: str                 # the status in the last run
    block_runs: int             # consecutive runs in ``status``
    onset_id: str               # first run of the current block
    onset_at: datetime
    onset_slug: str             # the slug at the onset run (the flip's key)
    before_onset_id: str | None  # the run before the onset (the old status)
    last_at: datetime           # newest run of the current block
    confirmed: str | None = None
    raw_since: int = 0          # raw toggles since ``confirmed`` last held

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id, "statement": self.statement, "status": self.status,
            "block_runs": self.block_runs, "onset_id": self.onset_id,
            "onset_at": self.onset_at.isoformat(), "onset_slug": self.onset_slug,
            "before_onset_id": self.before_onset_id,
            "last_at": self.last_at.isoformat(), "confirmed": self.confirmed,
            "raw_since": self.raw_since,
        }

    @classmethod
    def from_json(cls, d: Mapping[str, Any]) -> "Lineage":
        return cls(
            id=str(d["id"]), statement=str(d.get("statement") or ""),
            status=str(d["status"]), block_runs=int(d["block_runs"]),
            onset_id=str(d["onset_id"]),
            onset_at=datetime.fromisoformat(str(d["onset_at"])),
            onset_slug=str(d.get("onset_slug") or d["id"]),
            before_onset_id=d.get("before_onset_id"),
            last_at=datetime.fromisoformat(str(d["last_at"])),
            confirmed=d.get("confirmed"), raw_since=int(d.get("raw_since") or 0),
        )


@dataclass
class StreamState:
    """The persisted watermark of one ``(source analyst, target)`` stream."""

    last_finding_id: str | None = None
    last_produced_at: datetime | None = None
    lineages: list[Lineage] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "v": 1,
            "last_finding_id": self.last_finding_id,
            "last_produced_at": (
                self.last_produced_at.isoformat() if self.last_produced_at else None
            ),
            "lineages": [lin.to_json() for lin in self.lineages],
        }

    @classmethod
    def from_json(cls, d: Mapping[str, Any] | None) -> "StreamState":
        if not d:
            return cls()
        at = d.get("last_produced_at")
        return cls(
            last_finding_id=d.get("last_finding_id"),
            last_produced_at=datetime.fromisoformat(str(at)) if at else None,
            lineages=[Lineage.from_json(x) for x in d.get("lineages") or []],
        )


def _held(lin: Lineage, runs: int, hours: float | None) -> str | None:
    """``'runs'`` / ``'hours'`` when the current block has held, else None."""
    if lin.block_runs >= runs:
        return "runs"
    if hours and hours > 0:
        span_h = (lin.last_at - lin.onset_at).total_seconds() / 3600.0
        if span_h >= hours:
            return "hours"
    return None


def advance_stream(
    state: StreamState,
    runs: Iterable[Mapping[str, Any]],
    *,
    target_id: str | None,
    analyst_id: str | None,
    hysteresis_runs: int = DEFAULT_HYSTERESIS_RUNS,
    hysteresis_hours: float | None = None,
) -> tuple[StreamState, list[dict[str, Any]], list[dict[str, Any]]]:
    """Walk ``runs`` (oldest first; each ``{'id', 'produced_at', 'indicators'}``
    with cleaned indicator entries) on from ``state``.

    Returns ``(new_state, raw_flips, debounced_flips)``. ``state`` is not
    mutated. A run at or before the state's watermark is skipped, so handing
    the same runs back in is a no-op.
    """
    n_runs = max(1, int(hysteresis_runs))
    st = StreamState.from_json(state.to_json())
    raw: list[dict[str, Any]] = []
    debounced: list[dict[str, Any]] = []
    for run in runs:
        rid = str(run["id"])
        at: datetime = run["produced_at"]
        if st.last_produced_at is not None and (
            (at, rid) <= (st.last_produced_at, str(st.last_finding_id))
        ):
            continue
        entries = list(run.get("indicators") or [])
        prev_entries = [
            {"id": lin.id, "statement": lin.statement} for lin in st.lineages
        ]
        pairs, match = join_runs(prev_entries, entries)
        joined = {ci: pi for pi, ci in pairs}
        nxt: list[Lineage] = []
        for ci, e in enumerate(entries):
            slug, status = str(e["id"]), str(e["status"])
            stmt = str(e.get("statement") or "")[:2048]
            pi = joined.get(ci)
            if pi is None:
                lin = Lineage(
                    id=slug, statement=stmt, status=status, block_runs=1,
                    onset_id=rid, onset_at=at, onset_slug=slug,
                    before_onset_id=None, last_at=at,
                )
            else:
                lin = st.lineages[pi]
            if pi is None:
                pass
            elif status == lin.status:
                lin.block_runs += 1
                lin.last_at = at
            else:
                raw.append({
                    "grade": "raw", "target_id": target_id,
                    "source_analyst_id": analyst_id, "indicator_id": slug,
                    "statement": stmt or lin.statement,
                    "from_status": lin.status, "to_status": status,
                    "activation": is_activation(lin.status, status),
                    "match": match,
                    "citations": e.get("citations") if isinstance(e.get("citations"), list) else [],
                    "evidence_finding_id": rid, "evidence_at": at,
                    "prev_finding_id": st.last_finding_id,
                })
                lin.raw_since += 1
                lin.status, lin.block_runs = status, 1
                lin.onset_id, lin.onset_at, lin.onset_slug = rid, at, slug
                lin.before_onset_id = st.last_finding_id
                lin.last_at = at
            lin.id, lin.statement = slug, stmt or lin.statement
            how = _held(lin, n_runs, hysteresis_hours)
            if how is not None:
                if lin.confirmed is None or lin.confirmed == lin.status:
                    lin.confirmed, lin.raw_since = lin.status, 0
                else:
                    debounced.append({
                        "grade": "debounced", "target_id": target_id,
                        "source_analyst_id": analyst_id,
                        "indicator_id": lin.onset_slug,
                        "statement": lin.statement,
                        "from_status": lin.confirmed, "to_status": lin.status,
                        "activation": is_activation(lin.confirmed, lin.status),
                        "match": match,
                        "citations": e.get("citations") if isinstance(e.get("citations"), list) else [],
                        "evidence_finding_id": lin.onset_id,
                        "evidence_at": lin.onset_at,
                        "prev_finding_id": lin.before_onset_id,
                        "confirmed_finding_id": rid, "confirmed_at": at,
                        "confirmed_by": how, "held_runs": lin.block_runs,
                        "held_hours": round(
                            (lin.last_at - lin.onset_at).total_seconds() / 3600.0, 3),
                        "raw_flips_between": lin.raw_since,
                    })
                    lin.confirmed, lin.raw_since = lin.status, 0
            nxt.append(lin)
        st.lineages = nxt
        st.last_finding_id, st.last_produced_at = rid, at
    return st, raw, debounced


__all__ = [
    "DEFAULT_HYSTERESIS_RUNS",
    "Lineage",
    "StreamState",
    "advance_stream",
    "is_activation",
    "join_runs",
    "normalize_statement",
]
