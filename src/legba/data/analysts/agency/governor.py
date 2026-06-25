# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Per-pack governor enforcement (P-11 — extends the budget ledger / 0022).

The pack governor is the RUNTIME gate that answers *may this granted pack tool
run RIGHT NOW*. Resolution (:mod:`.resolution`) already established the pack is
granted+allowed+applicable; this layer enforces the :class:`PackGovernor` caps
over a rolling window against the ``action_pack_invocations`` ledger (defined in
the 0001 baseline schema), AND consults the system-wide ``global_budget_envelope`` (migration 0022)
so a pack call respects the global token cap that gates the rest of the runtime.

Enforced cap dimensions (each independently; any breach → BLOCK):

  * ``max_invocations_per_hour``  — count of invocations in the trailing hour.
  * ``api_rate_per_minute``       — count of invocations in the trailing minute.
  * ``max_cost_usd_per_day``      — summed ``cost_usd`` for the UTC day.
  * ``max_sources_per_window``    — summed ``units`` in the trailing hour
                                    (the crawl/discovery "sources discovered"
                                    cap; ``units`` is the per-call source count).
  * global envelope               — if the system-wide ``tokens_cap`` is fully
                                    consumed for the bucket, the pack call is
                                    blocked too (the global gate beats per-pack
                                    head-room — matches BudgetEnforcer ordering).

A breach returns a :class:`GovernorDecision` with ``admitted=False`` and the
precise ``cap_dimension`` / ``cap_limit`` / ``observed`` so the caller stamps an
operator-visible BLOCK event. An admitted call gets ``admitted=True``; the
caller then records the invocation row (cost/units) so the NEXT call sees it.

The window queries are conservative + pre-call: they count what already
happened. ``estimated_cost`` / ``estimated_units`` are the forward-looking
allowance for THIS call so a call that WOULD cross a cap is blocked before it
runs (not after).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID

import asyncpg

from ...schemas.action_pack import PackGovernor

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


def _today_utc() -> date:
    return _utcnow().date()


# --- Env-overridable timing knobs --------------------------------------------
_PACK_TOOL_TIMEOUT_ENV = "LEGBA_PACK_TOOL_TIMEOUT_SECONDS"
_PACK_TOOL_TIMEOUT_DEFAULT_S = 60
_STALE_RECONCILE_ENV = "LEGBA_PACK_TOOL_STALE_RECONCILE_SECONDS"
_STALE_RECONCILE_DEFAULT_S = 300


def _env_positive_int(name: str, default: int) -> int:
    """Resolve a positive int from the environment, falling back to ``default``
    on an unset / malformed / non-positive value so a typo never silently
    disables the bound."""
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        val = int(raw)
    except (TypeError, ValueError):
        logger.warning("%s.bad_env value=%r — using default %d", name, raw, default)
        return default
    if val <= 0:
        logger.warning("%s.non_positive value=%d — using default %d", name, val, default)
        return default
    return val


def pack_tool_timeout_seconds() -> int:
    """Wall-clock budget for a single pack-tool handler dispatch (env-overridable).

    Deliberately SHORTER than the actor-invoke budget
    (:func:`legba.runtime.source_first_runtime.actor_invoke_timeout_seconds`, 180s)
    so a wedged handler is caught at the tool boundary — settling the ledger row
    failed + reclaiming the actor — rather than pinning the actor for the full
    invoke window and leaving the row stuck ``admitted``.
    """
    return _env_positive_int(_PACK_TOOL_TIMEOUT_ENV, _PACK_TOOL_TIMEOUT_DEFAULT_S)


def pack_tool_stale_reconcile_seconds() -> int:
    """Age past which a still-``admitted`` invocation row is treated as orphaned
    (the dispatching process died before the per-call timeout/settle could run)
    and swept to ``failed`` by the leader-gated reconcile. Comfortably larger
    than the tool timeout + the actor-invoke budget so a legitimately in-flight
    call is never reaped."""
    return _env_positive_int(_STALE_RECONCILE_ENV, _STALE_RECONCILE_DEFAULT_S)


@dataclass(frozen=True)
class GovernorDecision:
    """Outcome of a per-pack governor pre-call check.

    ``admitted`` True → the call may run. False → blocked; ``cause`` is the
    machine-readable block reason and ``cap_dimension``/``cap_limit``/
    ``observed`` describe the cap that fired (for the operator event).
    """

    admitted: bool
    cause: str = "ok"
    cap_dimension: str | None = None
    cap_limit: float | None = None
    observed: float | None = None
    detail: str = ""


class PackGovernorEnforcer:
    """Enforces one pack's :class:`PackGovernor` over the invocation ledger.

    Construct per (pack, account); the pre-call check + post-call record share
    the pack id + governor + account. Holds no connection — every method takes
    an ``asyncpg`` connection so the caller owns pooling/transactions (matches
    the runtime ``BudgetEnforcer`` convention).
    """

    def __init__(
        self,
        *,
        pack_id: str,
        pack_version: str = "",
        governor: PackGovernor | None,
        budget_account: str,
        tenant_id: str = "default",
    ) -> None:
        self.pack_id = pack_id
        self.pack_version = pack_version
        self.governor = governor or PackGovernor()
        self.budget_account = budget_account
        self.tenant_id = tenant_id

    # ------------------------------------------------------------------
    # Window aggregates
    # ------------------------------------------------------------------

    async def _count_since(
        self, conn: asyncpg.Connection, *, seconds: int, now: datetime
    ) -> int:
        row = await conn.fetchrow(
            """
            SELECT COUNT(*) AS n
            FROM action_pack_invocations
            WHERE pack_id = $1 AND budget_account = $2
              AND occurred_at >= $3
            """,
            self.pack_id, self.budget_account,
            now - _delta(seconds),
        )
        return int(row["n"]) if row else 0

    async def _units_since(
        self, conn: asyncpg.Connection, *, seconds: int, now: datetime
    ) -> int:
        row = await conn.fetchrow(
            """
            SELECT COALESCE(SUM(units), 0)::BIGINT AS u
            FROM action_pack_invocations
            WHERE pack_id = $1 AND budget_account = $2
              AND occurred_at >= $3
            """,
            self.pack_id, self.budget_account,
            now - _delta(seconds),
        )
        return int(row["u"]) if row and row["u"] is not None else 0

    async def _cost_today(
        self, conn: asyncpg.Connection, *, bucket: date
    ) -> Decimal:
        row = await conn.fetchrow(
            """
            SELECT COALESCE(SUM(cost_usd), 0)::NUMERIC AS c
            FROM action_pack_invocations
            WHERE pack_id = $1 AND budget_account = $2
              AND occurred_at >= $3::date AND occurred_at < ($3::date + INTERVAL '1 day')
            """,
            self.pack_id, self.budget_account, bucket,
        )
        return Decimal(row["c"]) if row and row["c"] is not None else Decimal(0)

    async def _global_exhausted(
        self, conn: asyncpg.Connection, *, bucket: date
    ) -> tuple[bool, int | None, int]:
        """Consult the system-wide token envelope (0022). Returns
        (exhausted, cap, used)."""
        gr = await conn.fetchrow(
            "SELECT tokens_cap FROM global_budget_envelope WHERE bucket = $1",
            bucket,
        )
        if gr is None or gr["tokens_cap"] is None:
            return False, None, 0
        cap = int(gr["tokens_cap"])
        ur = await conn.fetchrow(
            "SELECT COALESCE(SUM(tokens_used), 0)::BIGINT AS used "
            "FROM budget_ledger WHERE bucket = $1",
            bucket,
        )
        used = int(ur["used"]) if ur and ur["used"] is not None else 0
        return used >= cap, cap, used

    # ------------------------------------------------------------------
    # Pre-call gate
    # ------------------------------------------------------------------

    async def precall_check(
        self,
        conn: asyncpg.Connection,
        *,
        estimated_cost_usd: float = 0.0,
        estimated_units: int = 1,
        now: datetime | None = None,
    ) -> GovernorDecision:
        """Decide whether one pack-tool invocation may proceed.

        Each declared cap is checked; the FIRST breach short-circuits with a
        BLOCK (the global envelope is checked first — system-wide beats
        per-pack, matching :class:`legba.runtime.budget.BudgetEnforcer`). A
        cap left ``None`` on the governor is unenforced (no limit on that
        dimension). An empty governor admits everything.
        """
        now = now or _utcnow()
        bucket = now.date()
        g = self.governor

        # ---- Global envelope (system-wide) first -------------------------
        exhausted, gcap, gused = await self._global_exhausted(conn, bucket=bucket)
        if exhausted:
            return GovernorDecision(
                admitted=False, cause="global_exhausted",
                cap_dimension="global_tokens", cap_limit=float(gcap or 0),
                observed=float(gused),
                detail=f"global token envelope used {gused} >= cap {gcap}",
            )

        # ---- api_rate_per_minute -----------------------------------------
        if g.api_rate_per_minute is not None:
            n = await self._count_since(conn, seconds=60, now=now)
            if n + 1 > g.api_rate_per_minute:
                return GovernorDecision(
                    admitted=False, cause="over_rate",
                    cap_dimension="api_rate_per_minute",
                    cap_limit=float(g.api_rate_per_minute), observed=float(n + 1),
                    detail=f"{n} calls in trailing minute; +1 exceeds "
                           f"cap {g.api_rate_per_minute}",
                )

        # ---- max_invocations_per_hour ------------------------------------
        if g.max_invocations_per_hour is not None:
            n = await self._count_since(conn, seconds=3600, now=now)
            if n + 1 > g.max_invocations_per_hour:
                return GovernorDecision(
                    admitted=False, cause="over_rate",
                    cap_dimension="max_invocations_per_hour",
                    cap_limit=float(g.max_invocations_per_hour),
                    observed=float(n + 1),
                    detail=f"{n} invocations in trailing hour; +1 exceeds "
                           f"cap {g.max_invocations_per_hour}",
                )

        # ---- max_sources_per_window (trailing hour, summed units) --------
        if g.max_sources_per_window is not None:
            u = await self._units_since(conn, seconds=3600, now=now)
            if u + estimated_units > g.max_sources_per_window:
                return GovernorDecision(
                    admitted=False, cause="over_rate",
                    cap_dimension="max_sources_per_window",
                    cap_limit=float(g.max_sources_per_window),
                    observed=float(u + estimated_units),
                    detail=f"{u} units in window; +{estimated_units} exceeds "
                           f"cap {g.max_sources_per_window}",
                )

        # ---- max_cost_usd_per_day ----------------------------------------
        if g.max_cost_usd_per_day is not None:
            used = await self._cost_today(conn, bucket=bucket)
            projected = used + Decimal(str(estimated_cost_usd))
            if projected > Decimal(str(g.max_cost_usd_per_day)):
                return GovernorDecision(
                    admitted=False, cause="over_budget",
                    cap_dimension="max_cost_usd_per_day",
                    cap_limit=float(g.max_cost_usd_per_day),
                    observed=float(projected),
                    detail=f"day cost {used} + est {estimated_cost_usd} exceeds "
                           f"cap {g.max_cost_usd_per_day}",
                )

        return GovernorDecision(admitted=True, cause="ok")

    # ------------------------------------------------------------------
    # Post-call ledger write
    # ------------------------------------------------------------------

    async def record_invocation(
        self,
        conn: asyncpg.Connection,
        *,
        tool_name: str,
        requested_by: str = "system",
        cost_usd: float = 0.0,
        units: int = 1,
        outcome: str = "admitted",
        job_id: UUID | None = None,
    ) -> UUID:
        """Append the admitted invocation onto the ledger (rolling-window state).

        Called AFTER ``precall_check`` admits. The row makes this call visible to
        the next ``precall_check`` (the rate/budget windows close over it). Cost
        and units are what THIS call actually consumed (or the pre-call estimate
        when the true cost isn't known until later — the caller may re-stamp via
        ``settle``).
        """
        return await conn.fetchval(
            """
            INSERT INTO action_pack_invocations (
                pack_id, pack_version, tool_name, budget_account, requested_by,
                tenant_id, cost_usd, units, outcome, job_id
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
            RETURNING id
            """,
            self.pack_id, self.pack_version, tool_name, self.budget_account,
            requested_by, self.tenant_id, Decimal(str(cost_usd)), units,
            outcome, job_id,
        )

    @staticmethod
    async def settle(
        conn: asyncpg.Connection,
        invocation_id: UUID,
        *,
        outcome: str,
        cost_usd: float | None = None,
        units: int | None = None,
    ) -> None:
        """Stamp the final outcome (+ true cost/units) onto an invocation row."""
        sets = ["outcome = $2"]
        params: list[Any] = [invocation_id, outcome]
        if cost_usd is not None:
            params.append(Decimal(str(cost_usd)))
            sets.append(f"cost_usd = ${len(params)}")
        if units is not None:
            params.append(units)
            sets.append(f"units = ${len(params)}")
        await conn.execute(
            f"UPDATE action_pack_invocations SET {', '.join(sets)} WHERE id = $1",
            *params,
        )

    @staticmethod
    async def reconcile_stale_admitted(
        conn: asyncpg.Connection,
        *,
        older_than_seconds: int,
    ) -> int:
        """Settle orphaned ``admitted`` invocation rows to ``failed``.

        ``run_pack_tool`` settles every row it dispatches — on success, on a
        handler crash, AND on the per-call timeout. The one path it CANNOT
        reach is the dispatching PROCESS dying mid-handler: that leaves the row
        stuck at ``admitted`` forever. This leader-gated sweep flips rows older
        than ``older_than_seconds`` to ``failed`` so the ledger reflects reality.

        NOTE — this is ledger/observability hygiene, NOT budget reclamation. The
        cap windows in this module count rows REGARDLESS of outcome (a failed
        call still counts against the rate windows, by anti-bypass design; the
        common ``cost_usd`` admit estimate is 0 and the rate windows self-heal as
        they slide), so reconciling an orphan does not free rate/cost head-room.
        It heals the perpetual-``admitted`` wart and the crash path the per-call
        timeout misses. Returns the number of rows reconciled.
        """
        n = await conn.fetchval(
            """
            WITH swept AS (
                UPDATE action_pack_invocations
                   SET outcome = 'failed'
                 WHERE outcome = 'admitted'
                   AND occurred_at < (now() - make_interval(secs => $1))
                RETURNING 1
            )
            SELECT COUNT(*) FROM swept
            """,
            older_than_seconds,
        )
        return int(n or 0)


def _delta(seconds: int):
    from datetime import timedelta
    return timedelta(seconds=seconds)


__all__ = ["GovernorDecision", "PackGovernorEnforcer"]
