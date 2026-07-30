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
from typing import Any, Mapping

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

# P7-F4 / P7 r2 — ABSENCE VERDICT LEADS. A confident 'nothing is happening' read
# must NOT page the escalations channel (the channel is for the signal, not for
# confident boredom). P7 r2 TIGHTENS the round-1 heuristic, which over-suppressed:
# a bare ``startswith('no ')`` gagged 'No off-ramp as Iran-Israel exchange widens'
# and 'No ceasefire despite strikes', and the broad 'routine' / 'steady state' /
# 'no new' / 'no change' SUBSTRINGS caught 'Routine patrol ambushed' and 'Non-
# routine mobilization near border' — all REAL escalations. The match is now
# anchored: only a title that, after an optional '<subject> – ' prefix, OPENS with a
# recognized absence VERDICT phrase reads as boredom. These mirror the verify floor's
# absence vocabulary (legba.data.provenance.verify._ABSENCE_MARKERS). A genuine
# indicator-flip escalation is unaffected: this suppression lives ONLY on the
# confidence×severity gate leg (_is_indicator_activation does NOT call this).
_ABSENCE_VERDICT_LEADS = (
    "no observable",
    "no discernible",
    "no significant",
    "no credible",
    "no material",
    "no confirmed",
    "no evidence",
    "no indication",
    "no reports",
    "no report of",
    "no notable",
    "no observed",
    "no escalation",
    "steady-state",
    "steady state",
    "status quo",
    "nothing to suggest",
    "nothing indicating",
)

# Grammatical false-positives for the bare 'No <qualifier>' lead — an idiom or a
# named escalation, NOT an absence verdict. Consulted by _is_bare_negative_lead.
_NEGATIVE_LEAD_FALSE_POSITIVES = (
    "no fewer",
    "no less",
    "no-fly",
    "no fly",
    "no longer",
    "no doubt",
    "no single",
    "no one",
    "no off-ramp",
    "no ceasefire",
)

# Coarse severity ORDER (low→high) for the bare-negative severity gate: a
# moderate-or-higher finding is NEVER suppressed by the bare 'No …' title heuristic.
_SEVERITY_RANK = {
    "info": 0,
    "low": 1,
    "moderate": 2,
    "medium": 2,
    "elevated": 3,
    "high": 4,
    "critical": 5,
}
_MODERATE_RANK = 2

# Separators an assessor puts between a SUBJECT and its verdict ('Argentina – Low
# leadership transition risk', 'United States – No observable WMD proliferation …').
_SUBJECT_SEPARATORS = (" – ", " — ", " -- ", " - ", ": ")


def _strip_subject_prefix(low: str) -> str:
    """Drop a short leading '<subject> <sep> ' prefix so a verdict phrase that
    follows a country/subject label can be matched at the START of the title."""
    for sep in _SUBJECT_SEPARATORS:
        idx = low.find(sep)
        if 0 < idx <= 40:
            return low[idx + len(sep):].strip()
    return low


def is_absence_or_negative_title(title: str | None) -> bool:
    """True when a finding TITLE is a WHOLE-title absence / low-risk / steady-state
    VERDICT (P7-F4, tightened P7 r2).

    Anchored on the leading verdict phrase — after an optional '<subject> – ' prefix
    — NOT a bare ``startswith('no ')`` and NOT a mid-title substring, so a real
    escalation title ('No off-ramp as … widens', 'Routine patrol ambushed', 'Non-
    routine mobilization near border') never trips it. This is a PURE title
    classifier (no severity input); its USE for alert suppression in
    :func:`escalation_gate_decision` is gated on SUB-MODERATE severity (FU2), so a
    moderate+/high negation-framed event ('No confirmed casualties as fighting
    intensifies') is never title-gagged — only low/info absence reads are. The
    WEAKER bare 'No <qualifier>' catch-all is gated the same way at that call site.
    """
    if not title:
        return False
    low = _strip_subject_prefix(str(title).strip().lower())
    if not low:
        return False
    if any(low.startswith(lead) for lead in _ABSENCE_VERDICT_LEADS):
        return True
    # A 'Low … risk / likelihood / near-term …' verdict lead.
    if low.startswith("low ") and any(
        w in low for w in ("risk", "likelihood", "probability", "near-term", "prospect")
    ):
        return True
    return False


def _is_bare_negative_lead(title: str | None) -> bool:
    """A weaker 'No <qualifier> …' title lead that is NOT a recognized absence
    verdict. Consulted ONLY by :func:`escalation_gate_decision`, and only for a
    sub-moderate finding (P7 r2) — a moderate+/high finding is never gagged by it."""
    if not title:
        return False
    low = _strip_subject_prefix(str(title).strip().lower())
    if not low:
        return False
    return low.startswith("no ") and not low.startswith(_NEGATIVE_LEAD_FALSE_POSITIVES)


def _severity_rank(severity: str | None) -> int:
    """Coarse rank for the bare-negative severity gate. An unknown / absent severity
    is treated as MODERATE — the safe direction (do NOT gag it)."""
    if severity is None:
        return _MODERATE_RANK
    return _SEVERITY_RANK.get(str(severity).lower(), _MODERATE_RANK)


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


#: The action the escalation edge has always invoked. Stays the default so a
#: pack descriptor that says nothing behaves byte-identically to the literal
#: this used to be.
DEFAULT_ESCALATION_ACTION = "escalate"

#: Pack-tool config key naming which tool the escalation edge invokes.
ESCALATION_ACTION_CONFIG_KEY = "action_tool"


@dataclass
class EscalationBinding:
    """The ``escalate_finding`` pack bound to one analyst (A-3c).

    ``binding`` carries the analyst-constant legs (agency, pack, grants,
    tool context); the actor run path re-points the target leg per run via
    :meth:`AgencyToolBinding.for_target` — which target a finding belongs
    to is only known at write time. The gates come from the pack's
    ``escalate`` tool config (``severity_gate`` / ``confidence_gate``).

    ``action_tool`` is WHICH tool the crossing invokes. It was a hardcoded
    ``"escalate"`` string literal at the fire site, which made the escalation
    edge — the one threshold→action rule that actually runs in production —
    fixed to a single action even though the config dict that could name
    another was already free-form and DB-read. It now comes from the same
    ``escalate`` tool config the gates do (``action_tool``), validated against
    the bound pack's tool list at read time. Default preserves today exactly.

    ``action_degraded`` is set when a configured action was REFUSED (it named
    no tool on the pack): the binding falls back to
    :data:`DEFAULT_ESCALATION_ACTION` and this note rides every emit into the
    durable ``alert_sink_deliveries.payload_summary``, so a typo'd action is
    visible on the delivery ledger, not just in a boot log line.
    """

    binding: AgencyToolBinding
    severity_gate: str = "high"
    confidence_gate: float = 0.85
    action_tool: str = DEFAULT_ESCALATION_ACTION
    action_degraded: str | None = None


def resolve_escalation_action(
    tool_config: Mapping[str, Any] | None,
    pack: ActionPack | None,
    *,
    default: str = DEFAULT_ESCALATION_ACTION,
    log_context: str = "",
) -> tuple[str, str | None]:
    """Resolve the escalation edge's action tool from the pack tool config.

    Returns ``(action_tool, degrade_note)``. ``degrade_note`` is ``None`` on
    the clean paths (no config, or a configured action that names a real tool
    on the pack).

    LOUD DEGRADE, not refuse: an action naming no tool on the pack falls back
    to ``default`` with an ERROR log and a note that follows the emit onto the
    delivery ledger. Refusing would take the escalation edge offline for a
    misspelling — the operator would stop being paged about real findings
    because of a config typo, which is strictly worse than paging them through
    the default channel while shouting about the typo.

    Validation is against the LIVE pack's tool list (the registry DB row), so
    an operator who adds ``create_incident`` to the ``escalate_finding`` pack
    via ``PUT /descriptors/…`` can select it in the same edit — no code change,
    no rebuild. A tool named on the pack but lacking a registered handler is
    still caught downstream by the agency's ``unknown_tool`` sentinel.
    """
    raw = (tool_config or {}).get(ESCALATION_ACTION_CONFIG_KEY)
    if raw is None:
        return default, None

    configured = str(raw).strip()
    pack_tools = [str(t.name) for t in (getattr(pack, "tools", None) or [])]
    if not configured:
        note = (
            f"{ESCALATION_ACTION_CONFIG_KEY} was empty; using {default!r}"
        )
    elif configured in pack_tools:
        return configured, None
    else:
        note = (
            f"{ESCALATION_ACTION_CONFIG_KEY}={configured!r} names no tool on "
            f"pack {getattr(getattr(pack, 'identity', None), 'id', '?')} "
            f"(has {sorted(pack_tools)}); using {default!r}"
        )
    logger.error(
        "agency_binding.escalation_action.degraded analyst=%s %s",
        log_context or "?", note,
    )
    return default, note


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
    # P7-F4 / FU2 — a title-heuristic absence / 'nothing is happening' verdict gags
    # the confidence×severity alert leg ONLY for a SUB-MODERATE finding. Round-1
    # suppressed a strong-absence VERDICT LEAD ('No confirmed …') at ANY severity,
    # which title-gagged a high-severity NEGATION-FRAMED event — 'No confirmed
    # casualties as fighting intensifies' is the report of an INTENSIFYING battle,
    # not boredom, and must page. A moderate+/high finding describes an ongoing
    # situation, so a title heuristic never suppresses it; only genuinely low/info
    # absence reads ('Argentina – Low leadership transition risk', 'United States –
    # No observable WMD…') are gagged here. The WEAKER bare 'No <qualifier> …' lead
    # ('No major developments …') is gated the SAME way. A genuine indicator flip
    # still escalates via _is_indicator_activation, which does NOT call this.
    if _severity_rank(severity) < _MODERATE_RANK and (
        is_absence_or_negative_title(title) or _is_bare_negative_lead(title)
    ):
        return False
    weight = (
        _SEVERITY_WEIGHT.get(str(severity).lower(), _SEVERITY_WEIGHT_BASELINE)
        if severity is not None
        else _SEVERITY_WEIGHT_BASELINE
    )
    return (confidence * weight) >= confidence_gate


__all__ = [
    "DEFAULT_ESCALATION_ACTION",
    "ESCALATION_ACTION_CONFIG_KEY",
    "GLOBAL_SCOPE",
    "AgencyToolBinding",
    "EscalationBinding",
    "escalation_gate_decision",
    "fetch_action_pack",
    "grants_include",
    "is_absence_or_negative_title",
    "resolve_escalation_action",
]
