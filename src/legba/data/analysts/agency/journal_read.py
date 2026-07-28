# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The ``journal_read`` pack — the journal_assessor's GOVERNED read surface.

Wave 1 (planning/JOURNAL_ASSESSOR_PLAN.md §5 / §12) gives the journal the WHOLE
ANIMAL + its OWN instruments — the read surface that makes it the self-narrating
analyst. Three slices:

  1. The platform's OWN finished intelligence + ground truth (reused substrate_read
     handlers, the pack is the grant boundary, not a second copy):
       - ``list_findings``   — recent findings + meta-findings (the
         cross_analyst_correlator contradiction/agreement/blind-spot outputs are
         the richest fuel);
       - ``query_facts``     — current temporal facts (grounding);
       - ``query_nexuses``   — open signed/typed relationships;
       - ``list_situations`` — ongoing first-class frames;
       - ``get_timeline``    — time-ordered facts ∪ signals ∪ situations.

  2. The journal's OWN INSTRUMENTS (§5 — the bulk of the build, net-new readers on
     the SubstrateQueryPort): recent ``get_assessments`` (country/world_assessor),
     ``get_graph_structure`` (graph_mining communities/centrality),
     ``get_structural_balance`` (unstable triads), ``get_critic_scores``,
     ``get_calibration`` (incl. the segregated ``brier_forecast_acute``),
     ``get_run_health`` (what fired vs went quiet), ``get_source_health``
     (source_poll_outcomes), ``get_budget_status`` (governor/budget pressure), and
     ``get_journal_delta`` (what changed since the last entry + the journal's own
     prior entry / current consolidation — its surviving attentional-continuity
     thread, §7.5).

WHY a SEPARATE pack (not just granting substrate_read): the §7.6 grant invariant.
The journal must be granted ONLY this read pack (and later the journal_proposals
pack) so it is structurally NOT effective for any pack whose tools call
write_fact / write_nexus / write_hypothesis — the grant-layer backstop for the
never-write-a-fact invariant (§3.1).

FOUR-SURFACE convergence (memory: consult-tools-must-be-pack-tools). The drift
guard is REAL — a tool not in the live pack blocks as ``unknown_tool`` even on
the governed path. The four surfaces must agree:

  1. the in-code TUPLE (``JOURNAL_READ_TOOLS``) — below;
  2. the DESCRIPTOR (``descriptors/action_pack_journal_read.yaml``);
  3. the HANDLERS (``register_journal_read_tools``) — the reused substrate_read
     handlers for the shared reads + the net-new ``*_tool`` handlers below;
  4. every tool the journal's run_method can dispatch ∈ this pack.

A tool name maps to one global handler; the pack is the GRANT/governance
boundary, not a second copy of the handler. The instrument handlers dispatch to
the SAME :class:`SubstrateQueryPort` the substrate_read handlers use — the port
is read-only, so no write surface is ever reachable from this pack.

NOTE on consult (``consult_on_demand._KNOWN_TOOLS``): the journal does NOT run on
the consult path — it runs in-actor through the agency ``run_pack_tool`` GATHER
loop. The instrument tools are journal-specific self-introspection that the
operator-facing consult surface does NOT need, so they are deliberately NOT added
to consult's ``_KNOWN_TOOLS`` (consult reaches the substrate, not the journal's
own dormancy/budget). The five SHARED reads already in ``_KNOWN_TOOLS`` (via
substrate_read) keep working on consult unchanged. The journal's GATHER loop
recognises every JOURNAL_READ_TOOLS entry via ``inline_target._gather``'s
``extra_read_tools`` channel.
"""

from __future__ import annotations

import logging

from ...schemas.action_pack import ActionPack
from .substrate_read import (
    get_timeline_tool,
    list_findings_tool,
    list_situations_tool,
    query_facts_tool,
    query_nexuses_tool,
)
from .tools import ToolCall, ToolContext, ToolRegistry, ToolResult

logger = logging.getLogger(__name__)

JOURNAL_READ_PACK_ID = "journal_read"

# Wave 1: the whole animal + its own instruments. Keep this tuple ==
# the descriptor's `tools` names == the handlers registered below (the per-pack
# drift guard asserts it).
JOURNAL_READ_TOOLS = (
    # --- the platform's own finished intelligence + ground truth (reused) ---
    "list_findings",
    "query_facts",
    "query_nexuses",
    "list_situations",
    "get_timeline",
    # --- the journal's OWN instruments (net-new self-introspection) ---
    "get_assessments",
    "get_graph_structure",
    "get_structural_balance",
    "get_critic_scores",
    "get_calibration",
    "get_run_health",
    "get_source_health",
    "get_budget_status",
    "get_journal_delta",
    # VOICES LV-1 (planning/VOICES_BUILD_DESIGN §3.2): the chorus DIFF pass reads
    # this cycle's four faculty lens reads through this tool. It is granted to the
    # whole journal_read pack (one pack, one grant) but is inert for the tiers
    # that never call it (entry/consolidation/chronicle/faculty).
    "get_lens_reads",
)


# ---------------------------------------------------------------------------
# Net-new instrument handlers. Each dispatches to the read-only
# SubstrateQueryPort wired on ToolContext.substrate (per-binding by the runtime),
# mirroring the substrate_read pack's _call_port shape. A port-level failure
# returns a `failed` ToolResult the GATHER loop folds back so the agent recovers.
# ---------------------------------------------------------------------------


async def _call_instrument(call: ToolCall, ctx: ToolContext, name: str) -> ToolResult:
    port = ctx.substrate
    if port is None:
        return ToolResult(
            status="failed",
            error=f"no SubstrateQueryPort wired for {name} (ToolContext.substrate is None)",
        )
    args = call.args
    try:
        if name == "get_assessments":
            out = await port.get_assessments(
                analyst_id=args.get("analyst_id"),
                target_id=args.get("target_id"),
                since_hours=(
                    int(args["since_hours"])
                    if args.get("since_hours") is not None
                    else 48
                ),
                limit=int(args.get("limit", 20)),
            )
        elif name == "get_graph_structure":
            out = await port.get_graph_structure(limit=int(args.get("limit", 20)))
        elif name == "get_structural_balance":
            out = await port.get_structural_balance(limit=int(args.get("limit", 20)))
        elif name == "get_critic_scores":
            out = await port.get_critic_scores(
                analyst_id=args.get("analyst_id"),
                since_hours=(
                    int(args["since_hours"])
                    if args.get("since_hours") is not None
                    else 168
                ),
                limit=int(args.get("limit", 20)),
            )
        elif name == "get_calibration":
            out = await port.get_calibration()
        elif name == "get_run_health":
            # W2-T6 head coverage: default to the whole fleet (the port clamps
            # at _MAX_ROW_LIMIT=200) — a 40-row dispatcher default silently
            # re-clipped the roster even after the port default was widened.
            out = await port.get_run_health(
                analyst_id=args.get("analyst_id"),
                quiet_hours=int(args.get("quiet_hours", 24)),
                limit=int(args.get("limit", 200)),
            )
        elif name == "get_source_health":
            out = await port.get_source_health(
                silent_only=bool(args.get("silent_only", False)),
                silent_hours=int(args.get("silent_hours", 48)),
                limit=int(args.get("limit", 200)),
            )
        elif name == "get_budget_status":
            out = await port.get_budget_status(
                analyst_id=args.get("analyst_id"),
                demotion_lookback_hours=int(args.get("demotion_lookback_hours", 168)),
                limit=int(args.get("limit", 40)),
            )
        elif name == "get_journal_delta":
            out = await port.get_journal_delta(
                since=args.get("since"),
                limit=int(args.get("limit", 30)),
            )
        elif name == "get_lens_reads":
            # VOICES LV-1 (§3.2): the faculty id set is passed at CALL TIME (never
            # imported into the runtime port) — the kind-module constant lives in
            # the analyst layer, this pack is the analyst layer, so importing it
            # here does NOT cross the port boundary the design guards.
            from legba.data.analysts.journal_assessor import LENS_ANALYST_IDS

            out = await port.get_lens_reads(
                lens_analyst_ids=list(LENS_ANALYST_IDS),
                since=args.get("since"),
                limit=int(args.get("limit", 20)),
            )
        else:  # pragma: no cover — registry only maps the known names
            return ToolResult(status="failed", error=f"unknown journal tool {name!r}")
    except Exception as exc:  # noqa: BLE001 — port failures fold into the loop
        logger.warning("journal_read.tool.error tool=%s err=%s", name, exc)
        return ToolResult(status="failed", error=f"tool_failed: {exc!s}")
    return ToolResult(status="completed", output=dict(out))


async def get_assessments_tool(
    call: ToolCall, pack: ActionPack, ctx: ToolContext
) -> ToolResult:
    return await _call_instrument(call, ctx, "get_assessments")


async def get_graph_structure_tool(
    call: ToolCall, pack: ActionPack, ctx: ToolContext
) -> ToolResult:
    return await _call_instrument(call, ctx, "get_graph_structure")


async def get_structural_balance_tool(
    call: ToolCall, pack: ActionPack, ctx: ToolContext
) -> ToolResult:
    return await _call_instrument(call, ctx, "get_structural_balance")


async def get_critic_scores_tool(
    call: ToolCall, pack: ActionPack, ctx: ToolContext
) -> ToolResult:
    return await _call_instrument(call, ctx, "get_critic_scores")


async def get_calibration_tool(
    call: ToolCall, pack: ActionPack, ctx: ToolContext
) -> ToolResult:
    return await _call_instrument(call, ctx, "get_calibration")


async def get_run_health_tool(
    call: ToolCall, pack: ActionPack, ctx: ToolContext
) -> ToolResult:
    return await _call_instrument(call, ctx, "get_run_health")


async def get_source_health_tool(
    call: ToolCall, pack: ActionPack, ctx: ToolContext
) -> ToolResult:
    return await _call_instrument(call, ctx, "get_source_health")


async def get_budget_status_tool(
    call: ToolCall, pack: ActionPack, ctx: ToolContext
) -> ToolResult:
    return await _call_instrument(call, ctx, "get_budget_status")


async def get_journal_delta_tool(
    call: ToolCall, pack: ActionPack, ctx: ToolContext
) -> ToolResult:
    return await _call_instrument(call, ctx, "get_journal_delta")


async def get_lens_reads_tool(
    call: ToolCall, pack: ActionPack, ctx: ToolContext
) -> ToolResult:
    return await _call_instrument(call, ctx, "get_lens_reads")


def register_journal_read_tools(registry: ToolRegistry) -> None:
    """Register the journal_read pack's tool handlers.

    Reused reads (``list_findings`` / ``query_facts`` / ``query_nexuses`` /
    ``list_situations`` / ``get_timeline``) re-register the substrate_read
    handlers (one tool name → one global handler; the pack is the grant boundary,
    not a handler copy — register is idempotent, a repeated name overwrites with
    the same callable). The instrument reads register the net-new handlers above.
    Called by ``default_tool_registry`` so the global registry carries every
    journal_read handler whether or not substrate_read is also registered.
    """
    # Reused finished-intelligence + ground-truth reads.
    registry.register("list_findings", list_findings_tool)
    registry.register("query_facts", query_facts_tool)
    registry.register("query_nexuses", query_nexuses_tool)
    registry.register("list_situations", list_situations_tool)
    registry.register("get_timeline", get_timeline_tool)
    # Net-new self-instrument reads.
    registry.register("get_assessments", get_assessments_tool)
    registry.register("get_graph_structure", get_graph_structure_tool)
    registry.register("get_structural_balance", get_structural_balance_tool)
    registry.register("get_critic_scores", get_critic_scores_tool)
    registry.register("get_calibration", get_calibration_tool)
    registry.register("get_run_health", get_run_health_tool)
    registry.register("get_source_health", get_source_health_tool)
    registry.register("get_budget_status", get_budget_status_tool)
    registry.register("get_journal_delta", get_journal_delta_tool)
    registry.register("get_lens_reads", get_lens_reads_tool)


__all__ = [
    "JOURNAL_READ_PACK_ID",
    "JOURNAL_READ_TOOLS",
    "register_journal_read_tools",
    "get_assessments_tool",
    "get_graph_structure_tool",
    "get_structural_balance_tool",
    "get_critic_scores_tool",
    "get_calibration_tool",
    "get_run_health_tool",
    "get_source_health_tool",
    "get_budget_status_tool",
    "get_journal_delta_tool",
    "get_lens_reads_tool",
]
