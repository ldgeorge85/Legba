# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""H10 (docs/V3_IMPLEMENTATION_PLAN.md §H) — the journal's PROPOSE phase,
extracted verbatim from ``journal_assessor.py`` when H10's write-time shape
validator pushed the module 9 lines past its 3,000-line size-gate ceiling
(the module-size gate is honoured by SPLITTING, never by raising a ceiling —
the section banner below was already the seam the plan's own comment names).
Behavior is byte-identical to the pre-extraction code — same constants, same
prompt text, same branches — and ``journal_assessor`` re-exports every name,
so every existing import path (direct or via the ``journal_assessor`` module
attribute) keeps working unchanged.

Moved: the PROPOSE phase (``_propose_phase`` / ``_propose_phase_prompt``) and
everything ONLY it uses — the round/cap/ref-echo constants, the decline
instruction, the H10 shape-discipline text, and
``_JOURNAL_PROPOSE_TOOL_SCHEMAS`` (also read by ``journal_assessor``'s
``_journal_gather_catalog`` for the GATHER phase's WRITE-BACK section, hence
the re-export rather than a private move).

Left behind in ``journal_assessor.py``: ``_journal_gather_catalog`` itself
(the GATHER catalog is a sibling concern — read tools + the write-back
preview — not part of the PROPOSE turn), and the call site that invokes
``_propose_phase`` from the entry/consolidation run flow.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Mapping
from uuid import UUID

from .agency.journal_propose import JOURNAL_PROPOSE_TOOLS
from .inline_target import InlineTargetDeps, _reason_via_llm

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# PROPOSE (§7 Wave 4) — the phase the propose pack never had (W1-C)
# ---------------------------------------------------------------------------
# THE DEFECT THIS FIXES (engine-review p5, 2026-08-02): ``journal_propose`` was
# granted to two analysts, registered active, bound end-to-end by dapr_host +
# the actor's ``_gather_write_bindings_for_target`` META self-allow, and
# catalogued in the GATHER prompt since 0875b7d — with **0 invocations EVER**.
# Live proof at diagnosis (read-only psql): ``action_pack_invocations`` carried
# only journal_read/substrate_read/escalate_finding; ``governor_events`` had
# not ONE row for the pack under any decision — so the model never named a
# propose tool and got blocked, it never named one at all; ``journal_proposals``
# = 0; no journal trace mentions "propose". The wiring was never the problem.
#
# THE CAUSE IS PHASE PLACEMENT, and it cuts both ways:
#   * Propose was offered ONLY in GATHER — a phase framed "Before you write the
#     entry you may FIRST query the substrate … Do not write the entry yet". At
#     GATHER the model has read nothing and formed no judgment, so it has
#     nothing to propose; we asked before the reasoning happened.
#   * At NARRATE — the one phase where it HAS reasoned and would know "that
#     leader fact is stale" — propose is absent from the catalog,
#     ``_narrate_with_tools`` dispatches ``JOURNAL_READ_TOOLS`` and nothing
#     else, and propose-shaped JSON is caught by
#     :func:`_guard_against_tool_call_leak` as a LEAK: hard "prose only" retry,
#     then :class:`NarrateToolCallLeakError` — a FAILED run. The model was
#     structurally punished for proposing at the only moment it could.
#
# So: a third phase, AFTER the body is final and REFLECT has bound its
# citations, showing the model its own entry + resolved refs + the pack's
# guidance, and asking the question no other phase asks — with a cheap no.
#
# THE INVARIANT IS UNCHANGED (§7.5): a propose_* call writes ONE ``pending``
# ``journal_proposals`` row and nothing else. This adds a *moment*, never a
# permission — every call still goes through ``binding.run_tool`` →
# ``Agency.run_pack_tool`` → resolve ∩ allow ∩ applicability → governor → the
# ``action_pack_invocations`` ledger, on the actor's per-run WritebackContext.
# The journal SUGGESTS; a human CAUSES.

#: Turns the PROPOSE phase may spend. Each is one LLM call that names a propose
#: tool or declines; a decline (or anything unparsable) ends the phase. Small by
#: intent — a coda, not a second ReAct loop.
_PROPOSE_MAX_ROUNDS = 3

#: Hard ceiling on proposals ONE run may queue. The pack governor's 60/hour is
#: the fleet-wide bound; this is the per-entry one. "Do NOT propose lightly"
#: (the pack's own rule) needs an enforcer that is not a sentence in a prompt.
_PROPOSE_MAX_PER_RUN = 2

#: Cap on refs echoed into the propose prompt as the legal warrant vocabulary.
_PROPOSE_REF_ECHO_CAP = 25

_PROPOSE_DECLINE_INSTRUCTION = (
    "\n\nIf nothing further warrants a proposal, reply with exactly: "
    '{"propose": false}'
)

# H10 (make proposals applicable by construction, plan §7.4): the apply worker
# (journal_proposals_apply.py's ``validate_proposal_shape``) recognises EXACTLY
# three diff shapes and nothing else — a proposal outside them used to reach
# 'pending' anyway and then fail an ACCEPT with a ProposalApplyError (all 30
# pending proposals of 2026-08/09 were free-form and archived that way). The
# write-time validator now catches the same gap before it is ever queued
# 'pending' — but the SHAPE has to be named here too, or the model has no way
# to hit it on the first try. ONE concrete example per shape, and the explicit
# fallback: a diff that fits none of them is an observation, never a proposal.
_PROPOSE_SHAPE_DISCIPLINE = (
    "A proposal's diff MUST fit exactly ONE of the three shapes the apply "
    "worker recognises — anything else is archived unread, never reviewed by "
    "a human:\n"
    '  1. correction / supersede_fact — e.g. {"op": "supersede_fact", '
    '"subject": "<subject>", "predicate": "<predicate>", "value": "<the '
    'corrected value>"}. Needs an OPEN fact for that exact subject+predicate '
    "already in the substrate (you are correcting something real, not "
    "minting a new claim), and a cited_substrate_refs entry that is a real "
    "ref your read tools returned — never a correction recalled from your "
    "own memory (the 'the president is X' / 'the king is alive' failure "
    "mode).\n"
    '  2. change / update_descriptor or update_stack — e.g. {"op": '
    '"update_descriptor", "family": "analyst", "descriptor_id": "<id>", '
    '"patch": {"cadence": {"cooldown_seconds": 21000}}} or {"op": '
    '"update_stack", "stack_id": "<id>", "patch": {...}}.\n'
    '  3. self_revision / revise_prompt — e.g. {"op": "revise_prompt", '
    '"target_analyst_id": "<your own analyst id>", "new_prompt_text": "<the '
    'full revised prompt>", "summary": "<why>"}.\n'
    "A diff that fits NONE of these three shapes is not a proposal — it is "
    "an OBSERVATION. Write it as prose in the entry instead (with its own "
    "[[ref:...]] citation, or [[spec]]/[[inference]] where it warrants one); "
    "do not call a propose tool for it."
)

# H10-3/H10-4 — one-line schema + ONE concrete example diff per propose tool.
# Read by BOTH this module's own ``_propose_phase_prompt`` (below) and
# ``journal_assessor._journal_gather_catalog``'s WRITE-BACK section (hence the
# re-export from ``journal_assessor``, not a private module-local name).
_JOURNAL_PROPOSE_TOOL_SCHEMAS: dict[str, str] = {
    "propose_correction": (
        "propose_correction(rationale, diff, [cited_substrate_refs]) — "
        "propose a correction (a stale fact to supersede / an entity merge / "
        "a situation fix). Example diff: "
        '{"op": "supersede_fact", "subject": "<subject>", "predicate": '
        '"<predicate>", "value": "<the corrected value>"}. Queues ONE '
        "journal_proposals row; NEVER a live write."
    ),
    "propose_change": (
        "propose_change(rationale, diff, [cited_substrate_refs]) — propose a "
        "descriptor/config change. Example diff: "
        '{"op": "update_descriptor", "family": "analyst", "descriptor_id": '
        '"<id>", "patch": {"cadence": {"cooldown_seconds": 21000}}}. Queues '
        "ONE journal_proposals row; NEVER a live write."
    ),
    "propose_self_revision": (
        "propose_self_revision(rationale, diff, [cited_substrate_refs]) — "
        "propose a diff to YOUR OWN system prompt (the highest-scrutiny "
        "class — protected sections auto-reject at accept time). Example "
        'diff: {"op": "revise_prompt", "target_analyst_id": "<your own '
        'analyst id>", "new_prompt_text": "<the full revised prompt>", '
        '"summary": "<why>"}. Queues ONE journal_proposals row; NEVER a '
        "direct self-edit."
    ),
}


def _propose_phase_prompt(
    *, body: str, cited_refs: list[UUID], write_fragments: Any,
) -> str:
    """Build the PROPOSE turn's user prompt: the entry just written, the pack's
    operator-authored guidance, the tool schemas, and the legal warrant
    vocabulary — this entry's OWN resolved refs. The pack rule is "cite only
    UUIDs your read tools returned"; handing the model exactly those is the
    anti-fabrication anchor."""
    lines = [
        "YOU HAVE JUST WRITTEN THIS ENTRY:",
        "",
        body.strip(),
        "",
        "Now — and only now, having reasoned it through — consider whether "
        "anything in it warrants a PROPOSAL. A proposal is a suggestion "
        "queued for a human to review; it changes NOTHING by itself, and it "
        "is never a fact write.",
    ]
    frags = [str(f).strip() for f in (write_fragments or []) if str(f).strip()]
    if frags:
        lines.append("")
        lines.extend(frags)
    lines += ["", "Available proposal tools:"] + [
        "  - " + _JOURNAL_PROPOSE_TOOL_SCHEMAS.get(
            name, f"{name}(...) — journal propose tool (see persona)."
        )
        for name in JOURNAL_PROPOSE_TOOLS
    ]
    lines += ["", _PROPOSE_SHAPE_DISCIPLINE]
    if cited_refs:
        lines += ["", (
            "Refs this entry actually resolved (the ONLY UUIDs you may put in "
            "cited_substrate_refs — never invent one): "
            + ", ".join(str(r) for r in cited_refs[:_PROPOSE_REF_ECHO_CAP])
        )]
    lines += ["", (
        "Reply with EITHER a single strict-JSON tool call — "
        '{"tool": "<name>", "args": {"rationale": "...", "diff": {...}, '
        '"cited_substrate_refs": ["..."]}} — OR, if nothing warrants one, '
        'exactly {"propose": false}. Most entries warrant nothing; declining '
        "is the normal answer and costs you nothing."
    )]
    return "\n".join(lines)


async def _propose_phase(
    deps: InlineTargetDeps,
    *,
    body: str,
    cited_refs: list[UUID],
    analyst_id: str | None,
    tool_bindings: Mapping[str, Any],
    write_fragments: Any,
    steps: list[dict[str, Any]],
) -> dict[str, int]:
    """Offer the journal_propose pack at the ONE moment the model has a formed
    judgment: right after its entry is final and its citations are bound.

    Every admitted call runs through the pack's OWN per-run binding out of
    ``options['gather_tool_bindings']`` — the object the actor built via
    ``_gather_write_bindings_for_target`` (META self-allow + WritebackContext),
    the same one GATHER routes write tools through. No second dispatch path, no
    hand-built allow: an unbound propose tool is a LOUD no-op, never an
    ungoverned write. DEGRADE-NOT-DROP throughout — an LLM error, unparsable
    reply, or blocked/failing tool must never fail a run whose entry is already
    written. Returns the phase's token usage for the caller to fold.
    """
    usage_total = {"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0}
    from .inline_target import _extract_json

    messages: list[Mapping[str, Any]] = [
        {
            "role": "user",
            "content": _propose_phase_prompt(
                body=body, cited_refs=cited_refs, write_fragments=write_fragments
            ),
        }
    ]
    queued = 0
    for round_idx in range(_PROPOSE_MAX_ROUNDS):
        try:
            content, usage = await _reason_via_llm(
                deps.llm,
                user_prompt="",
                max_tokens=deps.max_tokens,
                temperature=deps.temperature,
                system_prompt=deps.system_prompt,
                messages=messages,
            )
        except Exception as exc:  # degrade-not-drop — the entry is already written
            logger.warning(
                "journal_assessor.propose.llm_failed analyst_id=%s round=%d err=%s",
                analyst_id, round_idx + 1, exc,
            )
            steps.append({"phase": "propose", "kind": "llm_error", "round": round_idx + 1})
            break
        for k in usage_total:
            usage_total[k] += usage.get(k, 0)
        parsed = _extract_json(content or "")
        tool_name = str(parsed.get("tool")) if isinstance(parsed, dict) else ""
        if tool_name not in JOURNAL_PROPOSE_TOOLS:
            # The normal, expected ending: nothing warranted a proposal (or the
            # model said something that is not a proposal, which means the same).
            steps.append({
                "phase": "propose",
                "kind": "declined",
                "round": round_idx + 1,
                "queued": queued,
            })
            break
        binding = tool_bindings.get(tool_name)
        if binding is None:
            # Granted-and-catalogued but unbound: the pack was shown and cannot
            # be called. Loud, because it means the host/actor binding legs
            # disagree with the prompt surface — the exact silent-bypass shape
            # this whole phase exists to stop being invisible.
            logger.warning(
                "journal_assessor.propose.unbound analyst_id=%s tool=%s — the "
                "propose catalog was shown but no binding was wired for it",
                analyst_id, tool_name,
            )
            steps.append({
                "phase": "propose", "kind": "unbound", "tool": tool_name,
                "round": round_idx + 1,
            })
            break
        tool_args = parsed.get("args") or {}
        if not isinstance(tool_args, Mapping):
            tool_args = {}
        admitted = False
        detail: str = ""
        try:
            outcome = await binding.run_tool(tool_name, dict(tool_args))
            admitted = bool(outcome.admitted)
            if not admitted:
                detail = f"blocked: {outcome.block_cause}"
            elif outcome.tool_result is None or outcome.tool_result.status == "failed":
                admitted = False
                detail = (
                    f"failed: {outcome.tool_result.error}"
                    if outcome.tool_result is not None
                    else "failed: tool produced no result"
                )
        except Exception as exc:  # degrade-not-drop
            detail = f"failed: {exc!s}"
        steps.append({
            "phase": "propose",
            "kind": "tool_call",
            "round": round_idx + 1,
            "tool": tool_name,
            "admitted": admitted,
            **({"detail": detail} if detail else {}),
        })
        if admitted:
            queued += 1
            logger.info(
                "journal_assessor.propose.queued analyst_id=%s tool=%s queued=%d "
                "(pending human review — no live write)",
                analyst_id, tool_name, queued,
            )
        if queued >= _PROPOSE_MAX_PER_RUN:
            steps.append({"phase": "propose", "kind": "per_run_cap", "queued": queued})
            break
        messages = messages + [
            {"role": "assistant", "content": content or ""},
            {
                "role": "tool",
                "name": tool_name,
                "content": json.dumps(
                    {"queued": admitted, "detail": detail or "pending human review"}
                ),
            },
            {"role": "user", "content": _PROPOSE_DECLINE_INSTRUCTION.strip()},
        ]
    else:
        steps.append({"phase": "propose", "kind": "rounds_exhausted", "queued": queued})
    return usage_total


__all__ = [
    "_JOURNAL_PROPOSE_TOOL_SCHEMAS",
    "_PROPOSE_DECLINE_INSTRUCTION",
    "_PROPOSE_MAX_PER_RUN",
    "_PROPOSE_MAX_ROUNDS",
    "_PROPOSE_REF_ECHO_CAP",
    "_PROPOSE_SHAPE_DISCIPLINE",
    "_propose_phase",
    "_propose_phase_prompt",
]
