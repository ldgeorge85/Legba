# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""H14 — the ``relationship_reifier`` wall-clock TURN budget.

Extracted from ``relationship_reifier.py`` the same way ``_reifier_receipt.py``
was at V3/P5: the module crossed the 1,500-line size gate's entry threshold and
this is the cohesive seam — everything here is the pure bookkeeping behind the
turn gate, none of it touches the typing prompt, the accept bar or the
selection SQL.

WHY
---
Dapr actors are turn-based; an unbounded reifier run starves the actor's
reminders and reconcile behind it (measured 2026-09-24 12:45Z: 600 candidates
typed, held the turn 2,257 s against a 180 s invoke timeout; the run before,
1,295 s). This mirrors the house pattern the grader (H2,
``LEGBA_GRADER_PASS_BUDGET_SECONDS``) and the clustering run (P1b,
``LEGBA_EVENT_CLUSTERING_PASS_BUDGET_SECONDS``) already hold their turn with:
:class:`~.deterministic_handlers.fact_contention_pass.PassBudget` (re-exported
here, unmodified) as the wall-clock ceiling, checked before each admitted unit
of typing work.

THE ESTIMATE
------------
``relationship_reifier`` batches candidates ``batch_size`` at a time into one
LLM call — batching is the K-G2 throughput win and this lane does not touch
it. The admission check is nonetheless per CANDIDATE, not per batch:
:class:`ChunkCostTracker.estimate` is this run's own running mean
seconds/candidate (a :data:`DEFAULT_CANDIDATE_SECONDS_PRIOR` before anything
has been typed this run), and :func:`PassBudget.allows` is asked about that
one candidate's share, not the whole upcoming batch's. This is deliberate: the
prior (10 s) times the default batch size (12) would tie the default pass
budget (120 s) almost exactly, so a per-batch scaled check would refuse the
very first batch of every cold run on the button. The per-candidate check
trades a looser worst-case bound (one more admitted batch can still run past
the budget by its own wall-clock cost) for never starving a run of its first
candidate — a real, large improvement over the pre-H14 unbounded shape either
way.

THE RESUME
----------
:class:`ChunkCostTracker` also carries the ``stopped_after`` pointer: the id
(+ qual_score + produced_at) of the last candidate a truncated run examined,
for the run receipt's ``cursor``. It is NOT wired into the selection SQL — see
the reifier module's report for why a receipt-only pointer is enough here.
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime
from typing import Any, Mapping, Sequence

from .deterministic_handlers.fact_contention_pass import PassBudget

logger = logging.getLogger(__name__)

#: Env var name — the marker a deploy verifies. ``<= 0`` disables the gate
#: entirely (the pre-H14 unbounded shape); read once per run.
REIFIER_PASS_BUDGET: str = "LEGBA_REIFIER_PASS_BUDGET_SECONDS"
DEFAULT_REIFIER_PASS_BUDGET_SECONDS: float = 120.0

#: The per-candidate cost estimate fed to ``budget.allows()`` before this run
#: has typed anything. Close to, but under, the measured ~3.8 s/candidate
#: steady state (2026-09-24 12:45Z receipt) so a cold run still admits its
#: first batch under the default 120 s budget.
DEFAULT_CANDIDATE_SECONDS_PRIOR: float = 10.0


def reifier_pass_budget_seconds() -> float:
    """The per-run wall-clock ceiling in seconds (``<= 0`` = unbounded).

    A malformed value reads as the default, never as an accidental zero
    (which would make every run refuse its first candidate).
    """
    raw = os.getenv(REIFIER_PASS_BUDGET, "").strip()
    if not raw:
        return DEFAULT_REIFIER_PASS_BUDGET_SECONDS
    try:
        return float(raw)
    except (TypeError, ValueError):
        logger.warning(
            "relationship_reifier.bad_pass_budget %r; using %s",
            raw, DEFAULT_REIFIER_PASS_BUDGET_SECONDS,
        )
        return DEFAULT_REIFIER_PASS_BUDGET_SECONDS


class ChunkCostTracker:
    """Running per-candidate cost + the stopped-after resume pointer, for
    ONE run. A small stateful object rather than a closure over
    ``run_method``'s locals, so the bookkeeping can live in its own module.
    """

    __slots__ = (
        "typed_call_seconds_total",
        "typed_call_candidates_total",
        "max_candidate_seconds",
        "stopped_after_id",
        "stopped_after_qual",
        "stopped_after_produced",
    )

    def __init__(self) -> None:
        self.typed_call_seconds_total = 0.0
        self.typed_call_candidates_total = 0
        self.max_candidate_seconds = 0.0
        self.stopped_after_id: Any = None
        self.stopped_after_qual: Any = None
        self.stopped_after_produced: Any = None

    @property
    def estimate(self) -> float:
        """The next admission check's per-candidate cost estimate."""
        if self.typed_call_candidates_total:
            return (
                self.typed_call_seconds_total / self.typed_call_candidates_total
            )
        return DEFAULT_CANDIDATE_SECONDS_PRIOR

    def record(
        self, chunk_started: float, examined: Sequence[Mapping[str, Any]]
    ) -> None:
        """Fold one admitted chunk's wall-clock cost into the running mean and
        advance the stopped-after pointer to its last member. Called after
        EVERY admitted chunk regardless of outcome (typed, all-junk,
        degraded) — every exit path counts toward the next estimate."""
        elapsed = time.monotonic() - chunk_started
        n = len(examined)
        self.typed_call_seconds_total += elapsed
        self.typed_call_candidates_total += n
        per_candidate = elapsed / max(1, n)
        if per_candidate > self.max_candidate_seconds:
            self.max_candidate_seconds = per_candidate
        if examined:
            last = examined[-1]
            self.stopped_after_id = last.get("id")
            self.stopped_after_qual = last.get("qual_score")
            self.stopped_after_produced = last.get("produced_at")

    def per_candidate_seconds(self) -> dict[str, float]:
        """The receipt's ``{mean, max}`` reading — ``0.0``/``0.0`` when
        nothing was ever admitted this run."""
        if not self.typed_call_candidates_total:
            return {"mean": 0.0, "max": 0.0}
        return {
            "mean": round(self.estimate, 3),
            "max": round(self.max_candidate_seconds, 3),
        }

    def resume_cursor(
        self, pass_budget_exceeded: bool
    ) -> tuple[str | None, dict[str, Any] | None]:
        """The receipt's ``(stopped_after, cursor)`` pair — both ``None``
        unless the wall-clock budget is what ended the run. ``cursor``
        restates ``stopped_after`` as the resume position inside the ORDERED
        candidate list (id / qual_score / produced_at) — a receipt-only
        observability marker, not persisted to ``alert_trigger_watermarks``
        (no SQL-side resume consumes it this lane; the already-reified guard
        and the H9 cooldown already make the next scan self-resuming)."""
        if not pass_budget_exceeded or self.stopped_after_id is None:
            return None, None
        stopped_after = str(self.stopped_after_id)
        cursor = {
            "stopped_after": stopped_after,
            "qual_score": (
                round(float(self.stopped_after_qual), 6)
                if self.stopped_after_qual is not None else None
            ),
            "produced_at": (
                self.stopped_after_produced.isoformat()
                if isinstance(self.stopped_after_produced, datetime) else None
            ),
        }
        return stopped_after, cursor


__all__ = [
    "PassBudget",
    "REIFIER_PASS_BUDGET",
    "DEFAULT_REIFIER_PASS_BUDGET_SECONDS",
    "DEFAULT_CANDIDATE_SECONDS_PRIOR",
    "reifier_pass_budget_seconds",
    "ChunkCostTracker",
]
