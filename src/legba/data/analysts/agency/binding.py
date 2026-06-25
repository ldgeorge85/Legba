# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Per-analyst agency binding — the production on-ramp into ``run_pack_tool``.

A-3 (review G2): the agency pipeline was mechanism-complete but had zero
production callers. This module is the missing connective tissue: a small
bundle that carries everything one analyst needs to invoke ONE pack's tools
through the full hard-gate pipeline (resolve ∩ allow ∩ applicability →
governor → dispatch → settle → audit), so call sites stay one-liners:

  * the consult kind routes every ReAct tool call through its
    ``substrate_read`` binding (:mod:`legba.data.analysts.consult_on_demand`);
  * the actor run path invokes the ``escalate_finding`` pack when a landed
    finding crosses the pack's severity gate
    (:mod:`legba.runtime.dapr_actors`).

The binding is deliberately dumb: it owns no policy. Denials, caps, and
auditing all live in :class:`Agency`; the binding just acquires a
connection, shapes the :class:`ToolCall`, and hands over.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from ...schemas.action_pack import ActionPack, ActionPackRef
from .agency import Agency, AgencyOutcome
from .resolution import TargetScopeView
from .tools import ToolCall, ToolContext

logger = logging.getLogger(__name__)

# Scope used when an analyst invokes a pack with NO target in context (the
# global consult surface). Packs meant to be invocable there must be
# universally applicable (no applies_to_tags / applicability_predicate).
GLOBAL_SCOPE = TargetScopeView(target_id="__global__")

_SEVERITY_ORDER = {"info": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}


@dataclass
class AgencyToolBinding:
    """Everything needed to run ONE pack's tools for ONE analyst context."""

    agency: Agency
    pack: ActionPack
    pg_pool: Any
    tool_context: ToolContext
    analyst_grants: list[Any] | None
    target_allows: list[Any] | None
    scope: TargetScopeView = field(default_factory=lambda: GLOBAL_SCOPE)
    requested_by: str = "system"
    budget_account: str = "system"
    tenant_id: str = "default"

    async def run_tool(
        self,
        tool_name: str,
        args: dict[str, Any],
        *,
        estimated_cost_usd: float = 0.0,
        estimated_units: int = 1,
    ) -> AgencyOutcome:
        """One governed tool invocation. Never raises for gate denials —
        the returned :class:`AgencyOutcome` carries admit/block + result."""
        call = ToolCall(
            pack_id=self.pack.identity.id,
            tool_name=tool_name,
            args=dict(args),
            requested_by=self.requested_by,
            budget_account=self.budget_account,
            tenant_id=self.tenant_id,
        )
        async with self.pg_pool.acquire() as conn:
            return await self.agency.run_pack_tool(
                conn,
                pack=self.pack,
                call=call,
                analyst_grants=self.analyst_grants,
                target_allows=self.target_allows,
                scope=self.scope,
                ctx=self.tool_context,
                estimated_cost_usd=estimated_cost_usd,
                estimated_units=estimated_units,
            )

    def for_target(
        self,
        *,
        scope: TargetScopeView,
        target_allows: list[Any] | None,
    ) -> "AgencyToolBinding":
        """A copy re-pointed at a concrete target's scope + allow-list.

        The escalate hook resolves the target leg PER RUN (the analyst's
        grant leg and the pack are constant; which target a finding belongs
        to is not).
        """
        return AgencyToolBinding(
            agency=self.agency,
            pack=self.pack,
            pg_pool=self.pg_pool,
            tool_context=self.tool_context,
            analyst_grants=self.analyst_grants,
            target_allows=target_allows,
            scope=scope,
            requested_by=self.requested_by,
            budget_account=self.budget_account,
            tenant_id=self.tenant_id,
        )


@dataclass
class EscalationBinding:
    """The ``escalate_finding`` pack bound to one analyst (A-3c).

    ``binding`` carries the analyst-constant legs (agency, pack, grants,
    tool context); the actor run path re-points the target leg per run via
    :meth:`AgencyToolBinding.for_target` — which target a finding belongs
    to is only known at write time. The gates come from the pack's
    ``escalate`` tool config (``severity_gate`` / ``confidence_gate``).
    """

    binding: AgencyToolBinding
    severity_gate: str = "high"
    confidence_gate: float = 0.85


async def fetch_action_pack(
    registry_client: Any, pack_id: str
) -> ActionPack | None:
    """Fetch + validate one head action-pack descriptor from the registry.

    Returns None on a registry miss; raises nothing else fatal — callers
    decide whether a missing pack is fail-loud (production analyst deps) or
    skip (optional bindings).
    """
    try:
        row = await registry_client.get_descriptor(pack_id, family="action_pack")
    except Exception as exc:
        logger.warning(
            "agency_binding.pack_fetch_failed pack_id=%s err=%s", pack_id, exc,
        )
        return None
    if row is None:
        return None
    body = row.get("body") or {}
    try:
        return ActionPack.model_validate(body, strict=False)
    except Exception as exc:
        logger.error(
            "agency_binding.pack_parse_failed pack_id=%s err=%s", pack_id, exc,
        )
        return None


def grants_include(grants: list[Any] | None, pack_id: str) -> bool:
    """True when an ``action_packs`` grant list names ``pack_id``."""
    for g in grants or []:
        gid = g.pack_id if isinstance(g, ActionPackRef) else (
            g.get("pack_id") if isinstance(g, dict) else getattr(g, "pack_id", None)
        )
        if gid == pack_id:
            return True
    return False


def escalation_gate_decision(
    *,
    severity: str | None,
    confidence: float | None,
    severity_gate: str = "high",
    confidence_gate: float = 0.85,
) -> bool:
    """Should a landed finding be escalated?

    Findings rarely carry an explicit severity (the column is alert-kind
    territory; LLM kinds may stamp one into ``payload.data['severity']``).
    The gate therefore fires on EITHER: an explicit severity at/above
    ``severity_gate``, OR confidence at/above ``confidence_gate``. Unknown
    severity strings never fire (conservative — no guessing).
    """
    if severity is not None:
        rank = _SEVERITY_ORDER.get(str(severity).lower())
        gate_rank = _SEVERITY_ORDER.get(str(severity_gate).lower(), 3)
        if rank is not None and rank >= gate_rank:
            return True
    if confidence is not None and confidence >= confidence_gate:
        return True
    return False


__all__ = [
    "GLOBAL_SCOPE",
    "AgencyToolBinding",
    "EscalationBinding",
    "escalation_gate_decision",
    "fetch_action_pack",
    "grants_include",
]
