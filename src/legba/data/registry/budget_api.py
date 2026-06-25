# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Budget + cost telemetry surface for P-17 (Budget Ledger panel).

Three GET endpoints, all under `/api/v1/budget/` once mounted by
`server.py` (which is intentionally NOT modified by this module — wire it
in there with:

    from .budget_api import build_budget_router
    app.include_router(build_budget_router(deps), prefix="/api/v1/budget")

The panel slices the data three ways:

  1. ``/ledger`` — `budget_ledger` rows, the per-(analyst, bucket) token
     + dollar burn tally. Powers the per-analyst time series and the
     per-day stacked-bar.
  2. ``/envelope`` — current `global_budget_envelope` row(s) plus the
     live rollup (sum of `budget_ledger.tokens_used` /
     `cost_estimate_usd` for the same bucket). Powers the global
     envelope gauge.
  3. ``/demotions`` — `budget_demotion_events` audit table for the
     "demoted at HH:MM, cause=…" inline log.

Reads only. The writer side is :mod:`legba.runtime.budget`
(BudgetEnforcer) — that's where ledger rows + demotion events get
appended in the runtime hot path; this router doesn't duplicate any of
that logic.

The Decimal columns (``cost_usd``, ``cost_estimate_usd``, ``usd_cap``,
``current_cost_usd``) serialize as **JSON strings** rather than the
FastAPI default of ``float``. NUMERIC(12,6) and NUMERIC(18,6) carry
six decimal places — round-tripping through Python ``float`` loses
precision past ~15 significant digits, which matters for
micro-dollar accumulation (sub-cent rounding compounds over thousands
of runs into visible drift). The frontend treats these as strings and
parses with a fixed-point lib for display.

Bucket granularity:
  The substrate column is ``budget_ledger.bucket DATE`` (UTC day,
  per migration 0005). There is no hour-grained data in the schema, so
  ``bucket_granularity=hour`` returns the same day-grained rows that
  ``day`` does and documents the limitation. When/if a future
  migration adds an hour-grained ledger, this endpoint gains a real
  resampling code path; until then, "hour" is a no-op the UI can
  pass without erroring.

No synthetic fields — there's no "burn forecast", "predicted
runway", or fabricated trend line here. Only observed rows.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field, field_serializer

from .api import RegistryAPIDeps, require_bearer


# ---------------------------------------------------------------------------
# Response models — column-for-column views of the underlying tables.
# ---------------------------------------------------------------------------


class BudgetLedgerRow(BaseModel):
    """One ``budget_ledger`` row.

    Mirrors migration 0005 + 0015 column-for-column. The primary key is
    ``(analyst_id, analyst_version, bucket)``; ``bucket`` is a UTC date.

    ``cost_usd`` is the operator-stamped authoritative figure (filled
    from provider invoice reconciliation, NUMERIC(18,6)). It stays
    zero in the common path. ``cost_estimate_usd`` is the
    price-table-derived figure stamped by ``record_budget`` at write
    time (NUMERIC(12,6)). The panel surfaces the estimate; the
    authoritative column is exposed so an operator with the
    reconciled invoice can compare.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    analyst_id: str
    analyst_version: str
    bucket: date
    tokens_used: int
    runs: int
    cost_usd: Decimal
    cost_estimate_usd: Decimal
    last_updated: datetime

    @field_serializer("cost_usd", "cost_estimate_usd")
    def _ser_decimal(self, v: Decimal) -> str:
        # Stringify Decimals to preserve NUMERIC(12,6)/NUMERIC(18,6)
        # precision through JSON. Default FastAPI emits floats, which
        # silently truncate past ~15 significant digits.
        return format(v, "f")


class BudgetEnvelopeState(BaseModel):
    """Current state of the global budget envelope for a given bucket.

    Combines the operator-set caps from ``global_budget_envelope``
    (migration 0022) with the live rollup of ``budget_ledger`` for the
    same bucket. The runtime's BudgetEnforcer reads both side-by-side
    in the hot path; this endpoint exposes the same join for the UI.

    Fields:
      * ``bucket`` — UTC day the envelope applies to.
      * ``tokens_cap`` / ``usd_cap`` — operator-set caps, NULL = no cap.
      * ``on_exceeded`` — policy at cap-hit: 'demote_all', 'pause_all',
        'alert_only'. Default 'demote_all'.
      * ``note`` — operator notes for this bucket's envelope.
      * ``current_tokens`` — SUM(``budget_ledger.tokens_used``) for the
        bucket across all analysts.
      * ``current_cost_usd`` — SUM(``budget_ledger.cost_estimate_usd``)
        for the bucket across all analysts.
      * ``demoted`` — True iff the runtime would treat this bucket as
        envelope-exhausted RIGHT NOW (current_tokens >= tokens_cap, OR
        current_cost_usd >= usd_cap; either dimension trips it). NULL
        if no cap is configured on the relevant dimension.
      * ``last_updated`` — last time the envelope row itself was
        touched. NULL when no envelope row exists yet for the bucket.

    When no envelope row exists for the bucket, ``tokens_cap``,
    ``usd_cap``, ``on_exceeded``, ``note``, ``last_updated`` are all
    NULL but the ``current_*`` rollup is still populated — operators can
    see the live spend with no cap configured. ``demoted`` is NULL in
    that case (no cap → no demotion possible).
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    bucket: date
    tokens_cap: int | None
    usd_cap: Decimal | None
    on_exceeded: str | None
    note: str | None
    current_tokens: int
    current_cost_usd: Decimal
    demoted: bool | None
    last_updated: datetime | None

    @field_serializer("usd_cap", "current_cost_usd")
    def _ser_decimal(self, v: Decimal | None) -> str | None:
        if v is None:
            return None
        return format(v, "f")


class DemotionEvent(BaseModel):
    """One ``budget_demotion_events`` row.

    Mirrors migration 0022 column-for-column.

      * ``cause`` is 'per_analyst' or 'global' (matches the table's
        CHECK constraint).
      * ``primary_llm`` / ``fallback_llm`` are the stack-refs the
        actor demoted FROM and TO at the moment of the event.
      * ``tokens_used_at_demote`` / ``tokens_cap_at_demote`` are the
        cap-snapshot at the demotion moment (informational; the live
        cap may have changed since).
    """

    id: str
    analyst_id: str
    analyst_version: str
    bucket: date
    cause: str
    tokens_used_at_demote: int | None
    tokens_cap_at_demote: int | None
    primary_llm: str | None
    fallback_llm: str | None
    occurred_at: datetime


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------


def build_budget_router(deps: RegistryAPIDeps) -> APIRouter:
    """Construct the P-17 budget telemetry router bound to the registry deps.

    Mount on the same FastAPI app that hosts the v1 registry + v3
    telemetry routers. In ``server.py``::

        from .budget_api import build_budget_router
        app.include_router(build_budget_router(deps), prefix="/api/v1/budget")

    All three routes require the same bearer-token gate
    (`require_bearer` from `api.py`) so the auth surface stays
    consistent with the rest of the registry API.
    """
    router = APIRouter(tags=["budget"])

    # ------------------------------------------------------------------
    # /ledger — per-(analyst, bucket) token + dollar burn rows
    # ------------------------------------------------------------------

    @router.get("/ledger", response_model=list[BudgetLedgerRow])
    async def list_budget_ledger(
        analyst_id: str | None = Query(
            default=None,
            description="Filter to one analyst. Combine with from/to for a "
                        "per-analyst time series.",
        ),
        from_: date | None = Query(
            default=None,
            alias="from",
            description="Inclusive lower bound on `bucket` (UTC day).",
        ),
        to: date | None = Query(
            default=None,
            description="Inclusive upper bound on `bucket` (UTC day).",
        ),
        bucket_granularity: Literal["day", "hour"] = Query(
            default="day",
            description=(
                "Bucket granularity. The substrate column is DATE (UTC day) "
                "per migration 0005 — no hour-grained rows exist yet. "
                "Passing 'hour' returns the same day-grained rows that "
                "'day' returns; the parameter is accepted for forward "
                "compatibility but does NOT currently resample to hours."
            ),
        ),
        limit: int = Query(default=1000, ge=1, le=10_000),
        _principal: str = Depends(require_bearer),
    ) -> list[BudgetLedgerRow]:
        # NOTE on bucket_granularity: parameter is accepted (the UI may
        # pass it) but the substrate has no hour-grained ledger. We do
        # not invent hour buckets by splitting day rows — that would be
        # a synthetic field. When a future migration adds an hour-grained
        # ledger column, this branch grows a real resampling path.
        _ = bucket_granularity  # explicitly documented no-op

        clauses: list[str] = []
        params: list[Any] = []
        if analyst_id is not None:
            params.append(analyst_id)
            clauses.append(f"analyst_id = ${len(params)}")
        if from_ is not None:
            params.append(from_)
            clauses.append(f"bucket >= ${len(params)}")
        if to is not None:
            params.append(to)
            clauses.append(f"bucket <= ${len(params)}")
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        sql = (
            "SELECT analyst_id, analyst_version, bucket, tokens_used, runs, "
            "cost_usd, cost_estimate_usd, last_updated "
            "FROM budget_ledger"
            f"{where} "
            "ORDER BY bucket DESC, analyst_id ASC, analyst_version ASC "
            f"LIMIT ${len(params)}"
        )

        async with deps.descriptor_registry.pg.acquire() as conn:
            rows = await conn.fetch(sql, *params)

        return [
            BudgetLedgerRow(
                analyst_id=r["analyst_id"],
                analyst_version=r["analyst_version"],
                bucket=r["bucket"],
                tokens_used=int(r["tokens_used"]),
                runs=int(r["runs"]),
                cost_usd=Decimal(r["cost_usd"]),
                cost_estimate_usd=Decimal(r["cost_estimate_usd"]),
                last_updated=r["last_updated"],
            )
            for r in rows
        ]

    # ------------------------------------------------------------------
    # /envelope — current state of the global budget envelope
    # ------------------------------------------------------------------

    @router.get("/envelope", response_model=BudgetEnvelopeState)
    async def get_budget_envelope(
        bucket: date | None = Query(
            default=None,
            description="UTC day to inspect. Defaults to today.",
        ),
        _principal: str = Depends(require_bearer),
    ) -> BudgetEnvelopeState:
        target_bucket = bucket or datetime.utcnow().date()

        async with deps.descriptor_registry.pg.acquire() as conn:
            env_row = await conn.fetchrow(
                """
                SELECT bucket, tokens_cap, usd_cap, on_exceeded, note,
                       last_updated
                FROM global_budget_envelope
                WHERE bucket = $1
                """,
                target_bucket,
            )
            # Live rollup. Run regardless of whether the envelope row
            # exists — operators want to see live spend even with no cap
            # configured.
            rollup = await conn.fetchrow(
                """
                SELECT COALESCE(SUM(tokens_used), 0)::BIGINT       AS tokens,
                       COALESCE(SUM(cost_estimate_usd), 0)::NUMERIC AS cost
                FROM budget_ledger
                WHERE bucket = $1
                """,
                target_bucket,
            )

        current_tokens = int(rollup["tokens"]) if rollup else 0
        current_cost = (
            Decimal(rollup["cost"]) if rollup and rollup["cost"] is not None
            else Decimal("0")
        )

        if env_row is None:
            return BudgetEnvelopeState(
                bucket=target_bucket,
                tokens_cap=None,
                usd_cap=None,
                on_exceeded=None,
                note=None,
                current_tokens=current_tokens,
                current_cost_usd=current_cost,
                demoted=None,
                last_updated=None,
            )

        tokens_cap = (
            int(env_row["tokens_cap"]) if env_row["tokens_cap"] is not None
            else None
        )
        usd_cap = (
            Decimal(env_row["usd_cap"]) if env_row["usd_cap"] is not None
            else None
        )
        # `demoted` mirrors BudgetEnforcer.precall_check semantics: either
        # dimension exhausted trips it. NULL when no cap is configured on
        # either dimension (you can't be demoted without a cap).
        demoted: bool | None
        if tokens_cap is None and usd_cap is None:
            demoted = None
        else:
            demoted = False
            if tokens_cap is not None and current_tokens >= tokens_cap:
                demoted = True
            if usd_cap is not None and current_cost >= usd_cap:
                demoted = True

        return BudgetEnvelopeState(
            bucket=env_row["bucket"],
            tokens_cap=tokens_cap,
            usd_cap=usd_cap,
            on_exceeded=env_row["on_exceeded"],
            note=env_row["note"],
            current_tokens=current_tokens,
            current_cost_usd=current_cost,
            demoted=demoted,
            last_updated=env_row["last_updated"],
        )

    # ------------------------------------------------------------------
    # /demotions — budget_demotion_events audit table
    # ------------------------------------------------------------------

    @router.get("/demotions", response_model=list[DemotionEvent])
    async def list_demotions(
        analyst_id: str | None = Query(
            default=None,
            description="Filter to one analyst.",
        ),
        since: datetime | None = Query(
            default=None,
            description="Inclusive lower bound on `occurred_at`.",
        ),
        limit: int = Query(default=100, ge=1, le=1000),
        _principal: str = Depends(require_bearer),
    ) -> list[DemotionEvent]:
        clauses: list[str] = []
        params: list[Any] = []
        if analyst_id is not None:
            params.append(analyst_id)
            clauses.append(f"analyst_id = ${len(params)}")
        if since is not None:
            params.append(since)
            clauses.append(f"occurred_at >= ${len(params)}")
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        sql = (
            "SELECT id, analyst_id, analyst_version, bucket, cause, "
            "tokens_used_at_demote, tokens_cap_at_demote, primary_llm, "
            "fallback_llm, occurred_at "
            "FROM budget_demotion_events"
            f"{where} "
            "ORDER BY occurred_at DESC "
            f"LIMIT ${len(params)}"
        )

        async with deps.descriptor_registry.pg.acquire() as conn:
            rows = await conn.fetch(sql, *params)

        return [
            DemotionEvent(
                id=str(r["id"]),
                analyst_id=r["analyst_id"],
                analyst_version=r["analyst_version"],
                bucket=r["bucket"],
                cause=r["cause"],
                tokens_used_at_demote=(
                    int(r["tokens_used_at_demote"])
                    if r["tokens_used_at_demote"] is not None
                    else None
                ),
                tokens_cap_at_demote=(
                    int(r["tokens_cap_at_demote"])
                    if r["tokens_cap_at_demote"] is not None
                    else None
                ),
                primary_llm=r["primary_llm"],
                fallback_llm=r["fallback_llm"],
                occurred_at=r["occurred_at"],
            )
            for r in rows
        ]

    return router


__all__ = [
    "BudgetEnvelopeState",
    "BudgetLedgerRow",
    "DemotionEvent",
    "build_budget_router",
]
