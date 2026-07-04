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

# S3-T4 — per-severity multiplier applied to the verify-FOLDED effective
# confidence to form the single alert score (``effective_confidence ×
# severity``). Ordered low→high; both live vocabularies are covered — the unit
# tags (low/moderate/elevated/high/critical) and the AlertPayload ladder
# (info/low/medium/high/critical). Only ``high``/``critical`` (the escalate
# pack's ``severity_gate`` and above) get a >1 boost, so a SEVERE finding can
# alert on less post-verify confidence; everything below ``high`` stays at the
# baseline 1.0 so it behaves exactly like the prior plain confidence gate (no
# regression for the many kinds that never stamp severity). Crucially, NO weight
# lets a verify-DEMOTED finding (low effective confidence) cross the threshold —
# that is the raw-confidence gate S3-T4 closes (a high-severity TAG alone can no
# longer fire an alert on a floored finding).
# P7-F4 (2026-07-04): DOWN-WEIGHT the low-signal severities so a confident-but-
# BORING finding cannot page the escalations channel. The pre-fix curve left
# info/low/moderate at 1.0, so a 0.88-confidence 'Low leadership transition risk'
# finding cleared the 0.85 gate and paged an alert that said nothing was
# happening — the channel selected for confident boredom. info/low now carry a
# <1.0 weight (0.3 / 0.4) so a low-severity finding needs implausibly high
# post-verify confidence to fire (0.88 × 0.4 = 0.35, well under 0.85); moderate+
# stays at the baseline (a moderate development is a legitimate page) and
# high/critical keep their >1 boost so a SEVERE verified finding alerts on less
# confidence. Absence/negative findings are suppressed outright (see
# escalation_gate_decision) regardless of weight.
_SEVERITY_WEIGHT = {
    "info": 0.3,
    "low": 0.4,
    "moderate": 1.0,
    "medium": 1.0,
    "elevated": 1.0,
    "high": 1.2,
    "critical": 1.5,
}
# Baseline multiplier for a finding with NO (or an unknown) severity: the alert
# score reduces to the effective confidence itself, so the gate is the plain
# effective-confidence threshold — unchanged for severity-less findings.
_SEVERITY_WEIGHT_BASELINE = 1.0

# P7-F4 — ABSENCE / negative-finding title markers. A confident 'nothing is
# happening' read must NOT page the escalations channel (the channel is for the
# signal, not for confident boredom). These mirror the verify floor's absence
# vocabulary (legba.data.provenance.verify._ABSENCE_MARKERS) so the alert gate
# classifies the same low-risk reads the verify pass already recognizes. The
# check is on the finding TITLE (the assessors lead with the verdict there — 'No
# material escalation', 'Argentina – Low leadership transition risk'). A genuine
# indicator-flip escalation is unaffected: this suppression lives ONLY on the
# confidence×severity gate leg, so a pre-registered warning signpost firing
# (_is_indicator_activation) still escalates regardless of an absence title.
_ABSENCE_TITLE_MARKERS = (
    "no material",
    "no significant",
    "no credible",
    "no confirmed",
    "no evidence",
    "no indication",
    "no reports",
    "no report of",
    "no new",
    "no notable",
    "no observed",
    "no discernible",
    "low risk",
    "low near-term",
    "low leadership transition risk",
    "nothing to suggest",
    "nothing indicating",
    "steady state",
    "steady-state",
    "no change",
    "no escalation",
    "routine",
)


def is_absence_or_negative_title(title: str | None) -> bool:
    """True when a finding TITLE reads as an absence / low-risk 'nothing is
    happening' verdict (P7-F4). Conservative: keyed on the leading verdict phrase
    or an explicit absence marker, so a real event title never trips it."""
    if not title:
        return False
    low = str(title).strip().lower()
    if not low:
        return False
    if low.startswith("no ") and not low.startswith(
        ("no fewer", "no less", "no-fly", "no fly")
    ):
        return True
    return any(marker in low for marker in _ABSENCE_TITLE_MARKERS)


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
    title: str | None = None,
) -> bool:
    """Should a landed finding be escalated? (S3-T4 combined gate.)

    ``confidence`` is the verify-FOLDED EFFECTIVE confidence — the caller
    (:func:`_maybe_escalate_finding`) demotes the raw LLM-asserted number to
    ``min(raw, faithfulness_score[, confidence_ceiling])`` BEFORE calling this,
    per S8-T2. The gate keys on a SINGLE alert score,
    ``effective_confidence × severity_weight``, crossing ``confidence_gate``:

      * a high-severity VERIFIED finding (high effective confidence) alerts —
        ``high``/``critical`` carry a >1 weight, so a severe finding needs less
        post-verify confidence to cross;
      * a verify-DEMOTED finding (low effective confidence) does NOT alert, no
        matter how severe — closing the raw-confidence gate the review flagged
        (previously an explicit ``high``/``critical`` severity fired regardless
        of confidence, so a floored high-severity finding still paged);
      * a severity-less (or unknown-severity) finding reduces to the plain
        effective-confidence gate (baseline weight 1.0) — nothing regresses for
        the kinds that never stamp a severity.

    ``severity_gate`` is accepted for call-site/back-compat; the per-severity
    weight curve now encodes which severities get a boost (``high`` and above).
    """
    if confidence is None:
        return False
    # P7-F4: an absence / 'nothing is happening' verdict never pages the
    # escalations channel on the confidence×severity leg (a genuine indicator
    # flip escalates via _is_indicator_activation, which does NOT call this).
    if is_absence_or_negative_title(title):
        return False
    weight = (
        _SEVERITY_WEIGHT.get(str(severity).lower(), _SEVERITY_WEIGHT_BASELINE)
        if severity is not None
        else _SEVERITY_WEIGHT_BASELINE
    )
    return (confidence * weight) >= confidence_gate


__all__ = [
    "GLOBAL_SCOPE",
    "AgencyToolBinding",
    "EscalationBinding",
    "escalation_gate_decision",
    "fetch_action_pack",
    "grants_include",
    "is_absence_or_negative_title",
]
