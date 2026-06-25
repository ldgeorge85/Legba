# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The ``journal_propose`` pack — the journal's PROPOSE-AND-GATE write surface
(planning/JOURNAL_ASSESSOR_PLAN.md §7 / §12 Wave 4).

THE GOVERNING PRINCIPLE (§7.1). The journal writes EXACTLY TWO things directly:
entries + consolidations (the ``journal`` kind, the off-chain table). EVERYTHING
else it wants to affect, it PROPOSES — into ONE human-gated queue
(``journal_proposals``), NEVER to a live table. There is no third direct-write
tool. A human always sits between the journal's voice and any change to the
system: the journal can SUGGEST, never CAUSE (§7.5). This structurally kills the
self-confirmation loop (the adversarial review's self-confirmed-forecast class).

WHY A SEPARATE PACK (not widening ``journal_read``): the §7.6 grant invariant.
The journal must be granted ONLY read packs + this proposals pack — a pack whose
tools write EXCLUSIVELY to ``journal_proposals`` and call NO ``write_fact`` /
``write_nexus`` / ``write_hypothesis``. That keeps the analyst structurally NOT
effective for any pack that mutates the knowledge layer. Widening ``journal_read``
to carry a write tool would couple the read grant to a write surface and break
the invariant.

WHY NOT REUSE ``propose_facts``: that pack does the OPPOSITE of what is needed
here (§7.2). ``propose_fact_tool`` calls ``write_fact(source_type='proposed')``
and INSERTs a LIVE ``facts`` row with NO human in the loop; ``request_source`` /
``open_question`` write LIVE ``hypotheses`` rows the same way. There is NO
review-queue indirection anywhere in that tree to reuse. So this pack is
NET-NEW: a bounded toolset that writes ONLY a ``journal_proposals`` row.

THE BOUNDED TOOLSET (each writes ONLY a ``journal_proposals`` row):

  * ``propose_correction``    — a stale fact to supersede / an entity merge / a
                                situation correction. On accept the operator's
                                worker applies it via the EXISTING write/lifecycle
                                path (``supersede_prior_facts`` / entity
                                resolution / situation lifecycle).
  * ``propose_change``        — a descriptor / config diff ("country_critic cadence
                                looks halved"). On accept → ``PUT /stack/{id}`` /
                                ``PUT /descriptors/{family}/{id}``.
  * ``propose_self_revision`` — a diff to the journal's OWN instruction prompt.
                                THE HIGHEST-SCRUTINY CLASS (§7.5): a self-
                                modification loop on the most PERSUASIVE surface
                                in the system. Ships LAST within Wave 4, after the
                                other two prove the queue. On accept → the
                                optimizer's champion-promotion path
                                (``resolve_promoted_system_prompt``).

THE GATING PRECONDITION (the safety claim, asserted by test — not a comment): a
``propose_*`` call writes ONLY a ``journal_proposals`` row and NEVER a live
``facts`` / ``hypotheses`` / ``nexuses`` row or a descriptor mutation. Every
handler here reaches the SAME per-run ``WritebackContext`` the propose_facts
tools use (``ctx.writeback``: the run pg_pool + AnalystContext + publish_fn) but
runs a single parameterised INSERT into ``journal_proposals`` — it touches NO
provenance writer and NO live substrate table. The "suggested, never caused"
contract is structural.

FOUR-SURFACE convergence (memory: consult-tools-must-be-pack-tools). The drift
guard is REAL — a tool not in the live pack blocks as ``unknown_tool`` even on
the governed path. The four surfaces must agree:

  1. the in-code TUPLE (``JOURNAL_PROPOSE_TOOLS``) — below;
  2. the DESCRIPTOR (``descriptors/action_pack_journal_propose.yaml``);
  3. the HANDLERS (``register_journal_propose_tools``) — below;
  4. every tool the journal's run_method can dispatch ∈ this pack.

These propose tools are journal-specific gated-agency tools the operator-facing
consult surface does NOT need, so they are deliberately NOT added to consult's
``_KNOWN_TOOLS`` (the journal does not run on consult). The journal's GATHER loop
recognises them via ``inline_target._gather``'s ``extra_write_tools`` channel and
routes each through THIS pack's per-run binding (carrying the writeback).
"""

from __future__ import annotations

import json
import logging
from typing import Any
from uuid import UUID

from ...schemas.action_pack import ActionPack
from .tools import ToolCall, ToolContext, ToolResult, WritebackContext

logger = logging.getLogger(__name__)

JOURNAL_PROPOSE_PACK_ID = "journal_propose"

# Wave 4: the bounded propose toolset. Keep this tuple == the descriptor's
# `tools` names == the handlers registered below (the per-pack drift guard
# asserts it). ORDER mirrors the §7.5 build order: correction + change prove the
# queue + accept/reject path FIRST; self_revision (the highest-scrutiny class)
# ships LAST and is listed last.
JOURNAL_PROPOSE_TOOLS = (
    "propose_correction",
    "propose_change",
    "propose_self_revision",
)

# The three proposal kinds the queue stores (journal_proposals.proposal_kind).
# A 1:1 map tool -> proposal_kind so the apply-on-accept worker dispatches by the
# stored kind, not by re-deriving the tool name.
_PROPOSAL_KIND_BY_TOOL = {
    "propose_correction": "correction",
    "propose_change": "change",
    "propose_self_revision": "self_revision",
}

# Hard caps so a runaway narrative can never write an unbounded queue row. The
# rationale is the in-voice "why"; the diff is the structured proposal.
_MAX_RATIONALE_CHARS = 8192
_MAX_DIFF_CHARS = 65536


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _writeback(ctx: ToolContext) -> WritebackContext | None:
    """The per-run write surface (pg_pool + AnalystContext) the actor injects into
    this pack's binding. A propose tool NEEDS it to stamp the queue row with the
    run that raised the proposal; absent → a clean ``failed`` ToolResult (no
    silent no-op, no un-stamped write). NOTE: it carries the connection source +
    the run identity ONLY — no provenance writer is reached from here."""
    wb = ctx.writeback
    if wb is None or wb.pg_pool is None or wb.analyst_ctx is None:
        return None
    return wb


def _coerce_refs(raw: Any) -> list[UUID]:
    """Parse the OPTIONAL ``cited_substrate_refs`` arg into a UUID list (the
    lineage warrant). Unlike propose_facts (where lineage is mandatory), a
    proposal MAY cite zero refs — a correction the journal noticed from its own
    run health legitimately has no single substrate id. Non-UUID entries are
    dropped silently (best-effort warrant), never raising."""
    if raw is None:
        return []
    items = raw if isinstance(raw, (list, tuple)) else [raw]
    out: list[UUID] = []
    for it in items:
        s = str(it).strip()
        if not s:
            continue
        try:
            out.append(UUID(s))
        except (ValueError, TypeError):
            continue
    return out


def _coerce_diff(raw: Any) -> tuple[dict[str, Any] | None, str | None]:
    """Coerce the ``diff`` arg into a JSON object (the structured proposal).

    Accepts a dict or a JSON string. Returns ``(diff, None)`` on success or
    ``(None, error)`` when missing / unparseable / not an object / oversized.
    The diff is the human-reviewable payload (a fact triple, a descriptor patch,
    a prompt diff), so it MUST be structured, not free text."""
    if raw is None:
        return None, "diff is required (the structured proposal payload)"
    diff = raw
    if isinstance(diff, str):
        try:
            diff = json.loads(diff)
        except json.JSONDecodeError as exc:
            return None, f"diff is not valid JSON: {exc}"
    if not isinstance(diff, dict):
        return None, "diff must be a JSON object"
    try:
        encoded = json.dumps(diff)
    except (TypeError, ValueError) as exc:
        return None, f"diff is not JSON-serialisable: {exc}"
    if len(encoded) > _MAX_DIFF_CHARS:
        return None, f"diff exceeds {_MAX_DIFF_CHARS} chars"
    return diff, None


async def _insert_proposal(
    wb: WritebackContext,
    *,
    proposal_kind: str,
    rationale: str,
    diff: dict[str, Any],
    cited_refs: list[UUID],
    requested_by: str | None,
    pack_id: str,
) -> UUID:
    """The ONE write this whole pack performs: a single parameterised INSERT into
    ``journal_proposals`` (status defaults to 'pending'). NO provenance writer, NO
    live substrate table. Stamped with the per-run AnalystContext (the journal run
    that raised it) so the operator review surface can trace the proposal to its
    source run. Returns the new proposal id.
    """
    ctx = wb.analyst_ctx
    async with wb.pg_pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO journal_proposals (
                proposal_kind,
                proposed_by_analyst_id,
                run_id,
                rationale,
                diff,
                cited_substrate_refs,
                status
            )
            VALUES ($1, $2, $3, $4, $5::jsonb, $6::uuid[], 'pending')
            RETURNING id
            """,
            proposal_kind,
            ctx.analyst_id,
            ctx.run_id,
            rationale[:_MAX_RATIONALE_CHARS],
            json.dumps(diff),
            list(cited_refs),
        )
    proposal_id = row["id"]
    logger.info(
        "journal_propose.queued kind=%s analyst=%s run=%s id=%s requested_by=%s "
        "pack=%s refs=%d",
        proposal_kind, ctx.analyst_id, ctx.run_id, proposal_id, requested_by,
        pack_id, len(cited_refs),
    )
    return proposal_id


async def _propose(
    call: ToolCall, pack: ActionPack, ctx: ToolContext, tool_name: str
) -> ToolResult:
    """The shared propose path: validate the args, then write ONE pending
    ``journal_proposals`` row. Every ``propose_*`` tool funnels through here so
    the queue-only guarantee lives in exactly ONE place."""
    wb = _writeback(ctx)
    if wb is None:
        return ToolResult(
            status="failed",
            error=f"no writeback surface wired for {tool_name} (ctx.writeback is None)",
        )
    proposal_kind = _PROPOSAL_KIND_BY_TOOL[tool_name]

    args = call.args
    rationale = str(args.get("rationale", "")).strip()
    if not rationale:
        return ToolResult(
            status="failed",
            error=f"{tool_name} requires a non-empty 'rationale' (your in-voice 'why')",
        )
    diff, diff_err = _coerce_diff(args.get("diff"))
    if diff_err is not None:
        return ToolResult(status="failed", error=diff_err)
    assert diff is not None  # _coerce_diff guarantees this when diff_err is None
    cited_refs = _coerce_refs(args.get("cited_substrate_refs"))

    try:
        proposal_id = await _insert_proposal(
            wb,
            proposal_kind=proposal_kind,
            rationale=rationale,
            diff=diff,
            cited_refs=cited_refs,
            requested_by=call.requested_by,
            pack_id=pack.identity.id,
        )
    except Exception as exc:  # noqa: BLE001 — a queue write failure folds into the loop
        logger.warning("journal_propose.insert_failed tool=%s err=%s", tool_name, exc)
        return ToolResult(status="failed", error=f"proposal_insert_failed: {exc!s}")

    return ToolResult(
        status="completed",
        output={
            "proposal_id": str(proposal_id),
            "proposal_kind": proposal_kind,
            "status": "pending",
            "note": (
                "queued for human review — the journal suggested; a human decides. "
                "No live table was written."
            ),
        },
        units=1,
    )


# ---------------------------------------------------------------------------
# The three propose tools. Each is a thin wrapper over the shared path — the
# proposal_kind is selected by the tool name, NOT a free arg, so the agent cannot
# mislabel a self_revision as a correction to dodge the §7.5 protected-section
# gate (the gate runs on the stored kind at accept time).
# ---------------------------------------------------------------------------


async def propose_correction_tool(
    call: ToolCall, pack: ActionPack, ctx: ToolContext
) -> ToolResult:
    """Propose a CORRECTION to the substrate — a stale fact to supersede, an
    entity merge, or a situation correction.

    ``args``:
      * ``rationale`` (required) — the in-voice "why I think this is stale/wrong".
      * ``diff`` (required) — the structured correction. Shape depends on the
        sub-kind (operator-reviewed), e.g.
        ``{"op": "supersede_fact", "subject": ..., "predicate": ..., "value": ...}``
        / ``{"op": "merge_entities", "from": ..., "into": ...}`` /
        ``{"op": "correct_situation", "situation_id": ..., "patch": {...}}``.
      * ``cited_substrate_refs`` (optional) — the lineage warrant.

    Writes ONE pending ``journal_proposals`` row (proposal_kind='correction').
    NEVER a live ``facts`` / ``entities`` / ``situations`` row — the operator's
    accept-apply worker performs the actual supersession on accept.
    """
    return await _propose(call, pack, ctx, "propose_correction")


async def propose_change_tool(
    call: ToolCall, pack: ActionPack, ctx: ToolContext
) -> ToolResult:
    """Propose a descriptor / config CHANGE — e.g. "country_critic cadence looks
    halved; bump cooldown below the interval".

    ``args``:
      * ``rationale`` (required) — the in-voice "why".
      * ``diff`` (required) — the structured descriptor/stack patch, e.g.
        ``{"op": "update_descriptor", "family": "analyst", "descriptor_id": ...,
        "patch": {...}}`` or ``{"op": "update_stack", "stack_id": ...,
        "patch": {...}}``.
      * ``cited_substrate_refs`` (optional) — the warrant (e.g. the run-health
        observation that flagged the halved cadence).

    Writes ONE pending ``journal_proposals`` row (proposal_kind='change'). NEVER a
    descriptor/stack mutation — the operator's worker applies it via
    ``PUT /stack/{id}`` / ``PUT /descriptors/{family}/{id}`` on accept.
    """
    return await _propose(call, pack, ctx, "propose_change")


async def propose_self_revision_tool(
    call: ToolCall, pack: ActionPack, ctx: ToolContext
) -> ToolResult:
    """Propose a diff to the journal's OWN instruction/system prompt — THE
    HIGHEST-SCRUTINY CLASS (§7.5). A self-modification loop on the most PERSUASIVE
    surface in the system, so a human ALWAYS gates it (never a direct self-edit).

    ``args``:
      * ``rationale`` (required) — the in-voice "why I want to change my own
        instructions" (the operator reviews this ALONGSIDE objective evidence —
        the recent critic/calibration of the journal's own entries — not in
        isolation; see the accept API §7.5(a)).
      * ``diff`` (required) — the prompt diff, e.g.
        ``{"op": "revise_prompt", "target_analyst_id": "journal_assessor",
        "new_prompt_text": "...", "summary": "..."}``. A diff that touches the
        prompt's grounding / honesty / anti-self-confirmation clauses is a
        PROTECTED SECTION and is AUTO-REJECTED at accept time (§7.5(b)) — the
        gate runs server-side on the stored diff, not here.
      * ``cited_substrate_refs`` (optional) — the warrant.

    Writes ONE pending ``journal_proposals`` row (proposal_kind='self_revision').
    NEVER edits a prompt directly — on accept the operator's worker routes it
    through the optimizer's champion-promotion path.
    """
    return await _propose(call, pack, ctx, "propose_self_revision")


def register_journal_propose_tools(registry: Any) -> None:
    """Register the journal_propose pack's three handlers (called by
    ``default_tool_registry``). One tool name → one global handler; the pack is
    the GRANT/governance boundary, not a handler copy."""
    registry.register("propose_correction", propose_correction_tool)
    registry.register("propose_change", propose_change_tool)
    registry.register("propose_self_revision", propose_self_revision_tool)


__all__ = [
    "JOURNAL_PROPOSE_PACK_ID",
    "JOURNAL_PROPOSE_TOOLS",
    "propose_correction_tool",
    "propose_change_tool",
    "propose_self_revision_tool",
    "register_journal_propose_tools",
]
