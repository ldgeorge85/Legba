# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The DUE band's read — acute forecasts past their resolution date (7b-iv).

The Morning Read's fourth band asks "what is due today?" and the honest
answer the platform already writes is the pre-registered forecast pilot:
``acute_forecasts`` rows whose forward window has CLOSED and which carry no
outcome yet. Until this module there was no read route over that table at
all — the resolver wrote it, the scoreboard aggregated it, and no reader
could see which individual calls were hanging.

WHY A LEAF MODULE AND NOT MORE OF ``since_api.py``. Two reasons, in order:

  * ``since_api`` is the registry-SLIM surface. Everything the DUE band
    needs about the pilot lives in
    ``analysts.deterministic_handlers.forecast_acute`` — a module whose
    import graph reaches the whole analyst runtime (``feedparser`` included)
    and which therefore 500s the registry image on import, deferred or not
    (the ``unit_correctness_api`` precedent). So the two marks this band
    reads are MIRRORED here, and
    ``tests/data_pkg/test_v3_since_api.py::test_drift_guard_forecast_marks``
    asserts each mirror stays equal to its producer.
  * The SQL, the model and the reducer are pure and DB-free apart from one
    indexed SELECT, so the band is testable without FastAPI and without the
    route — the shape every other ``/since`` section already has.

WHAT THIS BAND WILL AND WILL NOT SAY

  * ``resolution_test`` is projected VERBATIM. It is the falsifiable
    contract frozen on the row at mint (H13, migration 0212) — the event
    class, the exogenous join the resolver counts on, the threshold and the
    window. A row minted before the column existed carries the same text
    prefixed ``retro: ``; that prefix is preserved, never trimmed, because
    "this is the rule we are grading it by today" and "this is the rule it
    was minted under" are different claims.
  * A VOIDED row is not due. ``voided:*`` means the call was WITHDRAWN from
    grading deliberately; surfacing it as an outstanding obligation would
    read as a stalled resolver forever, which is exactly the permanent-19
    misread H13 was written to end.
  * ``mark`` separates the three honest states a past-window row can be in
    rather than folding them into "late": ``in_grace`` (the window closed
    but the resolver's own grace period has not elapsed — nothing is
    overdue yet), ``awaiting`` (past grace, still unmarked — the resolver
    has not run since), and ``expired`` (the resolver ran, could not grade
    it, and stamped ``unresolved:expired``). Only the last is a failure to
    answer, and it stays in the scoreboard's denominator at maximum penalty.
  * ``days_overdue`` is measured from ``window_end``, the moment the claim
    became answerable — never from ``issued_at`` (which would inflate every
    row by the seven-day horizon) and never from the grace cutoff (which
    would hide the first day).

Reference: migrations ``0047_acute_forecasts.sql`` /
``0212_sealed_forecast_ledger.sql``, ``forecast_acute.py``'s
``UNRESOLVED_EXPIRED`` / ``VOID_PREFIX`` / ``RESOLUTION_GRACE_DAYS``.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Mapping

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Mirrored constants (drift-guarded — see test_v3_since_api.py)
# ---------------------------------------------------------------------------

#: A due-but-ungradeable row's mark — mirrors
#: ``forecast_acute.UNRESOLVED_EXPIRED``. NOT imported: the registry image
#: carries no analyst-runtime dependencies.
UNRESOLVED_EXPIRED: str = "unresolved:expired"

#: The withdrawn-from-grading prefix — mirrors ``forecast_acute.VOID_PREFIX``.
#: A row whose ``resolved_by`` starts with this is excluded from the band.
VOID_PREFIX: str = "voided:"

#: Days after ``window_end`` the resolver waits before a row counts as late —
#: mirrors ``forecast_acute.RESOLUTION_GRACE_DAYS``. Inside it a closed window
#: is ``in_grace``, not overdue.
RESOLUTION_GRACE_DAYS: int = 1

#: Rows returned per request. The pilot mints ~19/week, so a hundred rows is
#: five weeks of total resolver silence — far past the point the band has made
#: its case. The section still reports its FULL ``total``.
DUE_FORECAST_CAP: int = 100

#: The three honest states of a closed-window row (see the module docstring).
MARK_IN_GRACE = "in_grace"
MARK_AWAITING = "awaiting"
MARK_EXPIRED = "expired"


# ---------------------------------------------------------------------------
# SQL — SELECT only.
# ---------------------------------------------------------------------------

#: Closed-window, unresolved, not-withdrawn forecasts, longest-overdue first.
#:
#: The predicate is the partial index ``acute_forecasts_open_window_idx
#: (window_end) WHERE resolved_outcome IS NULL`` verbatim, so this is an index
#: range scan over the open set, not a table walk. ``count(*) OVER ()`` gives
#: the honest full total beside the capped page — the house idiom every other
#: ``/since`` section uses. NO correlated sub-select: every column is on the
#: row (the resolution test is frozen there by migration 0212, which is the
#: whole reason this band needs no join to the producer).
DUE_FORECASTS_SQL = f"""
    SELECT id::text          AS id,
           region            AS region,
           event_class       AS event_class,
           window_start      AS window_start,
           window_end        AS window_end,
           p                 AS p,
           p_base            AS p_base,
           method            AS method,
           method_version    AS method_version,
           scale_version     AS scale_version,
           resolution_test   AS resolution_test,
           resolved_by       AS resolved_by,
           issued_at         AS issued_at,
           count(*) OVER ()  AS total
      FROM acute_forecasts
     WHERE resolved_outcome IS NULL
       AND window_end <= $1
       AND (resolved_by IS NULL OR resolved_by NOT LIKE '{VOID_PREFIX}%')
     ORDER BY window_end ASC, id ASC
     LIMIT $2
"""


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class ForecastDue(BaseModel):
    """One pre-registered call whose window has closed with no outcome."""

    id: str
    region: str = Field(
        description="The desk descriptor id the call was minted for "
        "(`country_g20_us`) — the band groups on it.",
    )
    event_class: str
    window_start: datetime
    window_end: datetime
    p: float = Field(description="The claimed probability, scored by Brier.")
    p_base: float = Field(description="The climatological base rate.")
    method: str
    method_version: str | None = Field(
        default=None,
        description=(
            "H12 — the instrument revision the call was computed under. NULL "
            "on a row minted before the stamp existed; never back-labelled."
        ),
    )
    scale_version: str | None = Field(
        default=None,
        description=(
            "K3 — the SCALE `p` and `p_base` are read on (the clamped open "
            "probability interval). Answers a different question from "
            "`method_version`: that one says whether the same code ran, this "
            "one says whether two probabilities are comparable at all. NULL on "
            "a row minted before the stamp existed; never back-labelled."
        ),
    )
    resolution_test: str = Field(
        description=(
            "H13 — the falsifiable test frozen on the row at mint, projected "
            "VERBATIM. A `retro: ` prefix means it was stamped by migration "
            "0212's backfill, not at mint; the prefix is preserved."
        ),
    )
    resolved_by: str | None = Field(
        default=None,
        description=(
            "The resolver's mark, if any. `unresolved:expired` = it ran and "
            "could not grade this row. NULL = it has not marked it at all."
        ),
    )
    issued_at: datetime
    days_overdue: float = Field(
        description=(
            "Days from `window_end` to the request stamp — when the claim "
            "became answerable, not when it was issued. 0.0 while the window "
            "has only just closed."
        ),
    )
    mark: str = Field(
        description="`in_grace` | `awaiting` | `expired` — see the module doc.",
    )


class ForecastsDueSection(BaseModel):
    """The DUE band's envelope. Same `{items, total, truncated}` contract as
    every other ``/since`` section: a capped list is never presented as the
    whole story."""

    items: list[ForecastDue] = Field(default_factory=list)
    total: int = 0
    truncated: bool = False


# ---------------------------------------------------------------------------
# Pure reducer (unit-testable with no database)
# ---------------------------------------------------------------------------


def _as_float(value: Any) -> float:
    return float(value) if isinstance(value, (int, float)) and not isinstance(
        value, bool
    ) else 0.0


def forecast_mark(
    window_end: datetime,
    resolved_by: str | None,
    *,
    now: datetime,
    grace_days: int = RESOLUTION_GRACE_DAYS,
) -> str:
    """Which of the three honest states a closed-window row is in.

    The resolver's OWN mark wins when it carries one: if it stamped
    ``unresolved:expired`` the row is expired regardless of arithmetic here,
    because the resolver is the authority on whether it tried. Absent a mark
    the state is pure timestamp math against the same grace the resolver uses.
    """
    if resolved_by == UNRESOLVED_EXPIRED:
        return MARK_EXPIRED
    if now <= window_end + timedelta(days=grace_days):
        return MARK_IN_GRACE
    return MARK_AWAITING


def due_forecasts(
    rows: list[Mapping[str, Any]],
    *,
    now: datetime,
    grace_days: int = RESOLUTION_GRACE_DAYS,
) -> list[ForecastDue]:
    """Project the SQL rows into the DUE band, longest-overdue first.

    Row order is the SQL's (``window_end ASC``) and is preserved — the query
    already sorts on the only axis this band ranks by, so no re-sort can
    disagree with the page boundary the ``LIMIT`` drew.
    """
    out: list[ForecastDue] = []
    for r in rows:
        window_end = r["window_end"]
        resolved_by = r.get("resolved_by")
        overdue = (now - window_end).total_seconds() / 86_400.0
        out.append(
            ForecastDue(
                id=str(r["id"]),
                region=str(r["region"]),
                event_class=str(r["event_class"]),
                window_start=r["window_start"],
                window_end=window_end,
                p=_as_float(r["p"]),
                p_base=_as_float(r["p_base"]),
                method=str(r["method"] or ""),
                method_version=r.get("method_version"),
                scale_version=r.get("scale_version"),
                # NOT NULL since 0212, but a row read through a pre-0212
                # connection would be None — say so rather than crash the
                # whole diff on one row.
                resolution_test=str(r.get("resolution_test") or ""),
                resolved_by=resolved_by,
                issued_at=r["issued_at"],
                days_overdue=max(0.0, overdue),
                mark=forecast_mark(
                    window_end, resolved_by, now=now, grace_days=grace_days
                ),
            )
        )
    return out
