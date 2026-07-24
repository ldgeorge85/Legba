# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""scorecard ↔ composition reconciliation — PURE reducers (no substrate).

Extracted from ``v3_api`` (B0-5 / audit W6) so BOTH read surfaces reconcile the
same way against the same code:

  * the registry ``GET /eval/country_scorecard`` endpoint (``v3_api``), and
  * the journal self-instrument ``get_assessments``
    (``legba.runtime.substrate_query_port``), which surfaces the same
    ``disagreements`` block so the journal narrates the divergence honestly.

Everything here is a PURE function of already-fetched, plain-Python shapes — no
asyncpg, no ORM, no I/O — so it is unit-testable without a substrate and safe to
import from either the registry image or the runtime image without a dependency
cycle (it pulls only pydantic + stdlib typing). Each caller owns its own DB
fetch (the two read surfaces query different starting rows); the reconciliation
LOGIC lives here, once.
"""
from __future__ import annotations

from typing import Any, Literal, Mapping, Sequence

from pydantic import BaseModel, Field

__all__ = [
    "ScorecardDisagreement",
    "composition_usages",
    "scorecard_disagreements",
]


class ScorecardDisagreement(BaseModel):
    """One scorecard↔composition CONTRADICTION for a country (B0-5, audit W6).

    The two P4 products judge the same critique join with different bars over
    different windows (scorecard: dedicated ``FAITH_FLOOR`` per-claim over 336h,
    scorecard_banding R1b; composition: its own admission floor over 24h), and
    until B0-5 NOTHING reconciled them — the scorecard could exclude a finding
    as low-faithfulness while the live composition head CITES that exact finding
    (the US ``leadership_transition`` case). One row = one finding the scorecard
    EXCLUDED from a dimension's basis that the composition head nonetheless
    USES:

      * ``finding_id`` — the contested ``analyst_outputs`` row id.
      * ``dimension`` — the scorecard dimension (= the producing unit
        ``analyst_id``) that excluded it.
      * ``scorecard_verdict`` — ``excluded:<reason>`` with scorecard_banding's
        machine reason (``low-faithfulness`` / ``below-floor`` /
        ``verify-failed`` / ``no-severity-tag`` / ``no-finding``).
      * ``composition_usage`` — how the composition head uses the finding:
        ``cited`` (a ``[N]`` citation in the prose) or ``derived_from``
        (lineage-only).
      * ``note`` — the human-legible one-liner the panel renders as-is.
    """
    finding_id: str
    dimension: str
    scorecard_verdict: str
    composition_usage: Literal["cited", "derived_from"]
    note: str = ""


def composition_usages(
    citations: Any,
    derived_from: Sequence[Any],
    derived_analysts: Mapping[str, str],
) -> dict[str, tuple[str, str]]:
    """Map ``finding_id -> (producing analyst_id, usage)`` for a composition head.

    Pure reducer (unit-testable without a substrate). Inputs are the two
    LIVE-VERIFIED shapes on a ``country_composition`` head row:

      * ``citations`` — the row's ``data['data']['citations']`` JSONB array; each
        element carries ``ref_id`` (the cited ``analyst_outputs`` id),
        ``ref_kind`` and ``source`` (= the PRODUCING unit ``analyst_id``, e.g.
        ``leadership_transition``).
      * ``derived_from`` — the row's ``derived_from`` ``uuid[]`` COLUMN (lineage).

    A citation is the stronger usage claim, so a finding both cited and in
    lineage reads ``cited``; a lineage-only id is attributed via
    ``derived_analysts`` (an id→analyst_id lookup resolved by the caller) and
    reads ``derived_from``. Non-``finding`` refs and malformed elements are
    skipped, never guessed.
    """
    usages: dict[str, tuple[str, str]] = {}
    if isinstance(citations, list):
        for c in citations:
            if not isinstance(c, dict):
                continue
            ref_kind = c.get("ref_kind")
            if ref_kind is not None and ref_kind != "finding":
                continue
            ref_id = c.get("ref_id")
            source = c.get("source")
            if (
                isinstance(ref_id, str) and ref_id
                and isinstance(source, str) and source
            ):
                usages[ref_id] = (source, "cited")
    for raw_fid in derived_from:
        fid = str(raw_fid)
        if not fid or fid in usages:
            continue
        analyst = derived_analysts.get(fid)
        if isinstance(analyst, str) and analyst:
            usages[fid] = (analyst, "derived_from")
    return usages


def scorecard_disagreements(
    dimensions: Mapping[str, Any],
    usages: Mapping[str, tuple[str, str]],
) -> list[ScorecardDisagreement]:
    """Pure reducer: (banded dimensions, composition usages) → disagreements.

    A disagreement exists exactly where a scorecard dimension banded
    ``insufficient-evidence`` (it EXCLUDED the dimension's claim — or found
    none) while the composition head USES a finding produced by that same
    dimension's analyst. Deterministic order (dimension, finding_id); an empty
    list is the normal reconciled state, never padded.
    """
    excluded: dict[str, str] = {}
    for dim, verdict in dimensions.items():
        if not isinstance(verdict, dict):
            continue
        if verdict.get("band") != "insufficient-evidence":
            continue
        excluded[str(dim)] = str(verdict.get("reason") or "insufficient-evidence")
    if not excluded:
        return []
    out: list[ScorecardDisagreement] = []
    for finding_id, (analyst_id, usage) in usages.items():
        reason = excluded.get(analyst_id)
        if reason is None:
            continue
        verb = "cites" if usage == "cited" else "derives from"
        out.append(
            ScorecardDisagreement(
                finding_id=finding_id,
                dimension=analyst_id,
                scorecard_verdict=f"excluded:{reason}",
                composition_usage=usage,  # type: ignore[arg-type]
                note=(
                    f"composition {verb} {finding_id}; scorecard excluded the "
                    f"{analyst_id} dimension as {reason}"
                ),
            )
        )
    out.sort(key=lambda d: (d.dimension, d.finding_id))
    return out
