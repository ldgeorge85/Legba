# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The ``relationship_reifier`` run receipt — extracted from
``relationship_reifier.py`` at V3/P5 (the module crossed the 1,500-line size
gate's entry threshold; this summary builder is the cohesive seam).

Everything here is pure: counters in, :class:`FindingPayload` out. The
payload's ``data`` block is what ``analyst_traces.output_payload`` persists —
the funnel readings DATA_MODEL_V3 §5.2 measures against.
"""
from __future__ import annotations

from typing import Any, Mapping

from ..provenance.models import FindingPayload
from .reifier_selection import SelectionCounters


def build_reifier_summary(
    *,
    n_candidates: int,
    typed: int,
    written: int,
    superseded: int,
    degraded: int,
    skipped_endpoints: int = 0,
    budget_paused: bool,
    target_id: str | None,
    accepted: int = 0,
    rejected: int = 0,
    alias_pairs: int = 0,
    alias_pairs_routed: int = 0,
    inserted: int = 0,
    folded: int = 0,
    rejected_marked: int = 0,
    selection: SelectionCounters | None = None,
    # H14 — the wall-clock TURN budget (distinct from `budget_paused` above,
    # which is the $/token spend envelope). `pass_budget_exceeded` is True
    # only when the run stopped because a whole candidate no longer fit in
    # what was left of LEGBA_REIFIER_PASS_BUDGET_SECONDS; `stopped_after`
    # names the last candidate examined before that stop; `per_candidate_seconds`
    # is this run's own measured {mean, max} seconds/candidate; `cursor`
    # restates the stop as a resume pointer inside the ordered candidate list.
    pass_budget_exceeded: bool = False,
    stopped_after: str | None = None,
    per_candidate_seconds: Mapping[str, float] | None = None,
    cursor: Mapping[str, Any] | None = None,
) -> FindingPayload:
    """Build the per-run summary finding. Pure counters → payload."""
    # K-G2 counter vocabulary. ``typed`` is now "the typer returned a verdict",
    # which splits into ``accepted`` (a payload the coercion accepted → an edge
    # is attempted) and ``rejected`` (the model said no relationship, or the
    # verdict failed validation). Before batching, ``typed`` meant only the
    # accepted half, so a run could not distinguish "the typer rejected these"
    # from "the typer never saw these" — the exact ambiguity that let the dead-row
    # window hide for weeks.
    title = (
        f"Relationship reifier: {written} nexuses written "
        f"({typed} typed / {n_candidates} candidates)"
    )
    if target_id:
        title = f"{title} for {target_id}"
    tags = ["meta", "relationship_reifier"]
    if written:
        tags.append("nexuses_written")
    if degraded:
        tags.append("degraded")
    if budget_paused:
        tags.append("budget_paused")
    # H14 — a DISTINCT tag from `budget_paused`: that one is the $/token
    # envelope, this one is the wall-clock actor-turn budget.
    if pass_budget_exceeded:
        tags.append("pass_budget_exceeded")
    # K-G2 — the selection receipt. The old summary said ``candidates=40`` on
    # every tick while all 40 were dead rows; these counters are what makes a
    # collapsed window visible without a DB session.
    sel = (selection or SelectionCounters()).as_dict()
    pcs = dict(per_candidate_seconds) if per_candidate_seconds else {
        "mean": 0.0, "max": 0.0,
    }
    return FindingPayload(
        title=title[:2048],
        body=(
            f"candidates={n_candidates} typed={typed} accepted={accepted} "
            f"rejected={rejected} alias_pairs={alias_pairs} "
            f"alias_pairs_routed={alias_pairs_routed} written={written} "
            f"inserted={inserted} folded={folded} rejected_marked={rejected_marked} "
            f"superseded={superseded} degraded={degraded} "
            f"skipped_endpoints={skipped_endpoints} "
            f"budget_paused={budget_paused} "
            f"pass_budget_exceeded={pass_budget_exceeded} "
            f"stopped_after={stopped_after} "
            f"per_candidate_seconds_mean={pcs.get('mean')} "
            f"per_candidate_seconds_max={pcs.get('max')} "
            + " ".join(f"selection_{k}={v}" for k, v in sel.items())
        )[:65536],
        confidence=1.0,
        tags=tags,
        data={
            "meta": True,
            "sub_handler": "relationship_reifier",
            "candidates": n_candidates,
            "typed": typed,
            "accepted": accepted,
            "rejected": rejected,
            "alias_pairs": alias_pairs,
            "alias_pairs_routed": alias_pairs_routed,
            "written": written,
            # V3/P5 — the entity_edges upsert's insert/fold split for this
            # run's writes (xmax=0 idiom); written-(inserted+folded) is the
            # parked residue. scan_limit_binding rides inside `selection`.
            "inserted": inserted,
            "folded": folded,
            "rejected_marked": rejected_marked,
            "superseded": superseded,
            "degraded": degraded,
            "skipped_endpoints": skipped_endpoints,
            "budget_paused": budget_paused,
            "selection": sel,
            # H14 — the wall-clock turn budget's readings (see the kwarg docs
            # above). `cursor` is `None` on a run that never hit the budget.
            "pass_budget_exceeded": pass_budget_exceeded,
            "stopped_after": stopped_after,
            "per_candidate_seconds": pcs,
            "cursor": dict(cursor) if cursor else None,
        },
    )


__all__ = ["build_reifier_summary"]
