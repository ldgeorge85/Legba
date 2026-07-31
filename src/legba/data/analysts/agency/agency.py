# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Action-pack agency orchestrator (P-11 — the complete seam).

``run_pack_tool`` is the ONE entry point an analyst's method (or a tool-using
loop) calls to invoke a pack action. It runs the full hard-gate pipeline in
order, and is fail-closed at every step:

    1. RESOLVE   — three-way allow-list (analyst grant ∩ target allow ∩
                   applicability). A denial → BLOCK event, return; the tool
                   never runs. (:mod:`.resolution`)
    2. TOOL KNOWN — the requested tool must be named in the pack AND have a
                    registered handler. Unknown → BLOCK event, return.
    3. GOVERN    — the per-pack governor's rate/budget/source caps + the global
                   token envelope. A breach → BLOCK event, return; the tool
                   never runs. (:mod:`.governor`)
    4. RECORD    — the admit lands an ``action_pack_invocations`` row (so the
                   next call's window sees it) + an ALLOW governor event.
    5. DISPATCH  — the tool handler runs (enqueue a job / emit to channels),
                   bounded by a wall-clock timeout so a wedged handler settles
                   failed instead of pinning the actor.
                   (:func:`.governor.pack_tool_timeout_seconds`)
    6. SETTLE    — the invocation row's outcome (+ true cost/units) is stamped.

Every BLOCK is operator-visible (a ``governor_events`` row + best-effort NATS
publish). The acceptance criteria map 1:1:
  * a pack the domain doesn't allow CANNOT run        → step 1 BLOCK.
  * process_media enqueues a real job                 → step 5 dispatch.
  * over-budget / over-rate is BLOCKED + visible      → step 3 BLOCK event.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

import asyncpg

from ...run_accounting import record_tool_call
from ...schemas.action_pack import ActionPack
from .events import GovernorEvent, NatsPublish, record_governor_event
from .governor import PackGovernorEnforcer, pack_tool_timeout_seconds
from .resolution import (
    PackResolution,
    TargetScopeView,
    resolve_pack,
    scope_view_from_target,
)
from .tools import ToolCall, ToolContext, ToolRegistry, ToolResult, default_tool_registry

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AgencyOutcome:
    """The result of a ``run_pack_tool`` call.

    ``admitted`` False → the hard gate blocked the call (``block_cause`` says
    which rail); ``tool_result`` is None. True → the tool ran; ``tool_result``
    carries its outcome.
    """

    admitted: bool
    pack_id: str
    tool_name: str
    block_cause: str | None = None
    detail: str = ""
    resolution: PackResolution | None = None
    tool_result: ToolResult | None = None


class Agency:
    """Resolve → govern → dispatch a pack tool, with operator-visible events.

    One instance per process; cheap to construct. Holds the tool registry +
    optional NATS publisher (for governor-event + channel telemetry). The DB
    connection is passed per call so the caller owns pooling/transactions.
    """

    def __init__(
        self,
        *,
        tool_registry: ToolRegistry | None = None,
        nats_publish: NatsPublish | None = None,
    ) -> None:
        self._tools = tool_registry or default_tool_registry()
        self._nats_publish = nats_publish

    async def run_pack_tool(
        self,
        conn: asyncpg.Connection,
        *,
        pack: ActionPack,
        call: ToolCall,
        analyst_grants: list[Any] | None,
        target_allows: list[Any] | None,
        scope: TargetScopeView,
        ctx: ToolContext,
        estimated_cost_usd: float = 0.0,
        estimated_units: int = 1,
    ) -> AgencyOutcome:
        """Run the full hard-gate pipeline for one pack-tool call.

        R11: this is the ONE entry point for a pack action, and it has SIX exit
        paths (three blocks, a timeout, a handler crash, the settled success),
        so the per-run ``analyst_traces.tool_calls`` receipt is stamped HERE —
        around :meth:`_run_pack_tool` — rather than at each return. Every
        outcome including a BLOCK therefore reaches the receipt; the durable
        ``action_pack_invocations`` / ``governor_events`` ledgers are unchanged.
        """
        started = time.monotonic()
        outcome: AgencyOutcome | None = None
        raised: BaseException | None = None
        try:
            outcome = await self._run_pack_tool(
                conn,
                pack=pack,
                call=call,
                analyst_grants=analyst_grants,
                target_allows=target_allows,
                scope=scope,
                ctx=ctx,
                estimated_cost_usd=estimated_cost_usd,
                estimated_units=estimated_units,
            )
            return outcome
        except BaseException as exc:
            raised = exc
            raise
        finally:
            self._account_tool_call(
                pack=pack, call=call, started_monotonic=started,
                outcome=outcome, exc=raised,
            )

    def _account_tool_call(
        self,
        *,
        pack: ActionPack,
        call: ToolCall,
        started_monotonic: float,
        outcome: AgencyOutcome | None,
        exc: BaseException | None,
    ) -> None:
        """Stamp one tool call onto the bound run account. Never raises.

        Arguments and results are deliberately NOT collected — they are
        unbounded (a substrate_read result can be the whole slice) and already
        live in ``action_pack_invocations``. What the receipt needs is that the
        call happened, through which pack, and how it ended.
        """
        try:
            fields: dict[str, Any] = {
                "source": "agency",
                "pack": pack.identity.id,
                "name": call.tool_name,
                "duration_ms": int((time.monotonic() - started_monotonic) * 1000),
            }
            if exc is not None:
                fields["status"] = "error"
                fields["error"] = type(exc).__name__
            elif outcome is not None:
                fields["admitted"] = outcome.admitted
                if not outcome.admitted:
                    fields["status"] = "blocked"
                    fields["block_cause"] = outcome.block_cause
                else:
                    result = outcome.tool_result
                    fields["status"] = result.status if result else "completed"
                    if result is not None and result.cost_usd:
                        fields["cost_usd"] = result.cost_usd
                    if result is not None and result.units:
                        fields["units"] = result.units
            record_tool_call(**fields)
        except Exception:  # pragma: no cover — instrumentation must never bite
            logger.debug("agency.account_tool_call failed", exc_info=True)

    async def _run_pack_tool(
        self,
        conn: asyncpg.Connection,
        *,
        pack: ActionPack,
        call: ToolCall,
        analyst_grants: list[Any] | None,
        target_allows: list[Any] | None,
        scope: TargetScopeView,
        ctx: ToolContext,
        estimated_cost_usd: float = 0.0,
        estimated_units: int = 1,
    ) -> AgencyOutcome:
        """The hard-gate pipeline itself (see :meth:`run_pack_tool`)."""
        pack_id = pack.identity.id
        tool_name = call.tool_name

        # --- 1) RESOLVE (three-way allow-list) ---------------------------
        res = resolve_pack(
            pack=pack,
            analyst_grants=analyst_grants,
            target_allows=target_allows,
            scope=scope,
        )
        if not res.effective:
            cause = (
                "not_granted" if not res.granted
                else "not_allowed" if not res.allowed
                else "not_applicable"
            )
            await self._block(
                conn, call, cause=cause, detail=res.reason,
            )
            return AgencyOutcome(
                admitted=False, pack_id=pack_id, tool_name=tool_name,
                block_cause=cause, detail=res.reason, resolution=res,
            )

        # --- 2) TOOL KNOWN (named on the pack + has a handler) -----------
        pack_tool_names = {t.name for t in pack.tools}
        if tool_name not in pack_tool_names:
            detail = f"tool {tool_name!r} not granted by pack {pack_id!r}"
            await self._block(conn, call, cause="unknown_tool", detail=detail)
            return AgencyOutcome(
                admitted=False, pack_id=pack_id, tool_name=tool_name,
                block_cause="unknown_tool", detail=detail, resolution=res,
            )
        handler = self._tools.handler_for(tool_name)
        if handler is None:
            detail = f"no registered handler for tool {tool_name!r}"
            await self._block(conn, call, cause="unknown_tool", detail=detail)
            return AgencyOutcome(
                admitted=False, pack_id=pack_id, tool_name=tool_name,
                block_cause="unknown_tool", detail=detail, resolution=res,
            )

        # --- 3) GOVERN (per-pack caps + global envelope) -----------------
        account = (
            (res.governor.budget_account if res.governor else None)
            or call.budget_account
        )
        enforcer = PackGovernorEnforcer(
            pack_id=pack_id,
            pack_version=pack.identity.version,
            governor=res.governor,
            budget_account=account,
            tenant_id=call.tenant_id,
        )
        decision = await enforcer.precall_check(
            conn,
            estimated_cost_usd=estimated_cost_usd,
            estimated_units=estimated_units,
        )
        if not decision.admitted:
            await self._block(
                conn, call, cause=decision.cause, detail=decision.detail,
                cap_dimension=decision.cap_dimension,
                cap_limit=decision.cap_limit, observed=decision.observed,
                budget_account=account,
            )
            return AgencyOutcome(
                admitted=False, pack_id=pack_id, tool_name=tool_name,
                block_cause=decision.cause, detail=decision.detail,
                resolution=res,
            )

        # --- 4) RECORD admit (ledger row + ALLOW event) ------------------
        inv_id = await enforcer.record_invocation(
            conn,
            tool_name=tool_name,
            requested_by=call.requested_by,
            cost_usd=estimated_cost_usd,
            units=estimated_units,
            outcome="admitted",
        )
        await record_governor_event(
            conn,
            GovernorEvent(
                pack_id=pack_id, tool_name=tool_name, decision="allow",
                cause="ok", budget_account=account,
                requested_by=call.requested_by, tenant_id=call.tenant_id,
                detail="resolved + under governor caps",
            ),
            nats_publish=self._nats_publish,
        )

        # --- 5) DISPATCH the tool handler --------------------------------
        # Bound the dispatch with a wall-clock timeout STRICTLY SHORTER than the
        # actor-invoke budget: a wedged handler (a hung sink, a stuck external
        # API, a runaway query) is caught HERE — the ledger row settles failed
        # and the actor is reclaimed — instead of pinning the actor for the full
        # invoke window and leaving the row stuck `admitted`. Env-overridable.
        timeout_s = pack_tool_timeout_seconds()
        try:
            result = await asyncio.wait_for(
                handler(call, pack, ctx), timeout=timeout_s,
            )
        except asyncio.TimeoutError:  # wedged handler — settle failed, free the actor
            logger.warning(
                "tool.timeout pack=%s tool=%s after=%ds", pack_id, tool_name, timeout_s,
            )
            await PackGovernorEnforcer.settle(conn, inv_id, outcome="failed")
            return AgencyOutcome(
                admitted=True, pack_id=pack_id, tool_name=tool_name,
                detail=f"handler exceeded {timeout_s}s timeout", resolution=res,
                tool_result=ToolResult(
                    status="failed", error=f"handler timeout after {timeout_s}s",
                ),
            )
        except Exception as exc:  # a handler crash settles the row failed
            logger.exception("tool.handler_error pack=%s tool=%s", pack_id, tool_name)
            await PackGovernorEnforcer.settle(conn, inv_id, outcome="failed")
            return AgencyOutcome(
                admitted=True, pack_id=pack_id, tool_name=tool_name,
                detail=f"handler raised: {exc}", resolution=res,
                tool_result=ToolResult(status="failed", error=str(exc)),
            )

        # --- 6) SETTLE the invocation row with the true outcome ----------
        # The admit-time COMMITTED cost/units (the estimate) is the budget
        # commitment; a tool that doesn't report its own cost (cost_usd == 0)
        # must NOT erase that commitment — otherwise a stream of zero-reported
        # calls would silently bypass the day-cost cap. Only overwrite when the
        # tool reports a meaningful (non-zero) value.
        settled = (
            "completed"
            if result.status in ("enqueued", "emitted", "noop", "completed")
            else "failed"
        )
        await PackGovernorEnforcer.settle(
            conn, inv_id, outcome=settled,
            cost_usd=result.cost_usd if result.cost_usd else None,
            units=result.units if result.units else None,
        )
        return AgencyOutcome(
            admitted=True, pack_id=pack_id, tool_name=tool_name,
            resolution=res, tool_result=result,
        )

    # ------------------------------------------------------------------

    async def _block(
        self,
        conn: asyncpg.Connection,
        call: ToolCall,
        *,
        cause: str,
        detail: str,
        cap_dimension: str | None = None,
        cap_limit: float | None = None,
        observed: float | None = None,
        budget_account: str | None = None,
    ) -> None:
        await record_governor_event(
            conn,
            GovernorEvent(
                pack_id=call.pack_id, tool_name=call.tool_name,
                decision="block", cause=cause,
                budget_account=budget_account or call.budget_account,
                requested_by=call.requested_by, tenant_id=call.tenant_id,
                cap_dimension=cap_dimension, cap_limit=cap_limit,
                observed_value=observed, detail=detail,
            ),
            nats_publish=self._nats_publish,
        )


__all__ = ["Agency", "AgencyOutcome"]
