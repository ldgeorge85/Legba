# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Finish a cut consult run — one synthesis call, no new drilling.

This is the recovery half of the c8a0105c fix. The loop-side half stops runs
from being cut so often and delivers the partial when they are; this half lets
an operator take a run that WAS cut and get the answer its evidence already
paid for.

The contract is narrow on purpose:

* **No drilling.** The tools are never offered. The only thing this path is
  allowed to do is write an answer over evidence that already exists.
* **The same prompt.** It sends the messages the loop itself sent, from the
  transcript that run recorded — not a fresh prompt over a digest. The wording
  of the system turn and the forcing instruction come from
  ``consult_transcript``, which the loop also calls, so there is one definition
  and the identity claim is checkable rather than aspirational.
* **The fidelity is stated.** ``exact`` means the recorded transcript was
  replayed verbatim. ``rebuilt`` means it was reconstructed by re-executing the
  recorded tool calls, and the response carries a sentence saying so and saying
  what could not be recovered. There is no third, quieter option.

Runs persisted before this change get ``rebuilt``. That is not a small
asterisk: their trace holds tool names, TRIMMED args and a small result digest,
and never held the assistant's prose between rounds or the bounded bodies the
model actually read. Re-executing the calls recovers the evidence; nothing
recovers the prose. Both facts travel with the answer.

It runs in the ANALYST, not the registry, because that is where the tools, the
governed binding and the model planes are. The front door reaches it by putting
``synthesize_from`` on the request row — the same actor method, no new
actor-side surface.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any
from uuid import UUID

from . import consult_transcript as _tx

logger = logging.getLogger(__name__)

#: Fidelity labels. ``unavailable`` means neither path could produce a prompt —
#: no transcript and no re-executable calls — and the caller must say so rather
#: than sending the model a question with no evidence attached.
FIDELITY_EXACT = "exact"
FIDELITY_REBUILT = "rebuilt"
FIDELITY_UNAVAILABLE = "unavailable"


async def run_synthesis_only(
    recovery: Mapping[str, Any],
    *,
    deps: Any,
    active_llm: Any,
    wire: str,
    system_prompt: str,
    record: Any,
    analyst_id: str | None,
) -> Any:
    """One synthesis turn over a persisted run's evidence.

    ``recovery`` is the persisted turn's evidence as the front door read it:
    ``question``, ``steps``, ``cited_refs``, and — for a run recorded after
    this change — ``replay_transcript``.

    Returns the kind's ordinary :class:`AnalystMethodResult`, so every consumer
    downstream (the finding wrapper, the front-door projection, the turn
    writer) is unchanged. What is new is on the payload's data bag:
    ``replay_fidelity`` and ``replay_note``.
    """
    # Imported here, not at module scope: ``consult_on_demand`` reaches THIS
    # module from inside its own run path, and a module-level import back would
    # close the cycle. One direction at import time.
    from .consult_on_demand import (
        FINAL_SENTINEL,
        AnalystMethodResult,
        _build_consult_response,
        _coerce_uuid_list,
        _reason_with_response,
        _render_user_prompt,
        _run_one_call,
        _wrap_as_finding,
        final_payload_from_text,
    )

    question = str(recovery.get("question") or "")
    steps = list(recovery.get("steps") or [])
    scope_predicate = recovery.get("scope_predicate")
    collected_refs: list[UUID] = _coerce_uuid_list(recovery.get("cited_refs") or [])

    # --- Resolve the prompt: exact first, rebuilt second -------------------
    exact = _tx.replay_request(recovery.get("replay_transcript") or {})
    drift: list[dict[str, Any]] = []
    if exact is not None:
        fidelity = FIDELITY_EXACT
        force_system, messages = exact
        note = "replayed the recorded synthesis prompt verbatim"
        calls_replayed = 0
    else:
        force_system = _tx.synthesis_system(system_prompt, FINAL_SENTINEL)

        async def _run_tool(name: str, args: Mapping[str, Any]) -> Any:
            result, _meta = await _run_one_call(
                deps,
                tool_name=name,
                tool_args=args,
                scope_predicate=scope_predicate,
                analyst_id=analyst_id,
            )
            return result

        rebuilt = await _tx.rebuild_transcript(
            steps,
            run_tool=_run_tool,
            user_prompt=_render_user_prompt(question, scope_predicate, ""),
            wire=wire,
        )
        if not rebuilt.calls_replayed:
            fidelity = FIDELITY_UNAVAILABLE
        else:
            fidelity = FIDELITY_REBUILT
        # Unconditional, because it is unconditionally true: neither the
        # operator's pinned records nor the scope predicate is persisted on a
        # turn, so a rebuilt prompt carries neither. A run whose answer leaned
        # on a pinned document will notice; saying so is the only honest move
        # available until those are persisted too.
        rebuilt.missing.append(
            "pinned records and scope predicate are not persisted on a turn, "
            "so the replayed prompt carries neither"
        )
        messages = rebuilt.messages + [
            {"role": "user", "content": _tx.synthesis_instruction(FINAL_SENTINEL)},
        ]
        note = rebuilt.note()
        drift = [d.as_dict() for d in rebuilt.drift if d.drifted]
        calls_replayed = rebuilt.calls_replayed

    await record({
        "phase": "plan",
        "kind": "resynthesis",
        "fidelity": fidelity,
        "calls_replayed": calls_replayed,
        "drifted_calls": len(drift),
        "note": note,
    })

    if fidelity == FIDELITY_UNAVAILABLE:
        # No prompt could be built. Say that; do not ask the model to answer
        # from nothing and present the result as a recovery.
        consult = _build_consult_response(
            question=question,
            final_payload={
                "final": True,
                "answer": (
                    "**This run cannot be re-synthesised.** Its persisted turn "
                    "carries neither a recorded synthesis prompt nor any "
                    "re-executable tool calls, so there is no evidence to write "
                    "an answer over. Re-ask the question to gather it again."
                ),
                "uncertainty": 1.0,
                "unanswered_aspects": [question],
            },
            collected_refs=collected_refs,
            rounds_used=0,
            forced_final=True,
            subprovider=getattr(active_llm, "subprovider", None),
            extra_data={
                "replay_fidelity": fidelity,
                "replay_note": note,
                "synthesis_status": "none",
                "resynthesizable": False,
            },
        )
        return AnalystMethodResult(
            finding=_wrap_as_finding(consult, analyst_id=analyst_id),
            consult_response=consult,
            usage={"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0},
            derived_from=list(collected_refs),
            intermediate_steps=steps,
        )

    # --- The one call. Tools are never offered on this path. ---------------
    content, usage, _response = await _reason_with_response(
        active_llm,
        messages=messages,
        max_tokens=deps.max_tokens,
        temperature=deps.temperature,
        system_prompt=force_system,
    )
    await record({
        "phase": "reason",
        "kind": "resynthesis_final",
        "tokens": usage.get("prompt_tokens", 0) + usage.get("completion_tokens", 0),
    })

    final_payload = final_payload_from_text(content)
    if not final_payload.get("answer"):
        final_payload = None
    consult = _build_consult_response(
        question=question,
        final_payload=final_payload,
        collected_refs=collected_refs,
        rounds_used=0,
        forced_final=True,
        subprovider=getattr(active_llm, "subprovider", None),
        extra_data={
            "replay_fidelity": fidelity,
            "replay_note": note,
            "replay_drift": drift,
            "synthesis_status": "complete" if final_payload else "none",
            "resynthesizable": final_payload is None,
            "steps": steps,
            "tool_calls": list(recovery.get("tool_calls") or []),
        },
    )
    return AnalystMethodResult(
        finding=_wrap_as_finding(consult, analyst_id=analyst_id),
        consult_response=consult,
        usage=usage,
        derived_from=list(collected_refs),
        intermediate_steps=steps,
    )


__all__ = [
    "FIDELITY_EXACT",
    "FIDELITY_REBUILT",
    "FIDELITY_UNAVAILABLE",
    "run_synthesis_only",
]
