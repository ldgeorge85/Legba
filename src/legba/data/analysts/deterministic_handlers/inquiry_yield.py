# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``inquiry_yield`` sub-handler — the H12 weekly instrument over the ledger.

Program 5 lane 1 (planning/PROGRAM5_INQUIRY_DESIGN_2026-09-24.md §4): an
inquiry is scored by YIELD, never by correctness. Weekly, per descriptor,
this handler counts — DETERMINISTICALLY, off ``inquiry_ledger``
(migration 0215) alone plus the two mechanical cross-references §4 names —

  * hypotheses opened / confirmed / refuted / expired;
  * questions dispatched / answered;
  * observations ANTICIPATED — an observation whose ``cited_refs`` (set only
    at ``ledger_close``, see ``inquiry_state.py``) a LATER finding cites
    (``analyst_outputs.derived_from`` overlaps it), i.e. a desk independently
    carried forward what the inquiry had already flagged;
  * blind spots — an open, dispatched question whose target had NO read this
    cycle.

Published as a FINDING (never TRACE_ONLY — §4: "Published on the journal
panel beside the entry"), and never a gate: nothing downstream is blocked or
demoted by this number, it is read-only measurement. "An inquiry that opens
hypotheses it never tests scores as an inquiry that opens nothing" — the
arithmetic below is the whole enforcement of that sentence; there is no
separate penalty path.

Two properties, engineered in from receipt_anchor's own precedent:

  * the ARITHMETIC (:func:`compute_yield`) is PURE — no DB, no clock reads —
    so it is testable against a hand-built fixture ledger with no container;
  * :func:`handle` is the thin I/O shell: it reads the bounded slice off
    ``deps.pg_pool`` and hands it to :func:`compute_yield` unchanged.

SCOPE NOTE on blind spots (honest, not hidden): ``dispatched_to`` may name an
``open_question`` id, a ``fact_contention`` id, or a desk ``analyst_id`` (§3).
This instrument's "had a read" check only recognizes the desk-analyst_id /
target_id shape (an ``analyst_outputs`` row this cycle) — a question
dispatched into an open_question or a contention id will read as a standing
blind spot until it is closed. That is a real limitation of this METHOD_VERSION,
not a silent gap: it is named here and the arithmetic is agnostic to WHICH
kind of target a caller hands it (``read_targets`` is just a set of strings
the caller resolved).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Sequence

from ...provenance.models import FindingPayload
from ....runtime.analyst_method import AnalystMethodResult

logger = logging.getLogger(__name__)

SUB_HANDLER_NAME = "inquiry_yield"

#: H12 — the arithmetic's own revision: which ledger fields feed each counter,
#: the week window (weekly, ending at run time), and the two mechanical
#: cross-reference rules ("anticipated" / "blind spot"). Bump when any of
#: those move, so a yield diff across a change reads as an instrument
#: revision, never a change in what the inquiries actually did.
METHOD_VERSION = "inquiry_yield/2026-09.1"

#: The rolling window this instrument scores — a full week ending "now".
WINDOW_DAYS = 7

#: Bounds on the two DB reads `handle()` performs — a runaway ledger or a
#: runaway findings slice can never turn a weekly tick into a full-table
#: walk. Mirrors the `LIMIT 500` idiom forecast_acute already uses.
_MAX_LEDGER_ROWS = 20_000
_MAX_FINDING_ROWS = 5_000


# ---------------------------------------------------------------------------
# The pure arithmetic — no DB, no clock. See module docstring.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class YieldCounts:
    """One descriptor's weekly yield — every field a plain count (or, for
    ``blind_spots``, the named targets) over the window ``[week_start,
    week_end)``."""

    descriptor_id: str
    hypotheses_opened: int = 0
    hypotheses_confirmed: int = 0
    hypotheses_refuted: int = 0
    hypotheses_expired: int = 0
    questions_dispatched: int = 0
    questions_answered: int = 0
    observations_anticipated: int = 0
    blind_spots: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "descriptor_id": self.descriptor_id,
            "hypotheses_opened": self.hypotheses_opened,
            "hypotheses_confirmed": self.hypotheses_confirmed,
            "hypotheses_refuted": self.hypotheses_refuted,
            "hypotheses_expired": self.hypotheses_expired,
            "questions_dispatched": self.questions_dispatched,
            "questions_answered": self.questions_answered,
            "observations_anticipated": self.observations_anticipated,
            "blind_spots": list(self.blind_spots),
            "blind_spot_count": len(self.blind_spots),
        }


def _in_window(ts: datetime | None, start: datetime, end: datetime) -> bool:
    return ts is not None and start <= ts < end


def _overlaps_window(
    opened_at: datetime, closed_at: datetime | None, start: datetime, end: datetime,
) -> bool:
    """True when a row was OPEN at any instant inside ``[start, end)`` — it
    existed before the window closed, and (if closed at all) closed no
    earlier than the window opened."""
    if opened_at >= end:
        return False
    return closed_at is None or closed_at >= start


def compute_yield(
    ledger_rows: Sequence[Mapping[str, Any]],
    *,
    week_start: datetime,
    week_end: datetime,
    finding_refs: Sequence[tuple[datetime, frozenset[str]]] = (),
    read_targets: frozenset[str] = frozenset(),
) -> dict[str, YieldCounts]:
    """The H12 arithmetic, driven entirely off already-materialized rows.

    ``ledger_rows`` — one dict per ``inquiry_ledger`` row, each carrying at
    least ``descriptor_id``, ``kind``, ``status``, ``created_at``,
    ``closed_at``, ``dispatched_to``, ``cited_refs`` (a list of ref strings).

    ``finding_refs`` — ``(produced_at, ref_set)`` for every substrate finding
    produced in (or overlapping) the window — the mechanical half of
    "anticipated" (§4): an OBSERVATION closed inside the window counts once
    it it is closed AND some finding produced STRICTLY AFTER that close
    shares at least one ref with its (close-time-set) ``cited_refs``.

    ``read_targets`` — the set of "this target had activity this cycle"
    identifiers a caller resolved (see the module's blind-spot SCOPE NOTE);
    a dispatched, still-open question naming anything ELSE counts as a blind
    spot for this week.

    Returns one :class:`YieldCounts` per descriptor_id seen in
    ``ledger_rows`` — a descriptor with an empty ledger never appears (there
    is nothing to score).
    """
    by_descriptor: dict[str, list[Mapping[str, Any]]] = {}
    for row in ledger_rows:
        by_descriptor.setdefault(str(row["descriptor_id"]), []).append(row)

    out: dict[str, YieldCounts] = {}
    for descriptor_id, rows in by_descriptor.items():
        opened = confirmed = refuted = expired = 0
        dispatched = answered = 0
        anticipated = 0
        blind_spots: list[str] = []

        for row in rows:
            kind = row.get("kind")
            status = row.get("status")
            created_at = row["created_at"]
            closed_at = row.get("closed_at")

            if kind == "hypothesis":
                if _in_window(created_at, week_start, week_end):
                    opened += 1
                if _in_window(closed_at, week_start, week_end):
                    if status == "confirmed":
                        confirmed += 1
                    elif status == "refuted":
                        refuted += 1
                    elif status == "expired":
                        expired += 1

            elif kind == "question":
                target = row.get("dispatched_to")
                if target and _in_window(created_at, week_start, week_end):
                    dispatched += 1
                if target and _in_window(closed_at, week_start, week_end) and status == "answered":
                    answered += 1
                # Blind spot: still open, dispatched, and live at some point
                # in the window, with no read on its target this cycle.
                if (
                    target
                    and status == "open"
                    and _overlaps_window(created_at, closed_at, week_start, week_end)
                    and str(target) not in read_targets
                ):
                    blind_spots.append(str(target))

            elif kind == "observation":
                if closed_at is not None and _in_window(closed_at, week_start, week_end):
                    cited = frozenset(str(r) for r in (row.get("cited_refs") or []))
                    if cited and _anticipated_by_a_later_finding(
                        cited, closed_at, finding_refs,
                    ):
                        anticipated += 1

        out[descriptor_id] = YieldCounts(
            descriptor_id=descriptor_id,
            hypotheses_opened=opened,
            hypotheses_confirmed=confirmed,
            hypotheses_refuted=refuted,
            hypotheses_expired=expired,
            questions_dispatched=dispatched,
            questions_answered=answered,
            observations_anticipated=anticipated,
            blind_spots=tuple(dict.fromkeys(blind_spots)),  # de-duplicate, keep order
        )
    return out


def _anticipated_by_a_later_finding(
    cited_refs: frozenset[str],
    closed_at: datetime,
    finding_refs: Sequence[tuple[datetime, frozenset[str]]],
) -> bool:
    for produced_at, refs in finding_refs:
        if produced_at > closed_at and (refs & cited_refs):
            return True
    return False


# ---------------------------------------------------------------------------
# The receipt — the FINDING this instrument publishes
# ---------------------------------------------------------------------------


def build_finding(
    *,
    week_start: datetime,
    week_end: datetime,
    by_descriptor: Mapping[str, YieldCounts],
    warnings: list[str],
) -> FindingPayload:
    """Per §4: a per-run receipt naming every scored descriptor's counters —
    never a gate, so ``confidence`` is fixed at 1.0 (a count is not a claim
    graded for truth) and nothing here demotes or blocks anything downstream."""
    n = len(by_descriptor)
    total_open = sum(c.hypotheses_opened for c in by_descriptor.values())
    total_blind = sum(len(c.blind_spots) for c in by_descriptor.values())
    head = (
        f"Inquiry yield {week_start.date().isoformat()}..{week_end.date().isoformat()}: "
        f"{n} inquiries scored, {total_open} hypotheses opened, "
        f"{total_blind} blind spots"
    )
    lines = [
        f"descriptor={d.descriptor_id} opened={d.hypotheses_opened} "
        f"confirmed={d.hypotheses_confirmed} refuted={d.hypotheses_refuted} "
        f"expired={d.hypotheses_expired} dispatched={d.questions_dispatched} "
        f"answered={d.questions_answered} anticipated={d.observations_anticipated} "
        f"blind_spots={len(d.blind_spots)}"
        for d in by_descriptor.values()
    ]
    return FindingPayload(
        title=head[:2048],
        body=(
            f"week={week_start.date().isoformat()}..{week_end.date().isoformat()}\n"
            + "\n".join(lines)
            + f"\nwarnings={warnings}\n"
        )[:65536],
        confidence=1.0,
        evidence=[],
        tags=["deterministic", SUB_HANDLER_NAME],
        data={
            "sub_handler": SUB_HANDLER_NAME,
            "method_version": METHOD_VERSION,
            "week_start": week_start.isoformat(),
            "week_end": week_end.isoformat(),
            "descriptors_scored": n,
            "by_descriptor": {
                d.descriptor_id: d.as_dict() for d in by_descriptor.values()
            },
            "warnings": warnings,
        },
    )


# ---------------------------------------------------------------------------
# The I/O shell
# ---------------------------------------------------------------------------

_LEDGER_SQL = """
SELECT descriptor_id, kind, status, dispatched_to, cited_refs,
       created_at, closed_at
  FROM inquiry_ledger
 ORDER BY created_at
 LIMIT $1
"""

# Findings produced INSIDE the window with a non-empty derived_from — the
# only rows that could possibly be "later than" an in-window observation
# close (closed_at is itself >= week_start, so nothing produced before
# week_start can ever be "later").
_FINDING_REFS_SQL = """
SELECT produced_at, derived_from
  FROM analyst_outputs
 WHERE produced_at >= $1 AND produced_at < $2
   AND array_length(derived_from, 1) IS NOT NULL
 ORDER BY produced_at
 LIMIT $3
"""

# The desks/targets that produced ANYTHING this cycle — a dispatched
# question's target clearing this set means "read this cycle" (see the
# module's blind-spot SCOPE NOTE).
_READ_TARGETS_SQL = """
SELECT DISTINCT analyst_id, target_id
  FROM analyst_outputs
 WHERE produced_at >= $1 AND produced_at < $2
"""


async def handle(
    inputs: list[dict[str, Any]],
    options: Mapping[str, Any],
    deps: Any | None,
) -> AnalystMethodResult:
    """Sub-handler entry point — see module docstring.

    A META global sweep (no ``targets`` selector), mirroring ``receipt_anchor``:
    ``deps``/pool of ``None`` degrades to an honest empty receipt, never a
    run failure."""
    warnings: list[str] = []
    week_end = datetime.now(timezone.utc)
    week_start = week_end - timedelta(days=WINDOW_DAYS)

    pool = getattr(deps, "pg_pool", None) if deps is not None else None
    if pool is None:
        warnings.append("inquiry_yield.no_pool")
        return AnalystMethodResult(
            finding=build_finding(
                week_start=week_start, week_end=week_end,
                by_descriptor={}, warnings=warnings,
            ),
            usage={"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0},
        )

    async with pool.acquire() as conn:
        ledger_rows = await conn.fetch(_LEDGER_SQL, _MAX_LEDGER_ROWS)
        finding_rows = await conn.fetch(_FINDING_REFS_SQL, week_start, week_end, _MAX_FINDING_ROWS)
        read_rows = await conn.fetch(_READ_TARGETS_SQL, week_start, week_end)

    if len(ledger_rows) >= _MAX_LEDGER_ROWS:
        warnings.append("inquiry_yield.ledger_capped")
    if len(finding_rows) >= _MAX_FINDING_ROWS:
        warnings.append("inquiry_yield.findings_capped")

    ledger = [dict(r) for r in ledger_rows]
    finding_refs: list[tuple[datetime, frozenset[str]]] = [
        (r["produced_at"], frozenset(str(u) for u in (r["derived_from"] or [])))
        for r in finding_rows
    ]
    read_targets: set[str] = set()
    for r in read_rows:
        if r["analyst_id"]:
            read_targets.add(str(r["analyst_id"]))
        if r["target_id"]:
            read_targets.add(str(r["target_id"]))

    by_descriptor = compute_yield(
        ledger,
        week_start=week_start,
        week_end=week_end,
        finding_refs=finding_refs,
        read_targets=frozenset(read_targets),
    )

    logger.info(
        "inquiry_yield.tick week=%s..%s descriptors=%d",
        week_start.date().isoformat(), week_end.date().isoformat(), len(by_descriptor),
    )
    return AnalystMethodResult(
        finding=build_finding(
            week_start=week_start, week_end=week_end,
            by_descriptor=by_descriptor, warnings=warnings,
        ),
        usage={"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0},
    )


__all__ = [
    "handle",
    "build_finding",
    "compute_yield",
    "YieldCounts",
    "SUB_HANDLER_NAME",
    "METHOD_VERSION",
    "WINDOW_DAYS",
]
