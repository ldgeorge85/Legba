# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``forecast_scoreboard`` sub-handler — P4-T7 acute-forecast scoreboard producer.

The dedicated deterministic META analyst that DRIVES the pre-registered acute-
binary forecast pilot on its OWN weekly-idempotent cadence, decoupled from the
daily calibration tick. Each run it calls the EXISTING :mod:`forecast_acute`
writers — it reimplements NO forecasting math:

  * :func:`forecast_acute.issue_weekly_forecasts` — issue one binary forecast per
    G20 country for the NEXT weekly window (idempotent ``ON CONFLICT``; honors the
    D9 pre-issue degeneracy ABSTAIN internally — a geography-dominated p-vector
    issues ZERO rows). This producer NEVER pre-checks or bypasses that guard, so
    an ``issued=0`` receipt is the honest abstain / idempotent no-op.
  * :func:`forecast_acute.resolve_open_acute_forecasts` — grade every forecast
    whose forward window has CLOSED and SETTLED, exogenously, by the UPSTREAM
    event time (never overwrites an already-resolved row).
  * :func:`forecast_acute.pull_resolved_acute_forecasts` — a read-only receipt
    count of resolved pilot calls (not persisted anywhere by this handler).

HONESTY DECISION (the whole point of P4-T7)
-------------------------------------------
Forecasting returns as a MEASURED number, NEVER a free-text claim. This handler's
ONLY persisted product is ``acute_forecasts`` rows (issued / resolved by the
writers above) + the run's ``analyst_traces`` receipt. The returned
:class:`FindingPayload` is a per-run RECEIPT (counts only, no probability text),
marked ``TRACE_ONLY`` in :mod:`legba.data.analysts.deterministic` so it is NEVER
persisted as a ``finding`` / ``prediction`` / ``claim`` row on any trust surface
— mirroring the :mod:`scorecard_producer` side-write precedent. The forecast
numbers themselves surface ONLY in the T4 scoreboard
(``GET /api/v1/v3/eval/calibration``), fed by the SEGREGATED pilot Brier /
Brier-skill-score that :mod:`calibration_tracking` folds from these same resolved
``acute_forecasts`` rows — the project earns the word "forecast" only when that
BSS is positive on a non-degenerate at-sample pilot, never before.

Each leg is wrapped best-effort (mirroring calibration_tracking's acute side-
calls) so one leg failing never aborts the other, and a ``deps``/pool of None
degrades to an HONEST empty receipt (issued=0 / resolved=0), NOT a stub.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping

from ...provenance.models import FindingPayload
from ....runtime.analyst_method import AnalystMethodResult
from . import forecast_acute

logger = logging.getLogger(__name__)

SUB_HANDLER_NAME = "forecast_scoreboard"


# ---------------------------------------------------------------------------
# Receipt assembly (pure — testable without a DB)
# ---------------------------------------------------------------------------


def brier_of(rows: list[dict[str, Any]]) -> float | None:
    """Mean (p - o)^2 over the pulled resolved rows — the honest-null Brier:
    ``None`` on an empty population, never a fabricated 0."""
    vals = [
        (float(r["claimed_confidence"]) - int(r["outcome"])) ** 2
        for r in rows
        if r.get("claimed_confidence") is not None and r.get("outcome") is not None
    ]
    return sum(vals) / len(vals) if vals else None


def brier_all_of(rows: list[dict[str, Any]], expired_count: int) -> float | None:
    """H13 — the honest denominator: the answered Brier's numerator plus every
    expired (due-but-never-resolved) row at MAXIMUM penalty (1.0), over the
    answered + expired population. ``None`` when that population is empty."""
    vals = [
        (float(r["claimed_confidence"]) - int(r["outcome"])) ** 2
        for r in rows
        if r.get("claimed_confidence") is not None and r.get("outcome") is not None
    ]
    n = len(vals) + int(expired_count)
    if n == 0:
        return None
    return (sum(vals) + float(expired_count)) / n


def build_receipt(
    *,
    issued: int,
    resolved: int,
    resolved_total: int,
    warnings: list[str],
    brier_answered: float | None = None,
    brier_all: float | None = None,
    expired_count: int = 0,
    hypothesis_minters: list[dict[str, Any]] | None = None,
) -> FindingPayload:
    """Assemble the per-run TRACE_ONLY receipt (counts + instrument numbers).

    The receipt reports what the writers DID this tick — how many forecasts were
    issued (``0`` on an idempotent no-op OR a D9 abstain — both honest), how many
    closed windows were graded, and the running total of resolved pilot calls.
    H13 adds the honest denominator side by side: ``brier_answered`` (the
    answered-only Brier, today's number), ``brier_all`` (expired rows held at
    maximum penalty) and ``expired_count`` — plus the per-minter hypothesis
    open/resolved counts, where selection bias would show. It carries NO
    probability / claim / finding text: the forecast values live ONLY in
    ``acute_forecasts`` + the T4 scoreboard, never here.
    """
    head = (
        f"Forecast scoreboard: issued={issued} resolved={resolved} "
        f"(resolved_total={resolved_total}, expired={expired_count}, "
        f"class={forecast_acute.EVENT_CLASS})"
    )
    body = (
        f"issued={issued}\n"
        f"resolved={resolved}\n"
        f"resolved_total={resolved_total}\n"
        f"expired_count={expired_count}\n"
        f"brier_answered={brier_answered}\n"
        f"brier_all={brier_all}\n"
        f"hypothesis_minters={hypothesis_minters or []}\n"
        f"event_class={forecast_acute.EVENT_CLASS}\n"
        f"warnings={warnings}\n"
    )
    return FindingPayload(
        title=head[:2048],
        body=body[:65536],
        confidence=1.0,
        evidence=[],
        tags=["deterministic", "forecast_scoreboard"],
        data={
            "sub_handler": SUB_HANDLER_NAME,
            "issued": int(issued),
            "resolved": int(resolved),
            "resolved_total": int(resolved_total),
            # H13 — the sealed-ledger denominator trio, side by side so
            # answered-only skill can never hide an unresolved backlog.
            "brier_answered": brier_answered,
            "brier_all": brier_all,
            "expired_count": int(expired_count),
            # H13 — hypotheses by MINTER (analyst_id): open vs resolved. The
            # units mint ~1.5k rows nobody resolves; this is where that shows.
            "hypothesis_minters": hypothesis_minters or [],
            "event_class": forecast_acute.EVENT_CLASS,
            "warnings": warnings,
        },
    )


#: H13 — the selection-bias meter: hypotheses grouped by MINTER
#: (``analyst_id``), open vs resolved. ``resolved_outcome`` (migration 0038) is
#: the exogenous-resolution stamp; NULL = still open.
_HYPOTHESIS_MINTER_SQL = """
SELECT COALESCE(analyst_id, 'unattributed') AS analyst_id,
       count(*) FILTER (WHERE resolved_outcome IS NULL)::int     AS open,
       count(*) FILTER (WHERE resolved_outcome IS NOT NULL)::int AS resolved
  FROM hypotheses
 GROUP BY analyst_id
 ORDER BY analyst_id
"""


async def _pull_hypothesis_minter_counts(pool: Any) -> list[dict[str, Any]]:
    """Per-minter open/resolved hypothesis counts — where selection bias in
    resolution would show up as a number."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(_HYPOTHESIS_MINTER_SQL)
    return [
        {
            "analyst_id": r["analyst_id"],
            "open": int(r["open"]),
            "resolved": int(r["resolved"]),
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Public handler entry point
# ---------------------------------------------------------------------------


async def handle(
    inputs: list[dict[str, Any]],
    options: Mapping[str, Any],
    deps: Any | None,
) -> AnalystMethodResult:
    """Sub-handler entry point — the weekly-idempotent acute-forecast drive.

    A META single global sweep: the cadence actor hands the generic signals slice
    as ``inputs`` (ignored — everything the pilot needs is pulled per-country
    inside the forecast_acute writers). ``deps`` (or its pool) being None degrades
    to the HONEST empty receipt (issued=0 / resolved=0, a TRACE_ONLY summary "no
    pool", writes nothing), NOT a stub.
    """
    pool = getattr(deps, "pg_pool", None) if deps is not None else None

    warnings: list[str] = []
    if pool is None:
        warnings.append("forecast_scoreboard.no_pool")
        return AnalystMethodResult(
            finding=build_receipt(
                issued=0, resolved=0, resolved_total=0, warnings=warnings
            ),
            usage={"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0},
        )

    # ISSUE — one binary forecast per G20 country for the NEXT weekly window.
    # Idempotent ON CONFLICT; forecast_acute internally ABSTAINS (issues 0) on a
    # degenerate / geography-dominated p-vector (D9). This producer NEVER pre-
    # checks or bypasses that guard. DQ P6 — pass a receipt sink so an issued=0
    # tick is ATTRIBUTED: a D9 degeneracy abstain, an idempotent window-already-
    # issued no-op, and a genuine no-targets case each emit a DISTINCT warnings[]
    # entry (they used to be indistinguishable as "issued=0 warnings=[]").
    issued = 0
    issue_receipt: dict[str, Any] = {}
    try:
        issued = await forecast_acute.issue_weekly_forecasts(
            deps, options, receipt=issue_receipt
        )
    except Exception as exc:  # noqa: BLE001 — one leg never aborts the other
        logger.warning("forecast_scoreboard.issue_failed err=%s", exc)
        warnings.append("forecast_scoreboard.issue_failed")
    _reason = str(issue_receipt.get("reason") or "")
    if issued == 0:
        if _reason == "abstained_degenerate":
            warnings.append(
                "forecast_scoreboard.abstained_degenerate_p"
                f"(staged={issue_receipt.get('staged')},"
                f"uncertain={issue_receipt.get('uncertain')})"
            )
        elif _reason == "window_already_issued":
            warnings.append("forecast_scoreboard.window_already_issued")
        elif _reason == "no_regions":
            warnings.append("forecast_scoreboard.issued_0_no_targets")
        elif _reason not in ("", "issue_failed"):
            warnings.append(f"forecast_scoreboard.issued_0_{_reason}")

    # RESOLVE — grade every forecast whose forward window has closed + settled,
    # exogenously (UPSTREAM event time). Never overwrites an already-resolved row.
    # 2026-08-02 — receipt sink, same treatment as the issue leg above: a bare
    # `resolved=0` could not distinguish "nothing was due" from "every due row
    # was skipped or threw", and a review reading the daily line concluded the
    # leg was dead when it had simply never been handed a gradeable row.
    resolved = 0
    resolve_receipt: dict[str, Any] = {}
    try:
        resolved = await forecast_acute.resolve_open_acute_forecasts(
            deps, options, receipt=resolve_receipt
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("forecast_scoreboard.resolve_failed err=%s", exc)
        warnings.append("forecast_scoreboard.resolve_failed")
    _rreason = str(resolve_receipt.get("reason") or "")
    if resolved == 0 and _rreason not in ("", "nothing_due"):
        warnings.append(f"forecast_scoreboard.resolved_0_{_rreason}")
    # The backlog self-check is INDEPENDENT of this tick's outcome: gradeable
    # rows left unresolved after a pass mean the leg is failing, and one honest
    # counter here is what would have surfaced that months ago.
    if int(resolve_receipt.get("stale_unresolved") or 0) > 0:
        warnings.append(
            "forecast_scoreboard.resolve_backlog"
            f"(n={resolve_receipt.get('stale_unresolved')},"
            f"oldest_days={resolve_receipt.get('stale_oldest_days')})"
        )

    # PULL — resolved pilot calls (read-only). H13: the rows now ALSO feed the
    # receipt's brier_answered (the answered-only Brier, today's number).
    resolved_total = 0
    resolved_rows: list[dict[str, Any]] = []
    brier_answered: float | None = None
    try:
        resolved_rows = await forecast_acute.pull_resolved_acute_forecasts(
            deps, options
        )
        resolved_total = len(resolved_rows)
        brier_answered = brier_of(resolved_rows)
    except Exception as exc:  # noqa: BLE001
        logger.warning("forecast_scoreboard.pull_failed err=%s", exc)
        warnings.append("forecast_scoreboard.pull_failed")

    # H13 — the expired backlog (due-but-never-resolved rows) and the honest
    # brier_all that holds them at maximum penalty. Both best-effort.
    expired_count = 0
    try:
        expired_count = await forecast_acute.pull_expired_forecast_count(
            deps, options
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("forecast_scoreboard.expired_failed err=%s", exc)
        warnings.append("forecast_scoreboard.expired_failed")
    brier_all = brier_all_of(resolved_rows, expired_count)

    # H13 — hypotheses by minter: the units mint ~1,500 rows nobody resolves;
    # the published open-vs-resolved split is where that bias shows.
    hypothesis_minters: list[dict[str, Any]] = []
    try:
        hypothesis_minters = await _pull_hypothesis_minter_counts(pool)
    except Exception as exc:  # noqa: BLE001
        logger.warning("forecast_scoreboard.minters_failed err=%s", exc)
        warnings.append("forecast_scoreboard.minters_failed")

    logger.info(
        "forecast_scoreboard.tick issued=%d resolved=%d resolved_total=%d "
        "expired=%d class=%s",
        issued, resolved, resolved_total, expired_count, forecast_acute.EVENT_CLASS,
    )
    return AnalystMethodResult(
        finding=build_receipt(
            issued=issued,
            resolved=resolved,
            resolved_total=resolved_total,
            warnings=warnings,
            brier_answered=brier_answered,
            brier_all=brier_all,
            expired_count=expired_count,
            hypothesis_minters=hypothesis_minters,
        ),
        usage={"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0},
    )


__all__ = [
    "handle",
    "build_receipt",
    "brier_of",
    "brier_all_of",
    "SUB_HANDLER_NAME",
]
